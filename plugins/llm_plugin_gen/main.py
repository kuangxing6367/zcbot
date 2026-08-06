"""
LLM 开发助手插件
================================
通过 OpenAI 兼容接口让 LLM 编写插件、管理插件文件，支持自定义人格与 skills。

核心能力：
  1. /ai <指令>      — 自然对话，LLM 通过函数调用（ls/write/edit/rm/load_plugin 等）写插件并加载
  2. /pluginlist     — 查看已加载插件

本插件为全异步实现（async handler + httpx 异步 HTTP），不占用框架线程池，
避免同步阻塞导致机器人假死。

配置项（_conf_schema.json，Web UI 可改）：
  base_url / api_key / model / temperature / max_tokens
  persona / skills / cwd

权限说明：命令仅超管可用；文件操作不受路径限制（超管自负其责）。
"""
import ast
import asyncio
import json
import os
import re
import traceback

import httpx

__plugin_meta__ = {
    "name": "LLM 开发助手",
    "version": "1.4.0",
    "author": "ZGRIC",
    "desc": "通过 OpenAI 兼容接口让 LLM 编写插件并管理插件文件（支持人格/skills/函数调用）",
    "priority": 100,
}

# 默认人格
_DEFAULT_PERSONA = "你是 ZCBOT OneBot QQ 机器人框架的插件开发专家，擅长编写高质量、健壮的 Python 插件。"

# 插件开发提示（精简；完整文档索引在 docs/INDEX.md，需要时用 ls 读取）
_PLUGIN_DEV_GUIDE = """\
## 插件开发提示
- 完整开发文档索引：`plugins/llm_plugin_gen/docs/INDEX.md`，编写插件前先 ls 读取索引，再按需 ls 对应文档
- 写完代码后务必调用 load_plugin 加载；失败则 ls/edit/reload_plugin 修复
"""

# 默认 skills（可从配置覆盖）
_DEFAULT_SKILLS = [
    "生成插件：根据用户需求生成符合框架规范的插件代码",
    "修改插件：阅读现有插件代码后修复 Bug / 增加功能",
    "文件操作：使用 ls/write/edit/rm 管理插件目录文件",
]


# 函数调用工具定义
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "列出指定路径下的文件和目录（支持通配符，如 plugins/*.py）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录或文件路径，相对 cwd 或绝对路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "写入/新建文件（覆盖原内容）。用于创建新插件文件或重写文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，如 plugins/my_plugin/main.py"},
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "修改已有文件：把 old_text 替换为 new_text（首次出现处）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文（须精确匹配）"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rm",
            "description": "删除文件或目录（目录需为空，或递归删除目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件/目录路径"},
                    "recursive": {"type": "boolean", "description": "目录递归删除，默认 true"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_plugin",
            "description": "加载插件：把 plugins/<插件名> 目录下的插件加载到框架并注册命令。写插件完成后必须调用此工具使其生效",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名（与 main.py 所在目录一致）"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unload_plugin",
            "description": "卸载插件：从框架移除并清理其命令/任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_plugin",
            "description": "重载插件：先卸载再重新加载（修改代码后热更新用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_name": {"type": "string", "description": "插件目录名"},
                },
                "required": ["plugin_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugins",
            "description": "列出当前已加载的插件及版本",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def register(ctx):
    """插件注册入口"""
    # _init_db 为 async：在事件循环内则调度任务，否则同步跑完（CLI/同步加载场景）
    try:
        asyncio.get_running_loop().create_task(_init_db(ctx))
    except RuntimeError:
        asyncio.run(_init_db(ctx))
    ctx.command(
        "/ai", handle_ai,
        priority=100,
        alias=["/ai开发", "/生成插件", "/写插件", "/开发"],
        require_superuser=True,
        description="与 LLM 对话，可让 AI 编写/修改/加载插件（群内超管可用），用法: /ai <需求>",
    )
    ctx.command(
        "/pluginlist", handle_list_plugins,
        priority=100,
        alias=["/插件列表"],
        require_superuser=True,
        description="列出当前已加载的插件",
    )


# ---------------------------------------------------------------- 配置

def _get_config(ctx, key, default=None):
    return ctx.get_config(key, default)


def _build_system_prompt(ctx) -> str:
    """组装 system prompt：人格 + 技能 + 插件规范"""
    persona = str(_get_config(ctx, "persona", "")).strip() or _DEFAULT_PERSONA
    skills = _get_config(ctx, "skills", None)
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.replace('\n', ',').split(',') if s.strip()]
    elif not isinstance(skills, list):
        skills = _DEFAULT_SKILLS

    parts = [persona]
    if skills:
        parts.append("## 技能\n" + "\n".join(f"- {s}" for s in skills))
    parts.append(_PLUGIN_DEV_GUIDE)
    return "\n\n".join(parts)


