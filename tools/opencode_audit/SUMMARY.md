# zcbot 16 轮 opencode 审计汇总

跑法：opencode 逐轮串行，每轮只读小摘录 + Grep 核对调用点，跑 ruff 两道门禁，写独立报告。
16/16 轮完成，无失败轮次。16 份报告在 `tools/opencode_audit/audit_round_*.md`。

## ruff 结果

16 轮每轮都跑了两道门禁，全部 `All checks passed!`。本轮审计期间未改产品代码。

## 主理人抽查（已实跑/核对）

| 编号 | 结论 | 说明 |
|---|---|---|
| D-1-3 | **确认，且是我上轮回归** | `def f(event, *, bot_name)` 会 TypeError，被 fail-closed 转成 False 丢事件。实跑 `[False]`、handler 未命中 |
| D-15-1 | **确认** | 可信代理下取 XFF 最左值且无 IP 格式校验，可伪造绕过 IP 风控 |
| D-13-1 | **确认** | `role: 123` → `.strip()` 在 try 外，直接 500 |
| D-13-2 | **确认** | `page=abc` → `int()` 在 try 外，直接 500 |
| D-3-1 | 部分成立 | 文本子串判只读有误判风险，但实际触发需错误信息里恰好含该词，优先级低 |
| D-1-2 | **设计权衡分歧** | opencode 认为异常转 False 与主动否决不可区分；这是 1.28.17 刻意选择的 fail-closed，待定夺 |

## 全部 44 条发现

- **D-1-1** 短路点上允许静默跳过 async handler，HTTP 守卫可 fail-open
  - 位置：`framework/hooks.py:147-150`
  - 影响：请求被无条件放行，且没有任何告警；这是典型的 Guard fail-open，不自愈（除非 loop 恢复且把守卫改成同步并真正返回结果），运维无法从日志发现守卫从未执行。
- **D-1-2** 短路点上 handler 异常被转成 False，与“主动否决”不可区分
  - 位置：`framework/hooks.py:337-343`
  - 影响：调用方把 False 当成明确的一票否决——`framework/core/dispatch.py:285-293` 会把事件丢弃并打“被扩展点拦截”warning；后续 handler 全部不再执行。消息凭空消失且日志归因错误（报错是 ERROR，看到的却是“拦截”）。HTTP 侧更糟：`http.before_request` 收到的 False 不会被 `framework/api/webapp.py:569-571` 当成 Response，请求继续走，守卫崩溃等于放行。
- **D-1-3** 参数适配把多余位置参数直接截断，keyword-only 参数必然报错并导致事件被丢
  - 位置：`framework/hooks.py:40-70`
  - 影响：该 handler 对每条消息都抛 `TypeError`，再叠加 D-1-2 被转成 False，事件被整条丢弃；日志只有“处理器异常/被拦截”，插件作者很难定位是参数适配造成的。属于稳定复现、不可自愈的功能静默失效。
- **D-1-4** `_SHORT_CIRCUIT` 声明 Response 短路，实现却只认 `is False`
  - 位置：`framework/hooks.py:138-144`
  - 影响：请求最终确实被 `framework/api/webapp.py:570-571` 拦下，但 registry 不会提前终止链——后面的 handler（统计、写库、发消息等副作用）全部照常执行完。守卫以为自己“短路”了，实际只是调用方顺带返回了第一个 Response；与模块文档和常量表承诺的语义不符，拒绝请求仍付出完整处理代价并可能产生不该有的副作用。
- **D-1-5** 混合 serial/parallel 时 `trigger_async` 破坏跨段位优先级保证
  - 位置：`framework/hooks.py:98-105`
  - 影响：LATE（收尾/观察）先于 GUARD 执行，看到的是尚未被拦截/改写的事件，且可能已经产生副作用；GUARD 即使随后返回 False，按模块文档第 29 行并行 handler 不参与短路，事件仍已被“收尾”处理。段位约定（EARLY→GUARD→NORMAL→LATE）在真实混合配置下不成立，顺序随 `serial` 声明漂移。
- **D-2-1** `database.path` 未做类型校验，显式 null 会让 `os.path.isabs` 裸崩
  - 位置：`framework/database/db_conn.py:54,59`
  - 影响：启动阶段 `os.path.isabs(None)` 直接抛 `TypeError: expected str, bytes or os.PathLike object, not NoneType`，初始化中断；异常栈指向 `db_conn.py:59` 而非配置文件，用户拿不到「database.path 写错了」这样的可操作提示，也没有 error 级日志。不会破坏全局共享状态，修正配置即可恢复（进程重启），但属于类型误用导致的
