"""
动态生活状态插件 - 从 AstrBot 迁移至 zgric_onebot11
===================================================

为 Bot 生成一天的生活时间线，并在每次 LLM 请求前注入当前时间段对应的状态。

功能：
- 按 generate_time 划分业务周期，每个周期生成一份完整生活状态
- 支持每日定时生成；缺少当前状态时，会在首次 LLM 请求或查询时自动补充生成
- 支持自然时段和具体时间区间，根据当前时间选择对应日程与穿搭
- 提供 /dls show /dls full /dls regen 指令
- 注册 get_full_dynamic_life_state LLM 工具

LLM 事件钩子说明：
- 本插件监听 llm_request 事件总线事件。
- llm_core 插件需要在 LLM 请求前 emit 该事件以使状态注入生效。
- 若 llm_core 未 emit 该事件，状态注入不会生效，但命令和定时生成仍可正常工作。
"""

import datetime
import os
import re
import threading
import zoneinfo

from plugins.dynamic_life_state.core.generator import Generator
from plugins.dynamic_life_state.core.injector import (
    build_fake_tool_call,
    build_injection_text,
    remove_fake_tool_call_from_context,
)
from plugins.dynamic_life_state.core.state import (
    DEFAULT_GENERATE_TIME,
    NATURAL_SLOT_NAMES,
    BusinessCycle,
    DataManager,
    LifeState,
    SlotMatch,
    build_business_cycle,
    find_slot_by_name,
    format_cycle,
    format_datetime,
    format_interval,
    parse_generate_time,
    resolve_business_cycle,
    resolve_entry_intervals,
    resolve_time_in_cycle,
    select_current_slot,
)

PLUGIN_NAME = "dynamic_life_state"

# 模块级全局状态
ctx = None
_logger = None
_timezone = None
_generate_time = DEFAULT_GENERATE_TIME
_data_mgr = None
_generator = None
_scheduler_lock = threading.Lock()
_scheduler_running = False

__plugin_meta__ = {
    "name": "动态生活状态",
    "version": "0.1.4",
    "author": "Hola-Gracias (zgric 迁移)",
    "desc": "为 Bot 生成一天的生活时间线，并在每次 LLM 请求前注入当前时间段对应的状态",
    "repo": "https://github.com/Hola-Gracias/astrbot_plugin_dynamic_life_state.git",
    "priority": 60,
}


# ══════════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════════


def _log(msg: str, level: str = "info"):
    """统一日志输出"""
    if _logger:
        log_method = getattr(_logger, level, _logger.info)
        log_method(f"[DynamicLifeState] {msg}")
    else:
        print(f"[DynamicLifeState] [{level.upper()}] {msg}")


def _get_data_dir() -> str:
    """获取插件数据存储目录（相对于框架根目录）"""
    framework_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(framework_root, "data", "plugin_data", PLUGIN_NAME)


def _cfg(key: str, default=None):
    """读取插件配置"""
    if ctx is None:
        return default
    return ctx.get_config(key, default)


def _reply(event, text: str):
    """统一回复：群聊回群、私聊回私"""
    if ctx is None:
        return
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )


def _now() -> datetime.datetime:
    """获取当前带时区的时间"""
    global _timezone
    return datetime.datetime.now(_timezone or zoneinfo.ZoneInfo("Asia/Shanghai"))


def _current_cycle(now: datetime.datetime | None = None) -> BusinessCycle:
    """解析当前时间所属的业务周期"""
    global _timezone, _generate_time
    return resolve_business_cycle(
        _generate_time,
        _timezone or zoneinfo.ZoneInfo("Asia/Shanghai"),
        now or _now(),
    )


def _cycle_for_date(business_date: str) -> BusinessCycle:
    """为指定业务日期构造周期"""
    global _timezone, _generate_time
    return build_business_cycle(
        business_date,
        _generate_time,
        _timezone or zoneinfo.ZoneInfo("Asia/Shanghai"),
    )


