import requests
import pandas as pd
import numpy as np
from datetime import datetime
import time
import concurrent.futures
from telegram import Bot
from telegram.constants import ParseMode
import json
import pickle
from ratelimit import limits, sleep_and_retry
from binance.client import Client
from typing import Dict, List, Tuple
import logging
from scipy import stats
import configparser
import schedule
from PIL import Image, ImageDraw, ImageFont
import io
import textwrap
import os
import asyncio
import re

# 加载配置文件
config = configparser.ConfigParser()
config.read('config.ini')

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Binance API 端点
SPOT_BASE_URL = "https://api.binance.com/api/v3"
FUTURES_BASE_URL = "https://fapi.binance.com/fapi/v1"

# DeepSeek API 配置
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = config.get('API', 'DEEPSEEK_API_KEY')  # 从配置文件读取

# Binance 客户端初始化
BINANCE_API_KEY = config.get('API', 'BINANCE_API_KEY')  # 从配置文件读取
BINANCE_API_SECRET = config.get('API', 'BINANCE_API_SECRET')  # 从配置文件读取
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# 固定交易对
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SUIUSDT', 'TONUSDT', 'PNUTUSDT']


def format_number(value):
    """将数值格式化为K/M表示，保留两位小数"""
    if abs(value) >= 1000000:
        return f"{value / 1000000:.2f}M"
    elif abs(value) >= 1000:
        return f"{value / 1000:.2f}K"
    else:
        return f"{value:.2f}"


@sleep_and_retry
@limits(calls=20, period=1)
def get_klines_data(symbol: str, interval: str = '5m', limit: int = 50, is_futures: bool = False) -> List[Dict]:
    """获取K线数据，并剔除最新的一根（未完成的）
    
    根据Binance API文档获取K线数据：
    - 现货: /api/v3/klines
    - 期货: /fapi/v1/klines
    
    参数:
        symbol: 交易对名称
        interval: K线周期 (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M)
        limit: 获取的K线数量，默认50，最大1500
        is_futures: 是否为期货
        
    返回:
        K线数据列表，已剔除最新的未完成K线
    """
    try:
        # 确定API基础URL和端点
        base_url = FUTURES_BASE_URL if is_futures else SPOT_BASE_URL
        endpoint = "/klines"
        
        # 检查并限制limit参数
        if limit > 1500:
            logger.warning(f"请求的limit({limit})超过最大值1500，已自动调整为1500")
            limit = 1500
            
        # 构建请求参数
        params = {
            'symbol': symbol, 
            'interval': interval, 
            'limit': limit + 1  # 多请求一根，以便剔除最新的未完成K线
        }
        
        # 发送请求获取K线数据
        logger.info(f"正在获取 {symbol} {'期货' if is_futures else '现货'} {interval} K线数据...")
        response = requests.get(f"{base_url}{endpoint}", params=params)
        
        # 检查请求是否成功
        if response.status_code != 200:
            logger.error(f"获取K线数据失败: HTTP {response.status_code}, {response.text}")
            return []
            
        data = response.json()

        # 检查返回的数据量
        if not data:
            logger.warning(f"{symbol} 未返回任何K线数据")
            return []
            
        if len(data) < 2:
            logger.warning(f"需要至少2根K线数据以剔除最新未完成K线，但{symbol}只返回了{len(data)}根")
            return []

        # 剔除最新的一根K线（未完成的）
        complete_klines = data[:-1]
        logger.info(f"成功获取 {symbol} K线数据: {len(complete_klines)}根 (已剔除最新未完成K线)")

        # 将K线数据转换为结构化格式
        results = []
        for k in complete_klines:
            # 将时间戳转换为可读时间格式
            open_time = datetime.fromtimestamp(k[0] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            close_time = datetime.fromtimestamp(k[6] / 1000).strftime('%Y-%m-%d %H:%M:%S')

            # 计算净流入资金 (买方成交量 - 卖方成交量)
            taker_buy_quote_volume = float(k[10])  # 主动买入成交额
            total_quote_volume = float(k[7])       # 总成交额
            taker_sell_quote_volume = total_quote_volume - taker_buy_quote_volume  # 主动卖出成交额
            net_inflow = taker_buy_quote_volume - taker_sell_quote_volume  # 净流入 = 买入 - 卖出

            results.append({
                'symbol': symbol,
                'open_time': open_time,
                'close_time': close_time,
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),              # 成交量(基础资产数量)
                'quote_volume': total_quote_volume, # 成交额(计价资产数量)
                'trades': int(k[8]),                # 成交笔数
                'taker_buy_base_volume': float(k[9]),           # 主动买入成交量
                'taker_buy_quote_volume': taker_buy_quote_volume, # 主动买入成交额
                'taker_sell_quote_volume': taker_sell_quote_volume, # 主动卖出成交额
                'net_inflow': net_inflow,           # 净流入资金
                'buy_ratio': taker_buy_quote_volume / total_quote_volume if total_quote_volume > 0 else 0,  # 买盘比例
                'timestamp': k[0]                   # 原始时间戳(毫秒)，用于排序
            })

        return results
    except Exception as e:
        logger.error(f"获取{symbol} K线数据时出错: {str(e)}")
        return []


