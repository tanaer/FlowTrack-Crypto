"""LLM API客户端模块

负责调用外部LLM API进行分析生成。
"""
import json
import logging
import requests
from typing import Dict, Any, List
from ..config import config, LLM_API_PROVIDERS
from ..analysis.result_manager import get_latest_results
import numpy as np
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)

# 创建一个增强型JSON编码器，处理各种numpy类型和数值无法序列化的情况
class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif hasattr(obj, 'to_json'):
            return obj.to_json()
        elif hasattr(obj, 'tolist'):
            return obj.tolist()
        # 捕获所有无法序列化的对象，转为字符串
        try:
            return super(EnhancedJSONEncoder, self).default(obj)
        except TypeError:
            return str(obj)


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
    
    response = requests.post(api_url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()  # 如果请求失败，抛出异常
    
    result = response.json()
    
    logger.info(f"{provider} API调用成功")
    return result['choices'][0]['message']['content']


def generate_analysis(data: Dict) -> str:
    """按照配置的顺序尝试使用各个LLM API进行分析，自动拼接最近3次历史分析"""
    try:
        # 简化数据，减小请求大小
        simplified_data = {}
        for symbol, symbol_data in data.items():
            # 初始化安全获取各市场类型的数据
            spot_data = symbol_data.get('spot', {})
            futures_data = symbol_data.get('futures', {})
            
            simplified_data[symbol] = {
                'spot': {
                    'trend': spot_data.get('trend', {'direction': 'neutral', 'strength': 0, 'net_inflow': 0}),
                    'pressure': spot_data.get('pressure', {'buy': 0, 'sell': 0, 'ratio': 0}),
                    'anomalies': spot_data.get('anomalies', [])[:5] if spot_data.get('anomalies') else []
                },
                'futures': {
                    'trend': futures_data.get('trend', {'direction': 'neutral', 'strength': 0, 'net_inflow': 0}),
                    'pressure': futures_data.get('pressure', {'buy': 0, 'sell': 0, 'ratio': 0}),
                    'anomalies': futures_data.get('anomalies', [])[:5] if futures_data.get('anomalies') else []
                }
            }
            
            # 添加订单簿摘要信息
            orderbook = spot_data.get('orderbook', {})
            if orderbook:
                simplified_data[symbol]['spot']['orderbook_summary'] = {
                    'volume_imbalance': orderbook.get('volume_imbalance', 0),
                    'value_imbalance': orderbook.get('value_imbalance', 0),
                    'price': orderbook.get('price', 0)
                }
                
            orderbook = futures_data.get('orderbook', {})
            if orderbook:
                simplified_data[symbol]['futures']['orderbook_summary'] = {
                    'volume_imbalance': orderbook.get('volume_imbalance', 0),
                    'value_imbalance': orderbook.get('value_imbalance', 0),
                    'price': orderbook.get('price', 0)
                }
                
            # 添加短线交易新增数据
            spot_short_term = symbol_data.get('spot_short_term', {})
            if spot_short_term:
                # 添加技术指标数据
                if 'technical_indicators' in spot_short_term:
                    simplified_data[symbol]['spot']['technical_indicators'] = spot_short_term['technical_indicators']
                
                # 添加多周期技术指标
                if 'technical_indicators_by_period' in spot_short_term:
                    simplified_data[symbol]['spot']['technical_indicators_by_period'] = spot_short_term['technical_indicators_by_period']
                
                # 添加支撑阻力数据
                if spot_short_term.get('orderbook', {}) and 'support_resistance' in spot_short_term.get('orderbook', {}):
                    simplified_data[symbol]['spot']['support_resistance'] = spot_short_term['orderbook']['support_resistance']
                
                # 添加大单交易数据
                if 'large_order_analysis' in spot_short_term:
                    simplified_data[symbol]['spot']['large_orders'] = spot_short_term['large_order_analysis']
                
                # 添加波动率指标
                if spot_short_term.get('klines', {}) and '1h' in spot_short_term.get('klines', {}):
                    # 计算价格波动指标
                    klines_1h = spot_short_term['klines']['1h']
                    if len(klines_1h) > 20:
                        prices = [k['close'] for k in klines_1h[-20:]]
                        volatility = {
                            'price_volatility': float(np.std(prices) / np.mean(prices)) if np.mean(prices) > 0 else 0,
                            'price_range_pct': float((max(prices) - min(prices)) / min(prices) * 100) if min(prices) > 0 else 0
                        }
                        simplified_data[symbol]['spot']['volatility'] = volatility
            
            futures_short_term = symbol_data.get('futures_short_term', {})
            if futures_short_term:
                # 添加技术指标数据
                if 'technical_indicators' in futures_short_term:
                    simplified_data[symbol]['futures']['technical_indicators'] = futures_short_term['technical_indicators']
                
                # 添加多周期技术指标
                if 'technical_indicators_by_period' in futures_short_term:
                    simplified_data[symbol]['futures']['technical_indicators_by_period'] = futures_short_term['technical_indicators_by_period']
                
                # 添加支撑阻力数据
                if futures_short_term.get('orderbook', {}) and 'support_resistance' in futures_short_term.get('orderbook', {}):
                    simplified_data[symbol]['futures']['support_resistance'] = futures_short_term['orderbook']['support_resistance']
                
                # 添加大单交易数据
                if 'large_order_analysis' in futures_short_term:
                    simplified_data[symbol]['futures']['large_orders'] = futures_short_term['large_order_analysis']
                
                # 添加资金费率数据
                if 'funding_rate' in futures_short_term and futures_short_term.get('funding_rate', {}).get('stats'):
                    simplified_data[symbol]['futures']['funding_rate'] = futures_short_term['funding_rate']['stats']
                
                # 添加多空比例数据
                if 'long_short_ratio' in futures_short_term and futures_short_term.get('long_short_ratio', {}).get('stats'):
                    simplified_data[symbol]['futures']['long_short_ratio'] = futures_short_term['long_short_ratio']['stats']
                
                # 添加未平仓合约数据
                if 'open_interest' in futures_short_term and futures_short_term.get('open_interest', {}).get('stats'):
                    simplified_data[symbol]['futures']['open_interest'] = futures_short_term['open_interest']['stats']
                
                # 添加大户持仓比例数据
                if 'top_traders_ratio' in futures_short_term and futures_short_term.get('top_traders_ratio', {}).get('stats'):
                    simplified_data[symbol]['futures']['top_traders_ratio'] = futures_short_term['top_traders_ratio']['stats']
                    
                # 添加波动率指标
                if futures_short_term.get('klines', {}) and '1h' in futures_short_term.get('klines', {}):
                    # 计算价格波动指标
                    klines_1h = futures_short_term['klines']['1h']
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

        # 构建强化版提示文本
        prompt = (
            history_prompt +
            f"## 加密货币短线交易专业分析任务（分析时间：{current_time}）\n\n"
            "我已收集了现货和期货市场的全面数据集，包括：\n"
            "- **多周期K线**：200根1分钟/5分钟/15分钟/1小时K线数据（已剔除最新未完成的一根）\n"
            "- **多周期技术指标**：每个周期的RSI、MACD、布林带、ATR等指标\n"
            "- **订单簿热力图**：深度分析的热力图和自动识别的支撑/阻力位\n"
            "- **大单交易分析**：统计过去1000笔交易中的大单（超过2个标准差）情况\n"
            "- **资金流向趋势**：各交易对的资金流向分析\n"
            "- **资金压力分析**：买卖压力强度（量化买卖单金额对比）\n"
            "- **波动率分析**：包括整体价格波动率和ATR指标\n"
            "- **多空比例**：期货市场多空持仓比例及其变化趋势\n"
            "- **大户持仓比例**：头部交易者的多空持仓比例\n"
            "- **未平仓合约变化**：期货市场未平仓合约数量趋势\n"
            "- **资金费率分析**：期货市场资金费率状态\n\n"
            
            "请从专业交易员和机构投资者角度进行深度分析，重点关注短线交易机会：\n\n"
            
            "### 一、市场整体状态解读\n"
            "   - 总结当前市场整体多空力量对比\n"
            "   - 判断大盘主流交易对处于哪个市场周期阶段\n"
            "   - 分析市场整体资金流向动向和大户行为\n\n"
            
            "### 二、技术指标解读指南\n"
            "**RSI（相对强弱指数）**：\n"
            "   - RSI > 70: 可能超买，考虑减仓或逢高做空\n"
            "   - RSI < 30: 可能超卖，考虑逢低做多\n"
            "   - RSI底背离：价格创新低但RSI未创新低，潜在反转信号\n"
            "   - RSI顶背离：价格创新高但RSI未创新高，潜在见顶信号\n\n"
            
            "**MACD（移动平均线收敛/发散）**：\n"
            "   - MACD金叉：MACD线上穿信号线，多头信号\n"
            "   - MACD死叉：MACD线下穿信号线，空头信号\n"
            "   - 柱状图扩大：趋势增强\n"
            "   - 柱状图收缩：趋势减弱\n"
            "   - 零线上方的金叉：强烈多头信号\n"
            "   - 零线下方的死叉：强烈空头信号\n\n"
            
            "**布林带**：\n"
            "   - 价格触及上轨：可能超买，考虑逢高做空\n"
            "   - 价格触及下轨：可能超卖，考虑逢低做多\n"
            "   - 带宽收窄：波动率降低，可能即将大幅波动\n"
            "   - 带宽扩大：波动率增加，趋势可能形成\n"
            "   - 价格长期在带的上/下半区运行：上升/下降趋势确认\n\n"
            
            "**ATR（平均真实波幅）**：\n"
            "   - ATR增大：波动率上升，可能有大行情\n"
            "   - ATR减小：波动率下降，可能进入盘整\n"
            "   - 使用ATR设置止损：通常为1-3倍ATR距离\n\n"
            
            "**移动平均线系统**：\n"
            "   - 价格站上所有均线：强势上涨\n"
            "   - 价格跌破所有均线：强势下跌\n"
            "   - 短期均线上穿长期均线：多头信号\n"
            "   - 短期均线下穿长期均线：空头信号\n"
            "   - 均线系统由空头排列转为多头排列：趋势可能反转\n\n"
            
            "### 三、短线交易决策框架\n"
            "1. **多周期分析**：\n"
            "   - 寻找不同周期技术指标共振的交易对\n"
            "   - 1分钟/5分钟/15分钟指标提供入场时机\n"
            "   - 1小时图提供大趋势方向\n\n"
            
            "2. **支撑/阻力与热力图分析**：\n"
            "   - 识别强支撑位的反弹机会\n"
            "   - 识别强阻力位的做空机会\n"
            "   - 分析热力图数据揭示隐藏的机构下单情况\n\n"
            
            "3. **多空情绪与资金流向分析**：\n"
            "   - 极端多空比例可能预示反转\n"
            "   - 资金费率极值状态下的反向交易\n"
            "   - 大户持仓变化预示中期趋势\n\n"
            
            "4. **波动率与ATR周期分析**：\n"
            "   - 波动率收缩期准备入场，等待突破\n"
            "   - 波动率放大期适度控制仓位\n"
            "   - 根据ATR设置合理的止损范围\n\n"
            
            "5. **大单交易监控**：\n"
            "   - 连续大单买入/卖出分析\n"
            "   - 大单后价格反应评估主力意图\n"
            "   - 大单与技术指标的配合分析\n\n"
            
            "### 四、交易策略建议\n"
            "为每个交易对提供：\n"
            "   - 方向（做多/做空/观望）与市场类型（现货/合约）\n"
            "   - 精确的入场/止损/止盈价位\n"
            "   - 建议仓位大小（基于风险评估）\n"
            "   - 预期持仓周期（短线4小时内/中线8-24小时/长线1-3天）\n"
            "   - 信心指数（1-10分，10分最高）\n"
            "   - 关键监控指标和平仓条件\n\n"
            
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
            "2. 市场整体波动状态：" + ("极度波动" if any(bool(symbol_data.get('futures', {}).get('technical_indicators', {}).get('volatility_20', 0) > 0.05) for symbol_data in simplified_data.values()) else "正常波动") + "\n"
            "3. 市场整体趋势：基于大盘币种趋势判断\n\n"
            
            "如果市场处于极端波动状态（如波动率超过平均值2倍），请更保守地评估并在表格前注明。\n"
            "同时，如果检测到技术指标背离或极端值，请特别标注这些情况，它们通常预示着可能的反转机会。\n\n"
            
            "请使用专业术语，保持分析简洁但深入。所有分析都应基于提供的数据以及技术指标，不需要引入外部市场观点。数据如下：\n\n" +
            json.dumps(simplified_data, indent=2, ensure_ascii=False, cls=EnhancedJSONEncoder) +
            "\n\n回复格式要求：中文，使用markdown格式，重点突出，适当使用表格对比分析，不要使用mermaid内容。"
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
    except Exception as e:
        logger.error(f"生成分析过程中发生错误: {e}")
        # 在发生异常时返回基本的错误报告
        return f"# 系统生成分析时发生错误\n\n抱歉，在生成分析报告时遇到了问题: {str(e)}\n\n请查看日志获取详细信息。" 