def _find_active_state(now: datetime.datetime) -> tuple[LifeState, BusinessCycle] | None:
    """查找覆盖当前时间的有效状态"""
    global _data_mgr
    if _data_mgr is None:
        return None
    state = _data_mgr.find_active(now)
    if state is None:
        return None
    return state, state.to_cycle()


def _select_current_state(
    now: datetime.datetime,
) -> tuple[LifeState | None, BusinessCycle]:
    """选择起点最新的已存周期，或返回需要生成的配置周期。"""
    global _data_mgr
    configured_cycle = _current_cycle(now)
    active = _find_active_state(now)
    if active is None:
        return None, configured_cycle
    state, frozen_cycle = active
    if frozen_cycle.start < configured_cycle.start:
        return None, configured_cycle
    return state, frozen_cycle


def _extract_args_after(message_str: str, command: str) -> str | None:
    """从消息中提取指定命令之后的整段剩余文本。"""
    text = message_str.lstrip("/").strip()
    text = re.sub(r"\s+", " ", text)
    prefix = f"{command} "
    idx = text.find(prefix)
    if idx == -1:
        return None
    remainder = text[idx + len(prefix):].strip()
    return remainder or None


# ══════════════════════════════════════════════════════════════════
#  会话过滤
# ══════════════════════════════════════════════════════════════════


def _unified_msg_origin(event) -> str:
    """从事件构造 unified_msg_origin（类似 AstrBot 的标识）。"""
    bot_name = getattr(event, "bot_name", "default")
    if event.is_group:
        return f"{bot_name}:group:{event.group_id}"
    return f"{bot_name}:private:{event.user_id}"


def _is_session_enabled(event) -> bool:
    """检查当前会话是否启用了动态生活状态功能。"""
    mode = str(_cfg("session_list_mode", "none")).strip()
    session_list: list[str] = list(_cfg("session_list", []) or [])

    if mode == "none":
        return True
    origin = _unified_msg_origin(event)
    if mode == "whitelist":
        return origin in session_list
    if mode == "blacklist":
        return origin not in session_list
    return True


# ══════════════════════════════════════════════════════════════════
#  注入方式降级
# ══════════════════════════════════════════════════════════════════


def _resolve_injection_method(configured: str) -> str:
    """对不兼容的模型做降级。Gemini 不支持 fake_tool_call。"""
    if configured != "fake_tool_call":
        return configured
    try:
        model = str(_cfg("model", "")).strip().lower()
        if "gemini" in model:
            _log("Gemini 不支持 fake_tool_call，降级为 extra_user_content_parts")
            return "extra_user_content_parts"
    except Exception:
        pass
    return configured


# ══════════════════════════════════════════════════════════════════
#  LLM 请求事件处理器
# ══════════════════════════════════════════════════════════════════


def _on_llm_request(payload: dict):
    """LLM 请求事件处理器 - 注入当前时段的生活状态。

    事件负载格式（由 llm_core 在 chat() 中 emit）：
        {
            "event": event_object,
            "contexts": [{"role": "...", "content": "..."}, ...],
            "conversation_id": "...",
        }
    """
    global _generator, _data_mgr

    event = payload.get("event")
    if event is None:
        return
    contexts = payload.get("contexts", [])

    # 会话过滤
    if not _is_session_enabled(event):
        return

    now = _now()
    data, cycle = _select_current_state(now)
    if data is None:
        if _generator is not None and _generator.is_generating:
            return  # 已有生成任务在跑，本轮跳过
        if _generator is not None:
            data = _generator.generate(cycle)

    if not data or data.status == "failed":
        return

    business_date = data.business_date

    # 选择当前时段
    current_match = select_current_slot(data.timeline, cycle, now)

    # 解析注入方式
    injection_method = str(
        _cfg("injection_mode", "extra_user_content_parts")
    )
    injection_method = _resolve_injection_method(injection_method)

    # 清理上次注入残留
    remove_fake_tool_call_from_context(contexts)

    # 执行注入
    if injection_method == "extra_user_content_parts":
        inject_text = build_injection_text(data, cycle, current_match, now)
        contexts.append({"role": "user", "content": inject_text})
        if _cfg("debug_mode", False):
            _log(f"注入内容:\n{inject_text}")

    elif injection_method == "fake_tool_call":
        fake_messages = build_fake_tool_call(data, cycle, current_match, now)
        contexts.extend(fake_messages)
        if _cfg("debug_mode", False):
            _log(
                f"注入内容(fake_tool_call):\n"
                f"{fake_messages[1]['content']}"
            )

    if _cfg("debug_mode", False):
        _log(
            f"注入方式={injection_method}, "
            f"业务日期={business_date}, "
            f"时段={current_match.entry.time if current_match else 'N/A'}"
        )


