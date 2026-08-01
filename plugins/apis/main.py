"""
API 聚合插件主入口（zgric_onebot11 框架适配版）
==============================================

复刻自 astrbot_plugin_apis，1:1 完整迁移。

功能：
- 提供预设的 API 聚合服务，支持文本、图片、视频、音频类型
- /查看api [名称]         查看API列表或详情
- 自动匹配消息中的API关键词并返回数据
- LLM 工具：search_image / list_image_apis / call_api
"""
import asyncio
import base64
import json
import os
import sys
import threading
from typing import Any

from plugins.apis.api_aggregator import APICoreApp, DataResource
from plugins.apis.config import PluginConfig
from plugins.apis.page_controller import APIPageController
from plugins.apis.utils import get_nickname, get_reply_text

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

_PLUGIN_NAME = "apis"

__plugin_meta__ = {
    "name": "API聚合",
    "version": "1.0.0",
    "author": "zgric",
    "desc": "预设API聚合服务，支持文本/图片/视频/音频类型",
    "priority": 20,
}

# 模块级实例
_core: APICoreApp | None = None
_cfg: PluginConfig | None = None
_page_controller: APIPageController | None = None
_ctx = None
_started = False
_message_subscribed = False

# 后台事件循环（用于执行异步操作）
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


# ====================================================================
#  后台事件循环管理
# ====================================================================

def _ensure_loop() -> asyncio.AbstractEventLoop:
    """确保后台事件循环正在运行，返回事件循环引用"""
    global _loop, _loop_thread
    if _loop is None or (_loop_thread and not _loop_thread.is_alive()):
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True, name="apis-async-loop")
        _loop_thread.start()
    return _loop


def _run_async(coro) -> Any:
    """在后台事件循环中执行异步协程，阻塞等待结果"""
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def _stop_loop():
    """停止后台事件循环"""
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
        if _loop_thread and _loop_thread.is_alive():
            _loop_thread.join(timeout=3)
    _loop = None
    _loop_thread = None


# ====================================================================
#  插件入口/出口
# ====================================================================

def register(ctx):
    """插件注册入口"""
    global _core, _cfg, _page_controller, _ctx, _started, _message_subscribed

    _ctx = ctx

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = _get_data_dir(ctx)

    # 初始化配置
    _cfg = PluginConfig(plugin_dir, data_dir, ctx)

    # 初始化核心应用
    _core = APICoreApp(_cfg)

    # 启动核心服务（同步执行异步启动）
    _start_core()

    # 加载预设
    _load_presets()

    # 获取 Flask 应用并注册页面路由
    flask_app = _get_flask_app(ctx)
    _page_controller = APIPageController(_core)
    _page_controller.register_routes(flask_app)

    # 注册命令
    ctx.command("查看api|查看api列表|api列表", handle_api_detail, priority=20,
                description="查看API列表或详情，可指定API名称")

    # 订阅消息事件做API匹配（避免重复订阅）
    if not _message_subscribed:
        ctx.on("message", on_message)
        _message_subscribed = True

    # 注册LLM工具
    _register_llm_tools()

    ctx.log("API聚合插件已加载")


def on_unload():
    """插件卸载"""
    global _started, _message_subscribed
    _message_subscribed = False
    if _core is not None:
        try:
            _run_async(_core.stop())
        except Exception as e:
            _ctx and _ctx.log(f"停止核心服务异常: {e}", level="warning")
    _started = False
    _unregister_llm_tools()
    _stop_loop()
    _ctx and _ctx.log("API聚合插件已卸载")


# ====================================================================
#  辅助函数
# ====================================================================

def _get_data_dir(ctx) -> str:
    """获取插件数据目录"""
    try:
        base = ctx.get_config("data_dir", "data/plugins")
    except Exception:
        base = "data/plugins"
    return os.path.join(base, "apis")


def _get_flask_app(ctx):
    """获取 Flask 应用实例"""
    try:
        return ctx._framework.web.app
    except AttributeError:
        try:
            from framework.web import create_web_app
            # 创建临时的 Flask 应用（仅用于路由注册）
            return None
        except ImportError:
            return None


def _start_core():
    """同步启动核心服务"""
    global _started
    if _started or _core is None:
        return
    try:
        _run_async(_core.start())
        _started = True
    except Exception as e:
        _ctx and _ctx.log(f"启动核心服务失败: {e}", level="warning")


