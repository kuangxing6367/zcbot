---
title: 品牌标识
description: zcbot 标志的形态释义、变体家族、色彩系统与使用规范
---

# 品牌标识

标志是一个**几何 Z**：上下两横完整，斜笔画断成三节等长块。

所有图形均为手工矢量构造（非位图、非截图、无水印），源文件见[文末清单](#源文件)。
仓库内的同名文档见 [`BRAND.md`](https://github.com/kuangxing6367/zcbot/blob/main/BRAND.md)，两处内容保持一致。

## 总 logo

横版锁定组合是**总 logo**，用于 README 头图、文档页眉、社交卡片等需要完整识别的场合。

<div style="display:flex;gap:16px;flex-wrap:wrap;margin:20px 0;">
  <div style="flex:1;min-width:260px;background:#F4F5F8;border-radius:16px;padding:36px 28px;display:flex;align-items:center;justify-content:center;">
    <img src="/brand/zcbot-logo-horizontal-light.svg" alt="zcbot 亮底版" width="240" />
  </div>
  <div style="flex:1;min-width:260px;background:#0B0D14;border-radius:16px;padding:36px 28px;display:flex;align-items:center;justify-content:center;">
    <img src="/brand/zcbot-logo-horizontal-dark.svg" alt="zcbot 暗底版" width="240" />
  </div>
</div>

## 释义

- **Z** —— 承接项目名 zcbot 的首字母，也延续了早期 ZC 字标的字母基因。
- **断裂的斜笔画** —— 讲的是消息沿路径**分包传输**：一个事件进入内核，被拆成若干段、经扩展点逐段处理、再拼回响应。这是机器人框架每天在做的事，也是这个剪影唯一不可替代的地方。
- **两横保持完整** —— 内核的入口与出口是连续不断的，断的只是中间的处理链路。

形态刻意避开了「中心实心 + 对称环绕」这条 SaaS 通用路径 —— 那一类剪影谁都能画，认不出是谁。

## 变体家族

| 变体 | 用途 | 最小尺寸 | 文件 |
| ---- | ---- | -------- | ---- |
| ① 主锁定组合（横版） | **总 logo**：README 头图、文档页眉、社交卡片 | 宽 96px | `zcbot-logo-horizontal-{light,dark}.svg` |
| ② 竖版组合 | 方形版位与居中场景：启动屏、幻灯片封面 | 宽 64px | `zcbot-logo-stacked.svg` |
| ③ 纯图标 | 侧边栏、favicon、机器人头像 | 24px | `zcbot-icon.svg` |
| ④ 简化版图标 | **16px 及以下专用**：favicon、极小徽标 | 16px | `zcbot-icon-mini.svg` |
| ⑤ 单色版 | 单色印刷、水印、受限配色平台 | 24px | 由 ③④ 改单色得到 |

### 竖版组合

<div style="display:flex;justify-content:center;background:#0B0D14;border-radius:16px;padding:32px;margin:20px 0;">
  <img src="/brand/zcbot-logo-stacked.svg" alt="zcbot 竖版组合" width="140" />
</div>

### 纯图标与缩放

<div style="display:flex;gap:16px;flex-wrap:wrap;margin:20px 0;">
  <div style="flex:1;min-width:200px;background:#F4F5F8;border-radius:16px;padding:28px;display:flex;align-items:center;justify-content:center;gap:20px;">
    <img src="/brand/zcbot-icon-light.svg" alt="图标 64" width="64" />
    <img src="/brand/zcbot-icon-light.svg" alt="图标 32" width="32" />
    <img src="/brand/zcbot-icon-light.svg" alt="图标 24" width="24" />
  </div>
  <div style="flex:1;min-width:200px;background:#0B0D14;border-radius:16px;padding:28px;display:flex;align-items:center;justify-content:center;gap:20px;">
    <img src="/brand/zcbot-icon-dark.svg" alt="暗底图标 64" width="64" />
    <img src="/brand/zcbot-icon-dark.svg" alt="暗底图标 32" width="32" />
    <img src="/brand/zcbot-icon-dark.svg" alt="暗底图标 24" width="24" />
  </div>
</div>

### 16px 必须换简化版

标准版的断口是 8/96 单位。缩到 16px 时断口只剩 **1.33px**，抗锯齿后会糊成一团黑点，Z 读不出来。
简化版把斜笔画连成一条实线，牺牲「分包」语义换可辨识性 —— 这是 favicon 能不能看清的分水岭。

| 尺寸 | 用哪个 |
| ---- | ------ |
| ≥ 24px | 标准版（带断口） |
| ≤ 16px | 简化版（连续斜线） |

<div style="display:flex;gap:16px;flex-wrap:wrap;margin:20px 0;">
  <div style="flex:1;min-width:200px;background:#0B0D14;border-radius:16px;padding:28px;display:flex;align-items:center;justify-content:center;gap:24px;">
    <img src="/brand/zcbot-icon-dark.svg" alt="标准版 16px" width="16" />
    <img src="/brand/zcbot-icon-mini.svg" alt="简化版 16px" width="16" />
  </div>
</div>

左为标准版（16px 下断口糊掉），右为简化版（连续斜线）。

## 构造规格

单位制为 96×96 viewBox，所有坐标可直接用于复现：

| 元素 | 定义 |
| ---- | ---- |
| 上横 | `(16,25) → (80,25)` |
| 下横 | `(16,71) → (80,71)` |
| 斜笔画 | 由 `(80,25)` 指向 `(16,71)`，弧长 `L = 78.816` |
| 分段 | 三段等长，各 `SEG = (L − 2×GAP) / 3 = 20.939`，断口 `GAP = 8` |
| 笔画宽 | `14`（约为图标宽的 14.6%） |
| 端点 | butt cap（平头，不加圆角） |

::: tip
重新生成位图档时，请以本节的坐标为准，不要凭截图放大。
:::

## 色彩系统

<div style="display:flex;gap:12px;flex-wrap:wrap;margin:20px 0;">
  <div style="flex:1;min-width:150px;border:1px solid var(--vp-c-divider);border-radius:12px;overflow:hidden;">
    <div style="height:64px;background:#6366F1;"></div>
    <div style="padding:12px;">
      <div style="font-size:13px;font-weight:600;">主色 Primary</div>
      <div style="font-size:12px;opacity:.7;">#6366F1 · 亮底标准色</div>
    </div>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid var(--vp-c-divider);border-radius:12px;overflow:hidden;">
    <div style="height:64px;background:#818CF8;"></div>
    <div style="padding:12px;">
      <div style="font-size:13px;font-weight:600;">暗底主色</div>
      <div style="font-size:12px;opacity:.7;">#818CF8 · 深底必用</div>
    </div>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid var(--vp-c-divider);border-radius:12px;overflow:hidden;">
    <div style="height:64px;background:#38BDF8;"></div>
    <div style="padding:12px;">
      <div style="font-size:13px;font-weight:600;">辅助色 Accent</div>
      <div style="font-size:12px;opacity:.7;">#38BDF8 · 渐变搭配</div>
    </div>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid var(--vp-c-divider);border-radius:12px;overflow:hidden;">
    <div style="height:64px;background:#0B0D14;"></div>
    <div style="padding:12px;">
      <div style="font-size:13px;font-weight:600;">暗底 Canvas</div>
      <div style="font-size:12px;opacity:.7;">#0B0D14 · 后台底色</div>
    </div>
  </div>
  <div style="flex:1;min-width:150px;border:1px solid var(--vp-c-divider);border-radius:12px;overflow:hidden;">
    <div style="height:64px;background:#E8EBF4;"></div>
    <div style="padding:12px;">
      <div style="font-size:13px;font-weight:600;">墨色 Ink</div>
      <div style="font-size:12px;opacity:.7;">#E8EBF4 · 暗底文字</div>
    </div>
  </div>
</div>

### 深底提亮规则（必须遵守）

`#6366F1` 压在 `#0B0D14` 这类深底上会发闷、边缘发虚。
**暗底场景一律换成 `#818CF8`** —— 这不是可选优化，是可读性要求。

单色版纯黑 `#0A0A0A` 与纯白 `#FFFFFF` 两端都成立，用于单色印刷、水印、第三方受限配色平台。

## 使用规范

**保护空间** —— 标志四周留白不小于图标宽度的 **1/3**，任何文字、边框、装饰不得侵入。

**深底提亮** —— 见上，`#6366F1` → `#818CF8`。

**双主题切换** —— WebUI 有亮/暗双主题（`html.dark`），侧边栏与登录卡底色随主题变化，
因此前端**必须按主题加载对应版本**，不能只用一份 PNG 通吃：

```
img/zcbot-icon-light.svg        # 亮主题
img/zcbot-icon-dark.svg         # 暗主题
img/zcbot-icon-mini-light.svg   # 折叠态 · 亮
img/zcbot-icon-mini-dark.svg    # 折叠态 · 暗
```

**禁止** —— 拉伸变形、改色、加描边或投影、旋转、压在花哨图片上直接使用、把断口挪位或改宽度。

**交付格式** —— 矢量 SVG 优先；位图提供 16 / 32 / 64 / 128 / 256 / 512 六档 PNG。

## 项目落地点

| 位置 | 内容 |
| ---- | ---- |
| `webui/public/img/logo.png` | 512×512 方形图标（`#6366F1`），侧边栏与登录页的通用兜底 |
| `webui/public/img/zcbot-icon-*.svg` | 主题双版图标（标准版 + 简化版） |
| `webui/public/img/apple-touch-icon.png` | 256×256，iOS 添加到主屏用 |
| `webui/public/favicon.ico` | 16 / 32 / 48 三档打包 |
| `webui/index.html` | `favicon` + `apple-touch-icon` + `theme-color` 引用 |
| `webui/src/views/Layout.vue` | 侧边栏 30×30、折叠态 24×24（简化版），按主题切换 |
| `webui/src/views/Login.vue` | 登录卡 64×64，按主题切换 |
| `core_plugins/webui/web/` | 前端构建产物目录 |
| `docs/public/brand/` | 全部矢量源文件（文档站静态资源目录） |
| `docs/.vitepress/config.mjs` | 导航栏标志（明暗双版）+ favicon 引用 |

### 改前端后的构建同步

`webui/src/` 下的改动需要重新构建才会进入产物目录：

```bash
cd webui
npm run build      # 产物输出到 ../core_plugins/webui/web/
```

`webui/public/` 下的静态文件（`img/`、`favicon.ico`）由 Vite 原样拷贝，也会随构建同步。

## 源文件

全部存放于 [`docs/public/brand/`](https://github.com/kuangxing6367/zcbot/tree/main/docs/public/brand)：

| 文件 | 说明 |
| ---- | ---- |
| `zcbot-logo-horizontal-light.svg` | 主锁定组合 · 亮底版 |
| `zcbot-logo-horizontal-dark.svg` | 主锁定组合 · 暗底版 |
| `zcbot-logo-stacked.svg` | 竖版锁定组合 · 暗底版 |
| `zcbot-icon.svg` | 纯图标 · 亮底标准色 `#6366F1` |
| `zcbot-icon-light.svg` / `zcbot-icon-dark.svg` | 纯图标 · 主题双版 |
| `zcbot-icon-mini.svg` | 简化版图标 · 16px 专用 |

::: warning 标准字未转曲
横版/竖版组合里的 `zcbot` 字标是 `<text>` 元素，字体回退链为 `Inter → Segoe UI → -apple-system → Helvetica Neue → Arial`。
**没有 Inter 的环境会回退到系统无衬线字体**，字形和字距有细微差异。

- 纯图标（③④⑤）**无字体依赖**，任何环境都一致，优先用于 favicon、头像、侧边栏；
- 需要字标严格一致的场合（印刷、对外物料），请先把 `<text>` 转为路径再分发。
:::

## 开源协议

与项目主体一致：MIT + Apache 2.0 双协议，任选其一适用。
标志的使用不额外设限，但请保持形态与配色不被改动。
