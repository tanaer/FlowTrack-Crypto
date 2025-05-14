"""主应用程序入口

币安资金流向分析的主要入口点。
"""
import asyncio
import time
import logging
import os
import schedule
from datetime import datetime
import pandas as pd

from .config import SYMBOLS, config
from .data.binance_client import get_klines_data, get_orderbook_stats, client
from .analysis.market_analysis import analyze_funding_flow_trend, detect_anomalies, analyze_funding_pressure
from .api.llm_client import generate_analysis
from .notification.telegram_sender import send_telegram_message_async

# 配置日志
logger = logging.getLogger(__name__)


async def main():
    """主函数，获取数据并生成分析报告"""
    try:
        logger.info("开始执行资金流向分析程序")
        
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
        for symbol in SYMBOLS:
            logger.info(f"开始获取 {symbol} 数据")
            
            # 获取现货K线数据
            spot_klines = get_klines_data(symbol, interval='5m', limit=50, is_futures=False)
            if not spot_klines:
                logger.warning(f"{symbol} 现货K线数据获取失败")
                
            # 获取期货K线数据
            futures_klines = get_klines_data(symbol, interval='5m', limit=50, is_futures=True)
            if not futures_klines:
                logger.warning(f"{symbol} 期货K线数据获取失败")
                
            # 获取订单簿数据
            spot_orderbook = get_orderbook_stats(symbol, is_futures=False)
            futures_orderbook = get_orderbook_stats(symbol, is_futures=True)
            
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
                }
            }
            
            logger.info(f"{symbol} 数据处理完成")
            
        # 发送数据到AI进行解读
        logger.info("发送数据到AI服务进行解读")
        analysis_result = generate_analysis(all_results)
        
        # 发送结果到Telegram
        logger.info("发送分析结果到Telegram")
        await send_telegram_message_async(analysis_result, as_image=True)
        
        logger.info("资金流向分析完成")
        print("资金流向分析已完成并发送到Telegram")
        
    except Exception as e:
        logger.error(f"执行过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"执行过程中发生错误: {e}")


def run_analysis():
    """包装函数，执行分析任务"""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 开始执行Binance资金流向分析...")
    asyncio.run(main())
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 分析完成")


if __name__ == "__main__":
    try:
        # 检查配置文件是否存在
        if not os.path.exists('config.ini'):
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
            exit(1)
        
        # 设置每小时执行一次
        schedule.every().hour.do(run_analysis)
        
        print("程序已启动，将每小时执行一次分析")
        print(f"首次分析将在 {datetime.now().replace(minute=0, second=0, microsecond=0) + pd.Timedelta(hours=1)} 执行")
        print("你也可以按 Ctrl+C 停止程序")
        
        # 立即执行一次
        run_analysis()
        
        # 持续运行并检查调度任务
        while True:
            schedule.run_pending()
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序执行失败: {e}")
        import traceback
        traceback.print_exc() 