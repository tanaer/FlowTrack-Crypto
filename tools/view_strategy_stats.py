#!/usr/bin/env python
"""策略评估统计分析工具

查看策略评估结果，并提供数据分析功能。
"""
import os
import sys
import json
import logging
import argparse
import pandas as pd
from datetime import datetime, timedelta
from tabulate import tabulate
import matplotlib.pyplot as plt

# 添加项目根目录到Python路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.simulation.strategy_evaluator import get_latest_evaluation_results, EVAL_RESULTS_DIR
from src.simulation.trade_simulator import stat_sim_results

def setup_logging(debug=False):
    """配置日志级别和格式"""
    log_level = logging.DEBUG if debug else logging.INFO
    
    # 配置日志
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    
    return logging.getLogger(__name__)

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="策略评估统计分析工具")
    parser.add_argument('-d', '--debug', action='store_true', help="启用调试日志")
    parser.add_argument('-s', '--session', type=str, help="查看指定会话ID的评估结果")
    parser.add_argument('-a', '--all', action='store_true', help="显示所有评估会话")
    parser.add_argument('-c', '--compare', action='store_true', help="比较不同会话的策略表现")
    parser.add_argument('--visual', action='store_true', help="生成视觉化图表")
    parser.add_argument('--sim', action='store_true', help="查看模拟盘统计数据")
    return parser.parse_args()

