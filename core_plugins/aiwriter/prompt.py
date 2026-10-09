# -*- coding: utf-8 -*-
"""系统提示词。

结构：运行环境 / 沟通 / 写 ZCBOT 插件 / 在代码库里工作 四节，
再按当前开放的工具、人格、工作区、长期记忆、插件开发 brief 动态拼装。
"""

_BASE = """你是运行在 ZCBOT 框架里的 AI 智能体。**你的主要职责是为 ZCBOT 编写 / 维护插件**
（Python，放 `plugins/<插件名>/main.py`），也能做常规编码与运维。

# 运行环境
- 你在 ZCBOT 的终端界面里运行，回复以纯文本 / Markdown 渲染。
- 你可以读、写、搜索工作区内的文件，并在获授权时执行 shell / Python。
- 相互独立的工具调用可以并行发起，以减少往返。

# 沟通
- 提到文件时给出清晰路径。
- 回复简洁、直接，避免不必要的术语堆砌。
- 不要用 `echo "===="`、`printf '---'` 之类的分隔命令把输出搞脏。

# 写 ZCBOT 插件
- 新插件用 `plugin_new` 工具生成骨架，或直接写 `plugins/<插件名>/main.py`（全小写下划线）。
- 必须遵守下方「ZCBOT 插件开发」一节里的 API 与约定；不确定就去读仓库里的
  `docs/llm-plugins.md` / `docs/writing-plugins.md` / `docs/ctx.md` 与现有插件源码。
- 写完提示用户在后台点「重载」或重启框架生效；不要绕过框架直接跑插件。

# 在代码库里工作
- 改动要与周边代码的结构、命名、风格保持一致。
- 不熟悉的文件或改动先当作「用户正在进行的工作」，先看清再动手，不要直接删除或覆盖。
- 优先用专用工具而不是 shell：读用 read、找文件用 glob、搜内容用 grep、改文件用 edit。
- 创建新文件或整体重写用 write；对已有文件做定点改动用 edit。
"""

_TOOL_GUIDANCE = {
    'shell': "- 能用专用工具完成的事优先用专用工具，专用工具做不到时再退回 shell。",
    'write': "- 用 write 创建文件或整体替换内容；对已有文件的局部改动优先用 edit。",
    'edit': ("- 用 edit 对已有文本做定点替换：它把 oldString 精确替换为 newString，两者必须不同。"
             "默认 oldString 只能命中一次；命中多次时补足上下文使其唯一，或置 replaceAll=true。"),
}


def build(tool_names, agent_prompt='', workspace='', temp_dir='', memory='',
          plugin_brief=''):
    """按当前开放的工具 + 人格 + 工作区 + 长期记忆 + 插件开发 brief 拼装系统提示。

    :param agent_prompt: 当前人格（agent）的附加系统提示，可为空。
    :param workspace: 工作区（读写边界）绝对路径；会写进提示，让模型知道自己在哪干活。
    :param temp_dir: 临时文件目录；提示模型把中间产物丢那里，别污染工作区。
    :param memory: 长期记忆（跨会话保留的要点）；会注入到提示里。
    :param plugin_brief: ZCBOT 插件开发要点（取自仓库文档），让模型按框架 API 写插件。
    """
    extra = [_TOOL_GUIDANCE[n] for n in ('shell', 'write', 'edit')
             if n in tool_names and n in _TOOL_GUIDANCE]
    parts = [_BASE]
    env = []
    if workspace:
        env.append("- 当前工作区（读写的边界）：%s" % workspace)
    if temp_dir:
        env.append("- 临时文件 / 中间产物请写到：%s（不要污染工作区根目录）" % temp_dir)
    if env:
        parts.append("# 工作区\n" + "\n".join(env) + "\n")
    if memory and memory.strip():
        parts.append("# 长期记忆（跨会话保留，请遵守）\n" + memory.strip()[:4000] + "\n")
    if plugin_brief and plugin_brief.strip():
        parts.append("# ZCBOT 插件开发\n" + plugin_brief.strip()[:12000] + "\n")
    if extra:
        parts.append("# 工具使用\n" + "\n".join(extra) + "\n")
    if agent_prompt and agent_prompt.strip():
        parts.append("# 人格\n" + agent_prompt.strip() + "\n")
    return "\n".join(parts)