def _load_presets():
    """加载预设API池（从 presets/ 目录加载默认JSON）"""
    if _core is None:
        return
    try:
        if not _core.site_mgr.entries:
            _core.load_site_pool_from_file(_cfg.site_pool_file)
        if not _core.api_mgr.entries:
            _core.load_api_pool_from_file(_cfg.api_pool_file)
    except Exception as e:
        _ctx and _ctx.log(f"加载预设失败: {e}", level="warning")


def _send_msg(event, message: str):
    """发送消息（群聊回群，私聊回私）"""
    if _ctx is None:
        return
    _ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=message,
    )


def data_to_comp(data: DataResource) -> str:
    """将 DataResource 转换为 CQ 码字符串

    替代原 AstrBot 的 Comp.* 消息组件系统。
    """
    data_type = data.data_type
    if data_type.is_text and data.final_text:
        return data.final_text

    if data_type.is_image:
        if data.saved_path:
            path_str = str(data.saved_path).replace("\\", "/")
            return f"[CQ:image,file=file:///{path_str}]"
        if data.binary:
            encoded = base64.b64encode(data.binary).decode("utf-8")
            return f"[CQ:image,file=base64://{encoded}]"
        raise ValueError("missing image payload")

    if data_type.is_video:
        if data.saved_path:
            path_str = str(data.saved_path).replace("\\", "/")
            return f"[CQ:video,file=file:///{path_str}]"
        raise ValueError("missing video payload")

    if data_type.is_audio:
        if data.saved_path:
            path_str = str(data.saved_path).replace("\\", "/")
            return f"[CQ:record,file=file:///{path_str}]"
        if data.binary:
            encoded = base64.b64encode(data.binary).decode("utf-8")
            return f"[CQ:record,file=base64://{encoded}]"
        raise ValueError("missing audio payload")

    raise ValueError(f"unsupported data type: {data.data_type}")


def _build_params(event, entry, args: list[str]) -> dict[str, Any]:
    """构建API请求参数

    从消息参数、回复文本、用户昵称中依次填充API参数。
    1) 先用消息中的参数填充空值参数
    2) 再用剩余参数按顺序覆盖
    3) 最后用回复文本/用户昵称填充剩余空值
    """
    params = entry.params or {}
    keys = list(params.keys())
    updated_params = dict(params)
    if not keys:
        return updated_params

    def is_empty(value: Any) -> bool:
        return value is None or (isinstance(value, str) and value.strip() == "")

    remaining_args = [value for value in args if value not in (None, "")]

    # 1) Fill empty params first.
    if remaining_args:
        for key in keys:
            if not remaining_args:
                break
            if is_empty(updated_params.get(key)):
                updated_params[key] = remaining_args.pop(0)

    # 2) Force overwrite in param order with leftover args.
    if remaining_args:
        for i, value in enumerate(remaining_args):
            if i >= len(keys):
                break
            updated_params[keys[i]] = value

    if not any(is_empty(updated_params.get(key)) for key in keys):
        return updated_params

    extra_args: list[str] = []
    reply_text = get_reply_text(event)
    if reply_text:
        extra_args = [item for item in reply_text.strip().split() if item]

    if not extra_args:
        sender_id = str(event.user_id or "")
        if sender_id:
            nickname = get_nickname(event, sender_id)
            if nickname:
                extra_args = [nickname]

    # 3) Fill remaining empty params from reply/nickname fallback.
    for value in extra_args:
        if value in (None, ""):
            continue
        for key in keys:
            if is_empty(updated_params.get(key)):
                updated_params[key] = value
                break
        else:
            break

    return updated_params


# ====================================================================
#  命令处理
# ====================================================================

def handle_api_detail(event, match):
    """查看API列表或详情

    用法：/查看api [名称]
    - 不指定名称时列出所有API
    - 指定名称时显示API详情
    """
    if _core is None:
        _send_msg(event, "API 聚合服务未初始化")
        return

    api_name = None
    if match and match.groups():
        api_name = (match.group(1) or "").strip()
    if not api_name:
        # 尝试从消息中提取参数
        parts = event.message.split(maxsplit=1)
        if len(parts) > 1:
            api_name = parts[1].strip()

    if api_name:
        entry = _core.api_mgr.get_entry(api_name)
        if entry:
            msg = json.dumps(entry.to_dict(), ensure_ascii=False, indent=2)
            _send_msg(event, msg)
            return

    display = _core.api_mgr.display_entries()
    _send_msg(event, display)