def view_session_results(session_id: str, visual: bool = False):
    """查看指定会话的评估结果"""
    session_dir = os.path.join(EVAL_RESULTS_DIR, session_id)
    if not os.path.exists(session_dir):
        print(f"找不到会话 {session_id} 的评估结果")
        return
        
    # 加载评估结果
    results_file = os.path.join(session_dir, "evaluation_results.json")
    if not os.path.exists(results_file):
        print(f"会话 {session_id} 没有评估结果文件")
        return
        
    with open(results_file, 'r', encoding='utf-8') as f:
        results = json.load(f)
    
    # 显示基本信息
    print("\n" + "="*80)
    print(f"策略评估会话: {session_id}")
    print(f"开始时间: {results.get('start_time', 'N/A')}")
    print(f"结束时间: {results.get('end_time', 'N/A')}")
    print(f"交易对: {', '.join(results.get('symbols', []))}")
    print("-"*80)
    
    # 显示总体表现
    win_rate = results.get("win_rate", 0) * 100
    win_orders = results.get("win_orders", 0)
    loss_orders = results.get("loss_orders", 0)
    total_closed = win_orders + loss_orders
    total_profit = results.get("total_profit", 0)
    total_loss = results.get("total_loss", 0)
    net_pnl = total_profit + total_loss
    
    print(f"总体表现:")
    print(f"- 总订单数: {results.get('orders_created', 0)}")
    print(f"- 已平仓订单: {total_closed}")
    print(f"- 盈利订单: {win_orders}")
    print(f"- 亏损订单: {loss_orders}")
    print(f"- 胜率: {win_rate:.2f}%")
    print(f"- 总盈利: {total_profit:.2f} USDT")
    print(f"- 总亏损: {total_loss:.2f} USDT")
    print(f"- 净盈亏: {net_pnl:.2f} USDT")
    print(f"- 盈亏比: {abs(total_profit/total_loss):.2f}" if total_loss != 0 else "- 盈亏比: N/A")
    print("-"*80)
    
    # 显示轮次数据表格
    rounds_data = results.get("rounds_data", [])
    if rounds_data:
        rounds_table = []
        for rd in rounds_data:
            win_rate_pct = rd.get("win_rate", 0) * 100
            rounds_table.append([
                rd.get("round", "N/A"),
                rd.get("time", "N/A"),
                len(rd.get("orders", [])),
                rd.get("closed_orders", 0),
                rd.get("win_orders", 0),
                f"{win_rate_pct:.2f}%",
                f"{rd.get('profit', 0):.2f}",
                f"{rd.get('loss', 0):.2f}",
                f"{rd.get('net_pnl', 0):.2f}"
            ])
        
        print("轮次详情:")
        print(tabulate(rounds_table, 
                      headers=["轮次", "时间", "订单数", "已平仓", "盈利数", "胜率", "盈利", "亏损", "净盈亏"],
                      tablefmt="grid"))
    
    # 生成可视化图表
    if visual and rounds_data:
        try:
            # 创建数据帧
            df = pd.DataFrame([{
                'round': rd.get('round', 0),
                'time': rd.get('time', ''),
                'orders': len(rd.get('orders', [])),
                'closed': rd.get('closed_orders', 0),
                'win_orders': rd.get('win_orders', 0),
                'win_rate': rd.get('win_rate', 0) * 100,
                'profit': rd.get('profit', 0),
                'loss': rd.get('loss', 0),
                'net_pnl': rd.get('net_pnl', 0)
            } for rd in rounds_data])
            
            # 创建图表目录
            charts_dir = os.path.join(session_dir, "charts")
            os.makedirs(charts_dir, exist_ok=True)
            
            # 绘制胜率变化图
            plt.figure(figsize=(10, 6))
            plt.plot(df['round'], df['win_rate'], 'b-o', linewidth=2)
            plt.axhline(y=50, color='r', linestyle='--', alpha=0.7)
            plt.title('策略胜率变化趋势')
            plt.xlabel('评估轮次')
            plt.ylabel('胜率 (%)')
            plt.grid(True, alpha=0.3)
            plt.savefig(os.path.join(charts_dir, 'win_rate_trend.png'))
            
            # 绘制盈亏变化图
            plt.figure(figsize=(10, 6))
            plt.plot(df['round'], df['profit'], 'g-o', label='盈利')
            plt.plot(df['round'], df['loss'], 'r-o', label='亏损')
            plt.plot(df['round'], df['net_pnl'], 'b-o', label='净盈亏')
            plt.axhline(y=0, color='k', linestyle='-', alpha=0.3)
            plt.title('策略盈亏变化趋势')
            plt.xlabel('评估轮次')
            plt.ylabel('盈亏 (USDT)')
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.savefig(os.path.join(charts_dir, 'pnl_trend.png'))
            
            # 绘制累计盈亏图
            plt.figure(figsize=(10, 6))
            plt.plot(df['round'], df['net_pnl'].cumsum(), 'b-o', linewidth=2)
            plt.axhline(y=0, color='r', linestyle='--', alpha=0.7)
            plt.title('策略累计盈亏曲线')
            plt.xlabel('评估轮次')
            plt.ylabel('累计盈亏 (USDT)')
            plt.grid(True, alpha=0.3)
            plt.savefig(os.path.join(charts_dir, 'cumulative_pnl.png'))
            
            print(f"\n图表已保存至: {charts_dir}")
        except Exception as e:
            print(f"生成图表失败: {e}")
    
    print("="*80 + "\n")
    
    # 显示报告文件路径
    report_file = os.path.join(session_dir, "evaluation_report.md")
    if os.path.exists(report_file):
        print(f"详细报告文件: {report_file}")

