"""
邮件处理插件 - 从 astrbot_plugin_mail 迁移到 zgric_onebot11 框架

功能：监控多个 IMAP 邮箱的新邮件，通过 QQ 群自动推送通知，
      支持手动回复、查询历史邮件，以及通过 SMTP 发送邮件。

命令：
  /mail_status           查看所有邮箱的监控状态
  /mail_check            立即手动检查所有邮箱
  /mail_query <账户名> <日期>  查询指定邮箱自某日期以来的邮件
  /mail_reply <账户名> <收件人> <主题>|<正文>  手动发送邮件回复

被动：
  后台线程定时轮询邮箱，新邮件自动推送到配置的 notify_umo 目标会话
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from plugins.mail.imap_client import imap_fetch_new, imap_query_since, is_recent_email
from plugins.mail.smtp_client import smtp_send_mail

__plugin_meta__ = {
    "name": "mail",
    "version": "1.0.0",
    "author": "Neo（在gangcaiyoule基础上修改）",
    "desc": "监控处理发送 IMAP/SMTP 邮箱",
    "priority": 30,
}

# ====================================================================
# 模块级全局变量（由 register(ctx) 注入）
# ====================================================================
ctx = None  # 插件上下文，由 register() 注入

# 硬编码兜底配置
_FALLBACK_ACCOUNTS = [
    {
        "name": "foxmail",
        "email": "zgric@foxmail.com",
        "sender_name": "BillionMail",
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "username": "zgric@foxmail.com",
        "password": "cqgowgogustxdfgi",
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_password": "cqgowgogustxdfgi",
        "smtp_use_ssl": True,
        "folder": "INBOX",
        "forward_to_user": True,
        "ai_mode": True,
        "custom_prompt": "你收到了一封新邮件。请阅读邮件内容，用中文总结邮件的核心内容（不超过100字），判断邮件的重要性（高/中/低），并说明是否需要回复。如果需要回复，请使用 send_smtp_mail 工具发送回复。回复格式：\n【邮件摘要】\n发件人：xxx\n主题：xxx\n重要性：高/中/低\n摘要：xxx\n是否需要回复：是/否",
        "notify_umo": "QQGroup:1082979372",
    }
]
_FALLBACK_ADMIN_UIDS = ["2765126451"]

# 运行时状态（仅内存，不持久化）
_last_check_time: dict[str, str] = {}
_account_status: dict[str, str] = {}

# 后台轮询线程控制
_check_thread: threading.Thread | None = None
_check_stop_event = threading.Event()

# KV 存储（持久化到 JSON 文件，用于存储 last_uid / init_time 等）
_kv_data: dict = {}
_kv_lock = threading.Lock()
_kv_path: str = ""


# ====================================================================
# 配置读取辅助函数
# ====================================================================
def _get_config(key: str, default=None):
    """从插件配置中读取值，若为空则使用兜底"""
    global ctx
    if ctx is None:
        return default
    return ctx.get_config(key, default)


def _set_config(key: str, value):
    """写入插件配置（持久化到数据库）"""
    global ctx
    if ctx is None:
        return
    try:
        ctx.db_execute(
            "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE config_value = %s",
            (ctx._plugin_name, key, json.dumps(value, ensure_ascii=False),
             json.dumps(value, ensure_ascii=False)),
        )
    except Exception as e:
        ctx.log(f"写入配置 {key} 失败: {e}", level="error")


def _get_mail_accounts() -> list:
    """获取邮箱账户列表，带兜底"""
    accounts = _get_config("mail_accounts", [])
    if not accounts:
        accounts = _FALLBACK_ACCOUNTS
    return accounts


def _get_admin_uids() -> set[str]:
    """获取管理员 UID 列表，带兜底"""
    uids = _get_config("admin_uids", [])
    if not uids:
        uids = _FALLBACK_ADMIN_UIDS
    return {str(uid).strip() for uid in uids if isinstance(uid, (str, int)) and str(uid).strip()}


# ====================================================================
# KV 存储（基于 JSON 文件，用于持久化 last_uid / init_time）
# ====================================================================
def _kv_load():
    """从磁盘加载 KV 数据"""
    global _kv_data
    if not _kv_path or not os.path.exists(_kv_path):
        _kv_data = {}
        return
    try:
        with open(_kv_path, "r", encoding="utf-8") as f:
            _kv_data = json.load(f)
    except Exception:
        _kv_data = {}


def _kv_save():
    """将 KV 数据写入磁盘"""
    if not _kv_path:
        return
    try:
        with open(_kv_path, "w", encoding="utf-8") as f:
            json.dump(_kv_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        ctx.log(f"KV 存储写入失败: {e}", level="error")


def _kv_get(key: str, default=None):
    with _kv_lock:
        return _kv_data.get(key, default)


def _kv_set(key: str, value):
    with _kv_lock:
        _kv_data[key] = value
        _kv_save()


# ====================================================================
# 消息发送辅助函数
# ====================================================================
def _parse_umo(umo_str: str) -> tuple:
    """
    解析 UMO 字符串，返回 (group_id, user_id)
    支持格式：QQGroup:1082979372, QQUser:2765126451, 纯数字（默认群号）
    """
    if not umo_str:
        return None, None
    umo_str = str(umo_str).strip()
    if umo_str.startswith("QQGroup:"):
        try:
            return int(umo_str[8:].strip()), None
        except (ValueError, IndexError):
            return None, None
    elif umo_str.startswith("QQUser:"):
        try:
            return None, int(umo_str[7:].strip())
        except (ValueError, IndexError):
            return None, None
    # 纯数字，默认作为群号
    if umo_str.isdigit():
        return int(umo_str), None
    return None, None


def _send_to_umo(umo_str: str, message: str):
    """向 UMO 目标发送消息"""
    group_id, user_id = _parse_umo(umo_str)
    if group_id:
        ctx.send_msg(group_id=group_id, message=message)
    elif user_id:
        ctx.send_msg(user_id=user_id, message=message)
    else:
        ctx.log(f"无法解析 UMO 目标: {umo_str}", level="warning")


def _reply(event, message: str):
    """回复当前事件来源（群聊或私聊）"""
    if event.is_group and event.group_id:
        ctx.send_msg(group_id=event.group_id, message=message)
    else:
        ctx.send_msg(user_id=event.user_id, message=message)


# ====================================================================
# 权限判断
# ====================================================================
def _is_plugin_admin(event) -> bool:
    """判断当前用户是否为插件管理员"""
    admin_uids = _get_admin_uids()
    sender_id = str(event.user_id).strip()
    return bool(sender_id and sender_id in admin_uids)


def _get_admin_denied_message() -> str:
    """获取无权限提示"""
    if not _get_admin_uids():
        return (
            " 还未指定插件管理员。\n"
            "请在插件设置的 admin_uids 中添加用户 id。"
        )
    return " 无权限使用该命令。"


# ====================================================================
# 过滤规则
# ====================================================================
def _get_filter_settings(prefix: str) -> dict:
    settings = _get_config(f"{prefix}_settings", {}) or {}
    if isinstance(settings, dict):
        return settings
    return {}


def _get_filter_enabled(prefix: str) -> bool:
    settings = _get_filter_settings(prefix)
    if "enable" in settings:
        return bool(settings.get("enable", False))
    return bool(_get_config(f"enable_{prefix}", False))


def _get_filter_rules(prefix: str, field: str) -> list[str]:
    settings = _get_filter_settings(prefix)
    nested_values = settings.get(f"{field}_rules", [])
    if not nested_values:
        nested_values = _get_config(f"{field}_{prefix}", []) or []
    values = nested_values or []
    return [
        str(value).strip()
        for value in values
        if isinstance(value, (str, int, float)) and str(value).strip()
    ]


def _matches_sender_rule(mail_info: dict, rule: str) -> bool:
    normalized_rule = rule.strip().casefold()
    if not normalized_rule:
        return False
    from_addr = (mail_info.get("from_addr") or "").strip().casefold()
    from_name = (mail_info.get("from_name") or "").strip().casefold()

    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_rule):
        return from_addr == normalized_rule

    if normalized_rule.startswith("@"):
        return from_addr.endswith(normalized_rule)

    return normalized_rule in from_addr or normalized_rule in from_name


def _matches_contains_rule(text: str, rule: str) -> bool:
    normalized_rule = rule.strip().casefold()
    if not normalized_rule:
        return False
    return normalized_rule in (text or "").casefold()


def _match_rule_group(mail_info: dict, prefix: str) -> tuple:
    sender_rules = _get_filter_rules(prefix, "sender")
    for rule in sender_rules:
        if _matches_sender_rule(mail_info, rule):
            return True, f"sender:{rule}"

    subject_rules = _get_filter_rules(prefix, "subject")
    subject = mail_info.get("subject") or ""
    for rule in subject_rules:
        if _matches_contains_rule(subject, rule):
            return True, f"subject:{rule}"

    body_rules = _get_filter_rules(prefix, "body")
    filter_body = mail_info.get("filter_body") or mail_info.get("body") or ""
    for rule in body_rules:
        if _matches_contains_rule(filter_body, rule):
            return True, f"body:{rule}"

    return False, None


def _should_notify_mail(mail_info: dict) -> tuple:
    enable_blacklist = _get_filter_enabled("blacklist")
    enable_whitelist = _get_filter_enabled("whitelist")

    if enable_blacklist:
        is_blacklisted, rule = _match_rule_group(mail_info, "blacklist")
        if is_blacklisted:
            return False, f"被黑名单屏蔽 ({rule})"

    if enable_whitelist:
        is_whitelisted, rule = _match_rule_group(mail_info, "whitelist")
        if not is_whitelisted:
            return False, "被白名单屏蔽"
        return True, f"白名单允许 ({rule})"

    return True, "允许，因为不需要匹配白名单限制"


# ====================================================================
# 账户查找
# ====================================================================
def _get_account_by_name_or_email(account_name: str) -> dict | None:
    accounts = _get_mail_accounts()
    target_name = account_name.strip()
    for acc in accounts:
        name = (acc.get("name") or "").strip()
        addr = (acc.get("email") or "").strip()
        if target_name in (name, addr):
            return acc
    return None


def choose_account(account_name: str, to_addr: str) -> dict | None:
    """
    智能选择 SMTP 账户
    逻辑：从多个可能的配置路径获取账户列表
    """
    all_accounts = _get_mail_accounts()

    if not all_accounts:
        ctx.log("choose_account: 所有配置路径均未找到账户列表", level="warning")
        return None

    # 情况1：AI 填写了账户名
    if account_name and account_name.strip():
        query = account_name.strip()
        ctx.log(f"正在尝试匹配账户名: '{query}'", level="debug")

        for acc in all_accounts:
            config_name = str(acc.get("name", "")).strip()
            if query.lower() in config_name.lower():
                ctx.log(f"成功匹配账户 (模糊匹配): {config_name} ({acc.get('email')})", level="info")
                return acc

        available = [a.get("name", "未命名") for a in all_accounts]
        ctx.log(f"未找到匹配的账户。搜索词: '{query}', 可用账户名: {available}", level="warning")
        return None

    # 情况2：AI 没填写账户名，使用默认账户
    if all_accounts:
        first_name = all_accounts[0].get("name", "未知")
        ctx.log(f"使用默认账户: {first_name}", level="info")
        return all_accounts[0]

    return None


# ====================================================================
# 邮件发送工具函数（原 LLM Tool，现转为普通函数）
# ====================================================================
def send_smtp_mail(to_addr: str, subject: str, body: str,
                   account_name: str = None, attachments: list = None) -> str:
    """
    发送 SMTP 邮件。
    返回结果字符串，供调用方直接展示。
    """
    import os as _os

    # 1. 验证收件人邮箱格式
    if not to_addr or "@" not in to_addr:
        return " 错误：收件人邮箱格式不正确。"

    # 2. 验证主题和内容
    if not subject or not subject.strip():
        return " 错误：邮件主题不能为空。"
    if not body or not body.strip():
        return " 错误：邮件内容不能为空。"

    # 3. 验证附件（如果提供了的话）
    if attachments:
        for path in attachments:
            if not _os.path.exists(path):
                return f" 错误：附件文件不存在：{path}"

    # 4. 获取 SMTP 账户配置
    smtp_config = choose_account(account_name, to_addr)
    if not smtp_config:
        if account_name:
            return f" 错误：未找到匹配的SMTP账户 '{account_name}'。请检查账户备注名。"
        else:
            return " 错误：未找到有效的SMTP账户配置。"

    # 5. 使用 smtp_client 发送邮件
    try:
        result = smtp_send_mail(smtp_config, to_addr, subject, body, attachments)
        base_msg = f" 邮件发送成功！\n 已发送至：{to_addr}\n 主题：{subject}"
        if attachments:
            base_msg += f"\n 附有 {len(attachments)} 个文件"
        return base_msg
    except ValueError as e:
        return f" 配置错误：{str(e)}"
    except RuntimeError as e:
        return f" 发送失败：{str(e)}"
    except Exception as e:
        ctx.log(f"发送邮件异常: {e}", level="error")
        return f" 未知错误：{str(e)}"


# ====================================================================
# IMAP 检查逻辑
# ====================================================================
def _check_account(account: dict):
    """检查指定邮箱的新邮件（同步函数）"""
    account_email = account["email"]
    max_body_len = max(int(_get_config("max_body_length", 500) or 500), 1)
    filter_body_len = max(
        int(_get_config("filter_body_length", 3000) or 3000),
        max_body_len,
    )

    uid_key = f"last_uid_{account_email}"
    init_key = f"init_time_{account_email}"
    last_uid = _kv_get(uid_key, 0) or 0
    init_time = _kv_get(init_key, "")

    is_first_run = not init_time
    if is_first_run:
        # 首次运行记录初始化时间和当前 UID 基线，防止历史邮件被推送
        init_time = datetime.now(timezone.utc).isoformat()
        _kv_set(init_key, init_time)

    # imaplib 为阻塞操作，直接在线程中执行
    new_emails, new_max_uid = imap_fetch_new(
        account, last_uid, max_body_len, filter_body_len
    )

    if new_max_uid > last_uid:
        _kv_set(uid_key, new_max_uid)

    if is_first_run:
        if new_max_uid > 0:
            ctx.log(
                f"邮件通知插件：{account_email} 初始化完成，最大UID = {new_max_uid}",
                level="info",
            )
        return

    init_dt = datetime.fromisoformat(init_time)
    for mail_info in new_emails:
        # 二次校验邮件时间，避免刚拉取的邮件属于历史存量
        if is_recent_email(mail_info, init_dt):
            # --- 开关1: 是否转发给用户 ---
            should_forward = account.get("forward_to_user", True)
            # --- 目标会话 ID ---
            notify_umo = account.get("notify_umo", "")

            if not notify_umo:
                ctx.log(f"账户 {account['email']} 的 notify_umo 未配置，跳过处理。", level="warning")
                continue

            # --- 独立判断：是否转发原始邮件 ---
            if should_forward:
                _send_to_umo(
                    notify_umo,
                    f" {account.get('name', '邮件')}更新:\n"
                    f"来自: {mail_info['from_name']} <{mail_info['from_addr']}>\n"
                    f"主题: {mail_info['subject']}\n"
                    f"时间: {mail_info['date']}\n"
                    f"内容: {mail_info['body'][:200]}...",
                )


# ====================================================================
# 后台轮询循环（线程中运行）
# ====================================================================
def _check_loop():
    """后台邮件检查循环（线程函数）"""
    time.sleep(10)  # 等待系统初始化完成
    ctx.log("邮件通知插件：后台检查循环已启动。", level="info")

    while not _check_stop_event.is_set():
        try:
            interval = _get_config("check_interval", 5)
            accounts = _get_mail_accounts()

            for account in accounts:
                if not account.get("email") or not account.get("imap_server"):
                    continue

                # 优先使用账户内的 notify_umo，如果没有再用全局的
                notify_umo = account.get("notify_umo") or _get_config("notify_umo", "")

                # 只有当这个账户有通知目标时，才去检查
                if notify_umo:
                    try:
                        _check_account(account)
                        _account_status[account["email"]] = " 正常"
                    except Exception as e:
                        _account_status[account["email"]] = f" {str(e)[:80]}"
                        ctx.log(f"邮件通知插件：{account['email']} 检查失败: {e}", level="error")

                    _last_check_time[account["email"]] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                else:
                    ctx.log(
                        f"邮件通知插件：账户 {account['email']} 未配置 notify_umo，跳过检查。",
                        level="debug",
                    )

            # 等待下一次检查（可被停止事件中断）
            _check_stop_event.wait(max(interval, 1) * 60)

        except Exception as e:
            ctx.log(f"邮件通知插件：循环异常: {e}", level="error")
            _check_stop_event.wait(60)


# ====================================================================
# 命令处理函数
# ====================================================================
def handle_mail_status(event, match):
    """查看所有邮箱的监控状态"""
    if not _is_plugin_admin(event):
        _reply(event, _get_admin_denied_message())
        return

    accounts = _get_mail_accounts()
    notify_umo = _get_config("notify_umo", "")
    interval = _get_config("check_interval", 5)

    if not accounts:
        _reply(event, " 未配置任何邮箱账户，请在 WebUI 插件配置中添加。")
        return

    lines = [
        f" 邮箱监控状态 (间隔: {interval}分钟)",
        f" 通知目标: {'已绑定' if notify_umo else '未绑定，请先在webui配置'}",
        "━━━━━━━━━━━━━━━━",
    ]
    for acc in accounts:
        addr = acc.get("email", "?")
        name = acc.get("name") or addr
        status = _account_status.get(addr, "⏳ 等待首次检查")
        last = _last_check_time.get(addr, "尚未检查")
        lines.append(f" {name} ({addr})")
        lines.append(f"   状态: {status}")
        lines.append(f"   最近检查: {last}")

    _reply(event, "\n".join(lines))


def handle_mail_check(event, match):
    """立即手动检查所有邮箱"""
    if not _is_plugin_admin(event):
        _reply(event, _get_admin_denied_message())
        return

    accounts = _get_mail_accounts()
    if not accounts:
        _reply(event, " 未配置任何邮箱账户，请在 WebUI 插件配置中添加。")
        return

    _reply(event, " 正在检查所有邮箱...")
    errors = []
    for account in accounts:
        if not account.get("email") or not account.get("imap_server"):
            continue
        email_addr = account["email"]
        try:
            _check_account(account)
            _account_status[email_addr] = " 正常"
        except Exception as e:
            _account_status[email_addr] = f" {str(e)[:80]}"
            errors.append(f"{account.get('name') or email_addr}: {e}")
        _last_check_time[email_addr] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if errors:
        _reply(event, " 部分邮箱检查失败:\n" + "\n".join(errors))
    else:
        _reply(event, " 所有邮箱检查完成。")


def handle_mail_query(event, match):
    """查询指定邮箱自某日期以来的邮件，如 /mail_query qq邮箱 2026-03-01"""
    if not _is_plugin_admin(event):
        _reply(event, _get_admin_denied_message())
        return

    # 解析参数：/mail_query <account_name> <since_date>
    parts = (event.message or "").strip().split()
    if len(parts) < 3:
        _reply(event, " 用法: /mail_query <账户名> <日期(YYYY-MM-DD)>")
        return

    account_name = parts[1]
    since_date = parts[2]

    accounts = _get_mail_accounts()
    # 解析目标账户
    target = None
    for acc in accounts:
        name = acc.get("name", "")
        addr = acc.get("email", "")
        if account_name in (name, addr):
            target = acc
            break
    if not target:
        _reply(
            event,
            f' 未找到名为 "{account_name}" 的邮箱账户。\n'
            f"已配置的账户: {', '.join(a.get('name') or a.get('email', '?') for a in accounts)}",
        )
        return

    # 验证日期格式
    try:
        since_dt = datetime.strptime(since_date, "%Y-%m-%d")
    except ValueError:
        _reply(event, " 日期格式错误，请使用 YYYY-MM-DD，如 2026-03-01")
        return

    _reply(event, f" 正在查询 {account_name} 自 {since_date} 以来的邮件...")
    try:
        max_body_len = _get_config("max_body_length", 500)
        emails = imap_query_since(target, since_dt, max_body_len)
    except Exception as e:
        _reply(event, f" 查询失败: {e}")
        return

    if not emails:
        _reply(event, f" {account_name} 自 {since_date} 以来没有邮件。")
        return

    lines = [
        f" {account_name} 自 {since_date} 以来共 {len(emails)} 封邮件：",
        "━━━━━━━━━━━━━━━━",
    ]
    for i, m in enumerate(emails, 1):
        lines.append(f"{i}.  {m['subject']}")
        lines.append(f"    {m['from_name']}   {m['date']}")

    _reply(event, "\n".join(lines))


def _parse_mail_reply_args(message_str: str) -> tuple:
    """
    解析 mail_reply 命令参数。
    格式: /mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>
    返回 (account_name, to_addr, subject, body)
    """
    raw = re.sub(r"\s+", " ", (message_str or "").strip())
    if not raw:
        raise ValueError("参数为空。")
    parts = raw.split(" ", 1)
    if len(parts) < 2:
        raise ValueError("参数缺失。")

    args_text = parts[1].strip()
    args = args_text.split(" ", 2)
    if len(args) < 3:
        raise ValueError("参数不足。")

    account_name, to_addr, subject_body = args[0].strip(), args[1].strip(), args[2]

    if "|" not in subject_body:
        raise ValueError("缺少主题与正文分隔符。")

    subject, body = [s.strip() for s in subject_body.split("|", 1)]

    if not account_name:
        raise ValueError("账户名不能为空。")
    if not to_addr:
        raise ValueError("收件人不能为空。")
    if "@" not in to_addr:
        raise ValueError("收件人邮箱格式错误。")
    if not subject:
        raise ValueError("邮件主题不能为空。")
    if not body:
        raise ValueError("邮件正文不能为空。")
    if len(subject) > 200:
        raise ValueError("邮件主题过长（最多 200 字符）。")
    if len(body) > 5000:
        raise ValueError("邮件正文过长（最多 5000 字符）。")

    return account_name, to_addr, subject, body


def handle_mail_reply(event, match):
    """手动发送邮件回复。格式：/mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>"""
    if not _is_plugin_admin(event):
        _reply(event, _get_admin_denied_message())
        return

    usage = (
        " 用法错误\n"
        "格式: /mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>\n"
        "示例: /mail_reply qq邮箱 test@example.com 回复主题|你好，已收到你的邮件。"
    )
    try:
        account_name, to_addr, subject, body = _parse_mail_reply_args(event.message or "")
    except ValueError as e:
        _reply(event, f"{usage}\n原因: {e}")
        return

    account = _get_account_by_name_or_email(account_name)
    if not account:
        accounts = _get_mail_accounts()
        account_names = ", ".join(
            (a.get("name") or a.get("email") or "?") for a in accounts
        )
        _reply(
            event,
            f' 未找到名为 "{account_name}" 的邮箱账户。\n已配置账户: {account_names or "(空)"}',
        )
        return

    if not account.get("smtp_server"):
        _reply(
            event,
            " 该账户未配置 SMTP 服务器。请在插件配置中填写 smtp_server、smtp_port、smtp_use_ssl。",
        )
        return

    _reply(event, " 正在发送邮件...")
    try:
        smtp_send_mail(account, to_addr, subject, body)
    except Exception as e:
        _reply(event, f" 发送失败: {e}")
        return
    account_display = account.get("name") or account.get("email") or account_name
    _reply(event, f" 发送成功\n账户: {account_display}\n收件人: {to_addr}\n主题: {subject}")


# ====================================================================
# 插件注册入口
# ====================================================================
def register(plugin_ctx):
    """插件注册入口，由框架在加载时调用"""
    global ctx, _kv_path, _check_thread, _check_stop_event

    ctx = plugin_ctx

    # 初始化 KV 存储路径
    kv_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
    )
    _kv_path = os.path.join(kv_dir, "_kv_data.json")
    _kv_load()

    # 注册命令
    ctx.command(
        "/mail_status",
        handle_mail_status,
        alias="/邮箱状态",
        description="查看所有邮箱的监控状态",
    )
    ctx.command(
        "/mail_check",
        handle_mail_check,
        alias="/邮箱检查",
        description="立即手动检查所有邮箱",
    )
    ctx.command(
        "/mail_query",
        handle_mail_query,
        alias="/邮箱查询",
        description="查询指定邮箱自某日期以来的邮件，如 /mail_query qq邮箱 2026-03-01",
    )
    ctx.command(
        "/mail_reply",
        handle_mail_reply,
        alias="/邮箱回复",
        description="手动发送邮件回复。格式：/mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>",
    )

    # 启动后台轮询线程
    _check_stop_event.clear()
    _check_thread = threading.Thread(target=_check_loop, daemon=True, name="mail_check_loop")
    _check_thread.start()

    ctx.log("邮件插件注册完成，后台轮询线程已启动。")


# ====================================================================
# 插件卸载清理
# ====================================================================
def on_unload():
    """插件卸载时的清理"""
    global _check_thread, _check_stop_event

    # 停止后台轮询线程
    _check_stop_event.set()
    if _check_thread and _check_thread.is_alive():
        _check_thread.join(timeout=5)
        _check_thread = None

    _kv_save()  # 保存 KV 数据
    _last_check_time.clear()
    _account_status.clear()

    try:
        ctx.log("邮件插件已卸载。")
    except Exception:
        pass