# ====================================================================
#  API 消息匹配
# ====================================================================

def on_message(event, match=None):
    """被动消息处理：匹配API关键词并返回数据

    当消息匹配已注册的API条目时，自动获取数据并返回。
    需要前缀时，仅响应@机器人的消息。
    """
    if _core is None or _cfg is None:
        return

    # 需要前缀时仅响应@消息
    if _cfg.need_prefix and not event.has_at_bot:
        return

    msg = event.message or ""
    if not msg:
        return

    parts = msg.split()
    cmd = parts[0]
    args = parts[1:]

    # 获取用户/群信息
    user_id = event.user_id
    group_id = event.group_id if event.is_group else 0
    session_id = f"{user_id}_{group_id}"
    is_admin = event.is_admin

    # 匹配API条目
    entries = _core.api_mgr.match_entries(
        cmd,
        user_id=user_id,
        group_id=group_id,
        session_id=session_id,
        is_admin=is_admin,
    )
    if not entries:
        return

    # 标记消息已被消费（停止传播）
    event.stop_event()

    for entry in entries:
        entry.updated_params = _build_params(event, entry, args)
        try:
            data = _run_async(
                _core.data_service.fetch(entry, use_local=_cfg.use_local)
            )
        except Exception as exc:
            _ctx and _ctx.log(f"数据获取失败 [{entry.name}]: {exc}", level="warning")
            continue

        if data is None:
            continue

        try:
            comp = data_to_comp(data)
        except Exception as exc:
            _ctx and _ctx.log(f"数据转换失败: {exc}", level="warning")
            continue

        _send_msg(event, comp)

        if not _cfg.save_data:
            try:
                data.unlink()
            except Exception:
                pass


# ====================================================================
#  LLM 工具注册
# ====================================================================

def _register_llm_tools():
    """通过 sys.modules 获取 llm_core 模块，注册LLM工具"""
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is None:
        _ctx and _ctx.log("llm_core 未加载，跳过 LLM 工具注册", level="info")
        return

    try:
        llm_core.register_tool(
            plugin_name=_PLUGIN_NAME,
            tool_name="search_image",
            description="搜索并发送图片。根据关键词从预设图库中搜索图片并发送给用户。"
                        "支持的关键词：壁纸、头像、动漫、帅哥、美女、猫咪、风景、美食等",
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如壁纸、头像、动漫、帅哥、美女",
                    }
                },
                "required": ["keyword"],
            },
            handler=tool_search_image,
        )
        llm_core.register_tool(
            plugin_name=_PLUGIN_NAME,
            tool_name="list_image_apis",
            description="列出所有可用的图片类API名称和关键词，供用户选择",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=tool_list_image_apis,
        )
        llm_core.register_tool(
            plugin_name=_PLUGIN_NAME,
            tool_name="call_api",
            description="调用指定的预设API获取数据。支持文本、图片、视频、音频类型。"
                        "先用list_image_apis或查看api列表命令获取可用API名称，然后调用此工具获取数据",
            parameters={
                "type": "object",
                "properties": {
                    "api_name": {
                        "type": "string",
                        "description": "API名称，如搜图、高清壁纸、今日运势",
                    },
                    "params": {
                        "type": "string",
                        "description": "JSON格式参数（可选），如 '{\"nr\": \"猫咪\"}'",
                    },
                },
                "required": ["api_name"],
            },
            handler=tool_call_api,
        )
        _ctx and _ctx.log(f"已注册 3 个 LLM 工具（{_PLUGIN_NAME}）")
    except Exception as e:
        _ctx and _ctx.log(f"注册 LLM 工具失败: {e}", level="warning")


def _unregister_llm_tools():
    """反注册 LLM 工具"""
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is not None:
        try:
            unregister = getattr(llm_core, "unregister_plugin_tools", None)
            if unregister:
                unregister(_PLUGIN_NAME)
        except Exception as e:
            _ctx and _ctx.log(f"反注册 LLM 工具失败: {e}", level="warning")


