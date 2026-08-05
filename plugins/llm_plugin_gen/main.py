"""
LLM 插件生成器插件
================================
通过 OpenAI 兼容接口让 LLM 根据需求自动编写插件，并写入 plugins/ 后加载。

用法：
  /genplugin <插件需求描述>     让 LLM 生成插件并加载（仅超管可用）

配置项（Web UI 插件配置 / _conf_schema.json）：
  base_url     OpenAI 兼容接口地址（如 https://api.openai.com/v1 或本地 vllm/ollama）
  api_key      API Key（本地模型可留空）
  model        模型名
  temperature  采样温度（默认 0.3，越低越稳定）
  max_tokens   最大生成 token 数

安全说明：
  - 命令仅超管可用（require_superuser=True）
  - 生成的代码会做语法校验与 register(ctx) 结构校验，加载失败自动回滚
  - 生成代码执行任意逻辑，请确认模型可信后再使用
"""
import ast
import json
import os
import re
import time
import traceback

import requests

__plugin_meta__ = {
    "name": "LLM 插件生成器",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "通过 OpenAI 兼容接口让 LLM 自动生成插件并加载",
    "priority": 100,
}


# 插件开发规范（注入到 system prompt，约束 LLM 生成符合框架语法的代码）
_PLUGIN_DEV_GUIDE = """\
你是 ZCBOT OneBot QQ 机器人框架的插件开发专家，根据用户需求生成一个可运行的插件。

## 插件文件结构
只需生成单个 main.py 文件，内容必须包含：

1. `__plugin_meta__` 字典：
   {
       "name": "插件显示名",
       "version": "1.0.0",
       "author": "ZGRIC",
       "desc": "一句话功能描述",
       "priority": 50,
   }

2. `def register(ctx):` 函数：注册命令（ctx.command(...)）、定时任务（ctx.task(...)）、事件（ctx.on(...)）等

3. 处理函数：签名固定为 `def handler(event, match):`（或 async def handler(event, match)），
   通过模块级 `ctx` 变量访问上下文（框架会把 ctx 注入到模块全局变量）。

## ctx 可用 API（常用）
- ctx.command(pattern, handler, priority=50, alias=..., description=..., require_admin=False, require_superuser=False)
  注册命令，pattern 是正则或命令名（如 "/hello" 或 "^/hi"）
- ctx.send_msg(user_id=..., group_id=..., message=...) 发送消息（同步）
- ctx.log(msg, level='info') 记录日志
- ctx.get_config(key, default) 读取插件配置
- ctx.db_query(sql, params) / ctx.db_execute(sql, params) 数据库操作
- ctx.api(action, **params) 调用 OneBot API（如 send_msg / set_group_ban）
- ctx.task(cron_expr, executor) 注册定时任务（cron 表达式）
- ctx.on(event_name, handler) 订阅事件

## 输出格式（严格 JSON，不要输出任何其他内容）
{
    "plugin_name": "英文目录名(小写下划线，如 hello_weather)",
    "plugin_meta": {"name": "显示名", "version": "1.0.0", "author": "ZGRIC", "desc": "描述", "priority": 50},
    "main_py": "完整的 main.py 源码字符串",
    "dependencies": ["可选依赖包名列表，无则 []"],
    "usage": "一句话说明如何使用该插件"
}

## 硬性要求
- 只用标准库 + requirements 已列出的包（requests, flask, pyyaml, Pillow, numpy 等），不要假设其他包已安装
- 如果依赖额外包，必须写入 dependencies 字段
- 禁止使用相对导入（from .xxx），用绝对导入或单文件实现
- 代码要健壮：参数校验、异常捕获、不阻塞主流程
- 注释使用中文
"""

_DANGEROUS_PATTERNS = [
    (r'\bos\.system\s*\(', '调用系统命令'),
    (r'\bsubprocess\s*\.', '调用子进程'),
    (r'\b__import__\s*\(', '动态导入'),
    (r'\beval\s*\(', 'eval 执行'),
    (r'\bexec\s*\(', 'exec 执行'),
    (r'\bsocket\s*\.', 'socket 网络'),
]


