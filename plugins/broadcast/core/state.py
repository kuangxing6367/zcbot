from collections.abc import Iterable
from typing import Literal

TargetType = Literal["group", "friend"]


class BroadcastState:
    """
    广播状态管理（群聊 / 私聊结构对齐）

    适配目标框架：使用 ctx.get_config() / ctx.set_config() 读写配置
    """

    def __init__(self, ctx):
        self.ctx = ctx

        self._disable: dict[TargetType, list[str]] = {
            "group": list(ctx.get_config("disable_gids", [])),
            "friend": list(ctx.get_config("disable_uids", [])),
        }

    # =========================
    # 通用查询
    # =========================

    def is_disabled(self, t: TargetType, id_: str) -> bool:
        return id_ in self._disable[t]

    def filter_broadcastable(self, t: TargetType, ids: Iterable[str]) -> list[str]:
        return [i for i in ids if not self.is_disabled(t, i)]

    # =========================
    # 人工策略
    # =========================

    def enable(self, t: TargetType, id_: str) -> bool:
        if id_ in self._disable[t]:
            self._disable[t].remove(id_)
            self._save()
            return True
        return False

    def disable(self, t: TargetType, id_: str) -> bool:
        if id_ not in self._disable[t]:
            self._disable[t].append(id_)
            self._save()
            return True
        return False

    # =========================
    # 持久化
    # =========================

    def _save(self) -> None:
        self.ctx.set_config("disable_gids", self._disable["group"])
        self.ctx.set_config("disable_uids", self._disable["friend"])

    # =========================
    # 只读视图（防误改）
    # =========================

    def disabled_ids(self, t: TargetType) -> tuple[str, ...]:
        return tuple(self._disable[t])