def _get_current_event():
    """获取当前对话事件上下文（由 llm_core 在工具调用前注入）"""
    llm_core = sys.modules.get("plugin_llm_core")
    if llm_core is not None:
        getter = getattr(llm_core, "get_current_event", None)
        if not callable(getter):
            tools_mod = getattr(llm_core, "_tools_module", None)
            getter = getattr(tools_mod, "get_current_event", None) if tools_mod else None
        if callable(getter):
            ev = getter()
            if ev is not None:
                return ev
    return None


# ====================================================================
#  LLM 工具处理函数
# ====================================================================

def tool_search_image(**kwargs) -> str:
    """LLM 工具：搜索并发送图片"""
    ev = _get_current_event()
    if ev is None:
        return "未获取到当前对话上下文"

    keyword = kwargs.get("keyword", "")
    if not keyword:
        return "请提供搜索关键词，如：壁纸、头像、动漫、帅哥、美女"

    if _core is None:
        return "API聚合服务未初始化"

    # 查找匹配的图片类API
    image_entries = []
    for entry in _core.api_mgr.entries:
        if not entry.enabled or not entry.valid:
            continue
        if entry.type != "image":
            continue
        for kw in entry.keywords:
            if keyword.lower() in kw.lower() or kw.lower() in keyword.lower():
                image_entries.append(entry)
                break

    if not image_entries:
        return f"未找到与'{keyword}'匹配的图片API"

    import random
    entry = random.choice(image_entries)
    _ctx and _ctx.log(f"[apis] LLM搜索图片: {keyword} -> {entry.name}")

    try:
        entry.updated_params = dict(entry.params)
        data = _run_async(
            _core.data_service.fetch(entry, use_local=_cfg.use_local)
        )
    except Exception as e:
        return f"图片获取失败: {e}"

    if data is None:
        return "图片获取失败"

    try:
        comp = data_to_comp(data)
    except Exception as e:
        return f"图片处理失败: {e}"

    # 直接发送图片
    _send_msg(ev, comp)

    if not _cfg.save_data:
        try:
            data.unlink()
        except Exception:
            pass

    return f"已发送【{keyword}】图片，来自: {entry.name}"


def tool_list_image_apis(**kwargs) -> str:
    """LLM 工具：列出所有可用的图片类API"""
    if _core is None:
        return "API聚合服务未初始化"

    image_apis = []
    for entry in _core.api_mgr.entries:
        if not entry.enabled or not entry.valid:
            continue
        if entry.type == "image":
            image_apis.append({
                "name": entry.name,
                "keywords": entry.keywords,
            })

    if not image_apis:
        return "当前没有可用的图片API"

    lines = [f"【可用图片API ({len(image_apis)}个)】"]
    for api in image_apis:
        kws = ", ".join(api["keywords"][:3])
        lines.append(f"- {api['name']}: {kws}")
    return "\n".join(lines)


def tool_call_api(**kwargs) -> str:
    """LLM 工具：调用指定的预设API"""
    ev = _get_current_event()
    if ev is None:
        return "未获取到当前对话上下文"

    if _core is None:
        return "API聚合服务未初始化"

    api_name = kwargs.get("api_name", "")
    params_str = kwargs.get("params", "")

    if not api_name:
        return "请提供API名称"

    entry = _core.api_mgr.get_entry(api_name)
    if not entry:
        return f"未找到API: {api_name}"

    if not entry.enabled:
        return f"API '{api_name}' 已禁用"

    extra_params = {}
    if params_str:
        try:
            extra_params = json.loads(params_str)
        except json.JSONDecodeError:
            return f"参数JSON格式错误: {params_str}"

    entry.updated_params = {**entry.params, **extra_params}
    _ctx and _ctx.log(f"[apis] LLM调用API: {api_name}")

    try:
        data = _run_async(
            _core.data_service.fetch(entry, use_local=_cfg.use_local)
        )
    except Exception as e:
        return f"API调用失败: {e}"

    if data is None:
        return "API返回为空"

    try:
        comp = data_to_comp(data)
    except Exception as e:
        return f"数据处理失败: {e}"

    if data.data_type.is_text:
        result = data.final_text or comp
    else:
        # 非文本类型（图片/视频/音频）直接发送
        _send_msg(ev, comp)
        result = f"已发送{data.data_type.value}，来自: {entry.name}"

    if not _cfg.save_data:
        try:
            data.unlink()
        except Exception:
            pass

    return result