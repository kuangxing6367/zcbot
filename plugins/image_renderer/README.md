# 图片渲染器 (image_renderer)

通用图片渲染引擎，提供信息卡片 / 文字图片绘制工具。**原生 Rust 扩展优先**（`zcbot_render`，Rust + pyo3 编译，abi3 稳定 ABI 兼容 Python 3.9+），按平台自动加载；其他架构或二进制缺失时**自动回退 PIL（Pillow）渲染**，功能一致。

## 原生扩展（按架构强制绑定）

| 平台 | 产物路径 |
| ---- | ---- |
| Windows x86_64 | `native/bin/win64/zcbot_render.pyd` |
| Linux x86_64 | `native/bin/linux-x86_64/zcbot_render.so` |
| Linux aarch64 | `native/bin/linux-aarch64/zcbot_render.so` |

Rust 源码见 `native/`（`Cargo.toml` + `src/lib.rs`），CI 工作流见框架仓库 `.github/workflows/build-zcbot-render.yml`（三平台矩阵构建）。

## 用法

| 命令 | 说明 |
| ---- | ---- |
| `/render_card 标题 \| 内容` | 生成信息卡片图片 |
| `/render_text 文字` | 将文字渲染为图片 |

## 依赖

- `Pillow>=10.0.0`（原生扩展缺失时回退渲染，默认安装）
