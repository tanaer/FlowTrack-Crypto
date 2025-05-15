"""本地分析回退模块

当外部API调用失败时，提供本地分析功能。
"""
import logging
from typing import Dict

# 配置日志
logger = logging.getLogger(__name__)


def generate_fallback_analysis(data: Dict) -> str:
    """当API调用全部失败时，生成基本分析结果
    
    Args:
        data: 简化后的交易数据
        
    Returns:
        Markdown格式的分析报告
    """
    logger.info("使用本地分析逻辑生成基本分析结果")
    
    analysis = "# Telegram @jin10light 区块链资金流向报告\n\n"
    analysis += "## 一、资金行为解读\n\n"
    
    for symbol, symbol_data in data.items():
        # 使用安全获取方法，避免KeyError
        spot_data = symbol_data.get('spot', {})
        futures_data = symbol_data.get('futures', {})
        
        # 安全获取各种数据，提供默认值防止键不存在
        spot_trend = get_safe_data(spot_data, 'trend', {})
        futures_trend = get_safe_data(futures_data, 'trend', {})
        spot_pressure = get_safe_data(spot_data, 'pressure', {})
        futures_pressure = get_safe_data(futures_data, 'pressure', {})
        
        # 安全获取趋势值和置信度
        trend_value = get_safe_data(spot_trend, 'trend', '未知')
        trend_confidence = get_safe_data(spot_trend, 'confidence', 0)
        futures_trend_value = get_safe_data(futures_trend, 'trend', '未知')
        futures_trend_confidence = get_safe_data(futures_trend, 'confidence', 0)
        
        # 安全获取压力方向和强度
        spot_pressure_direction = get_safe_data(spot_pressure, 'direction', '未知')
        spot_pressure_strength = get_safe_data(spot_pressure, 'strength', 0) 
        futures_pressure_direction = get_safe_data(futures_pressure, 'direction', '未知')
        futures_pressure_strength = get_safe_data(futures_pressure, 'strength', 0)
        
        analysis += f"### {symbol} 分析\n\n"
        analysis += f"- **现货趋势**: {trend_value}, 置信度: {trend_confidence:.2f}\n"
        analysis += f"- **期货趋势**: {futures_trend_value}, 置信度: {futures_trend_confidence:.2f}\n"
        analysis += f"- **现货资金压力**: {spot_pressure_direction}, 强度: {spot_pressure_strength:.2f}\n"
        analysis += f"- **期货资金压力**: {futures_pressure_direction}, 强度: {futures_pressure_strength:.2f}\n\n"
        
        # 处理技术指标
        spot_technical = get_safe_data(spot_data, 'technical_indicators', {})
        futures_technical = get_safe_data(futures_data, 'technical_indicators', {})
        if spot_technical or futures_technical:
            analysis += "**技术指标**:\n\n"
            if spot_technical:
                rsi = get_safe_data(spot_technical, 'rsi_14', '未知')
                analysis += f"- 现货RSI(14): {rsi if isinstance(rsi, str) else f'{rsi:.2f}'}\n"
                
            if futures_technical:
                rsi = get_safe_data(futures_technical, 'rsi_14', '未知')
                analysis += f"- 期货RSI(14): {rsi if isinstance(rsi, str) else f'{rsi:.2f}'}\n"
            
            analysis += "\n"
        
        # 处理异常情况
        spot_anomalies = get_safe_data(spot_data, 'anomalies', [])
        futures_anomalies = get_safe_data(futures_data, 'anomalies', [])
        
        if spot_anomalies or futures_anomalies:
            analysis += "**异常情况**:\n\n"
            
            if spot_anomalies:
                analysis += "现货异常:\n"
                for anomaly in spot_anomalies:
                    analysis += f"- {anomaly.get('type', '未知类型')} 于 {anomaly.get('timestamp', '未知时间')}\n"
            
            if futures_anomalies:
                analysis += "期货异常:\n"
                for anomaly in futures_anomalies:
                    analysis += f"- {anomaly.get('type', '未知类型')} 于 {anomaly.get('timestamp', '未知时间')}\n"
            
            analysis += "\n"
    
    analysis += "## 二、策略建议\n\n"
    
    # 生成交易策略表格
    analysis += "\n[STRATEGY_TABLE_BEGIN]\n"
    analysis += "| 交易对 | 类型 | 方向 | 入场 | 止损 | 止盈 | 建议仓位 | 持仓周期 | 信心指数 |\n"
    analysis += "|--------|------|------|------|------|------|----------|----------|----------|\n"
    
    for symbol, symbol_data in data.items():
        # 安全获取数据
        spot_data = symbol_data.get('spot', {})
        futures_data = symbol_data.get('futures', {})
        
        spot_trend_data = get_safe_data(spot_data, 'trend', {})
        futures_trend_data = get_safe_data(futures_data, 'trend', {})
        
        spot_trend = get_safe_data(spot_trend_data, 'trend', 'unknown')
        futures_trend = get_safe_data(futures_trend_data, 'trend', 'unknown')
        
        # 生成模拟交易策略
        direction = "观望"
        entry = "-"
        stop_loss = "-"
        take_profit = "-"
        position_size = "0%"
        holding_period = "-"
        confidence = 5  # 默认中等自信度
        
        # 基于趋势生成简单策略
        if spot_trend == 'rising' and futures_trend == 'rising':
            direction = "做多"
            # 模拟生成一些简单的价格数据用于演示
            current_price = get_safe_data(symbol_data, 'price', 0)
            if current_price == 0:
                # 尝试从orderbook中获取价格
                orderbook = get_safe_data(spot_data, 'orderbook_summary', {})
                current_price = get_safe_data(orderbook, 'price', 1000)  # 默认价格如果无法获取
            
            # 设置入场、止损和止盈价格
            entry = f"{current_price:.2f}"
            stop_loss = f"{current_price * 0.98:.2f}"
            take_profit = f"{current_price * 1.05:.2f}"
            position_size = "30%"
            holding_period = "中线"
            confidence = 7
        elif spot_trend == 'falling' and futures_trend == 'falling':
            direction = "做空"
            # 模拟生成价格
            current_price = get_safe_data(symbol_data, 'price', 0)
            if current_price == 0:
                orderbook = get_safe_data(spot_data, 'orderbook_summary', {})
                current_price = get_safe_data(orderbook, 'price', 1000)
            
            entry = f"{current_price:.2f}"
            stop_loss = f"{current_price * 1.02:.2f}"
            take_profit = f"{current_price * 0.95:.2f}"
            position_size = "30%"
            holding_period = "中线"
            confidence = 7
        
        # 添加到表格
        market_type = "合约" if futures_trend != 'unknown' else "现货"
        analysis += f"| {symbol} | {market_type} | {direction} | {entry} | {stop_loss} | {take_profit} | {position_size} | {holding_period} | {confidence} |\n"
    
    analysis += "[STRATEGY_TABLE_END]\n"
    
    return analysis


def get_safe_data(data_dict: Dict, key: str, default_value):
    """安全获取字典中的值，避免KeyError
    
    Args:
        data_dict: 数据字典
        key: 要获取的键
        default_value: 键不存在时返回的默认值
        
    Returns:
        键对应的值或默认值
    """
    if not isinstance(data_dict, dict):
        return default_value
    return data_dict.get(key, default_value) 