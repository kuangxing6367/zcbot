"""
插件配置模块
从 zgric 框架的 plugin_configs 表中读取配置
同时管理插件数据目录
"""
import os
from pathlib import Path
from typing import Any


PLUGIN_NAME = "apis"


class PluginConfig:
    """插件配置，适配 zgric 框架的 ctx.get_config() 模式"""

    def __init__(self, plugin_dir: str | Path, data_dir: str | Path, ctx=None):
        self.plugin_dir = Path(plugin_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self._ctx = ctx

        # 创建数据目录
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.pool_files_dir.mkdir(parents=True, exist_ok=True)

        # 默认配置值
        self.need_prefix = False
        self.save_data = True
        self.use_local = True
        self.admin_ids: list[str] = []

        # 从 ctx 加载配置（如果可用）
        if ctx is not None:
            self._load_from_ctx()

    def _load_from_ctx(self):
        """从框架配置系统加载配置"""
        self.need_prefix = self._ctx.get_config("need_prefix", False)
        self.save_data = self._ctx.get_config("save_data", True)
        self.use_local = self._ctx.get_config("use_local", True)

    @property
    def local_dir(self) -> Path:
        return self.data_dir / "local"

    @property
    def pool_files_dir(self) -> Path:
        return self.data_dir / "pool_files"

    @property
    def presets_dir(self) -> Path:
        return self.plugin_dir / "presets"

    @property
    def api_pool_file(self) -> Path:
        return self.presets_dir / "api_pool_default.json"

    @property
    def site_pool_file(self) -> Path:
        return self.presets_dir / "site_pool_default.json"

    @property
    def dashboard_dir(self) -> Path:
        return self.plugin_dir / "pages" / "dashboard"

    @property
    def dashboard_assets_dir(self) -> Path:
        return self.dashboard_dir / "assets"

    @property
    def logo_path(self) -> Path:
        return self.dashboard_assets_dir / "images" / "logo.png"