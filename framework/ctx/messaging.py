# -*- coding: utf-8 -*-
"""PluginContext 协议中立快捷动作 / 身份判断 / 群级插件开关（自 ctx.py 剥离的 mixin）"""

class PluginMessagingMixin:
    """协议中立快捷动作 / 身份判断 / 群级插件开关"""

    # 以下方法依赖 PluginContext.__init__ 建立的实例字段
    # 动作经 self.actions（ctx.actions）转发到当前接入端，不绑定具体协议

    # ---- 最常用 API 快捷方法（省得每次都拼 params）----

    def send_msg(self, user_id: int = None, group_id: int = None,
                 message=None, auto_escape: bool = False, bot: str = None):
        """快捷发送消息，支持通过 user_id/group_id 自动判断私聊/群聊（同步桥接）"""
        # 未指定 bot 时，自动使用当前消息来源的 bot（由 router 注入 _current_bot）
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return self.actions.send_msg(
            user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    async def asend_msg(self, user_id: int = None, group_id: int = None,
                        message=None, auto_escape: bool = False, bot: str = None):
        """异步快捷发送消息（推荐 async handler 使用，不阻塞事件循环）"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return await self.actions.acall(
            'send_msg', user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    def ban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """快捷禁言群成员（duration=0 解禁）（同步桥接）"""
        return self.actions.set_group_ban(group_id, user_id, duration=duration, bot=bot)

    async def aban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """异步快捷禁言群成员"""
        return await self.actions.acall(
            'set_group_ban', group_id=group_id, user_id=user_id,
            duration=duration, bot=bot
        )

    def kick(self, group_id: int, user_id: int, reject_add_request: bool = False, bot: str = None):
        """快捷踢出群成员（同步桥接）"""
        return self.actions.set_group_kick(group_id, user_id, reject_add_request=reject_add_request, bot=bot)

    async def akick(self, group_id: int, user_id: int,
                    reject_add_request: bool = False, bot: str = None):
        """异步快捷踢出群成员"""
        return await self.actions.acall(
            'set_group_kick', group_id=group_id, user_id=user_id,
            reject_add_request=reject_add_request, bot=bot
        )

    def mute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """快捷全员禁言/解禁（同步桥接）"""
        return self.actions.set_group_whole_ban(group_id, enable=enable, bot=bot)

    async def amute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """异步快捷全员禁言/解禁"""
        return await self.actions.acall(
            'set_group_whole_ban', group_id=group_id, enable=enable, bot=bot
        )

    def set_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """快捷设置群名片（空字符串清除名片）（同步桥接）"""
        return self.actions.set_group_card(group_id, user_id, card=card, bot=bot)

    async def aset_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """异步快捷设置群名片"""
        return await self.actions.acall(
            'set_group_card', group_id=group_id, user_id=user_id, card=card, bot=bot
        )

    def get_member_list(self, group_id: int, bot: str = None):
        """快捷获取群成员列表（同步桥接）"""
        return self.actions.get_group_member_list(group_id=group_id, bot=bot)

    async def aget_member_list(self, group_id: int, bot: str = None):
        """异步快捷获取群成员列表"""
        return await self.actions.acall('get_group_member_list', group_id=group_id, bot=bot)

    def get_member_info(self, group_id: int, user_id: int, bot: str = None):
        """快捷获取群成员信息（同步桥接）"""
        return self.actions.get_group_member_info(group_id, user_id, bot=bot)

    async def aget_member_info(self, group_id: int, user_id: int, bot: str = None):
        """异步快捷获取群成员信息"""
        return await self.actions.acall(
            'get_group_member_info', group_id=group_id, user_id=user_id, bot=bot
        )

    # ---- 权限判断快捷方法 ----

    def is_group_admin(self, group_id: int, user_id: int) -> bool:
        """
        判断用户是否为群管理员或群主
        :return: True=管理员/群主, False=普通成员或不存在
        """
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] in ('owner', 'admin')
        except Exception:
            return False

    def is_group_owner(self, group_id: int, user_id: int) -> bool:
        """判断用户是否为群主"""
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] == 'owner'
        except Exception:
            return False

    def is_superuser(self, user_id: int) -> bool:
        """
        判断用户是否为框架超管（从 users 表的 role 字段判断）
        超管在 config.yaml 中配置，自动同步到 users.role
        """
        try:
            row = self._db.query_one(
                "SELECT role FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('role') == 'super'
        except Exception:
            return False

    def is_blacklisted(self, user_id: int) -> bool:
        """判断用户是否在黑名单中"""
        try:
            row = self._db.query_one(
                "SELECT is_blacklist FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('is_blacklist') == 1
        except Exception:
            return False

    def get_user_role(self, group_id: int, user_id: int) -> str:
        """
        获取用户在群内的完整身份
        :return: "super"（超管）> "owner"（群主）> "admin"（管理员）> "member"（成员）> "blacklist"（黑名单）
        """
        if self.is_superuser(user_id):
            return "super"
        if self.is_blacklisted(user_id):
            return "blacklist"
        if self.is_group_owner(group_id, user_id):
            return "owner"
        if self.is_group_admin(group_id, user_id):
            return "admin"
        return "member"

    # ---- 群级插件开关 ----

    def enable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群启用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, True)

    def disable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群禁用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, False)

    def is_plugin_enabled_in_group(self, plugin_name: str, group_id: int) -> bool:
        """检查插件在指定群是否启用"""
        return self._framework.plugin_loader.is_plugin_enabled_for_group(plugin_name, group_id)

    def get_plugin_status_list(self, group_id: int) -> dict:
        """获取指定群所有插件的启用状态"""
        settings = self._framework.plugin_loader.get_group_plugin_settings(group_id)
        disabled_plugins = {r['plugin_name'] for r in settings if not r['enabled']}
        result = {}
        for name in self._framework.plugin_loader.get_loaded_plugins():
            result[name] = name not in disabled_plugins
        return result
