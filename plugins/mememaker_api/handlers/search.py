"""
搜索处理器 - 表情搜索功能
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import logging
from typing import Dict, List, Optional

from plugins.mememaker_api._shared import _api_client, _meme_manager, _ctx

logger = logging.getLogger(__name__)


async def handle_meme_search(event, match):
    """处理表情搜索"""
    ctx = _ctx
    query = (match.group(1) or '').strip() if match else ''
    if not query:
        _send(event, ctx, "请输入搜索关键词，例如：-表情搜索 猫")
        return

    try:
        _send(event, ctx, f"正在搜索"{query}"...")
        searched_keys = await _api_client.search_memes(query, include_tags=True)
        if not searched_keys:
            _send(event, ctx, "没有找到相关表情！")
            return

        searched_memes = [_meme_manager.meme_infos[key] for key in searched_keys if key in _meme_manager.meme_infos]

        if not searched_memes:
            _send(event, ctx, "没有找到相关表情！")
            return

        num_per_page = 8
        total_page = (len(searched_memes) - 1) // num_per_page + 1
        page_num = 0

        def format_page() -> str:
            start = page_num * num_per_page
            end = min(start + num_per_page, len(searched_memes))
            page_content = [
                f"{start + i + 1}. {meme.key} ({'/'.join(meme.keywords)})" +
                (f"\n    tags: {'、'.join(meme.tags)}" if meme.tags else "")
                for i, meme in enumerate(searched_memes[start:end])
            ]
            msg = f"找到了与"{query}"相关的表情：\n" + "\n".join(page_content)
            if total_page > 1:
                msg += f"\n\n--- 页码 {page_num + 1}/{total_page} ---\n发送 '<' 或 '>' 翻页，或直接发送页码。超时30秒后自动结束。"
            return msg

        if total_page <= 1:
            _send(event, ctx, format_page())
            return

        _send(event, ctx, format_page())

        # 保存分页上下文到模块级字典
        _search_sessions[str(event.user_id)] = {
            "page_num": 0,
            "total_page": total_page,
            "query": query,
            "memes": searched_memes,
            "num_per_page": num_per_page,
            "timeout": 30,
        }

    except Exception as e:
        logger.error(f"搜索表情时出错: {e}", exc_info=True)
        _send(event, ctx, "搜索失败了，呜呜...")


# 模块级搜索会话存储
_search_sessions: Dict[str, dict] = {}


def handle_search_pagination(event, ctx):
    """处理搜索分页（由 universal_handler 中的路由调用）"""
    session = _search_sessions.get(str(event.user_id))
    if not session:
        return False

    resp = event.message.strip()
    page_num = session["page_num"]
    total_page = session["total_page"]
    memes = session["memes"]
    num_per_page = session["num_per_page"]

    if resp.isdigit() and 1 <= (page := int(resp)) <= total_page:
        page_num = page - 1
    elif resp in ["上一页", "上页", "上", "<-", "<", "←"]:
        page_num = (page_num - 1 + total_page) % total_page
    elif resp in ["下一页", "下页", "下", "->", ">", "→"]:
        page_num = (page_num + 1) % total_page
    else:
        _search_sessions.pop(str(event.user_id), None)
        return False

    session["page_num"] = page_num

    start = page_num * num_per_page
    end = min(start + num_per_page, len(memes))
    page_content = [
        f"{start + i + 1}. {meme.key} ({'/'.join(meme.keywords)})" +
        (f"\n    tags: {'、'.join(meme.tags)}" if meme.tags else "")
        for i, meme in enumerate(memes[start:end])
    ]
    msg = f"找到了与"{session['query']}"相关的表情：\n" + "\n".join(page_content)
    if total_page > 1:
        msg += f"\n\n--- 页码 {page_num + 1}/{total_page} ---\n发送 '<' 或 '>' 翻页，或直接发送页码。超时30秒后自动结束。"

    _send(event, ctx, msg)
    return True


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )