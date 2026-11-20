"""PluginContext 协议中立快捷动作 / 身份判断 / 群级插件开关（自 ctx.py 剥离的 mixin）"""
import logging

logger = logging.getLogger('zcbot')

class PluginMessagingMixin:
    """协议中立快捷动作 / 身份判断 / 群级插件开关"""

    # 以下方法依赖 PluginContext.__init__ 建立的实例字段
    # 动作经 self.actions（ctx.actions）转发到当前接入端，不绑定具体协议

    def _bb_send(self, user_id, group_id, message, bot):
        """黑匣子：记录发送动作（send）—— 事件流第 15 步，记录准备发给谁。"""
        try:
            from framework.runtime import current_event_id_var as _eid_var
            _eid = _eid_var.get()
        except Exception as _exc:
            logger.debug('[_bb_send] 捕获异常后按降级路径继续: %s', _exc)
            _eid = None
        _to = (f"群{group_id}" if group_id else f"私聊{user_id}")
        from framework.blackbox import record as _bb_record
        _bb_record('send', plugin=self._plugin_name, fn='send_msg',
                   event_id=_eid, step=15, to=_to, adapter=bot or '',
                   msg=f"发送({len(message) if message else 0}字)")

    def _bb_send_fail(self, user_id, group_id, bot, err):
        """黑匣子：发送动作实际失败（send_fail）—— 记录发给谁但失败，便于排障。"""
        try:
            from framework.runtime import current_event_id_var as _eid_var
            _eid = _eid_var.get()
        except Exception as _exc:
            logger.debug('[_bb_send_fail] 捕获异常后按降级路径继续: %s', _exc)
            _eid = None
        _to = (f"群{group_id}" if group_id else f"私聊{user_id}")
        from framework.blackbox import record as _bb_record
        _bb_record('send_fail', plugin=self._plugin_name, fn='send_msg',
                   event_id=_eid, to=_to, adapter=bot or '', level='warn',
                   msg=f"发送失败({type(err).__name__})")

    # ---- 最常用 API 快捷方法（省得每次都拼 params）----

    def send_msg(self, user_id: int = None, group_id: int = None,
                 message=None, auto_escape: bool = False, bot: str = None):
        """快捷发送消息，支持通过 user_id/group_id 自动判断私聊/群聊（同步桥接）"""
        # 未指定 bot 时，自动使用当前消息来源的 bot（由 router 注入 _current_bot）
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        self._bb_send(user_id, group_id, message, bot)
        try:
            return self.actions.send_msg(
                user_id=user_id, group_id=group_id,
                message=message, auto_escape=auto_escape, bot=bot
            )
        except Exception as e:
            # 黑匣子：发送实际失败（适配器宕机 / 无可用接入端 / API 异常）
            self._bb_send_fail(user_id, group_id, bot, e)
            raise

    async def asend_msg(self, user_id: int = None, group_id: int = None,
                        message=None, auto_escape: bool = False, bot: str = None):
        """异步快捷发送消息（推荐 async handler 使用，不阻塞事件循环）"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        self._bb_send(user_id, group_id, message, bot)
        try:
            return await self.actions.acall(
                'send_msg', user_id=user_id, group_id=group_id,
                message=message, auto_escape=auto_escape, bot=bot
            )
        except Exception as e:
            # 黑匣子：发送实际失败（适配器宕机 / 无可用接入端 / API 异常）
            self._bb_send_fail(user_id, group_id, bot, e)
            raise

    def recall(self, message_id, bot: str = None):
        """
        撤回消息（底层 `delete_msg`）。

        平台侧一般只允许撤回自己发出的消息，且有时限（QQ 约 2 分钟）；
        超时或无权撤回时返回 `retcode=-1`，不抛异常。
        """
        act = self.actions
        # onebot_api 上有显式的 delete_msg；ActionProxy 只有 __getattr__ 动态
        # 生成的同名方法，签名是 (bot=None, **params)。若对代理用位置传参，
        # message_id 会被当成 bot 发出去——静默发错目标，比报错更难查。
        if hasattr(type(act), 'delete_msg'):
            return act.delete_msg(message_id, bot=bot)
        return act.call('delete_msg', bot=bot, message_id=message_id)

    async def arecall(self, message_id, bot: str = None):
        """异步撤回消息（async handler 推荐，不阻塞事件循环）"""
        # 两种动作面的 acall 签名一致（action, bot=None, **params），可统一走
        return await self.actions.acall('delete_msg', bot=bot, message_id=message_id)

    def download_media(self, segment, dest_dir: str = None, filename: str = None,
                       max_bytes: int = None, timeout: float = 30,
                       bot: str = None, expected_type: str = None,
                       resolver=None) -> dict:
        """
        把消息里的媒体落到本地，返回 `{'status','path','size','ref','msg',
        'kind','mime','rejected_reason'}`。

        `segment` 可以是规范段（`ev.first_image` 之类）或裸引用字符串。
        支持 http(s) / base64 / file:// / 本地路径；`file_id` 会交给接入端
        换取真实地址（拿不到就是拿不到，不猜）。

        落盘规则：先按引用种类预检长度，再流式写入 `.part`；图片段在转正前
        过统一内容闸门，URL 段逐跳复查目标。任何一步失败都删掉半成品。
        超限返回 `status='failed'`，内容/URL 被拒返回 `status='rejected'`。

        `expected_type` 可显式声明段类型，不传时按段自身的 type 推断；
        `resolver` 是 URL 闸门的可注入 DNS 解析器。两者都放在末尾，原有
        位置参数调用不变。
        """
        from framework.messaging.media_io import (
            DEFAULT_MAX_BYTES,
            segment_file_id,
        )
        from framework.messaging.media_io import (
            download as _download,
        )

        try:
            limit = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
        except (TypeError, ValueError):
            return {'status': 'failed', 'path': '', 'size': 0, 'ref': '',
                    'msg': f'max_bytes 不是整数: {max_bytes!r}',
                    'kind': '', 'mime': '', 'rejected_reason': ''}

        # file_id 换地址后是按裸字符串再次落盘，若不在这里固定段类型，
        # 第二次调用会丢失 image 语义、绕过内容闸门。
        derived_type = expected_type
        if derived_type is None and isinstance(segment, dict):
            from framework.messaging.segments import canonical_type
            derived_type = canonical_type(segment.get('type'))
        opts = {'dest_dir': dest_dir, 'filename': filename,
                'max_bytes': limit, 'timeout': timeout,
                'expected_type': derived_type, 'resolver': resolver}
        res = _download(segment, **opts)
        if res.get('status') != 'needs_adapter':
            return res

        # file_id：向接入端换一次真实地址。只换一次，不递归追问——
        # 若换回来的还是 file_id，说明这个接入端没有可用的下载动作。
        file_id = segment_file_id(segment)
        if not file_id:
            return {**res, 'status': 'unsupported',
                    'msg': '该段只有 file_id / file_info，但取不出可用标识'}
        act = self.actions
        for action, params in (('get_image', {'file': file_id}),
                               ('get_record', {'file': file_id, 'out_format': 'mp3'})):
            try:
                raw = getattr(act, action)(bot=bot, **params)
            except Exception as e:
                logger.debug('[download_media] %s 不可用: %s', action, e)
                continue
            real = self._media_ref_from_action(raw)
            if real:
                again = _download(real, **opts)
                if again.get('status') != 'needs_adapter':
                    return again
                break
        return {**res, 'status': 'unsupported',
                'msg': '该接入端未能把 file_id 换成可下载地址'}

    async def adownload_media(self, segment, dest_dir: str = None,
                              filename: str = None, max_bytes: int = None,
                              timeout: float = 30, bot: str = None,
                              expected_type: str = None,
                              resolver=None) -> dict:
        """
        `download_media` 的异步版本。

        下载是网络 IO，同步版本会占住事件循环——定时任务和别的插件都得等
        它下完。async handler 里请用这个。新增参数与同步版本保持一致。
        """
        import asyncio
        return await asyncio.to_thread(
            self.download_media, segment, dest_dir=dest_dir, filename=filename,
            max_bytes=max_bytes, timeout=timeout, bot=bot,
            expected_type=expected_type, resolver=resolver)

    @staticmethod
    def _media_ref_from_action(raw):
        """从接入端动作返回值里挖出可下载的地址（各端字段不一致）。"""
        if not isinstance(raw, dict):
            return ''
        data = raw.get('data')
        if not isinstance(data, dict):
            return ''
        for key in ('file', 'url', 'path'):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ''

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
        except Exception as _exc:
            logger.debug('[is_group_admin] 捕获异常后按降级路径继续: %s', _exc)
            return False

    def is_group_owner(self, group_id: int, user_id: int) -> bool:
        """判断用户是否为群主"""
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] == 'owner'
        except Exception as _exc:
            logger.debug('[is_group_owner] 捕获异常后按降级路径继续: %s', _exc)
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
        except Exception as _exc:
            logger.debug('[is_superuser] 捕获异常后按降级路径继续: %s', _exc)
            return False

    def is_blacklisted(self, user_id: int) -> bool:
        """判断用户是否在黑名单中"""
        try:
            row = self._db.query_one(
                "SELECT is_blacklist FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('is_blacklist') == 1
        except Exception as _exc:
            logger.debug('[is_blacklisted] 捕获异常后按降级路径继续: %s', _exc)
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
