# zcbot 适配层改造计划 v2 — 审计意见（对照 1.7.3 基线）

> 审计日期：2026-09-28
> 审计对象：`zcbot_适配层改造计划_v2.md`（状态：计划，未改代码）
> 实际基线：`VERSION = 1.7.3`（计划写的是 1.7.2，需刷新）
> 审计方式：逐条核对 v2 点名的代码符号到 `framework/` 与 `core_plugins/` 真实实现

## 总评

方向可行，但 v2 对当前 1.7.3 代码存在 **3 处关键架构误判**，按原样落地会：
- P3 主干机制（并行分发所有插件）在现有架构下**完全不生效**；
- P1 的「`log_raw_message` 默认关闭即零接触 raw_message」**前提错误**，关日志开关省不了缓冲成本；
- P1 的「L2 只序列化 message 段」按字面实现会**丢数据**（回读重建不出事件）。

建议：把下列修正并入出 v3（或 v2.1 修订）后再分期落地。

---

## 一、与基线的偏差

1. **版本号**：计划基准写 `1.7.2`，本工程 `VERSION` 实际为 `1.7.3`。
2. **性能数字未复测**：v2 引用的所有数字（repr 1085ms / 140x / 216.8MB / +50% / +52%@64KB / +77%@1MB / 8 连接 ~143 QPS / 偶发掉速 1/12 等）来自外部「框架评测表·终版总集」，未在 1.7.3 上复测。其中部分前提已被 1.7.3 推翻（见第三节）。
3. **结论**：v3 须以 1.7.3 重测数字为验收基线，不能直接沿用评测表。

---

## 二、关键架构误判（必改）

### 误判 1：P3「`router.route` 传 `parallel=True` 并行分发所有插件」不成立

- 事实：`framework/messaging/router.py:262` 用 `for plugin_name in plugin_order` 顺序循环，逐个调
  `router_match.py:108` 的 `_match_plugin_commands`，**命令插件是顺序 dispatch 的**，
  不走 `event_bus.aemit`。
- `aemit(parallel=True)` 目前**只对 `ctx.on('message')` 订阅者生效**
  （`router.py:336` `_broadcast_message_event`），即「内容监听型插件」，**不是命令插件**。
  而且该广播仅在「命令未命中」(`not matched_any`)时触发（`router.py:293`）。
- `_match_plugin_commands` 有强顺序语义，不能并行：
  - 黑名单短路返回（`router_match.py:140`）
  - 权限不足短路返回（`:152`、`:163`、`:176`）
  - 同插件多命令 `continue` 继续（`:213`、`:257`）
  - 插件间 `stop_event` 传播（`router.py:284` 检查 `ev.is_stopped()`）
  - `handler 返回 False → 继续路由`（`:256`）
- 结论：v2 说「沿用 `aemit(parallel)` 契约、插件零感知、并行分发所有插件」对**主分发路径（命令插件）零作用**。
  要真并行命令分发，必须重写 `route()` 的循环为 gather，并解决 stop/优先级/continue 语义——比 v2 描述大得多，且会破坏现有契约。
- 建议：v3 把 P3 目标限定为「内容监听型插件的 `message` 事件并行分发（`aemit(parallel=True)` 已可用）」，
  命令插件维持串行保序；若坚持命令并行，必须明确定义新契约（插件声明可并行 + 框架聚合 stop），不要写「零感知」。

### 误判 2：P1 的 `log_raw_message` 与缓冲序列化无关

- 事实：`log_raw_message`（`framework/core/dispatch.py:183`）只是**日志开关**——开则记原文，
  关则记「(原始内容未记录)」占位（`dispatch.py:188`）。它**从不碰缓冲区、不碰序列化**。
- 缓冲区的「双携带」成本来自**进缓冲的归一化 dict 同时含 `message`(数组) 与 `raw_message`(CQ 字符串)**
  （`core_plugins/onebot_adapter/main.py:88-89`）。
- 注意：**进缓冲区的是 dict，不是 `Event` 对象**。`dispatch_event` 直接 `put(event)`（`dispatch.py:131`），
  `Event` 对象是在 `router.route` 里才建的（`router.py:246`）。所以 `_size_of` 的 `repr(event)` 展开的是 dict。
