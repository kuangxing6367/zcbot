# 已知问题与修订记录

> 这份清单追踪 ZCBOT 当前**已知的坑**以及**修到哪一步了**。文档里写到的坑，都能在这里查到状态，避免你踩到还没修的雷。
> 详细调研过程见 [优化分析报告](./optimization-notes.md)（内部报告，含每个问题的源码定位）。

## 状态图例

| 标记 | 含义 |
| ---- | ---- |
| ✅ 已修复 | 已修完并验证，可以放心 |
| ⏳ 待修复 | 确认存在的坑，还没修，注意规避 |
| 🎯 刻意设计 | 不是 bug，是特意这么做的，别改 |

> 影响等级：**P0**=安全/正确性，建议尽快处理；**P1**=架构/正确性，重要但不紧急；**P2**=增强/健壮性，锦上添花。

---

## P0 —— 安全/正确性

| # | 状态 | 问题 | 位置 | 说明 |
| -- | ---- | ---- | ---- | ---- |
| S1 | ⏳ 待修复 | **X-Forwarded-For 直接信任** | apis.py:608-609 | 攻击者可伪造 `XFF: 受害者IP`：绕过全局黑名单/登录限速，甚至远程封禁任意 IP（DoS）。修法：只信任反向代理白名单，直连时用 `request.remote_addr` |
| S4 | ✅ 已修复 | 多 bot 连接注册表泄漏 | api.py:155-172 | 旧连接未注销导致无限增长、默认实例指向死连接。已改为：断线注销、默认连接优先已连接、重复连接复用旧 bot_name |
| S5 | ✅ 已修复 | 事件订阅重复（心跳后 handler 翻倍） | loader.py + event_bus.py | `event_bus.subscribe` 已按 (plugin_name, handler) 去重 |
| S6 | ✅ 已修复 | module.ctx._current_bot 竞态 | router.py:606-607 | 已改用 contextvars 注入（framework/api.py 新增 current_bot_var），多 bot 并发互不干扰 |
| S7 | ✅ 已修复 | **Event.role 全员降级为 member** | event.py:162 | 清理缓存分支缺少 `global _user_role_cache/_group_role_cache` 声明 → UnboundLocalError 被 except 吞掉。已补全 global 声明，super/blacklist/admin/owner/member/private 全部验证通过 |

## P1 —— 架构/正确性

| # | 状态 | 问题 | 位置 | 说明 |
| -- | ---- | ---- | ---- | ---- |
| A1 | ⏳ 待修复 | 同连接事件并发乱序处理 | websocket_handler.py:249 | create_task 无界 + 无顺序保证，多轮/状态机插件可能出错 |
| A2 | ⏳ 待修复 | "热加载"只重 register 不重 import | loader.py:1244-1275 | 改 handler 函数体代码不生效，需手动重载插件 |
| A3 | ⏳ 待修复 | Event.role 热路径同步查库 | event.py:132-197 | MySQL 池繁忙时阻塞事件循环（已修了 S7 的降级 bug，同步查库的性能问题仍在） |
| A4 | ✅ 已修复 | meta_event 心跳包直接丢弃 | core.py:525-526 | 已广播为 meta.heartbeat / meta.lifecycle，插件可 `ctx.on("meta.heartbeat", ...)` 订阅 |
| A5 | ⏳ 待修复 | 纯 @ 消息被丢弃 | router.py:464-469 | 无文本段的 @消息 收不到，@触发型插件受影响 |
| A6 | ⏳ 待修复 | Web 改 config.yaml 运行期不生效 | apis.py:2735 | 改配置必须重启框架 |
| A7 | ⏳ 待修复 | web_server.stop() 只置标志 | core.py:636 | waitress 线程不退出，优雅停机不完整 |
| A8 | ⏳ 待修复 | ctx.get_config 每次同步查库 | ctx.py:137-163 | 无缓存，async handler 里阻塞事件循环 |
| A9 | ⏳ 待修复 | ctx.run_async 的 Future 异常无人消费 | ctx.py:507-521 | 无背压、无取消 |
| A10 | ⏳ 待修复 | ctx.db_connection 在 SQLite 下返回共享连接 | ctx.py:443-461 | close 即真关闭，与文档"归还池"不符 |
| A11 | ⏳ 待修复 | event_bus 载荷类型不一致 | event_bus.py:47-73 | message 传 Event，notice/request 传 dict，插件要区分处理 |
| A12 | ⏳ 待修复 | at 段转 [@qq] 混入文本 | event.py:52 | `/echo @某人 你好` 命令匹配可能失效 |
| A13 | ⏳ 待修复 | 黑名单只降权不拦截 | router.py:287-360 | 命中黑名单仅降为 member，不是真正拦截 |
| A14 | ⏳ 待修复 | 插件依赖默认装全局环境 | loader.py:436-511 | 多插件版本互斥会冲突（隔离 venv 存在但非默认） |

## P2 —— 增强/健壮性

| 状态 | 问题 | 位置 |
| ---- | ---- | ---- |
| ⏳ 待修复 | SQLite 无 busy 重试；execute 无事务入口；pool_status 访问私有属性；_register_one 写放大 | db.py |
| ✅ 已修复 | 内存泄漏 5+2 处（角色缓存/任务引用/注册队列/nonce 缓存/ws 排队/冷却字典上限） | event.py / core.py / dual_auth.py / websocket_handler.py / api.py |
| ⏳ 待修复 | 时区硬编码 Asia/Shanghai；任务无超时；stop 丢执行中任务 | scheduler.py |
| ⏳ 待修复 | _should_log_sent_message 硬编码路径；15s 同步桥接超时占线程 | api.py |
| ⏳ 待修复 | WS 启动失败只记日志不退出 | websocket_handler.py |
| ⏳ 待修复 | 每请求全量查库校验 token 无缓存；ZIP 解压不拒符号链接；SSE token 走 query；.bak 无限堆积 | apis.py |
| ⏳ 待修复 | 无测试目录、无 CI、无 lint 配置 | 项目根 |
| ✅ 已修复 | send_like 封装（标准 38 API 全覆盖）；X-Self-ID 作 bot_name；meta 事件广播 | onebot_api.py / websocket_handler.py / core.py |

## 🎯 刻意设计（不是 bug）

| # | 项 | 说明 |
| -- | ---- | ---- |
| S2 | 默认口令 admin/admin123 | README 已说明，首次登录后应改密。可选改进：登录成功提示改密 |
| S3 | 破坏性路由仅 require_auth | admin 与 super 同权为刻意设计（普通管理员为保留角色） |

---

## 修订记录

| 日期 | 内容 |
| ---- | ---- |
| 2026-08-20 | 建表：从 optimization-notes.md 盘点整理公开清单 |
| 2026-08-20 | S7 已修复：Event.role 缓存 global 声明补全，全员降级 bug 修复并验证 |

> 想参与修复？框架源码在 `framework/`，改完记得更新本表状态。