- **D-3-1** `_is_readonly_error` 用错误文本子串判定，把普通 SQL 失败误升级为全局“只读降级”状态
  - 位置：`framework/database/db.py:71-81`
  - 影响：`self._persist_degraded` 被置为 True，并打出“数据库进入只读状态（...）：本次运行的所有写入都不会落盘”的 **error**，把普通 SQL/数据错误伪装成存储层故障。该标记是实例级共享状态，且告警被有意压成“只打一次”（`db.py:318-321`），之后再发生真实只读也不会补报；只有下一次任意写成功才由 `_note_write_ok`（`db.py:331-334`）清除，若失败SQL反复执行则一直保持误报状态。补充核对：全仓 Grep `persist_degraded` 只命
- **D-3-2** `_emergency_recover` 退出失败时吞掉退出异常并返回 True，与自身注释的 fail-open 承诺相反
  - 位置：`framework/database/db.py:116-127`
  - 影响：函数只记一条 error 就 `return True`，调用方拿到的是“已按恢复流程处理”的信号；注释明确写着“不能吞掉原异常，调用方仍要看到最初的损坏”，实现却没有重新抛出原异常或退出异常。当前 4 个调用点在调用后都跟了裸 `raise`（`db.py:237/261/302/743`），所以**目前**损坏异常仍会外抛、进程仍会结束；但不会走 `perform_exit` 的 crash log 计数/安全模式路径（告警链断裂），且这个返回值契约会诱导后续任何按 `if _emergency_recover(...): return ...` 写的调用点静默吞掉损坏异常，继续带病运行。属于已埋好的 fail-open 陷阱，不是当前调用点上的直接数据损失。
- **D-4-1** 有来源上下文时适配器查询失败会 fail-open 串线到全局 OneBot/api_caller
  - 位置：`framework/ctx/base.py:88-113`
  - 影响：本应发往当前来源的动作被发到全局 OneBot 或“最后注册”的 `api_caller`（`protocol.py:346` 为覆盖式注册；Grep 显示 multiple 接入端均注册 `api_caller`）。消息/禁言/删消息发错端；调用方拿到的是“看起来正常”的代理对象，只有 DEBUG 日志、无告警，除非重建来源上下文否则不会自愈。
- **D-4-2** `ctx.onebot` 兼容别名并不绑定 OneBot，会随事件来源串到其它协议
  - 位置：`framework/ctx/base.py:115-122`
  - 影响：OneBot 专用动作被动态转发（`ActionProxy.__getattr__`，`protocol.py:316-321`）到其它适配器，通常得到 `{"status": "unsupported"}`（`protocol.py:290`）或 AttributeError；老插件静默失效/误发，无告警。
- **D-4-3** `_PluginLogger` 先 f-string 再传 `*args`，带百分号的消息触发 logging 格式化异常并丢日志
  - 位置：`framework/ctx/base.py:43-59`
  - 影响：`logging` 抛 `ValueError: unsupported format character ':'`，该条日志被丢弃，只在 stderr 打印 `--- Logging error ---`；同时 f-string 提前展开，失去延迟格式化，插件热路径有额外开销。
- **D-4-4** 模块级共享线程池无生命周期管理，4 个 worker 可被单个插件占满且拖住退出
  - 位置：`framework/ctx/base.py:23-27`
  - 影响：所有插件共用这 4 个线程，一个插件的慢任务会饿死其它插件的异步任务；任务异常只留在 Future 里，调用方不取就静默失败；ThreadPoolExecutor 的 atexit join 会让卡住的任务阻塞进程退出，无超时、无告警、无法在运行期回收。
- **D-4-5** `_PluginLogger` 自称“标准 logger 接口”但缺少常用方法
  - 位置：`framework/ctx/base.py:38-59`
  - 影响：直接 `AttributeError` 中断插件逻辑。Grep 当前仓库未发现 `ctx.logger.*` 调用点，因此影响面主要是外部/历史插件与文档承诺不一致。
- **D-05-1** `command()` 对 `handler.__name__` 的无条件访问会让可调用对象在注册期崩溃
  - 位置：`framework/ctx/events.py:46-47, 68, 73`
  - 影响：注册在构造 dict 字面量时即抛异常，`_commands.append(...)` 与 `_journal('command', ...)` 都没有执行——命令完全没被登记。若加载器吞掉异常，插件已是「加载成功但命令缺失」的不一致状态且无命令级告警；若不吞，则整个插件加载中断。值得注意的是第 47 行的报错分支自己就写了 `handler.__name__ if hasattr(handler, '__name__') else handler`，说明作者已意识到该属性可能不存在，但后续两处未沿用该防护。
