"""
共享状态模块 - 存储整个插件运行时的全局单例引用
"""
from typing import Optional, Dict, Any

# 这些变量在 register() 中被初始化
_api_client = None
_meme_manager = None
_recorder = None
_ctx = None
_config: Dict[str, Any] = {}