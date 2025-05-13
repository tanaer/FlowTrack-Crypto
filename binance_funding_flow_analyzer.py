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
from typing import Dict, List
import logging
from scipy import stats
import configparser
import schedule
from PIL import Image, ImageDraw, ImageFont
import io
import textwrap
import os
import asyncio

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


def text_to_image(text, watermark="Telegram: @jin10light"):
    """将文本转换为图片，并添加水印和二维码"""
    try:
        import qrcode
        from PIL import Image, ImageDraw, ImageFont, ImageColor
        from io import BytesIO
        import re
        
        # 设置字体和颜色
        background_color = (255, 255, 255)  # 白色背景
        text_color = (0, 0, 0)  # 黑色文字
        watermark_color = (180, 180, 180)  # 灰色水印
        title_color = (31, 35, 40)  # 深灰色标题
        
        # 准备字体
        try:
            # 尝试使用阿里巴巴普惠体等中文字体
            font_path = "AlibabaPuHuiTi-3-55-Regular.ttf"
            bold_font_path = "AlibabaPuHuiTi-3-65-Medium.ttf"
           
            # 加载字体
            if font_path and os.path.exists(font_path):
                regular_font = ImageFont.truetype(font_path, 18)
                bold_font = ImageFont.truetype(bold_font_path if os.path.exists(bold_font_path) else font_path, 18)
                title_font = ImageFont.truetype(bold_font_path if os.path.exists(bold_font_path) else font_path, 26)
                subtitle_font = ImageFont.truetype(bold_font_path if os.path.exists(bold_font_path) else font_path, 22)
                watermark_font = ImageFont.truetype(font_path, 20)
            else:
                raise FileNotFoundError("找不到字体文件")
                
        except Exception as e:
            logger.warning(f"加载自定义字体失败: {e}，使用默认字体")
            # 如果找不到系统字体，使用默认字体
            regular_font = ImageFont.load_default()
            bold_font = ImageFont.load_default()
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default()
            watermark_font = ImageFont.load_default()
        
        # 解析Markdown
        lines = text.split('\n')
        
        # 计算图片大小
        line_height = 24  # 增加行高以提高可读性
        padding = 30
        table_padding = 8
        
        # 预处理Markdown确定图片宽度
        max_line_length = 0
        in_table = False
        table_columns = []
        table_data = []
        current_table_row = []
        
        for line in lines:
            # 如果是表格分隔线，跳过长度计算
            if re.match(r'^\|[\-\|]+\|$', line):
                in_table = True
                continue
                
            # 如果是表格行
            if line.startswith('|') and line.endswith('|'):
                in_table = True
                columns = [col.strip() for col in line.split('|')[1:-1]]
                if not table_columns and in_table:
                    table_columns = columns
                else:
                    current_table_row = columns
                    if columns:  # 确保不是空行
                        table_data.append(columns)
                        
                # 计算表格行长度（包括边距）
                table_width = sum(len(col) * 11 for col in columns) + (len(columns) + 1) * table_padding
                max_line_length = max(max_line_length, table_width)
            else:
                # 重置表格状态
                if in_table:
                    in_table = False
                    
                # 处理普通文本行，计算最大长度
                # 去除Markdown标记计算实际显示长度
                clean_line = re.sub(r'\*\*(.*?)\*\*', r'\1', line)  # 去除加粗标记
                clean_line = re.sub(r'##+ ', '', clean_line)  # 去除标题标记
                max_line_length = max(max_line_length, len(clean_line) * 11)  # 估计每个字符11像素宽
                
        # 确保至少1000像素宽
        image_width = max(1000, max_line_length + padding * 2)
        
        # 处理表格，增加表格宽度以避免文字溢出
        if table_columns:
            col_widths = []
            for i in range(len(table_columns)):
                # 计算此列的最大宽度
                col_width = len(table_columns[i]) * 11
                for row in table_data:
                    if i < len(row):
                        col_width = max(col_width, len(row[i]) * 11)
                col_widths.append(col_width + table_padding * 2)
                
            # 表格总宽度加上边框
            table_total_width = sum(col_widths) + table_padding * 2
            image_width = max(image_width, table_total_width + padding * 2)
        
        # 估计图片高度
        estimated_lines = len(lines) * 1.5  # 考虑表格和标题可能需要额外空间
        image_height = int(estimated_lines * line_height + padding * 2)
        
        # 创建图片，增加尺寸确保内容完整显示
        image = Image.new('RGB', (image_width, image_height), background_color)
        draw = ImageDraw.Draw(image)
        
        # 绘制文本
        y_position = padding
        in_table = False
        table_header = []
        table_rows = []
        skip_next = False
        
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
                
            # 处理表格分隔线
            if re.match(r'^\|[\-\|]+\|$', line):
                in_table = True
                continue
                
            # 处理表格
            if line.startswith('|') and line.endswith('|'):
                columns = [col.strip() for col in line.split('|')[1:-1]]
                
                if not in_table:  # 表格开始
                    in_table = True
                    table_header = columns
                    table_rows = []
                else:  # 表格行
                    if columns and all(col for col in columns):  # 确保不是空行或分隔线
                        table_rows.append(columns)
                        
                # 如果是最后一行或下一行不是表格，则绘制表格
                if i == len(lines) - 1 or not lines[i+1].startswith('|'):
                    # 计算列宽
                    col_widths = []
                    for col_idx in range(len(table_header)):
                        header_width = draw.textlength(table_header[col_idx], font=bold_font)
                        max_width = header_width
                        for row in table_rows:
                            if col_idx < len(row):
                                cell_text = row[col_idx]
                                # 处理单元格中的加粗文本
                                cell_text = re.sub(r'\*\*(.*?)\*\*', r'\1', cell_text)
                                cell_width = draw.textlength(cell_text, font=regular_font)
                                max_width = max(max_width, cell_width)
                        col_widths.append(max_width + table_padding * 2)
                    
                    # 绘制表格
                    table_width = sum(col_widths)
                    table_x = padding
                    table_y = y_position
                    
                    # 绘制表头
                    x = table_x
                    for col_idx, header in enumerate(table_header):
                        cell_width = col_widths[col_idx]
                        # 绘制单元格背景
                        draw.rectangle([(x, table_y), (x + cell_width, table_y + line_height)], 
                                      fill=(240, 240, 240))
                        # 绘制表头文本(加粗)
                        draw.text((x + table_padding, table_y + 3), header, font=bold_font, fill=text_color)
                        x += cell_width
                    
                    table_y += line_height
                    
                    # 绘制表格行
                    for row in table_rows:
                        x = table_x
                        max_height = line_height  # 默认行高
                        
                        # 绘制行
                        for col_idx, cell in enumerate(row):
                            if col_idx < len(col_widths):
                                cell_width = col_widths[col_idx]
                                
                                # 处理单元格中的加粗文本
                                bold_spans = re.findall(r'\*\*(.*?)\*\*', cell)
                                if bold_spans:
                                    # 有加粗文本，分段绘制
                                    plain_text = re.sub(r'\*\*(.*?)\*\*', r'__BOLD__\1__BOLD__', cell)
                                    parts = plain_text.split('__BOLD__')
                                    offset = 0
                                    using_bold = False
                                    for part in parts:
                                        if not part:  # 跳过空字符串
                                            using_bold = not using_bold
                                            continue
                                            
                                        font = bold_font if using_bold else regular_font
                                        draw.text((x + table_padding + offset, table_y + 3), 
                                                 part, font=font, fill=text_color)
                                        offset += draw.textlength(part, font=font)
                                        using_bold = not using_bold
                                else:
                                    # 无加粗文本，直接绘制
                                    draw.text((x + table_padding, table_y + 3), 
                                             cell, font=regular_font, fill=text_color)
                                
                                # 更新X坐标
                                x += cell_width
                        
                        table_y += max_height
                    
                    # 绘制表格边框
                    for i in range(len(table_header) + 1):
                        x = table_x
                        if i > 0:
                            x += sum(col_widths[:i])
                        draw.line([(x, y_position), (x, table_y)], fill=(200, 200, 200), width=1)
                    
                    for i in range(len(table_rows) + 2):
                        y = y_position + i * line_height
                        draw.line([(table_x, y), (table_x + table_width, y)], 
                                 fill=(200, 200, 200), width=1)
                    
                    y_position = table_y + line_height  # 表格后增加间距
                    in_table = False  # 重置表格状态
                
                continue
                
            # 处理标题（#开头）
            if line.startswith('# '):
                draw.text((padding, y_position), line[2:], font=title_font, fill=title_color)
                y_position += line_height * 1.8
            elif line.startswith('## '):
                draw.text((padding, y_position), line[3:], font=subtitle_font, fill=title_color)
                y_position += line_height * 1.5
            elif line.startswith('### '):
                draw.text((padding, y_position), line[4:], font=bold_font, fill=title_color)
                y_position += line_height * 1.3
            # 处理加粗文本和普通文本
            else:
                # 查找所有加粗文本
                bold_spans = re.findall(r'\*\*(.*?)\*\*', line)
                if bold_spans:
                    # 有加粗文本，分段绘制
                    plain_text = re.sub(r'\*\*(.*?)\*\*', r'__BOLD__\1__BOLD__', line)
                    parts = plain_text.split('__BOLD__')
                    x_position = padding
                    using_bold = False
                    for part in parts:
                        if not part:  # 跳过空字符串
                            using_bold = not using_bold
                            continue
                            
                        font = bold_font if using_bold else regular_font
                        draw.text((x_position, y_position), part, font=font, fill=text_color)
                        x_position += draw.textlength(part, font=font)
                        using_bold = not using_bold
                else:
                    # 无加粗文本，直接绘制
                    draw.text((padding, y_position), line, font=regular_font, fill=text_color)
                
                y_position += line_height
        
        # 修正图片高度，裁剪未使用的空间
        image = image.crop((0, 0, image_width, y_position + padding))
        
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
        
        # 调整二维码大小（适当缩小）
        qr_size = 100
        qr_img = qr_img.resize((qr_size, qr_size))
        
        # 将二维码放在右下角
        image.paste(qr_img, (image_width - qr_size - padding, image.height - qr_size - padding))
        
        # 添加水印文本在左下角
        draw = ImageDraw.Draw(image)
        draw.text((padding, image.height - 30), watermark, font=watermark_font, fill=watermark_color)
        
        # 添加半透明对角线水印
        for i in range(0, image_width + image.height, 300):
            x1 = max(0, i - image.height)
            y1 = max(0, image.height - i)
            x2 = min(i, image_width)
            y2 = min(image.height, i + image_width - image.height)
            draw.text((x1 + 50, y1 + 50), watermark, font=watermark_font, fill=(245, 245, 245))
            
        # 将图片保存到内存缓冲区
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        buffer.seek(0)
        
        return buffer
    except Exception as e:
        logger.error(f"文本转图片失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
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
