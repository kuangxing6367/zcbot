"""
OneBot 11 标准 API 封装（属于 onebot_adapter 协议插件，不属于框架核心）
完整覆盖 38 个标准 Action，提供类型提示；非标准/扩展动作通过 __getattr__ 动态转发，
同时保留 acall() 异步入口与 ctx.api() 通用兜底。
"""
import logging

logger = logging.getLogger('zcbot')


class OneBotAPI:
    """
    OneBot 11 标准 API 调用封装（由 onebot_adapter 注册为 services['onebot_api']）

    所有方法的 bot 参数用于指定 OneBot 客户端实例（多账号场景），
    为 None 时调用默认实例。

    调用示例：
        ctx.onebot.send_private_msg(user_id=123456, message="你好")
        ctx.onebot.set_group_ban(group_id=123456, user_id=789012, duration=600)
        ctx.onebot.get_group_member_list(group_id=123456)

    非标准/扩展 API：可直接 ctx.onebot.<扩展动作名>(**params)（动态转发），
    或使用 ctx.api(action, **params) 兜底。
    """

    def __init__(self, caller, default_bot=None):
        self._caller = caller
        self._default_bot = default_bot

    def _call(self, action: str, bot=None, **params):
        """底层调用转发（同步桥接）"""
        return self._caller.call(action, bot=bot or self._default_bot, **params)

    async def acall(self, action: str, bot=None, **params):
        """底层异步调用转发（不阻塞事件循环，async handler 推荐）"""
        return await self._caller.acall(action, bot=bot or self._default_bot, **params)

    def __getattr__(self, action: str):
        """扩展/非标准动作兜底：任意未显式定义的动作名动态转发为同步调用"""
        def _method(bot=None, **params):
            return self._caller.call(action, bot=bot or self._default_bot, **params)
        _method.__name__ = action
        return _method

    # ====================================================================
    #  1. 消息类 API
    # ====================================================================

    def send_private_msg(self, user_id: int, message, auto_escape: bool = False, bot=None) -> dict:
        """发送私聊消息"""
        return self._call("send_private_msg", bot=bot,
                          user_id=user_id, message=message, auto_escape=auto_escape)

    def send_group_msg(self, group_id: int, message, auto_escape: bool = False, bot=None) -> dict:
        """发送群聊消息"""
        return self._call("send_group_msg", bot=bot,
                          group_id=group_id, message=message, auto_escape=auto_escape)

    def send_msg(self, message_type: str = None, user_id: int = None,
                 group_id: int = None, message=None, auto_escape: bool = False, bot=None) -> dict:
        """
        发送消息：有 group_id 走群、否则走私聊（与历史运行时行为一致）。
        message_type 仅为兼容旧签名保留。
        """
        if group_id:
            return self._call("send_group_msg", bot=bot,
                              group_id=group_id, message=message, auto_escape=auto_escape)
        return self._call("send_private_msg", bot=bot,
                          user_id=user_id, message=message, auto_escape=auto_escape)

    def delete_msg(self, message_id: int, bot=None) -> dict:
        """撤回消息"""
        return self._call("delete_msg", bot=bot, message_id=message_id)

    def get_msg(self, message_id: int, bot=None) -> dict:
        """获取消息详情"""
        return self._call("get_msg", bot=bot, message_id=message_id)

    def get_forward_msg(self, message_id: str, bot=None) -> dict:
        """获取合并转发消息内容"""
        return self._call("get_forward_msg", bot=bot, message_id=message_id)

    def send_like(self, user_id: int, times: int = 1, bot=None) -> dict:
        """发送好友赞"""
        return self._call("send_like", bot=bot, user_id=user_id, times=times)

    def mark_msg_as_read(self, message_id: int, bot=None) -> dict:
        """标记消息已读"""
        return self._call("mark_msg_as_read", bot=bot, message_id=message_id)

    # ====================================================================
    #  2. 群管理类 API
    # ====================================================================

    def set_group_kick(self, group_id: int, user_id: int,
                       reject_add_request: bool = False, bot=None) -> dict:
        """踢出群成员"""
        return self._call("set_group_kick", bot=bot,
                          group_id=group_id, user_id=user_id,
                          reject_add_request=reject_add_request)

    def set_group_ban(self, group_id: int, user_id: int,
                      duration: int = 1800, bot=None) -> dict:
        """禁言群成员（duration=0 解除禁言）"""
        return self._call("set_group_ban", bot=bot,
                          group_id=group_id, user_id=user_id, duration=duration)

    def set_group_anonymous_ban(self, group_id: int, anonymous_flag: str,
                                duration: int = 1800, bot=None) -> dict:
        """禁言匿名用户"""
        return self._call("set_group_anonymous_ban", bot=bot,
                          group_id=group_id, anonymous_flag=anonymous_flag,
                          duration=duration)

    def set_group_whole_ban(self, group_id: int, enable: bool = True, bot=None) -> dict:
        """全员禁言"""
        return self._call("set_group_whole_ban", bot=bot,
                          group_id=group_id, enable=enable)

    def set_group_admin(self, group_id: int, user_id: int,
                        enable: bool = True, bot=None) -> dict:
        """设置/取消群管理员"""
        return self._call("set_group_admin", bot=bot,
                          group_id=group_id, user_id=user_id, enable=enable)

    def set_group_card(self, group_id: int, user_id: int,
                       card: str, bot=None) -> dict:
        """设置群名片（card 为空字符串则清除名片）"""
        return self._call("set_group_card", bot=bot,
                          group_id=group_id, user_id=user_id, card=card)

    def set_group_name(self, group_id: int, group_name: str, bot=None) -> dict:
        """设置群名"""
        return self._call("set_group_name", bot=bot,
                          group_id=group_id, group_name=group_name)

    def set_group_special_title(self, group_id: int, user_id: int,
                                special_title: str = "", duration: int = -1, bot=None) -> dict:
        """设置群头衔"""
        return self._call("set_group_special_title", bot=bot,
                          group_id=group_id, user_id=user_id,
                          special_title=special_title, duration=duration)

    def set_group_leave(self, group_id: int, is_dismiss: bool = False, bot=None) -> dict:
        """退群"""
        return self._call("set_group_leave", bot=bot,
                          group_id=group_id, is_dismiss=is_dismiss)

    def set_group_anonymous(self, group_id: int, enable: bool = True, bot=None) -> dict:
        """开启/关闭匿名聊天"""
        return self._call("set_group_anonymous", bot=bot,
                          group_id=group_id, enable=enable)

    # ====================================================================
    #  3. 请求处理类 API
    # ====================================================================

    def set_friend_add_request(self, flag: str, approve: bool = True,
                               remark: str = None, bot=None) -> dict:
        """处理好友添加请求"""
        params = {"flag": flag, "approve": approve}
        if remark is not None:
            params["remark"] = remark
        return self._call("set_friend_add_request", bot=bot, **params)

    def set_group_add_request(self, flag: str, sub_type: str, approve: bool = True,
                              reason: str = None, bot=None) -> dict:
        """处理群添加请求/邀请（sub_type: add/invite）"""
        params = {"flag": flag, "sub_type": sub_type, "approve": approve}
        if reason is not None:
            params["reason"] = reason
        return self._call("set_group_add_request", bot=bot, **params)

    # ====================================================================
    #  4. 信息查询类 API
    # ====================================================================

    def get_login_info(self, bot=None) -> dict:
        """获取机器人自身信息"""
        return self._call("get_login_info", bot=bot)

    def get_stranger_info(self, user_id: int, no_cache: bool = False, bot=None) -> dict:
        """获取陌生人信息"""
        return self._call("get_stranger_info", bot=bot,
                          user_id=user_id, no_cache=no_cache)

    def get_friend_list(self, bot=None) -> dict:
        """获取好友列表"""
        return self._call("get_friend_list", bot=bot)

    def get_group_info(self, group_id: int, no_cache: bool = False, bot=None) -> dict:
        """获取群信息"""
        return self._call("get_group_info", bot=bot,
                          group_id=group_id, no_cache=no_cache)

    def get_group_list(self, bot=None) -> dict:
        """获取群列表"""
        return self._call("get_group_list", bot=bot)

    def get_group_member_info(self, group_id: int, user_id: int,
                              no_cache: bool = False, bot=None) -> dict:
        """获取群成员信息"""
        return self._call("get_group_member_info", bot=bot,
                          group_id=group_id, user_id=user_id, no_cache=no_cache)

    def get_group_member_list(self, group_id: int, bot=None) -> dict:
        """获取群成员列表"""
        return self._call("get_group_member_list", bot=bot, group_id=group_id)

    def get_cookies(self, domain: str = None, bot=None) -> dict:
        """获取 Cookies"""
        params = {}
        if domain is not None:
            params["domain"] = domain
        return self._call("get_cookies", bot=bot, **params)

    def get_csrf_token(self, bot=None) -> dict:
        """获取 CSRF Token"""
        return self._call("get_csrf_token", bot=bot)

    def get_credentials(self, domain: str = None, bot=None) -> dict:
        """同时获取 Cookies 和 CSRF Token"""
        params = {}
        if domain is not None:
            params["domain"] = domain
        return self._call("get_credentials", bot=bot, **params)

    # ====================================================================
    #  5. 媒体类 API
    # ====================================================================

    def get_record(self, file: str, out_format: str = "mp3", bot=None) -> dict:
        """获取语音消息文件"""
        return self._call("get_record", bot=bot,
                          file=file, out_format=out_format)

    def get_image(self, file: str, bot=None) -> dict:
        """获取图片文件"""
        return self._call("get_image", bot=bot, file=file)

    def can_send_image(self, bot=None) -> dict:
        """检查能否发送图片"""
        return self._call("can_send_image", bot=bot)

    def can_send_record(self, bot=None) -> dict:
        """检查能否发送语音"""
        return self._call("can_send_record", bot=bot)

    # ====================================================================
    #  6. 实用工具类 API
    # ====================================================================

    def get_status(self, bot=None) -> dict:
        """获取 OneBot 运行状态"""
        return self._call("get_status", bot=bot)

    def get_version_info(self, bot=None) -> dict:
        """获取版本信息"""
        return self._call("get_version_info", bot=bot)

    def set_restart(self, delay: int = 0, bot=None) -> dict:
        """重启 OneBot 实现"""
        return self._call("set_restart", bot=bot, delay=delay)

    def clean_cache(self, bot=None) -> dict:
        """清理缓存"""
        return self._call("clean_cache", bot=bot)
