"""LLM API客户端模块

负责调用外部LLM API进行分析生成。
"""
import json
import logging
import requests
from typing import Dict
from ..config import config, LLM_API_PROVIDERS
from ..analysis.result_manager import get_latest_results
import numpy as np
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)

def call_llm_api(provider: str, prompt: str) -> str:
    """通用LLM API调用函数
    
    Args:
        provider: API提供商名称，例如'deepseek'或'ppinfra'
        prompt: 要发送的提示文本
        
    Returns:
        API响应内容，或者在失败时抛出异常
    """
    logger.info(f"正在使用 {provider} API 生成分析...")
    
    # 根据提供商配置参数
    if provider == 'deepseek':
        api_url = config.get('API', 'DEEPSEEK_API_URL')
        api_key = config.get('API', 'DEEPSEEK_API_KEY')
        model = config.get('API', 'DEEPSEEK_API_MODEL')
    elif provider == 'ppinfra':
        api_url = config.get('API', 'PPINFRA_API_URL')
        api_key = config.get('API', 'PPINFRA_API_KEY')
        model = config.get('API', 'PPINFRA_API_MODEL')
    else:
        raise ValueError(f"不支持的API提供商: {provider}")
    
    # 准备请求
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.7
    }
    
    # 发送请求
    endpoint = '/chat/completions'
    if not api_url.endswith(endpoint):
        # 确保URL格式正确
        if api_url.endswith('/'):
            api_url = api_url[:-1]
        if not api_url.endswith('/v1') and not api_url.endswith('/v3'):
            api_url = f"{api_url}{endpoint}"
        else:
            api_url = f"{api_url}{endpoint}"
    
    logger.info(f"调用API: {api_url}, 模型: {model}")
    
    response = requests.post(api_url, headers=headers, json=payload)
    response.raise_for_status()  # 如果请求失败，抛出异常
    
    result = response.json()
    
    logger.info(f"{provider} API调用成功")
    return result['choices'][0]['message']['content']