# ══════════════════════════════════════════════════════════════════
#  LLM Tool：按需查询完整状态
# ══════════════════════════════════════════════════════════════════


def get_full_dynamic_life_state(**kwargs):
    """获取 Bot 当前或指定业务日期的完整生活状态。

    注册为 LLM Tool，供模型在需要时调用。
    签名：get_full_dynamic_life_state(date: str = "") -> str
    """
    global _generator, _data_mgr, ctx

    now = _now()
    date = kwargs.get("date", "")

    if _generator is not None and _generator.is_generating:
        return "生活状态正在生成中，请稍后再试。"

    if date:
        date = date.strip()
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            return "日期格式错误，请使用 YYYY-MM-DD 格式，例如 2026-07-01。"
        target_str = date
        if _data_mgr is not None:
            data = _data_mgr.get_latest_for_business_date(target_str)
        else:
            data = None
    else:
        data, cycle = _select_current_state(now)
        if data is None:
            return "当前时间没有已生成的生活状态。"
        target_str = data.business_date

    if not data:
        return f"{target_str} 的生活状态尚未生成。"
    if data.status == "failed":
        return f"{target_str} 的生活状态生成失败。"
    if date:
        cycle = data.to_cycle()

    return _format_full_state(data, cycle)


# ══════════════════════════════════════════════════════════════════
#  状态格式化
# ══════════════════════════════════════════════════════════════════


def _format_match_intervals(match: SlotMatch) -> str:
    return "、".join(format_interval(interval) for interval in match.intervals)


def _format_current_state(
    state: LifeState,
    cycle: BusinessCycle,
    match: SlotMatch,
    *,
    natural_datetime: datetime.datetime | None = None,
    show_current: bool = True,
) -> str:
    time_label = "当前时段" if show_current else "时段"
    schedule_label = "当前安排" if show_current else "安排"
    outfit_label = "当前穿搭" if show_current else "穿搭"
    lines = [
        f" 业务日期：{state.business_date}",
        f" 状态周期：{format_cycle(cycle)}",
    ]
    if natural_datetime is not None:
        natural_label = "当前自然日期时间" if show_current else "自然日期时间"
        lines.append(f" {natural_label}：{format_datetime(natural_datetime)}")
    lines.extend(
        [
            f" {time_label}：{match.entry.time}",
            f"⌛ 时段范围：{_format_match_intervals(match) or '无'}",
            f" {schedule_label}：{match.entry.schedule}",
            f" {outfit_label}：{match.entry.outfit}",
            f"⏰ 实际生成时间：{state.generated_at or '未知'}",
        ]
    )
    return "\n".join(lines)


