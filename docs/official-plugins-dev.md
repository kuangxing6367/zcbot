# 官方插件开发实例

> 本篇把**官方插件的开发文档集中整合**到框架文档体系，避免散落在 `plugins/<name>/docs/` 导致割裂。
> 每个实例给出「能力 → 实现步骤 → 代码示例」，可直接照做。
> 插件基础 API 见 [API 参考](api-reference.md)；框架架构见 [architecture.md](architecture.md)。

---

## 一、custom_ui — 模板化接管 Web 面板

**能力**：从 GitHub 拉取网页模板 zip，用户下载/切换/激活后一键接管后台 Web 面板。
**适合学习**：插件如何接管前端、模板如何调用框架 API。

### 1.1 模板 zip 结构

模板是一个 zip 包，内部结构即一个静态网站，放在插件仓库顶层 `webui/<模板名>.zip`：

```text
<模板名>.zip
├── index.html          # 入口页（必填，根路径访问的文件）
├── css/
│   └── style.css
├── js/
│   └── app.js
└── img/
    └── logo.png
```

- zip 根目录直接放文件（**不要**套一层外层文件夹）
- 入口文件必须是 `index.html`
- 引用静态资源用相对路径或 `/custom_ui/<path>` 均可

### 1.2 被接管后的 URL 规则

| 路径 | 对应文件 |
| ---- | ---- |
| `/custom_ui/` | `templates/<active>/index.html` |
| `/custom_ui/<path>` | `templates/<active>/<path>`（防目录穿越） |

> 框架自身的 `/css/*`、`/js/*`、`/img/*` **不会被**模板接管，仍指向框架前端。模板内资源建议用相对路径。

### 1.3 模板内调用框架 API

模板页与框架同源，可直接 `fetch` 后台 API，鉴权带 `Bearer` token：

```javascript
// 登录后浏览器 localStorage 存有 token
const token = localStorage.getItem('zcbot_token')

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
  })
  return r.json()
}

// 示例：拉取框架状态
const d = await api('/api/dashboard')
console.log(d.data)   // { bots, plugins, ... }
```

常用端点：`/api/dashboard`、`/api/runtime/stats`、`/api/plugins`、`/api/runtime_logs?limit=60`、`/api/me`。模板源与插件源同仓库（`kuangxing6367/zcbot_plugins`），zip 放 `webui/`。

### 1.4 打包并发布模板

```bash
# 在插件仓库根目录
cd webui
zip -r mytheme.zip mytheme/   # zip 根目录直接是 index.html，不套 mytheme/ 层
```

或用 Python（推荐，跨平台）：

```python
import zipfile, os
src = 'mytheme'  # 目录内直接是 index.html
with zipfile.ZipFile('webui/mytheme.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for f in files:
            fp = os.path.join(root, f)
            z.write(fp, os.path.relpath(fp, src))
```

推送 `webui/<模板名>.zip` 到 `main` 分支后，管理页刷新即可见新模板。

### 1.5 接管机制（框架侧）

custom_ui 依赖框架 `override_webui` 能力：

```python
# 插件调用后，框架根路由 / 变为：
entry = framework.plugin_loader.get_override_webui()
if entry:
    return redirect(f'/{entry}/', code=302)
return send_from_directory(_web_root_dir(), 'index.html')
```

- **自动回退**：插件被禁用/卸载/删除时，`unload_plugin` 自动 `clear_override_webui()`，回到默认前端。
- **刷新过快保护**：同一 IP 5 秒内刷新 ≥5 次，重定向到 `/reset` 恢复页（排除 `/api/` 与静态资源）。
- **路由注册方式（重要）**：插件动态注册 Flask 路由**不能**用 `app.add_url_rule`（Flask 处理首次请求后调用会抛异常），应改用底层方式：

```python
from werkzeug.routing import Rule
if rule_path not in {str(r.rule) for r in app.url_map.iter_rules()}:
    app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
app.view_functions[endpoint] = fn  # 已存在时仅刷新 view 函数
```

### 1.6 HTTP API（`/api/custom_ui/*`）

| 方法 & 路径 | 说明 |
| ---- | ---- |
| `GET /api/custom_ui/templates` | 远端可用模板 + 本地已下载 + 当前激活（写操作需登录） |
| `POST /api/custom_ui/templates/<name>/download` | 下载 `<name>.zip` 解压到 `templates/<name>/` |
| `POST /api/custom_ui/templates/<name>/activate` | 设为激活模板（刷新生效，需先下载） |
| `POST /api/custom_ui/override` | 接管前端：写 `override.txt` + `ctx.override_webui()`（需已有激活模板） |
| `DELETE /api/custom_ui/override` | 取消接管：清标记 + `clear_override_webui()` |
| `POST /api/custom_ui/reset` | 卸载插件并停用，回到默认前端 |