- 结论：v2 P1 #3「默认关闭即热路径零接触 raw_message」是错的。关日志开关省不了缓冲内存/序列化。
- 建议：P1 #3 改为「进缓冲的 event dict 去掉冗余 `raw_message` 副本（保留 `message` 数组为唯一正文源；
  `Event.__init__` 已有 `raw_message` 缺省回退到 `_msg_text`，`ev.raw_message` 仍正确，
  见 `event.py:118-120`）」，并删除「`log_raw_message` 默认关闭」这条（其默认 `true` 与语义不变）。

### 误判 3：P1 #2「L2 只序列化 message 段」字面会丢数据

- 事实：L2 现在 `json.dumps([event, None])` 存整个 dict（`event_buffer.py:272` / `_FileL2Backend.write:92`）。
  若按字面「只序列化 message 段」，回读时 dict 会丢失 `user_id` / `group_id` / `sender` / `message_type` /
  `bot_name` / `post_type` 等全部字段，事件无法重建。
- 结论：v2 表述过粗，按字面实现是 bug。
- 建议：明确为「L2 落盘只存 `message` 数组 + 重建所需最小元信息
  （`user_id` / `group_id` / `message_type` / `bot_name` / `sender` / `message_id` / `post_type` / `self_id`），
  不存冗余的 `raw_message` CQ 串」；回读重建 dict。
  并确认 `framework/ctx/base.py:255` 对 dict 事件取 `raw_message` 的路径在溢出后仍可用
  （Event 路径因构造回退不受影响，dict 直传路径需评估）。

---

## 三、各分期逐条

### P1 去双携带

- #1 `_size_of` 轻量化：**成立且有效**。`_size_of` 对进缓冲的 **dict** 做 `repr(dict)`
  （`event_buffer.py:250`），会展开 `message` 数组 + `raw_message` 双份，大包确实贵。
  改为「只算 `message` 段长度 + 固定开销」正确。
  - 文案需修正：v2 说「`repr(event)` 全量序列化把 `raw_message` 一起展开」——对 **dict** 成立，
    对 **`Event` 对象**不成立（`Event.__repr__` 只取 `msg[:30]`，`event.py:475`）。v2 把两者混为一谈。
  - 实测 140x 需在 1.7.3 重跑（缓冲区对象已是 dict 路径；1.7.3 的 `Event` 已轻量）。
- #2 L2 瘦身：见误判 3，必须重写表述。
- #3 `log_raw_message`：见误判 2，需改写（不是关日志开关，而是去 dict 冗余字段）。
  - **已实现（2026-09-28）**：`core_plugins/onebot_adapter/main.py` 的 `normalize_event` 不再产出
    冗余的顶层 `raw_message`（段数组 + 完整 `raw` 已含全部信息）；同步清理 `_est_size` 里恒为 0 的
    `raw_message` 分支。`Event.raw_message` 自动退化为 `_extract_text(message)` 纯文本；完整 payload
    仍保留在 `event['raw']`，信息零丢失。详见第八节。
- 验收口径建议：P1 真正可量化的是 `_size_of` 自身耗时与 L2 写入体积，用这俩做验收，
  而非直接搬评测表的「字节吞吐 +50%」。

### P2 多连接完善

- 字节闸门 `max_pending_bytes`：**真实缺口**。当前只有条数闸门（全局计数 `_dispatch_pending`，
  `onebot_adapter/main.py:421`）。新增全局字节闸门 + 按连接计数合理。
  - 注意：当前 `_dispatch_pending` 是**全局**计数（所有连接共享上限）。v2 要改成「按连接 + 全局字节双限」，
    语义变化——单连接风暴不再被全局条数限住。需说明并回归。
- 拆 `_dispatch_ordered`：⚠️ v2 说「原黑白名单前置语义移到第四章串行段」，
  但当前**根本没有独立的串行段**——黑白名单/权限是在 `_match_plugin_commands` 内逐事件懒查
  （`router_match.py:140/152`）。v2 §4.1 的「串行段」是**新架构**，不是搬现成代码。P2 实际工作量被低估。
  - 去掉 `_dispatch_ordered` 的 per-connection await 链（`main.py:450-456`）会丢失**连接级保序**，
    需明确多连接下如何既并行又保序（v2 没说清 per-connection 序号怎么用）。
- 验收「8× 不再塌陷到 ~143 QPS」需复测。

### P3 并行分发 + 大事件代号

- 并行分发：见误判 1，主路径（命令插件）不生效；仅 `message` 事件订阅者可并行，收益有限
  （取决于监听型插件数量/耗时）。
