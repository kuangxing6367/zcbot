# zcbot_render — ZCBOT 原生图片渲染扩展

基于 **Rust + pyo3** 编译的 Python 原生扩展，供 image_renderer 插件使用：
内存增量 <5MB、无子进程开销。abi3 稳定 ABI，一份扩展兼容 Python 3.9+。

## 目录结构

```
native/
├── Cargo.toml          # 工程配置（pyo3 + image + fontdue）
├── src/lib.rs          # render_card / render_text 实现
└── bin/                # 编译产物（插件按平台自动加载）
    ├── win64/zcbot_render.pyd     # Windows x64
    └── linux64/zcbot_render.so    # Linux x64
```

## 函数签名

```python
render_text(text, font_path, width=500, font_size=24, padding=20, options=None) -> bytes
render_card(title, content, font_path, timestamp, width=600, padding=30, options=None) -> bytes
render_list(title, items, font_path, width=600, padding=30, options=None) -> bytes
```

## Canvas 链式图元 API（第一层）

`Canvas.new(width, height, bg_color=None, font_path=None)` 创建画布（RGBA），
所有绘制方法返回自身可链式调用，最后 `to_png()` 输出 PNG bytes：

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
c.blur(3.0)                                          # 高斯模糊整张画布
png = c.to_png()
```

| 方法 | 说明 |
|---|---|
| `rect(x0,y0,x1,y1,radius,fill,outline,width)` | 圆角矩形（卡片/背景/边框） |
| `line(points,color,width)` | 折线（曲线图/网格/分隔线），points=[[x,y],...] |
| `circle(cx,cy,r,fill,outline,width)` | 圆形（头像/圆点） |
| `ellipse(x0,y0,x1,y1,fill,outline,width)` | 椭圆 |
| `text(x,y,text,font_size,color,align,wrap_width)` | 文本（自动换行） |
| `gradient_rect(x0,y0,x1,y1,color_a,color_b,direction)` | 渐变矩形（vertical/horizontal） |
| `paste(image_bytes,x,y,width,height)` | 贴图（alpha 混合） |
| `blur(radius)` | 高斯模糊 |
| `alpha_overlay(x0,y0,x1,y1,color,alpha)` | 半透明遮罩 |
| `text_metrics(text,font_size,wrap_width)` | 测量文本尺寸 (w,h) |
| `to_png()` | 输出 PNG bytes |

## 图像处理函数（第二层）

输入图片 bytes，输出 PNG bytes（原生实现，PIL 回退参数一致）：

| 函数 | 说明 |
|---|---|
| `image_resize(img,width,height,keep_ratio=True)` | 等比缩放（LANCZOS） |
| `image_crop_16_9(img)` | 16:9 居中裁剪（签到背景图） |
| `image_circle_crop(img,size=256)` | 圆形裁剪（头像） |
| `image_round_corners(img,radius=16)` | 圆角裁剪 |
| `image_blur(img,radius=4.0)` | 高斯模糊 |
| `image_flip(img,direction="horizontal")` | 水平/垂直翻转 |
| `image_rotate(img,angle=90)` | 90/180/270 旋转 |
| `image_gray(img)` | 灰度化 |
| `image_contrast(img,factor=1.5)` | 对比度 |
| `image_overlay(bg,fg,x,y)` | 前景合成到背景 |

### render_list items

`items` 为列表，每项为字符串或 dict：

| 键 | 说明 | 默认 |
|---|---|---|
| `name` | 左侧文本 | `""` |
| `value` | 右侧数值文本（右对齐） | `""`（不显示） |
| `rank` | 最左侧序号（右对齐到序号区） | 无 |
| `highlight` | 整行高亮背景 + 强调色文字 | `false` |

## options 参数（与 PIL 回退版完全一致）

颜色格式：`"#RRGGBB"` / `"#RRGGBBAA"` / `[r,g,b]` / `[r,g,b,a]`

| 键 | 说明 | 默认 |
|---|---|---|
| `text_color` | 文字颜色 | `[40,40,60,255]` |
| `title_color` | 卡片标题颜色 | `[20,30,60,255]` |
| `content_color` | 卡片内容颜色 | `[60,60,80,255]` |
| `footer_color` | 页脚颜色 | `[160,160,170,255]` |
| `accent_color` | 标题栏左侧彩条 | `[99,102,241,255]` |
| `name_color` | render_list 条目名称颜色 | `[40,40,60,255]` |
| `value_color` | render_list 条目数值颜色 | `[120,120,140,255]` |
| `highlight_bg` | render_list 高亮行背景 | `[236,239,255,255]` |
| `highlight_color` | render_list 高亮行文字 | `[99,102,241,255]` |
| `rank_color` | render_list 序号颜色 | `[160,160,170,255]` |
| `bg_color` | 纯色背景（有则覆盖默认渐变） | 无 |
| `bg_gradient` | 垂直渐变 `[顶部色, 底部色]` | 卡片默认 `[[248,250,255],[255,255,245]]` |
| `border_color` | 边框颜色 | 无（不画边框） |
| `border_width` | 边框宽度（px） | `2` |
| `radius` | 圆角半径（px） | `0` |
| `font_size` | render_text 字号 | `24` |
| `title_size` / `content_size` / `footer_size` | 卡片各段字号 | `28` / `20` / `14` |
| `item_size` | render_list 条目字号 | `18` |
| `line_height` | 行高（px），0=自动 | 文本 `font_size*1.35`，卡片 `30` |
| `padding` | 内边距 | 文本 `20`，卡片 `30` |
| `align` | 对齐：`"left"` / `"center"` / `"right"` | `"left"` |
| `show_footer` | 是否显示卡片页脚 | `true` |
| `footer_text` | 页脚右侧文字，传 `""` 或 `None` 则不显示 | `"ZGRIC"` |

示例：

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

## CI 编译（推荐，无需本地工具链）

推送后由 `.github/workflows/build-zcbot-render.yml` 自动构建，也可手动触发：

```bash
# Windows / Linux 各出一个 artifact，内含 zcbot_render.pyd / zcbot_render.so
gh workflow run build-zcbot-render.yml
gh run watch          # 等待完成
gh run download <run_id> --pattern 'zcbot_render-*'
```

下载解压后放入对应平台目录：

- `native/bin/win64/zcbot_render.pyd`（Windows x64）
- `native/bin/linux-x86_64/zcbot_render.so`（Linux x64，aarch64 用户放 `linux-aarch64/`）

## 本地编译（需要 MSVC 工具链 / Linux gcc）

```bash
pip install maturin
cd plugins/image_renderer/native
maturin build --release
# 从 target/wheels/*.whl 中取出 .pyd / .so，重命名为 zcbot_render.pyd / .so
# 放入 bin/win64/ 或 bin/linux64/
```

## 插件回退

`plugins/image_renderer/main.py` 启动时按平台探测 `native/bin/<平台>/` 下的扩展：
找到则使用原生渲染；找不到或加载失败自动回退 PIL（Pillow），功能与参数一致。
