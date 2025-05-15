#!/usr/bin/env python
"""测试短线交易数据采集与分析

此脚本用于测试短线交易数据获取和分析功能。
"""
import os
import sys
import logging
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.data.binance_client import get_short_term_trading_data
from src.api.llm_client import generate_analysis

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/test_short_term_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    ]
)

logger = logging.getLogger(__name__)

def main():
    """主函数"""
    try:
        # 1. 定义要分析的交易对
        symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'SUIUSDT']
        logger.info(f"开始获取短线交易数据，交易对: {symbols}")
        
        # 2. 获取短线交易数据
        data = {}
        for symbol in symbols:
            logger.info(f"获取 {symbol} 短线交易数据...")
            
            # 获取现货数据
            spot_data = get_short_term_trading_data(symbol, is_futures=False)
            
            # 获取期货数据
            futures_data = get_short_term_trading_data(symbol, is_futures=True)
            
            # 组装数据
            data[symbol] = {
                'spot': spot_data,
                'futures': futures_data,
                'spot_short_term': spot_data,
                'futures_short_term': futures_data
            }
            
            logger.info(f"{symbol} 数据获取完成")
        
        # 3. 生成分析结果
        logger.info("开始生成分析结果...")
        analysis_result = generate_analysis(data)
        
        # 4. 输出分析结果
        logger.info("分析完成，结果如下:")
        print("\n" + "="*80 + "\n")
        print(analysis_result)
        print("\n" + "="*80 + "\n")
        
        # 5. 将结果保存到文件
        output_dir = "results"
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"short_term_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(analysis_result)
        
        logger.info(f"分析结果已保存到: {output_file}")
        
    except Exception as e:
        logger.error(f"测试过程中出错: {e}", exc_info=True)
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main()) 