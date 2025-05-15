#!/usr/bin/env python
"""模拟盘启动工具

解析LLM生成的策略建议，创建模拟订单并追踪盈亏状况。
支持将结果通知发送到Telegram。
"""
import os
import sys
import time
import json
import logging
import argparse
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.binance_client import get_klines_data
from src.simulation.trade_simulator import (
    parse_ai_strategy,
    save_sim_orders,
    load_open_orders,
    backtest_open_orders,
    stat_sim_results,
    get_sim_orders_24h
)
from src.config import config


def setup_logging(debug=False):
    """配置日志级别和格式"""
    log_level = logging.DEBUG if debug else logging.INFO
    
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
    # 配置日志
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file)
        ]
    )
    
    # 设置第三方库日志级别为WARNING，减少干扰
    logging.getLogger("binance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="模拟盘启动工具")
    parser.add_argument('-d', '--debug', action='store_true', help="启用调试日志")
    parser.add_argument('-f', '--file', type=str, help="LLM策略分析文件路径")
    parser.add_argument('-m', '--monitor', action='store_true', help="监控模式，持续监控所有未平仓订单")
    parser.add_argument('-t', '--telegram', action='store_true', help="将结果发送到Telegram")
    parser.add_argument('-i', '--interval', type=int, default=5, help="监控模式下检查订单状态的间隔（分钟）")
    parser.add_argument('-s', '--stats', action='store_true', help="仅显示模拟盘统计数据")
    return parser.parse_args()


def send_telegram_message(message: str):
    """发送Telegram消息"""
    try:
        import requests
        
        # 从配置中获取Telegram设置
        bot_token = config.get('TELEGRAM', 'BOT_TOKEN')
        chat_id = config.get('TELEGRAM', 'CHAT_ID')
        
        if not bot_token or not chat_id:
            logger.error("Telegram配置不完整，无法发送消息")
            return False
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        
        response = requests.post(url, data=data)
        if response.status_code == 200:
            logger.info("Telegram消息发送成功")
            return True
        else:
            logger.error(f"Telegram消息发送失败: {response.status_code}, {response.text}")
            return False
    except Exception as e:
        logger.error(f"发送Telegram消息时出错: {e}")
        return False


def run_simulation_from_file(file_path: str, send_to_telegram: bool = False):
    """从文件加载策略分析结果并创建模拟订单"""
    logger.info(f"从文件 {file_path} 加载策略分析...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            analysis_text = f.read()
            
        # 解析策略并创建订单
        orders = parse_ai_strategy(analysis_text)
        if not orders:
            logger.warning("未能从分析结果中解析出任何订单")
            return False
            
        # 保存模拟订单
        saved_path = save_sim_orders(orders)
        logger.info(f"已创建 {len(orders)} 个模拟订单，保存至 {saved_path}")
        
        # 构建订单概览消息
        msg = []
        msg.append("\n## 🤖 模拟盘订单创建")
        msg.append(f"- **时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        msg.append(f"- **文件来源**: {os.path.basename(file_path)}")
        msg.append(f"- **订单数量**: {len(orders)}")
        msg.append("\n### 订单详情:")
        
        for order in orders:
            if order['direction'] == '观望':
                continue
            msg.append(f"- **{order['symbol']}** ({order['type']}): {order['direction']} @ {order['entry']} → 止损:{order['stop']} 止盈:{order['take']} 仓位:{order['position']}")
        
        message = "\n".join(msg)
        print(message)
        
        # 发送到Telegram
        if send_to_telegram:
            logger.info("发送订单信息到Telegram...")
            send_telegram_message(message)
        
        return True
    except Exception as e:
        logger.error(f"从文件创建模拟订单时出错: {e}")
        return False


def monitor_orders(interval_minutes: int = 5, send_to_telegram: bool = False):
    """监控模式，持续监控所有未平仓订单"""
    logger.info(f"启动监控模式，检查间隔: {interval_minutes}分钟")
    
    try:
        while True:
            logger.info("检查未平仓订单...")
            
            # 加载并回测所有未平仓订单
            open_orders = load_open_orders()
            if not open_orders:
                logger.info("当前无未平仓订单")
            else:
                logger.info(f"找到 {len(open_orders)} 个未平仓订单")
                
                # 回测订单
                closed_orders = backtest_open_orders()
                
                if closed_orders:
                    logger.info(f"{len(closed_orders)} 个订单已平仓")
                    
                    # 构建平仓订单消息
                    msg = []
                    msg.append("\n## 🔔 模拟盘订单更新")
                    msg.append(f"- **时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    msg.append(f"- **平仓订单数**: {len(closed_orders)}")
                    
                    # 统计盈亏
                    profit_orders = [o for o in closed_orders if o.get('pnl', 0) > 0]
                    loss_orders = [o for o in closed_orders if o.get('pnl', 0) <= 0]
                    total_pnl = sum(o.get('pnl', 0) for o in closed_orders)
                    
                    msg.append(f"- **盈利订单**: {len(profit_orders)}")
                    msg.append(f"- **亏损订单**: {len(loss_orders)}")
                    msg.append(f"- **总盈亏**: {total_pnl:.2f} USDT")
                    
                    msg.append("\n### 平仓详情:")
                    for order in closed_orders:
                        profit_emoji = "✅" if order.get('pnl', 0) > 0 else "❌"
                        msg.append(f"- {profit_emoji} **{order['symbol']}** ({order['type']}): {order['direction']} {order['close_reason']} @ {order['close_price']} | PnL: {order.get('pnl', 0):.2f} USDT")
                    
                    # 更新统计数据
                    stats = stat_sim_results()
                    
                    msg.append("\n### 总体表现:")
                    msg.append(f"- **总订单**: {stats.get('total', 0)}")
                    msg.append(f"- **盈利订单**: {stats.get('win', 0)}")
                    msg.append(f"- **胜率**: {stats.get('win_rate', 0)*100:.2f}%")
                    msg.append(f"- **总盈利**: {stats.get('profit', 0):.2f} USDT")
                    msg.append(f"- **总亏损**: {stats.get('loss', 0):.2f} USDT")
                    msg.append(f"- **净盈亏**: {stats.get('net', 0):.2f} USDT")
                    
                    message = "\n".join(msg)
                    print(message)
                    
                    # 发送到Telegram
                    if send_to_telegram:
                        logger.info("发送订单更新到Telegram...")
                        send_telegram_message(message)
            
            # 等待下一次检查
            logger.info(f"等待 {interval_minutes} 分钟后再次检查...")
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("监控模式被用户中断")
    except Exception as e:
        logger.error(f"监控订单时出错: {e}")


def show_simulation_stats(send_to_telegram: bool = False):
    """显示模拟盘统计数据"""
    try:
        stats = stat_sim_results()
        orders_24h = get_sim_orders_24h()
        
        # 构建统计消息
        msg = []
        msg.append("\n## 📊 模拟盘统计数据")
        msg.append(f"- **统计时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        msg.append(f"- **总订单数**: {stats.get('total', 0)}")
        msg.append(f"- **盈利订单**: {stats.get('win', 0)}")
        msg.append(f"- **胜率**: {stats.get('win_rate', 0)*100:.2f}%")
        msg.append(f"- **总盈利**: {stats.get('profit', 0):.2f} USDT")
        msg.append(f"- **总亏损**: {stats.get('loss', 0):.2f} USDT")
        msg.append(f"- **净盈亏**: {stats.get('net', 0):.2f} USDT")
        
        # 添加24小时订单信息
        msg.append(orders_24h)
        
        message = "\n".join(msg)
        print(message)
        
        # 发送到Telegram
        if send_to_telegram:
            logger.info("发送统计数据到Telegram...")
            send_telegram_message(message)
            
        return True
    except Exception as e:
        logger.error(f"显示模拟盘统计数据时出错: {e}")
        return False


def main():
    """主函数"""
    args = parse_args()
    global logger
    logger = setup_logging(args.debug)
    
    try:
        # 确保模拟结果目录存在
        sim_results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'sim_results')
        sim_results_dir = os.path.abspath(sim_results_dir)
        os.makedirs(sim_results_dir, exist_ok=True)
        
        # 仅显示统计数据
        if args.stats:
            logger.info("显示模拟盘统计数据...")
            show_simulation_stats(send_to_telegram=args.telegram)
            return 0
            
        # 从文件加载策略并创建订单
        if args.file:
            if not os.path.exists(args.file):
                logger.error(f"文件不存在: {args.file}")
                return 1
                
            success = run_simulation_from_file(args.file, send_to_telegram=args.telegram)
            if not success:
                return 1
        
        # 监控模式
        if args.monitor:
            logger.info("启动监控模式...")
            monitor_orders(interval_minutes=args.interval, send_to_telegram=args.telegram)
            
        # 如果既没有指定文件也没有开启监控模式，则显示使用帮助
        if not args.file and not args.monitor and not args.stats:
            logger.info("未指定操作模式，请使用 -f 指定策略文件, -m 开启监控模式, 或 -s 显示统计数据")
            parser.print_help()
            return 1
            
    except KeyboardInterrupt:
        logger.warning("操作被用户中断")
        return 1
    except Exception as e:
        logger.error(f"执行过程中出错: {e}", exc_info=True)
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main()) 