def register(ctx):
    """插件注册入口"""
    ctx.command(
        "/genplugin", handle_gen_plugin,
        priority=100,
        alias=["/生成插件", "/ai插件"],
        require_superuser=True,
        description="让 LLM 生成插件并加载，用法: /genplugin <插件需求描述>",
    )
    ctx.command(
        "/pluginlist", handle_list_plugins,
        priority=100,
        alias=["/插件列表"],
        require_superuser=True,
        description="列出当前已加载的插件",
    )


# ---------------------------------------------------------------- helpers

def _get_config(ctx, key, default=None):
    """读取插件配置"""
    return ctx.get_config(key, default)


def _call_llm(ctx, prompt: str) -> str:
    """调用 OpenAI 兼容接口，返回 assistant 文本"""
    base_url = str(_get_config(ctx, "base_url", "https://api.openai.com/v1")).rstrip('/')
    api_key = str(_get_config(ctx, "api_key", "")).strip()
    model = str(_get_config(ctx, "model", "gpt-4o-mini")).strip() or "gpt-4o-mini"
    temperature = float(_get_config(ctx, "temperature", 0.3) or 0.3)
    max_tokens = int(_get_config(ctx, "max_tokens", 8192) or 8192)

    url = base_url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PLUGIN_DEV_GUIDE},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    ctx.log(f"调用 LLM: {base_url} model={model}", level="info")
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应解析失败: {e} - {str(data)[:300]}")


def _extract_json(text: str) -> dict:
    """
    从 LLM 输出中提取 JSON 对象
    兼容输出被 markdown 代码块包裹或前后有杂散文字的情况
    """
    text = text.strip()
    # 去掉 markdown 代码块围栏
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    # 定位第一个 { 到最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 输出中未找到 JSON: {text[:200]}")
    raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e} - {raw[:300]}")


def _validate_generated(data: dict) -> None:
    """校验 LLM 生成结果的基本合法性"""
    plugin_name = str(data.get("plugin_name") or "").strip()
    if not re.match(r'^[a-zA-Z0-9_\-]+$', plugin_name):
        raise ValueError(f"插件名非法（须为英文/数字/下划线/短横线）: {plugin_name!r}")
    main_py = data.get("main_py")
    if not isinstance(main_py, str) or not main_py.strip():
        raise ValueError("LLM 未返回 main_py 代码")
    # 语法校验
    try:
        ast.parse(main_py)
    except SyntaxError as e:
        raise ValueError(f"生成的代码语法错误: {e}")
    # register(ctx) 结构校验
    if 'def register(ctx)' not in main_py and 'def register(' not in main_py:
        raise ValueError("生成的代码缺少 register(ctx) 入口函数")
    if '__plugin_meta__' not in main_py:
        raise ValueError("生成的代码缺少 __plugin_meta__ 元信息")


def _scan_dangerous(main_py: str) -> list:
    """扫描代码中的危险操作，返回 [(模式描述, 命中片段)]"""
    hits = []
    for pattern, desc in _DANGEROUS_PATTERNS:
        m = re.search(pattern, main_py)
        if m:
            snippet = main_py[max(0, m.start() - 20):m.end() + 20].replace('\n', ' ')
            hits.append((desc, snippet))
    return hits


def _plugins_dir(ctx) -> str:
    return ctx._framework.plugin_loader.plugins_dir


def _plugins_dat_dir(ctx) -> str:
    return ctx._framework.plugin_loader.plugins_dat_dir


