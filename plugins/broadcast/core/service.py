import random
import time
from dataclasses import dataclass, field

from plugins.broadcast.core.model import BroadcastScope
from plugins.broadcast.core.state import BroadcastState, TargetType


# =========================
# 结果对象
# =========================


@dataclass(slots=True)
class BroadcastResult:
    success_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def success_count(self) -> int:
        return len(self.success_ids)

    @property
    def failed_count(self) -> int:
        return len(self.failed_ids)

    @property
    def total(self) -> int:
        return self.success_count + self.failed_count


# =========================
# 广播服务
# =========================


class BroadcastCancelledError(Exception):
    """广播被取消"""
    pass


class BroadcastService:
    """
    广播服务（单实例单任务，同步版本）

    适配目标框架：
      - 使用 ctx.api() 代替 aiocqhttp CQHttp
      - 使用 time.sleep() 代替 asyncio.sleep()
      - 使用 ctx.get_config() 代替 AstrBotConfig
    """

    def __init__(self, ctx, state: BroadcastState):
        self.ctx = ctx
        self.state = state
        self._cancel_flag = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_flag

    def cancel(self):
        """请求取消当前广播"""
        self._cancel_flag = True

    def reset_cancel(self):
        """重置取消标志"""
        self._cancel_flag = False

    # ========================
    # 目标解析
    # ========================

    def _get_targets(self, t: TargetType) -> list[str]:
        api = self.ctx.api()
        if t == "group":
            groups = api.get_group_list()
            ids = [str(g["group_id"]) for g in groups]
        else:
            friends = api.get_friend_list()
            ids = [str(f["user_id"]) for f in friends]

        return self.state.filter_broadcastable(t, ids)

    def _scope_to_targets(self, scope: BroadcastScope) -> list[TargetType]:
        if scope == BroadcastScope.GROUP:
            return ["group"]
        if scope == BroadcastScope.FRIEND:
            return ["friend"]
        return ["group", "friend"]

    # ========================
    # 执行广播
    # ========================

    def broadcast(
        self,
        message_id: str | int,
        scope: BroadcastScope,
    ) -> BroadcastResult:
        result = BroadcastResult()
        api = self.ctx.api()

        try:
            for t in self._scope_to_targets(scope):
                ids = self._get_targets(t)

                for id_ in ids:
                    if self._cancel_flag:
                        raise BroadcastCancelledError()

                    time.sleep(
                        random.uniform(0, self.ctx.get_config("broadcast_max_delay", 1.1))
                    )

                    try:
                        self._send_single(api, t, id_, message_id)
                        result.success_ids.append(f"{t}:{id_}")

                    except BroadcastCancelledError:
                        raise

                    except Exception as e:
                        result.failed_ids.append(f"{t}:{id_}")
                        self.ctx.logger.warning(f"{t} {id_} 广播失败: {e}")

        except BroadcastCancelledError:
            self.ctx.logger.info("广播任务被取消")
            result.cancelled = True

        return result

    # ========================
    # 发送封装
    # ========================

    def _send_single(
        self,
        api,
        t: TargetType,
        id_: str,
        message_id: str | int,
    ) -> None:
        if t == "group":
            api.forward_group_single_msg(
                group_id=int(id_),
                message_id=message_id,
            )
        else:
            api.forward_friend_single_msg(
                user_id=int(id_),
                message_id=message_id,
            )