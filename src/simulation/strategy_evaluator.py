"""策略评估器模块

用于评估策略的胜率、盈亏比等指标，提供持续回测功能。
"""
import os
import time
import json
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

from ..data.binance_client import get_klines_data, get_orderbook_stats
from ..api.llm_client import generate_analysis
from .trade_simulator import parse_ai_strategy, save_sim_orders, stat_sim_results

# 配置日志
logger = logging.getLogger(__name__)

# 评估结果保存目录
EVAL_RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'eval_results')
EVAL_RESULTS_DIR = os.path.abspath(EVAL_RESULTS_DIR)

if not os.path.exists(EVAL_RESULTS_DIR):
    os.makedirs(EVAL_RESULTS_DIR)


def generate_and_evaluate_strategy(symbols: List[str], days: int = 3, interval_hours: int = 3, fast_mode: bool = True) -> Dict:
    """
    连续多天生成并评估策略:
    1. 按设定的时间间隔获取市场数据
    2. 生成策略并模拟下单
    3. 在下一个时间点检查订单状态
    4. 统计整体表现

    Args:
        symbols: 交易对列表，如 ["BTCUSDT", "ETHUSDT"]
        days: 回测天数
        interval_hours: 策略生成间隔（小时）
        fast_mode: 是否使用快速模式（随机模拟成交，不依赖实际K线）

    Returns:
        策略评估结果统计
    """
    logger.info(f"开始{days}天策略评估，间隔{interval_hours}小时，交易对: {symbols}，{'快速模式' if fast_mode else '标准模式'}")
    
    # 计算评估的总轮次
    total_rounds = (days * 24) // interval_hours
    logger.info(f"总共将进行{total_rounds}轮策略生成与评估")
    
    # 创建评估会话ID
    session_id = f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    session_dir = os.path.join(EVAL_RESULTS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    # 初始化结果统计
    results = {
        "session_id": session_id,
        "start_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "end_time": None,
        "symbols": symbols,
        "days": days,
        "interval_hours": interval_hours,
        "total_rounds": total_rounds,
        "completed_rounds": 0,
        "strategies_generated": 0,
        "orders_created": 0,
        "orders_closed": 0,
        "win_orders": 0,
        "loss_orders": 0,
        "win_rate": 0,
        "total_profit": 0,
        "total_loss": 0,
        "net_pnl": 0,
        "rounds_data": []
    }
    
    try:
        # 循环每一轮
        for round_num in range(1, total_rounds + 1):
            logger.info(f"开始第{round_num}/{total_rounds}轮评估")
            
            # 当前轮次数据
            round_data = {
                "round": round_num,
                "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "orders": []
            }
            
            # 1. 获取市场数据
            market_data = {}
            for symbol in symbols:
                try:
                    # 获取现货数据
                    spot_data = get_market_data(symbol, is_futures=False)
                    
                    # 获取期货数据
                    futures_data = get_market_data(symbol, is_futures=True)
                    
                    # 组装数据
                    market_data[symbol] = {
                        'spot': spot_data,
                        'futures': futures_data,
                        'spot_short_term': spot_data,
                        'futures_short_term': futures_data
                    }
                except Exception as e:
                    logger.error(f"获取{symbol}市场数据失败: {e}")
            
            # 2. 生成策略分析
            try:
                analysis_result = generate_analysis(market_data)
                results["strategies_generated"] += 1
                
                # 保存分析结果
                analysis_file = os.path.join(session_dir, f"round_{round_num}_analysis.md")
                with open(analysis_file, 'w', encoding='utf-8') as f:
                    f.write(analysis_result)
                
                # 解析策略并生成订单
                orders = parse_ai_strategy(analysis_result)
                results["orders_created"] += len(orders)
                
                # 添加当前轮次标识
                for order in orders:
                    order["eval_round"] = round_num
                    order["eval_session"] = session_id
                    round_data["orders"].append(order)
                
                # 保存模拟订单
                if orders:
                    orders_file = os.path.join(session_dir, f"round_{round_num}_orders.json")
                    with open(orders_file, 'w', encoding='utf-8') as f:
                        json.dump(orders, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"生成策略分析失败: {e}")
                continue
            
            # 3. 等待并监控订单执行（模拟时间推进）
            time.sleep(2)  # 短暂暂停，减少API调用频率
            
            # 模拟时间推进，获取最新价格检查订单
            closed_orders = backtest_orders_simulation(orders, fast_mode=fast_mode)
            results["orders_closed"] += len(closed_orders)
            
            # 4. 统计当前轮次表现
            win_count = sum(1 for o in closed_orders if o.get('pnl', 0) > 0)
            loss_count = len(closed_orders) - win_count
            
            profit = sum(o.get('pnl', 0) for o in closed_orders if o.get('pnl', 0) > 0)
            loss = sum(o.get('pnl', 0) for o in closed_orders if o.get('pnl', 0) <= 0)
            
            results["win_orders"] += win_count
            results["loss_orders"] += loss_count
            results["total_profit"] += profit
            results["total_loss"] += loss
            
            # 更新轮次数据
            round_data["closed_orders"] = len(closed_orders)
            round_data["win_orders"] = win_count
            round_data["win_rate"] = win_count / len(closed_orders) if closed_orders else 0
            round_data["profit"] = profit
            round_data["loss"] = loss
            round_data["net_pnl"] = profit + loss
            
            results["rounds_data"].append(round_data)
            results["completed_rounds"] += 1
            
            # 计算总体胜率和净盈亏
            total_closed = results["win_orders"] + results["loss_orders"]
            results["win_rate"] = results["win_orders"] / total_closed if total_closed > 0 else 0
            results["net_pnl"] = results["total_profit"] + results["total_loss"]
            
            # 保存当前完整结果
            save_evaluation_results(results, session_dir)
            
            # 打印当前进度和表现
            logger.info(f"第{round_num}轮完成，胜率: {round_data['win_rate']*100:.2f}%，净盈亏: {round_data['net_pnl']:.2f} USDT")
            logger.info(f"总体进度: {results['completed_rounds']}/{total_rounds}，总胜率: {results['win_rate']*100:.2f}%，总净盈亏: {results['net_pnl']:.2f} USDT")
            
            # 如果未到最后一轮，等待到下一个间隔时间
            if round_num < total_rounds:
                wait_seconds = 10  # 在快速模式下等待10秒
                logger.info(f"等待{wait_seconds}秒后开始下一轮评估...")
                time.sleep(wait_seconds)
    
    except KeyboardInterrupt:
        logger.warning("评估被用户中断")
    except Exception as e:
        logger.error(f"评估过程中发生错误: {e}", exc_info=True)
    finally:
        # 完成所有评估，更新最终结果
        results["end_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_evaluation_results(results, session_dir)
        
        # 生成评估报告
        report = generate_evaluation_report(results)
        report_file = os.path.join(session_dir, "evaluation_report.md")
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        
        logger.info(f"策略评估完成，报告已保存至: {report_file}")
        
        return results


def get_market_data(symbol: str, is_futures: bool = False) -> Dict:
    """
    获取市场数据，简化版本用于快速回测
    """
    data = {'symbol': symbol, 'market_type': 'futures' if is_futures else 'spot'}
    
    # 获取K线数据
    data['klines'] = {}
    for interval in ['1m', '5m', '15m', '1h']:
        klines = get_klines_data(symbol, interval=interval, limit=200, is_futures=is_futures)
        data['klines'][interval] = klines
    
    # 获取订单簿数据
    orderbook = get_orderbook_stats(symbol, is_futures=is_futures)
    data['orderbook'] = orderbook
    
    # 计算基本趋势
    if data['klines'].get('1h'):
        klines = data['klines']['1h']
        if len(klines) > 10:
            # 简单趋势计算：最近10根K线收盘价变化
            closes = [k['close'] for k in klines[-10:]]
            if 'net_inflow' in klines[0]:
                net_inflow = sum([k.get('net_inflow', 0) for k in klines[-10:]])
            else:
                net_inflow = 0
                
            if closes[-1] > closes[0]:
                data['trend'] = {
                    'direction': 'up',
                    'strength': (closes[-1] - closes[0]) / closes[0] * 100,
                    'net_inflow': net_inflow
                }
            else:
                data['trend'] = {
                    'direction': 'down',
                    'strength': (closes[0] - closes[-1]) / closes[0] * 100,
                    'net_inflow': net_inflow
                }
    
    # 计算买卖压力
    if orderbook:
        data['pressure'] = {
            'buy': orderbook.get('bids_value', 0),
            'sell': orderbook.get('asks_value', 0),
            'ratio': orderbook.get('volume_imbalance', 0)
        }
    
    # 加入当前市场价格
    if data['klines'].get('1m') and data['klines']['1m']:
        data['current_market_price'] = data['klines']['1m'][-1]['close']
    
    return data


def backtest_orders_simulation(orders: List[Dict], fast_mode: bool = False) -> List[Dict]:
    """
    模拟回测订单执行，加速模式下直接使用随机价格模拟成交
    
    Args:
        orders: 订单列表
        fast_mode: 是否使用快速模式（使用随机价格模拟）
    
    Returns:
        已平仓订单列表
    """
    closed_orders = []
    
    for order in orders:
        if order['direction'] == '观望':
            continue
            
        symbol = order['symbol']
        direction = order['direction']
        entry = order['entry']
        stop = order['stop']
        take = order['take']
        
        # 快速模式: 使用随机成交价格，更快速地模拟订单执行
        if fast_mode:
            # 模拟80%触及止盈/止损，20%触及相反方向
            import random
            is_profit = random.random() < 0.5  # 50%概率盈利/亏损
            
            if direction == '做多':
                if is_profit:
                    close_price = take
                    close_reason = '止盈'
                else:
                    close_price = stop
                    close_reason = '止损'
            else:  # 做空
                if is_profit:
                    close_price = take
                    close_reason = '止盈'
                else:
                    close_price = stop
                    close_reason = '止损'
                    
            # 计算盈亏和手续费
            usdt_amt = order['usdt_amt']
            qty = usdt_amt / entry
            
            if direction == '做多':
                pnl = (close_price - entry) * qty
            else:  # 做空
                pnl = (entry - close_price) * qty
                
            # 计算手续费
            if order['type'] == '合约':
                fee = usdt_amt * 0.0005 * 2  # 开平各一次
            else:  # 现货
                fee = usdt_amt * 0.00075 * 2
                
            # 更新订单信息
            order['status'] = 'closed'
            order['close_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            order['close_price'] = close_price
            order['pnl'] = round(pnl - fee, 4)
            order['fee'] = round(fee, 4)
            order['close_reason'] = close_reason
            
            closed_orders.append(order)
        else:
            # 非快速模式: 获取实际K线数据判断成交情况 (略，使用trade_simulator中的逻辑)
            pass
    
    return closed_orders


def save_evaluation_results(results: Dict, session_dir: str):
    """保存评估结果"""
    results_file = os.path.join(session_dir, "evaluation_results.json")
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def generate_evaluation_report(results: Dict) -> str:
    """生成评估报告"""
    if not results:
        return "# 策略评估报告\n\n无有效评估数据。"
    
    # 计算总体统计
    total_orders = results["orders_created"]
    closed_orders = results["orders_closed"]
    win_rate = results["win_rate"] * 100 if "win_rate" in results else 0
    
    # 创建报告
    report = [
        "# 策略评估报告\n",
        f"## 评估概要\n",
        f"- **评估会话**: {results.get('session_id', 'N/A')}",
        f"- **开始时间**: {results.get('start_time', 'N/A')}",
        f"- **结束时间**: {results.get('end_time', 'N/A')}",
        f"- **交易品种**: {', '.join(results.get('symbols', []))}",
        f"- **评估天数**: {results.get('days', 0)}",
        f"- **间隔时间**: {results.get('interval_hours', 0)}小时",
        f"- **总轮次**: {results.get('total_rounds', 0)}",
        f"- **完成轮次**: {results.get('completed_rounds', 0)}",
        f"\n## 策略表现\n",
        f"- **生成策略数**: {results.get('strategies_generated', 0)}",
        f"- **总订单数**: {total_orders}",
        f"- **已平仓订单**: {closed_orders}",
        f"- **盈利订单**: {results.get('win_orders', 0)}",
        f"- **亏损订单**: {results.get('loss_orders', 0)}",
        f"- **胜率**: {win_rate:.2f}%",
        f"- **总盈利**: {results.get('total_profit', 0):.2f} USDT",
        f"- **总亏损**: {results.get('total_loss', 0):.2f} USDT",
        f"- **净盈亏**: {results.get('net_pnl', 0):.2f} USDT",
        f"\n## 轮次详情\n",
        f"| 轮次 | 时间 | 订单数 | 已平仓 | 盈利数 | 胜率 | 净盈亏 |",
        f"|------|------|--------|--------|--------|------|--------|"
    ]
    
    # 添加每轮评估详情
    for round_data in results.get("rounds_data", []):
        round_num = round_data.get("round", "N/A")
        time = round_data.get("time", "N/A")
        orders_count = len(round_data.get("orders", []))
        closed_count = round_data.get("closed_orders", 0)
        win_count = round_data.get("win_orders", 0)
        win_rate = round_data.get("win_rate", 0) * 100
        net_pnl = round_data.get("net_pnl", 0)
        
        report.append(f"| {round_num} | {time} | {orders_count} | {closed_count} | {win_count} | {win_rate:.2f}% | {net_pnl:.2f} |")
    
    # 添加结论和建议
    report.extend([
        f"\n## 结论与建议\n",
        f"- **整体表现**: {'良好' if win_rate > 55 else '一般' if win_rate > 45 else '较差'}",
        f"- **策略胜率**: {'优秀' if win_rate > 60 else '良好' if win_rate > 50 else '尚可' if win_rate > 40 else '需改进'}",
        f"- **盈亏比**: {'优秀' if results.get('net_pnl', 0) > 0 and abs(results.get('total_profit', 0)) > abs(results.get('total_loss', 0)) * 1.5 else '良好' if results.get('net_pnl', 0) > 0 else '需改进'}",
        f"\n### 优化建议\n"
    ])
    
    # 添加针对性的优化建议
    if win_rate < 45:
        report.append("- 提高策略胜率：调整更严格的入场条件，确保有更明确的趋势信号")
    if results.get('net_pnl', 0) < 0:
        report.append("- 改善风险管理：缩小止损范围，放大止盈空间，提高盈亏比")
    if results.get('completed_rounds', 0) < results.get('total_rounds', 0) * 0.8:
        report.append("- 提高系统稳定性：部分评估轮次未完成，检查市场数据获取和策略生成模块")
    
    return "\n".join(report)


def get_latest_evaluation_results(limit: int = 5) -> List[Dict]:
    """获取最近几次的评估结果"""
    results = []
    
    if not os.path.exists(EVAL_RESULTS_DIR):
        return results
    
    # 获取所有评估会话目录
    session_dirs = [d for d in os.listdir(EVAL_RESULTS_DIR) 
                   if os.path.isdir(os.path.join(EVAL_RESULTS_DIR, d)) and d.startswith('eval_')]
    
    # 按创建时间排序
    session_dirs.sort(reverse=True)
    
    # 提取最近几次评估结果
    for session_dir in session_dirs[:limit]:
        results_file = os.path.join(EVAL_RESULTS_DIR, session_dir, "evaluation_results.json")
        if os.path.exists(results_file):
            try:
                with open(results_file, 'r', encoding='utf-8') as f:
                    results.append(json.load(f))
            except Exception as e:
                logger.error(f"读取评估结果文件失败: {e}")
    
    return results 