- **D-05-2** `methods` 传入单个字符串时被拆成逐字符方法名，路由实际不可用
  - 位置：`framework/ctx/events.py:93-95`
  - 影响：本地/远程注册器收到的都是非法 HTTP 方法列表；真实 POST/GET 请求不会命中该路由（典型表现为 405/404），插件 API 看似注册成功却完全不可达。第 120/122 行的成功日志里会打出 `('P','O','S','T')`，但混在启动日志中很容易被忽略，属于静默失败；函数仍返回 True，调用方无法据返回值发现问题。
- **D-05-3** 远程路由注册的 `except TypeError` 全捕获兜底会掩盖真实错误并可能重复注册
  - 位置：`framework/ctx/events.py:100-109`
  - 影响：
- **D-6-1** `sender` 为显式 `None` 时消息分发崩溃，单条事件被丢弃
  - 位置：`framework/core/dispatch.py:532,548,553`
  - 影响：群消息在 `sender.get('group_name')` 处抛 `AttributeError`；私聊消息跳过该分支后在 `self.stats_writer.register_user(...)` 内部 `dict(sender)` 抛 `TypeError`（`framework/core/stats_writer.py:74`）。异常从 `_process_event` 抛出：worker 模式下被 `framework/core/dispatch.py:391-392` 捕获并记录 `事件处理异常`，worker 存活但该条消息不会路由、不会响应；同步回退模式（worker 未启动/停机收尾，`dispatch.py:296-298`）下异常直接向上冒泡。无针对该畸形输入的专门告警，表现为消息“丢失”。
- **D-6-2** 原始消息工作流解析异常时 fail-open，绕过插件过滤
  - 位置：`framework/core/dispatch.py:624-633`
  - 影响：代码记录 warning 后将 `_wf_plan` 保持为 `None`，随后 `if _wf_plan is not None and not _wf_plan.allows(...)` 不生效，所有原始消息处理器被“全部放行”。若工作流用于限制某些插件对特定用户/群的可见性或权限边界，该异常会造成策略绕过。系统有日志，但行为是静默放宽策略，属于 fail-open。
- **D-7-1** 取消后台任务时，已出队的整批统计数据被静默丢弃
  - 位置：`framework/core/stats_writer.py:96-106`
  - 影响：`_drain_to_fns()` 已把 `_cmd_hits`/`_kw_hits` 换成空 dict，并把注册项从 `_reg_queue` 取出；取消会连带取消 gather 中尚未完成的写库 future。随后 `stop()` 的 `_flush()` 只能处理"仍在内存中"的数据，无法恢复已交给 `fns` 的那一批：最多一个 flush 周期的命令/关键词命中与用户/群注册永久丢失。若部分闭包已完成，还会留下"部分写入"的不一致状态。无专门告警，不可自愈（源事件已被消费）。
- **D-7-2** 写库异常只记日志、不回滚数据，整批统计静默丢失
  - 位置：`framework/core/stats_writer.py:99-109`
  - 影响：这些 hits/items 已在 `_drain_to_fns()` 阶段从 `_cmd_hits`/`_kw_hits`/`_reg_queue` 移除，失败后既不回填也不重试；进程继续正常运行、后续周期照常 flush，丢失的数据没有持久标记或补偿。只有一行 `logger.error`，没有失败计数/告警，事后无法察觉命中计数与用户/群注册出现空洞，不可自愈。
- **D-7-3** adapter_key 未做入队前收敛，一条坏身份键可带崩整批注册
  - 位置：`framework/core/stats_writer.py:48-49,73-74`
  - 影响：`_aggregate_registrations` 用该值调用 `_namespace_id`（`stats_writer.py:143-154`）→ `namespace_identity` → `adapter_registry.uuid_for()`（`adapter_registry.py:83-86` 中 `self._cache.get(adapter_key)`），对 dict/list 抛 `TypeError: unhashable type`。异常发生在聚合阶段，使同一批中的所有正常用户/群注册一起失败（外层 `gather` 只记 ERROR，见 D-7-2），无专门告警、不可自愈。这与第 55-63 行注释声明的"坏身份键必须在入队前挡掉，不能带进批量写库"不一致：只挡了两个身份键，漏了第三个。
