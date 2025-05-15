#!/usr/bin/env python
"""自动策略评估与模拟交易工具

评估策略并将结果自动输入到模拟盘进行交易。
1. 评估策略
2. 生成评估报告
3. 将报告作为模拟盘输入
4. 启动模拟盘监控
"""
import os
import sys
import time
import shutil
import logging
import argparse
import subprocess
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.simulation.strategy_evaluator import get_latest_evaluation_results
from src.config import config


def setup_logging(debug=False):
    """配置日志级别和格式"""
    log_level = logging.DEBUG if debug else logging.INFO
    
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"auto_eval_trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
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
    parser = argparse.ArgumentParser(description="自动策略评估与模拟交易工具")
    parser.add_argument('-d', '--debug', action='store_true', help="启用调试日志")
    parser.add_argument('-s', '--symbols', type=str, default="BTCUSDT,ETHUSDT,SOLUSDT,SUIUSDT", 
                        help="要分析的交易对列表，用逗号分隔")
    parser.add_argument('--days', type=int, default=3, help="评估天数（默认3天）")
    parser.add_argument('--interval', type=int, default=4, help="策略生成间隔（小时，默认4小时）")
    parser.add_argument('--fast', action='store_true', help="使用快速模式（随机模拟成交，不依赖实际行情）")
    parser.add_argument('-c', '--continuous', action='store_true', help="连续模式，完成一轮后继续执行")
    parser.add_argument('-t', '--telegram', action='store_true', help="将结果发送到Telegram")
    parser.add_argument('-m', '--monitor-interval', type=int, default=5, help="模拟盘监控间隔（分钟，默认5分钟）")
    return parser.parse_args()


def run_evaluate_script(symbols, days, interval, fast_mode, telegram):
    """运行策略评估脚本"""
    logger.info("启动策略评估...")
    
    cmd = [
        sys.executable, 
        os.path.join(os.path.dirname(__file__), "evaluate_strategy.py"),
        "-s", symbols
    ]
    
    if fast_mode:
        cmd.append("--fast")
    
    cmd.extend(["--days", str(days)])
    cmd.extend(["--interval", str(interval)])
    
    if telegram:
        cmd.append("-t")
        
    logger.info(f"执行命令: {' '.join(cmd)}")
    
    try:
        # 执行评估脚本
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            logger.error(f"策略评估失败: {stderr}")
            return None
            
        logger.info(f"策略评估完成，返回码: {process.returncode}")
        return True
    except Exception as e:
        logger.error(f"执行评估脚本时出错: {e}")
        return None


def get_latest_evaluation_report():
    """获取最新的评估报告文件路径"""
    try:
        results = get_latest_evaluation_results(limit=1)
        if not results:
            logger.error("未找到任何评估结果")
            return None
            
        session_id = results[0].get("session_id")
        if not session_id:
            logger.error("评估结果中没有会话ID")
            return None
            
        # 评估报告路径
        eval_results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'eval_results')
        eval_results_dir = os.path.abspath(eval_results_dir)
        report_file = os.path.join(eval_results_dir, session_id, "evaluation_report.md")
        
        if not os.path.exists(report_file):
            logger.error(f"评估报告文件不存在: {report_file}")
            return None
            
        logger.info(f"找到最新评估报告: {report_file}")
        return report_file
    except Exception as e:
        logger.error(f"获取最新评估报告时出错: {e}")
        return None


def start_simulation(report_file, monitor_interval, telegram):
    """启动模拟盘"""
    logger.info(f"使用评估报告 {report_file} 启动模拟盘...")
    
    # 加载评估报告到模拟盘
    load_cmd = [
        sys.executable, 
        os.path.join(os.path.dirname(__file__), "run_simulation.py"),
        "-f", report_file
    ]
    
    if telegram:
        load_cmd.append("-t")
        
    logger.info(f"加载模拟订单命令: {' '.join(load_cmd)}")
    
    try:
        # 执行加载命令
        process = subprocess.Popen(load_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            logger.error(f"加载模拟订单失败: {stderr}")
            return False
            
        logger.info("模拟订单加载成功，启动监控...")
        
        # 启动监控
        monitor_cmd = [
            sys.executable, 
            os.path.join(os.path.dirname(__file__), "run_simulation.py"),
            "-m",
            "-i", str(monitor_interval)
        ]
        
        if telegram:
            monitor_cmd.append("-t")
            
        logger.info(f"监控命令: {' '.join(monitor_cmd)}")
        
        # 启动模拟盘监控（非阻塞）
        monitor_process = subprocess.Popen(monitor_cmd)
        
        # 等待一段时间
        time.sleep(10)
        
        return True
    except Exception as e:
        logger.error(f"启动模拟盘时出错: {e}")
        return False


def continuous_process(args):
    """连续执行评估和模拟交易"""
    try:
        while True:
            # 执行一轮评估
            success = run_evaluate_script(
                args.symbols, 
                args.days, 
                args.interval, 
                args.fast, 
                args.telegram
            )
            
            if success:
                # 获取评估报告
                report_file = get_latest_evaluation_report()
                
                if report_file:
                    # 启动模拟盘
                    start_simulation(report_file, args.monitor_interval, args.telegram)
            
            # 等待下一轮
            next_run = datetime.now() + timedelta(hours=args.interval)
            logger.info(f"下一轮评估将在 {next_run.strftime('%Y-%m-%d %H:%M:%S')} 进行")
            
            # 等待到下一轮时间
            wait_seconds = (next_run - datetime.now()).total_seconds()
            if wait_seconds > 0:
                time.sleep(wait_seconds)
    except KeyboardInterrupt:
        logger.info("连续执行被用户中断")
    except Exception as e:
        logger.error(f"连续执行过程中出错: {e}")


def main():
    """主函数"""
    args = parse_args()
    global logger
    logger = setup_logging(args.debug)
    
    try:
        # 创建必要的目录
        eval_results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'eval_results')
        eval_results_dir = os.path.abspath(eval_results_dir)
        os.makedirs(eval_results_dir, exist_ok=True)
        
        sim_results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'sim_results')
        sim_results_dir = os.path.abspath(sim_results_dir)
        os.makedirs(sim_results_dir, exist_ok=True)
        
        if args.continuous:
            # 连续模式
            logger.info("启动连续评估与模拟交易模式...")
            continuous_process(args)
        else:
            # 执行单次评估
            success = run_evaluate_script(
                args.symbols, 
                args.days, 
                args.interval, 
                args.fast, 
                args.telegram
            )
            
            if success:
                # 获取评估报告
                report_file = get_latest_evaluation_report()
                
                if report_file:
                    # 启动模拟盘
                    start_simulation(report_file, args.monitor_interval, args.telegram)
        
    except KeyboardInterrupt:
        logger.warning("操作被用户中断")
        return 1
    except Exception as e:
        logger.error(f"执行过程中出错: {e}", exc_info=True)
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main()) 