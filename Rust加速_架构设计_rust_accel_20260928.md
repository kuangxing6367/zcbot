# Rust 加速层架构设计（rust_accel，接收端 + 广播端）

日期：2026-09-28
状态：已实施并回归通过（2026-09-28 交付；Todo 1–4 按本设计落地，验收基准实测见 §10）

## 1. 目标与边界

以**官方插件**（core_plugins/rust_accel，Python 壳 + Rust 子进程）形式，把 OneBot 11 接入链路的两段移到 Rust：

- **接收端**：反向 WS 监听 → 收帧 → JSON 解析 → 体积预算 → 归一化（normalize_event 移植）→ IPC 送入 Python `dispatch_event`；
- **广播端**：Python 插件动作调用（ctx.actions / onebot_api）→ IPC 转发 → Rust 序列化 → WS 发送 → echo 响应匹配 → IPC 回传结果。

**不替换 Python 本体**：dispatch/router/event_bus/hooks/DB/插件业务全部留在 Python，Rust 只做「与 OneBot 客户端之间的收发」。用户实测 Rust 图片渲染对比 PIL 快 2.4x（且仍有字体缓存等优化空间），证明 Rust 移植同类指令密集路径收益显著；本架构的 IPC + 监控模式后续同样可复用到 image_renderer 等插件（见 §8）。

## 2. 现状链路与 Rust 介入点（源码已精读）

### 2.1 接收端现状（core_plugins/onebot_adapter/main.py）

```
websockets.serve (_serve, 6830)
  └ _handle_connection: async for raw_message in ws
      ├ json.loads(raw_message)                    ← Python JSON 解析
      ├ "echo" in data → conn.on_response          ← 广播端应答匹配
      ├ post_type → _try_enqueue_dispatch          ← 条数+字节闸门（_size_of 因带 _est_size 为 O(1)）
      │     → asyncio.create_task(_dispatch_ordered(prev, ...))   ← 每事件一个 task 的保序链
      │         → _dispatch（Semaphore(64)）
      │             → _on_raw_event → normalize_event（60+ 行纯 Python：dict 构造 + _est_size 预算）
      │                 → framework.dispatch_event
```

### 2.2 广播端现状

```
ctx.actions.send_group_msg(...) / ctx.onebot.xxx
  → ServiceRegistry['api_caller'] = ApiCaller
      → BotConnection.acall: echo=uuid4 → payload{action,params,echo}
          → ws_server.asend（json.dumps + ws.send）   ← Python JSON 序列化 + WS 写
          → asyncio.Event().wait() 10s               ← 等 echo
          → on_response: _responses[echo]=data; event.set()
  BotConnection.call（同步桥）: run_coroutine_threadsafe + future.result(timeout=15)
  ApiCaller.broadcast: 对每个连接逐个同步 call —— 多 bot 时一个超时即串行拖慢
```

### 2.3 消费侧契约（Rust 产物必须无缝接入）

- `EventBuffer._size_of` 对带 `_est_size: int >= 0` 的事件走 **O(1) 快路径**（event_buffer.py:284）——归一化尺寸预算必须由 Rust 完成；
- `dispatch_event` 消费的字段集与 `normalize_event` 输出完全一致（type/sub_type/message_type/user_id/group_id/message_id/message/sender/bot_name/adapter/raw/_est_size）；
- `ProtocolAdapter` 抽象是服务契约（protocol.py），Rust 插件壳实现等价服务面即可让全部既有调用方无感。

## 3. IPC 方案定案：本地 TCP + JSON Lines（双向）

**选型结论：Rust 子进程内置 OneBot WS 服务端 + 独立 IPC 服务端（127.0.0.1:0 随机端口），Python 插件以 `asyncio.open_connection` 原生读写。** 理由：

| 候选 | 结论 | 依据 |
|---|---|---|
| stdin/stdout 管道 | 否决 | Windows asyncio 对子进程管道无 `add_reader` 原生支持，需 `to_thread` + `call_soon_threadsafe`，每事件 1–2us 级线程切换且批量积压要额外聚合；stdout 还要与日志分流 |
| 本地 TCP (127.0.0.1) | **采用** | `asyncio` 对 socket 全原生：`StreamReader` 异步逐行读、`drain()` 背压语义清晰、零额外线程；绑定 `:0` 由 OS 分配端口零冲突 |
| Windows named pipe | 备选 | tokio 支持良好但实现复杂度高，收益与 TCP 回环无差异（同为本地内核对象），不构成第一版理由 |

### 3.1 启动握手

1. Python `register()` 读配置（rust_accel 段 + 复用 onebot 段）→ `subprocess.Popen(rust_accel.exe, stdin/stdout=管道, 环境变量传配置)`；
2. Rust 绑定 IPC `127.0.0.1:0` + WS `host:port`，stdout 首行输出 `HELLO`（见协议），随后 stderr 只出日志；
3. Python 阻塞读首行 → `asyncio.open_connection(127.0.0.1, ipc_port)` → 双向 JSON Lines；
4. 握手失败/超时（默认 10s）：按 §6 生命周期处理。