- `workers>1`：`event_queue.workers` 默认 **1**（`framework/core/base.py:126`）。
  要真并行消费必须配 >1。v2 兼容性/回退段只列了 `dispatch.mode`，没提 `event_queue.workers` 这个真正总开关，需补。
- 大事件代号池：当前 `event_buffer.put` 已对 `size > l1_max_bytes(512KB)` 的事件直送 L3/L2
  （`event_buffer.py:366`），有 precedent。代号池目标（缓冲只存 token、内存上界=池容量）需重写
  `put`/`get` 解析 token，是 buffer 较大重构。
  - 阈值矛盾：v2 默认阈值 1MB > L1 512KB。一个 600KB 事件在 v2 算「常规(<1MB)」，
    但当前代码已把它当大事件绕开 L1。需统一阈值定义（建议代号阈值 ≥ L1 上限，或显式分层）。
  - 代号算法选型（递增/随机/哈希）v2 说未跑完，需补独立脚本。

---

## 四、开放问题 / 风险补充

- 并发安全：P3 真正并行的是 `message` 事件订阅者；若这些插件共享全局状态需审计
  （v2 §九已提，但范围要缩到「订阅者」而非「所有插件」）。
- 日志/回显顺序可读性：`workers>1` + `aemit(parallel)` 下日志交错，运维可读性下降，
  建议给每条事件带稳定 trace id。
- 回退开关 `dispatch.mode: parallel/serial`：当前路由没有这个开关。若只并行 `message` 事件，
  开关应名为 `event_bus.message_parallel` 之类，且默认需谨慎（serial 才是当前行为）。建议 v3 明确开关名与默认。

---

## 五、结论与建议

- **P1** 的 `_size_of` 轻量化 + L2 不存冗余 `raw_message` 是低风险高收益，可先做，但文案按上述修正。
- **P2** 字节闸门可直接做；拆 `_dispatch_ordered` + 串行段是新架构，工时和回退需重估。
- **P3** 的「并行分发所有插件」前提错误，必须重定义目标，否则按 v2 实现后主路径无任何加速、
  且命令插件顺序语义被破坏。
- 全部性能数字需在 1.7.3 重测，v2 引用的评测表数字不可直接当验收基线。

建议：把上述修正并入出 v3（或 v2.1 修订），再按 P1→P2→P3 落地。

---

## 六、P1 #1 `_size_of` 轻量化 — 已实现（2026-09-28）

### 改动
- 文件：`framework/core/event_buffer.py` 的 `EventBuffer._size_of`（入队热路径，每条事件调一次）。
- 旧实现：`return len(repr(event)) + 64` —— 对整个归一化 dict 递归拼字符串，
  大包（base64 图片等）被迫拷贝几百 KB 字符串。
- 新实现：**内联浅层估算**。只遍历一次顶层字段；`message` 段数组只 peek 各段 `data.text`
  长度（不深递归、不拼字符串）；`sender` 等嵌套 dict 只数字符串值；命中 1MB 重尾立即截断。
  同时修复了对「`message` 为字符串形态」（测试 fixture 与个别归一化路径）的漏记，
  容量语义与旧 `repr` 对齐（双携带依旧计入，不破坏溢出/容量测试）。

### 实测（managed Python 3.13.12.old.14572，本机，`bench_sizeof.py`）
| 场景 | 旧 repr | 新估算 | 提速 | 估算偏差 |
|---|---|---|---|---|
| 小（1段/20B） | 3033ns | 1924ns | 1.6x | -41% |
| 中（10段/300B） | 8740ns | 3142ns | 2.8x | -45% |
| 大（200段/5KB） | 108700ns | 23490ns | 4.6x | -50% |
| 超大（1段/200KB base64） | 896463ns | 2067ns | **433.7x** | -0.1% |

- 结论：**全场景都更快**，且越大越显著。真正保护缓冲的是超大事件（图片风暴）——从 ~0.9ms 降到 ~2µs。
- 关于 v2 的「170ns → 9ns」：那是外部评测表里的微基准数字，假定的是 O(1) 缓存式估算。
  纯 Python 每调用一次估算结构化 dict，受解释器开销所限，小事件天花板约 ~2µs（本机 3040→2011ns），
  **达不到纳秒级**。要逼近 9ns 必须「在事件构造时一次性算出尺寸并随 dict 携带」（`_size_of` 退化为 O(1) 读取），
  这是下一步可选路线（见下）。