def _get_cwd(ctx) -> str:
    """LLM 文件操作的基准目录（默认项目根）"""
    cwd = str(_get_config(ctx, "cwd", "")).strip()
    if cwd:
        return os.path.abspath(cwd)
    # 默认：plugins 目录的父目录 = 项目根
    try:
        plugins_dir = ctx._framework.plugin_loader.plugins_dir
        return os.path.dirname(os.path.abspath(plugins_dir))
    except Exception:
        # 回退：本文件所在位置的第三级上级（plugins/<name>/main.py → 项目根）
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- 会话历史（数据库存储，异步）

_SESSION_MAX_MSGS = 20  # 每个会话最多保留的消息条数
_SESSION_MAX_CHARS = 6000  # 单条消息超过此长度则截断

_SESSION_SYSTEM_FIRST = "这是本会话的历史记录，供后续轮次参考。工具操作细节以磁盘文件为准。"


async def _init_db(ctx) -> None:
    """初始化会话与用量表（兼容 MySQL/SQLite）"""
    try:
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "role VARCHAR(16) NOT NULL, "
            "content TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await ctx.db_execute_async(
            "CREATE TABLE IF NOT EXISTS llm_dev_usage ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id VARCHAR(32) NOT NULL, "
            "prompt_tokens INT DEFAULT 0, "
            "completion_tokens INT DEFAULT 0, "
            "total_tokens INT DEFAULT 0, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await ctx.db_execute_async(
            "CREATE INDEX IF NOT EXISTS idx_llm_dev_sessions_user ON llm_dev_sessions(user_id, id)"
        )
        await ctx.db_execute_async(
            "CREATE INDEX IF NOT EXISTS idx_llm_dev_usage_user ON llm_dev_usage(user_id, id)"
        )
    except Exception as e:
        ctx.log(f"初始化数据库表失败: {e}", level="error")


async def _get_session(ctx, user_id) -> list:
    """从数据库读取用户会话历史（含首条 system 说明）"""
    try:
        rows = await ctx.db_query_async(
            "SELECT role, content FROM llm_dev_sessions WHERE user_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (str(user_id), _SESSION_MAX_MSGS),
        )
        rows = list(reversed(rows))
        if not rows or rows[0].get('role') != 'system':
            rows.insert(0, {"role": "system", "content": _SESSION_SYSTEM_FIRST})
        return rows
    except Exception as e:
        ctx.log(f"读取会话失败: {e}", level="warning")
        return [{"role": "system", "content": _SESSION_SYSTEM_FIRST}]


async def _append_session(ctx, user_id, role, content):
    """写入一条会话消息"""
    try:
        has = await ctx.db_query_async(
            "SELECT COUNT(*) AS c FROM llm_dev_sessions WHERE user_id = %s AND role = 'system'",
            (str(user_id),),
        )
        if not has or not has[0].get('c'):
            await ctx.db_execute_async(
                "INSERT INTO llm_dev_sessions (user_id, role, content) VALUES (%s, %s, %s)",
                (str(user_id), 'system', _SESSION_SYSTEM_FIRST),
            )
        await ctx.db_execute_async(
            "INSERT INTO llm_dev_sessions (user_id, role, content) VALUES (%s, %s, %s)",
            (str(user_id), role, _clip(content)),
        )
        # 裁剪旧消息：保留最近 N 条（含 system）
        cnt = await ctx.db_query_async(
            "SELECT COUNT(*) AS c FROM llm_dev_sessions WHERE user_id = %s", (str(user_id),),
        )
        if cnt and cnt[0].get('c', 0) > _SESSION_MAX_MSGS:
            extra = cnt[0]['c'] - _SESSION_MAX_MSGS
            await ctx.db_execute_async(
                "DELETE FROM llm_dev_sessions WHERE user_id = %s AND role <> 'system' "
                "AND id IN (SELECT id FROM (SELECT id FROM llm_dev_sessions WHERE user_id = %s "
                "AND role <> 'system' ORDER BY id ASC LIMIT %s) t)",
                (str(user_id), str(user_id), extra),
            )
    except Exception as e:
        ctx.log(f"写入会话失败: {e}", level="warning")