def _format_full_state(
    state: LifeState,
    cycle: BusinessCycle,
) -> str:
    lines = [
        f" 业务日期：{state.business_date}",
        f" 状态周期：{format_cycle(cycle)}",
        f" 周期概况：{state.schedule_summary or '无'}",
        f" 周期氛围：{state.style_summary or '无'}",
        f"⏰ 实际生成时间：{state.generated_at or '未知'}",
        "",
        " 完整时间线：",
    ]
    for entry in state.timeline:
        intervals = resolve_entry_intervals(entry, cycle)
        interval_text = "、".join(format_interval(item) for item in intervals)
        lines.append(
            f"  [{entry.time}] {interval_text or '无法解析'} | "
            f"{entry.schedule} |  {entry.outfit}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  命令处理
# ══════════════════════════════════════════════════════════════════


def handle_dls_show(event, match):
    """查看当前或指定时段状态。"""
    global _generator, _data_mgr

    # 从消息中提取参数
    time_query = ""
    msg = getattr(event, "message", "") or ""
    show_match = re.search(r"^/dls\s+show(?:\s+(.+))?$", msg.strip(), re.IGNORECASE)
    if show_match and show_match.group(1):
        time_query = show_match.group(1).strip()

    # 在加载/生成数据前校验，避免非法输入触发 LLM 调用
    if time_query:
        if time_query in NATURAL_SLOT_NAMES:
            pass
        elif ":" in time_query:
            if not re.fullmatch(r"\d{2}:\d{2}", time_query):
                _reply(event, "时间格式错误，请使用 HH:MM 格式 (00:00–23:59)。")
                return
            try:
                hour, minute = map(int, time_query.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                _reply(event, "时间格式错误，请使用 HH:MM 格式 (00:00–23:59)。")
                return
        else:
            allowed = "、".join(NATURAL_SLOT_NAMES)
            _reply(
                event,
                f"不支持的时段「{time_query}」，时段仅支持具体时间 (HH:MM) 或自然时段：{allowed}",
            )
            return

    now = _now()

    if _generator is not None and _generator.is_generating:
        _reply(event, "状态正在生成中，请稍后再试。")
        return

    data, cycle = _select_current_state(now)
    if data is None:
        _reply(event, "当前业务周期状态尚未生成，正在生成...")
        if _generator is not None:
            data = _generator.generate(cycle)

    if not data or data.status == "failed":
        _reply(event, "状态生成失败，请稍后再试或使用 /dls regen 重试。")
        return

    if not data.timeline:
        _reply(event, "没有可用的时段状态。")
        return

    # 无参数：当前时间
    if not time_query:
        match = select_current_slot(data.timeline, cycle, now)
        if match is None:
            _reply(event, "没有可用的时段状态。")
            return
        _reply(
            event,
            _format_current_state(
                data,
                cycle,
                match,
                natural_datetime=now,
            ),
        )
        return

    # 自然时段：直接选择该时段
    if time_query in NATURAL_SLOT_NAMES:
        match = find_slot_by_name(data.timeline, time_query, cycle)
        if match is None:
            _reply(event, "没有可用的时段状态。")
            return
        _reply(
            event,
            _format_current_state(
                data,
                cycle,
                match,
                show_current=False,
            ),
        )
        return

    # 具体时间：已在数据加载前校验
    query_datetime = resolve_time_in_cycle(cycle, time_query)
    match = select_current_slot(data.timeline, cycle, query_datetime)
    if match is None:
        _reply(event, "没有可用的时段状态。")
        return
    _reply(
        event,
        _format_current_state(
            data,
            cycle,
            match,
            natural_datetime=query_datetime,
            show_current=False,
        ),
    )


def handle_dls_full(event, match):
    """查看今日或指定日期的完整状态。"""
    global _generator, _data_mgr

    # 从消息中提取参数
    date_arg = ""
    msg = getattr(event, "message", "") or ""
    full_match = re.search(r"^/dls\s+full(?:\s+(.+))?$", msg.strip(), re.IGNORECASE)
    if full_match and full_match.group(1):
        date_arg = full_match.group(1).strip()

    now = _now()

    if date_arg:
        date_arg = date_arg.strip()
        try:
            datetime.date.fromisoformat(date_arg)
        except ValueError:
            _reply(event, "日期格式错误，请使用 YYYY-MM-DD 格式。")
            return
        if _data_mgr is not None:
            data = _data_mgr.get_latest_for_business_date(date_arg)
        else:
            data = None
        if not data:
            _reply(event, f"{date_arg} 的状态不存在。")
            return
        if data.status == "failed":
            _reply(event, f"{date_arg} 的状态生成失败，无有效状态。")
            return
        cycle = data.to_cycle()
        _reply(event, _format_full_state(data, cycle))
        return

    data, cycle = _select_current_state(now)
    if data is None:
        if _generator is not None and _generator.is_generating:
            _reply(event, "状态正在生成中，请稍后再试。")
            return
        _reply(event, "当前业务周期状态尚未生成，正在生成...")
        if _generator is not None:
            data = _generator.generate(cycle)

    if not data or data.status == "failed":
        _reply(event, "当前暂无有效状态。")
        return

    _reply(event, _format_full_state(data, cycle))


def handle_dls_regen(event, match):
    """强制重新生成今日状态，可附加额外要求。"""
    global _generator, _data_mgr

    if _generator is not None and _generator.is_generating:
        _reply(event, "已有生成任务在进行中，请稍后再试。")
        return

    # 从消息中手动提取额外要求
    msg = getattr(event, "message", "") or ""
    extra = _extract_args_after(msg, "dls regen")

    now = _now()
    _, cycle = _select_current_state(now)

    if extra:
        _reply(event, f"正在根据附加要求重新生成今日状态：{extra}")
    else:
        _reply(event, "正在强制重新生成今日状态...")

    if _generator is not None:
        data = _generator.generate(cycle, force=True, extra=extra)
    else:
        _reply(event, "生成器未初始化。")
        return

    if not data or data.status == "failed":
        _reply(event, "状态生成失败，请稍后再试。")
        return

    _reply(
        event,
        f"全局生活状态重新生成完成。\n\n{_format_full_state(data, cycle)}",
    )


# ══════════════════════════════════════════════════════════════════
#  定时生成任务
# ══════════════════════════════════════════════════════════════════


def _daily_generate():
    """每日定时生成任务。"""
    global _generator, _data_mgr, _timezone, _generate_time

    now = _now()
    data, cycle = _select_current_state(now)
    if data:
        _log(
            f"当前有效状态已属于最新周期 "
            f"{format_cycle(cycle)}，跳过生成"
        )
        return
    if _generator is not None:
        _generator.generate(cycle)


# ══════════════════════════════════════════════════════════════════
#  插件注册入口
# ══════════════════════════════════════════════════════════════════


def register(reg_ctx):
    """插件注册入口

    Args:
        reg_ctx: 框架注入的上下文对象
    """
    global ctx, _logger, _timezone, _generate_time, _data_mgr, _generator, _scheduler_running
    ctx = reg_ctx
    _logger = ctx.logger if hasattr(ctx, "logger") else None

    _log("动态生活状态插件正在加载...")

    # ── 解析配置 ──
    _timezone = _resolve_timezone()
    _generate_time = _resolve_generate_time()

    # ── 初始化数据管理器 ──
    state_file = os.path.join(_get_data_dir(), "life_state.json")
    _data_mgr = DataManager(
        type(state_file) if isinstance(state_file, str) else state_file,
        legacy_cycle_resolver=_cycle_for_date,
        logger=_logger,
    )

    # ── 初始化生成器 ──
    _generator = Generator(
        get_config_func=_cfg,
        data_mgr=_data_mgr,
        logger=_logger,
    )

    # ── 注册命令 ──
    ctx.command(
        r"^/dls\s+show(?:\s+.+)?$",
        handle_dls_show,
        priority=50,
        require_admin=True,
        description="查看当前或指定时段状态：/dls show [自然时段/HH:MM]",
    )
    ctx.command(
        r"^/dls\s+full(?:\s+.+)?$",
        handle_dls_full,
        priority=50,
        require_admin=True,
        description="查看今日或指定日期的完整状态：/dls full [YYYY-MM-DD]",
    )
    ctx.command(
        r"^/dls\s+regen(?:\s+.+)?$",
        handle_dls_regen,
        priority=50,
        require_admin=True,
        description="强制重新生成今日状态：/dls regen [额外要求]",
    )

    # ── 订阅 LLM 请求事件 ──
    try:
        ctx.on("llm_request", _on_llm_request)
        _log("已订阅 llm_request 事件，状态注入就绪")
    except Exception as e:
        _log(f"订阅 llm_request 事件失败: {e}", level="warning")

    # ── 注册 LLM 工具 ──
    _register_llm_tool()

    # ── 注册定时任务 ──
    try:
        boundary_time = parse_generate_time(_generate_time)
        cron_expr = f"{boundary_time.minute} {boundary_time.hour} * * *"
        ctx.task(
            cron_expr,
            _daily_generate,
            description="动态生活状态每日生成",
        )
        _scheduler_running = True
        _log(f"定时任务已注册，每日 {_generate_time} 生成")
    except Exception as e:
        _log(f"定时任务注册失败: {e}", level="warning")

    _log("动态生活状态插件加载完成")


def _register_llm_tool():
    """通过 llm_core 注册 LLM 工具。"""
    try:
        import sys
        llm_core = sys.modules.get("plugin_llm_core")
        if llm_core is not None and hasattr(llm_core, "register_tool"):
            llm_core.register_tool(
                plugin_name=PLUGIN_NAME,
                tool_name="get_full_dynamic_life_state",
                description=(
                    "获取 Bot 当前或指定业务日期的完整生活状态。"
                    "date 参数为可选，格式 YYYY-MM-DD，含义为业务日期。"
                    "不传则返回当前业务周期状态。"
                    "仅在用户明确询问'今天一整天在做什么''全天状态''完整日程'"
                    "'其他时间段的安排''回顾某天的状态'或类似问题时调用。"
                    "普通日常对话中不要调用此工具，LLM 请求时已有的当前时段状态已足够回答问题。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "业务日期，格式 YYYY-MM-DD，可选。不传则返回当前业务周期状态。",
                        }
                    },
                },
                handler=get_full_dynamic_life_state,
            )
            _log("已注册 get_full_dynamic_life_state LLM 工具")
        else:
            _log("未检测到 llm_core 插件，LLM 工具未注册", level="warning")
    except Exception as e:
        _log(f"注册 LLM 工具失败: {e}", level="warning")


