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
                'spot': {
                    'trend': {
                        'direction': 'neutral',
                        'strength': 0,
                        'net_inflow': 0
                    },
                    'pressure': {
                        'buy': 0,
                        'sell': 0,
                        'ratio': 0
                    },
                    'anomalies': [],
                    'orderbook': spot_data.get('orderbook', {}),
                },
                'futures': {
                    'trend': {
                        'direction': 'neutral',
                        'strength': 0,
                        'net_inflow': 0
                    },
                    'pressure': {
                        'buy': 0,
                        'sell': 0,
                        'ratio': 0
                    },
                    'anomalies': [],
                    'orderbook': futures_data.get('orderbook', {}),
                },
                'spot_short_term': spot_data,
                'futures_short_term': futures_data
            }
            
            # 如果有K线数据，计算基本趋势
            if spot_data.get('klines', {}).get('1h'):
                klines = spot_data['klines']['1h']
                if len(klines) > 10:
                    # 简单趋势计算：最近10根K线收盘价变化
                    closes = [k['close'] for k in klines[-10:]]
                    net_inflow = sum([k['net_inflow'] for k in klines[-10:]])
                    if closes[-1] > closes[0]:
                        data[symbol]['spot']['trend'] = {
                            'direction': 'up',
                            'strength': (closes[-1] - closes[0]) / closes[0] * 100,
                            'net_inflow': net_inflow
                        }
                    else:
                        data[symbol]['spot']['trend'] = {
                            'direction': 'down',
                            'strength': (closes[0] - closes[-1]) / closes[0] * 100,
                            'net_inflow': net_inflow
                        }
            
            if futures_data.get('klines', {}).get('1h'):
                klines = futures_data['klines']['1h']
                if len(klines) > 10:
                    # 简单趋势计算：最近10根K线收盘价变化
                    closes = [k['close'] for k in klines[-10:]]
                    net_inflow = sum([k['net_inflow'] for k in klines[-10:]])
                    if closes[-1] > closes[0]:
                        data[symbol]['futures']['trend'] = {
                            'direction': 'up',
                            'strength': (closes[-1] - closes[0]) / closes[0] * 100,
                            'net_inflow': net_inflow
                        }
                    else:
                        data[symbol]['futures']['trend'] = {
                            'direction': 'down',
                            'strength': (closes[0] - closes[-1]) / closes[0] * 100,
                            'net_inflow': net_inflow
                        }
                        
            # 计算买卖压力
            if spot_data.get('orderbook'):
                ob = spot_data['orderbook']
                data[symbol]['spot']['pressure'] = {
                    'buy': ob.get('bids_value', 0),
                    'sell': ob.get('asks_value', 0),
                    'ratio': ob.get('volume_imbalance', 0)
                }
                
            if futures_data.get('orderbook'):
                ob = futures_data['orderbook']
                data[symbol]['futures']['pressure'] = {
                    'buy': ob.get('bids_value', 0),
                    'sell': ob.get('asks_value', 0),
                    'ratio': ob.get('volume_imbalance', 0)
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