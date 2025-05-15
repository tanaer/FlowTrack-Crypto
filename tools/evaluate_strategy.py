#!/usr/bin/env python
"""策略评估与回测工具

用于评估LLM生成的交易策略在不同市场条件下的胜率和盈亏状况。
"""
import os
import sys
import logging
import argparse
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.simulation.strategy_evaluator import generate_and_evaluate_strategy
from src.config import config


def setup_logging(debug=False):
    """配置日志级别和格式"""
    log_level = logging.DEBUG if debug else logging.INFO
    
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"strategy_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
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
    parser = argparse.ArgumentParser(description="策略评估与回测工具")
    parser.add_argument('-d', '--debug', action='store_true', help="启用调试日志")
    parser.add_argument('-s', '--symbols', type=str, default="BTCUSDT,ETHUSDT,SOLUSDT,SUIUSDT", 
                        help="要分析的交易对列表，用逗号分隔")
    parser.add_argument('--days', type=int, default=3, help="评估天数（默认3天）")
    parser.add_argument('--interval', type=int, default=4, help="策略生成间隔（小时，默认4小时）")
    parser.add_argument('--fast', action='store_true', help="使用快速模式（随机模拟成交，不依赖实际行情）")
    parser.add_argument('-t', '--telegram', action='store_true', help="将结果发送到Telegram")
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


def main():
    """主函数"""
    args = parse_args()
    global logger
    logger = setup_logging(args.debug)
    
    try:
        # 解析交易对
        symbols = args.symbols.split(',')
        logger.info(f"开始策略评估，交易对: {symbols}, 天数: {args.days}, 间隔: {args.interval}小时")
        
        # 执行策略评估
        results = generate_and_evaluate_strategy(
            symbols=symbols,
            days=args.days,
            interval_hours=args.interval,
            fast_mode=args.fast
        )
        
        # 输出评估结果
        if results:
            win_rate = results.get("win_rate", 0) * 100
            net_pnl = results.get("net_pnl", 0)
            total_orders = results.get("orders_created", 0)
            closed_orders = results.get("orders_closed", 0)
            
            logger.info("策略评估完成，结果摘要:")
            print("\n" + "="*80 + "\n")
            print(f"策略评估结果摘要 (会话ID: {results.get('session_id', 'N/A')})")
            print(f"交易对: {', '.join(symbols)}")
            print(f"评估天数: {args.days}, 间隔: {args.interval}小时")
            print(f"总订单数: {total_orders}, 已平仓: {closed_orders}")
            print(f"胜率: {win_rate:.2f}%")
            print(f"盈利: {results.get('total_profit', 0):.2f} USDT")  
            print(f"亏损: {results.get('total_loss', 0):.2f} USDT")
            print(f"净盈亏: {net_pnl:.2f} USDT")
            print("\n详细报告已保存至: eval_results/" + results.get('session_id', '') + "/evaluation_report.md")
            print("\n" + "="*80 + "\n")
            
            # 发送Telegram消息
            if args.telegram:
                logger.info("发送评估结果到Telegram...")
                
                # 构建消息
                msg = []
                msg.append("\n## 📈 策略评估结果")
                msg.append(f"- **评估时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                msg.append(f"- **会话ID**: `{results.get('session_id', 'N/A')}`")
                msg.append(f"- **交易对**: {', '.join(symbols)}")
                msg.append(f"- **评估天数**: {args.days}")
                msg.append(f"- **间隔**: {args.interval}小时")
                msg.append(f"- **总订单数**: {total_orders}")
                msg.append(f"- **已平仓订单**: {closed_orders}")
                msg.append(f"- **胜率**: {win_rate:.2f}%")
                msg.append(f"- **总盈利**: {results.get('total_profit', 0):.2f} USDT")
                msg.append(f"- **总亏损**: {results.get('total_loss', 0):.2f} USDT")
                msg.append(f"- **净盈亏**: {net_pnl:.2f} USDT")
                
                # 添加优化建议
                if win_rate < 45:
                    msg.append("\n**建议**: 提高策略胜率 - 调整更严格的入场条件，确保有更明确的趋势信号")
                if net_pnl < 0:
                    msg.append("\n**建议**: 改善风险管理 - 缩小止损范围，放大止盈空间，提高盈亏比")
                
                # 发送消息
                message = "\n".join(msg)
                send_telegram_message(message)
        else:
            logger.error("策略评估未返回有效结果")
        
    except KeyboardInterrupt:
        logger.warning("用户中断评估过程")
        return 1
    except Exception as e:
        logger.error(f"评估过程中出错: {e}", exc_info=True)
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main()) 