`GET /api/custom_ui/templates` 返回示例：

```json
{
  "code": 0,
  "data": {
    "remote": [{ "name": "default", "file": "default.zip", "size": 11025, "download_url": "..." }],
    "local": ["default"],
    "active": "default",
    "error": ""
  }
}
```

### 1.7 二次开发 custom_ui

插件入口 `register(ctx)`：

```python
def register(ctx):
    _init_dat_dir(ctx)              # 数据目录（plugins_dat/custom_ui）
    ctx.webui("个性化前端", "index.html", icon="🎨", order=60)
    _register_routes(ctx)           # Flask 路由 + API
    if _is_override_enabled():      # 接管状态恢复（重启保持）
        active = _get_active_template()
        if active:
            ctx.override_webui()
```

关键内部函数：

| 函数 | 作用 |
| ---- | ---- |
| `_dat_dir()` | 插件数据目录（模板 + 状态标记） |
| `_templates_root()` | 模板根目录 |
| `_get_active_template()` / `_set_active_template(name)` | 读 / 写激活模板 |
| `_is_override_enabled()` / `_set_override(bool)` | 读 / 写接管标记 |
| `_list_remote_templates()` | 列出仓库 `webui/` 下模板 |
| `_download_template(ctx, name)` | 下载 zip 解压到 `templates/<name>/` |
| `_safe_template_name(name)` | 模板名白名单校验（字母数字 `_-.`） |
| `_github_raw_candidates(path)` | GitHub raw 候选地址（代理 → 镜像 → 直连） |

管理页 `manage.html` 是原生 HTML/JS（无构建步骤），改完推送、服务器点「更新」即生效（新版本需递增 `plugin.yaml` 版本号）。

---

## 二、image_renderer — 原生扩展（Rust + pyo3）

**能力**：高性能图片渲染（卡片/文本/列表），原生 Rust 扩展优先，缺失自动回退 PIL。
**适合学习**：插件如何用原生扩展做高性能底层能力、跨平台回退。

### 2.1 架构绑定

| 平台 | 产物路径 |
| ---- | ---- |
| Windows x86_64 | `native/bin/win64/zcbot_render.pyd` |
| Linux x86_64 | `native/bin/linux-x86_64/zcbot_render.so` |
| Linux aarch64 | `native/bin/linux-aarch64/zcbot_render.so` |

Rust 源码在 `native/`（`Cargo.toml` + `src/lib.rs`），CI 见框架仓库 `.github/workflows/build-zcbot-render.yml`（三平台矩阵）。

### 2.2 函数签名

```python
render_text(text, font_path, width=500, font_size=24, padding=20, options=None) -> bytes
render_card(title, content, font_path, timestamp, width=600, padding=30, options=None) -> bytes
render_list(title, items, font_path, width=600, padding=30, options=None) -> bytes
```

### 2.3 Canvas 链式图元 API（第一层）

`Canvas.new(width, height, bg_color=None, font_path=None)` 创建画布（RGBA），绘制方法返回自身可链式调用，最后 `to_png()` 输出 PNG bytes：

```python
c = Canvas.new(600, 400, bg_color="#f8faff", font_path=font)
c.rect(20, 20, 580, 380, radius=12, fill="#fff", outline="#ccc", width=1)
c.gradient_rect(20, 20, 580, 200, "#f8faff", "#fffcf8", "vertical")
c.circle(300, 120, 40, fill="#ffcc00")
c.ellipse(100, 300, 300, 350, fill="#3366cc")
c.line([[50, 350], [200, 300], [350, 260]], color="#3366cc", width=3)
c.text(60, 60, "标题", font_size=28, color="#141e3c", align="left", wrap_width=500)
c.paste(img_bytes, 10, 10, width=100, height=100)   # 贴图（png/jpeg）
c.alpha_overlay(0, 0, 600, 400, "#000000", 30)      # 半透明遮罩
c.blur(3.0)                                          # 高斯模糊
png = c.to_png()
```