- **D-08-1** 加载失败/卸载后插件目录残留在 `sys.path`
  - 位置：`framework/loader/lifecycle.py:386-387`
  - 影响：失效或已卸载插件目录持续留在 `sys.path` 前端，可能导致后续导入解析到陈旧/不应加载的插件文件，形成跨插件导入污染或隐蔽的模块遮蔽。问题不会自愈，无专门告警；仅进程重启可清除。
- **D-08-2** ImportError 重试后的终态诊断使用过期依赖快照
  - 位置：`framework/loader/lifecycle.py:396,546-579`
  - 影响：终态错误提示可能显示已安装/已变化前的过期缺失依赖，误导排障人员执行错误的 pip 命令。不会自愈（仅影响该次日志），无额外告警。
- **D-9-1** 核心插件加载失败后 sys.modules 残留半初始化模块，全程无回滚
  - 位置：`framework/core/runtime.py:103-108`
  - 影响：全局 `sys.modules['core_plugin_<name>']` 长期指向只执行到一半的 module。其他插件按公开契约用 `sys.modules.get("core_plugin_html_assembler")` 取主模块（`core_plugins/html_assembler/README.md:59,98,126`），会静默拿到残缺对象；`importlib.import_module("core_plugin_<name>")` 同样直接命中而不再重新执行。自愈重试成功前（若不自愈则直到进程结束）跨插件调用会得到 AttributeError/半初始化状态，只有一条 error 日志、无告警；module 对象及其全局状态还被无谓持有。
- **D-9-2** `_register_journal` 只在加载后清空一次，运行期无界增长
  - 位置：`framework/core/runtime.py:127-142`
  - 影响：每个已加载插件的 ctx 持有随消息量/DB 操作数线性增长的列表（每项含最长 60 字符的 SQL 片段），长跑进程内存无界上涨、无阈值无告警；127-128 行注释所说"读完即清空，避免运行时调用混入"在加载之后并不成立。另外，清空放在 130-141 的 blackbox 记录之后且受 `if _journal:` 保护：若记录过程抛错（例如 `meta` 非 dict 时 138 行 `meta.get` 抛 AttributeError），142
- **D-10-1** `_compact()` 压缩失败后文件句柄保持关闭，L2 永久静默失效
  - 位置：`framework/core/event_buffer.py:105-110`
  - 影响：所有后续 L2 写入抛 `ValueError: I/O operation on closed file.`，被 `write()` 的 `except` 吞掉并返回 `False`；读侧同样在 `seek()` 处失败并被 `pop_batch` 的 `except` 吞掉返回 `[]`。进程运行期间 L2 不可自愈——没有任何告警或状态位暴露这个致命状态（`stats()` 只会看到写入失败后的 L3/L4 接管），表现为"日志里只有一条无害的压缩失败 warning，之后 L2 无声死亡"。若 `os.replace` 已成功但重开失败，磁盘文件已压缩而内存 `_read_offset` 未复位，还存在偏移错位的二次风险。
- **D-10-2** `_file_size()` 异常即返回 0，被消费端当成"已读完"而 `truncate(0)` 清空，静默丢事件
  - 位置：`framework/core/event_buffer.py:85-91`
  - 影响：直接执行 `self._fh.truncate(0)` + `seek(0)` + `_read_offset = 0`，把尚未消费的 L2 积压全部抹掉，`_rows` 之后才按已取条目递减，计数与实际内容脱节。整个过程只有一条 `logger.debug`，无告警、不可自愈，事件永久丢失且 `stats()` 不体现丢弃。属于典型的" innocuous 调用 fail-open"：一次失败的 `getsize` 就破坏了共享的持久化缓冲状态。
- **D-11-1** 缓存 TTL 承诺的“最多一个 TTL 生效”不成立，实际受下层 60s 缓存拖累
  - 位置：`framework/messaging/router.py:88-90`
  - 影响：权限变更最长约 65s（下层 60s + 本层 5s）才生效，而不是注释承诺的 5s；无告警，只能靠下层 TTL 自然过期自愈。对封禁/提权类操作属于安全窗口被放大。
- **D-11-2** 传入具体 user_id 的失效调用会清掉全局共享缓存
  - 位置：`framework/messaging/router.py:93-95`
  - 影响：一个用户改权限会顺带清空 `perm._perm_cache`（全体用户）和 `_group_role_cache`（全体群角色），破坏全局共享状态；所有在线用户下一次消息都要重新解析权限，形成 DB 查询风暴/延迟尖峰。可自愈但无告警，且 `user_id` 参数形同虚设。
