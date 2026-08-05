"""
LLM 开发助手插件
================================
通过 OpenAI 兼容接口让 LLM 编写插件、管理插件文件，支持自定义人格与 skills。

核心能力：
  1. /genplugin <需求>  — 让 LLM 生成插件并加载
  2. /ai <指令>         — 通用对话，LLM 可通过函数调用（ls/write/edit/rm）直接操作插件文件
  3. /pluginlist        — 查看已加载插件

函数调用（OpenAI tools）：
  ls       列出目录/文件
  write    写入/新建文件
  edit     修改已有文件（按文本替换）
  rm       删除文件/目录

配置项（_conf_schema.json，Web UI 可改）：
  base_url / api_key / model / temperature / max_tokens
  persona        人格设定（自定义 system prompt，可写中文）
  skills         技能列表（每条一行或 JSON 数组，注入 system prompt）
  cwd            工作目录（LLM 文件操作的基准目录，默认项目根）

权限说明：命令仅超管可用；文件操作不受路径限制（超管自负其责）。
"""
import ast
import json
import os
import re
import traceback

import requests

__plugin_meta__ = {
    "name": "LLM 开发助手",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "通过 OpenAI 兼容接口让 LLM 编写插件并管理插件文件（支持人格/skills/函数调用）",
    "priority": 100,
}

# 默认人格
_DEFAULT_PERSONA = "你是 ZCBOT OneBot QQ 机器人框架的插件开发专家，擅长编写高质量、健壮的 Python 插件。"

# 插件开发规范（注入 system prompt，约束 LLM 生成符合框架语法的代码）
_PLUGIN_DEV_GUIDE = """\
## 插件开发规范（生成 /genplugin 时必须遵守）
- 生成单个 main.py，必须包含 `__plugin_meta__` 字典与 `def register(ctx):` 入口
- 处理函数签名：`def handler(event, match):`（或 async def），通过模块级 `ctx` 访问上下文
- ctx 常用 API：
  - ctx.command(pattern, handler, priority=50, alias=..., description=..., require_admin=False, require_superuser=False)
  - ctx.send_msg(user_id=..., group_id=..., message=...)  发送消息
  - ctx.log(msg, level='info')                            记录日志
  - ctx.get_config(key, default)                          读取插件配置
  - ctx.db_query(sql, params) / ctx.db_execute(sql, params)  数据库
  - ctx.api(action, **params)                              OneBot API
  - ctx.task(cron_expr, executor)                         定时任务
- 只用标准库 + 框架已装依赖（requests/flask/pyyaml/Pillow/numpy），额外依赖写入 dependencies 字段
- 禁止相对导入（from .xxx），单文件实现
- 代码要健壮：参数校验、异常捕获、不阻塞主流程
- 注释使用中文
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
]


def register(ctx):
    """插件注册入口"""
    ctx.command(
        "/genplugin", handle_gen_plugin,
        priority=100,
        alias=["/生成插件"],
        require_superuser=True,
        description="让 LLM 生成插件并加载，用法: /genplugin <插件需求描述>",
    )
    ctx.command(
        "/ai", handle_ai,
        priority=100,
        alias=["/ai开发"],
        require_superuser=True,
        description="与 LLM 对话，可通过函数调用管理插件文件，用法: /ai <指令>",
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
    """LLM 文件操作的基准目录"""
    cwd = str(_get_config(ctx, "cwd", "")).strip()
    if cwd:
        return os.path.abspath(cwd)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _chat_once(ctx, messages, tools=None, tool_choice=None) -> dict:
    """单次调用，返回原始 assistant message"""
    base_url = str(_get_config(ctx, "base_url", "")).strip().rstrip('/')
    url = base_url + "/chat/completions"
    payload = _llm_payload(ctx, messages, tools=tools, tool_choice=tool_choice)
    ctx.log(f"调用 LLM: {base_url}", level="info")
    resp = requests.post(url, headers=_llm_headers(ctx), json=payload, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应解析失败: {e} - {str(data)[:300]}")


def _chat_with_tools(ctx, messages: list, max_rounds: int = 12) -> str:
    """
    带函数调用的对话循环：
    1. 发送 messages + tools
    2. 若返回 tool_calls → 依次执行工具 → 把结果作为 tool 消息追加 → 继续
    3. 无 tool_calls → 返回最终文本
    """
    for _ in range(max_rounds):
        msg = _chat_once(ctx, messages, tools=_TOOLS)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or ""

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
                result = _execute_tool(ctx, name, args)
            except Exception as e:
                result = f"错误: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(result),
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


# ---------------------------------------------------------------- 生成插件

def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象（兼容 markdown 包裹/杂散文字）"""
    text = (text or "").strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 输出中未找到 JSON: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")


def _validate_generated(data: dict) -> None:
    plugin_name = str(data.get("plugin_name") or "").strip()
    if not re.match(r'^[a-zA-Z0-9_\-]+$', plugin_name):
        raise ValueError(f"插件名非法（须为英文/数字/下划线/短横线）: {plugin_name!r}")
    main_py = data.get("main_py")
    if not isinstance(main_py, str) or not main_py.strip():
        raise ValueError("LLM 未返回 main_py 代码")
    try:
        ast.parse(main_py)
    except SyntaxError as e:
        raise ValueError(f"生成的代码语法错误: {e}")
    if 'def register(ctx)' not in main_py and 'def register(' not in main_py:
        raise ValueError("生成的代码缺少 register(ctx) 入口函数")
    if '__plugin_meta__' not in main_py:
        raise ValueError("生成的代码缺少 __plugin_meta__ 元信息")