### 3.2 消息协议（每行一个 JSON 对象，UTF-8，`\n` 分隔）

**rust → python**

| type | 字段 | 说明 |
|---|---|---|
| `hello` | `ipc_port`, `ws_ready`, `ws_port`, `version` | 首行握手，Python 据此建连 |
| `event` | `event`（归一化内部事件，含 `_est_size` 与 `raw`） | 与 2.3 契约完全一致 |
| `call_resp` | `id`, `ok`, `result`, `elapsed_ms` | 动作应答，`id` 匹配 py→rust 的 `call` |
| `conn` | `state: 'up'|'down'`, `name` | bot 连接状态变化（监控/多 bot 管理） |
| `stats` | 见 §7 | 周期监控上报（默认 5s，0=off） |
| `alert` | `code`, `msg` | 降级/重启/异常告警 |

**python → rust**

| type | 字段 | 说明 |
|---|---|---|
| `call` | `id`, `action`, `bot?`, `params` | 动作发送请求（广播端入口） |
| `query` | `kind: 'connections'|'stats'` | 按需查询（WebUI/命令） |
| `shutdown` | — | 优雅停机（排空动作队列后退出） |

行长度上限 32MB（超限断开并告警）；Python 侧 `StreamReader.readline()` 天然背压：rust 转发队列满时暂停 WS 读循环（闸门语义前移）。

## 4. 接收端（Rust）设计

- **栈**：tokio + tokio-tungstenite + serde_json。
- **WS 服务端**：监听 `core_plugins.yaml → onebot` 段（listen_host/listen_port/access_token/max_frame_size，缺省与现插件一致：0.0.0.0:6830/16MB）；`process_request` 阶段完成 access_token 校验（Authorization: Bearer / query access_token），`X-Self-ID` 头决定 bot_name，缺省 `bot_N` 递增。
- **连接循环**（每连接一 task）：
  1. tungstenite 解帧 → 文本；
  2. `serde_json::from_str`（失败记 `parse_err` 计数并跳过）；
  3. 含 `echo` → 广播端应答匹配（§5）；
  4. 含 `post_type` → **体积预算（移植 `_est_size` 口径：message 段 data 字符串值求和、1MB 重尾截断）** → 闸门（max_pending_events / max_pending_bytes，超限记 dropped 并丢弃）→ 归一化（Rust 移植 normalize_event，message 段数组原样透传，adapter='onebot'）→ **每 bot 一个 mpsc 队列 + 独立转发 task**（保持"每 bot 有序、多 bot 并行"，语义与现 create_task 保序链一致）→ IPC 写出。
  5. 无 post_type 非 echo 帧 → 计数忽略。
- 转发并发上限：IPC 背压即天然限流；上报 `pending` 水位与丢弃数。

## 5. 广播端（Rust）设计

- **Python 侧（桥，插件代码零改动）**：
  - 实现 `RustAccelApiCaller`（等价 ApiCaller 服务面），`acall(action, bot, **params)`：`id` 自增 → IPC 写 `call` 行 → 注册 `{id: asyncio.Future}` → `await` 超时 15s；`call()` 同步桥复用 `ProtocolAdapter.call` 既有模式（run_coroutine_threadsafe + result 超时），**每条调用独立 future，只阻塞调用线程，不复刻 http_inject 单线程串行卡死问题**。
  - `broadcast`：并行 acall 所有 bot（不再逐个同步阻塞）。
  - `send_text` / `_on_message_sent`（after_message_sent 事件）行为与现 onebot_adapter 完全一致，由插件壳保留。
- **Rust 侧**：
  - 每 bot 一个写侧任务：mpsc 发送队列（上限 1024 条，满即即时失败 + 计数）+ Sink 串行写出；
  - `call` 处理：选连接（未指定 bot 时第一个 connected，与现语义一致）→ serde_json 序列化（`ensure_ascii=False` ⇔ 直接 UTF-8 输出）→ payload 带 `echo=uuid`（Rust 生成）→ 入队 → pending map `{echo → (id, oneshot, deadline)}`；
  - WS 帧 echo 匹配 → stdout 回 `call_resp`（成功/失败状态透传现有 retcode 语义）；10s 超时（`tokio::time::timeout`）回超时结果——与现有 BotConnection.acall 语义一致；
  - `_SENT_ACTIONS` 日志钩子由 Python 收到 call_resp 后执行（消息内容可在 IPC 响应中回带 params 以便记录）。

## 6. 生命周期与降级（Todo 3 细化）

- register：rust_accel.enabled=false 默认；启用时**自动跳过 onebot_adapter 注册**（双实现互斥，避免 6830 端口冲突）；注册 services：protocol_adapter / api_caller / onebot_api / rust_accel_status；
- 崩溃自愈：IPC 断开 → 重建并在 1 分钟内最多重启 3 次 → 仍失败：`alert` 降级（可配置 `fallback_to_python: true` 时启动原 Python onebot_adapter 平滑接管；默认 false 保持 down 并展示状态）；
- 停机：framework stop → `shutdown` → 等退出（排空动作队列，10s）→ 超时强杀；rust 启动失败不影响框架自身启动；
- 配置项（core_plugins.yaml → rust_accel 段）：enabled / ipc_host / stats_interval / restart_max / fallback_to_python / binary_path（缺省同目录 rust_accel.exe）。

