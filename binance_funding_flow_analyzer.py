import requests
import pandas as pd
import numpy as np
from datetime import datetime
import time
import concurrent.futures
import telegram
from telegram.ext import Updater
import json
import pickle
from ratelimit import limits, sleep_and_retry
from binance.client import Client
from typing import Dict, List
import logging
from scipy import stats
import configparser

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
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SUIUSDT', 'TONUSDT', 'PNUTSUSDT']


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
    """获取K线数据，并剔除最新的一根（未完成的）"""
    try:
        base_url = FUTURES_BASE_URL if is_futures else SPOT_BASE_URL
        endpoint = "/klines"
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        response = requests.get(f"{base_url}{endpoint}", params=params)
        data = response.json()

        if len(data) < 2:
            logger.warning(f"需要至少2根K线数据，但{symbol}只返回了{len(data)}根")
            return []

        # 剔除最新的一根K线（未完成的）
        data = data[:-1]

        results = []
        for k in data:
            open_time = datetime.fromtimestamp(k[0] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            close_time = datetime.fromtimestamp(k[6] / 1000).strftime('%Y-%m-%d %H:%M:%S')

            results.append({
                'symbol': symbol,
                'open_time': open_time,
                'close_time': close_time,
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
                'quote_volume': float(k[7]),
                'trades': int(k[8]),
                'taker_buy_base_volume': float(k[9]),
                'taker_buy_quote_volume': float(k[10]),
                'net_inflow': float(k[10]) - (float(k[7]) - float(k[10])),  # 买方成交量 - 卖方成交量
                'timestamp': k[0]  # 保存时间戳用于排序
            })

        return results
    except Exception as e:
        logger.error(f"获取{symbol} K线数据时出错: {e}")
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
    with open(f"binance_analysis.md", "w", encoding="utf-8") as f:
        f.write(analysis)
    logger.info(f"分析结果已保存到 binance_analysis_{timestamp}.md")

    # 打印分析结果
    print("\n分析结果:")
    print(analysis)


if __name__ == "__main__":
    main_optimized()