@sleep_and_retry
@limits(calls=20, period=1)
def get_orderbook_stats(symbol: str, is_futures: bool = False, retries: int = 3) -> Dict:
    """获取单个交易对的深度统计数据（现货5000档，期货1000档）"""
    limit = 1000 if is_futures else 5000  # 期货支持最大1000档，现货支持5000档
    for attempt in range(retries):
        try:
            if is_futures:
                orderbook = client.futures_order_book(symbol=symbol, limit=limit)
                current_price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
            else:
                orderbook = client.get_order_book(symbol=symbol, limit=limit)
                current_price = float(client.get_symbol_ticker(symbol=symbol)['price'])

            bids = [(float(bid[0]), float(bid[1])) for bid in orderbook['bids']]
            asks = [(float(ask[0]), float(ask[1])) for ask in orderbook['asks']]

            # 计算买卖盘总量和总价值
            bids_volume = sum(amount for _, amount in bids)
            asks_volume = sum(amount for _, amount in asks)
            bids_value = sum(price * amount for price, amount in bids)
            asks_value = sum(price * amount for price, amount in asks)

            # 计算买卖盘不平衡度
            volume_imbalance = (bids_volume - asks_volume) / (bids_volume + asks_volume) if (
                                                                                                        bids_volume + asks_volume) > 0 else 0
            value_imbalance = (bids_value - asks_value) / (bids_value + asks_value) if (
                                                                                                   bids_value + asks_value) > 0 else 0

            # 计算关键价格区间内的买卖盘量
            price_range_pct = 0.005  # 当前价格上下0.5%范围
            lower_bound = current_price * (1 - price_range_pct)
            upper_bound = current_price * (1 + price_range_pct)

            near_bids_volume = sum(amount for price, amount in bids if price >= lower_bound)
            near_asks_volume = sum(amount for price, amount in asks if price <= upper_bound)
            near_volume_imbalance = (near_bids_volume - near_asks_volume) / (near_bids_volume + near_asks_volume) if (
                                                                                                                                 near_bids_volume + near_asks_volume) > 0 else 0

            return {
                'symbol': symbol,
                'price': current_price,
                'bids_count': len(bids),
                'asks_count': len(asks),
                'bids_volume': bids_volume,
                'asks_volume': asks_volume,
                'bids_value': bids_value,
                'asks_value': asks_value,
                'volume_imbalance': volume_imbalance,
                'value_imbalance': value_imbalance,
                'near_volume_imbalance': near_volume_imbalance
            }
        except Exception as e:
            logger.error(f"获取 {symbol} orderbook 失败 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return None
    return None


def analyze_funding_flow_trend(klines_data: List[Dict]) -> Dict:
    """分析资金流向趋势，预测价格所处阶段"""
    if not klines_data or len(klines_data) < 10:
        return {
            'trend': 'unknown',
            'confidence': 0,
            'description': '数据不足，无法分析'
        }

    # 按时间排序
    sorted_data = sorted(klines_data, key=lambda x: x['timestamp'])

    # 提取价格和资金流向数据
    prices = [k['close'] for k in sorted_data]
    net_inflows = [k['net_inflow'] for k in sorted_data]
    volumes = [k['quote_volume'] for k in sorted_data]

    # 计算价格趋势
    price_changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    price_trend = sum(1 for change in price_changes if change > 0) / len(price_changes)

    # 计算资金流向趋势
    inflow_changes = [net_inflows[i] - net_inflows[i - 1] for i in range(1, len(net_inflows))]
    inflow_trend = sum(1 for change in inflow_changes if change > 0) / len(inflow_changes)

    # 计算成交量趋势
    volume_changes = [volumes[i] - volumes[i - 1] for i in range(1, len(volumes))]
    volume_trend = sum(1 for change in volume_changes if change > 0) / len(volume_changes)

    # 计算价格与资金流向的相关性
    correlation = np.corrcoef(prices, net_inflows)[0, 1]

    # 计算资金流向与成交量的相关性
    inflow_volume_corr = np.corrcoef(net_inflows, volumes)[0, 1]

    # 计算价格波动率
    price_volatility = np.std(price_changes) / np.mean(prices) if np.mean(prices) != 0 else 0

    # 使用线性回归分析价格趋势
    x = np.arange(len(prices))
    slope, _, r_value, p_value, _ = stats.linregress(x, prices)
    price_trend_strength = abs(r_value)
    price_trend_direction = 'up' if slope > 0 else 'down'

    # 使用线性回归分析资金流向趋势
    inflow_slope, _, inflow_r_value, inflow_p_value, _ = stats.linregress(x, net_inflows)
    inflow_trend_strength = abs(inflow_r_value)
    inflow_trend_direction = 'increasing' if inflow_slope > 0 else 'decreasing'

    # 分析最近的资金流向变化
    recent_inflows = net_inflows[-10:]
    recent_inflow_trend = sum(1 for i in range(1, len(recent_inflows)) if recent_inflows[i] > recent_inflows[i - 1]) / (
                len(recent_inflows) - 1)

    # 根据各种指标判断价格所处阶段
    stage = 'unknown'
    confidence = 0
    reasons = []

    # 顶部特征
    if (price_trend > 0.7 and inflow_trend < 0.3 and correlation < -0.3):
        stage = 'top'
        confidence = min(0.7 + price_trend - inflow_trend - correlation, 0.95)
        reasons = [
            "价格持续上涨但资金流入减少",
            "价格与资金流向呈负相关",
            f"价格趋势强度: {price_trend_strength:.2f}, 资金流向趋势强度: {inflow_trend_strength:.2f}"
        ]

    # 底部特征
    elif (price_trend < 0.3 and inflow_trend > 0.7 and correlation < -0.3):
        stage = 'bottom'
        confidence = min(0.7 - price_trend + inflow_trend - correlation, 0.95)
        reasons = [
            "价格持续下跌但资金流入增加",
            "价格与资金流向呈负相关",
            f"价格趋势强度: {price_trend_strength:.2f}, 资金流向趋势强度: {inflow_trend_strength:.2f}"
        ]

    # 上涨中特征
    elif (price_trend > 0.6 and inflow_trend > 0.6 and correlation > 0.3):
        stage = 'rising'
        confidence = min(price_trend + inflow_trend + correlation - 1.0, 0.95)
        reasons = [
            "价格与资金流入同步增加",
            "价格与资金流向呈正相关",
            f"价格趋势强度: {price_trend_strength:.2f}, 资金流向趋势强度: {inflow_trend_strength:.2f}"
        ]

    # 下跌中特征
    elif (price_trend < 0.4 and inflow_trend < 0.4 and correlation > 0.3):
        stage = 'falling'
        confidence = min(1.0 - price_trend - inflow_trend + correlation, 0.95)
        reasons = [
            "价格与资金流入同步减少",
            "价格与资金流向呈正相关",
            f"价格趋势强度: {price_trend_strength:.2f}, 资金流向趋势强度: {inflow_trend_strength:.2f}"
        ]

    # 整理阶段特征
    elif (abs(price_trend - 0.5) < 0.15 and price_volatility < 0.01):
        stage = 'consolidation'
        confidence = 0.5 + (0.15 - abs(price_trend - 0.5)) * 3
        reasons = [
            "价格波动率低",
            "无明显趋势",
            f"价格波动率: {price_volatility:.4f}"
        ]

    # 其他情况，根据趋势强度判断
    else:
        if price_trend > 0.5:
            if inflow_trend > 0.5:
                stage = 'rising'
                confidence = (price_trend + inflow_trend) / 2
                reasons = ["价格和资金流向均呈上升趋势"]
            else:
                stage = 'weakening_rise'
                confidence = price_trend * (1 - inflow_trend)
                reasons = ["价格上升但资金流向减弱"]
        else:
            if inflow_trend < 0.5:
                stage = 'falling'
                confidence = (1 - price_trend + 1 - inflow_trend) / 2
                reasons = ["价格和资金流向均呈下降趋势"]
            else:
                stage = 'weakening_fall'
                confidence = (1 - price_trend) * inflow_trend
                reasons = ["价格下降但资金流向增强"]

    return {
        'trend': stage,
        'confidence': confidence,
        'description': f"价格可能处于{stage}阶段，置信度{confidence:.2f}",
        'reasons': reasons,
        'metrics': {
            'price_trend': price_trend,
            'price_trend_direction': price_trend_direction,
            'price_trend_strength': price_trend_strength,
            'price_trend_p_value': p_value,
            'inflow_trend': inflow_trend,
            'inflow_trend_direction': inflow_trend_direction,
            'inflow_trend_strength': inflow_trend_strength,
            'inflow_trend_p_value': inflow_p_value,
            'correlation': correlation,
            'inflow_volume_correlation': inflow_volume_corr,
            'price_volatility': price_volatility,
            'recent_inflow_trend': recent_inflow_trend
        }
    }


def detect_anomalies(klines_data: List[Dict]) -> List[Dict]:
    """检测资金流向和价格变动的异常"""
    if not klines_data or len(klines_data) < 5:
        return []

    anomalies = []

    # 按时间排序
    sorted_data = sorted(klines_data, key=lambda x: x['timestamp'])

    # 计算成交量和价格变化的均值和标准差
    volumes = [k['quote_volume'] for k in sorted_data]
    price_changes = [abs(sorted_data[i]['close'] - sorted_data[i]['open']) / sorted_data[i]['open']
                     for i in range(len(sorted_data))]

    vol_mean = np.mean(volumes)
    vol_std = np.std(volumes)
    price_change_mean = np.mean(price_changes)
    price_change_std = np.std(price_changes)

    # 检测异常
    for i, k in enumerate(sorted_data):
        # 成交量异常高但价格变化不大
        if (k['quote_volume'] > vol_mean + 2 * vol_std and
                price_changes[i] < price_change_mean + 0.5 * price_change_std):
            anomalies.append({
                'timestamp': k['open_time'],
                'type': 'high_volume_low_price_change',
                'symbol': k['symbol'],
                'volume': k['quote_volume'],
                'price_change': price_changes[i],
                'net_inflow': k['net_inflow']
            })

        # 价格变化异常大但成交量不高
        if (price_changes[i] > price_change_mean + 2 * price_change_std and
                k['quote_volume'] < vol_mean + 0.5 * vol_std):
            anomalies.append({
                'timestamp': k['open_time'],
                'type': 'high_price_change_low_volume',
                'symbol': k['symbol'],
                'volume': k['quote_volume'],
                'price_change': price_changes[i],
                'net_inflow': k['net_inflow']
            })

        # 资金净流入异常大
        if k['net_inflow'] > 0 and k['net_inflow'] > 0.7 * k['quote_volume']:
            anomalies.append({
                'timestamp': k['open_time'],
                'type': 'extreme_net_inflow',
                'symbol': k['symbol'],
                'volume': k['quote_volume'],
                'price_change': price_changes[i],
                'net_inflow': k['net_inflow'],
                'inflow_ratio': k['net_inflow'] / k['quote_volume'] if k['quote_volume'] > 0 else 0
            })

        # 资金净流出异常大
        if k['net_inflow'] < 0 and abs(k['net_inflow']) > 0.7 * k['quote_volume']:
            anomalies.append({
                'timestamp': k['open_time'],
                'type': 'extreme_net_outflow',
                'symbol': k['symbol'],
                'volume': k['quote_volume'],
                'price_change': price_changes[i],
                'net_inflow': k['net_inflow'],
                'outflow_ratio': abs(k['net_inflow']) / k['quote_volume'] if k['quote_volume'] > 0 else 0
            })

    return anomalies


def analyze_funding_pressure(klines_data: List[Dict], orderbook: Dict) -> Dict:
    """分析资金压力，结合K线数据和订单簿数据"""
    if not klines_data or not orderbook:
        return {
            'pressure': 'unknown',
            'direction': 'neutral',
            'strength': 0
        }

    # 按时间排序
    sorted_data = sorted(klines_data, key=lambda x: x['timestamp'])

    # 提取最近的资金流向数据
    recent_inflows = [k['net_inflow'] for k in sorted_data[-10:]]
    recent_volumes = [k['quote_volume'] for k in sorted_data[-10:]]

    # 计算资金流向占成交量的比例
    inflow_ratios = [inflow / volume if volume > 0 else 0 for inflow, volume in zip(recent_inflows, recent_volumes)]
    avg_inflow_ratio = np.mean(inflow_ratios)

    # 结合订单簿数据
    volume_imbalance = orderbook.get('volume_imbalance', 0)
    value_imbalance = orderbook.get('value_imbalance', 0)
    near_volume_imbalance = orderbook.get('near_volume_imbalance', 0)

    # 计算综合压力指标
    pressure_score = (
            avg_inflow_ratio * 0.4 +
            volume_imbalance * 0.2 +
            value_imbalance * 0.2 +
            near_volume_imbalance * 0.2
    )

    # 判断压力方向和强度
    if pressure_score > 0.1:
        pressure = 'buying'
        direction = 'bullish'
        strength = min(pressure_score * 5, 1.0)
    elif pressure_score < -0.1:
        pressure = 'selling'
        direction = 'bearish'
        strength = min(abs(pressure_score) * 5, 1.0)
    else:
        pressure = 'balanced'
        direction = 'neutral'
        strength = abs(pressure_score) * 5

    return {
        'pressure': pressure,
        'direction': direction,
        'strength': strength,
        'metrics': {
            'avg_inflow_ratio': avg_inflow_ratio,
            'volume_imbalance': volume_imbalance,
            'value_imbalance': value_imbalance,
            'near_volume_imbalance': near_volume_imbalance,
            'pressure_score': pressure_score
        }
    }


def send_to_deepseek(data):
    """将数据发送给DeepSeek API并获取解读"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = (
            "## Binance资金流向专业分析任务\n\n"
            "我已收集了Binance现货和期货市场过去50根5分钟K线的资金流向数据（已剔除最新未完成的一根），包括：\n"
            "- 各交易对的资金流向趋势分析\n"
            "- 价格所处阶段预测（顶部、底部、上涨中、下跌中、整理中）\n"
            "- 订单簿数据（买卖盘不平衡度）\n"
            "- 资金压力分析\n"
            "- 异常交易检测\n\n"

            "请从专业交易员和机构投资者角度进行深度分析：\n\n"

            "1. **主力资金行为解读**：\n"
            "   - 通过资金流向趋势变化，识别主力资金的建仓、出货行为\n"
            "   - 结合订单簿数据，分析主力资金的意图（吸筹、出货、洗盘等）\n"
            "   - 特别关注资金流向与价格变化不匹配的异常情况\n\n"

            "2. **价格阶段判断**：\n"
            "   - 根据资金流向趋势和价格关系，判断各交易对处于什么阶段（顶部、底部、上涨中、下跌中、整理中）\n"
            "   - 提供判断的置信度和依据\n"
            "   - 对比不同交易对的阶段差异，分析可能的轮动关系\n\n"

            "3. **短期趋势预判**：\n"
            "   - 基于资金流向和资金压力分析，预判未来4-8小时可能的价格走势\n"
            "   - 识别可能的反转信号或趋势延续信号\n"
            "   - 关注异常交易数据可能暗示的短期行情变化\n\n"

            "4. **交易策略建议**：\n"
            "   - 针对每个交易对，给出具体的交易建议（观望、做多、做空、减仓等）\n"
            "   - 提供可能的入场点位和止损位\n"
            "   - 评估风险和回报比\n\n"

            "请使用专业术语，保持分析简洁但深入，避免泛泛而谈。数据如下：\n\n" +
            json.dumps(data, indent=2, ensure_ascii=False) +
            "\n\n回复格式要求：中文，使用markdown格式，重点突出，适当使用表格对比分析。"
    )

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.7
    }

    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return "无法获取DeepSeek分析结果"


def cache_data(data, filename):
    with open(filename, 'wb') as f:
        pickle.dump(data, f)


def load_cached_data(filename):
    try:
        with open(filename, 'rb') as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


def parse_markdown(text):
    """解析Markdown文本，返回带格式指示的行列表"""
    lines = text.split('\n')
    parsed_lines = []
    
    in_code_block = False
    in_table = False
    table_data = []
    
    for line in lines:
        # 处理代码块
        if line.startswith('```') or line.endswith('```'):
            in_code_block = not in_code_block
            parsed_lines.append(('code_block_marker', line, {}))
            continue
            
        if in_code_block:
            parsed_lines.append(('code', line, {}))
            continue
            
        # 检查是否是表格分隔符行
        if re.match(r'^[\|\-\s]+$', line) and '|' in line:
            if not in_table and parsed_lines and '|' in parsed_lines[-1][1]:
                in_table = True
                table_data = [parsed_lines.pop()[1]]  # 获取表头
                table_data.append(line)  # 添加分隔符行
            continue
            
        # 处理表格行
        if in_table or ('|' in line and line.startswith('|') and line.endswith('|')):
            if not in_table:
                in_table = True
                table_data = []
            table_data.append(line)
            continue
            
        # 表格结束
        if in_table and ('|' not in line or not line.strip()):
            if table_data:
                parsed_lines.append(('table', table_data, {}))
                table_data = []
                in_table = False
            if not line.strip():
                parsed_lines.append(('text', '', {}))
                continue
                
        # 处理标题
        header_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if header_match:
            level = len(header_match.group(1))
            title = header_match.group(2)
            parsed_lines.append(('header', title, {'level': level}))
            continue
            
        # 处理列表项
        list_match = re.match(r'^(\s*)([\*\-\+]|\d+\.)\s+(.+)$', line)
        if list_match:
            indent = len(list_match.group(1))
            list_type = 'bullet' if list_match.group(2) in ['*', '-', '+'] else 'numbered'
            content = list_match.group(3)
            parsed_lines.append(('list_item', content, {'type': list_type, 'indent': indent}))
            continue
            
        # 处理引用
        quote_match = re.match(r'^>\s+(.+)$', line)
        if quote_match:
            content = quote_match.group(1)
            parsed_lines.append(('quote', content, {}))
            continue
            
        # 处理水平线
        if re.match(r'^[\-\*\_]{3,}$', line):
            parsed_lines.append(('divider', '', {}))
            continue
            
        # 处理普通文本，并应用内联格式
        if line.strip():
            # 处理内联格式(粗体、斜体、链接等)将在渲染时处理
            parsed_lines.append(('text', line, {}))
        else:
            parsed_lines.append(('text', '', {}))
    
    # 处理剩余的表格
    if in_table and table_data:
        parsed_lines.append(('table', table_data, {}))
        
    return parsed_lines

def get_inline_formats(text):
    """处理文本中的内联格式，返回带格式信息的文本片段列表"""
    segments = []
    # 临时替换链接，以便处理其他格式
    links = []
    def replace_link(match):
        links.append((match.group(1), match.group(2)))
        return f"[[LINK{len(links)-1}]]"
    
    # 保存链接并替换为标记
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, text)
    
    # 处理粗体
    parts = re.split(r'(\*\*[^*]+\*\*)', text)
    for part in parts:
        if part.startswith('**') and part.endswith('**'):
            segments.append(('bold', part[2:-2]))
        else:
            # 处理斜体
            italic_parts = re.split(r'(\*[^*]+\*)', part)
            for italic_part in italic_parts:
                if italic_part.startswith('*') and italic_part.endswith('*'):
                    segments.append(('italic', italic_part[1:-1]))
                else:
                    # 处理代码
                    code_parts = re.split(r'(`[^`]+`)', italic_part)
                    for code_part in code_parts:
                        if code_part.startswith('`') and code_part.endswith('`'):
                            segments.append(('inline_code', code_part[1:-1]))
                        else:
                            # 处理替换的链接
                            if '[[LINK' in code_part:
                                link_parts = re.split(r'(\[\[LINK\d+\]\])', code_part)
                                for link_part in link_parts:
                                    link_match = re.match(r'\[\[LINK(\d+)\]\]', link_part)
                                    if link_match:
                                        idx = int(link_match.group(1))
                                        if idx < len(links):
                                            text, url = links[idx]
                                            segments.append(('link', text, {'url': url}))
                                    elif link_part:
                                        segments.append(('text', link_part))
                            elif code_part:
                                segments.append(('text', code_part))
    
    return segments

def _get_font_for_style(style_params, fonts_dict):
    """Helper to get font based on style parameters."""
    if style_params.get('is_bold'):
        return fonts_dict['bold']
    if style_params.get('is_italic'): # Assuming italic font is same as regular for now or handled by drawing
        return fonts_dict['regular']
    if style_params.get('is_code'):
        return fonts_dict['code']
    if style_params.get('is_link'):
        return fonts_dict['regular'] # Links styled by color, not font typically
    return fonts_dict['regular']

def _parse_table_content(table_md_lines: List[str]) -> List[List[str]]:
    """Parses markdown table lines into a list of lists of strings."""
    parsed_rows = []
    if not table_md_lines:
        return []

    for i, line in enumerate(table_md_lines):
        if i == 1 and re.match(r'^[\|\-\s]+$', line):  # Skip separator line
            continue
        if not line.strip().startswith('|') or not line.strip().endswith('|'):
            continue # Should not happen if parse_markdown is correct

        cells = [cell.strip() for cell in line.split('|')]
        # Remove empty cells resulting from leading/trailing pipes
        if len(cells) > 0 and not cells[0]:
            cells = cells[1:]
        if len(cells) > 0 and not cells[-1]:
            cells = cells[:-1]
        if cells: # Only add if there are actual cells
            parsed_rows.append(cells)
    return parsed_rows


def _calculate_wrapped_dimensions(
    parsed_markdown_lines: List[Tuple[str, any, Dict]],
    fonts: Dict[str, ImageFont.FreeTypeFont],
    base_image_width: int,
    temp_draw: ImageDraw.ImageDraw,
    line_heights_config: Dict[str, int],
    padding_config: int,
    line_spacing_config: int,
    cell_padding_config: int
):
    """
    Calculates content dimensions after text wrapping and prepares wrapped data.
    Returns a dict: {"content_width": int, "content_height": int, "processed_lines": list}
    Each item in processed_lines: (type, original_content, params, item_calc_width, item_calc_height, wrapped_sub_lines_data)
    wrapped_sub_lines_data for text-like: list of strings (the wrapped lines)
    wrapped_sub_lines_data for table: (list of list of wrapped_cell_lines, final_col_widths)
    """
    content_max_x = 0  # Max width reached by any single element
    current_y_offset = 0
    processed_lines_output = []

    available_render_width = base_image_width - 2 * padding_config

    for line_idx, (line_type, original_content, params) in enumerate(parsed_markdown_lines):
        item_width = 0
        item_height = 0
        wrapped_sub_lines_data = None

        font_regular = fonts['regular']
        font_bold = fonts['bold']
        font_code = fonts['code']
        
        if line_type == 'header':
            level = params.get('level', 1)
            current_font = fonts['title'] if level == 1 else fonts['h2'] if level == 2 else fonts['h3']
            item_width = temp_draw.textlength(original_content, font=current_font)
            item_height = (line_heights_config.get(f'h{level}', line_heights_config['header']) + 
                           (line_spacing_config if level == 1 else line_spacing_config // 2)) # More space after H1
            if level == 1: # Extra space for H1 separator line
                item_height += 7 # 5 for line + 2 for spacing
            wrapped_sub_lines_data = [original_content]
        elif line_type in ['text', 'list_item', 'quote']:
            indent = params.get('indent', 0)
            prefix_width = 0
            if line_type == 'list_item':
                prefix_width = 20 # For bullet/number
            
            text_render_width = available_render_width - (indent + prefix_width)
            
            # Estimate characters per line
            avg_char_width = temp_draw.textlength("x", font=font_regular)
            if avg_char_width == 0: avg_char_width = 8 # Fallback
            chars_to_wrap = max(10, int(text_render_width / avg_char_width))

            wrapped_lines = []
            if original_content and isinstance(original_content, str):
                 wrapped_lines = textwrap.wrap(original_content, width=chars_to_wrap, replace_whitespace=False, drop_whitespace=True)
            if not wrapped_lines and original_content: # If wrap results in empty but original was not
                wrapped_lines = [' '] 
            elif not wrapped_lines and not original_content: # Truly empty line
                 wrapped_lines = ['']


            current_item_max_w = 0
            for sub_line in wrapped_lines:
                # Width calculation for mixed inline formats is complex.
                # For now, estimate based on regular font for simplicity in dimension calculation.
                # Actual drawing will handle inline formats.
                sub_line_width = temp_draw.textlength(sub_line, font=font_regular)
                current_item_max_w = max(current_item_max_w, sub_line_width)
            
            item_width = current_item_max_w + indent + prefix_width
            item_height = (len(wrapped_lines) * line_heights_config[line_type] + 
                           (len(wrapped_lines) -1 ) * (line_spacing_config // 2) + # Intra-item line spacing
                           line_spacing_config) # Space after item
            wrapped_sub_lines_data = wrapped_lines
            
            if line_type == 'text' and not original_content.strip(): # Blank line
                item_height = line_heights_config['blank'] # No extra line_spacing_config

        elif line_type == 'code_block_marker':
            item_height = 5 + line_spacing_config # Small space for marker rendering logic
            # Actual code block background is drawn based on code lines between markers
        
        elif line_type == 'code': # A single line within a code block
            # Code lines are typically not wrapped aggressively, but could be if very long
            text_render_width = available_render_width
            avg_char_width_code = temp_draw.textlength("x", font=font_code)
            if avg_char_width_code == 0: avg_char_width_code = 7
            chars_to_wrap_code = max(20, int(text_render_width / avg_char_width_code))
            
            wrapped_code_lines = textwrap.wrap(original_content, width=chars_to_wrap_code)
            if not wrapped_code_lines: wrapped_code_lines = [' ']

            current_item_max_w = 0
            for sub_line in wrapped_code_lines:
                 current_item_max_w = max(current_item_max_w, temp_draw.textlength(sub_line, font=font_code))
            
            item_width = current_item_max_w
            item_height = (len(wrapped_code_lines) * line_heights_config['code'] +
                           (len(wrapped_code_lines) -1) * (line_spacing_config // 2) + # Intra-item line spacing
                           line_spacing_config // 2) # Smaller spacing after each code line before block ends
            wrapped_sub_lines_data = wrapped_code_lines

        elif line_type == 'divider':
            item_width = available_render_width
            item_height = line_heights_config['divider'] + line_spacing_config
        
        elif line_type == 'table':
            raw_rows_content = _parse_table_content(original_content)
            num_rows = len(raw_rows_content)
            num_cols = max(len(r) for r in raw_rows_content) if raw_rows_content else 0

            final_col_widths = [0] * num_cols
            wrapped_table_cells_data = [[[] for _ in range(num_cols)] for _ in range(num_rows)]

            if num_cols > 0:
                # Pass 1: Determine natural width of each cell
                col_natural_widths = [0] * num_cols
                for r_idx, row_data in enumerate(raw_rows_content):
                    for c_idx, cell_text in enumerate(row_data):
                        if c_idx < num_cols:
                            font_to_use = font_bold if r_idx == 0 else font_regular
                            cell_w = temp_draw.textlength(cell_text, font=font_to_use) + 2 * cell_padding_config
                            col_natural_widths[c_idx] = max(col_natural_widths[c_idx], cell_w)
                
                total_natural_table_width = sum(col_natural_widths)
                
                # Pass 2: Allocate available_render_width to columns
                if total_natural_table_width <= available_render_width:
                    final_col_widths = col_natural_widths
                else:
                    # Proportional scaling for columns that need to shrink
                    # Ensure minimum width for each column (e.g., 50px)
                    min_col_width = 50 
                    excess_width = available_render_width - sum(min_col_width for _ in range(num_cols) if col_natural_widths[_] > min_col_width)
                    
                    scalable_natural_width = sum(w for w in col_natural_widths if w > min_col_width)
                    
                    for c_idx in range(num_cols):
                        if col_natural_widths[c_idx] <= min_col_width:
                            final_col_widths[c_idx] = col_natural_widths[c_idx]
                        elif scalable_natural_width > 0 : # and excess_width > 0:
                             final_col_widths[c_idx] = max(min_col_width, (col_natural_widths[c_idx] / scalable_natural_width) * excess_width if excess_width > 0 else min_col_width)
                        else: # All columns are huge, divide equally
                            final_col_widths[c_idx] = max(min_col_width, available_render_width / num_cols)
                
                # Ensure sum of final_col_widths does not exceed available_render_width significantly (due to min_col_width constraints)
                # This simple normalization might be needed if the above logic makes it too wide.
                current_total_final_width = sum(final_col_widths)
                if current_total_final_width > available_render_width and current_total_final_width > 0:
                    scale_factor = available_render_width / current_total_final_width
                    final_col_widths = [max(int(w * scale_factor), 30) for w in final_col_widths] # Min 30px hard fallback


                # Pass 3: Wrap cell content based on final_col_widths and calculate row heights
                table_render_height = 0
                for r_idx, row_data in enumerate(raw_rows_content):
                    max_wrapped_lines_in_this_row = 1
                    for c_idx, cell_text in enumerate(row_data):
                        if c_idx < num_cols:
                            cell_render_width = final_col_widths[c_idx] - 2 * cell_padding_config
                            font_to_use = font_bold if r_idx == 0 else font_regular
                            avg_char_w_cell = temp_draw.textlength("x", font=font_to_use)
                            if avg_char_w_cell == 0: avg_char_w_cell = 8
                            
                            chars_to_wrap_cell = max(1, int(cell_render_width / avg_char_w_cell))
                            wrapped_cell = textwrap.wrap(cell_text, width=chars_to_wrap_cell)
                            if not wrapped_cell: wrapped_cell = [' '] # Ensure at least one line for height
                            
                            wrapped_table_cells_data[r_idx][c_idx] = wrapped_cell
                            max_wrapped_lines_in_this_row = max(max_wrapped_lines_in_this_row, len(wrapped_cell))
                    
                    table_render_height += max_wrapped_lines_in_this_row * line_heights_config['table_row']
                    if r_idx < num_rows -1:
                         table_render_height += line_spacing_config // 2 # spacing between table rows

                item_width = sum(final_col_widths)
                item_height = table_render_height + line_spacing_config # Space after table
                wrapped_sub_lines_data = (wrapped_table_cells_data, final_col_widths, raw_rows_content) # Store raw too for drawing
            else: # No columns
                item_width = 0
                item_height = line_spacing_config

        else: # Unknown type or blank line from parse_markdown
            item_height = line_heights_config['blank']

        content_max_x = max(content_max_x, item_width)
        current_y_offset += item_height
        processed_lines_output.append(
            (line_type, original_content, params, item_width, item_height, wrapped_sub_lines_data)
        )

    return {
        "content_width": content_max_x,
        "content_height": current_y_offset,
        "processed_lines": processed_lines_output,
    }

def text_to_image(text, watermark="Telegram: @jin10light"):
    """将Markdown文本转换为图片，并添加水印"""
    try:
        # 解析Markdown
        parsed_lines_md = parse_markdown(text)
        
        # --- Configuration ---
        MIN_IMAGE_WIDTH = 800
        MAX_IMAGE_WIDTH = 2800 # Increased for potentially wide tables
        DEFAULT_ESTIMATION_WIDTH = 1200

        padding = 25 # Increased padding
        line_spacing = 6 # Increased line spacing
        cell_padding = 8 # Table cell padding
        footer_height = 150  # Increased for more footer space + QR

        background_color = (255, 255, 255)
        text_color = (30, 30, 30) # Darker text
        watermark_color = (235, 235, 235) # Lighter watermark
        highlight_color = (240, 240, 240) # Code block and other highlights
        quote_color = (80, 80, 80)
        table_header_bg = (225, 225, 225)
        table_border_color = (180, 180, 180) # Darker border
        link_color = (0, 102, 204) # Standard link blue

        line_heights = {
            'header': 38, 'h1':38, 'h2': 32, 'h3': 28,
            'text': 22, 'list_item': 22, 'quote': 24,
            'divider': 15, 'code': 20, 'table_row': 24,
            'blank': 12
        }

        # --- Font Setup ---
        fonts = {}
        try:
            font_paths = [
                "AlibabaPuHuiTi-3-55-Regular.ttf",
                "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf" # Common Linux font
            ]
            chosen_font_path = None
            for path in font_paths:
                if os.path.exists(path):
                    chosen_font_path = path
                    logger.info(f"Using font: {path}")
                    break
            
            if not chosen_font_path:
                logger.warning("No preferred system fonts found, using Pillow default.")
                fonts['regular'] = ImageFont.load_default()
                fonts['bold'] = ImageFont.load_default()
                fonts['title'] = ImageFont.load_default()
                fonts['h2'] = ImageFont.load_default()
                fonts['h3'] = ImageFont.load_default()
                fonts['code'] = ImageFont.load_default()
                fonts['watermark'] = ImageFont.load_default()
            else:
                fonts['regular'] = ImageFont.truetype(chosen_font_path, 18)
                fonts['bold'] = ImageFont.truetype(chosen_font_path, 18) # Assuming bold variant or rely on draw for faux bold
                fonts['title'] = ImageFont.truetype(chosen_font_path, 28)
                fonts['h2'] = ImageFont.truetype(chosen_font_path, 24)
                fonts['h3'] = ImageFont.truetype(chosen_font_path, 22)
                fonts['code'] = ImageFont.truetype(chosen_font_path, 16) # Monospace preferred if available
                fonts['watermark'] = ImageFont.truetype(chosen_font_path, 22)

        except Exception as e:
            logger.error(f"Font loading failed: {e}. Using Pillow default.", exc_info=True)
            # Fallback to Pillow's default font if any TrueType loading fails
            default_font_instance = ImageFont.load_default()
            fonts = {k: default_font_instance for k in ['regular', 'bold', 'title', 'h2', 'h3', 'code', 'watermark']}

        # --- Dimension Calculation ---
        # Create a temporary draw object for textlength calls
        temp_img_for_calc = Image.new('RGB', (1,1), background_color)
        temp_draw_obj = ImageDraw.Draw(temp_img_for_calc)

        # First pass for width estimation
        pass1_dims = _calculate_wrapped_dimensions(
            parsed_markdown_lines=parsed_lines_md, fonts=fonts, base_image_width=DEFAULT_ESTIMATION_WIDTH,
            temp_draw=temp_draw_obj, line_heights_config=line_heights, padding_config=padding,
            line_spacing_config=line_spacing, cell_padding_config=cell_padding
        )
        
        calculated_content_width = pass1_dims["content_width"]
        image_width = int(min(MAX_IMAGE_WIDTH, max(MIN_IMAGE_WIDTH, calculated_content_width + 2 * padding)))

        # Second pass with the determined image_width to get accurate height and final wrapped data
        final_calculated_data = _calculate_wrapped_dimensions(
            parsed_markdown_lines=parsed_lines_md, fonts=fonts, base_image_width=image_width,
            temp_draw=temp_draw_obj, line_heights_config=line_heights, padding_config=padding,
            line_spacing_config=line_spacing, cell_padding_config=cell_padding
        )
        
        image_content_height = final_calculated_data["content_height"]
        image_height = int(image_content_height + 2 * padding + footer_height)
        processed_drawing_lines = final_calculated_data["processed_lines"]

        del temp_draw_obj, temp_img_for_calc # Clean up temporary objects

        # --- Image Creation ---
        image = Image.new('RGB', (image_width, image_height), background_color)
        draw = ImageDraw.Draw(image)

        # --- Watermark ---
        # Diagonal watermark
        try:
            wm_text_width = draw.textlength(watermark, font=fonts['watermark'])
            for i in range(-image_height // 2, image_width + image_height //2 , int(wm_text_width * 1.5) ):
                 draw.text((i, (i*0.3) % image_height ), watermark, font=fonts['watermark'], fill=watermark_color, anchor="lt", angle=30)
        except Exception as e:
            logger.warning(f"Could not draw diagonal watermark: {e}")
            # Fallback simpler watermark if advanced fails
            for i in range(0, image_width + image_height, 300):
                x1 = max(0, i - image_height)
                y1 = max(0, image_height - i) # Basic attempt at diagonal spread
                if x1 < image_width and y1 < image_height :
                    draw.text((x1 + 50, y1 + 50), watermark, font=fonts['watermark'], fill=watermark_color)


        # --- Drawing Content ---
        y_position = padding
        
        # Code block drawing state
        in_code_block_drawing = False
        code_block_rect_start_y = 0
        code_block_lines_buffer = []


        for line_idx, (line_type, original_content, params, item_calc_width, item_calc_height, wrapped_sub_data) in enumerate(processed_drawing_lines):
            
            # Handle code block rendering flush
            if line_type != 'code' and in_code_block_drawing:
                # Draw the collected code block background and text
                if code_block_lines_buffer:
                    # Background for the entire block
                    first_line_y = code_block_rect_start_y
                    # Calculate total height of buffered code lines for accurate rectangle
                    buffered_code_block_h = sum(lh for _,_,_,lh,_,_ in code_block_lines_buffer)
                    
                    draw.rectangle(
                        (padding - 5, first_line_y - (line_spacing//2) , 
                         image_width - padding + 5, first_line_y + buffered_code_block_h - (line_spacing//2) + 5), # Adjusted Y end
                        fill=highlight_color
                    )
                    # Draw each line of code text
                    temp_y_for_code = first_line_y
                    for code_l_type, code_l_orig, code_l_params, _, code_l_h, code_l_wrapped in code_block_lines_buffer:
                        if code_l_wrapped and isinstance(code_l_wrapped, list):
                             for sub_code_line in code_l_wrapped:
                                draw.text((padding, temp_y_for_code), sub_code_line, font=fonts['code'], fill=text_color)
                                temp_y_for_code += line_heights['code'] + (line_spacing // 2) # Uses line height for 'code'
                        # temp_y_for_code += (code_l_h - (line_spacing //2) ) # Advance by its calculated height
                
                in_code_block_drawing = False
                code_block_lines_buffer = []
                # y_position is already advanced by _calculate_wrapped_dimensions, so just continue

            if line_type == 'header':
                level = params.get('level', 1)
                current_font = fonts['title'] if level == 1 else fonts['h2'] if level == 2 else fonts['h3']
                text_content = wrapped_sub_data[0] if wrapped_sub_data else ""
                draw.text((padding, y_position), text_content, font=current_font, fill=text_color)
                if level == 1:
                    # Separator line for H1
                    sep_y = y_position + line_heights['h1'] + (line_spacing // 2)
                    draw.line((padding, sep_y, image_width - padding, sep_y), fill=table_border_color, width=2)
            
            elif line_type == 'text' or line_type == 'quote' or line_type == 'list_item':
                current_x = padding
                indent_val = params.get('indent', 0)
                current_x += indent_val

                if line_type == 'quote':
                    # Draw quote bar
                    draw.rectangle((padding, y_position, padding + 4, y_position + item_calc_height - line_spacing), 
                                   fill=quote_color)
                    current_x += 10 # Indent text for quote

                if line_type == 'list_item':
                    bullet_radius = 3
                    bullet_y_center = y_position + line_heights['list_item'] // 2
                    # Draw bullet point (simple circle for now)
                    draw.ellipse((current_x, bullet_y_center - bullet_radius,
                                  current_x + 2 * bullet_radius, bullet_y_center + bullet_radius),
                                 fill=text_color)
                    current_x += 15 # Space after bullet

                current_line_y = y_position
                if wrapped_sub_data and isinstance(wrapped_sub_data, list):
                    for sub_line_text in wrapped_sub_data:
                        # Apply inline formatting for this sub_line_text
                        segments = get_inline_formats(sub_line_text)
                        x_draw_cursor = current_x
                        for seg_idx, segment_info in enumerate(segments):
                            seg_type, seg_text = segment_info[0], segment_info[1]
                            seg_params = segment_info[2] if len(segment_info) > 2 else {}
                            
                            inline_font = fonts['regular']
                            inline_fill_color = text_color

                            if seg_type == 'bold': inline_font = fonts['bold']
                            elif seg_type == 'italic': pass # Pillow doesn't have simple italic toggle, use regular
                            elif seg_type == 'inline_code':
                                inline_font = fonts['code']
                                # Draw background for inline code
                                code_text_w = draw.textlength(seg_text, font=inline_font)
                                code_text_h = line_heights['text'] # Approx
                                draw.rectangle(
                                    (x_draw_cursor - 2, current_line_y - 1,
                                     x_draw_cursor + code_text_w + 2, current_line_y + code_text_h - 3),
                                    fill=highlight_color
                                )
                            elif seg_type == 'link': inline_fill_color = link_color
                            
                            if seg_text.strip(): # Only draw if there's actual text
                                draw.text((x_draw_cursor, current_line_y), seg_text, font=inline_font, fill=inline_fill_color)
                                x_draw_cursor += draw.textlength(seg_text, font=inline_font)
                        
                        current_line_y += line_heights[line_type] + (line_spacing // 2)
            
            elif line_type == 'code_block_marker':
                if not in_code_block_drawing: # Start of a code block
                    in_code_block_drawing = True
                    code_block_rect_start_y = y_position # Mark Y for the background rect
                # The flush logic at the start of the loop or end of file will handle drawing
                # y_position advances by item_calc_height which is small for marker

            elif line_type == 'code': # Inside a code block
                if not in_code_block_drawing: # Should be set by marker, but safety
                    in_code_block_drawing = True
                    code_block_rect_start_y = y_position 
                
                # Buffer this line's data instead of drawing immediately
                # The actual drawing happens when the block ends or another type starts
                code_block_lines_buffer.append((line_type, original_content, params, item_calc_width, item_calc_height, wrapped_sub_data))
                # y_position still advances based on _calculate_wrapped_dimensions for this line
            
            elif line_type == 'divider':
                div_y = y_position + line_heights['divider'] // 2
                draw.line((padding, div_y, image_width - padding, div_y), fill=table_border_color, width=1)

            elif line_type == 'table':
                if wrapped_sub_data:
                    wrapped_cells, col_widths, raw_table_content = wrapped_sub_data
                    num_actual_cols = len(col_widths)
                    
                    current_table_y = y_position
                    for r_idx, row_wrapped_cells in enumerate(wrapped_cells):
                        row_max_lines = 1
                        for cell_lines in row_wrapped_cells:
                            row_max_lines = max(row_max_lines, len(cell_lines))
                        
                        current_row_height = row_max_lines * line_heights['table_row']
                        
                        # Draw row background (header or alternating)
                        if r_idx == 0: # Header
                            draw.rectangle(
                                (padding, current_table_y, image_width - padding, current_table_y + current_row_height),
                                fill=table_header_bg
                            )
                        elif r_idx % 2 != 0: # Odd rows (after header) can have slightly different bg
                             draw.rectangle(
                                (padding, current_table_y, image_width - padding, current_table_y + current_row_height),
                                fill=(248, 248, 248) # Very light grey for alternating rows
                            )


                        current_cell_x = padding
                        for c_idx, single_cell_wrapped_lines in enumerate(row_wrapped_cells):
                            if c_idx >= num_actual_cols: continue # Should not happen if data is consistent

                            cell_text_y_offset = current_table_y + (current_row_height - len(single_cell_wrapped_lines) * line_heights['table_row']) / 2 # Center vertically somewhat
                            
                            font_to_use = fonts['bold'] if r_idx == 0 else fonts['regular']
                            cell_content_color = text_color
                            
                            # Try to get original cell text for styling keywords, assuming wrapped_cells structure matches raw_table_content
                            original_cell_text_for_style = ""
                            if r_idx < len(raw_table_content) and c_idx < len(raw_table_content[r_idx]):
                                original_cell_text_for_style = raw_table_content[r_idx][c_idx]

                            if any(kw in original_cell_text_for_style.lower() for kw in ['看涨', '做多', '上涨', '突破', 'buying']):
                                cell_content_color = (0, 130, 0)  # Green
                            elif any(kw in original_cell_text_for_style.lower() for kw in ['看跌', '做空', '下跌', '暴跌', 'selling']):
                                cell_content_color = (200, 0, 0)  # Red
                            elif any(kw in original_cell_text_for_style.lower() for kw in ['警示', '风险', '异常', 'warning']):
                                cell_content_color = (180, 80, 0) # Orange


                            for line_in_cell in single_cell_wrapped_lines:
                                draw.text(
                                    (current_cell_x + cell_padding, cell_text_y_offset),
                                    line_in_cell, font=font_to_use, fill=cell_content_color
                                )
                                cell_text_y_offset += line_heights['table_row']
                            current_cell_x += col_widths[c_idx]
                        
                        # Horizontal line for each row
                        draw.line((padding, current_table_y + current_row_height, image_width - padding, current_table_y + current_row_height),
                                  fill=table_border_color, width=1)
                        current_table_y += current_row_height + (line_spacing // 2 if r_idx < len(wrapped_cells) -1 else 0)

                    # Vertical lines for table
                    v_line_x = padding
                    for c_w in col_widths:
                        draw.line((v_line_x, y_position, v_line_x, current_table_y - (line_spacing//2 if len(wrapped_cells)>0 else 0) ), 
                                  fill=table_border_color, width=1)
                        v_line_x += c_w
                    # Last vertical line
                    draw.line((image_width - padding, y_position, image_width - padding, current_table_y - (line_spacing//2 if len(wrapped_cells)>0 else 0)),
                              fill=table_border_color, width=1)
                    
            y_position += item_calc_height # Advance y_position by the pre-calculated height for this item
        
        # Final flush for any pending code block at the end of the document
        if in_code_block_drawing and code_block_lines_buffer:
            first_line_y = code_block_rect_start_y # This might be off if y_pos was advanced
            # Find the y_pos of the first code line in buffer to be accurate
            buffered_code_block_h = sum(lh for _,_,_,_,lh,_ in code_block_lines_buffer)
            
            # Re-calculate y_start for buffered code block if y_position changed due to other elements
            # This needs careful thought. For now, assume code_block_rect_start_y was correctly set when block started.
            # It implies y_position for code lines in _calculate_wrapped_dimensions was used to set this start_y.

            draw.rectangle(
                (padding - 5, code_block_rect_start_y - (line_spacing//2), 
                 image_width - padding + 5, code_block_rect_start_y + buffered_code_block_h - (line_spacing//2) + 5),
                fill=highlight_color
            )
            temp_y_for_code = code_block_rect_start_y
            for code_l_type, code_l_orig, code_l_params, _, code_l_h, code_l_wrapped in code_block_lines_buffer:
                if code_l_wrapped and isinstance(code_l_wrapped, list):
                     for sub_code_line in code_l_wrapped:
                        draw.text((padding, temp_y_for_code), sub_code_line, font=fonts['code'], fill=text_color)
                        temp_y_for_code += line_heights['code'] + (line_spacing // 2)


        # --- Footer ---
        actual_content_end_y = y_position # This should be the end of all drawn content
        footer_start_y = max(actual_content_end_y + line_spacing, image_height - footer_height + padding)


        draw.line((padding, footer_start_y, image_width - padding, footer_start_y), 
                  fill=table_border_color, width=1)
        footer_text_y = footer_start_y + 10

        # Date and disclaimer
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC') # Use UTC or specify timezone
        footnote_text = f"注: 所有时间均为UTC时间，价格单位为USDT。数据截止 {current_time_str}，实际交易需结合实时数据验证。"
        
        # Wrap footnote
        avg_char_w_footer = draw.textlength("x", font=fonts['regular'])
        if avg_char_w_footer == 0: avg_char_w_footer = 8
        footer_wrap_chars = max(10, int((image_width - 2 * padding) / avg_char_w_footer))
        wrapped_footnote = textwrap.wrap(footnote_text, width=footer_wrap_chars)

        for line in wrapped_footnote:
            draw.text((padding, footer_text_y), line, font=fonts['regular'], fill=(100,100,100))
            footer_text_y += line_heights['text'] # Use text line height

        footer_text_y += (line_spacing // 2)
        disclaimer_text = "*免责声明: 本分析仅供专业参考，不构成投资建议，交易决策请自行承担风险。"
        wrapped_disclaimer = textwrap.wrap(disclaimer_text, width=footer_wrap_chars)
        for line in wrapped_disclaimer:
            draw.text((padding, footer_text_y), line, font=fonts['regular'], fill=(100,100,100))
            footer_text_y += line_heights['text']
        
        # QR Code and Telegram Info (Right side of footer)
        qr_available_width = image_width // 3 # Allocate space for QR + text
        qr_text_x_start = image_width - padding - qr_available_width
        qr_y_start = footer_start_y + 10

        try:
            import qrcode
            qr_data = "https://t.me/jin10light" # Example URL
            qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=8, border=2)
            qr.add_data(qr_data)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA") # Ensure RGBA for transparency if needed
            
            qr_max_size = footer_height - 40 # Max size for QR
            qr_img = qr_img.resize((min(qr_max_size, 100), min(qr_max_size, 100)), Image.Resampling.LANCZOS) # Keep it reasonable
            
            qr_actual_size = qr_img.width
            
            qr_paste_x = image_width - padding - qr_actual_size
            qr_paste_y = qr_y_start
            image.paste(qr_img, (qr_paste_x, qr_paste_y), qr_img if qr_img.mode == 'RGBA' else None)

            tg_text = "Telegram: @jin10light"
            tg_text_w = draw.textlength(tg_text, font=fonts['regular'])
            tg_text_x = qr_paste_x - tg_text_w - 10 # To the left of QR
            tg_text_y = qr_paste_y + qr_actual_size // 2 - (line_heights['text'] //2) 
            
            if tg_text_x < padding: # If text would overflow left, place below QR
                 tg_text_x = qr_paste_x
                 tg_text_y = qr_paste_y + qr_actual_size + 5

            draw.text((tg_text_x, tg_text_y), tg_text, font=fonts['regular'], fill=text_color)

        except ImportError:
            logger.warning("qrcode library not installed. Skipping QR code.")
            tg_text_fallback = "Telegram: @jin10light (QR disabled)"
            draw.text((qr_text_x_start, qr_y_start + 20), tg_text_fallback, font=fonts['regular'], fill=text_color)
        except Exception as e:
            logger.error(f"Failed to generate or place QR code: {e}", exc_info=True)
            tg_text_fallback = "Telegram: @jin10light (QR error)"
            draw.text((qr_text_x_start, qr_y_start + 20), tg_text_fallback, font=fonts['regular'], fill=text_color)


        # --- Save to Buffer ---
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer

    except Exception as e:
        logger.error(f"Text to image conversion failed: {e}", exc_info=True)
        # Create a simple error image
        try:
            err_img = Image.new('RGB', (600, 100), (255,200,200))
            err_draw = ImageDraw.Draw(err_img)
            err_font = ImageFont.load_default()
            err_draw.text((10,10), "Error generating image.", font=err_font, fill=(0,0,0))
            err_draw.text((10,30), str(e)[:80], font=err_font, fill=(0,0,0))
            err_buffer = io.BytesIO()
            err_img.save(err_buffer, format='PNG')
            err_buffer.seek(0)
            return err_buffer
        except: # Ultimate fallback
             return None


async def send_telegram_message_async(message, as_image=True):
    """异步发送Telegram消息"""
    try:
        bot_token = config.get('TELEGRAM', 'BOT_TOKEN')
        chat_id = config.get('TELEGRAM', 'CHAT_ID')
        
        # 在消息最后加上免责声明
        if not message.endswith("*免责声明：本分析仅供专业参考，不构成投资建议，交易决策请自行承担风险"):
            message += "\n\n*免责声明：本分析仅供专业参考，不构成投资建议，交易决策请自行承担风险"
        
        bot = Bot(token=bot_token)
        
        if as_image:
            # 将消息转换为图片
            image_buffer = text_to_image(message)
            if image_buffer:
                # 异步发送图片
                await bot.send_photo(chat_id=chat_id, photo=image_buffer)
                logger.info("成功发送Telegram图片消息")
                return True
            else:
                logger.error("图片生成失败，尝试发送文本消息")
                # 如果图片生成失败，回退到发送文本消息
                await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
                logger.info("成功发送Telegram文本消息")
                return True
        else:
            # 直接发送文本消息
            await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info("成功发送Telegram文本消息")
            return True
    except Exception as e:
        logger.error(f"发送Telegram消息时出错: {e}")
        return False

def send_telegram_message(message, as_image=True):
    """发送Telegram消息的同步包装函数"""
    try:
        # 创建新的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 运行异步函数
        result = loop.run_until_complete(send_telegram_message_async(message, as_image))
        
        # 关闭事件循环
        loop.close()
        
        return result
    except Exception as e:
        logger.error(f"执行Telegram异步消息发送时出错: {e}")
        return False


def main_optimized():
    logger.info(f"开始运行，当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"目标交易对: {SYMBOLS}")

    # 获取现货和期货的5分钟K线数据
    spot_klines_data = {}
    futures_klines_data = {}

    for symbol in SYMBOLS:
        logger.info(f"获取 {symbol} 现货5分钟K线数据...")
        spot_klines_data[symbol] = get_klines_data(symbol, interval='5m', limit=50, is_futures=False)

        logger.info(f"获取 {symbol} 期货5分钟K线数据...")
        futures_klines_data[symbol] = get_klines_data(symbol, interval='5m', limit=50, is_futures=True)

    # 获取订单簿数据
    logger.info("获取订单簿数据...")
    spot_order_books = {}
    futures_order_books = {}

    for symbol in SYMBOLS:
        spot_order_books[symbol] = get_orderbook_stats(symbol, is_futures=False)
        futures_order_books[symbol] = get_orderbook_stats(symbol, is_futures=True)

    # 分析资金流向趋势
    logger.info("分析资金流向趋势...")
    spot_trend_analysis = {}
    futures_trend_analysis = {}

    for symbol in SYMBOLS:
        spot_trend_analysis[symbol] = analyze_funding_flow_trend(spot_klines_data[symbol])
        futures_trend_analysis[symbol] = analyze_funding_flow_trend(futures_klines_data[symbol])

    # 检测异常交易
    logger.info("检测异常交易...")
    spot_anomalies = {}
    futures_anomalies = {}

    for symbol in SYMBOLS:
        spot_anomalies[symbol] = detect_anomalies(spot_klines_data[symbol])
        futures_anomalies[symbol] = detect_anomalies(futures_klines_data[symbol])

    # 分析资金压力
    logger.info("分析资金压力...")
    spot_funding_pressure = {}
    futures_funding_pressure = {}

    for symbol in SYMBOLS:
        if symbol in spot_order_books and spot_order_books[symbol]:
            spot_funding_pressure[symbol] = analyze_funding_pressure(spot_klines_data[symbol], spot_order_books[symbol])

        if symbol in futures_order_books and futures_order_books[symbol]:
            futures_funding_pressure[symbol] = analyze_funding_pressure(futures_klines_data[symbol],
                                                                        futures_order_books[symbol])

    # 准备DeepSeek数据
    deepseek_data = {
        "spot_klines_summary": {
            symbol: {
                "count": len(data),
                "time_range": f"{data[0]['open_time']} to {data[-1]['close_time']}" if data else "No data",
                "latest_price": data[-1]['close'] if data else None,
                "price_change_pct": ((data[-1]['close'] - data[0]['open']) / data[0]['open'] * 100) if data else None
            } for symbol, data in spot_klines_data.items()
        },
        "futures_klines_summary": {
            symbol: {
                "count": len(data),
                "time_range": f"{data[0]['open_time']} to {data[-1]['close_time']}" if data else "No data",
                "latest_price": data[-1]['close'] if data else None,
                "price_change_pct": ((data[-1]['close'] - data[0]['open']) / data[0]['open'] * 100) if data else None
            } for symbol, data in futures_klines_data.items()
        },
        "spot_trend_analysis": spot_trend_analysis,
        "futures_trend_analysis": futures_trend_analysis,
        "spot_anomalies": spot_anomalies,
        "futures_anomalies": futures_anomalies,
        "spot_funding_pressure": spot_funding_pressure,
        "futures_funding_pressure": futures_funding_pressure,
        "spot_order_books": {k: v for k, v in spot_order_books.items() if v is not None},
        "futures_order_books": {k: v for k, v in futures_order_books.items() if v is not None}
    }

    # 添加现货和期货的资金流向对比数据
    funding_flow_comparison = {}
    for symbol in SYMBOLS:
        spot_data = spot_klines_data.get(symbol, [])
        futures_data = futures_klines_data.get(symbol, [])

        if spot_data and futures_data:
            # 获取最近10个周期的资金流向数据
            recent_spot_inflows = [k['net_inflow'] for k in spot_data[-10:]]
            recent_futures_inflows = [k['net_inflow'] for k in futures_data[-10:]]

            # 计算现货和期货资金流向的差异
            spot_total_inflow = sum(recent_spot_inflows)
            futures_total_inflow = sum(recent_futures_inflows)
            flow_difference = spot_total_inflow - futures_total_inflow

            # 计算现货和期货资金流向的相关性
            if len(recent_spot_inflows) == len(recent_futures_inflows) and len(recent_spot_inflows) > 1:
                correlation = np.corrcoef(recent_spot_inflows, recent_futures_inflows)[0, 1]
            else:
                correlation = None

            funding_flow_comparison[symbol] = {
                "spot_total_inflow": spot_total_inflow,
                "futures_total_inflow": futures_total_inflow,
                "flow_difference": flow_difference,
                "correlation": correlation,
                "dominant_market": "spot" if spot_total_inflow > futures_total_inflow else "futures",
                "flow_ratio": abs(spot_total_inflow / futures_total_inflow) if futures_total_inflow != 0 else float(
                    'inf')
            }

    deepseek_data["funding_flow_comparison"] = funding_flow_comparison

    # 添加价格与资金流向的领先/滞后关系分析
    lead_lag_analysis = {}
    for symbol in SYMBOLS:
        spot_data = spot_klines_data.get(symbol, [])

        if len(spot_data) > 10:
            prices = [k['close'] for k in spot_data]
            inflows = [k['net_inflow'] for k in spot_data]

            # 计算不同滞后期的相关性
            correlations = []
            for lag in range(-5, 6):  # 从-5到5的滞后期
                if lag < 0:
                    # 资金流向领先于价格
                    corr = np.corrcoef(inflows[:lag], prices[-lag:])[0, 1]
                elif lag > 0:
                    # 价格领先于资金流向
                    corr = np.corrcoef(inflows[lag:], prices[:-lag])[0, 1]
                else:
                    # 同期相关性
                    corr = np.corrcoef(inflows, prices)[0, 1]

                correlations.append((lag, corr))

            # 找出最大相关性的滞后期
            max_corr_lag = max(correlations, key=lambda x: abs(x[1]))

            lead_lag_analysis[symbol] = {
                "max_correlation": max_corr_lag[1],
                "optimal_lag": max_corr_lag[0],
                "relationship": "资金流向领先于价格" if max_corr_lag[0] < 0 else "价格领先于资金流向" if max_corr_lag[
                                                                                                             0] > 0 else "同步变化",
                "all_correlations": correlations
            }

    deepseek_data["lead_lag_analysis"] = lead_lag_analysis

    # 格式化数值
    for symbol in SYMBOLS:
        if symbol in deepseek_data["spot_klines_summary"]:
            if deepseek_data["spot_klines_summary"][symbol]["latest_price"]:
                deepseek_data["spot_klines_summary"][symbol]["latest_price"] = format_number(
                    deepseek_data["spot_klines_summary"][symbol]["latest_price"])

        if symbol in deepseek_data["futures_klines_summary"]:
            if deepseek_data["futures_klines_summary"][symbol]["latest_price"]:
                deepseek_data["futures_klines_summary"][symbol]["latest_price"] = format_number(
                    deepseek_data["futures_klines_summary"][symbol]["latest_price"])

    # 发送分析请求
    logger.info("正在请求DeepSeek API进行数据解读...")
    analysis = send_to_deepseek(deepseek_data)

    # 保存结果
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    analysis_filename = f"binance_analysis_{timestamp}.md"
    
    with open(analysis_filename, "w", encoding="utf-8") as f:
        f.write(analysis)
    logger.info(f"分析结果已保存到 {analysis_filename}")

    # 发送Telegram消息
    logger.info("正在发送Telegram消息...")
    message_header = f"# CEX资金流向分析 - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    
    # 创建完整消息
    full_message = message_header + analysis
    
    # 由于发送为图片，无需担心Telegram消息长度限制
    send_telegram_message(full_message, as_image=True)

    # 打印分析结果
    print("\n分析结果:")
    print(analysis)


def job():
    """定时执行的任务"""
    logger.info("执行定时分析任务...")
    try:
        main_optimized()
        logger.info("定时分析任务完成")
    except Exception as e:
        logger.error(f"定时任务执行失败: {e}")


if __name__ == "__main__":
    try:
        # 首次运行
        main_optimized()
        
        # 设置每小时运行一次
        schedule.every(1).hour.do(job)
        
        logger.info("已设置定时任务，程序将每小时更新一次分析结果")
        
        # 持续运行，等待定时任务
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次是否有待执行的任务
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序出错: {e}")
