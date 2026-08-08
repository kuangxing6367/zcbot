"""
Event 对象
封装 OneBot 11 上报的事件数据，供插件 handler 使用
"""
import time

# 权限查询 TTL 缓存（热路径性能：避免每个事件反复查库）
# 用户：user_id -> (role, is_blacklist, ts) ；群成员：(`group_id`,`user_id`) -> (role, ts)
_ROLE_CACHE_TTL = 60.0
_user_role_cache: dict = {}
_group_role_cache: dict = {}


def invalidate_user_role_cache(user_id: int = None):
    """清除用户权限缓存（Web 端修改权限后调用，None=全部）"""
    if user_id is None:
        _user_role_cache.clear()
    else:
        _user_role_cache.pop(user_id, None)


def invalidate_group_role_cache(group_id: int = None, user_id: int = None):
    """清除群成员权限缓存"""
    if group_id is None:
        _group_role_cache.clear()
    else:
        for k in [k for k in _group_role_cache if
                  k[0] == group_id and (user_id is None or k[1] == user_id)]:
            _group_role_cache.pop(k, None)


def _extract_text(message):
    """
    从 OneBot 11 消息中提取纯文本
    支持：字符串格式（CQ码）和数组格式（消息段）
    """
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for seg in message:
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {})
                if seg_type == 'text':
                    parts.append(seg_data.get('text', ''))
                elif seg_type == 'at':
                    qq = seg_data.get('qq', '')
                    parts.append(f'[@{qq}]')
                elif seg_type == 'reply':
                    pass  # 回复消息不提取
                # 其他类型（image/face/record等）忽略
        return ''.join(parts)
    return str(message)


