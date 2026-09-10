# -*- coding: utf-8 -*-
"""
ApiContext —— Web 应用层的共享上下文

create_web_app 原来把所有共享状态（app / framework / db / plugins_dir /
config / dual_auth / 限速表 等）锁死在闭包里，导致无法拆分、插件无法插入。
这里用统一上下文对象承载这些状态，供各功能域路由模块（auth.py / plugins.py
/ commands.py / db_gateway.py 等，随 S4 迁移）注册时读取。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ApiContext:
    """Web 应用共享上下文（由 create_web_app 填充）"""

    app: Any = None                    # Flask app
    framework: Any = None              # Framework 实例
    db: Any = None                     # Database 实例
    config: Dict = field(default_factory=dict)  # framework.config
    plugins_dir: str = ''
    web_cfg: Dict = field(default_factory=dict)
    # 鉴权 / 安全（由 create_web_app 注入，供自定义路由复用）
    require_auth: Optional[Callable] = None
    require_super: Optional[Callable] = None
    # 限速 / 登录防爆破（S4 迁移时使用）
    login_failures: Dict = field(default_factory=dict)
    rate_buckets: Dict = field(default_factory=dict)
    # 任意插件/模块扩展挂载点（避免为每个新状态改构造）
    extras: Dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        """从 framework.config 取配置"""
        return self.config.get(key, default)

    def service(self, name: str, default=None):
        """从框架服务注册表取服务"""
        try:
            return self.framework.services.get(name)
        except Exception:
            return default