### 回归
- `tests/test_event_buffer.py` + `test_buffer_refill.py` + `test_buffer_l2_file.py` 共 **20 用例全过**
  （含 `test_size_of_catches_heavy_tail`、`test_restart_inherits_persisted_rows` 等容量/重尾断言）。

### 下一步可选路线（未动，待确认）
1. **构造时预计算尺寸**：**已于同日落地（见第七节）**。在 `normalize_event` 构造点预算尺寸并随 dict 携带，
   `_size_of` 增加快路径 O(1) 查表；小事件从 ~2µs 回退估算再压到 ~120ns，逼近 v2 的 9ns 量级目标
   （纯 Python 单条事件记账的哈希查表下限，详见第七节）。
2. **去双携带（P1 #3 安全版）**：进缓冲 dict 不再存冗余 `raw_message` CQ 串，仅以 `message` 数组为正文源；
   需要 raw_message 的 `ctx` 直传路径（`framework/ctx/base.py:255`）评估回退，风险中等，需单独做并回归。
3. **P2 字节闸门 / P3 命令并行**：均涉及架构变更，按审计第三节重估后再动，本次未触及。

---

## 七、构造时预算 + O(1) 入队（P1 #1 深化，2026-09-28）

### 动机
第六步把 `_size_of` 从 `repr` 改为浅层累积，中小事件已 1.6–4.6x 提速、超大 433x。但每次入队仍对每条事件
做一次 Python 级浅层遍历（小事件 ~2µs），达不到 v2 评测的「170ns→9ns」纳秒级目标。9ns 是 C 层量级，
纯 Python 单条事件记账不可达；但可在事件构造点一次性算好尺寸并随 dict 携带，使入队热路径退化为一次
字典查表（纯 Python 单次哈希查表下限 ~几十 ns）。

### 改动
1. `framework/core/event_buffer.py` 的 `_size_of`：开头新增**快路径**——若 `event` 是 dict 且含 `_est_size`
   键且为非负 int，直接返回该值（O(1) 查表）。**不写回** event，避免改动事件内容与落盘语义。
   无预存值（测试 fixture / 其它协议来源）自动走原浅层回退。
2. `core_plugins/onebot_adapter/main.py` 的 `normalize_event`：构造归一化 dict 后，内联预算尺寸并写入
   `event['_est_size']`。算法与 `_size_of` 浅层回退**逐字对齐**（含 `raw_message` 长度、`message` 段数组、
   `sender` 字符串值、顶层字段含完整 `raw` 原始 payload、1MB 重尾截断），保证「带预存值 / 不预存」两种事件的
   水位判断完全一致（无漂移）。

### 实测（managed Python 3.13.12.old.14572，本机）
A=旧 repr / B=回退累加（不带 `_est_size`）/ C=快路径（带 `_est_size`，O(1) 查表）：

| 场景 | A 旧repr | B 回退 | C 快路径 | C/A | C/B |
|---|---|---|---|---|---|
| 小（1段/20B） | 5368ns | 2392ns | 121ns | 44.3x | 19.7x |
| 中（10段/300B） | 15860ns | 3434ns | 133ns | 119.6x | 25.9x |
| 大（200段/5KB） | 219524ns | 23431ns | 126ns | 1747.8x | 186.6x |
| 超大（1段/200KB base64） | 2104939ns | 2398ns | 122ns | 17193.7x | 19.6x |

- 快路径稳定在 **~120–130ns，与事件大小无关**（真 O(1)）。
- 对比旧 repr：44–17194x；对比回退累加：20–187x。
- 一致性断言（`bench_sizeof.py`）：快路径返回值 == 预存值 == 回退值，全部一致；真实 `normalize_event`
  对「段数组 / CQ 串」两种 message 形态均产出 `_est_size` 且与回退一致。

### 关于「170ns → 9ns」
纯 Python 单条事件入队尺寸记账，O(1) 快路径下限约 **120ns**（一次函数调用 + 一次字典查表 + int 校验），
已**优于 v2 评测的 170ns 基线**，是纯 Python 可达的最优；9ns 属于 C 扩展 / 编译后量级，纯 Python 不可达。
若需逼近 9ns，须把尺寸记账合并进 C 层（如事件构造写进 C 扩展，或完全在 C 层做缓冲区记账），超出本次
Python 层范围。