- **D-12-1** `ProtocolAdapter.supports()` 吞掉全部异常并静默返回 False，能力查询故障被伪装成「不支持」
  - 位置：`framework/messaging/protocol.py:114-120`
  - 影响：能力查询被降级为 `False`，包括 `'text'` 也会被判为不支持；据此判断的插件会静默去掉图片/语音/按钮，甚至完全不发送消息，功能悄然缺失。只有一条 DEBUG 日志（默认日志级别下不可见），没有 WARN/告警；偶发错误下次调用可自愈，持续性故障会一直沉默，直到有人去看契约自检结果。与同一模块的契约自检行为不一致：`validate_adapter()` 对 `capabilities()` 抛异常会显式记入问题清单（`contract.py:394-399`：`issues.append(f'{name}.capabilities() 抛异常: {e}
- **D-13-1** `set_user_role` 对非字符串 role 直接 `.strip()`，且在 try 之外，必然抛未捕获 500
  - 位置：`framework/api/users_groups.py:124`
  - 影响：Flask 返回默认的 HTML 500（不是本模块约定的 `{'code':..., 'msg':...}` JSON），既没有 `logger.error` 也没有审计日志；接口契约和错误监控都被破坏。状态未被改动，可自愈但无告警。
- **D-13-2** 分页参数 `int()` 解析在 try 之外，畸形查询串直接打穿接口
  - 位置：`framework/api/users_groups.py:24-26`
  - 影响：任意用户（包括普通已登录用户）用一个查询参数即可让端点返回 HTML 500 而非 JSON 错误，日志里没有任何本模块的 `list_users 内部错误` 记录；配合前端分页/搜索框的异常输入很容易触发。
- **D-13-3** 管理端点用 `<int:user_id>`，与 `VARCHAR(64)`／命名空间身份不兼容，用户建得出来却改不了删不掉
  - 位置：`framework/api/users_groups.py:86`
  - 影响：多平台模式下，事件自动落库的用户全部无法通过 WebUI 改名、删除、设角色、拉黑；404 由路由层产生，没有错误日志，运维只能看到“点了没反应”；数据库中积累无法清理的僵尸用户。
- **D-13-4** 角色/黑名单写库后不失效共享权限缓存，撤权最长 60 秒不生效
  - 位置：`framework/api/users_groups.py:134`
  - 影响：这是安全相关的状态不一致——被撤销的超管权限在最长 60 秒内仍然有效，被拉黑用户仍可执行命令，删除后旧身份仍保留角色；无告警，只能靠 TTL 自愈。对比 `framework/perm/groups.py:186/197/219` 在改权限时都会调用 `invalidate_user`，这里明显漏了一环。
- **D-13-5** `DELETE` 只删 `users` 行，留下 `group_members` 孤立数据与群角色残留
  - 位置：`framework/api/users_groups.py:111`
  - 影响：用户被删除后，其群成员行与群级角色继续存在，权限判定在缓存有效期内仍按旧角色放行；用户下次发言时 `stats_writer` 会重建 `users` 行但库中仍残留历史成员记录。审计日志显示“用户已删除”，实际共享状态只删了一半，无告警。
- **D-14-1** 脱敏占位符只过滤顶层，嵌套凭据会被原样写回成 `***`
  - 位置：`framework/api/config.py:438,453~465`
  - 影响：嵌套凭据被持久化为字面量 `***`，对应实例/通道重启后认证失败或外连中断；无告警，只能等用户发现功能异常；需手工改 yaml 才能恢复。
- **D-14-2** 同一配置段的 PUT 是非原子 read-modify-write，且写助手整文件重写
  - 位置：`framework/api/config.py:463~465`
  - 影响：先提交的字段静默丢失，GET 回读不到；无冲突检测、无告警；进程重启后配置回退到旧值，属于典型 lost update。
- **D-14-3** 敏感键匹配过宽，非凭据的数值参数被打码后无法在面板修改
  - 位置：`framework/api/config.py:438,453~461`
  - 影响：这些普通数值参数在面板里看不到真值、也改不了；用户以为保存成功，实际未变更，且没有任何提示；与代码注释 `22: # token_timeout / secret_length 之类是普通数值，不能当凭据打码` 的意图相违背。