| 方法 | 说明 |
|---|---|
| `rect(x0,y0,x1,y1,radius,fill,outline,width)` | 圆角矩形 |
| `line(points,color,width)` | 折线，points=`[[x,y],...]` |
| `circle(cx,cy,r,fill,outline,width)` | 圆形 |
| `ellipse(x0,y0,x1,y1,fill,outline,width)` | 椭圆 |
| `text(x,y,text,font_size,color,align,wrap_width)` | 文本（自动换行） |
| `gradient_rect(x0,y0,x1,y1,color_a,color_b,direction)` | 渐变矩形（vertical/horizontal） |
| `paste(image_bytes,x,y,width,height)` | 贴图（alpha 混合） |
| `blur(radius)` | 高斯模糊 |
| `alpha_overlay(x0,y0,x1,y1,color,alpha)` | 半透明遮罩 |
| `text_metrics(text,font_size,wrap_width)` | 测量文本尺寸 (w,h) |
| `to_png()` | 输出 PNG bytes |

### 2.4 图像处理函数（第二层）

输入图片 bytes，输出 PNG bytes（原生实现，PIL 回退参数一致）：

| 函数 | 说明 |
|---|---|
| `image_resize(img,width,height,keep_ratio=True)` | 等比缩放（LANCZOS） |
| `image_crop_16_9(img)` | 16:9 居中裁剪 |
| `image_circle_crop(img,size=256)` | 圆形裁剪（头像） |
| `image_round_corners(img,radius=16)` | 圆角裁剪 |
| `image_blur(img,radius=4.0)` | 高斯模糊 |
| `image_flip(img,direction="horizontal")` | 水平/垂直翻转 |
| `image_rotate(img,angle=90)` | 90/180/270 旋转 |
| `image_gray(img)` | 灰度化 |
| `image_contrast(img,factor=1.5)` | 对比度 |
| `image_overlay(bg,fg,x,y)` | 前景合成到背景 |

### 2.5 render_list items 与 options

`items` 为列表，每项是字符串或 dict：`name`（左文本）、`value`（右数值）、`rank`（序号）、`highlight`（整行高亮）。

`options` 颜色格式 `"#RRGGBB"` / `"#RRGGBBAA"` / `[r,g,b]` / `[r,g,b,a]`，常用键：`text_color`、`title_color`、`content_color`、`accent_color`、`bg_gradient`、`border_color`、`border_width`、`radius`、`font_size`、`align`、`show_footer`。

```python
render_card(
    "公告", "这是一条测试公告",
    font_path, "2026-08-07 10:00", 600, 30,
    {
        "bg_gradient": [["#1e293b", "#334155"]],
        "title_color": "#ffffff",
        "content_color": "#cbd5e1",
        "accent_color": "#38bdf8",
        "radius": 12,
        "border_color": "#475569",
        "border_width": 1,
        "align": "center",
    },
)
```

### 2.6 编译与 CI

CI 自动构建（推荐，无需本地工具链）：

```bash
gh workflow run build-zcbot-render.yml
gh run watch
gh run download <run_id> --pattern 'zcbot_render-*'
# 放入 native/bin/win64/ 或 native/bin/linux-x86_64/
```

本地编译（需 MSVC / gcc）：

```bash
pip install maturin
cd plugins/image_renderer/native
maturin build --release
# 从 target/wheels/*.whl 取出 .pyd/.so，重命名为 zcbot_render.pyd/.so，放入 bin/
```

### 2.7 插件回退机制

`plugins/image_renderer/main.py` 启动时按平台探测 `native/bin/<平台>/` 下的扩展：找到则用原生渲染；找不到或加载失败**自动回退 PIL（Pillow）**，功能与参数一致。依赖 `Pillow>=10.0.0`。

---

## 三、复杂插件模式（llm_chat / broadcast）

复杂插件通常组合多项框架能力，可作为你写插件的骨架：

| 能力 | 用什么 |
| ---- | ---- |
| Web 配置面板 | `_conf_schema.json` 暴露模型/密钥/人格等 |
| 持久化 | `ctx.create_table` + `ctx.db_execute` / `ctx.db_query` |
| 管理/聊天控制台 | `ctx.webui(title=, entry=, icon=, order=)` + `ctx.dashboard_card` |
| 定时清理 | `ctx.task("…", cleanup)` |
| 消息拦截 | `ctx.on("message", fn)` / `ctx.on_raw_message(fn)` |

参考实现：`plugins/llm_chat/main.py`（配置 + 长期记忆 + WebUI 控制台）、`plugins/broadcast/main.py`（定时群发）。

---

## 四、相关文档

- [插件开发详解](plugin-tutorial.md) — 从零写插件（逐行讲解）
- [API 参考](api-reference.md) — `ctx` 全部方法 + `Event` 字段
- [框架架构与开发指南](architecture.md) — 模块/消息流/生命周期/扩展框架
- [配置系统](configuration.md) — `plugin.yaml` / `_conf_schema.json`
- [官方插件使用手册](official-plugins.md) — 每个官方插件的用法
