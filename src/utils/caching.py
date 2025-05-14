"""数据缓存模块

提供数据缓存和加载功能。
"""
import pickle
import logging
from typing import Any, Optional

# 配置日志
logger = logging.getLogger(__name__)


def cache_data(data: Any, filename: str) -> bool:
    """缓存数据到文件
    
    Args:
        data: 要缓存的数据
        filename: 缓存文件路径
        
    Returns:
        缓存是否成功
    """
    try:
        with open(filename, 'wb') as f:
            pickle.dump(data, f)
        logger.info(f"数据已缓存到文件: {filename}")
        return True
    except Exception as e:
        logger.error(f"缓存数据到{filename}失败: {e}")
        return False


def load_cached_data(filename: str) -> Optional[Any]:
    """从文件加载缓存数据
    
    Args:
        filename: 缓存文件路径
        
    Returns:
        缓存的数据，如果加载失败则返回None
    """
    try:
        with open(filename, 'rb') as f:
            data = pickle.load(f)
        logger.info(f"已从文件加载缓存数据: {filename}")
        return data
    except FileNotFoundError:
        logger.warning(f"缓存文件不存在: {filename}")
        return None
    except Exception as e:
        logger.error(f"加载缓存数据从{filename}失败: {e}")
        return None 