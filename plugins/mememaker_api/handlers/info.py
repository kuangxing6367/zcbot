"""
信息查询处理器 - 表情详情信息展示
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import logging
from typing import Dict, List, Optional

from plugins.mememaker_api.models import MemeOption
from plugins.mememaker_api._shared import _api_client, _meme_manager, _ctx
from plugins.mememaker_api.core.utils import image_to_base64_cq

logger = logging.getLogger(__name__)


def _format_meme_option(option: MemeOption) -> str:
    """辅助函数：格式化单个选项的详细信息"""
    flags, pf = [], option.parser_flags
    if pf.get("long", True):
        flags.append(f"--{option.name}")
    if pf.get("short", False):
        flags.append(f"-{option.name[0]}")
    flags.extend([f"--{a}" for a in pf.get("long_aliases", [])])
    flags.extend([f"-{a}" for a in pf.get("short_aliases", [])])

    text = f"  {'/'.join(flags)}"
    if option.type != "boolean":
        text += f" <{option.type.upper()}>"

    text += f"\n    说明: {option.description or '无'}"

    additions = []
    option_dict = option.model_dump()
    if option.type in ["integer", "float"]:
        if "minimum" in option_dict and option_dict["minimum"] is not None:
            additions.append(f"最小: {option_dict['minimum']}")
        if "maximum" in option_dict and option_dict['maximum'] is not None:
            additions.append(f"最大: {option_dict['maximum']}")
    if option.type == "string" and option_dict.get("choices"):
        additions.append(f"可选: {', '.join(option_dict['choices'])}")
    if option.default is not None:
        additions.append(f"默认: {option.default}")

    if additions:
        text += f" ({' | '.join(additions)})"
    return text


async def handle_meme_info(event, match):
    """处理表情详情查询"""
    ctx = _ctx
    keyword = (match.group(1) or '').strip() if match else ''
    try:
        if not keyword:
            _send(event, ctx, "请提供关键词，如：-表情详情 摸")
            return

        meme_info = _meme_manager.find_meme_by_keyword(keyword)
        if not meme_info:
            _send(event, ctx, f"未找到"{keyword}"相关表情。")
            return

        p = meme_info.params
        info_text = f"表情名：{meme_info.key}"
        info_text += f"\n关键词：{', '.join(meme_info.keywords)}"
        if meme_info.shortcuts:
            shortcuts = ", ".join([sc.get("humanized") or sc.get("pattern", "") for sc in meme_info.shortcuts])
            info_text += f"\n快捷指令：{shortcuts}"
        if meme_info.tags:
            info_text += f"\n标签：{', '.join(meme_info.tags)}"
        info_text += f"\n需要图片数：{p.min_images}" + (f" ~ {p.max_images}" if p.min_images != p.max_images else "")
        info_text += f"\n需要文字数：{p.min_texts}" + (f" ~ {p.max_texts}" if p.min_texts != p.max_texts else "")
        if p.default_texts:
            info_text += f"\n默认文字：{', '.join(p.default_texts)}"
        if p.options:
            options_info = "\n".join([_format_meme_option(opt) for opt in p.options])
            info_text += f"\n\n--- 可选选项 ---\n{options_info}"

        preview_img = await _api_client.get_meme_preview(meme_info.key)
        preview_cq = image_to_base64_cq(preview_img)

        _send(event, ctx, info_text + "\n\n--- 表情预览 ---\n" + preview_cq)

    except Exception as e:
        logger.error(f"获取表情详情失败: {e}", exc_info=True)
        _send(event, ctx, "获取表情详情失败了，呜呜...")


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )