# 图像渲染器

图像渲染插件（官方插件）。提供文字 / 卡片渲染为图片的能力，供其他插件生成富媒体消息。

## 元信息

- 优先级：200
- 进程归属：host（单进程与双进程宿主均加载）
- 默认开关：启用

## 渲染后端

两套实现，对外是同一套 Canvas API，**用哪套由配置决定**：

| `image_renderer.backend` | 实现 | 说明 |
|---|---|---|
| `pil`（默认） | Pillow | 零额外依赖，稳定；字体缺失时自动用系统字体 |
| `rust` | 原生扩展 `zcbot_render` | 需下载产物；**资源缺失时不回退 PIL，直接报错** |

改法：WebUI 配置页 → 官方插件 → 图像渲染器 → `backend`，或直接改
`core_plugins.yaml` 里 `image_renderer.backend`。改完重启生效。

### 为什么 rust 失败不回退

`rust` 是用户在面板里的显式选择。若资源不可用时静默回退 PIL，现象是「选了 rust，
跑的却是 PIL」，性能与内存表现都对不上，问题会一直藏到有人拿数据对账才发现。
所以这里选择明确失败：日志打 ERROR、渲染抛错，提示改回 `pil` 或补齐资源。

## 适用与局限（写在这里，免得靠猜）

### 它是干什么的

Pillow 之外的**第二种渲染实现**（Rust：fontdue 光栅化 + image 编码），对外是同一套
Canvas API，调用方无感切换。

它的真实价值只有一个场景：**没装 Pillow，或该平台没有 Pillow wheel、只能源码编译**
（部分嵌入式 / 非主流架构 / 精简镜像）。这类环境下它是唯一能跑的渲染后端。
如果你能顺利 `pip install Pillow`，那 rust 后端对你没有收益。

### 它做不到什么（实测，win64 / Python 3.13，n=30 中位数）

| 项目 | PIL | rust | 结论 |
|---|---|---|---|
| 3 行中文纯渲染 | 1.27ms | 3.11ms | rust 慢 **2.4×** |
| PNG 编码 | 1.86ms | 更快（含在调用内） | rust 占优 |
| 文本端到端 | 3.38ms | 3.10ms | 基本打平 |
| 卡片端到端 | 6.95ms | 8.66ms | rust 慢 **25%** |
| 内存增量 | +25.2MB | +20.6MB | rust 低 18% |

其他边界：

- **只有 12 个绘图原语**：`rect`/`line`/`circle`/`ellipse`/`gradient_rect`/`text`/
  `text_metrics`/`paste`/`blur`/`alpha_overlay`/`to_png`/`get_size`。
  没有 `resize`/`crop`/`rotate`/`arc`/`polygon`/`save` —— 需要这些的插件用不了。
- **不减少依赖**：Pillow 仍在 `requirements.txt`（它是默认后端）。
- **生僻字要靠系统字体**：子集字体覆盖不到的字会落到 `msyh.ttc` 之类的系统字体。
- **本机测试没有复现慢性内存泄漏**：600 轮 `image_resize`，PIL +0.2MB、rust 无增长；
  但 PIL 的 `Image.MAX_IMAGE_PIXELS` / `ImageFile.LOAD_TRUNCATED_IMAGES` 是**模块级全局**，
  同进程任何插件都能改，不能当作安全边界 —— 所以 `main.py` 在解码前自己做像素预算
  （`_guard_image_bytes`，上限 1.34 亿像素 = 512MB 位图），不依赖任何一方的默认阈值。

## 字体与原生产物不入源码

字体子集（约 8MB）与 7 个平台的原生扩展（约 7MB）**不在源码仓库里**，也不进 wheel。
理由：合计 15MB 会拖慢 clone、让每次打包体积翻倍，而任何一台机器只会用到其中一个平台。

启用插件后首次运行会自动下载到 `data/image_renderer/assets/`：

```yaml
image_renderer:
  backend: pil
  # ask（默认，不下载只提示）/ auto（自动下载约 9MB）/ manual（只用手动放置的文件）
  asset_consent: ask
  asset_url: https://github.com/kuangxing6367/zcbot-render-assets/releases/latest/download/assets.zip
  asset_timeout: 30
```

