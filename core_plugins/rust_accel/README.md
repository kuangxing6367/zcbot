# rust_accel —— OneBot 反向 WS 链路的 Rust 加速层

把 OneBot 反向 WebSocket 的**接收（事件）与广播（动作）**两条热路径从 Python
搬进 Rust 原生实现，Python 侧只保留进程管理、IPC 桥接与监控观测。事件从
WS 帧到 JSON 解析、归一化、内存分派全部在 Rust 完成，动作发送走无阻塞
队列，避免 Python 事件循环在指令密集路径上的解释器开销。

## 平台兼容

| 层面 | 兼容性 |
| --- | --- |
| Rust 源码 | 跨平台（Windows / macOS / Linux），依赖全部为纯 Rust crate（tokio / tokio-tungstenite / serde），无系统库依赖，`cargo build` 即得 |
| 交付二进制 | 按目标平台构建，互不通用（Windows 产物为 rust_accel.exe，Linux/macOS 产物为 rust_accel） |
| Python 插件壳 | 跨平台，二进制定位自动适配后缀（`binary_path` / `RUST_ACCEL_BIN` 可显式指定任意平台路径） |

框架侧兼容 Python 3.7+/3.13（与框架主版本一致），Rust 侧 MSRV 无特殊要求
（使用当前 stable 即可，1.70+ 稳妥）。

## 构建

```bash
cd core_plugins/rust_accel/rust_accel
cargo build --release        # 产物: target/release/rust_accel(.exe)
```

插件启动时按以下顺序定位二进制：

1. `core_plugins.yaml → rust_accel.binary_path`（显式路径）
2. 环境变量 `RUST_ACCEL_BIN`
3. `core_plugins/rust_accel/rust_accel/target/{release,debug}/rust_accel(.exe)`

配置 `auto_build: true` 时，若上述路径都找不到会尝试用 `cargo build --release`
自动构建（需 PATH 中有 cargo）。

## 启用与配置

core_plugins.yaml 是唯一权威：

```bash
python tools/scan_core_plugins.py --enable rust_accel
```

配置项（均在 core_plugins.yaml → rust_accel 段）：

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| ws_host / ws_port | 0.0.0.0 / 6831 | 反向 WS 监听，缺省独立端口避开 onebot 的 6830 |
| access_token | '' | OneBot 鉴权 token，空 = 不校验 |
| max_frame_size | 16777216 | WS 单帧上限（字节） |
| max_pending_events | 4096 | 分发闸门条数上限 |
| max_pending_bytes | 67108864 | 分发闸门字节上限（base64 大图防护） |
| stats_interval_secs | 5 | 监控推送周期（秒，0 = 关闭） |
| ipc_strip_raw | false | IPC 事件是否剥离 raw 字段 |
| inherit_onebot | false | true 时缺省项继承 onebot 段（listen_host/port/token） |
| binary_path | '' | 二进制显式路径 |
| auto_build | false | 找不到二进制时自动 cargo build --release |

## 与 onebot_adapter 的关系

两者都是反向 WS 接入，同时启用会**重复注入事件并竞争端口**。启用 rust_accel
前请先停用 onebot_adapter：

```bash
python tools/scan_core_plugins.py --disable onebot_adapter
python tools/scan_core_plugins.py --enable rust_accel
```

插件启动时若检测到 onebot_adapter 仍在启用，会输出告警（不擅自改动配置）。

## 运行时接口（services['rust_accel']）

| 方法 | 说明 |
| --- | --- |
| `async acall(action, bot=None, **params)` | 广播动作，返回 call_resp（elapsed_ms 已含） |
| `call(action, bot=None, **params)` | 同步版（桥接事件循环） |
| `get_connected_bots()` | 当前已连接的 OneBot 客户端名列表 |
| `get_stats()` | 最近一次监控快照 |
| `status` | 进程/IPC/连接/重连次数/uptime |

## 监控观测

Rust 侧按 stats_interval_secs 推送 stats 行，Python 壳落日志并保存快照：

- `conn.now / peak / reject` —— 连接状态
- `api.ok / fail / timeout / qfull / rt_avg_ms / rt_max_ms` —— 广播延迟与成功率
- `fwd.events / fwd.bytes / fwd.ipc_fail` —— 接收吞吐
- `drop.gate / drop.norm` —— 分发闸门 / 归一化丢包计数

## 性能基准（N=2000，Windows / release 二进制）

### Python 基线（tools/bench_*.py，框架内部、无网络）

| 场景 | 吞吐 | 单事件 |
| --- | --- | --- |
| pure_await（直 await 空 handler） | 2,387,774 ev/s | 0.4 us |
| event_bus 空订阅 | 224,190 ev/s | 4.5 us |
| event_bus 10 订阅 | 118,535 ev/s | 8.4 us |
| fw_end2end（1 worker，真实分发链） | 6,951 ev/s | 143.9 us |
| 稳态吞吐 workers=1/2/4 | 86,852 / 91,415 / 89,046 ev/s | 11.5 / 10.9 / 11.2 us |

> 以上为框架内部纯 Python 路径，不含 OneBot WS 收发包、JSON 解析与连接管理。

### rust_accel 端到端（python core_plugins/rust_accel/bench.py，含真实 WS + IPC 全链路）

| 场景 | 指标 | 数值 |
| --- | --- | --- |
| A 事件注入端到端（WS→Rust 解析→IPC→Python dispatch 落点） | 吞吐 / 单事件 | ~30K ev/s / 33.6 us |
| B 广播并发 acall（含 echo 回执） | 吞吐 / 成功率 | 8,894 qps / 2000 成功 |
| C 广播串行延迟（Python acall→Rust 写 WS→回执） | P50 / P95 / 均值 | 136 / 272 / 152 us |

### 结论口径（如实对照）

- A 场景**已包含** WS 收包、Rust JSON 解析、IPC、Python 分发落点全链路，
  单事件 33.6 us，低于 Python 框架内部单 worker 端到端 143.9 us——即
  「Rust 收 + Python 分发」的整条链路快于纯 Python 的「分发」本身，
  解析与收包的 Python 开销已被完全移出事件循环。
- B/C 场景证明广播路径不阻塞事件循环：acall 为纯异步入队 + 回执等待，
  并发 8.9K qps、串行 P95 272 us，Python 侧无解释器热路径开销。
- 注意 A 的基数（~30K ev/s）受 Python 侧 `_ipc_loop.readline` + 每事件
  `create_task` 落点限制，非 Rust 侧瓶颈；如需更高吞吐可加大
  `max_pending_events` 闸门并批量消费。

## 开发与测试

```bash
cd core_plugins/rust_accel/rust_accel
cargo test                                    # 单测（归一化/估算/解析）
cd ../..
python it_smoke.py                            # Rust 层端到端（IPC 双向 + WS 收发）
python plugin_smoke.py                        # 插件封装（生命周期 + 事件注入 + 广播回执 + 监控）
```