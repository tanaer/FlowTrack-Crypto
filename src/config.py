"""配置模块

负责加载和管理应用程序配置。
"""
import configparser
import os
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 加载配置文件
def load_config():
    """加载配置文件"""
    config = configparser.ConfigParser()
    
    # 检查配置文件是否存在
    if not os.path.exists('config.ini'):
        logger.error("配置文件 config.ini 不存在")
        raise FileNotFoundError("配置文件 config.ini 不存在")
    
    config.read('config.ini')
    return config

# 全局配置对象
try:
    config = load_config()

    # 从配置文件读取LLM API调用顺序
    LLM_API_PROVIDERS = config.get('API', 'LLM_API_PROVIDERS', fallback='deepseek,ppinfra,local').split(',')

    # Binance API端点
    SPOT_BASE_URL = "https://api.binance.com/api/v3"
    FUTURES_BASE_URL = "https://fapi.binance.com/fapi/v1"

    # Binance API密钥
    BINANCE_API_KEY = config.get('API', 'BINANCE_API_KEY')
    BINANCE_API_SECRET = config.get('API', 'BINANCE_API_SECRET')

    # 固定交易对
    SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SUIUSDT', 'TONUSDT', 'PNUTUSDT']
    
except Exception as e:
    logger.error(f"加载配置失败: {e}")
    raise 