def _load_generated_plugin(ctx, plugin_name: str, main_py: str, dependencies: list, meta: dict) -> dict:
    """
    将生成的插件写入磁盘并加载：
    1. 写入 plugins/<name>/main.py（存在则备份）
    2. 写入 plugins_dat/<name>/plugin.yaml（声明依赖）
    3. load_plugin + register_commands + 路由缓存失效
    失败时回滚（恢复备份 / 清理目录）
    """
    plugins_dir = _plugins_dir(ctx)
    dat_dir = _plugins_dat_dir(ctx)
    plugin_path = os.path.join(plugins_dir, plugin_name)
    main_path = os.path.join(plugin_path, 'main.py')
    backup_main = None

    try:
        # 1. 写入 main.py（先备份旧文件）
        os.makedirs(plugin_path, exist_ok=True)
        if os.path.isfile(main_path):
            backup_main = main_path + f'.bak.{int(time.time())}'
            os.replace(main_path, backup_main)
        with open(main_path, 'w', encoding='utf-8') as f:
            f.write(main_py)

        # 2. 写入 plugin.yaml（依赖 + 元信息）
        os.makedirs(dat_dir, exist_ok=True)
        yaml_data = {
            "name": meta.get("name", plugin_name),
            "version": meta.get("version", "1.0.0"),
            "author": meta.get("author", "ZGRIC"),
            "description": meta.get("desc", ""),
            "priority": meta.get("priority", 50),
            "dependencies": {"python": dependencies or []},
        }
        yaml_path = os.path.join(dat_dir, 'plugin.yaml')
        # 不覆盖已有 yaml（保留用户修改）
        if not os.path.isfile(yaml_path):
            import yaml as _yaml
            with open(yaml_path, 'w', encoding='utf-8') as f:
                _yaml.safe_dump(yaml_data, f, allow_unicode=True, sort_keys=False)

        # 3. 加载插件
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
        # 回滚：恢复备份
        if backup_main and os.path.exists(backup_main):
            os.replace(backup_main, main_path)
        else:
            try:
                os.remove(main_path)
            except OSError:
                pass
        raise


def _truncate(text: str, limit: int = 1800) -> str:
    """截断长文本，避免消息过长"""
    return text if len(text) <= limit else text[:limit] + f"...（已截断，共 {len(text)} 字）"


# ---------------------------------------------------------------- handlers

def handle_gen_plugin(event, match):
    """生成插件主处理"""
    # 读取需求描述
    prompt = ""
    if match:
        prompt = match.group(1).strip()
    if not prompt:
        msg = event.message or ""
        if msg.startswith("/genplugin"):
            prompt = msg[len("/genplugin"):].strip()
    if not prompt:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="用法: /genplugin <插件需求描述>\n例如: /genplugin 写一个每日早报插件，每天早上8点发送天气和新闻",
        )
        return

    base_url = str(_get_config(ctx, "base_url", "")).strip()
    if not base_url:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="尚未配置 LLM 接口。请在 Web UI → 插件配置 中设置 base_url / api_key / model。",
        )
        return

    reply = (
        f"正在调用 LLM 生成插件，需求:\n{prompt}\n\n"
        "这可能需要几十秒，请耐心等待..."
    )
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=reply,
    )

    try:
        raw = _call_llm(ctx, f"请根据以下需求生成插件：{prompt}")
        data = _extract_json(raw)
        _validate_generated(data)

        plugin_name = str(data.get("plugin_name") or "").strip()
        main_py = data.get("main_py")
        dependencies = data.get("dependencies") or []
        meta = data.get("plugin_meta") or {}
        usage = str(data.get("usage") or "").strip()

        # 危险代码扫描（仅警告，不拦截——超管使用自负其责）
        danger_hits = _scan_dangerous(main_py)

        # 写入并加载
        result = _load_generated_plugin(ctx, plugin_name, main_py, dependencies, meta)

        msg = (
            f"✅ 插件生成并加载成功\n"
            f"插件名: {plugin_name}\n"
            f"显示名: {meta.get('name', plugin_name)} v{meta.get('version', '1.0.0')}\n"
            f"位置: {result['plugin_path']}"
        )
        if dependencies:
            msg += f"\n依赖: {', '.join(dependencies)}（自动安装到全局，冲突自动跳过）"
        if usage:
            msg += f"\n用法: {_truncate(usage, 300)}"
        if danger_hits:
            desc = '、'.join(d for d, _ in danger_hits)
            msg += f"\n⚠️ 代码包含危险操作: {desc}（请审查后再使用）"
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=msg,
        )
    except Exception as e:
        ctx.log(f"生成插件失败: {e}\n{traceback.format_exc()}", level="error")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=f"❌ 生成插件失败: {_truncate(str(e), 500)}",
        )


def handle_list_plugins(event, match):
    """列出已加载插件"""
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
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )
