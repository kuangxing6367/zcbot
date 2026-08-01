"""
工具函数模块 - 提供辅助函数
从 AstrBot 迁移至 zgric_onebot11，使用 CQ 码解析替代 AstrBot 组件
"""
import re
import logging
from typing import List

logger = logging.getLogger(__name__)

# CQ 码正则表达式
CQ_AT_PATTERN = re.compile(r'\[CQ:at,qq=(\d+)\]')
CQ_IMAGE_PATTERN = re.compile(r'\[CQ:image,file=([^\]]+)\]')


def get_ats(message_str: str) -> List[str]:
    """从消息字符串中提取所有被 @ 的用户 QQ 号

    解析 CQ 码格式: [CQ:at,qq=123456]
    """
    if not message_str:
        return []
    return CQ_AT_PATTERN.findall(message_str)


def get_images(message_str: str) -> List[str]:
    """从消息字符串中提取所有图片的 file 字段

    返回图片 CQ 码中的 file 值列表（可能是 file:/// 路径或 base64:// 数据）
    """
    if not message_str:
        return []
    return CQ_IMAGE_PATTERN.findall(message_str)


def extract_cq_segments(message_str: str) -> List[dict]:
    """将消息字符串解析为 CQ 码段列表

    返回格式: [{"type": "text", "data": "..."}, {"type": "at", "data": {"qq": "..."}}, ...]
    """
    if not message_str:
        return [{"type": "text", "data": ""}]

    segments = []
    # 匹配 CQ 码
    pattern = re.compile(r'\[CQ:([^,\]]+)((?:,[^=]+=[^\]]*)*)\]')
    last_end = 0

    for match in pattern.finditer(message_str):
        # 添加 CQ 码前的纯文本
        if match.start() > last_end:
            text = message_str[last_end:match.start()]
            if text:
                segments.append({"type": "text", "data": text})

        cq_type = match.group(1)
        params_str = match.group(2)

        # 解析参数
        params = {}
        if params_str:
            for param_match in re.finditer(r',([^=]+)=([^,}]*)', params_str):
                key = param_match.group(1).strip()
                value = param_match.group(2).strip()
                params[key] = value

        segments.append({"type": cq_type, "data": params})
        last_end = match.end()

    # 添加剩余的纯文本
    if last_end < len(message_str):
        text = message_str[last_end:]
        if text:
            segments.append({"type": "text", "data": text})

    return segments


def has_reply(message_str: str) -> bool:
    """检查消息是否包含回复"""
    return bool(re.search(r'\[CQ:reply', message_str))


def get_reply_id(message_str: str) -> str:
    """提取回复消息的 ID"""
    match = re.search(r'\[CQ:reply,id=(-?\d+)\]', message_str)
    if match:
        return match.group(1)
    return ""


def image_to_base64_cq(image_bytes: bytes) -> str:
    """将图片字节转换为 base64 CQ 码"""
    import base64
    b64_str = base64.b64encode(image_bytes).decode()
    return f"[CQ:image,file=base64://{b64_str}]"


def at_cq(user_id: str) -> str:
    """生成 @ 用户的 CQ 码"""
    return f"[CQ:at,qq={user_id}]"


def reply_cq(message_id: str) -> str:
    """生成回复的 CQ 码"""
    return f"[CQ:reply,id={message_id}]"