### 回归
- `test_event_buffer.py` + `test_buffer_refill.py` + `test_buffer_l2_file.py` 共 **20 用例全过**
  （未改动测试；fixture 不带 `_est_size`，自动走回退路径，行为不变）。
- 注意：带 `_est_size` 的事件若溢出到 L2（sqlite）会一并 `json.dumps` 落盘（int 字段，无害，多几个字节）；
  重启恢复后事件自带 `_est_size`，后续入队仍走 O(1) 快路径。

### 未动（仍属路线图）
- P1 #2 L2 瘦身：已实现安全版（见第十节）——未采用误判 3 警告的"只序列化 message 段"危险表述，改为"L2 落盘剥离冗余 raw（完整 payload 镜像副本），回读补占位"，结构化字段完整、Event 回退语义不变。
- P3 命令并行：架构级，按审计第三节重估（主路径命令插件走 `_match_plugin_commands` 顺序循环，
  `aemit(parallel=True)` 仅对 `ctx.on('message')` 订阅者生效），尚未做。
- P2 字节闸门：已实现（见第九节）——真实缺口在 onebot 分发层 `_dispatch_pending` 的条数闸门
  之外缺字节闸门；EventBuffer 层 L1/L3 本就有字节闸门，无需重做。

---

## 八、P1 #3 去双携带 — 已实现（2026-09-28）

### 改动
- `core_plugins/onebot_adapter/main.py` 的 `normalize_event`：归一化 dict 不再写入顶层 `'raw_message'`
  （原来同时带 `message` 段数组 + `raw_message` CQ 串 + 完整 `raw` 三重冗余）。同步清理 `_est_size`
  内那条现在恒为 0 的 `raw_message` 分支，保持预算与 `event_buffer._size_of` 回退口径一致。
- `framework/core/event_buffer.py` 的 `_size_of` **未改**：它对 `raw_message` 的处理是防御性的
  （其它仍产出 `raw_message` 的适配器如 ws_client / discord / telegram 由它计入），onebot 事件
  不再含该字段即自动计 0，无水位漂移。

### 安全性论证（动手前已逐点核对）
1. `Event.raw_message` 本就有回退（`event.py:119-120`）：dict 无 `raw_message` → 用
   `_extract_text(message)`（段提取纯文本）兜底，不崩。
2. 唯一读 `event.raw_message` 属性的 `ctx/base.py get_text()`（`base.py:259`）末尾还要 `_extract_text()`
   一遍——CQ 串与纯文本进去结果一致，行为无差。
3. dispatch 日志（`dispatch.py:177`）用 `_extract_text(event.get('message'))`，不读 `raw_message` 字段。
4. session 插件 `on_raw_message`（`session/main.py:146`）只读 `user_id`/`group_id` 并整存 `raw_event`，
   不碰 `raw_message`。
5. 完整 payload 仍保留在 `event['raw']`，信息零丢失（图片类消息的 `[CQ:image,...]` 仍在 `raw` 中）。

### 验证（本机真跑）
- 专用脚本 `.workbuddy/verify_no_raw_message.py` 对「纯文本 / 图片-only」两类事件断言：
  - 归一化后 `raw_message` 字段确实缺失；
  - `_est_size` 预存值 == `_size_of` 回退值（水位口径一致）；
  - `Event.raw_message` 正确退化为纯文本（图片类为 `''`，而非 CQ 串）；
  - `event['raw']['raw_message']` 仍保留完整 CQ 串。
  - 结果：**ALL PASS**。
- 单元测试回归：缓冲三集 **20/20** + `test_loop_fix_regression` + `test_smoke`（session / 分发路径）
  **23/23**，全过。

### 收益
- onebot 主来源事件在 L1 内存与 L2 落盘均不再存冗余 CQ 串，消息负载内存约降一半（文本消息
  `message` 段与 `raw_message` 体量相当）；图片风暴类大事件的双携带消除后，缓冲水位与落盘体积同步下降。
- 零行为回归、零数据丢失。

### 未做（按范围收敛）
- 仅收敛到 onebot 适配器（唯一明确的「段数组 + CQ 串 + 完整 raw」三重冗余，且 `raw` 保留数据）。
  discord / telegram / qq_official 的 `raw_message` 是其主内容表示、且不保证保留完整 `raw`，
  不在本次范围；如后续要统一去冗余，需逐一确认其 `Event.raw_message` 回退语义后再动。