def list_all_sessions():
    """列出所有评估会话"""
    if not os.path.exists(EVAL_RESULTS_DIR):
        print("没有找到任何评估会话")
        return
        
    session_dirs = [d for d in os.listdir(EVAL_RESULTS_DIR) 
                   if os.path.isdir(os.path.join(EVAL_RESULTS_DIR, d)) and d.startswith('eval_')]
    
    if not session_dirs:
        print("没有找到任何评估会话")
        return
        
    # 按创建时间排序
    session_dirs.sort(reverse=True)
    
    print("\n" + "="*80)
    print("评估会话列表:")
    print("-"*80)
    
    sessions_table = []
    for session_dir in session_dirs:
        results_file = os.path.join(EVAL_RESULTS_DIR, session_dir, "evaluation_results.json")
        if os.path.exists(results_file):
            try:
                with open(results_file, 'r', encoding='utf-8') as f:
                    results = json.load(f)
                    
                win_rate = results.get("win_rate", 0) * 100
                net_pnl = results.get("net_pnl", 0)
                
                sessions_table.append([
                    session_dir,
                    results.get("start_time", "N/A"),
                    results.get("end_time", "N/A"),
                    ', '.join(results.get("symbols", [])),
                    f"{results.get('completed_rounds', 0)}/{results.get('total_rounds', 0)}",
                    f"{win_rate:.2f}%",
                    f"{net_pnl:.2f}"
                ])
            except Exception as e:
                sessions_table.append([session_dir, "错误", "错误", "错误", "错误", "错误", "错误"])
    
    print(tabulate(sessions_table, 
                  headers=["会话ID", "开始时间", "结束时间", "交易对", "轮次", "胜率", "净盈亏"],
                  tablefmt="grid"))
    print("="*80 + "\n")

def compare_sessions():
    """比较不同会话的策略表现"""
    results = get_latest_evaluation_results(limit=10)
    
    if not results:
        print("没有找到可比较的评估会话")
        return
        
    print("\n" + "="*80)
    print("策略评估会话比较:")
    print("-"*80)
    
    compare_table = []
    for result in results:
        session_id = result.get("session_id", "N/A")
        symbols = ', '.join(result.get("symbols", []))
        completed = f"{result.get('completed_rounds', 0)}/{result.get('total_rounds', 0)}"
        win_rate = result.get("win_rate", 0) * 100
        net_pnl = result.get("net_pnl", 0)
        total_orders = result.get("orders_created", 0)
        closed_orders = result.get("orders_closed", 0)
        
        compare_table.append([
            session_id,
            symbols,
            completed,
            total_orders,
            closed_orders,
            result.get("win_orders", 0),
            f"{win_rate:.2f}%",
            f"{result.get('total_profit', 0):.2f}",
            f"{result.get('total_loss', 0):.2f}",
            f"{net_pnl:.2f}"
        ])
    
    print(tabulate(compare_table, 
                  headers=["会话ID", "交易对", "轮次", "订单数", "已平仓", "盈利数", "胜率", "盈利", "亏损", "净盈亏"],
                  tablefmt="grid"))
    print("="*80 + "\n")

def view_simulation_stats():
    """查看模拟盘统计数据"""
    stats = stat_sim_results()
    
    print("\n" + "="*80)
    print("模拟盘统计数据:")
    print("-"*80)
    
    win_rate = stats.get("win_rate", 0) * 100
    
    print(f"总订单数: {stats.get('total', 0)}")
    print(f"盈利订单: {stats.get('win', 0)}")
    print(f"胜率: {win_rate:.2f}%")
    print(f"总盈利: {stats.get('profit', 0):.2f} USDT")
    print(f"总亏损: {stats.get('loss', 0):.2f} USDT")
    print(f"净盈亏: {stats.get('net', 0):.2f} USDT")
    
    print("="*80 + "\n")

def main():
    """主函数"""
    args = parse_args()
    logger = setup_logging(args.debug)
    
    try:
        # 查看模拟盘统计
        if args.sim:
            view_simulation_stats()
            return 0
            
        # 根据参数执行不同操作
        if args.session:
            view_session_results(args.session, args.visual)
        elif args.compare:
            compare_sessions()
        elif args.all:
            list_all_sessions()
        else:
            # 默认查看最新评估结果
            latest_results = get_latest_evaluation_results(limit=1)
            if latest_results:
                view_session_results(latest_results[0].get("session_id", ""), args.visual)
            else:
                list_all_sessions()
                
    except KeyboardInterrupt:
        logger.warning("用户中断操作")
        return 1
    except Exception as e:
        logger.error(f"执行过程中出错: {e}", exc_info=True)
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main()) 