def generate_analysis(data: Dict) -> str:
    """按照配置的顺序尝试使用各个LLM API进行分析，自动拼接最近3次历史分析"""
    # 简化数据，减小请求大小
    simplified_data = {}
    for symbol, symbol_data in data.items():
        simplified_data[symbol] = {
            'spot': {
                'trend': symbol_data['spot']['trend'],
                'pressure': symbol_data['spot']['pressure'],
                'anomalies': symbol_data['spot']['anomalies'][:5] if symbol_data['spot']['anomalies'] else []
            },
            'futures': {
                'trend': symbol_data['futures']['trend'],
                'pressure': symbol_data['futures']['pressure'],
                'anomalies': symbol_data['futures']['anomalies'][:5] if symbol_data['futures']['anomalies'] else []
            }
        }
        
        # 添加订单簿摘要信息
        if symbol_data['spot']['orderbook']:
            simplified_data[symbol]['spot']['orderbook_summary'] = {
                'volume_imbalance': symbol_data['spot']['orderbook'].get('volume_imbalance', 0),
                'value_imbalance': symbol_data['spot']['orderbook'].get('value_imbalance', 0),
                'price': symbol_data['spot']['orderbook'].get('price', 0)
            }
            
        if symbol_data['futures']['orderbook']:
            simplified_data[symbol]['futures']['orderbook_summary'] = {
                'volume_imbalance': symbol_data['futures']['orderbook'].get('volume_imbalance', 0),
                'value_imbalance': symbol_data['futures']['orderbook'].get('value_imbalance', 0),
                'price': symbol_data['futures']['orderbook'].get('price', 0)
            }
            
        # 添加短线交易新增数据
        if 'spot_short_term' in symbol_data:
            # 添加技术指标数据
            if 'technical_indicators' in symbol_data['spot_short_term']:
                simplified_data[symbol]['spot']['technical_indicators'] = symbol_data['spot_short_term']['technical_indicators']
            
            # 添加支撑阻力数据
            if 'orderbook' in symbol_data['spot_short_term'] and 'support_resistance' in symbol_data['spot_short_term']['orderbook']:
                simplified_data[symbol]['spot']['support_resistance'] = symbol_data['spot_short_term']['orderbook']['support_resistance']
            
            # 添加大单交易数据
            if 'recent_trades' in symbol_data['spot_short_term'] and 'large_order_stats' in symbol_data['spot_short_term']['recent_trades']:
                simplified_data[symbol]['spot']['large_orders'] = symbol_data['spot_short_term']['recent_trades']['large_order_stats']
                
            # 添加波动率指标
            if 'klines' in symbol_data['spot_short_term'] and '1h' in symbol_data['spot_short_term']['klines']:
                # 计算价格波动指标
                klines_1h = symbol_data['spot_short_term']['klines']['1h']
                if len(klines_1h) > 20:
                    prices = [k['close'] for k in klines_1h[-20:]]
                    volatility = {
                        'price_volatility': float(np.std(prices) / np.mean(prices)) if np.mean(prices) > 0 else 0,
                        'price_range_pct': float((max(prices) - min(prices)) / min(prices) * 100) if min(prices) > 0 else 0
                    }
                    simplified_data[symbol]['spot']['volatility'] = volatility
        
        if 'futures_short_term' in symbol_data:
            # 添加技术指标数据
            if 'technical_indicators' in symbol_data['futures_short_term']:
                simplified_data[symbol]['futures']['technical_indicators'] = symbol_data['futures_short_term']['technical_indicators']
            
            # 添加支撑阻力数据
            if 'orderbook' in symbol_data['futures_short_term'] and 'support_resistance' in symbol_data['futures_short_term']['orderbook']:
                simplified_data[symbol]['futures']['support_resistance'] = symbol_data['futures_short_term']['orderbook']['support_resistance']
            
            # 添加大单交易数据
            if 'recent_trades' in symbol_data['futures_short_term'] and 'large_order_stats' in symbol_data['futures_short_term']['recent_trades']:
                simplified_data[symbol]['futures']['large_orders'] = symbol_data['futures_short_term']['recent_trades']['large_order_stats']
            
            # 添加资金费率数据
            if 'funding_rate' in symbol_data['futures_short_term'] and symbol_data['futures_short_term']['funding_rate'].get('stats'):
                simplified_data[symbol]['futures']['funding_rate'] = symbol_data['futures_short_term']['funding_rate']['stats']
            
            # 添加多空比例数据
            if 'long_short_ratio' in symbol_data['futures_short_term'] and symbol_data['futures_short_term']['long_short_ratio'].get('stats'):
                simplified_data[symbol]['futures']['long_short_ratio'] = symbol_data['futures_short_term']['long_short_ratio']['stats']
            
            # 添加未平仓合约数据
            if 'open_interest' in symbol_data['futures_short_term'] and symbol_data['futures_short_term']['open_interest'].get('stats'):
                simplified_data[symbol]['futures']['open_interest'] = symbol_data['futures_short_term']['open_interest']['stats']
                
            # 添加波动率指标
            if 'klines' in symbol_data['futures_short_term'] and '1h' in symbol_data['futures_short_term']['klines']:
                # 计算价格波动指标
                klines_1h = symbol_data['futures_short_term']['klines']['1h']
                if len(klines_1h) > 20:
                    prices = [k['close'] for k in klines_1h[-20:]]
                    volatility = {
                        'price_volatility': float(np.std(prices) / np.mean(prices)) if np.mean(prices) > 0 else 0,
                        'price_range_pct': float((max(prices) - min(prices)) / min(prices) * 100) if min(prices) > 0 else 0
                    }
                    simplified_data[symbol]['futures']['volatility'] = volatility

    # 获取历史分析内容
    history = get_latest_results(3)
    history_prompt = ""
    if history:
        history_prompt += "### 历史分析（最近3次）\n"
        for idx, h in enumerate(history[::-1], 1):
            history_prompt += f"【第{idx}次】\n{h}\n\n"
            
    # 当前时间（作为市场分析时间点）
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构建提示文本
    prompt = (
        history_prompt +
        f"## 短线交易资金流向专业分析任务（分析时间：{current_time}）\n\n"
        "我已收集了现货和期货市场过去200根1小时K线的资金流向数据（已剔除最新未完成的一根），同时收集了多个短周期（1分钟、5分钟、15分钟）的行情数据。包括：\n"
        "- 资金流向趋势，各交易对的资金流向趋势分析\n"
        "- 价格阶段预测，基于技术指标（如移动平均线、布林带、RSI、MACD等）预测顶部、底部、上涨中、下跌中、整理中\n"
        "- 订单簿数据，买卖盘不平衡度及热力图\n"
        "- 支撑压力位分析，根据订单簿热力图识别的关键支撑压力位\n"
        "- 大单交易分析，统计过去1000笔交易中大单（超过2个标准差）的情况\n"
        "- 资金压力分析，买卖压力强度（量化买卖单金额对比）\n"
        "- 异常交易检测，交易量或价格异常波动（如突增超2倍标准差）\n"
        "- 波动率分析，包括整体价格波动率和ATR指标\n"
        "- 多空比例及趋势，期货市场多空持仓比例及其变化趋势\n"
        "- 未平仓合约变化趋势，期货市场未平仓合约数量变化\n"
        "- 资金费率极值分析，期货市场资金费率偏离程度\n\n"
        
        "请从专业交易员和机构投资者角度进行深度分析：\n\n"
        "**主力资金行为**：\n"
        "   - 通过资金流向趋势变化，识别主力资金的建仓、出货行为\n"
        "   - 结合订单簿数据和大单交易分析，识别主力资金的意图（吸筹、出货、洗盘等）\n"
        "   - 特别关注资金流向与价格变化不匹配的异常情况，以及大单交易时点的价格反应\n"
        "   - 分析短时间内支撑/阻力位变化，判断主力资金是否在试图突破或防守关键价位\n\n"
        
        "**短线交易机会**：\n"
        "   - 根据布林带位置、RSI超买超卖、MACD金叉死叉等指标识别短线交易机会\n"
        "   - 分析支撑压力位强度及价格接近程度，寻找高胜率突破或反弹机会\n"
        "   - 结合多空比例和资金费率极值状况，发掘可能的短期反转机会\n"
        "   - 关注ATR指标变化，识别波动率收缩后可能的爆发行情\n"
        "   - 分析未平仓合约急剧变化的交易对，可能暗示大资金正在布局\n\n"
        
        "**价格阶段判断**：\n"
        "   - 根据技术指标组合分析当前价格所处阶段（布林带位置、多周期均线关系等）\n"
        "   - 判断支撑压力位的有效性和强度，预测可能的突破或反弹\n"
        "   - 结合资金流向和大单分析，确认价格突破的有效性和持续性\n"
        "   - 多周期（1分钟-1小时）技术指标协同确认，提高判断准确性\n\n"
        
        "**短期趋势预判**：\n"
        "   - 基于多项技术指标和资金数据，预判未来4-8小时可能的价格走势\n"
        "   - 识别可能的反转信号或趋势延续信号，关注指标背离现象\n"
        "   - 关注交易量变化与价格变化的配合度，判断趋势强度\n"
        "   - 预判关键支撑压力位可能出现的价格反应\n\n"
        
        "**交易策略建议**：\n"
        "   - 针对每个交易对，给出具体的交易建议（做多、做空或观望）\n"
        "   - 提供入场点位/止损位/止盈位/回报比，以及持仓周期建议（短线4小时内/中线8-24小时/长线1-3天）\n"
        "   - 为每个策略提供信心指数（1-10分，10分最高）\n"
        "   - 评估风险：根据ATR和波动率估算止损范围，避免止损过近或过远\n"
        "   - 关注技术指标共振：当多个技术指标（RSI、MACD、布林带等）同时发出相同信号时，提高策略信心指数\n\n"
        
        "请严格按照如下唯一标准格式输出策略表格，表格前后加上唯一锚点：\n"
        "\n[STRATEGY_TABLE_BEGIN]\n"
        "| 交易对 | 类型 | 方向 | 入场 | 止损 | 止盈 | 建议仓位 | 持仓周期 | 信心指数 |\n"
        "|--------|------|------|------|------|------|----------|----------|----------|\n"
        "| BTCUSDT | 合约 | 做多 | 65000 | 64000 | 67000 | 50% | 中线 | 8 |\n"
        "| ETHUSDT | 合约 | 观望 | - | - | - | 0% | - | 6 |\n" 
        "| SUIUSDT | 现货 | 做空 | 1.25 | 1.30 | 1.10 | 30% | 短线 | 7 |\n"
        "\n[STRATEGY_TABLE_END]\n"
        
        "\n注意事项：\n"
        "1. 表格必须完整，包含所有分析的交易对\n"
        "2. 对于'观望'方向，入场/止损/止盈填'-'，仓位填'0%'，持仓周期填'-'\n"
        "3. 信心指数为1-10的整数，表示对该策略的确信程度\n"
        "4. 持仓周期必须是'短线'、'中线'或'长线'之一\n"
        "5. 表格外不要输出任何策略建议内容\n\n"
        
        "以下是市场状态参考信息：\n"
        "1. 交易时间：" + current_time + "\n"
        "2. 市场整体波动状态：" + ("极度波动" if any(symbol_data.get('futures', {}).get('technical_indicators', {}).get('volatility_20', 0) > 0.05 for symbol_data in simplified_data.values()) else "正常波动") + "\n"
        "3. 市场整体趋势：基于大盘币种趋势判断\n\n"
        
        "此外，如果市场处于极端波动状态（如波动率超过平均值2倍），请更保守地评估并在表格前注明\n\n"
        
        "请使用专业术语，保持分析简洁但深入。所有分析都应基于提供的数据以及技术指标，不需要引入外部市场观点。数据如下：\n\n" +
        json.dumps(simplified_data, indent=2, ensure_ascii=False) +
        "\n\n回复格式要求：中文，使用markdown格式，重点突出，适当使用表格对比分析，不要使用mermaid内容。不用返回主力资金行为部分的内容。"
    )

    # 按配置顺序依次尝试各个API
    for provider in LLM_API_PROVIDERS:
        try:
            if provider == 'local':
                logger.info("使用本地分析生成结果")
                from ..utils.fallback import generate_fallback_analysis
                return generate_fallback_analysis(simplified_data)
            else:
                return call_llm_api(provider, prompt)
        except Exception as e:
            logger.error(f"{provider} API调用失败: {e}")
            if provider == LLM_API_PROVIDERS[-1]:
                # 如果是最后一个提供商也失败，则生成本地分析
                logger.warning("所有配置的API都调用失败，使用本地分析")
                from ..utils.fallback import generate_fallback_analysis
                return generate_fallback_analysis(simplified_data)
            else:
                # 否则继续尝试下一个提供商
                logger.info(f"尝试下一个API提供商: {LLM_API_PROVIDERS[LLM_API_PROVIDERS.index(provider) + 1]}")
                continue 