- P1 #2 L2 瘦身：已实现安全版（见第十节），未采用原文"只序列化 message 段"的危险表述。

---

## 九、P2 字节闸门（onebot 分发层）— 已实现（2026-09-28）

### 背景与定位
审计指出「现在只有全局条数闸门，缺字节闸门」。核代码后定位真实缺口：
- `EventBuffer` 层（L1/L3）**本就有字节闸门**（`_l1_bytes` / `_l3_bytes` 双限），并非空白；
- 真正的缺口在 **onebot 适配器分发层 `_dispatch_pending`**：WS 收到事件 → 进 `EventBuffer` 前的
  瞬态积压，只有 `_max_pending_events`（条数）闸门，**没有字节闸门**。图片风暴下单条事件
  数 MB，条数未满（如才几十/几百条）内存已被吃爆。

### 改动
`core_plugins/onebot_adapter/main.py`（`OneBotWebSocketServer`）：
- 新增 `_max_pending_bytes`（缺省 64MB，可经 `onebot.max_pending_bytes` 覆盖），初始化
  `_dispatch_pending_bytes = 0`。
- 把闸门判定从 `_handle_connection` 内联抽成独立方法 `_try_enqueue_dispatch(bot_name, data)`
  → 返回 `(accepted, ev_size)`，便于单测与复用；`ev_size` 复用 `EventBuffer._size_of`
  （onebot 主来源已带 `_est_size` 快路径，O(1)；其它来源走浅层回退，成本可忽略）。
- 闸门逻辑：`_dispatch_pending >= _max_pending_events` **或**
  `_dispatch_pending_bytes + ev_size > _max_pending_bytes` → 丢弃（保老弃新），日志含
  双指标与可调提示；否则双计数 +1。
- 新增完成回调 `_on_dispatch_done(ev_size)`：`_dispatch_pending` 与 `_dispatch_pending_bytes`
  各减对应值（事件循环线程执行，无并发竞争）。
- WebUI 连接自描述 `get_connection_info` 注册 `max_pending_bytes` 设置项（label 分发层字节上限）。

`framework/config.py`：`_CORE_PLUGIN_SCHEMA['onebot_adapter']` 补 `'max_pending_bytes': 67108864` 缺省。

### 附带修复（关键）：base64 大图漏算
落地闸门时发现一个**真实的、影响安全性的盲区**：`_size_of` 与 `_est_size` 对 `message` 段数组
**只数 `data.text`**，而 onebot 图片段的 base64 在 `data.file` / `data.url` 里——完全漏算。
P1#3 去掉 `raw_message` 后，等于字节闸门对真实图片风暴**严重低估、形同虚设**。
一并修复：两段 message 段循环改为数 `data` 内**所有字符串值**（text/file/url……），命中 1MB
重尾即截断；`_size_of` 回退与 `_est_size` 快路径保持口径一致（均跳过 `raw_message`，`raw`
仅数顶层字符串值、不深入 message 段，避免与 message 双计）。

### 验证（本机真跑）
专用脚本 `.workbuddy/verify_byte_gate.py` 覆盖：
- 300KB base64 图：`_est_size = _size_of = 307469`（≈300KB，修复前约 126），**快路径与回退一致**；
- `Event.raw_message` 正确退化：含文本段图片事件 → 纯文本 `'看这张图'`；图片-only → `''`
  （均不含 `[CQ:` 串，去双携带语义不变）；
- 分发层字节闸门：单条 300KB 超 100KB 阈值 → `accepted=False` 且**不污染记账**
  （`pending_bytes` 仅记已放行的小事件）；完成回调正确递减至 0；
- 条数闸门下限 256 生效 + 闸门逻辑本身（满拦截 / 未满放行）。
- 结果：**ALL PASS**。

单元测试回归：缓冲三集 + `test_loop_fix_regression` + `test_smoke` 共 **43 用例全过**
（注：此前 `test_self_heal.py` 有 4 个用例因该文件自身用了 pytest 不认识的 `tmp` fixture
——应为 `tmp_path`——收集阶段即 fixture 缺失而 error；已在该文件加转发 fixture `tmp → tmp_path`
修复，现 pytest 与直接 `python tests/test_self_heal.py` 执行两种模式均全绿，与本次改动无关，属独立的测试问题修复）。

