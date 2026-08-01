"""
权限管理模块 - 定义用户权限等级和权限检查机制
从 AstrBot 迁移至 zgric_onebot11，适配新框架 API
"""
import asyncio
import functools
import inspect
import logging
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class PermLevel(IntEnum):
    """定义用户的权限等级。数字越小，权限越高。"""
    SUPERUSER = 0
    OWNER = 1
    ADMIN = 2
    MEMBER = 3
    UNKNOWN = 4

    def __str__(self):
        return {
            PermLevel.SUPERUSER: "超管",
            PermLevel.OWNER: "群主",
            PermLevel.ADMIN: "管理员",
            PermLevel.MEMBER: "成员",
        }.get(self, "未知")

    @classmethod
    def from_str(cls, perm_str: str):
        return {
            "超管": cls.SUPERUSER,
            "群主": cls.OWNER,
            "管理员": cls.ADMIN,
            "成员": cls.MEMBER,
        }.get(perm_str, cls.UNKNOWN)


class PermissionManager:
    _instance: Optional["PermissionManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        superusers: Optional[List[str]] = None,
        perms: Optional[Dict[str, str]] = None,
        recorder_instance=None,
        ctx=None,
    ):
        if self._initialized:
            return
        self.superusers = superusers or []
        if perms is None:
            raise ValueError("初始化必须传入 perms")
        self.perms: Dict[str, PermLevel] = {
            k: PermLevel.from_str(v) for k, v in perms.items()
        }
        self.recorder = recorder_instance
        self.ctx = ctx
        self._initialized = True

    @classmethod
    def get_instance(
        cls,
        superusers: Optional[List[str]] = None,
        perms: Optional[Dict[str, str]] = None,
        recorder_instance=None,
        ctx=None,
    ) -> "PermissionManager":
        if cls._instance is None:
            cls._instance = cls(
                superusers=superusers,
                perms=perms,
                recorder_instance=recorder_instance,
                ctx=ctx,
            )
        return cls._instance

    async def get_perm_level(self, event, user_id: str) -> PermLevel:
        """获取用户在事件上下文中的权限等级"""
        user_id = str(user_id)
        group_id = getattr(event, 'group_id', None)
        if not group_id or not user_id:
            return PermLevel.MEMBER

        # 1. 检查是否为超级用户
        if user_id in self.superusers:
            return PermLevel.SUPERUSER

        # 2. 检查原生权限（群主/管理员）
        try:
            if self.ctx and hasattr(self.ctx, 'api'):
                api = self.ctx.api()
                info = await api.get_group_member_info(
                    group_id=int(group_id), user_id=int(user_id), no_cache=True
                )
                role = info.get("role", "unknown")
                if role == "owner":
                    return PermLevel.OWNER
                if role == "admin":
                    return PermLevel.ADMIN
        except Exception:
            logger.warning(f"无法获取用户 {user_id} 在群 {group_id} 的原生权限信息。")

        # 3. 检查是否为插件数据库中手动设置的管理员
        if self.recorder and await self.recorder.is_plugin_group_admin(
            group_id, user_id
        ):
            return PermLevel.ADMIN

        # 4. 如果以上都不是，则为普通成员
        return PermLevel.MEMBER

    async def perm_block(self, event, perm_key: str) -> Optional[str]:
        """检查用户是否有权限执行某个操作，返回 None 表示有权限，返回字符串表示拒绝原因"""
        user_level = await self.get_perm_level(event, user_id=getattr(event, 'user_id', ''))
        required_level = self.perms.get(perm_key)

        if required_level is None:
            return None

        if user_level > required_level:
            return f"您的权限（{user_level}）不足以使用此指令（需要：{required_level}）"

        return None


def perm_required(perm_key: Optional[str] = None):
    """权限检查装饰器。

    包装一个 handler(event, match) 函数，在执行前检查权限。
    支持同步和异步 handler。
    """
    def decorator(func):
        actual_perm_key = perm_key or func.__name__

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(event, match=None):
                perm_manager = PermissionManager.get_instance()
                if not perm_manager._initialized:
                    logger.error(f"PermissionManager 未初始化（尝试访问权限项：{actual_perm_key}）")
                    _send_reply(event, "内部错误：权限系统未正确加载")
                    return

                result = await perm_manager.perm_block(event, perm_key=actual_perm_key)
                if result:
                    _send_reply(event, result)
                    return

                return await func(event, match)
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(event, match=None):
                perm_manager = PermissionManager.get_instance()
                if not perm_manager._initialized:
                    logger.error(f"PermissionManager 未初始化（尝试访问权限项：{actual_perm_key}）")
                    _send_reply(event, "内部错误：权限系统未正确加载")
                    return

                # 同步 wrapper 中无法直接 await，使用 asyncio.run
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 如果在运行的事件循环中，创建任务
                        async def check_and_call():
                            result = await perm_manager.perm_block(event, perm_key=actual_perm_key)
                            if result:
                                _send_reply(event, result)
                                return
                            return func(event, match)
                        return asyncio.ensure_future(check_and_call())
                    else:
                        result = loop.run_until_complete(
                            perm_manager.perm_block(event, perm_key=actual_perm_key)
                        )
                        if result:
                            _send_reply(event, result)
                            return
                        return func(event, match)
                except Exception:
                    # 兜底：直接调用
                    return func(event, match)
            return sync_wrapper
    return decorator


def _send_reply(event, text: str):
    """发送回复消息"""
    # 从 event 中获取 ctx（如果可用）
    ctx = getattr(event, '_ctx', None)
    if ctx is not None:
        ctx.send_msg(
            user_id=getattr(event, 'user_id', None),
            group_id=getattr(event, 'group_id', None) if getattr(event, 'is_group', False) else None,
            message=text,
        )