"""主应用程序入口

币安资金流向分析的主要入口点。
"""
import asyncio
import time
import logging
import os
import sys
import argparse
import schedule
from datetime import datetime
import pandas as pd
import json

from .config import SYMBOLS, config
from .data.binance_client import get_klines_data, get_orderbook_stats, client, get_short_term_trading_data
from .analysis.market_analysis import analyze_funding_flow_trend, detect_anomalies, analyze_funding_pressure
from .api.llm_client import generate_analysis, EnhancedJSONEncoder
from .notification.telegram_sender import send_telegram_message_async
from .analysis.result_manager import save_analysis_result
from .simulation.trade_simulator import parse_ai_strategy, save_sim_orders, stat_sim_results, get_sim_orders_24h

# 配置日志
logger = logging.getLogger(__name__)

# 设置第三方库日志等级，抑制不必要的信息
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("binance").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("schedule").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)


async def main(dry_run=False, debug=False, symbols=None):
    """主函数，获取数据并生成分析报告
    
    参数:
        dry_run: 如果为True，则不发送到Telegram
        debug: 如果为True，则显示更详细的日志和保存调试信息
        symbols: 要分析的交易对列表，如果为None则使用配置中的所有交易对
    """
    try:
        logger.info("开始执行资金流向分析程序")
        
        # 使用指定的交易对或默认配置中的交易对
        target_symbols = symbols if symbols else SYMBOLS
        logger.info(f"本次分析的交易对: {', '.join(target_symbols)}")
        
        # 检查配置是否完整
        if not config.has_section('API') or not config.has_section('TELEGRAM'):
            logger.error("配置文件缺失必要部分，请检查config.ini文件")
            print("配置文件缺失必要部分，请检查config.ini文件")
            return
            
        # 检查API密钥是否有效
        try:
            client.get_system_status()
            logger.info("Binance API连接正常")
        except Exception as e:
            logger.error(f"Binance API连接失败: {e}")
            print(f"Binance API连接失败: {e}")
            return
        
        all_results = {}
        
        # 遍历所有交易对获取数据
        for symbol in target_symbols:
            logger.info(f"开始获取 {symbol} 数据")
            
            try:
                # 获取短线交易数据（包含了多周期K线、订单簿、大单分析等全部数据）
                logger.info(f"获取 {symbol} 现货短线交易数据")
                spot_short_term = get_short_term_trading_data(symbol, is_futures=False)
                
                logger.info(f"获取 {symbol} 期货短线交易数据")
                futures_short_term = get_short_term_trading_data(symbol, is_futures=True)
                
                # 提取1小时K线进行基础分析
                spot_klines = spot_short_term.get('klines', {}).get('1h', [])
                futures_klines = futures_short_term.get('klines', {}).get('1h', [])
                
                # 提取订单簿数据
                spot_orderbook = spot_short_term.get('orderbook', {})
                futures_orderbook = futures_short_term.get('orderbook', {})
                
                # 分析资金流向趋势
                spot_trend = analyze_funding_flow_trend(spot_klines)
                futures_trend = analyze_funding_flow_trend(futures_klines)
                
                # 检测异常交易
                spot_anomalies = detect_anomalies(spot_klines)
                futures_anomalies = detect_anomalies(futures_klines)
                
                # 分析资金压力
                spot_pressure = analyze_funding_pressure(spot_klines, spot_orderbook)
                futures_pressure = analyze_funding_pressure(futures_klines, futures_orderbook)
                
                # 汇总结果
                all_results[symbol] = {
                    'spot': {
                        'klines': spot_klines,
                        'orderbook': spot_orderbook,
                        'trend': spot_trend,
                        'anomalies': spot_anomalies,
                        'pressure': spot_pressure
                    },
                    'futures': {
                        'klines': futures_klines,
                        'orderbook': futures_orderbook,
                        'trend': futures_trend,
                        'anomalies': futures_anomalies,
                        'pressure': futures_pressure
                    },
                    'spot_short_term': spot_short_term,
                    'futures_short_term': futures_short_term
                }
                
                logger.info(f"{symbol} 数据处理完成")
            except Exception as e:
                logger.error(f"{symbol} 数据处理失败: {e}")
                
                # 如果在调试模式下，则记录详细错误
                if debug:
                    import traceback
                    logger.error(traceback.format_exc())
                
                # 继续处理其他交易对
                continue
        
        # 输出调试信息
        if debug:
            debug_dir = os.path.join('logs', 'debug')
            os.makedirs(debug_dir, exist_ok=True)
            debug_file = os.path.join(debug_dir, f"raw_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            
            try:
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(all_results, f, ensure_ascii=False, indent=2, cls=EnhancedJSONEncoder)
                logger.info(f"调试数据已保存到 {debug_file}")
            except Exception as e:
                logger.error(f"保存调试数据失败: {e}")
        
        # 发送数据到AI进行解读
        logger.info("发送数据到AI服务进行解读")
        analysis_result = generate_analysis(all_results)

        # 保存本次分析结果
        save_analysis_result(analysis_result)
        
        # 如果是空结果，则返回
        if not analysis_result or "系统生成分析时发生错误" in analysis_result:
            logger.error("生成分析结果失败")
            if not dry_run:
                await send_telegram_message_async("系统生成分析时发生错误，请检查日志。", as_image=False)
            return
        
        # 解析AI策略并模拟下单
        sim_orders = parse_ai_strategy(analysis_result)
        if sim_orders:
            save_sim_orders(sim_orders)
            logger.info(f"本轮模拟下单 {len(sim_orders)} 笔，已保存")
        else:
            logger.info("未检测到AI策略建议，未模拟下单")
        
        # 统计模拟交易胜率等信息
        sim_stat = stat_sim_results()
        stat_md = f"\n\n---\n**模拟交易统计（近全部历史）**\n\n- 总单数: {sim_stat['total']}\n- 胜率: {sim_stat['win_rate']*100:.2f}%\n- 总盈利: {sim_stat['profit']} USDT\n- 总亏损: {sim_stat['loss']} USDT\n- 净收益: {sim_stat['net']} USDT\n"
        final_report = analysis_result + stat_md
        
        # 发送结果到Telegram
        if not dry_run:
            logger.info("发送分析结果到Telegram")
            await send_telegram_message_async(final_report, as_image=True)
            # 追加推送最近24小时模拟盘明细（不转图片）
            sim_24h_md = get_sim_orders_24h()
            await send_telegram_message_async(sim_24h_md, as_image=False)
        else:
            logger.info("Dry run模式，不发送到Telegram")
            # 在控制台打印策略表格部分
            print_strategy_table(analysis_result)
        
        logger.info("资金流向分析完成")
        print("资金流向分析已完成" + (" 并发送到Telegram" if not dry_run else " (Dry Run模式)"))
        
    except Exception as e:
        logger.error(f"执行过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"执行过程中发生错误: {e}")
        
        # 在错误情况下发送通知
        if not dry_run:
            try:
                await send_telegram_message_async(f"系统运行出错: {str(e)}", as_image=False)
            except:
                pass


def print_strategy_table(analysis_result):
    """打印策略表格到控制台"""
    try:
        # 提取策略表格部分
        if "[STRATEGY_TABLE_BEGIN]" in analysis_result and "[STRATEGY_TABLE_END]" in analysis_result:
            table_start = analysis_result.find("[STRATEGY_TABLE_BEGIN]") + len("[STRATEGY_TABLE_BEGIN]")
            table_end = analysis_result.find("[STRATEGY_TABLE_END]")
            table = analysis_result[table_start:table_end].strip()
            print("\n策略表格:")
            print(table)
        else:
            print("未找到策略表格")
    except Exception as e:
        print(f"打印策略表格出错: {e}")


def setup_logging(debug=False):
    """设置日志配置
    
    参数:
        debug: 如果为True，则设置为调试等级
    """
    log_level = logging.DEBUG if debug else logging.INFO
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # 确保日志目录存在
    os.makedirs('logs', exist_ok=True)
    
    # 配置根日志
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.FileHandler(f"logs/flowtrack_{datetime.now().strftime('%Y%m%d')}.log"),
            logging.StreamHandler()
        ]
    )
    
    logger.info(f"日志系统初始化完成，等级: {'DEBUG' if debug else 'INFO'}")


