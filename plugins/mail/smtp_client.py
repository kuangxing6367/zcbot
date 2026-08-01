import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from email.mime.base import MIMEBase
from email import encoders
import os
import json
import urllib.request
from datetime import date
from plugins.mail.network_utils import open_tcp_socket

# 本地邮件 API 地址（使用 msmtp/sendmail 发送）
_LOCAL_MAIL_API = "http://127.0.0.1:5920/api/send"

# 每日发送限制
DAILY_SEND_LIMIT = 20
_daily_send_counter = {"date": date.today().isoformat(), "count": 0}


def _check_daily_limit() -> bool:
    """检查是否超过每日发送上限，返回 True 表示可以发送"""
    today = date.today().isoformat()
    if _daily_send_counter["date"] != today:
        _daily_send_counter["date"] = today
        _daily_send_counter["count"] = 0
    return _daily_send_counter["count"] < DAILY_SEND_LIMIT


def _increment_daily_counter():
    """增加每日发送计数"""
    _daily_send_counter["count"] += 1


def get_daily_send_status() -> dict:
    """获取每日发送状态"""
    today = date.today().isoformat()
    if _daily_send_counter["date"] != today:
        return {"sent": 0, "limit": DAILY_SEND_LIMIT, "remaining": DAILY_SEND_LIMIT}
    return {
        "sent": _daily_send_counter["count"],
        "limit": DAILY_SEND_LIMIT,
        "remaining": DAILY_SEND_LIMIT - _daily_send_counter["count"]
    }

def _emit_debug_log(client: smtplib.SMTP, *args: object) -> None:
    debug_printer = getattr(client, "_print_debug", None)
    if client.debuglevel > 0 and callable(debug_printer):
        debug_printer(*args)

class _SMTPPreferredIPv4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        _emit_debug_log(self, "connect: to", (host, port), self.source_address)
        return open_tcp_socket(
            host, port, timeout=timeout, source_address=self.source_address
        )

class _SMTPSSLPreferredIPv4(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        _emit_debug_log(self, "connect:", (host, port))
        new_socket = open_tcp_socket(
            host, port, timeout=timeout, source_address=self.source_address
        )
        return self.context.wrap_socket(new_socket, server_hostname=host)

def _resolve_smtp_auth(account: dict) -> tuple[str, str]:
    #  万能适配器：这里优先读取 smtp_username，如果没有就读 username，最后兜底读 email
    # 支持多种配置写法：email: xxx | username: xxx | smtp_username: xxx
    email_addr = (
        account.get("smtp_username") or 
        account.get("username") or 
        account.get("email") or 
        ""
    ).strip()
    
    # 原有的密码适配逻辑（smtp_password 或 password）
    password = (
        account.get("smtp_password") or 
        account.get("password") or 
        ""
    ).strip()

    if not email_addr:
        raise ValueError("未配置发件邮箱地址（请检查 email/username/smtp_username）。")
    if not password:
        raise ValueError("未配置 SMTP 密码（请检查 password/smtp_password）。")
    return email_addr, password

def _build_message(from_addr: str, from_name: str, to_addr: str, subject: str, body: str, attachments: list = None):
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    #  附件处理逻辑
    if attachments:
        for file_path in attachments:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"附件文件不存在: {file_path}")
            
            # 读取文件并添加到邮件
            with open(file_path, "rb") as f:
                file_data = f.read()
                file_name = os.path.basename(file_path)
            
            # EmailMessage 的 add_attachment 方法（Python 3.6+）
            msg.add_attachment(
                file_data,
                maintype="application",
                subtype="octet-stream",
                filename=file_name
            )
    return msg

def _send_via_local_api(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """通过本地邮件 API 发送邮件"""
    try:
        data = json.dumps({
            "to": to_addr,
            "subject": subject,
            "body": body
        }).encode("utf-8")

        req = urllib.request.Request(
            _LOCAL_MAIL_API,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 200:
                return True, "发送成功"
            else:
                return False, result.get("msg", "发送失败")
    except Exception as e:
        return False, f"本地API调用失败: {str(e)}"


def smtp_send_mail(account: dict, to_addr: str, subject: str, body: str, attachments: list = None):
    # 检查每日发送上限
    if not _check_daily_limit():
        status = get_daily_send_status()
        raise ValueError(f"今日发送次数已达上限 {status['limit']} 封，请明天再试")

    # 获取 SMTP 服务器配置（保持原逻辑）
    smtp_server = (account.get("smtp_server") or "").strip()
    smtp_port = int(account.get("smtp_port") or 0)
    smtp_use_ssl = bool(account.get("smtp_use_ssl", True))

    if not smtp_server:
        raise ValueError("未配置 SMTP 服务器地址(smtp_server)。")
    if smtp_port <= 0:
        raise ValueError("SMTP 端口(smtp_port)配置错误。")

    # 解析收件人地址
    real_name, real_addr = parseaddr(to_addr)
    if not real_addr or "@" not in real_addr:
        raise ValueError("收件人邮箱格式错误。")

    #  使用改进后的认证函数（支持多种字段名）
    from_addr, password = _resolve_smtp_auth(account)
    
    # 获取发件人名称
    from_name = account.get("sender_name") or from_addr

    #  优先使用本地邮件 API（无附件时）
    if not attachments:
        success, msg = _send_via_local_api(to_addr, subject.strip(), body.strip())
        if success:
            _increment_daily_counter()
            return
        # 本地 API 失败，降级到 SMTP
        print(f"[mail] 本地 API 发送失败，降级到 SMTP: {msg}")

    #  构建邮件（包含附件）
    # 注意：这里把 attachments 传给了 _build_message
    msg = _build_message(from_addr, from_name, real_addr, subject.strip(), body.strip(), attachments)

    try:
        if smtp_use_ssl:
            with _SMTPSSLPreferredIPv4(smtp_server, smtp_port, timeout=20) as server:
                server.login(from_addr, password)
                server.send_message(msg)
                _increment_daily_counter()
        else:
            with _SMTPPreferredIPv4(smtp_server, smtp_port, timeout=0) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(from_addr, password)
                server.send_message(msg)
                _increment_daily_counter()
    except smtplib.SMTPAuthenticationError:
        raise ValueError("SMTP 认证失败，请检查账号或授权码。")
    except smtplib.SMTPException as e:
        raise RuntimeError(f"SMTP 发送失败: {e}")