**这些项在 WebUI 里就能改**：设置 → 「图片渲染」分组（`/settings?tab=image_renderer`）。
保存写进 `config.yaml` 的 `image_renderer` 段，启动时覆盖 `core_plugins.yaml` 里的同名字段。
改完需重启框架生效。资源没就绪时，打开后台顶部会出现一条横幅说明缺什么、多大、以及
「PIL 后端根本不需要它」，点「前往设置」直达该分组——横幅只读，**不会触发任何下载**。

`asset_url` 指向**独立的资源仓库** `zcbot-render-assets`，不是主仓的 Release。
二进制与字体的更新节奏和代码发版无关（补个字形、重编一个平台的 `.so` 不该逼框架 bump
版本号），混在一个仓库只会让 clone / CI / wheel 一起变胖。资源仓库的 latest 是固定链接，
资源重发时覆盖同名 Release，这里的配置永远不用改。

### 为什么默认不自动下载

切到 `rust` 需要拉约 **9MB**。在按流量计费、内网或离线环境下静默下载会被当成事故，
所以 `asset_consent` 默认是 `ask`：只打日志说明「需要多少流量、放哪里」，
**一个字节都不下**。用户确认后改成 `auto` 才会下载；不想联网就改成 `manual` 自己放文件。

- 已就绪则不发请求（源码运行、开发机上插件目录本来就有文件时直接使用）
- 下载走 `.part` 临时文件 + `os.replace` 原子替换；解压前做路径穿越校验
- 每个文件按 `manifest.json` 的 sha256 + size 逐条核对，坏包不进正式目录
- 下载失败**只影响 rust 后端**：`pil` 会退到系统字体继续工作（日志 ERROR 记录原因）

### 写不了盘怎么办

容器只读挂载、分区满、`ProtectSystem=strict`、文件被杀软占用——这些都会让资源装不上。
处理原则：**资源是可选增强，写不了盘不许影响渲染本身**。

| 情况 | 行为 |
|---|---|
| data 目录建不出来 | 探测阶段直接返回，**不联网**，报错点名哪个路径建不出来 |
| 目录存在但不可写 | 写探针文件验证后才下结论（`os.access` 在 Windows 上不可信） |
| 空间不足 | 下载前按 40MB 预检，拿到 `Content-Length` 后再卡一次，不留半截文件 |
| 下到一半磁盘满 | 清理 `.part`，报「磁盘写入失败」，不报成网络错误 |
| 正式目录删不掉 | 退化为逐文件就地覆盖，不当作整体失败 |
| 覆盖也写不进去 | **保住旧资源**，标 `stale=True`，日志说明「仍在用旧版本」 |

状态里的 `writable` / `free_mb` / `stale` 用来区分「网络源没配」「目录写不进去」
「在用旧版」三种情形。`writable=false` 时不要去查网络，那是权限问题。

更新失败**不等于没有资源**：已有可用资源时状态是 `ok=True` + `stale=True`，
面板据此提示「仍在使用旧版本 vX」，而不是让人以为渲染坏了。

混合态也做了防护：就地覆盖时 `manifest.json` 只在其余文件**全部**写成功后才更新。
否则会出现「版本号显示新版、字体还是旧内容」，字表和 ttf 还可能对不上，比直接失败更难查。

发布侧：`python scripts/build_render_assets.py --strict` 生成 `dist/assets.zip`
（含 `manifest.json` 与各文件 sha256），再用资源仓库的 `publish.py` 发到 Release。

## 字体分层

内置字体分级（常用字子集 / 扩展子集），在体积极小时优先加载高频字库，降低内存占用。
子集缺失时回退完整字体 → 系统字体，逐级兜底，不会静默渲染成方框。

## 命令

- `/render_card` 渲染卡片图
- `/render_text` 渲染文字图

## 对外能力

插件注册后提供 Canvas 式渲染接口，其它插件可调用生成图片，再以 `base64://`
消息段发出。具体 API 以插件 `register` 时挂载到上下文的渲染方法为准。

## 依赖

- 原生后端：构建产物 `zcbot_render`（各平台 `.pyd` / `.so`），无运行时三方依赖
- 回退后端：Pillow（PIL）

构建原生后端需对应工具链（Windows 下需 MSVC C++ 工具链；CI 在 Windows / Linux 分别产出
`.pyd` / `.so`）。无工具链时自动回退 PIL，功能可用但性能较低。
