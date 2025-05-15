"""币安API客户端模块

负责与币安API交互，获取市场数据。
"""
import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
from binance.client import Client
from binance.exceptions import BinanceAPIException
from ratelimit import limits, sleep_and_retry
from ..config import BINANCE_API_KEY, BINANCE_API_SECRET

# 配置日志
logger = logging.getLogger(__name__)

# 初始化Binance客户端
try:
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
except Exception as e:
    logger.error(f"Binance客户端初始化失败: {e}")
    raise

@sleep_and_retry
@limits(calls=20, period=1)
def get_klines_data(symbol: str, interval: str = '1h', limit: int = 200, is_futures: bool = False) -> List[Dict]:
    """获取K线数据，并剔除最新的一根（未完成的）
    
    使用python-binance库获取K线数据：
    - 现货: client.get_klines
    - 期货: client.futures_klines
    
    参数:
        symbol: 交易对名称
        interval: K线周期 (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M)
        limit: 获取的K线数量，默认200，最大1500
        is_futures: 是否为期货
        
    返回:
        K线数据列表，已剔除最新的未完成K线
    """
    try:
        # 检查并限制limit参数
        if limit > 1500:
            logger.warning(f"请求的limit({limit})超过最大值1500，已自动调整为1500")
            limit = 1500
            
        # 使用python-binance库获取K线数据
        logger.info(f"正在获取 {symbol} {'期货' if is_futures else '现货'} {interval} K线数据...")
        
        # 根据是否为期货选择不同的API调用
        if is_futures:
            data = client.futures_klines(symbol=symbol, interval=interval, limit=limit + 1)
        else:
            data = client.get_klines(symbol=symbol, interval=interval, limit=limit + 1)

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
    """获取单个交易对的深度统计数据（现货5000档，期货1000档）
    
    使用python-binance库获取订单簿数据
    """
    limit = 1000 if is_futures else 5000  # 期货支持最大1000档，现货支持5000档
    for attempt in range(retries):
        try:
            # 使用python-binance库获取订单簿和当前价格
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

            # 新增：计算订单簿热力图数据（价格分布）
            price_bins = 20  # 划分20个价格区间
            price_range = 0.02  # 当前价格上下2%
            
            lower_price = current_price * (1 - price_range)
            upper_price = current_price * (1 + price_range)
            
            # 创建价格区间
            price_buckets = np.linspace(lower_price, upper_price, price_bins + 1)
            
            # 初始化热力图数据
            heatmap_data = {'bids': [0] * price_bins, 'asks': [0] * price_bins}
            
            # 计算每个价格区间的订单量
            for price, amount in bids:
                if lower_price <= price <= upper_price:
                    bucket_idx = min(price_bins - 1, int((price - lower_price) / (upper_price - lower_price) * price_bins))
                    heatmap_data['bids'][bucket_idx] += amount
            
            for price, amount in asks:
                if lower_price <= price <= upper_price:
                    bucket_idx = min(price_bins - 1, int((price - lower_price) / (upper_price - lower_price) * price_bins))
                    heatmap_data['asks'][bucket_idx] += amount
            
            # 计算价格支撑/阻力区
            support_resistance = []
            for i in range(price_bins):
                # 如果买单量明显大于周围区域，视为支撑
                if i > 0 and i < price_bins - 1:
                    if heatmap_data['bids'][i] > 1.5 * ((heatmap_data['bids'][i-1] + heatmap_data['bids'][i+1]) / 2):
                        price_level = lower_price + (i + 0.5) * (upper_price - lower_price) / price_bins
                        support_resistance.append({
                            'type': 'support',
                            'price': price_level,
                            'strength': heatmap_data['bids'][i]
                        })
                    # 如果卖单量明显大于周围区域，视为阻力
                    if heatmap_data['asks'][i] > 1.5 * ((heatmap_data['asks'][i-1] + heatmap_data['asks'][i+1]) / 2):
                        price_level = lower_price + (i + 0.5) * (upper_price - lower_price) / price_bins
                        support_resistance.append({
                            'type': 'resistance',
                            'price': price_level,
                            'strength': heatmap_data['asks'][i]
                        })

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
                'near_volume_imbalance': near_volume_imbalance,
                'orderbook_heatmap': heatmap_data,
                'support_resistance': support_resistance
            }
        except Exception as e:
            logger.error(f"获取 {symbol} orderbook 失败 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return None
    return None


@sleep_and_retry
@limits(calls=10, period=1)
def get_recent_trades(symbol: str, limit: int = 1000, is_futures: bool = False) -> List[Dict]:
    """获取最近成交记录
    
    参数:
        symbol: 交易对名称
        limit: 获取的成交记录数量，最大1000
        is_futures: 是否为期货
        
    返回:
        最近成交记录列表
    """
    try:
        logger.info(f"正在获取 {symbol} {'期货' if is_futures else '现货'} 最近成交记录...")
        
        if is_futures:
            trades = client.futures_recent_trades(symbol=symbol, limit=limit)
        else:
            trades = client.get_recent_trades(symbol=symbol, limit=limit)
            
        result = []
        for trade in trades:
            result.append({
                'id': trade['id'],
                'price': float(trade['price']),
                'qty': float(trade['qty']),
                'quoteQty': float(trade['quoteQty']),
                'time': datetime.fromtimestamp(trade['time'] / 1000).strftime('%Y-%m-%d %H:%M:%S'),
                'isBuyerMaker': trade['isBuyerMaker'],
                'timestamp': trade['time']
            })
            
        # 分析大单情况
        if result:
            qty_std = np.std([t['qty'] for t in result])
            qty_mean = np.mean([t['qty'] for t in result])
            
            # 标记大单（超过2个标准差）
            for trade in result:
                trade['is_large_order'] = trade['qty'] > (qty_mean + 2 * qty_std)
                
            # 计算大单统计
            large_orders = [t for t in result if t['is_large_order']]
            large_buy_orders = [t for t in large_orders if not t['isBuyerMaker']]
            large_sell_orders = [t for t in large_orders if t['isBuyerMaker']]
            
            large_order_stats = {
                'total_count': len(large_orders),
                'buy_count': len(large_buy_orders),
                'sell_count': len(large_sell_orders),
                'buy_volume': sum(t['qty'] for t in large_buy_orders),
                'sell_volume': sum(t['qty'] for t in large_sell_orders),
                'buy_value': sum(t['quoteQty'] for t in large_buy_orders),
                'sell_value': sum(t['quoteQty'] for t in large_sell_orders)
            }
            
            return {
                'trades': result,
                'large_order_stats': large_order_stats
            }
        
        return {'trades': result, 'large_order_stats': None}
    except Exception as e:
        logger.error(f"获取 {symbol} 最近成交记录失败: {e}")
        return {'trades': [], 'large_order_stats': None}


@sleep_and_retry
@limits(calls=2, period=1)
def get_funding_rate(symbol: str, limit: int = 100) -> List[Dict]:
    """获取资金费率历史
    
    参数:
        symbol: 交易对名称
        limit: 获取的记录数量，最大1000
        
    返回:
        资金费率历史列表
    """
    try:
        logger.info(f"正在获取 {symbol} 资金费率历史...")
        funding_history = client.futures_funding_rate(symbol=symbol, limit=limit)
        
        result = []
        for item in funding_history:
            result.append({
                'symbol': item['symbol'],
                'fundingRate': float(item['fundingRate']),
                'fundingTime': datetime.fromtimestamp(item['fundingTime'] / 1000).strftime('%Y-%m-%d %H:%M:%S'),
                'timestamp': item['fundingTime']
            })
        
        # 计算资金费率统计
        if result:
            rates = [item['fundingRate'] for item in result]
            
            # 添加资金费率统计
            funding_stats = {
                'current': rates[0],
                'mean': np.mean(rates),
                'std': np.std(rates),
                'max': max(rates),
                'min': min(rates),
                'is_extreme': abs(rates[0]) > (abs(np.mean(rates)) + 2 * np.std(rates))
            }
            
            return {
                'history': result,
                'stats': funding_stats
            }
        
        return {'history': result, 'stats': None}
    except Exception as e:
        logger.error(f"获取 {symbol} 资金费率历史失败: {e}")
        return {'history': [], 'stats': None}


@sleep_and_retry
@limits(calls=1, period=1)
def get_long_short_ratio(symbol: str, period: str = '1h', limit: int = 30) -> Dict:
    """获取多空持仓比例
    
    参数:
        symbol: 交易对名称
        period: 时间周期，可选: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
        limit: 获取的记录数量，最大500
        
    返回:
        多空持仓比例数据
    """
    try:
        logger.info(f"正在获取 {symbol} 多空持仓比例...")
        ratio_data = client.futures_top_long_short_position_ratio(symbol=symbol, period=period, limit=limit)
        
        result = []
        for item in ratio_data:
            result.append({
                'symbol': item['symbol'],
                'longShortRatio': float(item['longShortRatio']),
                'longAccount': float(item['longAccount']),
                'shortAccount': float(item['shortAccount']),
                'timestamp': item['timestamp'],
                'datetime': datetime.fromtimestamp(item['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            })
        
        # 计算多空比例变化趋势
        if len(result) > 1:
            # 最近一次的多空比变化
            current_ratio = result[0]['longShortRatio']
            prev_ratio = result[1]['longShortRatio']
            ratio_change = current_ratio - prev_ratio
            
            # 多空比例的平均值和标准差
            ratios = [item['longShortRatio'] for item in result]
            ratio_mean = np.mean(ratios)
            ratio_std = np.std(ratios)
            
            # 判断当前多空比是否处于极端状态
            is_extreme = abs(current_ratio - ratio_mean) > 2 * ratio_std
            
            # 多空趋势
            ratio_trend = []
            for i in range(1, len(result)):
                ratio_trend.append(result[i-1]['longShortRatio'] - result[i]['longShortRatio'])
            
            trend_direction = 'increasing' if np.mean(ratio_trend) > 0 else 'decreasing'
            
            return {
                'data': result,
                'stats': {
                    'current_ratio': current_ratio,
                    'ratio_change': ratio_change,
                    'ratio_mean': ratio_mean,
                    'ratio_std': ratio_std,
                    'is_extreme': is_extreme,
                    'trend': trend_direction
                }
            }
        
        return {'data': result, 'stats': None}
    except Exception as e:
        logger.error(f"获取 {symbol} 多空持仓比例失败: {e}")
        return {'data': [], 'stats': None}


@sleep_and_retry
@limits(calls=2, period=1)
def get_open_interest(symbol: str, period: str = '1h', limit: int = 30) -> Dict:
    """获取未平仓合约量
    
    参数:
        symbol: 交易对名称
        period: 时间周期，可选: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
        limit: 获取的记录数量，最大500
        
    返回:
        未平仓合约量数据
    """
    try:
        logger.info(f"正在获取 {symbol} 未平仓合约量...")
        oi_data = client.futures_open_interest_hist(symbol=symbol, period=period, limit=limit)
        
        result = []
        for item in oi_data:
            result.append({
                'symbol': item['symbol'],
                'sumOpenInterest': float(item['sumOpenInterest']),
                'sumOpenInterestValue': float(item['sumOpenInterestValue']),
                'timestamp': item['timestamp'],
                'datetime': datetime.fromtimestamp(item['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            })
        
        # 计算未平仓合约量变化
        if len(result) > 1:
            # 计算变化率
            current_oi = result[0]['sumOpenInterest']
            prev_oi = result[1]['sumOpenInterest']
            oi_change_pct = (current_oi - prev_oi) / prev_oi * 100 if prev_oi > 0 else 0
            
            # 计算未平仓合约量的均值和标准差
            oi_values = [item['sumOpenInterest'] for item in result]
            oi_mean = np.mean(oi_values)
            oi_std = np.std(oi_values)
            
            # 判断是否处于极端状态
            is_extreme = abs(current_oi - oi_mean) > 2 * oi_std
            
            # 计算变化趋势
            oi_changes = []
            for i in range(1, len(result)):
                change = (result[i-1]['sumOpenInterest'] - result[i]['sumOpenInterest']) / result[i]['sumOpenInterest'] * 100
                oi_changes.append(change)
            
            trend_direction = 'increasing' if np.mean(oi_changes) > 0 else 'decreasing'
            
            return {
                'data': result,
                'stats': {
                    'current_oi': current_oi,
                    'oi_change_pct': oi_change_pct,
                    'oi_mean': oi_mean,
                    'oi_std': oi_std,
                    'is_extreme': is_extreme,
                    'trend': trend_direction
                }
            }
        
        return {'data': result, 'stats': None}
    except Exception as e:
        logger.error(f"获取 {symbol} 未平仓合约量失败: {e}")
        return {'data': [], 'stats': None}


def calculate_technical_indicators(klines_data: List[Dict]) -> Dict:
    """计算技术指标
    
    参数:
        klines_data: K线数据列表
        
    返回:
        技术指标字典
    """
    if not klines_data or len(klines_data) < 20:
        return {'error': '数据不足，无法计算技术指标'}
    
    try:
        # 将K线数据转换为pandas DataFrame
        df = pd.DataFrame(klines_data)
        df = df.sort_values('timestamp')
        
        # 提取价格和成交量
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        volumes = df['volume'].values
        
        # 计算ATR (Average True Range)
        true_ranges = []
        for i in range(1, len(closes)):
            true_range = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(true_range)
        
        atr_14 = np.mean(true_ranges[-14:]) if len(true_ranges) >= 14 else None
        
        # 计算波动率 (20周期标准差/平均值)
        volatility_20 = np.std(closes[-20:]) / np.mean(closes[-20:]) if len(closes) >= 20 else None
        
        # 计算RSI (Relative Strength Index)
        changes = np.diff(closes)
        gains = changes.copy()
        losses = changes.copy()
        gains[gains < 0] = 0
        losses[losses > 0] = 0
        losses = abs(losses)
        
        # 14周期RSI
        if len(gains) >= 14 and len(losses) >= 14:
            avg_gain = np.mean(gains[-14:])
            avg_loss = np.mean(losses[-14:])
            
            if avg_loss == 0:
                rsi_14 = 100
            else:
                rs = avg_gain / avg_loss
                rsi_14 = 100 - (100 / (1 + rs))
        else:
            rsi_14 = None
        
        # 计算移动平均线
        ma_5 = np.mean(closes[-5:]) if len(closes) >= 5 else None
        ma_10 = np.mean(closes[-10:]) if len(closes) >= 10 else None
        ma_20 = np.mean(closes[-20:]) if len(closes) >= 20 else None
        ma_50 = np.mean(closes[-50:]) if len(closes) >= 50 else None
        
        # 计算布林带 (20周期)
        if len(closes) >= 20:
            middle_band = ma_20
            std_dev = np.std(closes[-20:])
            upper_band = middle_band + 2 * std_dev
            lower_band = middle_band - 2 * std_dev
            
            # 计算布林带宽度
            bb_width = (upper_band - lower_band) / middle_band
            
            # 计算价格相对布林带位置
            current_close = closes[-1]
            bb_position = (current_close - lower_band) / (upper_band - lower_band) if (upper_band - lower_band) > 0 else 0.5
        else:
            middle_band = upper_band = lower_band = bb_width = bb_position = None
        
        # 计算MACD (Moving Average Convergence Divergence)
        if len(closes) >= 26:
            # 计算EMA (Exponential Moving Average)
            ema_12 = np.zeros(len(closes))
            ema_26 = np.zeros(len(closes))
            
            # 初始化EMA
            ema_12[0] = closes[0]
            ema_26[0] = closes[0]
            
            # 计算EMA
            for i in range(1, len(closes)):
                ema_12[i] = (closes[i] - ema_12[i-1]) * (2 / (12 + 1)) + ema_12[i-1]
                ema_26[i] = (closes[i] - ema_26[i-1]) * (2 / (26 + 1)) + ema_26[i-1]
            
            # 计算MACD线和信号线
            macd_line = ema_12 - ema_26
            
            # 初始化信号线 (9周期EMA of MACD)
            signal_line = np.zeros(len(macd_line))
            signal_line[0] = macd_line[0]
            
            for i in range(1, len(macd_line)):
                signal_line[i] = (macd_line[i] - signal_line[i-1]) * (2 / (9 + 1)) + signal_line[i-1]
            
            # 计算MACD柱状图
            macd_histogram = macd_line - signal_line
            
            # 提取最新值
            current_macd = macd_line[-1]
            current_signal = signal_line[-1]
            current_histogram = macd_histogram[-1]
        else:
            current_macd = current_signal = current_histogram = None
        
        return {
            'atr_14': atr_14,
            'volatility_20': volatility_20,
            'rsi_14': rsi_14,
            'ma_5': ma_5,
            'ma_10': ma_10,
            'ma_20': ma_20,
            'ma_50': ma_50,
            'bb_middle': middle_band,
            'bb_upper': upper_band,
            'bb_lower': lower_band,
            'bb_width': bb_width,
            'bb_position': bb_position,
            'macd': current_macd,
            'macd_signal': current_signal,
            'macd_histogram': current_histogram,
            'current_price': closes[-1]
        }
    except Exception as e:
        logger.error(f"计算技术指标失败: {e}")
        return {'error': f'计算技术指标失败: {e}'}


def get_short_term_trading_data(symbol: str, is_futures: bool = False) -> Dict:
    """获取短线交易所需的全面数据
    
    整合多个API调用，获取短线交易所需的全面数据集
    
    参数:
        symbol: 交易对名称
        is_futures: 是否为期货
        
    返回:
        整合的数据字典
    """
    result = {'symbol': symbol, 'market_type': 'futures' if is_futures else 'spot'}
    
    # 1. 获取多个时间周期的K线数据
    result['klines'] = {}
    for interval in ['1m', '5m', '15m', '1h']:
        limit = 200 if interval == '1m' else 100
        klines = get_klines_data(symbol, interval=interval, limit=limit, is_futures=is_futures)
        result['klines'][interval] = klines
    
    # 2. 获取订单簿数据
    result['orderbook'] = get_orderbook_stats(symbol, is_futures=is_futures)
    
    # 3. 获取最近成交记录
    recent_trades = get_recent_trades(symbol, is_futures=is_futures)
    result['recent_trades'] = recent_trades
    
    # 只有期货才有以下数据
    if is_futures:
        # 4. 获取资金费率数据
        funding_rate = get_funding_rate(symbol)
        result['funding_rate'] = funding_rate
        
        # 5. 获取多空持仓比例
        long_short_ratio = get_long_short_ratio(symbol)
        result['long_short_ratio'] = long_short_ratio
        
        # 6. 获取未平仓合约量
        open_interest = get_open_interest(symbol)
        result['open_interest'] = open_interest
    
    # 7. 计算技术指标
    if result['klines']['1h']:
        technical_indicators = calculate_technical_indicators(result['klines']['1h'])
        result['technical_indicators'] = technical_indicators
    
    return result 