async def _clear_session(ctx, user_id):
    try:
        await ctx.db_execute_async("DELETE FROM llm_dev_sessions WHERE user_id = %s", (str(user_id),))
    except Exception as e:
        ctx.log(f"清空会话失败: {e}", level="warning")


async def _record_usage(ctx, user_id, usage: dict):
    """记录一次调用的 token 消耗"""
    if not usage:
        return
    try:
        await ctx.db_execute_async(
            "INSERT INTO llm_dev_usage (user_id, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (%s, %s, %s, %s)",
            (str(user_id),
             int(usage.get('prompt_tokens') or 0),
             int(usage.get('completion_tokens') or 0),
             int(usage.get('total_tokens') or 0)),
        )
    except Exception as e:
        ctx.log(f"记录用量失败: {e}", level="warning")


async def _usage_summary(ctx, user_id) -> str:
    """返回用户累计用量文本"""
    try:
        rows = await ctx.db_query_async(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt, "
            "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(total_tokens),0) AS tt "
            "FROM llm_dev_usage WHERE user_id = %s",
            (str(user_id),),
        )
        if not rows:
            return ""
        r = rows[0]
        return f"\nⓘ 本会话累计: {r.get('calls', 0)} 次调用, {r.get('tt', 0)} tokens (输入{r.get('pt', 0)}/输出{r.get('ct', 0)})"
    except Exception:
        return ""


def _clip(text, limit=_SESSION_MAX_CHARS):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit] + f"...（截断，共{len(text)}字）"


# ---------------------------------------------------------------- LLM 调用

def _llm_headers(ctx) -> dict:
    api_key = str(_get_config(ctx, "api_key", "")).strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _llm_payload(ctx, messages, tools=None, tool_choice=None) -> dict:
    base_url = str(_get_config(ctx, "base_url", "")).strip()
    model = str(_get_config(ctx, "model", "gpt-4o-mini")).strip() or "gpt-4o-mini"
    temperature = float(_get_config(ctx, "temperature", 0.3) or 0.3)
    max_tokens = int(_get_config(ctx, "max_tokens", 8192) or 8192)
    if not base_url:
        raise RuntimeError("未配置 base_url，请在 Web UI 插件配置中设置")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    return payload


async def _chat_once(ctx, messages, tools=None, tool_choice=None) -> dict:
    """单次调用（httpx 异步），返回 {"message":..., "usage":{...}}"""
    base_url = str(_get_config(ctx, "base_url", "")).strip().rstrip('/')
    url = base_url + "/chat/completions"
    payload = _llm_payload(ctx, messages, tools=tools, tool_choice=tool_choice)
    ctx.log(f"调用 LLM: {base_url}", level="info")
    # 每次新建 AsyncClient：避免跨事件循环复用被关闭的客户端（插件可热重载）
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        resp = await client.post(url, headers=_llm_headers(ctx), json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应解析失败: {e} - {str(data)[:300]}")
    usage = data.get("usage") or {}
    return {"message": message, "usage": usage}


async def _chat_with_tools(ctx, messages: list, max_rounds: int = 12):
    """
    带函数调用的对话循环（全异步，不占线程池）：
    1. 发送 messages + tools
    2. 若返回 tool_calls → 依次执行工具（to_thread 内同步 IO）→ 追加结果 → 继续
    3. 无 tool_calls → 返回 (最终文本, 累计 usage)
    """
    total_usage = {}
    for _ in range(max_rounds):
        result = await _chat_once(ctx, messages, tools=_TOOLS)
        msg = result["message"]
        # 累计 usage
        u = result.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total_usage[k] = total_usage.get(k, 0) + int(u.get(k) or 0)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or "", total_usage

        # 把 assistant 消息（含 tool_calls）加入历史
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                # 文件/插件操作放到线程池，避免阻塞事件循环
                result_txt = await asyncio.to_thread(_execute_tool, ctx, name, args)
            except Exception as e:
                result_txt = f"错误: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(result_txt),
            })
    raise RuntimeError(f"函数调用超过 {max_rounds} 轮仍未结束")


# ---------------------------------------------------------------- 工具实现

