#!/usr/bin/env python
"""
FlowTrack-Crypto 启动脚本

用于启动币安资金流向分析程序。
"""
import sys
import logging

# 配置基础日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('flowtrack.log')
    ]
)

if __name__ == "__main__":
    try:
        from src.main import run_analysis
        from src.config import config
        
        print("FlowTrack-Crypto 币安资金流向分析工具")
        print("----------------------------------")
        print(f"监控交易对: {config.get('SYMBOLS', 'SYMBOLS') if config.has_option('SYMBOLS', 'SYMBOLS') else '默认交易对'}")
        print("开始启动...")
        
        # 导入主模块并执行
        from src.main import run_analysis
        
        # 直接运行分析
        if len(sys.argv) > 1 and sys.argv[1] == "--run-now":
            print("开始执行一次性分析...")
            run_analysis()
            print("分析完成")
        else:
            # 启动定时任务
            print("启动定时分析任务...")
            from src.main import schedule, time
            
            # 设置每小时执行一次
            schedule.every().hour.do(run_analysis)
            
            # 立即执行一次
            run_analysis()
            
            # 持续运行并检查调度任务
            try:
                while True:
                    schedule.run_pending()
                    from src.simulation.trade_simulator import backtest_open_orders
                    backtest_open_orders()  # 每分钟自动回测模拟订单
                    time.sleep(60)
            except KeyboardInterrupt:
                print("\n程序被用户中断")
        
    except ImportError as e:
        print(f"导入模块失败: {e}")
        print("请确保已正确安装所有依赖项")
        sys.exit(1)
    except Exception as e:
        print(f"程序启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1) 