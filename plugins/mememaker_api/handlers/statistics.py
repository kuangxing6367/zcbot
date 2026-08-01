"""
统计处理器 - 表情调用统计功能
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import re
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from typing import List

from plugins.mememaker_api._shared import _api_client, _meme_manager, _recorder, _ctx
from plugins.mememaker_api.core.utils import image_to_base64_cq

logger = logging.getLogger(__name__)


async def handle_meme_stats(event, match):
    """处理表情调用统计"""
    ctx = _ctx
    arg_text = (match.group(1) or '').strip() if match else ''
    try:
        is_my, is_global, time_keyword, meme_name = False, False, None, None

        pattern = r"^(?:(我的|自己)\s*)?(?:(全局)\s*)?(日|24小时|1天|本日|今日|周|一周|7天|本周|月|30天|本月|月度|年|一年|本年|年度)?\s*表情(?:(?:调用|使用)?)?统计\s*(.*)$"
        match_obj = re.match(pattern, arg_text)

        if match_obj:
            my_group, global_group, time_group, meme_name_group = match_obj.groups()
            if my_group: is_my = True
            if global_group: is_global = True
            if time_group: time_keyword = time_group
            if meme_name_group: meme_name = meme_name_group.strip()
        else:
            params = arg_text.split()
            time_type_map_keys = ["日", "24小时", "1天", "本日", "今日", "周", "一周", "7天", "本周", "月", "30天", "本月", "月度", "年", "一年", "本年", "年度"]
            for param in params:
                if param in ["我的", "自己"]: is_my = True
                elif param == "全局": is_global = True
                elif param in time_type_map_keys: time_keyword = param
                else: meme_name = " ".join(params[params.index(param):]) if param != params[0] else param
            if meme_name is None and params:
                pass  # 无法确定

        time_type_map = {"日": "day", "本日": "day", "今日": "day", "24小时": "24h", "1天": "24h", "周": "week", "本周": "week", "7天": "7d", "月": "month", "本月": "month", "月度": "month", "30天": "30d", "年": "year", "本年": "year", "年度": "year", "一年": "1y"}
        time_type = time_type_map.get(time_keyword, "24h") if time_keyword else "24h"
        meme_info = _meme_manager.find_meme_by_keyword(meme_name) if meme_name else None
        now = datetime.now(timezone.utc)

        if time_type == "24h": start, td, fmt, humanized = now - timedelta(days=1), timedelta(hours=1), "%H:00", "24小时"
        elif time_type == "day": start, td, fmt, humanized = now.replace(hour=0, minute=0, second=0, microsecond=0), timedelta(hours=1), "%H:00", "本日"
        elif time_type == "week": start, td, fmt, humanized = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday()), timedelta(days=1), "%a", "本周"
        elif time_type == "30d": start, td, fmt, humanized = now - timedelta(days=30), timedelta(days=1), "%m/%d", "30天"
        elif time_type == "month": start, td, fmt, humanized = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), timedelta(days=1), "%m/%d", "本月"
        elif time_type == "1y": start, td, fmt, humanized = now - relativedelta(years=1), relativedelta(months=1), "%y/%m", "一年"
        else: start, td, fmt, humanized = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), relativedelta(months=1), "%b", "本年"

        query = "SELECT meme_key, timestamp FROM meme_usage_logs WHERE timestamp >= ?"
        params = [start]
        scope_text = ""
        if is_my and is_global:
            query += " AND user_id = ?"
            params.append(str(event.user_id))
            scope_text = "我的全局"
        elif is_my:
            query += " AND user_id = ? AND group_id = ?"
            params.extend([str(event.user_id), str(event.group_id) if event.group_id else "private"])
            scope_text = "我在本群"
        elif is_global:
            scope_text = "全局"
        else:
            query += " AND group_id = ?"
            params.append(str(event.group_id) if event.group_id else "private")
            scope_text = "本群"
        if meme_info:
            query += " AND meme_key = ?"
            params.append(meme_info.key)

        records = await _recorder.get_stats_records(query, tuple(params))
        if not records:
            _send(event, ctx, "该范围内没有找到任何表情调用记录。")
            return

        meme_keys = [rec[0] for rec in records]
        meme_times = [datetime.fromisoformat(rec[1]).replace(tzinfo=timezone.utc) for rec in records]
        meme_times.sort()
        time_counts: list = []
        stop = start + td
        count = 0
        key = start.strftime(fmt)
        for time_point in meme_times:
            while time_point >= stop:
                time_counts.append((key, count))
                key = stop.strftime(fmt)
                stop += td
                count = 0
            count += 1
        time_counts.append((key, count))
        while stop <= now:
            key = stop.strftime(fmt)
            stop += td
            time_counts.append((key, 0))

        key_counts = Counter(meme_keys)

        _send(event, ctx, "正在生成统计图，请稍候...")

        if meme_info:
            title = f""{meme_info.key}"{scope_text}{humanized}调用统计 (总计: {len(records)})"
            chart_data = await _api_client.render_statistics(title, "time_count", time_counts)
            _send(event, ctx, image_to_base64_cq(chart_data))
        else:
            title = f"{scope_text}{humanized}表情调用统计 (总计: {len(records)})"
            meme_counts = sorted(key_counts.items(), key=lambda item: item[1], reverse=True)
            meme_counts_with_keywords = []
            for key, num in meme_counts[:15]:
                meme = _meme_manager.find_meme_by_keyword(key)
                display_name = (meme.keywords[0] if meme and meme.keywords else key)
                meme_counts_with_keywords.append((display_name, num))
            meme_chart_data = await _api_client.render_statistics(title, "meme_count", meme_counts_with_keywords)
            time_chart_data = await _api_client.render_statistics(title, "time_count", time_counts)
            _send(event, ctx, image_to_base64_cq(meme_chart_data) + "\n" + image_to_base64_cq(time_chart_data))

    except Exception as e:
        logger.error(f"生成统计图失败: {e}", exc_info=True)
        _send(event, ctx, "生成统计图失败了，呜呜...")


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )