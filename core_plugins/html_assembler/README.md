# html_assembler —— 单文件 HTML 装配引擎（官方插件）

## 概述

**可编程的 HTML 装配引擎**：对外暴露一组标准接口，调用方通过这些接口往一个单文件
HTML 模板里**赋值、填数据、塞图片**，引擎输出一个浏览器能直接打开、正常渲染的
**单文件 HTML**。

引擎职责（仅文本层面装配）：

```
接收数据 → 替换占位符 → 图片转 base64 内嵌 → 输出完整 HTML
```

引擎**不做**的事（全部交给下游浏览器）：

- 不启动浏览器
- 不执行 JS
- 不解析 CSS
- 不画像素

特性：**内存可控**（模板/图片/输出均有字节上限）、**零第三方依赖**（仅标准库）、
纯文本装配（不引入 HTML 解析器）。

## 安装与启用

官方插件，位于 `core_plugins/html_assembler/`，框架启动时自动发现。
`core_plugins.yaml` 会自动补出配置块，可独立开关：

```yaml
core_plugins:
  html_assembler:
    enabled: true
```

配置项（改 `core_plugins.yaml` 或 Web 面板配置中心）：
`max_template_bytes` / `max_image_bytes` / `max_output_bytes` /
`max_images` / `escape_html` / `on_missing`。

自测命令：`/html_asm` → 用内置 demo 模板渲染页面并回复输出路径。

## 占位符语法（写在 HTML 模板里）

| 语法 | 含义 | 示例 |
| --- | --- | --- |
| `{{ key }}` | 变量替换，自动 HTML 转义；支持嵌套 `a.b.c` 与列表下标 `items.0.name` | `{{ user }}` |
| `{{ img:key }}` | 图片内嵌，替换为 `data:image/xxx;base64,....` | `<img src="{{ img:avatar }}">` |
| `{{ raw:key }}` | 原文插入，不转义（内嵌 HTML/CSS/JS 片段用） | `{{ raw:style }}` |

缺失行为由 `on_missing` 控制：`keep`（保留原占位符，默认）/ `empty`（替换为空）/
`raise`（抛 KeyError）。

## 标准接口（门面）

### A. 模块级函数（推荐，其他插件通过主模块名访问）

```python
import sys
asm = sys.modules.get("core_plugin_html_assembler")   # 官方插件合成模块名
if asm is None:
    asm = sys.modules.get("plugin_html_assembler")    # 旧契约兼容别名，拿不到返回 None
if asm is not None:
    html = asm.render_html(tpl, data={"title": "hi"}, images={"avatar": "a.png"})
```

| 函数 | 说明 |
| --- | --- |
| `load_template(path) -> str` | 读取模板文件（UTF-8/GBK 自适应） |
| `render_html(template, data=None, images=None, output_path=None, **opts) -> str` | 一键装配；`template` 可为文本或文件路径；不传 `output_path` 返回 HTML 字符串，传则写入文件并返回路径 |
| `render_to_file(template, output_path, data=None, images=None, **opts) -> str` | 装配并写盘，返回输出路径 |
| `embed_image(source, mime=None) -> str` | 图片（路径/bytes）转 data URI |
| `make_assembler(**opts) -> HtmlAssembler` | 创建独立装配器实例 |
| `HtmlAssembler` | 类式门面（见下方） |

### B. 框架服务（单例）

```python
fw = ctx._framework
asm = fw.services.get("html_assembler")     # register 时注册的全局单例
```

### C. 类式门面（链式）

```python
a = asm.HtmlAssembler(template_path="card.html")
a.set("title", "今日战报").set_many({"user": "小明", "score": 99})
a.add_image("avatar", "D:/imgs/avatar.png")          # 路径 / bytes / data URI
a.add_images({"qr": "D:/imgs/qr.png"})
html = a.render()                                     # 返回 HTML 字符串
path = a.render_to_file("out/card.html")              # 写盘，返回路径
stats = a.last_stats                                  # {replaced, missing, images, output_bytes}
```

### 使用示例（完整）

```python
import sys
asm = sys.modules.get("core_plugin_html_assembler") or sys.modules.get("plugin_html_assembler")
if asm is None:
    return

tpl = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{{ title }}</title>
<style>body{font-family:sans-serif;padding:20px}</style>
</head><body>
<h1>{{ title }}</h1>
<p>用户：{{ user }}，得分：<b>{{ score }}</b></p>
<img src="{{ img:avatar }}" width="80">
{{ raw:extra_html }}
</body></html>"""

data = {"title": "战报", "user": "小明", "score": 99, "extra_html": "<hr><i>由引擎原文插入</i>"}
images = {"avatar": "D:/imgs/avatar.png"}

# 输出 HTML 字符串
html = asm.render_html(tpl, data=data, images=images)
# 或直接写盘
path = asm.render_to_file(tpl, "D:/out/report.html", data=data, images=images)
```

输出文件为**单文件 HTML**：图片已转 base64 内嵌，无任何外部资源引用，
浏览器双击即可打开渲染。

## 与其他插件集成约定

- 跨插件访问主模块：`sys.modules.get("core_plugin_html_assembler")`（旧契约别名
  `plugin_html_assembler` 在本插件 register 后同样可用；拿不到返回 None，
  不要用 `[]` 直接取；加载顺序不确定时可订阅 `system.plugin.loaded` 后再取）。
- 框架服务：`fw.services.get("html_assembler")`。
- 插件热重载：`register` 只做登记；服务单例在 `register` 内重建，安全。
- 引擎纯标准库实现，也可脱离 zcbot 独立 `import engine` 使用
  （`core_plugins/html_assembler/engine.py`）。