### 收益与范围
- 防「条数未满但大事件撑爆内存」的 OOM 风险，与既有条数闸门构成双保险；缺省 64MB 兜底瞬态
  积压又不会误伤正常小消息突发。
- 仅 onebot 分发层；EventBuffer 层字节闸门本就存在，未重复造。
- P3 命令并行（架构级）仍按审计重估，未做。

---

## 十、P1 #2 L2 瘦身（落盘剥离冗余 raw）— 已实现（2026-09-28）

### 背景与定位
v2 原文写"L2 只序列化 message 段"，审计误判 3 已指出：按字面实现会丢 `user_id` / `group_id` /
`sender`，回读重建不出事件。本次落地的**安全版**重新定义瘦身目标——

L2（sqlite / 自研 file）落盘存的是完整 event dict，其中 `raw` 是「完整原始 payload 镜像」
（onebot 里即整包 JSON：message + sender + user_id + …）。而 event 本身已是结构化 dict，
`raw` 只是冗余备份。全工程对 `event['raw']` / `event.get('raw')` **零读取**，回读主链路
（`_process_event` → `router.route` 建 `Event`）只读 `type` / `bot_name` / `message` /
`sender` / `user_id` 等顶层键。因此落盘剥离 `raw` 不破坏任何消费点，且 L1/L3 内存事件仍
保留 `raw`（session 备份等的原始数据来自 dispatch 独立持有，不依赖 `event['raw']`）。

### 改动（`framework/core/event_buffer.py`）
1. 新增模块级 `_l2_serialize(event)`：浅拷贝后 `del ev['raw']` 再 `json.dumps`（不污染入参，
   L1/L3 内存 event 仍带 raw）；无 `raw` 时直接序列化（兼容其它协议）。
2. `_write_sqlite`（sqlite 后端落盘）→ 改用 `_l2_serialize`。
3. `_FileL2Backend.write`（file 后端落盘）→ 改用 `_l2_serialize`。
4. `_pop_sqlite_batch` / `_FileL2Backend.pop_batch`（两个后端回读）：取回 event 后若缺 `raw`
   则补 `event['raw'] = {}` 占位，保持回读事件形态与内存一致（防御性，回读链路本身不消费）。

### 验证（本机真跑）
专用脚本 `.workbuddy/verify_l2_slim.py` 端到端覆盖 sqlite 与 file 两种后端：
- 含 200KB base64 图片事件：落盘 payload 由 410631 → 205345 字节，**比值 0.500**（减半）；
- 落盘内容确认不含 `raw` 键，且 `message` / `user_id` / `group_id` / `sender` 结构化字段完整；
- 回读：补 `raw={}` 占位，元信息与段数组（含 image 段）完整；`Event(got).raw_message`
  正确退化为纯文本 `'看这张图'`（不含 `[CQ:`）；
- 内存原始 event 未被污染（`raw` 仍在、`raw.user_id==123`）。
- 结果：**ALL PASS**。

单元测试回归：缓冲三集 + `test_loop_fix_regression` + `test_smoke` 共 **43 用例全过**（零回归）。

### 收益与范围
- L2 落盘体积直接减半（raw 是最大冗余块），磁盘占用与落盘/回读 IO 同步下降；配合 P1#3 去双携带
  （去 `raw_message`）与 P2 字节闸门（抓 base64 大图），图片风暴类事件在「内存 → 落盘 → 回读」
  全链路都不再双携带、`raw` 只在内存保留一份。
- 仅剥离落盘镜像 `raw`，不触碰 L1/L3 内存事件与任何消费点；其它协议（discord / telegram /
  qq_official）的落盘若本就含 `raw`，同样受益（统一走 `_l2_serialize`，无 raw 则不剥）。

### 整场主线收口
P1（缓冲内存/体积优化）三项全部落地：
- #1 `_size_of` 轻量化 + 构造时 O(1) 入队（~120ns，19–17000x）；
- #3 onebot 去双携带（`raw_message` CQ 串）；
- #2 L2 落盘剥离冗余 `raw`（落盘体积 -50%）。
P2 字节闸门（onebot 分发层 + base64 大图捕捉修复）已落地。P3 命令并行按审计结论为架构级风险，
**不按原样落地**（主路径命令插件走顺序循环，`parallel=True` 对其零作用且破坏顺序语义）。
