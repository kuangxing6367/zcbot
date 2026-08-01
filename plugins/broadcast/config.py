# config.py
from __future__ import annotations

import random

TargetType = str  # "group" | "friend"


class PluginConfig:
    """
    插件配置管理（适配目标框架）

    使用 ctx.get_config() / ctx.set_config() 读写配置。
    """

    def __init__(self, ctx):
        self.ctx = ctx

    # ========================
    # 读取配置
    # ========================

    @property
    def broadcast_max_delay(self) -> float:
        return float(self.ctx.get_config("broadcast_max_delay", 1.1))

    @property
    def skip_source(self) -> bool:
        return bool(self.ctx.get_config("skip_source", True))

    def get_broadcast_delay(self) -> float:
        return random.uniform(0, self.broadcast_max_delay)

    # ========================
    # 禁用列表管理
    # ========================

    def disabled_list(self, is_group: bool = True) -> list[str]:
        if is_group:
            return list(self.ctx.get_config("disable_gids", []))
        return list(self.ctx.get_config("disable_uids", []))

    def is_disabled(self, target_id: str, is_group: bool = True) -> bool:
        return target_id in self.disabled_list(is_group)

    def filter_broadcastable(self, ids: list[str], is_group: bool = True) -> list[str]:
        disabled = self.disabled_list(is_group)
        return [target_id for target_id in ids if target_id not in disabled]

    # ========================
    # 修改禁用列表
    # ========================

    def enable_target(self, target_id: str, is_group: bool = True):
        key = "disable_gids" if is_group else "disable_uids"
        disabled = list(self.ctx.get_config(key, []))
        if target_id in disabled:
            disabled.remove(target_id)
            self.ctx.set_config(key, disabled)

    def disable_target(self, target_id: str, is_group: bool = True):
        key = "disable_gids" if is_group else "disable_uids"
        disabled = list(self.ctx.get_config(key, []))
        if target_id not in disabled:
            disabled.append(target_id)
            self.ctx.set_config(key, disabled)
            return True
        return False