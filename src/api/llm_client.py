"""LLM API客户端模块

负责调用外部LLM API进行分析生成。
"""
import json
import logging
import requests
from typing import Dict
from ..config import config, LLM_API_PROVIDERS
from ..analysis.result_manager import get_latest_results

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

    # 获取历史分析内容
    history = get_latest_results(3)
    history_prompt = ""
    if history:
        history_prompt += "### 历史分析（最近3次）\n"
        for idx, h in enumerate(history[::-1], 1):
            history_prompt += f"【第{idx}次】\n{h}\n\n"

    # 构建提示文本
    prompt = (
        history_prompt +
        "## 资金流向专业分析任务\n\n"
        "我已收集了现货和期货市场过去50根5分钟K线的资金流向数据（已剔除最新未完成的一根），包括：\n"
        "- 资金流向趋势，各交易对的资金流向趋势分析\n"
        "- 价格阶段预测，基于技术指标（如移动平均线、价格波动范围）预测顶部、底部、上涨中、下跌中、整理中\n"
        "- 订单簿数据，买卖盘不平衡度\n"
        "- 资金压力分析，买卖压力强度（量化买卖单金额对比）\n"
        "- 异常交易检测，交易量或价格异常波动（如突增超2倍标准差）\n\n"
        "请从专业交易员和机构投资者角度进行深度分析：\n\n"
        "**主力资金行为**：\n"
        "   - 通过资金流向趋势变化，识别主力资金的建仓、出货行为\n"
        "   - 结合订单簿数据，分析主力资金的意图（吸筹、出货、洗盘等）\n"
        "   - 特别关注资金流向与价格变化不匹配的异常情况（如资金流入但价格下跌），分析潜在原因（如洗盘或假突破）\n\n"
        "**价格阶段判断**：\n"
        "   - 根据资金流向趋势和价格关系，判断各交易对处于什么阶段（顶部、底部、上涨中、下跌中、整理中等）\n"
        "   - 提供判断的置信度（基于资金流向一致性和技术指标）及量化依据（如价格突破前高）\n"
        "   - 对比不同交易对的阶段差异，分析可能的轮动关系\n\n"
        "**短期趋势预判**：\n"
        "   - 基于资金流向和资金压力分析，预判未来4-8小时可能的价格走势\n"
        "   - 识别可能的反转信号或趋势延续信号\n"
        "   - 关注异常交易数据可能暗示的短期行情变化，分析潜在催化剂（如机构买入或卖出，如果你可以联网的情况下还可以参考市场新闻）\n\n"
        "**交易策略建议**：\n"
        "   - 针对每个交易对，给出具体的交易建议（观望、做多、做空、减仓）\n"
        "   - 提供入场点位/止损位/止盈位/回报比（严谨的分析，不要使用 可能 等字眼）\n"
        "   - 必须评估风险\n\n"
        "请严格按照如下唯一标准格式输出策略表格，表格前后加上唯一锚点：\n"
        "\n[STRATEGY_TABLE_BEGIN]\n"
        "| 交易对 | 类型 | 方向 | 入场 | 止损 | 止盈 | 建议仓位 |\n"
        "|--------|------|------|------|------|------|----------|\n"
        "| BTCUSDT | 合约 | 做多 | 65000 | 64000 | 67000 | 50% |\n"
        "| SUIUSDT | 现货 | 做空 | 1.25 | 1.30 | 1.10 | 30% |\n"
        "...（每个有建议的币对都要输出，观望也要写明）\n"
        "\n[STRATEGY_TABLE_END]\n"
        "\n表格必须完整、无缺漏、无多余内容，表头和顺序必须与示例完全一致。\n"
        "表格外不要输出任何策略建议内容。\n"
        "\n请使用专业术语，保持分析简洁但深入。数据如下：\n\n" +
        json.dumps(simplified_data, indent=2, ensure_ascii=False) +
        "\n\n回复格式要求：中文，使用markdown格式，重点突出，适当使用表格对比分析，不要使用mermaid内容。不用返回 主力资金行为部分的内容。"
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