def _resolve_path(ctx, path: str) -> str:
    """把相对路径解析到 cwd 基准目录，绝对路径直接用"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(_get_cwd(ctx), path))


def _execute_tool(ctx, name: str, args: dict):
    """执行 LLM 请求的工具调用"""
    # 插件管理工具（不依赖 path）
    if name in ("load_plugin", "unload_plugin", "reload_plugin", "list_plugins"):
        return _execute_plugin_tool(ctx, name, args)

    # 文件操作工具：必须提供 path
    path = str(args.get("path") or "").strip()
    if not path:
        return "错误: 缺少 path 参数"

    if name == "ls":
        target = _resolve_path(ctx, path)
        if os.path.isfile(target):
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return f"[文件] {target} ({len(content)} 字符)\n" + content[:4000]
        if os.path.isdir(target):
            try:
                entries = sorted(os.listdir(target))
            except OSError as e:
                return f"错误: {e}"
            lines = [f"[目录] {target} ({len(entries)} 项)"]
            for e in entries:
                full = os.path.join(target, e)
                mark = '/' if os.path.isdir(full) else ''
                lines.append(f"  {e}{mark}")
            return "\n".join(lines)
        # 支持通配符
        import glob
        matches = sorted(glob.glob(target))
        if matches:
            lines = [f"[匹配 {len(matches)} 项] {path}"]
            for m in matches:
                mark = '/' if os.path.isdir(m) else ''
                lines.append(f"  {m}{mark}")
            return "\n".join(lines)
        return f"未找到: {path}"

    if name == "write":
        target = _resolve_path(ctx, path)
        content = str(args.get("content") or "")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"已写入 {target} ({len(content)} 字符)"

    if name == "edit":
        target = _resolve_path(ctx, path)
        if not os.path.isfile(target):
            return f"错误: 文件不存在 {target}"
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if not old_text:
            return "错误: 缺少 old_text"
        with open(target, 'r', encoding='utf-8') as f:
            content = f.read()
        if old_text not in content:
            return f"错误: 未找到要替换的文本（old_text 须精确匹配）"
        new_content = content.replace(old_text, new_text, 1)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"已修改 {target}"

    if name == "rm":
        target = _resolve_path(ctx, path)
        if os.path.isdir(target):
            recursive = bool(args.get("recursive", True))
            if recursive:
                import shutil
                shutil.rmtree(target, ignore_errors=True)
                return f"已删除目录: {target}"
            try:
                os.rmdir(target)
                return f"已删除空目录: {target}"
            except OSError as e:
                return f"错误: {e}"
        if os.path.isfile(target):
            os.remove(target)
            return f"已删除文件: {target}"
        return f"未找到: {target}"

    return f"错误: 未知工具 {name}"


def _execute_plugin_tool(ctx, name: str, args: dict):
    """执行插件管理工具（load/unload/reload/list）"""
    if name == "list_plugins":
        plugins = ctx._framework.plugin_loader.get_loaded_plugins()
        if not plugins:
            return "当前没有已加载的插件"
        lines = ["已加载插件:"]
        for n, info in sorted(plugins.items()):
            meta = info.get('meta', {})
            lines.append(f"- {meta.get('name', n)} ({n}) v{meta.get('version', '?')}")
        return "\n".join(lines)

    plugin_name = str(args.get("plugin_name") or "").strip()
    if not plugin_name or not re.match(r'^[a-zA-Z0-9_\-]+$', plugin_name):
        return f"错误: 非法插件名 {plugin_name!r}（须为英文/数字/下划线/短横线）"

    if name == "load_plugin":
        loader = ctx._framework.plugin_loader
        main_path = os.path.join(loader.plugins_dir, plugin_name, 'main.py')
        if not os.path.isfile(main_path):
            return f"错误: 未找到 {main_path}，请先用 write 创建插件代码"
        try:
            with open(main_path, 'r', encoding='utf-8') as f:
                ast.parse(f.read())
        except SyntaxError as e:
            return f"错误: main.py 语法错误: {e}"
        try:
            ok = loader.load_plugin(plugin_name)
        except Exception as e:
            return f"错误: 加载异常: {e}"
        if not ok:
            return f"错误: 加载失败，请检查代码或依赖（可 ls 查看，edit 修改，reload_plugin 重试）"
        try:
            loader.register_commands(plugin_name)
            ctx._framework.router._invalidate_cache()
        except Exception:
            pass
        meta = loader.get_loaded_plugins().get(plugin_name, {}).get('meta', {})
        return f"✅ 插件已加载: {meta.get('name', plugin_name)} v{meta.get('version', '?')}"

    if name == "unload_plugin":
        try:
            ctx._framework.plugin_loader.unload_plugin(plugin_name)
        except Exception as e:
            return f"错误: {e}"
        return f"已卸载插件: {plugin_name}"

    if name == "reload_plugin":
        loader = ctx._framework.plugin_loader
        try:
            loader.unload_plugin(plugin_name)
            ok = loader.load_plugin(plugin_name)
            if not ok:
                return f"错误: 重载失败（代码可能有问题）"
            loader.register_commands(plugin_name)
            ctx._framework.router._invalidate_cache()
        except Exception as e:
            return f"错误: 重载异常: {e}"
        return f"✅ 插件已重载: {plugin_name}"

    return f"错误: 未知工具 {name}"


# ---------------------------------------------------------------- handlers

def _truncate(text, limit=1800):
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


def _get_prompt(event, match, cmd_prefix):
    prompt = ""
    if match:
        prompt = match.group(1).strip()
    if not prompt:
        msg = event.message or ""
        if msg.startswith(cmd_prefix):
            prompt = msg[len(cmd_prefix):].strip()
    return prompt


def _loaded_plugins_text(ctx) -> str:
    """当前已加载插件列表（注入对话上下文）"""
    plugins = ctx._framework.plugin_loader.get_loaded_plugins()
    if not plugins:
        return "（当前没有已加载的插件）"
    lines = ["当前已加载插件："]
    for n, info in sorted(plugins.items()):
        meta = info.get('meta', {})
        lines.append(f"- {meta.get('name', n)} ({n}) v{meta.get('version', '?')}")
    return "\n".join(lines)


async def handle_ai(event, match):
    """
    通用 AI 对话 + 函数调用（ls/write/edit/rm/load_plugin/unload_plugin/reload_plugin/list_plugins）
    超管在群内即可让 AI 写插件：/ai 写一个 xxx 插件
    自动携带本用户会话上下文（跨轮记忆）；发 "/ai 重置" 清空上下文
    全异步：不占用框架线程池（httpx 异步 HTTP + db_*_async + asend_msg）
    """
    prompt = _get_prompt(event, match, "/ai")
    if not prompt:
        await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                            message="用法: /ai <指令>\n例如:\n/ai 写一个每日早报插件，每天早上8点发天气\n/ai 修改 echo 插件，加个参数\n/ai 查看 plugins 目录下有哪些插件\n/ai 重置  清空上下文")
        return

    # 重置上下文
    if prompt in ("重置", "清空", "新会话", "重来"):
        await _clear_session(ctx, event.user_id)
        await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                            message="✅ 已清空该用户的历史上下文，AI 将从全新状态开始。")
        return

    if not str(_get_config(ctx, "base_url", "")).strip():
        await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                            message="尚未配置 LLM 接口，请在 Web UI → 插件配置 中设置 base_url / api_key / model。")
        return

    await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                        message=f"🤖 已收到指令，AI 正在处理（可写文件并加载插件，通常需 1~2 分钟）...\n{prompt}")
    try:
        system = _build_system_prompt(ctx)
        history = await _get_session(ctx, event.user_id)
        # 组装：system(人格) + 历史 + 当前指令（当前插件列表注入到 user 消息）
        user_content = (
            f"{_loaded_plugins_text(ctx)}\n\n"
            f"用户指令：{prompt}\n\n"
            "（提示：如需写新插件，请先 ls plugins/llm_plugin_gen/docs/INDEX.md 阅读文档索引，"
            "再按需 ls 具体文档；然后按规范用 write 创建文件并调用 load_plugin 加载；若加载失败可 edit/reload_plugin 修复）"
        )
        messages = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_content},
        ]
        result, usage = await _chat_with_tools(ctx, messages)
        if not result.strip():
            result = "（AI 未返回文本内容）"

        # 记录本轮回话 + token 消耗
        await _append_session(ctx, event.user_id, "user", prompt)
        await _append_session(ctx, event.user_id, "assistant", result)
        await _record_usage(ctx, event.user_id, usage)

        # 附带本次与累计 token 消耗
        cost = ""
        if usage.get('total_tokens'):
            cost = f"\n[本次消耗 {usage.get('total_tokens')} tokens]"
        summary = await _usage_summary(ctx, event.user_id)

        await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                            message=_truncate(result) + cost + summary)
    except Exception as e:
        ctx.log(f"AI 处理失败: {e}\n{traceback.format_exc()}", level="error")
        await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                            message=f"❌ 处理失败: {_truncate(str(e), 500)}")


async def handle_list_plugins(event, match):
    loader = ctx._framework.plugin_loader
    plugins = loader.get_loaded_plugins()
    if not plugins:
        text = "当前没有已加载的插件。"
    else:
        lines = ["📦 已加载插件:"]
        for name, info in sorted(plugins.items()):
            meta = info.get('meta', {})
            lines.append(f"- {meta.get('name', name)} ({name}) v{meta.get('version', '?')}")
        text = "\n".join(lines)
    await ctx.asend_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None, message=text)