- **D-14-4** 插件段发现异常被 debug 级吞掉，GET 与 PUT 的可用段视图可能不一致
  - 位置：`framework/api/config.py:382~388,399~417`
  - 影响：同一时刻 GET 展示的段与 PUT 允许的段不一致——用户看到配置项却无法保存，或看到部分段；只留 `logger.debug`，无告警。与 `_yaml_sections` 文档 `38~45` 行「不放行这些段就改不了」的承诺正好相反。
- **D-15-1** 采信 X-Forwarded-For 最左值且不做 IP 校验，可绕过全部基于 IP 的风控
  - 位置：`framework/api/webapp.py:619-626`
  - 影响：`get_client_ip()` 的返回值被认证失败封禁（`_deny_auth` → `_record_auth_failure`）、`dual_auth.handle_request()`、`_detect_rapid_reload()`、登录失败/限流、`audit_log()` 共用。伪造 `127.0.0.1` 可命中 `dual_auth.whitelist_ips` 默认值，`is_blacklisted()` 与 `handle_request()` 均对白名单 IP 直接放行，软封禁/nonce 校验被绕过；轮换伪造 IP 可逃避连续认证失败阈值、登录速率与刷新阈值；伪造字符串还会写入 `_auth_failures`/`_refresh_windows`、审计日志乃至 `ip_blacklist` 表（
- **D-15-2** `http.after_request` 扩展点的返回值被静默丢弃，插件无法改写响应
  - 位置：`framework/api/webapp.py:576-586`
  - 影响：`trigger_sync()` 的返回列表被直接丢弃，函数固定 `return resp`，插件的改写永不生效且无任何告警；钩子内部异常也仅 `logger.warning` 后继续返回原响应，形成静默失败。不会自愈，只有改代码或改插件约定才能恢复。
- **D-15-3** 两个 IP 状态字典只有"按最近时间清理"的条件，轮换 IP 可无限增长
  - 位置：`framework/api/webapp.py:499-503`
  - 影响：所有新键的 `v[-1]` 都是刚写入的时间戳，清理时没有一个键满足 stale 条件，两个字典随请求数无上限增长；`_refresh_windows` 每个键还携带时间戳列表。属于内存泄漏/内存型 DoS：无告警，进程 OOM 才暴露；只有流量停止且后续仍有清理触发时才可能自愈。
- **D-16-1** 单个 plugin.yaml 结构异常会让整个 /api/plugins 列表接口返回 500
  - 位置：`framework/api/plugins.py:83-87`
  - 影响：只要有一个插件的配置写法不合规，插件管理页的整个列表接口就不可用（不是只影响该插件），其他插件也无法展示；只能靠人工修正该 YAML 才能恢复，无专门告警，仅有顶层 `logger.error('获取插件列表失败: ...')`。同文件 `:422` 对同一类返回值已做 `isinstance(yaml_data, dict)` 保护，说明此处缺少校验是不一致的防御缺口。
- **D-16-2** 命令/任务计数查询失败被吞成 0，前端无法区分“真的为 0”和“查询失败”
  - 位置：`framework/api/plugins.py:100-112`
  - 影响：UI 显示“0 条命令 / 0 个任务”，与插件真实拥有命令/任务时不可区分，属于静默的数据不一致；异常仅写入 `logger.debug`，生产日志级别下不可见、无告警，接口仍返回 `code: 0`，不会自愈（只能等 DB/表结构恢复后下一次请求刷新）。按插件逐个追加两次查询也放大了故障期间的失败面。

## 待主理人决定

44 条里已抽查 6 条（4 真 + 1 部分 + 1 设计分歧），其余 38 条尚未逐条复核。
opencode 用 Grep 交叉核对 + 实跑验证，报告质量较高，但自由模型存在夸大倾向，修之前需逐条验证。
## 主理人逐条核实结论（2026-11-27）

44 条全部读报告正文 + 核对源码，结论：**41 条确认为真缺陷并修复**，3 条暂缓：

| 暂缓项 | 理由 |
|---|---|
| D-1-5 混合 serial/parallel 破坏段位优先级 | 需重构 trigger_async 执行模型，改动面大，单独排期 |
| D-6-2 工作流解析失败 fail-open | 涉及安全策略取舍（fail-open vs fail-closed），已有 warning，需产品决策 |
| D-11-1 权限 TTL 承诺与下层 60s 缓存不符 | 改权限缓存语义影响面广，且 D-13-4 已补主动失效，风险已缓解 |

已修 41 条见 CHANGELOG 1.28.18。