def run_analysis(dry_run=False, debug=False, symbols=None):
    """包装函数，执行分析任务
    
    参数:
        dry_run: 如果为True，则不发送到Telegram
        debug: 如果为True，则显示更详细的日志
        symbols: 要分析的交易对列表，如果为None则使用配置中的所有交易对
    """
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 开始执行Binance资金流向分析...")
    asyncio.run(main(dry_run=dry_run, debug=debug, symbols=symbols))
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 分析完成")


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="FlowTrack-Crypto - 加密货币资金流向分析工具")
    parser.add_argument("--dry-run", action="store_true", help="运行但不发送到Telegram")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    parser.add_argument("--no-schedule", action="store_true", help="仅运行一次，不设置定时任务")
    parser.add_argument("--symbols", type=str, help="要分析的交易对，以逗号分隔，例如: BTCUSDT,ETHUSDT")
    
    return parser.parse_args()


if __name__ == "__main__":
    try:
        # 解析命令行参数
        args = parse_arguments()
        
        # 设置日志
        setup_logging(debug=args.debug)
        
        # 解析交易对
        selected_symbols = args.symbols.split(',') if args.symbols else None
        
        # 检查配置文件是否存在
        if not os.path.exists('config.ini'):
            logger.error("配置文件 config.ini 不存在")
            print("错误: 配置文件 config.ini 不存在")
            print("请创建config.ini文件并填写必要配置")
            print("格式例如:")
            print("[API]")
            print("BINANCE_API_KEY = 你的Binance API KEY")
            print("BINANCE_API_SECRET = 你的Binance API SECRET")
            print("DEEPSEEK_API_KEY = 你的DeepSeek API KEY")
            print("[TELEGRAM]")
            print("BOT_TOKEN = 你的Telegram Bot Token")
            print("CHAT_ID = 你的Telegram聊天ID")
            sys.exit(1)
        
        if args.no_schedule:
            # 仅运行一次
            logger.info("单次运行模式")
            run_analysis(dry_run=args.dry_run, debug=args.debug, symbols=selected_symbols)
        else:
            # 设置每小时执行一次
            schedule.every().hour.do(
                run_analysis, 
                dry_run=args.dry_run, 
                debug=args.debug, 
                symbols=selected_symbols
            )
            
            logger.info("计划任务模式，将每小时执行一次分析")
            print("程序已启动，将每小时执行一次分析")
            print(f"首次分析将在 {datetime.now().replace(minute=0, second=0, microsecond=0) + pd.Timedelta(hours=1)} 执行")
            print("你也可以按 Ctrl+C 停止程序")
            
            # 立即执行一次
            run_analysis(dry_run=args.dry_run, debug=args.debug, symbols=selected_symbols)
            
            # 持续运行并检查调度任务
            while True:
                schedule.run_pending()
                time.sleep(1)
                
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
        print("\n程序被用户中断")
    except Exception as e:
        logger.critical(f"程序执行失败: {e}", exc_info=True)
        print(f"程序执行失败: {e}")
        import traceback
        traceback.print_exc() 