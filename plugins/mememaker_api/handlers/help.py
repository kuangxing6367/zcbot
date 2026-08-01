"""
帮助处理器 - 表情列表显示
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict

from plugins.mememaker_api._shared import _api_client, _meme_manager, _recorder, _config, _ctx
from plugins.mememaker_api.core.utils import image_to_base64_cq

logger = logging.getLogger(__name__)


async def handle_meme_list(event, match):
    """生成并发送动态表情包列表图"""
    ctx = _ctx
    try:
        _send(event, ctx, "正在生成动态列表，请稍候...")

        start_time = datetime.now(timezone.utc) - timedelta(days=_config["label_hot_days"])
        recent_meme_keys = await _recorder.get_recent_meme_keys(start_time)
        hot_counts = Counter(recent_meme_keys)

        meme_properties: Dict[str, Dict[str, bool]] = {}
        now_utc = datetime.now(timezone.utc)
        new_timedelta = timedelta(days=_config["label_new_days"])

        for meme in _meme_manager.meme_infos.values():
            try:
                is_new = (now_utc - meme.date_created) < new_timedelta
            except (ValueError, TypeError):
                is_new = False

            is_hot = hot_counts.get(meme.key, 0) >= _config["label_hot_threshold"]
            is_disabled = await _recorder.is_meme_disabled(meme.key, str(event.group_id) if event.group_id else None)
            meme_properties[meme.key] = {"new": is_new, "hot": is_hot, "disabled": is_disabled}

        image_data = await _api_client.render_list_image(meme_properties)

        A_text = '触发："-关键词 [文] [@人] [--选项]"\n'
        B_text = "-表情详情 <关键词> | -表情搜索 <关键词>\n"
        full_text = A_text + B_text

        image_cq = image_to_base64_cq(image_data)
        _send(event, ctx, full_text + image_cq)

    except Exception as e:
        logger.error(f"生成动态表情列表图失败: {e}", exc_info=True)
        _send(event, ctx, "生成列表图失败了，呜呜...")


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )