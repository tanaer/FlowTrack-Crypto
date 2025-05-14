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
        spot_trend = symbol_data['spot']['trend']
        futures_trend = symbol_data['futures']['trend']
        spot_pressure = symbol_data['spot']['pressure']
        futures_pressure = symbol_data['futures']['pressure']
        
        analysis += f"### {symbol} 分析\n\n"
        analysis += f"- **现货趋势**: {spot_trend.get('trend', '未知')}, 置信度: {spot_trend.get('confidence', 0):.2f}\n"
        analysis += f"- **期货趋势**: {futures_trend.get('trend', '未知')}, 置信度: {futures_trend.get('confidence', 0):.2f}\n"
        analysis += f"- **现货资金压力**: {spot_pressure.get('direction', '未知')}, 强度: {spot_pressure.get('strength', 0):.2f}\n"
        analysis += f"- **期货资金压力**: {futures_pressure.get('direction', '未知')}, 强度: {futures_pressure.get('strength', 0):.2f}\n\n"
        
        # 处理异常情况
        spot_anomalies = symbol_data['spot'].get('anomalies', [])
        futures_anomalies = symbol_data['futures'].get('anomalies', [])
        
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
    
    for symbol, symbol_data in data.items():
        spot_trend = symbol_data['spot']['trend']['trend'] if 'trend' in symbol_data['spot'] and 'trend' in symbol_data['spot']['trend'] else "未知"
        futures_trend = symbol_data['futures']['trend']['trend'] if 'trend' in symbol_data['futures'] and 'trend' in symbol_data['futures']['trend'] else "未知"
        
        analysis += f"### {symbol} 策略\n\n"
        
        # 基于趋势提供简单建议
        if spot_trend == 'rising' and futures_trend == 'rising':
            analysis += "**建议**: 可考虑逢低做多，注意设置止损\n\n"
        elif spot_trend == 'falling' and futures_trend == 'falling':
            analysis += "**建议**: 可考虑逢高做空，注意设置止损\n\n"
        elif spot_trend == 'consolidation' or futures_trend == 'consolidation':
            analysis += "**建议**: 目前处于整理阶段，建议观望等待突破\n\n"
        elif spot_trend == 'top' or futures_trend == 'top':
            analysis += "**建议**: 可能处于顶部区域，谨慎操作，可考虑减仓\n\n"
        elif spot_trend == 'bottom' or futures_trend == 'bottom':
            analysis += "**建议**: 可能处于底部区域，可考虑分批建仓\n\n"
        else:
            analysis += "**建议**: 当前趋势不明确，建议观望或轻仓操作\n\n"
    
    return analysis 