## 7. 监控观测（上行，符合"监管框架实践方法"）

- Rust 侧 tokio Instant 计延迟（各自进程内计，不跨时钟）：
  - **连接**：当前/峰值 bot 数、up/down 事件流；
  - **收**：WS 收帧总数、parse_err、闸门丢弃（条/字节）、归一化丢弃、IPC 发送失败；
  - **转**：收帧→IPC 写出的平均/P95/P99 延迟（采样窗口与直方图在 stats.rs 内实现）；
  - **发**：call 总数、成功/失败/超时/无连接/队列满，广播条数；发送→echo 往返延迟分位；
  - **进程**：版本、运行时长、CPU/内存（可选）。
- `stats` 消息默认 5s 一条；Python 聚合进 `RustAccelStatus`（暴露 stats()/status()），接 WebUI 连接页（复用 get_connection_info 的 status_extra 形态）与框架日志/统计系统。

## 8. 扩展方向（不含在本 Todo 范围）

Rust 实测比 PIL 快 2.4x 且仍有字体缓存等优化空间 → 本架构的「Rust 子进程 + IPC 契约 + 监控上行的三段式」可整体复用于 image_renderer 等 CPU 密集官方插件：同一 IPC 通道扩展消息类型即可（如 `render` 请求/响应），Python 侧只做参数组装与结果消费。第一版协议预留 `type` 分派结构即为该扩展留口。

## 9. 工程结构（草案）

```
core_plugins/rust_accel/
  main.py              # Python 壳：配置/进程管理/IPC 读循环/RustAccelApiCaller/统计聚合/服务注册
  rust_accel/          # Rust 工程（cargo）
    Cargo.toml
    src/main.rs        # 启动参数/HELLO/信号/shutdown
    src/ws_server.rs   # OneBot 反向 WS 服务端（鉴权/收帧/连接管理）
    src/normalize.rs   # normalize_event + _est_size 口径移植（golden 对拍用例）
    src/api.rs         # 广播端：每 bot 写队列/echo pending/超时
    src/ipc.rs         # IPC 服务端 + JSON Lines 协议编解码
    src/stats.rs       # 计数器 + 分位延迟 + 周期上报
  README.md
```

**构建与发布**：`cargo build --release`。当前默认 toolchain 为 `x86_64-pc-windows-gnu`（rustc/cargo 1.98.1 已确认可用）；发布产物须与部署机一致并锁定单一 toolchain（gnu/msvc 二选一），避免混用导致目标产物不兼容。dev 运行配置 `binary_path` 指向 `target/release/rust_accel.exe`。

## 10. 验收基准（Todo 4 沿用既有口径）

- 对照基线：纯转发稳态 / wait 模式 / 直 await（tools/bench_*.py 既有脚本）；
- 新增：rust_accel 端到端（WS 注入 N 条 → Rust → IPC → Python dispatch 落点延迟；广播端 acall 往返），与 Python onebot_adapter 基线同场景对照；
- 回归：`pytest tests/ -q --ignore=tests/test_perm.py --ignore=tests/test_plugin_imports.py -o asyncio_mode=auto` → **135 passed**；`python tests/test_perm.py` 43/43；`python tests/test_plugin_imports.py` 25/25。
- **实测（2026-09-28，Windows / release 二进制，N=2000）**：A 事件注入端到端（WS→Rust 解析→IPC→Python 分发落点）~30K ev/s / 33.6us；B 广播并发 acall 8,894 qps（2000/2000 成功）；C 广播串行 P50 136us / P95 272us / 均值 152us；对照基线 fw_end2end 6,951 ev/s（143.9us）、稳态吞吐 workers=1/2/4 ≈87–91K ev/s。另 `cargo test` 单测全过、`it_smoke.py` / `plugin_smoke.py`（优雅停机 0 残留进程）PASS、`bench.py` 快速档 PASS。

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| normalize 行为 Python↔Rust 不一致 | §9 中 3–5 条 golden 用例（文本段/图片段/notice/meta/无 post_type）双向对拍单测 |
| 事件 raw 字段使 IPC 带宽翻倍 | 默认携带保一致；配置 `ipc_strip_raw=true` 时省略，Python 补空 dict（L2 落盘本就剥 raw） |
| 双实现互斥不清导致 6830 端口冲突 | rust_accel 注册时抢占 onebot 配置段并跳过 onebot_adapter 注册，注册表互斥断言 |
| 16MB 大帧/32MB IPC 行 | tungstenite max_message_size 对齐 max_frame_size；IPC 行上限兜底断开 + 告警 |
| gnu/msvc 产物兼容 | 发布锁定单一 toolchain，CI/构建脚本内固定 `--target` |
| Rust 进程挂死 | IPC 心跳超时检测（>3×stats_interval 无消息判 dead）→ 重启流程 |