# ══════════════════════════════════════════════════════════════════
#  配置解析
# ══════════════════════════════════════════════════════════════════


def _resolve_timezone() -> zoneinfo.ZoneInfo:
    """解析时区配置。"""
    tz_setting = str(_cfg("timezone", "Asia/Shanghai"))
    try:
        return zoneinfo.ZoneInfo(tz_setting)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        _log(f"无效时区 {tz_setting!r}，回退为 Asia/Shanghai", level="error")
        return zoneinfo.ZoneInfo("Asia/Shanghai")


def _resolve_generate_time() -> str:
    """解析生成时间配置。"""
    configured = str(_cfg("generate_time", DEFAULT_GENERATE_TIME)).strip()
    try:
        parsed = parse_generate_time(configured)
    except ValueError as exc:
        _log(
            f"无效 generate_time {configured!r}: {exc}; "
            f"回退为 {DEFAULT_GENERATE_TIME}",
            level="error",
        )
        return DEFAULT_GENERATE_TIME
    return parsed.strftime("%H:%M")


# ══════════════════════════════════════════════════════════════════
#  卸载清理
# ══════════════════════════════════════════════════════════════════


def on_unload():
    """插件卸载时清理全局状态。"""
    global _data_mgr, _generator, _scheduler_running, ctx

    _log("动态生活状态插件正在卸载...")

    _scheduler_running = False

    # 注销 LLM 工具
    try:
        import sys
        llm_core = sys.modules.get("plugin_llm_core")
        if llm_core is not None and hasattr(llm_core, "unregister_tool"):
            llm_core.unregister_tool("get_full_dynamic_life_state")
            _log("已注销 get_full_dynamic_life_state LLM 工具")
    except Exception as e:
        _log(f"注销 LLM 工具失败: {e}", level="warning")

    _data_mgr = None
    _generator = None
    _log("动态生活状态插件已卸载")