# rust_accel 集成说明（2026-09-28）

## 本包内容

- `core_plugins/rust_accel/` —— Rust 加速层官方插件（唯一新增目录，包含
  Rust 源码工程、Python 插件壳、冒烟/基准脚本、README）
- 本文件：集成改动与验证记录

## 仓库内已落盘的集成改动

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `framework/config.py` | `_CORE_PLUGIN_SCHEMA` 在 discord 之后新增 `rust_accel` 默认配置块 | enabled: false、ws_host/ws_port/access_token/max_frame_size/max_pending_events/max_pending_bytes/stats_interval_secs/ipc_strip_raw/inherit_onebot/binary_path/auto_build |
| `core_plugins.yaml` | `tools/scan_core_plugins.py --write` 生成 `rust_accel` 段（enabled: false） | 该文件为本机 gitignore 配置，不入库；启停唯一权威 |

> 插件目录内不含 `target/` 构建产物——release 二进制需按目标平台构建
> （见插件 README「构建」节），或配置 `binary_path` / `RUST_ACCEL_BIN`。

## 验证记录（Windows / Python 3.13 / release 二进制）

| 检查 | 结果 |
| --- | --- |
| 插件冒烟 `python core_plugins/rust_accel/plugin_smoke.py` | 连跑 3 轮 PLUGIN_SMOKE_PASS，unregister 与停机均为优雅退出，0 残留子进程 |
| pytest（排除脚本式 test_perm/test_plugin_imports） | 135 passed（需 `-o asyncio_mode=auto`，已装 pytest-asyncio 1.4.0） |
| `python tests/test_perm.py` | 43/43 |
| `python tests/test_plugin_imports.py` | 25/25 |
| 端到端基准 `python core_plugins/rust_accel/bench.py 2000` | 见插件 README「性能基准」节 |

## 常见命令

```bash
# 启用（会先停掉 onebot_adapter 以避免双接入竞争）
python tools/scan_core_plugins.py --disable onebot_adapter
python tools/scan_core_plugins.py --enable rust_accel

# 构建与自测
cd core_plugins/rust_accel/rust_accel && cargo build --release && cargo test
cd ../.. && python it_smoke.py && python plugin_smoke.py && python bench.py 2000
```