class Event:
    """消息事件对象"""

    def __init__(self, raw: dict, bot_name: str = 'default'):
        self._raw = raw
        self.bot_name = bot_name  # 消息来源的 OneBot 实例名

        # 基础信息
        self.post_type = raw.get('post_type', '')     # message / notice / request / meta_event
        self.message_type = raw.get('message_type', '')  # group / private
        self.sub_type = raw.get('sub_type', '')          # 子类型
        self.self_id = raw.get('self_id', 0)             # 机器人QQ号

        # 消息内容（提取纯文本用于命令匹配）
        self.message = _extract_text(raw.get('message', ''))
        self.message_id = raw.get('message_id', 0)
        self.raw_message = _extract_text(raw.get('raw_message', raw.get('message', '')))

        # 保留原始消息段（供插件处理富媒体：图片/语音/视频/文件/回复等）
        raw_msg = raw.get('message', '')
        if isinstance(raw_msg, list):
            self.segments = raw_msg  # 数组格式：每个元素是 {type, data}
        elif isinstance(raw_msg, str) and raw_msg:
            self.segments = [{'type': 'text', 'data': {'text': raw_msg}}]
        else:
            self.segments = []

        # 发送者信息
        self.user_id = raw.get('user_id', 0)
        self.sender = raw.get('sender', {})

        # 群聊信息
        self.group_id = raw.get('group_id', 0)

        # 字体
        self.font = raw.get('font', 0)

        # ----- 权限信息（延迟加载，在调用属性时按需查询） -----
        self._framework = None  # 由 router 注入
        self._role_cache = None  # 缓存 role 查询结果

        # ----- 事件传播控制（参考 AstrBot PipelineScheduler）-----
        self._stopped = False  # 是否停止传播

    # ===== 权限属性（参考 AstrBot PermissionTypeFilter + is_admin） =====

    @property
    def sender_role(self) -> str:
        """OneBot 上报的 sender.role：owner / admin / member"""
        return self.sender.get('role', 'member')

    @property
    def role(self) -> str:
        """
        完整身份等级（参考 AstrBot 权限层级）：
        super（超管）> owner（群主）> admin（管理员）> member（成员）> blacklist（黑名单）
        首次查询后缓存结果，避免重复查库；底层权限查询带 60s TTL 内存缓存
        """
        if self._role_cache is not None:
            return self._role_cache
        if self._framework is None:
            self._role_cache = self.sender_role
            return self._role_cache
        try:
            db = self._framework.db
            now = time.time()
            uid = self.user_id

            # 用户级（super / blacklist）查询（60s TTL 内存缓存）
            u_entry = _user_role_cache.get(uid)
            if u_entry is None or now - u_entry[2] > _ROLE_CACHE_TTL:
                row = db.query_one(
                    "SELECT role, is_blacklist FROM users WHERE user_id=%s", (uid,))
                if row:
                    u_entry = (
                        row.get('role') or '',
                        1 if row.get('is_blacklist') else 0,
                        now,
                    )
                else:
                    u_entry = ('', 0, now)
                _user_role_cache[uid] = u_entry
            role, is_blacklist, _ = u_entry

            if role == 'super':
                self._role_cache = 'super'
                return 'super'
            if is_blacklist:
                self._role_cache = 'blacklist'
                return 'blacklist'

            # 群内角色（TTL 缓存）
            if self.is_group and self.group_id:
                gkey = (self.group_id, uid)
                grp_cached = _group_role_cache.get(gkey)
                if grp_cached is None or now - grp_cached[1] > _ROLE_CACHE_TTL:
                    grp = db.query_one(
                        "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                        (self.group_id, uid)
                    )
                    grole = (grp.get('role') or 'member') if grp else 'member'
                    _group_role_cache[gkey] = (grole, now)
                else:
                    grole = grp_cached[0]
                if grole in ('owner', 'admin'):
                    self._role_cache = grole
                    return grole
            self._role_cache = 'member'
            return 'member'
        except Exception:
            self._role_cache = self.sender_role
            return self._role_cache

    @property
    def is_admin(self) -> bool:
        """
        判断用户是否具备管理权限（等价于 AstrBot 的 event.is_admin()）
        超管 / 群主 / 群管理员均返回 True
        """
        return self.role in ('super', 'owner', 'admin')

    @property
    def is_superuser(self) -> bool:
        """判断用户是否为框架超级管理员"""
        return self.role == 'super'

    @property
    def is_group_owner(self) -> bool:
        """判断用户是否为群主"""
        return self.role == 'owner'

    # ===== 事件传播控制（参考 AstrBot PipelineScheduler）=====

    def stop_event(self) -> None:
        """
        停止事件继续传播到后续插件。
        参考 AstrBot AstrMessageEvent.stop_event() / MessageEventResult.stop_event()
        调用后，当前插件之后的插件将不再收到此事件。
        """
        self._stopped = True

    def is_stopped(self) -> bool:
        """
        检查事件是否已被停止传播。
        参考 AstrBot PipelineScheduler: 在 pipeline 的各阶段和 handler 循环中检查。
        """
        return self._stopped

    # ===== 富媒体辅助属性 =====

    @property
    def has_image(self) -> bool:
        """消息是否包含图片"""
        return any(s.get('type') == 'image' for s in self.segments)

    @property
    def has_reply(self) -> bool:
        """消息是否包含回复"""
        return any(s.get('type') == 'reply' for s in self.segments)

    @property
    def has_voice(self) -> bool:
        """消息是否包含语音"""
        return any(s.get('type') == 'record' for s in self.segments)

    @property
    def has_video(self) -> bool:
        """消息是否包含视频"""
        return any(s.get('type') == 'video' for s in self.segments)

    @property
    def has_file(self) -> bool:
        """消息是否包含文件"""
        return any(s.get('type') == 'file' for s in self.segments)

    @property
    def has_face(self) -> bool:
        """消息是否包含表情"""
        return any(s.get('type') == 'face' for s in self.segments)

    @property
    def has_share(self) -> bool:
        """消息是否包含分享卡片"""
        return any(s.get('type') == 'share' for s in self.segments)

    @property
    def share(self) -> dict:
        """
        获取分享卡片数据（title/url/desc 等）
        没有分享卡片则返回空 dict
        """
        for s in self.segments:
            if s.get('type') == 'share':
                return s.get('data', {}) or {}
        return {}

    @property
    def has_at(self) -> bool:
        """消息是否包含 @ 提及"""
        return any(s.get('type') == 'at' for s in self.segments)

    @property
    def has_at_bot(self) -> bool:
        """消息是否 @ 了机器人（self_id）"""
        return any(
            s.get('type') == 'at' and str(s.get('data', {}).get('qq', '')) == str(self.self_id)
            for s in self.segments
        )

    @property
    def reply_id(self):
        """
        获取回复的消息 ID（如果消息是回复）
        没有回复则返回 None
        """
        for s in self.segments:
            if s.get('type') == 'reply':
                try:
                    return int(s.get('data', {}).get('id', 0))
                except (ValueError, TypeError):
                    return None
        return None

    @property
    def images(self) -> list:
        """
        获取消息中所有图片信息
        返回 [{file, url, ...}]，每项取决于 OneBot 实现提供的数据
        """
        return [
            s.get('data', {}) for s in self.segments
            if s.get('type') == 'image'
        ]

    @property
    def first_image(self) -> dict:
        """获取第一张图片的数据（没有返回空 dict）"""
        imgs = self.images
        return imgs[0] if imgs else {}

    @property
    def at_list(self) -> list:
        """获取所有被 @ 的 QQ 号列表"""
        result = []
        for s in self.segments:
            if s.get('type') == 'at':
                qq = s.get('data', {}).get('qq', '')
                if qq and qq != 'all':
                    try:
                        result.append(int(qq))
                    except (ValueError, TypeError):
                        pass
        return result

    @property
    def at_all(self) -> bool:
        """消息是否 @全体成员"""
        return any(
            s.get('type') == 'at' and s.get('data', {}).get('qq') == 'all'
            for s in self.segments
        )

    @property
    def is_group(self) -> bool:
        return self.message_type == 'group'

    @property
    def is_private(self) -> bool:
        return self.message_type == 'private'

    @property
    def sender_nickname(self) -> str:
        return self.sender.get('nickname', '')

    @property
    def sender_card(self) -> str:
        return self.sender.get('card', '')

    def __repr__(self):
        return f"Event(type={self.message_type}, user={self.user_id}, group={self.group_id}, msg={self.message[:30]})"