def _load_generated_plugin(ctx, plugin_name: str, main_py: str, dependencies: list, meta: dict) -> dict:
    plugins_dir = ctx._framework.plugin_loader.plugins_dir
    dat_dir = ctx._framework.plugin_loader.plugins_dat_dir
    plugin_path = os.path.join(plugins_dir, plugin_name)
    main_path = os.path.join(plugin_path, 'main.py')
    backup_main = None
    try:
        os.makedirs(plugin_path, exist_ok=True)
        if os.path.isfile(main_path):
            backup_main = main_path + f'.bak.{int(time.time())}'
            os.replace(main_path, backup_main)
        with open(main_path, 'w', encoding='utf-8') as f:
            f.write(main_py)

        os.makedirs(dat_dir, exist_ok=True)
        yaml_path = os.path.join(dat_dir, plugin_name, 'plugin.yaml')
        os.makedirs(os.path.dirname(yaml_path), exist_ok=True)
        if not os.path.isfile(yaml_path):
            import yaml as _yaml
            yaml_data = {
                "name": meta.get("name", plugin_name),
                "version": meta.get("version", "1.0.0"),
                "author": meta.get("author", "ZGRIC"),
                "description": meta.get("desc", ""),
                "priority": meta.get("priority", 50),
                "dependencies": {"python": dependencies or []},
            }
            with open(yaml_path, 'w', encoding='utf-8') as f:
                _yaml.safe_dump(yaml_data, f, allow_unicode=True, sort_keys=False)

        loader = ctx._framework.plugin_loader
        ok = loader.load_plugin(plugin_name)
        if not ok:
            raise RuntimeError("load_plugin 失败，请查看日志（可能是依赖缺失或代码运行错误）")
        loader.register_commands(plugin_name)
        try:
            ctx._framework.router._invalidate_cache()
        except Exception:
            pass
        return {'success': True, 'plugin_path': plugin_path, 'backup': backup_main}
    except Exception:
        if backup_main and os.path.exists(backup_main):
            os.replace(backup_main, main_path)
        else:
            try:
                os.remove(main_path)
            except OSError:
                pass
        raise


def _truncate(text, limit=1800):
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


# ---------------------------------------------------------------- handlers

def _get_prompt(event, match, cmd_prefix):
    prompt = ""
    if match:
        prompt = match.group(1).strip()
    if not prompt:
        msg = event.message or ""
        if msg.startswith(cmd_prefix):
            prompt = msg[len(cmd_prefix):].strip()
    return prompt


def handle_gen_plugin(event, match):
    prompt = _get_prompt(event, match, "/genplugin")
    if not prompt:
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message="用法: /genplugin <插件需求描述>\n例如: /genplugin 写一个每日早报插件，每天早上8点发送天气和新闻")
        return
    if not str(_get_config(ctx, "base_url", "")).strip():
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message="尚未配置 LLM 接口，请在 Web UI → 插件配置 中设置 base_url / api_key / model。")
        return

    ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                 message=f"正在调用 LLM 生成插件，需求:\n{prompt}\n\n可能需要几十秒，请稍候...")
    try:
        system = _build_system_prompt(ctx)
        raw = _chat_once(ctx, [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请根据以下需求生成插件，严格以 JSON 输出（plugin_name/plugin_meta/main_py/dependencies/usage）：\n{prompt}"},
        ]).get("content") or ""
        data = _extract_json(raw)
        _validate_generated(data)

        plugin_name = str(data.get("plugin_name") or "").strip()
        main_py = data.get("main_py")
        dependencies = data.get("dependencies") or []
        meta = data.get("plugin_meta") or {}
        usage = str(data.get("usage") or "").strip()

        result = _load_generated_plugin(ctx, plugin_name, main_py, dependencies, meta)
        msg = (f"✅ 插件生成并加载成功\n插件名: {plugin_name}\n"
               f"显示名: {meta.get('name', plugin_name)} v{meta.get('version', '1.0.0')}\n"
               f"位置: {result['plugin_path']}")
        if dependencies:
            msg += f"\n依赖: {', '.join(dependencies)}（自动安装到全局，冲突自动跳过）"
        if usage:
            msg += f"\n用法: {_truncate(usage, 300)}"
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None, message=msg)
    except Exception as e:
        ctx.log(f"生成插件失败: {e}\n{traceback.format_exc()}", level="error")
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message=f"❌ 生成插件失败: {_truncate(str(e), 500)}")


def handle_ai(event, match):
    """通用 AI 对话 + 函数调用（ls/write/edit/rm）"""
    prompt = _get_prompt(event, match, "/ai")
    if not prompt:
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message="用法: /ai <指令>\n例如:\n/ai 查看 plugins 目录下有哪些插件\n/ai 写一个 hello 插件\n/ai 修改 echo 插件，加个参数")
        return
    if not str(_get_config(ctx, "base_url", "")).strip():
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message="尚未配置 LLM 接口，请在 Web UI → 插件配置 中设置 base_url / api_key / model。")
        return

    ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                 message=f"🤖 已收到指令，LLM 处理中（可操作文件: ls/write/edit/rm）...\n{prompt}")
    try:
        system = _build_system_prompt(ctx)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        result = _chat_with_tools(ctx, messages)
        if not result.strip():
            result = "（LLM 未返回文本内容）"
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message=_truncate(result))
    except Exception as e:
        ctx.log(f"AI 处理失败: {e}\n{traceback.format_exc()}", level="error")
        ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None,
                     message=f"❌ 处理失败: {_truncate(str(e), 500)}")


def handle_list_plugins(event, match):
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
    ctx.send_msg(user_id=event.user_id, group_id=event.group_id if event.is_group else None, message=text)
