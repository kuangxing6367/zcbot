import datetime
import json
import re
import threading
from pathlib import Path

import requests

from plugins.dynamic_life_state.core.state import (
    MAX_HISTORY_DAYS,
    BusinessCycle,
    DataManager,
    LifeState,
    TimelineEntry,
    is_valid_time_slot,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def _render_template(template: str, **kwargs: str) -> str:
    """安全渲染模板：只替换已知占位符，其他花括号原样保留。"""
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: kwargs.get(match.group(1), match.group(0)),
        template,
    )


def _load_default_prompt_template() -> str:
    """从配置 schema 读取 prompt_template 的默认值。"""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    template = schema.get("prompt_template", {}).get("default")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("_conf_schema.json 中缺少有效的 prompt_template.default")
    return template


class Generator:
    def __init__(
        self,
        get_config_func,
        data_mgr: DataManager,
        logger=None,
    ):
        self._get_config = get_config_func
        self.data_mgr = data_mgr
        self._logger = logger
        self._gen_lock = threading.Lock()

    @property
    def is_generating(self) -> bool:
        return self._gen_lock.locked()

    def _log(self, msg: str, level: str = "info"):
        if self._logger:
            log_method = getattr(self._logger, level, self._logger.info)
            log_method(f"[DynamicLifeState] {msg}")

    def _get_prompt_template(self) -> str:
        configured = self._get_config("prompt_template", "")
        if isinstance(configured, str) and configured.strip():
            return configured
        self._log("prompt_template 为空，使用配置 schema 默认提示词", level="warning")
        return _load_default_prompt_template()

    def _get_system_prompt(self) -> str:
        """读取 system_prompt 配置，用于状态生成时作为角色设定。"""
        return str(self._get_config("system_prompt", "")).strip()

    def _get_api_config(self) -> dict:
        """读取生成状态时使用的 LLM API 配置。

        优先使用本插件配置，若未设置则尝试从 llm_core 插件读取。
        """
        api_base = str(self._get_config("api_base", "")).strip()
        api_key = str(self._get_config("api_key", "")).strip()
        model = str(self._get_config("model", "")).strip()

        # 如果本插件未配置，尝试从 llm_core 读取
        if not api_base or not api_key or not model:
            try:
                import sys
                llm_core = sys.modules.get("plugin_llm_core")
                if llm_core is not None and hasattr(llm_core, "chat_engine"):
                    cfg = llm_core.chat_engine._config()
                    if not api_base:
                        api_base = cfg.get("api_base", "")
                    if not api_key:
                        api_key = cfg.get("api_key", "")
                    if not model:
                        model = cfg.get("model", "")
            except Exception:
                pass

        return {
            "api_base": api_base or "https://api.openai.com/v1",
            "api_key": api_key or "",
            "model": model or "gpt-3.5-turbo",
        }

    @staticmethod
    def _format_history(states: list[LifeState]) -> str:
        """将历史状态渲染为生成提示词使用的文本。"""
        if not states:
            return "无"

        sections: list[str] = []
        for state in states:
            lines = [
                f"[{state.business_date}]",
                f"整体日程：{state.schedule_summary or '无'}",
                f"穿搭风格：{state.style_summary or '无'}",
                "时间线：",
            ]
            for entry in state.timeline:
                lines.extend(
                    [
                        f"- {entry.time}：{entry.schedule or '无'}",
                        f"  - 穿搭：{entry.outfit or '无'}",
                    ]
                )
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def generate(
        self,
        cycle: BusinessCycle,
        force: bool = False,
        extra: str | None = None,
    ) -> LifeState:
        """生成并持久化指定业务周期的生活状态（同步）。"""
        with self._gen_lock:
            business_date = cycle.business_date.isoformat()

            # 二次检查：可能在等锁期间已被另一个任务生成（force 时跳过）
            if not force:
                existing = self.data_mgr.get_by_cycle(cycle)
                if existing and existing.status == "ok":
                    return existing

            debug = bool(self._get_config("debug_mode", False))

            try:
                self._log(f"正在生成业务日期 {business_date} 的生活状态...")

                self.data_mgr.archive_before_generation(business_date)
                try:
                    history_days = int(self._get_config("history_reference_days", 3))
                except (TypeError, ValueError):
                    history_days = 3
                history_days = max(0, min(history_days, MAX_HISTORY_DAYS))
                history_states = self.data_mgr.get_recent_history(
                    business_date,
                    history_days,
                )
                history_text = self._format_history(history_states)
                extra_text = (extra or "").strip() or "无"

            except Exception as e:
                self._log(
                    f"生成准备失败，保留现有状态 "
                    f"(业务日期 {business_date}): {e}",
                    level="error",
                )
                return LifeState.from_cycle(
                    cycle,
                    status="failed",
                    generated_at=datetime.datetime.now(cycle.start.tzinfo).isoformat(),
                )

            try:
                persona = self._get_system_prompt()
                prompt = _render_template(
                    self._get_prompt_template(),
                    business_date=business_date,
                    cycle_start=cycle.start.isoformat(timespec="minutes"),
                    cycle_end=cycle.end.isoformat(timespec="minutes"),
                    persona=persona,
                    history_states=history_text,
                    extra_requirements=extra_text,
                )

                if debug:
                    self._log(f"生成 prompt:\n{prompt}")

                api_cfg = self._get_api_config()
                if not api_cfg["api_key"]:
                    raise RuntimeError("API Key 未配置，无法执行 LLM 生成")

                resp_text = self._call_llm(prompt, api_cfg)

                if debug:
                    self._log(f"模型原始返回:\n{resp_text}")

                payload = self._extract_json(resp_text)
                generated_at = datetime.datetime.now(cycle.start.tzinfo).isoformat()
                state = self._validate_and_build(
                    payload,
                    cycle,
                    generated_at,
                )

            except Exception as e:
                self._log(
                    f"生成失败 (业务日期 {business_date}): {e}",
                    level="error",
                )
                failed = LifeState.from_cycle(
                    cycle,
                    status="failed",
                    generated_at=datetime.datetime.now(cycle.start.tzinfo).isoformat(),
                )
                try:
                    self.data_mgr.set(failed)
                except Exception as save_error:
                    self._log(
                        f"失败状态保存失败 "
                        f"(业务日期 {business_date}): {save_error}",
                        level="error",
                    )
                return failed

            try:
                self.data_mgr.set(state)
            except Exception as e:
                self._log(
                    f"生成结果保存失败 "
                    f"(业务日期 {business_date}): {e}",
                    level="error",
                )
                return LifeState.from_cycle(
                    cycle,
                    status="failed",
                    generated_at=datetime.datetime.now(cycle.start.tzinfo).isoformat(),
                )

            if debug:
                self._log(
                    f"解析后的状态:\n"
                    f"{json.dumps(self._state_to_dict(state), ensure_ascii=False, indent=2)}"
                )

            self._log(f"业务日期 {business_date} 生活状态生成成功")
            return state

    # ---------- LLM API 调用 ----------

    def _call_llm(self, prompt: str, api_cfg: dict) -> str:
        """调用 OpenAI 兼容接口生成状态文本。"""
        url = api_cfg["api_base"].rstrip("/") + "/chat/completions"

        headers = {
            "Content-Type": "application/json",
        }
        if api_cfg["api_key"]:
            headers["Authorization"] = f"Bearer {api_cfg['api_key']}"

        system_prompt = self._get_system_prompt()
        messages = [
            {"role": "system", "content": system_prompt or "你是一个智能助手。"},
            {"role": "user", "content": prompt},
        ]

        payload = {
            "model": api_cfg["model"],
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.8,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            raise RuntimeError("LLM 请求超时")
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = resp.text[:500]
            except Exception:
                pass
            raise RuntimeError(f"LLM HTTP 错误: {e} | body={body}")
        except Exception as e:
            raise RuntimeError(f"LLM 调用异常: {e}")

        return self._extract_completion_text(data)

    @staticmethod
    def _extract_completion_text(data: dict) -> str:
        """从 OpenAI 响应中提取 completion_text。"""
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        return content.strip() if isinstance(content, str) else ""

    # ---------- JSON parse ----------

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        text = text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

        start = text.find("{")
        if start == -1:
            return None

        brace = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                            return data if isinstance(data, dict) else None
                        except Exception:
                            return None
        return None

    # ---------- validate ----------

    @staticmethod
    def _validate_and_build(
        payload: dict | None,
        cycle: BusinessCycle,
        generated_at: str,
    ) -> LifeState:
        if not payload:
            raise ValueError("未能从模型输出中解析出 JSON 对象")

        # 基础校验
        business_date = cycle.business_date.isoformat()
        business_date_val = payload.get("business_date")
        if business_date_val != business_date:
            raise ValueError(
                "business_date 字段必须与目标业务日期一致: "
                f"expected={business_date}, actual={business_date_val}"
            )

        timeline_raw = payload.get("timeline")
        if not isinstance(timeline_raw, list) or len(timeline_raw) == 0:
            raise ValueError("timeline 字段缺失或为空列表")

        entries: list[TimelineEntry] = []
        for item in timeline_raw:
            if not isinstance(item, dict):
                continue
            time_val = item.get("time")
            schedule_val = item.get("schedule")
            outfit_val = item.get("outfit")
            if not time_val or not schedule_val or not outfit_val:
                continue
            if not is_valid_time_slot(str(time_val)):
                continue
            entries.append(
                TimelineEntry(
                    time=str(time_val),
                    schedule=str(schedule_val),
                    outfit=str(outfit_val),
                )
            )

        if not entries:
            raise ValueError(
                "timeline 中没有有效条目（每项需要可解析的 time 以及 schedule/outfit）"
            )

        return LifeState.from_cycle(
            cycle,
            schedule_summary=str(payload.get("schedule_summary", "")),
            style_summary=str(payload.get("style_summary", "")),
            timeline=entries,
            status="ok",
            generated_at=generated_at,
        )

    @staticmethod
    def _state_to_dict(state: LifeState) -> dict:
        return {
            "business_date": state.business_date,
            "cycle_start": state.cycle_start,
            "cycle_end": state.cycle_end,
            "timezone": state.timezone,
            "schedule_summary": state.schedule_summary,
            "style_summary": state.style_summary,
            "timeline": [
                {"time": e.time, "schedule": e.schedule, "outfit": e.outfit}
                for e in state.timeline
            ],
            "status": state.status,
            "generated_at": state.generated_at,
        }