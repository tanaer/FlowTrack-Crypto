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
import markdown
from bs4 import BeautifulSoup

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
            # 发送文本消息
            await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info("成功发送Telegram文本消息")
            return True
    except Exception as e:
        logger.error(f"发送Telegram消息失败: {e}")
        return False


def text_to_image(text, watermark="Telegram: @jin10light"):
    """将Markdown文本转换为图片，并添加水印"""
    try:
        import qrcode
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO
        from bs4 import BeautifulSoup
        
        # 设置字体和颜色
        background_color = (255, 255, 255)  # 白色背景
        text_color = (30, 30, 30)          # 深灰色文字
        title_color = (0, 0, 0)            # 黑色标题
        table_header_bg = (240, 240, 240)  # 表头背景
        table_border_color = (200, 200, 200)  # 表格边框
        watermark_color = (230, 230, 230)  # 浅灰色水印

        # 准备字体
        try:
            # 尝试使用常见中文字体
            font_paths = [
                "AlibabaPuHuiTi-3-55-Regular.ttf", 
                "C:/Windows/Fonts/msyh.ttc", 
                "C:/Windows/Fonts/simhei.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
            ]
            
            font_path = None
            for path in font_paths:
                if os.path.exists(path):
                    font_path = path
                    break
                    
            if font_path:
                regular_font = ImageFont.truetype(font_path, 16)
                title_font = ImageFont.truetype(font_path, 24)
                subtitle_font = ImageFont.truetype(font_path, 20)
                small_font = ImageFont.truetype(font_path, 14)
            else:
                # 使用默认字体
                regular_font = ImageFont.load_default()
                title_font = ImageFont.load_default()
                subtitle_font = ImageFont.load_default()
                small_font = ImageFont.load_default()
        except Exception as e:
            logger.warning(f"加载字体失败: {e}, 使用默认字体")
            regular_font = ImageFont.load_default()
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default() 
            small_font = ImageFont.load_default()
            
        # 将Markdown转换为HTML
        html = markdown.markdown(text, extensions=['tables', 'fenced_code'])
        soup = BeautifulSoup(html, 'html.parser')
        
        # 计算图像大小
        padding = 20
        line_height = 24
        
        # 创建临时图像用于测量文本大小
        temp_img = Image.new('RGB', (1, 1), background_color)
        draw = ImageDraw.Draw(temp_img)
        
        # 估算宽度和高度
        max_width = 800
        total_height = 0
        
        for element in soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'table', 'pre']):
            if element.name.startswith('h'):
                level = int(element.name[1])
                font = title_font if level == 1 else subtitle_font
                text_width = draw.textlength(element.text, font=font)
                max_width = max(max_width, text_width + 2 * padding)
                total_height += line_height * (1.5 if level == 1 else 1.3)
            elif element.name == 'p':
                # 段落文本分行
                text = element.text
                wrapped_text = textwrap.wrap(text, width=70)  # 假设约70个字符宽度
                total_height += len(wrapped_text) * line_height
            elif element.name == 'table':
                # 表格高度估算
                rows = element.find_all('tr')
                total_rows = len(rows)
                total_height += total_rows * line_height * 1.5 + 20  # 表格行加额外空间
            elif element.name in ('ul', 'ol'):
                # 列表项
                items = element.find_all('li')
                total_height += len(items) * line_height * 1.2
            elif element.name == 'pre':
                # 代码块
                code_lines = element.text.strip().split('\n')
                total_height += len(code_lines) * line_height + 20
        
        # 添加页脚空间
        total_height += 100
        
        # 创建图像
        img_width = max(max_width, 800)
        img_height = int(total_height + 2 * padding)
        image = Image.new('RGB', (img_width, img_height), background_color)
        draw = ImageDraw.Draw(image)
        
        # 绘制内容
        y_position = padding
        
        for element in soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'table', 'pre']):
            if element.name.startswith('h'):
                level = int(element.name[1])
                font = title_font if level == 1 else subtitle_font
                draw.text((padding, y_position), element.text, font=font, fill=title_color)
                y_position += line_height * (1.5 if level == 1 else 1.3)
                
                # 为h1添加下划线
                if level == 1:
                    draw.line([(padding, y_position - 5), (img_width - padding, y_position - 5)], 
                             fill=table_border_color, width=1)
            
            elif element.name == 'p':
                text = element.text
                wrapped_text = textwrap.wrap(text, width=70)
                for line in wrapped_text:
                    draw.text((padding, y_position), line, font=regular_font, fill=text_color)
                    y_position += line_height
                y_position += 5  # 段落间距
            
            elif element.name == 'table':
                # 绘制表格
                rows = element.find_all('tr')
                
                # 计算列宽
                col_count = max(len(row.find_all(['th', 'td'])) for row in rows)
                col_widths = [img_width / col_count] * col_count
                
                # 表格开始位置
                table_top = y_position
                table_left = padding
                
                for row_idx, row in enumerate(rows):
                    cells = row.find_all(['th', 'td'])
                    row_height = line_height * 1.5
                    
                    # 绘制行背景
                    row_bg_color = table_header_bg if row_idx == 0 else background_color
                    draw.rectangle([(table_left, y_position), 
                                    (img_width - padding, y_position + row_height)], 
                                  fill=row_bg_color)
                    
                    # 绘制单元格内容
                    x_pos = table_left
                    for col_idx, cell in enumerate(cells):
                        if col_idx < len(col_widths):
                            cell_width = col_widths[col_idx]
                            cell_text = cell.text.strip()
                            
                            # 调整单元格文本颜色 (可以根据内容设置不同颜色)
                            cell_color = text_color
                            if "看涨" in cell_text or "做多" in cell_text:
                                cell_color = (0, 130, 0)  # 绿色
                            elif "看跌" in cell_text or "做空" in cell_text:
                                cell_color = (200, 0, 0)  # 红色
                            
                            # 绘制文本 (居中)
                            text_width = draw.textlength(cell_text, font=regular_font)
                            text_x = x_pos + (cell_width - text_width) / 2
                            text_y = y_position + (row_height - line_height) / 2
                            draw.text((text_x, text_y), cell_text, font=regular_font, fill=cell_color)
                            
                            # 绘制单元格边框
                            draw.line([(x_pos, y_position), (x_pos, y_position + row_height)], 
                                     fill=table_border_color, width=1)
                            
                            x_pos += cell_width
                    
                    # 绘制右侧边框和底部边框
                    draw.line([(img_width - padding, y_position), 
                               (img_width - padding, y_position + row_height)], 
                             fill=table_border_color, width=1)
                    draw.line([(table_left, y_position + row_height), 
                               (img_width - padding, y_position + row_height)], 
                             fill=table_border_color, width=1)
                    
                    y_position += row_height
                
                y_position += 10  # 表格底部额外间距
            
            elif element.name in ('ul', 'ol'):
                for idx, item in enumerate(element.find_all('li')):
                    bullet = '• ' if element.name == 'ul' else f"{idx+1}. "
                    item_text = bullet + item.text
                    wrapped_lines = textwrap.wrap(item_text, width=65)  # 略窄以容纳缩进
                    
                    for line_idx, line in enumerate(wrapped_lines):
                        # 第一行使用项目符号，后续行缩进对齐
                        if line_idx == 0:
                            draw.text((padding, y_position), line, font=regular_font, fill=text_color)
                        else:
                            # 缩进与项目符号对齐
                            indent = draw.textlength(bullet, font=regular_font)
                            draw.text((padding + indent, y_position), line, font=regular_font, fill=text_color)
                        y_position += line_height
                
                y_position += 5  # 列表底部额外间距
            
            elif element.name == 'pre':
                # 绘制代码块
                code_text = element.text.strip()
                code_lines = code_text.split('\n')
                
                # 代码块背景
                code_bg_height = len(code_lines) * line_height + 15
                draw.rectangle([(padding - 5, y_position - 5), 
                                (img_width - padding + 5, y_position + code_bg_height)], 
                              fill=(245, 245, 245))  # 浅灰色背景
                
                for code_line in code_lines:
                    draw.text((padding + 5, y_position + 5), code_line, font=small_font, fill=text_color)
                    y_position += line_height
                
                y_position += 15  # 代码块底部额外间距
        
        # 添加水印和二维码
        # 对角线水印
        watermark_font = regular_font
        watermark_text_width = draw.textlength(watermark, font=watermark_font)
        
        for i in range(0, img_width + img_height, 300):
            x = max(0, i - img_height)
            y = max(0, img_height - i)
            draw.text((x + 50, y + 50), watermark, font=watermark_font, fill=watermark_color)
        
        # 添加免责声明底部水印
        disclaimer = "免责声明：本分析仅供专业参考，不构成投资建议，交易决策请自行承担风险"
        draw.text((padding, img_height - 40), disclaimer, font=small_font, fill=text_color)
        
        # 创建二维码
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data("https://t.me/jin10light")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        # 调整二维码大小
        qr_size = 80
        qr_img = qr_img.resize((qr_size, qr_size))
        
        # 将二维码放在右下角
        image.paste(qr_img, (img_width - qr_size - padding, img_height - qr_size - padding))
        
        # 保存到内存
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        
        return buffer
    except Exception as e:
        logger.error(f"生成图片失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


# 添加主函数
async def main():
    """主函数，获取数据并生成分析报告"""
    try:
        logger.info("开始执行资金流向分析程序")
        
        # 检查配置是否完整
        if not config.has_section('API') or not config.has_section('TELEGRAM'):
            logger.error("配置文件缺失必要部分，请检查config.ini文件")
            print("配置文件缺失必要部分，请检查config.ini文件")
            return
            
        # 检查API密钥
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            logger.error("Binance API密钥未配置")
            print("Binance API密钥未配置，请在config.ini中设置")
            return
            
        # 尝试连接Binance API
        try:
            client.get_system_status()
            logger.info("Binance API连接正常")
        except Exception as e:
            logger.error(f"Binance API连接失败: {e}")
            print(f"Binance API连接失败: {e}")
            return
        
        all_results = {}
        
        # 遍历所有交易对获取数据
        for symbol in SYMBOLS:
            logger.info(f"开始获取 {symbol} 数据")
            
            # 获取现货K线数据
            spot_klines = get_klines_data(symbol, interval='5m', limit=50, is_futures=False)
            if not spot_klines:
                logger.warning(f"{symbol} 现货K线数据获取失败")
                
            # 获取期货K线数据
            futures_klines = get_klines_data(symbol, interval='5m', limit=50, is_futures=True)
            if not futures_klines:
                logger.warning(f"{symbol} 期货K线数据获取失败")
                
            # 获取订单簿数据
            spot_orderbook = get_orderbook_stats(symbol, is_futures=False)
            futures_orderbook = get_orderbook_stats(symbol, is_futures=True)
            
            # 分析资金流向趋势
            spot_trend = analyze_funding_flow_trend(spot_klines)
            futures_trend = analyze_funding_flow_trend(futures_klines)
            
            # 检测异常交易
            spot_anomalies = detect_anomalies(spot_klines)
            futures_anomalies = detect_anomalies(futures_klines)
            
            # 分析资金压力
            spot_pressure = analyze_funding_pressure(spot_klines, spot_orderbook)
            futures_pressure = analyze_funding_pressure(futures_klines, futures_orderbook)
            
            # 汇总结果
            all_results[symbol] = {
                'spot': {
                    'klines': spot_klines,
                    'orderbook': spot_orderbook,
                    'trend': spot_trend,
                    'anomalies': spot_anomalies,
                    'pressure': spot_pressure
                },
                'futures': {
                    'klines': futures_klines,
                    'orderbook': futures_orderbook,
                    'trend': futures_trend,
                    'anomalies': futures_anomalies,
                    'pressure': futures_pressure
                }
            }
            
            logger.info(f"{symbol} 数据处理完成")
            
        # 发送数据到DeepSeek进行解读
        logger.info("发送数据到DeepSeek进行解读")
        analysis_result = send_to_deepseek(all_results)
        
        # 发送结果到Telegram
        logger.info("发送分析结果到Telegram")
        await send_telegram_message_async(analysis_result, as_image=True)
        
        logger.info("资金流向分析完成")
        print("资金流向分析已完成并发送到Telegram")
        
    except Exception as e:
        logger.error(f"执行过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"执行过程中发生错误: {e}")

# 入口点
if __name__ == "__main__":
    try:
        # 检查配置文件是否存在
        if not os.path.exists('config.ini'):
            print("错误: 配置文件 config.ini 不存在")
            print("请创建config.ini文件并填写必要配置")
            print("格式例如:")
            print("[API]")
            print("BINANCE_API_KEY = 你的Binance API KEY")
            print("BINANCE_API_SECRET = 你的Binance API SECRET")
            print("DEEPSEEK_API_KEY = 你的DeepSeek API KEY")
            print("[TELEGRAM]")
            print("BOT_TOKEN = 你的Telegram Bot Token")
            print("CHAT_ID = 你的Telegram聊天ID")
            exit(1)
            
        print("开始执行Binance资金流向分析...")
        # 执行主函数
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序执行失败: {e}")
        import traceback
        traceback.print_exc()

