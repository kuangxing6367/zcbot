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

## 本地编译（需要 MSVC 工具链 / Linux gcc）

```bash
pip install maturin
cd plugins/image_renderer/native
maturin build --release
# 从 target/wheels/*.whl 中取出 .pyd / .so，重命名为 zcbot_render.pyd / .so
# 放入 bin/win64/ 或 bin/linux64/
```

## CI 自动构建

推送/手动触发 `.github/workflows/build-zcbot-render.yml`，自动在
Windows / Ubuntu 上编译并上传 Artifact（`zcbot_render-windows-latest` /
`zcbot_render-ubuntu-latest`），下载后放入对应平台目录即可。

## 插件回退

`plugins/image_renderer/main.py` 启动时按平台探测 `native/bin/<平台>/` 下的扩展：
找到则使用原生渲染；找不到或加载失败自动回退 PIL（Pillow），功能一致。
