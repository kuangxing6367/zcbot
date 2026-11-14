# 更新日志（CHANGELOG）

本文件记录 ZCBOT 每一个版本的变化，**按代际（发展阶段）组织、版本内按时间倒序**。
版本事实以 GitHub [Releases](https://github.com/kuangxing6367/zcbot/releases) 与 Git Tag 为准，
日期取 Tag 创建日期（UTC+8）；未发布的在研变化放在最顶部「开发中」一节。

版本号遵循语义化版本：`主版本.次版本.修订号`，`-alpha/-beta` 为预发布，`build.N` 为 Alpha 期的持续构建号。

---

## 代际主线（一句话看懂 ZCBOT 的演进）

| 代际 | 版本区间 | 时间 | 这一代在解决什么 |
|------|----------|------|------------------|
| **第一代 · 诞生** | v0.0.1-alpha（build.4–23） | 2026-08-04 ~ 08-10 | 从 0 搭出「基于 OneBot v11 的事件驱动的 IM 平台」：反向 WS、插件热加载、Web 面板、双方言数据库，并用连续 build 快速夯实稳定性 |
| **第二代 · 公测与界面现代化** | v0.0.1-beta、v0.1.0-beta | 2026-08-11 ~ 08-20 | 公测首发、补齐基础插件；Web 管理后台从原生 HTML/JS 全量重写为 Vue 3 + Element Plus |
| **第三代 · 正式版与健壮性** | v1.0.x、v1.1.x | 2026-08-20 ~ 08-30 | 发布首个正式版，前端可被插件接管；集中修复内存/性能/安全，引入内存看门狗与插件管理修复 |
| **第四代 · 权限与治理** | v1.2.0-beta | 2026-08-30 ~ 09-08 | 引入 节点式权限引擎与接口令牌（API Key），插件孤儿任务自检，框架开始具备"可治理的多用户后台"骨架 |
| **第五代 · 事件驱动 IM 平台** | v1.3.x | 2026-09-09 起 | 官方能力全部下沉为 `core_plugins`，框架回归"极简内核"；补齐终端、相对导入、可靠热重载；**v1.3.5 起框架核心零 OneBot 实现，OneBot 11 退为可插拔的默认接入端** |
| **第六代 · 多协议接入 + 定位改版** | v1.6.x | 2026-09-24 起 | 内置 QQ 官方 / Telegram / Discord / 出站 WS 四接入端，连接页与发送路径协议中立；全库统一为「事件驱动 IM 平台」定位 |

> 主线叙事：ZCBOT 起步于「OneBot v11 接入端的事件驱动 IM 平台」，但插件化、权限、持久化、Web 后台这些骨架从一开始就是通用的。
> 第五代（v1.3.x）把这条路线收口——**内核 = 极简内核 + 扩展点契约 + 官方插件集（core_plugins）+ 用户插件（plugins）**，
> 换一个 `ProtocolAdapter` 就能接入 HTTP Webhook、定时事件或任意其它 IM，OneBot 只是默认接入端，不再是身份。
> v1.3.8 起内核正式确立**扩展点（Hook）系统**，允许扩展挂到启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每一个运行环节。

---

## 开发中

> 主题：**把测试口径从「两份手写清单」收敛为「一处自动发现」**。
> 起因是 v1.22.0 的技术债审计：`ci.yml` 与 `tests.yml` 各自维护一份手写测试文件清单，
> 两份内容互相错位，导致 26 个测试文件、342 个用例不在任何 CI 里执行；
> 另有 14 个文件的用例函数体零 `assert`，被 pytest 收集到却永远 PASS。
> 本节改动全部以真跑结案（本地全量 + 干净树序列 + CI 实跑）。

### 修复：消息首尾「看不见的空白」导致命令全部失配（机器人静默不回复）

线上反馈：某些适配器/平台会在消息前后多带一个空格，同一条 `/help` 客户端看着
完全正常，机器人就是不回。实测确认范围比反馈更大：

| 消息实际内容 | 旧行为 |
|---|---|
| `" /help"` / `"\t/help"` / `"   /help"` | 失配 |
| `"\u3000/help"`（中文输入法全角空格） | 失配 |
| `"\u00a0/help"`（不间断空格） | 失配 |
| `"\u200b/help"`（零宽空格） | 失配 |
| `"/help\u3000"` / `"/help\u200b"` | 失配（只把 ' '、'\t' 当分隔符） |
| `"[@bot] /help"`，但 @ 段前有 `reply` 段 | 失配（旧逻辑只看第 0 段是不是 at） |

失配后整条命令静默丢弃，连"未匹配"日志都不显眼——这也是这类问题最难查的地方。

- `framework/messaging/event.py`：新增 `normalize_match_text()`——删零宽字符
  （U+200B/200C/200D/2060/FEFF）、把全角空格 / NBSP / 各类 Unicode 空白归一为
  半角空格、去首尾空白；ASCII 消息走 `str.strip()` 快路径。**只动首尾与非常规空白**，
  正文内部普通空格保留（`/echo a  b` 的参数仍是 `a  b`）。
- 新增 `_strip_bot_at_prefix()`：剥离开头连续的 `[@self_id]` / `[CQ:at,qq=self_id]`，
  不再要求 @ 必须在第 0 段——群里「先回复再 @机器人」、adapter 直接给 CQ 字符串
  两种场景都能命中；纯 @ 消息保留原文，@ 别人则不动（不把 `@某人 /help` 当本机命令）。
  原始 `segments` / `raw_message` 完全不变，插件要原文照样拿得到。
- `framework/messaging/router_match.py`：命令与参数之间的分隔符改用 `str.isspace()`
  判定（全角空格也能当分隔符）；对绕过 Event 的调用方补一次左侧清理兜底。
- `framework/messaging/router.py`：非正则命令 pattern 在预编译时规范化——后台/配置里
  粘贴进来的 `" /help"`、`"签到 "` 不会变成永远命中不了的死命令。
- `framework/messaging/router_keywords.py`：非正则关键词同样规范化。
- `framework/iipc/py_sdk/event.py` + `sdk.py`：插件进程侧是独立的 1:1 实现，
  同步了同一套规范化与 `isspace()` 判定，否则 IIPC 插件仍会漏匹配。

新增 `tests/test_message_whitespace.py`（18 用例）：六类空白 × 命令/参数/关键词 ×
Event 构造（含 reply 在前、CQ 字符串）逐一断言，并锁定"参数内部空格不被吞"、
"`/helper` 不能被 `/help` 命中"。

### 变更：前端探测端点常驻，不再靠蜜罐兜底放行

上一版给 `/api/session/handshake`、`/api/plugins/<名>/status` 开了"良性命中"分支，
但正常用户仍每次都在探测两个不存在的路径——良性记录照样刷日志。

- `framework/api/session_tokens.py`：会话令牌**关闭时也注册**握手端点，返回
  `{"code":0,"data":{"enabled":false,"master":null}}`，前端据此走普通 token 登录、
  不再重试。
- `framework/api/plugins.py`：新增 `GET /api/plugins/<plugin_name>/status`（带鉴权），
  返回 `{installed, loaded, enabled, status}`；未安装的插件也返回稳定结构。
- 蜜罐的良性分支保留为双保险（其它未注册路径不再误拉黑），但这两个路径已不走它了。

### 新增：操作审计（audit_logs）覆盖消息侧命令

此前 `audit_logs` 只有 Web 管理面的操作（登录、改配置、加管理员、封 IP），
消息侧「谁在哪个群执行了什么命令、被谁拒绝了」完全没有留痕，出问题只能翻运行日志。

- 新增 `framework/core/audit_writer.py`：`AuditWriter` 由 Framework 持有，
  生命周期与 `stats_writer` 一致（start / stop 时 flush 收尾）。
  * 事件：`command_exec`（执行，含成功/失败）、`command_denied`（权限不足）、
    `command_blocked`（黑名单拦截）、`message_unmatched`（无人接管，默认关闭）。
  * **热路径零阻塞**：`record()` 只做一次 `deque.append` + 截断，落库由后台任务
    按 `flush_interval` 批量 `execute_many`；队列满直接丢弃并计数，写失败只告警，
    绝不向调用方抛——审计是旁路，不能反过来拖垮消息路由。
  * **脱敏**：detail 里正文统一截断 200 字；`record_message_text: false` 时正文
    完全不入库（审计表不是聊天记录表）。
  * 整批写入失败时逐行隔离，坏行不拖垮整批。
- `framework/messaging/router_match.py`：在执行成功 / handler 异常 / 超时熔断 /
  权限不足（admin、super、权限节点）/ 黑名单拦截共 6 个分支写入审计，操作者记
  `group:<gid>/user:<uid>`（私聊 `user:<uid>`）。
- `framework/messaging/router.py`：消息最终无人接管时写 `message_unmatched`。

配置（全部有默认值，不配也生效）：

```yaml
framework:
  audit:
    enabled: true            # 总开关
    command_events: true     # 命令执行/拒绝
    unmatched_events: false  # 未匹配消息（噪音大，排查「为什么不回复」时打开）
    record_message_text: true
    flush_interval: 5.0
    max_queue: 5000
```

查看看板同一入口：`GET /api/audit_logs`（WebUI 审计页）。

### 修复：WebUI 首屏 1.71 MB 全量重下 → 0.49 MB，二次访问 0 字节

线上反馈「前端太大 + 随机刷新会命中诱饵」，查下来是两个独立问题叠在一起。

**体积**：首屏 9 个资源合计 1,792,361 B，服务端既不做压缩、也不给 `Cache-Control`，
浏览器每次刷新（以及在管理后台里点来点去）都在重新拉 element-plus 那个 1.03 MB 的 chunk。

- `framework/api/static_routes.py` 重写交付路径：JS/CSS/SVG 按 `Accept-Encoding`
  现场 gzip（压缩结果按 `(路径, mtime, size)` 缓存在进程内，带容量上限与陈旧键清理），
  带 `Vary: Accept-Encoding`。
- 缓存分层：`app.D4M7--WT.js` 这类带内容 hash 的文件名给
  `public, max-age=31536000, immutable`；`theme-init.js` / `zc-decoy.js` 这类散装脚本
  没有 hash，只给 5 分钟；`index.html` 必须 `no-cache`，否则发版后用户还在跑旧引用。
- 补 `ETag`（mtime+size）与 `If-None-Match` → 304。注意 Werkzeug 的
  `ETags.contains()` 只认不带引号的裸值，响应头里才加引号。
- 静态读取统一走 `werkzeug.security.safe_join`：`filename` 直接来自 URL，
  旧实现拼路径等于把 `../../config.yaml` 交给 `open()`。

实测（本机，同一套产物）：1,792,361 B → **511,937 B（-71.4%）**，
`element-plus` 1,055,560 → 321,064 B，CSS 379,342 → 52,464 B；
带 ETag 的二次访问 9 个资源全部 304、**0 B**。

**误伤**：`[蜜罐] 命中诱饵 POST /api/session/handshake ← 111.x.x.x` 这类日志，
来源不是扫描器，是**自己的管理后台**：

- WebUI 一打开就 `POST /api/session/handshake`（会话令牌默认关闭，
  `framework/api/session_tokens.py` 不注册路由）；
- 同时 `GET /api/plugins/image_renderer/status`（该插件没启用）。

两条都落到 `/api/<path>` 兜底诱饵，走默认分支：**拉黑 IP + tarpit 600ms + 写审计表**。
`blacklist_ttl` 默认 3600，于是正常用户打开后台就被自己服务器封一小时，
表现成「随机转圈/白屏」。安全看板的 TOP IP 榜首也一直是自己。

- `_hit()` 增加 `benign` 分支：良性只累加独立的 `_stats['benign_hits']`，
  打 debug 日志、不写审计、不进 `total_hits`/`by_ip` 威胁画像。
- `_api_fallback` 对前端探测路径放行：`/api/session/handshake`、`/api/session/confirm`、
  `/api/plugins/<名>/status` 返回标准 404 JSON，不 tarpit、不发 canary、不拉黑。
  其它未知路径的处置完全不变（用例里锁住了这条）。
- `webui/web/js/zc-decoy.js`：四个诱饵请求原本复用一个 `XMLHttpRequest`，
  第二次 `open()` 会 abort 上一次，控制台一堆 `net::ERR_ABORTED`；改为每个请求独立对象。

> 已经中招的部署：误封记录在 `ip_blacklist` 表（`expires_at` 到点自动解封），
> 也可以登录后台 `POST /api/security/unban {"ip": "..."}`（需超管）或直接删那一行。

### 工程：清理「用例返回值当汇报文字」（59 处）与死代码 ApiContext

`tests/` 里有一批用例用 `return "xxx 正常"` 把成功信息交给脚本运行器 `print`。
在 pytest 下这是另一条掩盖问题的通道：本机 pytest 9.2.0 跑出来只是
`PytestReturnNotNoneWarning`，用例照样绿——返回值**永远不可能让用例失败**，
真正的校验只靠前面那些 `assert`，而脚本运行器那边又把它当成「这条过了」的证据。

- 59 处全部清掉：函数末尾的返回直接删除；分支内的（`except ImportError: return "未安装 Flask，跳过"`
  这类）改成无值 `return`，控制流一字未变。
- 5 个文件的 `__main__` 运行器不再 `print(fn())`，改为先调用再取 `fn.__doc__` 打说明——
  人工跑脚本时仍然看得见每条用例在验什么。
- `pyproject.toml` 的 `[tool.pytest.ini_options]` 加
  `filterwarnings = ["error::pytest.PytestReturnNotNoneWarning"]`，防回涨。
- 顺手删掉 `framework/api/context.py`（45 行 `ApiContext` 骨架）：`webapp.py` 的功能域注册
  列表里没有它，全仓唯一的提及是 `framework/api/__init__.py` 的包说明，已同步删除。

**更正上一轮的一处误判**：`framework/perm/tracks.py` 并非死代码。它由
`framework/perm/__init__.py` 的惰性映射表按需导入（`list_tracks` / `promote` 等入口），
`from framework.perm import list_tracks` 实测可用，不能删。

> 遗留：仍有 14 个用例函数没有任何断言机制（`test_smoke.py` 的 9 个「不崩即过」启动冒烟
> + 5 个零断言用例），属下一批。

验证（Windows 本机 3.13）：全量 `pytest tests --ignore=tests/db_conformance` **967 passed /
1 skipped / 0 failed**（3m11s，与改动前基线一致）；脚本式回归 20 个文件 / 50.0s / 0 失败；
`--check` 收集 1019 例（下限 1000）；`framework.apis` / `framework.api` 导入正常；
`filterwarnings` 生效后三组相关用例 55 passed 且 `PytestReturnNotNoneWarning` 归零。

### 修复：数据库连接与事件缓冲句柄的确定性关闭

以 `-W always::ResourceWarning` 跑测试暴露两类真句柄泄漏，都用「显式 close + finalizer 兜底」
收口，不靠 GC 时机：

- **SQLite 线程本地连接**：`_get_conn_sqlite` 建的连接由创建线程持有，线程结束、`Database`
  被回收、进程退出三条路径都没人关。表现是 `ResourceWarning: unclosed database`，
  在 Windows 上是真故障——句柄不还，临时库/日志库删不掉。现在连接创建即登记
  （附线程 ident），`close()` 关当前线程 + 已结束线程遗留的连接（仍在跑的线程手里那条不碰，
  跨线程 close 会让它下一次查询直接 `ProgrammingError`），`Database` 侧挂 `weakref.finalize`
  做最后兜底。
- **L2 文件后端句柄**：`_FileL2Backend` 的 `a+` 句柄、`EventBuffer` 的 L2 sqlite 连接，
  调用方忘了 `close()` 时要挂到 GC 才关。补 `__del__` 只做关闭，不做正常 `close()` 里的
  VACUUM / L4 落盘——那些属于停机流程，不该跑在不确定的线程上。
- 测试侧一并收拾：`_teardown` 先关库再删临时库文件（旧写法靠 GC，Windows 上删不干净）；
  5 处 `open().read()` 换成 `Path.read_text()`；静态文件响应显式 `close()`。

验证（`-W always::ResourceWarning`，Windows 本机）：三个数据库测试文件 3 warnings → 0；
数据库 + 事件缓冲 + 令牌 + 加固四组 96 passed，只剩 3 条 `unclosed event loop`
（`ProactorEventLoop`，来源未定位，已单列待办）；`-W error::ResourceWarning` 下
「建库 → 查询 → 丢弃 → gc」探针通过。


### 修复：26 个测试文件、342 个用例从未在 CI 执行

`tests/` 下有 98 个 `test_*.py`，两份手写清单加起来只覆盖 72 个。漏跑的包括三个安全套件
（`test_api_security.py` / `test_p0_security_regressions.py` / `test_security_hardening.py`），
以及上一轮刚修完恒真断言的 `test_buffer_l2_file.py` / `test_scheduler_graceful.py`——等于修了也没人验证。
漏加文件是静默失败：YAML 里没有它，评审也看不出来。

- 新增 `tools/run_script_tests.py`：用 AST 静态分析判定「必须脚本执行」的文件——
  没有 `def test_*`（pytest 收集 0 例，整个文件不执行），或全部用例零断言
  （收集到了但永远 PASS）。判定结果 20 个文件，逐个 `python` 执行，清单不再手写。
- 新增 `--check` 模式：断言 pytest 收集数不低于基线（当前 1000），
  防止 `--ignore` / `-k` / `--deselect` 把用例悄悄丢掉。
- `tests.yml` 的 pytest 步骤改为 `pytest tests --ignore=tests/db_conformance` 全量收集，
  不再列文件名。

### 变更：CI 分层

- `ci.yml` 收敛为快速闸门：多版本 `compileall`（3.10–3.14）+ 启动冒烟 + WebUI 产物校验。
  两份手写测试清单删除——测试口径统一到 `tests.yml`，同一件事不再有两份真相。
- `tests.yml` 成为权威测试流：矩阵 3.10–3.13 正式 + 3.14 观察（`continue-on-error`，
  跑绿一轮后转正）；脚本式回归、全量 pytest、DB 一致性套件、覆盖率 XML 归档。
- 新增 `windows-smoke` 作业：此前 CI 全部跑在 `ubuntu-latest`，而本项目的主要使用与
  开发平台是 Windows。首轮观察，稳定后转硬门禁。

### 新增：覆盖率采集（只观测，不设阈值）

CI 开始归档 `coverage.xml`（仅 3.12 版本）。当前语句覆盖率约 50%（38,652 行 / 233 个文件，
11 个模块零覆盖）。先沉淀历史曲线，水位稳定后再上 ratchet（只许升不许降）。

### 修复：测试隔离污染（stdio 编码）与重复定义的测试块

干净树全量跑暴露 `3 error / 1 failed`，报错是 `UnicodeDecodeError` 却指向
`test_watchdog_stack`——实际根因在 `test_terminal_panel`：

- `test_align_and_restore_stdio_no_raise` 直接调用 `align_stdio_to_console()`。
  在 Windows + 控制台代码页 936 的环境下，它把 pytest 的全局捕获缓冲区
  （`CaptureIO`）`reconfigure` 成 cp936，此后中文输出写成 GBK 字节，
  pytest 读取捕获内容时按 UTF-8 解码即崩。改为替身流 + 伪造代码页，
  验证逻辑不碰真实 stdio。
- 同文件「终端编码对齐」测试块被整段复制了一遍，3 个函数重名、后者覆盖前者
  （pytest 只跑最后一份定义）。删掉重复块。
- `align_stdio_to_console` 取不到原编码时不再 `reconfigure`：原实现会记下
  `None`，而 `restore_stdio` 的 `if enc` 跳过回滚，等于把进程级 stdio
  永久改成 cp936。

### 修复：Windows CI 的 stdio 编码

GitHub 的 Windows runner 默认 stdio 是 cp1252，而冒烟脚本与框架日志都输出中文。
`PYTHONIOENCODING=cp1252` 可在本地稳定复现：`UnicodeEncodeError: 'charmap' codec
can't encode characters`，位置在 `test_smoke.py` 的 `print`。`windows-smoke`
作业级加 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` 后恢复。

> 遗留：Windows 下把输出重定向到非 UTF-8 流时的编码兜底属产品侧问题，单列待办，
> 不在本次 CI 修复范围内。

### 工程：lint 清账 1490 条，规则门禁按「已清零」开放

ruff 默认规则下全仓 4072 条违规中，1490 条属于工具自己标注为可自动修、且不改变运行语义的
那部分。分三批清完，每批一个提交，避免一次 400+ 文件的 diff 让 `git blame` 失效：

| 提交 | 规则 | 条数 | 性质 |
|---|---|---|---|
| `style:` 格式类 | `UP009` 335 / `I001` 220 / `UP032` 194 / `RUF022` 25 / `RET501` 22 / `FURB167` 19 / `PLR1711` 19 / `PIE790` 16 / `F541` 14 | 846 | 纯格式，零行为变更 |
| `style(typing):` 注解现代化 | `UP006` 174 / `UP045` 100 | 274 | `typing.Dict` → `dict`、`Optional[X]` → `X \| None`，`requires-python>=3.10` 下均为合法写法 |
| `style:` 失效抑制 | `RUF100` | 370 | 已不再匹配任何违规行的 `# noqa` |

`RUF100` 这批暴露出一个历史事实：被删的 370 条里有 **240 条是 `# noqa: BLE001`**——
盲 `except` 的抑制注释写了，但 `BLE001` 从来没在配置里启用过，注释一直空挂着。

- `tests.yml` 的 `static-contract` 作业新增 `Lint gate`：只锁上面这批**已清零**的规则，
  禁止回涨。**没有**开全量 lint 门禁——仓库仍有约 3k 条历史违规（盲 except、超长函数、
  `__all__` 以外的结构问题），一次性开全量会让主干永远红，治理按阶段走。
- 验证：846 + 274 + 370 全部 `0 remaining`；批次 A 全量 `967 passed / 1 skipped / 0 failed`
  （3m23s）+ 脚本式 20/20；批次 B/C 366 + 32 + 110 个改动文件 `py_compile` 全通过、
  全量用例收集 968 例 0 错误、`F821` 门禁复跑仍为 0。

### 工程：静默捕获治理（708 → 48）

全仓 708 处「捕获后既不记日志也不抛出」的宽泛 `except`（`framework` 517 / `core_plugins` 180 /
脚本 11）。原则是**先让它可见，不改控制流**——所有降级行为一字未动：

| 批次 | 范围 | 处数 | 处理 |
|---|---|---|---|
| 1 | `core/event_buffer.py` | 10 | debug：行数统计、句柄关闭、体积估算、L2 迁移单行失败 |
| 1 | `core/base.py` | 9 | debug 8 + warning 1（控制台 stop 失败 → 线程可能未释放） |
| 1 | `messaging/router.py` | 14 | debug 12 + warning 2（线程池解析权限失败、适配器判定 fail-open） |
| 1 | `loader/runtime.py` | 13 | debug：内存估算、路由缓存失效、孤儿任务清理、状态回写 |
| 1 | `console_server.py` | 18 | debug：acceptor 停止、连接关闭、只读打开回退读写 |
| 1 | `bootstrap/instance_lock.py` | 15 | debug：锁目录/pid/fd/flock/残留锁清理 |
| 2 | 其余 137 个文件 | 581 | 按函数名判级别，18 处落在权限/鉴权/会话相关函数走 warning，其余 debug |

第 2 批是脚本化处理的：AST 定位宽泛/bare 处理器、判定体内是否已有 raise/assert/日志调用，
无 `as name` 的补 `as _exc`（用 `e` 会遮蔽外层变量），无模块级 logger 的自动补
`import logging` + `getLogger('zcbot')`。补完日志后原 `pass` 成为多余占位，一并清掉（PIE790）。

剩下 48 处**刻意保持静默**，理由写在代码里：

- `framework/terminal/` 下 TUI 面板与终端命令 34 处：运行在 ANSI 备用屏 + raw 输入模式，
  写 stderr 会打花画面；终端恢复路径本身就在 `except` 里，再写日志可能重入。
  失败一律走安全降级返回值（80x24 回落、保留原终端模式），约定写进 `panel.py` docstring。
- `tools/` 与 `startup.py` 的一次性诊断脚本 14 处：进程即将退出，输出会污染被诊断者的标准流。

另有一处不算静默：`core/base.py` 命令执行失败，异常信息已作为回复返回给用户，属已观测路径。

验证：137 + 6 个改动文件 `py_compile` 全通过；门禁规则 0 违规；全量用例收集 968 例 0 错误；
脚本式回归 20/20 PASS；定向用例 31 + 17 + 16 passed；静默捕获复算 47 → 1（首批）、708 → 48（全量）。

### 其他

- `pyproject.toml` dev 组补 `pytest-cov`，并把 `pytest-asyncio` 锁到 `<1.0`，
  与 workflow 里的口径一致（此前 dev 组只写 `>=0.23`，本地可能装到 1.x 改变默认模式）。
- `.gitignore` 补 `coverage.xml`。

---

## [1.22.0] - 2026-11-14

> 主题：**把「装完就能用」和「装了能验」两条路都跑通**——先是 pip 安装态的整条
> 上手路径（入口 / 路径 / 版本自报 / CLI 视图 / 备份安全 / 发行包内容），再按外部
> 静态检查报告补上插件的类型契约与 F821 门禁，最后做一轮全仓审查，兑现「装饰器插件
> 可省略 `register`」的文档承诺并补齐 CI 缺口。三批改动全部以真跑结案（干净 venv、
> 干净 wheel、全量测试），不以「读代码觉得没问题」为准。
> 本节变化自 `435bdb7` 起陆续提交并推送到 `origin/main`，现随 `v1.22.0` 打 Tag 发版；
> `VERSION` / `pyproject.toml` / `README.md` 三处版本口径已同步为 1.22.0。

### 默认安装与首次启动

- `flask` / `flask-cors` / `waitress` 并入**核心依赖**（原只在 `web` 可选组）。
  `core_plugins.yaml` 里 `webui` 默认 `enabled: true`，即默认安装就会加载 Web 后台，
  这三个包对默认路径其实是必需的。原先 `pip install zcbot` 装完首次启动必然缺 flask，
  只能靠运行时依赖自愈现场联网补装——离线 / 内网环境装完就是起不来。
  `[web]` extra 保留（旧文档 `pip install "zcbot[web]"` 仍然有效）。
- 官方插件「依赖未安装」不再记 `ERROR` 并打完整堆栈：依赖缺失是自愈流程的**预期输入**，
  改为 `WARNING` + 可操作提示，堆栈降到 `DEBUG`。原实现让一次完全正常的首次启动
  刷出一屏红色 traceback，最终结果却是自愈成功——把正常流程呈现成故障。
  实测：首次启动 ERROR 行数 20 → **0**，启动耗时 20.27s → **1.94s**（无需自愈）。

### 启动入口与脚手架一致

- README 主推路径 `pip install zcbot` + `zcbot` **实际不会启动**（裸命令只打印帮助）。
  文档改为 `zcbot run`，并在裸执行时补一行提示，避免误判成装坏了。
- `zb run` 在本目录没有 `startup.py` 时回退到已安装包的入口（pip 安装态它就在
  site-packages 根）；显式 `-f <缺失文件>` 仍按错误退出（rc=2），不静默换目标。
  原实现只认 `cwd/startup.py`，而 `zb init` 又提示「zb run」——官方脚手架的第一条
  上手路径必然报「入口文件不存在」。
- `zb generate` 默认输出 `startup.py`（原 `run.py`），与 `run` 的默认入口统一；
  `-f` 仍可自定义，已存在则不覆盖。
- `zb init` / `zb create plugin` 的落点改为与内核同一套解析（`framework.paths`）：
  配置进 `<home>/data`、用户插件进 `<home>/data/plugins`。原实现写死 `cwd/config.yaml`
  与 `cwd/plugins`，pip 安装态造出的是一份运行时根本不读的旧布局，下次启动还被
  内核迁移判成「新旧两处并存」。

### 版本自报

- `zb -v` / `zb --version` / `zcbot -v` 在 pip 安装态恒为 `0.0.0`：`read_version()`
  只读 `REPO_ROOT/VERSION`，而该文件不属于任何包。改为复用
  `framework.migration.version.current_version()`，优先级
  `VERSION` → **已安装包元数据（`importlib.metadata`）** → `pyproject.toml` → `0.0.0+unknown`；
  四处全缺时输出带 `+unknown` 后缀，不伪装成合法版本号。

### CLI 视图与运行时一致

- `zb plugin list` 只扫 cwd 的 `plugins/` 与 `core_plugins/`，pip 安装态两者都不在 cwd，
  于是运行时已加载 19 个官方插件，CLI 却报「未找到任何插件」。改为优先扫内核解析出来的
  目录（`paths.user_plugins_dir()` / `paths.core_plugins_dir()`），再补 cwd 下的历史布局，
  同名去重；启用状态改读内核实际的 `core_plugins.yaml`。空列表时会打印实际扫描的根目录。

### 状态路径与备份（数据安全）

- **迁移前备份被静默跳过**：`backup_snapshot()` 拿配置里的相对值 `data/zcbot.db` 做
  `os.path.abspath()`，解析到 **cwd**；而连接层按 `get_home()` 把库开在 `<home>/data`。
  工作目录 ≠ home 时（pip 安装态、systemd `WorkingDirectory`、容器）两者不是同一个文件，
  备份打一行「库文件不存在，跳过快照」后 **destructive 迁移照常执行**——「可回退」承诺
  被静默降级成没有备份。改为优先用连接层实际打开的 `_db_path`，回落时按 `get_home()` 解析。
  实测：`已备份到 <home>/data/backups/zcbot-schema-<stamp>-v2.db`。
- L2 溢出日志同一处病：`event_buffer.l2_path` 取配置相对值后直接 `open()`，落到
  `<cwd>/data/event_buffer.l2.log`，而库与配置在 `<home>/data`，同一份状态被劈成两处。
  改为与仓库其余相对路径（`log.file`、iipc log、db path）同口径按 `get_home()` 解析。
- 全新安装默认落 `data/` 布局（`paths._prefer_data` / `user_plugins_dir` 在“两处都不存在”
  时返回新布局路径）。原实现返回旧的 home 根，于是全新装要跨两次启动才收敛，
  期间「新旧两处并存」触发 `manual-required`，用户什么都没做错却被要求人工确认信哪份配置。
  实测：连续两次启动 migration 无任何输出、cwd 零残留、`manual-required` 不再出现。

### 发行包内容

- wheel 不再包含 `framework/iipc/tools/` 下 14 个开发期工具（含 `iipc_hardening_test.py`）：
  该目录无 `__init__.py`、运行时无任何 import，却被当成命名空间包整目录打包。
  注意 `zcbot.egg-info/SOURCES.txt` 是陈旧清单，清掉 `egg-info/` 与 `build/` 后重建才生效。
- README 的仓库主次写清楚：GitHub 为主仓库（CI / Release / PyPI 发布在此），Gitee 为国内镜像，
  与 `pyproject.toml` 的 `Project-URL` 口径一致。

### 静态检查契约与回归测试（第二轮外部报告复核）

外部报告指出「ruff 报 204 处 F821 `ctx` 未定义」并批评这是隐式框架魔法。逐条实测后：

- 204 条 F821 属实，但 **202 条是裸 `ctx`、2 条是别的符号**：
  `framework/api/app_helpers.py` 的 `_yaml_config_path()` 漏导入 `get_home`（读配置段
  会真的 NameError）、`core_plugins/llm_load/src/llm_core/media.py` 的字符串注解漏
  `OrderedDict`（静态红、`get_type_hints` 会炸）。两处均已补齐。
- 注入点不是一处而是**四处**（`loader/base.py` / `core/runtime.py` /
  `iipc/py_sdk/sdk.py` / `terminal/cmd_plugin.py`）；202 处里 171 处集中在 IIPC 的
  `api_surface` 插件，走 SDK 那条路。运行时注入确实有效，报告「不是 bug」的判断成立；
  但「没声明、没类型、IDE 补全不全、静态检查全红」也确实成立。
- 六个裸用 `ctx` 的插件补上 `TYPE_CHECKING + cast("PluginContext", None)` 类型化占位：
  **不改调用方式、不动 API、不加运行时依赖**，handler 仍在注入后才执行。
  `ruff check --isolated --select F821 .` 由 204 条降到 **0 条**。
- 新增 `tests/test_ctx_contract_and_install_regressions.py`（25 条用例），把
  「四处注入点存在 / 六个模块有占位 / echo handler 注入后真能发消息」以及上一轮
  pip 安装态修复（版本自报、迁移备份路径、事件缓冲路径、`zb init` 落 `data/`、
  `zb run` 回退、核心依赖与 wheel 排除、CLI 插件视图）全部锁成回归测试——
  上一轮改了 15 个文件却零测试，这轮补上。
- CI 新增 `static-contract` 作业跑 F821 门禁；`docs/指南/编写插件.md` 补
  「模块级 ctx：给静态检查一个交代」一节（含模板与三条边界）。
- 顺手修 `pyproject.toml` 开头注释与核心依赖的口径漂移（还写着 flask 三件套
  属于可选组）。

### 全仓审查：装饰器-only 插件契约与 CI 缺口

> 起因：外部指出「文档写明可以省略 `register`，但无 register 时 `@command` 不落账、
> `_PENDING` 残留」。复现确认成立，且后果比报告更重——残留项会在下次加载时被
> 重复 flush，同一命令注册两次。

- **无 `register` 的装饰器插件现在能正常加载**。`load_plugin()` 原先在
  `hasattr(module, 'register')` 为假时直接判失败，而文档承诺「用了装饰器可以省略
  `def register(ctx)`」。现在改为：`register` 缺失但导入期有装饰器登记项
  （`framework.plugin.has_pending()`）→ 照常加载并 flush；两者都没有的空插件
  仍拒绝加载。`register_commands()` 同步放开，返回 `True`。
- **`_PENDING` 泄漏与重复注册**（数据正确性）：`clear_module()` 此前是死代码，
  全仓无人调用；`_purge_plugin_modules()` 只清 `sys.modules`。于是一个「导入期已
  登记、register 没跑到」的插件，登记项永久驻留，修好后重载会被 flush 两遍。
  现在 purge 会连带清掉待登记项。实测：失败后 `_PENDING` 残留 0，重载只注册 1 条。
- 新增 `tests/test_decorator_only_plugin.py`（3 条）：装饰器-only 可加载、
  空插件被拒、失败重载不重复注册。
- **CI 补齐与纠偏**：
  - `tests/db_conformance`（schema 对齐 / 事务 / 类型映射 / 方言翻译 / 并发，
    51 条，默认 sqlite 后端）此前两个 workflow 都没跑，现已纳入 `tests.yml`；
  - `ci.yml` 里「flask 未在 requirements.txt 中声明」的注释已过期（上一轮已并入
    核心依赖），同步删掉重复安装；
  - `tests.yml` 的 `pytest-asyncio` 未锁版本、`ci.yml` 锁 `<1.0`，统一为后者；
  - `pytest-timeout` 进了 `[dev]` 与 `requirements-dev.txt`：CI 用
    `--timeout=120`，但按文档装 dev 组不含它，本地复现 CI 会直接参数报错；
  - `tests.yml` 里 WebUI 作业注释串位到 `static-contract` 上方，已归位。
- **文档与依赖注释纠偏**：`requirements.txt` 头部仍写「main.py 启动自检读取本文件」
  （实际读它的是 `framework/bootstrap/deps_check.py`，由 `startup.py` 调用）；
  `core_plugins/webui/requirements.txt` 仍写「未启用时框架本体不安装 flask」
  （flask 已是核心依赖）；`image_renderer/requirements.txt` 缺行尾换行。
- **测试自身**：`test_buffer_l2_file.py` 与 `test_scheduler_graceful.py` 各有
  一处 `check(..., True)` 恒真占位，改为真实断言（前者删掉冗余行、由后续
  `source` 断言覆盖；后者改为测量两次 `stop()` 的实际耗时）。
- **退出期崩溃**（`Fatal Python error: _enter_buffered_busy`）：日志合并器的
  flush 线程是 daemon，`stop()` 只在 `Framework.shutdown()` 里调用；测试或异常
  退出时不走 shutdown，线程活到解释器 finalizing 阶段还往 stderr 写汇总，CPython
  直接终止进程。全量 pytest 结束时偶发，表现为「测试都过了但进程 fatal」。
  改为弱引用登记 + `atexit` 统一 `stop()`，并在 `_flush_loop` / `_emit_summary`
  上加 `sys.is_finalizing()` 守卫。修复后连跑全量 + 5 次裸退出复现，0 次 fatal。

### CI 红灯：干净 checkout 下的布局断言

- `tests` workflow 在 `3b018ea` / `f7d6b65` 两次推送后失败（`Startup + dispatch smoke`，
  Python 3.10 / 3.11 / 3.12 全挂，约 28s 退出）。根因不是产品代码：`test_smoke.py`
  有两处断言写死了**旧插件布局** `<仓库根>/plugins`，而 `435bdb7` 起全新安装默认落
  `<home>/data/plugins`（`framework.paths._prefer_data`）。开发机同时存在 `plugins/`
  与 `data/`，`_prefer_data()` 走旧分支所以本地一直通过；CI 是干净 checkout，两处都
  不存在 → 走新布局 → 断言失败。改为与 `framework.paths.user_plugins_dir()` 比口径，
  不再写死任何一种布局。
- `tests/test_decorator_only_plugin.py`（上一轮新增）没进 `tests.yml` 的 pytest 清单，
  已补上——新测试不进 CI 等于没测。

### 未处理（需单独立项）

- **顶层命名空间污染**（`framework` / `core_plugins` / `sql` 与 `startup.py` / `main.py` /
  `zb.py` 直接落在 site-packages 根，`entry_point` 指向 `framework.cli:main`）。这是真问题，
  但改名会让所有第三方插件的 `from framework ...` 失效，属破坏性变更，留待 v2.0 统一处理。
- **历史 lint 告警 / 类型覆盖 / 超大函数 / 静默吞异常**（第二轮报告复核确认成立）：
  ruff 0.17.5 默认规则集全仓约 4.2k 条告警；`framework + core_plugins` 的参数注解
  25.3%、返回注解 32.3%；`create_web_app` 738 行、`make_app_helpers` 511 行、
  11 个文件超 1000 行、最大 `core_plugins/qq_official/main.py` 2107 行；
  351 处 `except` 里纯 `pass`。都是真实债，但属大范围重构，本轮只把会直接导致
  运行时错误的 F821 纳入门禁，其余待单独立项。
- **发行包不带测试配置**：`pytest.ini` / `pyproject.toml` / `docs` / `CHANGELOG`
  都不进 wheel（社区惯例；仓库有 107 个测试文件、CI 有全量回归），
  「装了包就能复现验证」做不到；若要做需单独设计 sdist / 验证包形态。

### 验证

- 全量 `py -m pytest tests -q`：**1017 passed / 1 skipped / 0 failed**
  （上一轮 1014 条 + 本轮新增 3 条装饰器契约回归），耗时 3m09s；
  日志中 `Fatal Python error` 计数 **0**（修复前偶发）。
- `tests/db_conformance` 单独真跑：**51 passed / 3.8s**（`--db sqlite` 默认后端）。
- `ruff check --isolated --select F821 .`：**0 条**；`compileall` 通过；
  `tools/check_webui_artifact.py` 本地校验通过。
- 装饰器-only 三场景端到端复现：无 register 可加载并落账 1 条命令；导入抛异常后
  `_PENDING` 残留 0；补好 register 重载只注册 1 条（修复前为 2 条）。
- 上一轮（安装态修复）的 wheel 复验仍有效：干净重建 1,796,433 字节 / 309 项，
  0 个测试文件、0 个 `iipc/tools` 条目，`startup.py` 与 `framework/iipc/plugins/*`
  均在包内；本轮未改动打包配置，未重建。
- 干净 venv 装本地 wheel（无一预装依赖），逐条复跑外部报告：`zb -v` → 1.21.1；
  `zb init` 落 `<home>/data` 且 cwd 干净；`zb run` 无本地入口可启动、0 ERROR；
  `zb plugin list` 19 个与运行时一致；二次启动无 `manual-required`；`create plugin`
  落 `<home>/data/plugins` 并出现在列表里；迁移前备份真实产出文件。

---

## [1.21.1] - 2026-11-13

> 主题：**入口、路径、面板口径三处收口，再按公认测试法把畸形输入跑遍全部接入端**；
> 随后按文档记载的踩坑点给复杂逻辑补上祈福注释，并修掉一批既有缺陷与文档口径漂移。
> 本节变化自 `6c8ea6c` 起陆续提交并推送到 `origin/main`，现随 `v1.21.1` 打 Tag 发版；
> `VERSION` / `pyproject.toml` / `README.md` 三处版本口径已同步为 1.21.1。

### 启动入口

- 新增 `startup.py` 作为**唯一权威启动入口**；`main.py` 退化为兼容 shim，保留全部原有
  再导出符号与 `python main.py` 的行为。CLI 帮助、`zb run/generate` 默认文件、`pyproject.toml`
  的 `py-modules`、热重载 `os.execv`、文档与三语包文案统一指向 `startup.py`。

### 状态目录

- 新增 `framework/paths.py` 统一解析「代码根 / 可写 home / 状态目录」。新布局下
  `config.yaml`、`core_plugins.yaml`、`plugins/`、`logs/` 统一落在 `data/`（pip 安装态
  自动启用，即 `~/.zcbot/data`）；官方插件代码属于内核，固定由 `core_plugins/` 解析。
- 全仓 20+ 处硬编码路径（bootstrap / api / cli / iipc / terminal / deps / 更新器）改走
  统一解析层；旧根目录布局保留回退，源码 checkout 由内核迁移子系统自动收敛到 `data/`。
- 旧脚本 `tools/migrate_state_to_data.py` 退为兼容入口，不再承载任何迁移逻辑。

### 状态目录自动迁移（内核内置）

迁移能力从 `tools/` 脚本上收为内核子系统 `framework/migration/`，按职责拆开：

| 模块 | 职责 |
| --- | --- |
| `version.py` | 当前内核版本检测：VERSION → pyproject，SemVer 2.0.0 解析 / 优先层级比较、两处版本来源漂移检查 |
| `detector.py` | 探测当前目录**可能**是哪一代版本留下的布局（root / data / mixed / empty / missing / unknown），输出磁盘证据、候选版本区间与置信度 |
| `policy.py` | 自动迁移开关：**默认开启**，由 `config.yaml` 的 `framework.auto_migrate` 决定，只读绝不改写配置 |
| `migrator.py` | 幂等执行 + 账本 + 并发锁 + 启动钩子 |
| `__main__.py` | `python -m framework.migration --inspect` / `--dry-run` 命令行 |

- **方向是向后兼容**：新内核接住旧版本留下的目录布局，不要求旧版本能读新布局。
  候选版本区间取自本仓库 `CHANGELOG.md` 的真实记录（`1.8.9..1.20.2` 根目录布局、
  `>=1.21.0` 新 `data/` 布局；`1.8.9` 之前没有逐版本目录记录，只给低置信度「早期版本」），
  **不假装能精确报出版本号**。
- **自动迁移默认开启**：启动链路在 `load_config()` 之前调用 `ensure_auto_migrated()`；
  `--help` / `--version` / `config` / `attach` / `code` 等纯查询子命令不触发。
  手动关闭：在 `config.yaml` 的 `framework` 段写 `auto_migrate: false`；
  运维临时覆盖：`ZCBOT_AUTO_MIGRATE=0`。
- **账本与幂等**：`data/.zcbot-state-migration.json` 记录内核版本、探测到的代际、
  候选区间、逐项动作与 sha256 校验和；账本损坏不据此跳过迁移；已 `applied` 且无待办
  动作时重复启动零写盘；中途失败写 `partial`，下次启动按账本判定已完成项继续，
  不重复搬也不覆盖。`data/.zcbot-state-migration.lock` 防多进程并发，陈旧锁超时回收。
  做法对齐 Flyway 的 `schema_history` / `baselineOnMigrate` / 开机自动迁移语义。
- **三条硬约束**：只搬 `config.yaml` / `core_plugins.yaml` / `plugins/`，绝不碰代码资产；
  绝不覆盖 `data/` 下已有文件（新旧并存时默认要求人工确认）；用 `shutil.move`
  **绝不改写配置文件的任何字节**。迁移失败不阻断启动，旧布局仍由 `framework.paths` 兼容读取。

### 接入端与 Web 后台

- 配置面板 `_YAML_SECTIONS` 补齐 11 个此前「配置项存在、面板改不了」的顶层段
  （`startup` / `language` / `iipc` / `memory` / `dual_process` / `event_queue` /
  `multi_platform` / `github_proxy` / `terminal` / `console` / `dependencies`），
  实测可写段 29 → 40、`config.yaml` 顶层段不可写数 11 → 0。
- README 与 `docs/进阶/扩展清单.md` 补齐全部 10 个接入端
  （OneBot / Rust 加速 / 正向 WS / QQ 官方 / Telegram / Discord / KOOK / Matrix /
  HTTP 注入 / HTTP API），并纠正 `config.yaml` 与 `core_plugins.yaml` 的权威口径。

### 畸形输入与漏洞修复

按 OWASP WSTG 4.2 / ASVS 5.1 / OWASP API Top 10 2023 的输入验证与属性级授权方法，
对全部接入端入口构造通配、类型混淆、越界数值与注入载荷逐条真跑，修复：

- `http_api`：`?limit=` 直接 `int()` 再拼进 SQL，畸形值导致连接直接断开零响应、
  `-1` / `10^20` 无上界；消息 / 群管 / 广播 / DB 网关接口的 `message`、`*_id`、`duration`、
  `sql`、`params` 全部改为强类型 + 有界校验，处理函数统一异常兜底回 400。
- `rust_accel` IPC 行与 `_spawn_dispatch` 补严格 JSON 对象校验与事件净化，
  外部帧不能再携带 `role` / `permissions` / `trace_id` / `bot_name` 等控制字段。
- `http_inject` 事件构造改走 `sanitize_adapter_event()`，补齐 `bot_name` 伪造缺口。
- `framework/security/hardening.py` 新增 `coerce_int` / `coerce_text` /
  `coerce_sql_params` 与递归拒绝键（含 `trace_id` 等因果域字段）；
  `sanitize_adapter_event()` 追加字段类型归一化：外部帧把 `message` 写成 dict
  时旧路径用 `str(dict)` 兜底变成消息文本（可伪造命令），`sender` 写成数组时
  `Event.sender_role` 直接抛 AttributeError。现在保留 str / 段数组 / 数字或字符串
  ID 等合法形态，其余容器收敛为中性值。

### 验证

- 自带测试：修复前 91/91、修复后 91/91，`STATUS_DIFF=[]`（项目测试是模块级
  `sys.exit` 脚本，按 CI 方式逐个跑，未改动任何测试文件）。
- 编译检查：framework / core_plugins / tools / 双入口全量 `compileall` 通过。
- 对抗探针：240 条接入端用例 + 300 条 property-based 随机用例 + 20 条 http_api
  用例 + 6 条 rust_accel 用例，前后对照修复了断连、类型伪造与字段残留。
- 路径探针：15 个用例（dry-run / 真迁移 / 首次启动生成 / 双入口版本一致性）全通过。

### 迁移子系统补测（第 8 轮）

- **84 个用例全通过、0 失败、0 跳过**：内核版本检测（SemVer 解析 / 优先层级 /
  VERSION↔pyproject 回落与漂移）、六种目录代际探测、自动迁移开关的 16 种配置形态
  （含非法值、YAML 损坏、2 MiB 读炸弹、BOM、无 yaml 依赖的文本扫描、环境变量覆盖）、
  dry-run 零写盘、配置字节 sha256 不变、账本校验和与损坏自愈、幂等二次零写盘、
  mixed 不覆盖、partial 可重入、陈旧锁回收、3 进程并发、符号链接越界、
  启动子命令不误触发、配置/环境变量关闭、迁移异常不阻断启动、双入口一致、旧 shim 兼容。
- 自带测试：第 7 轮交付态 91/91 → 第 8 轮 91/91，`STATUS_DIFF=[]`；
  全量 `compileall` 退出码 0；**未改动任何测试文件**。
- 回归：第 7 轮对抗探针 240 条接入端用例重跑，崩溃 / 控制字段残留 /
  `bot_name` 伪造 / 非字符串 message 全为 0；webUI 探针可写段 29 → 40、
  不可写段 11 → 0；旧路径探针 15/15。
- 新增 `.github/workflows/release-pypi.yml`：`v*` tag → 版本一致性门禁 → 构建
  sdist/wheel（校验迁移子包随包分发）→ PyPI Trusted Publishing（OIDC，无需长期 token）。
  该文件已随 `6c8ea6c` 推送；只由 `v*` tag 触发，普通 push 不触发；`v1.21.1` 是它的首次
  触发。发布 job 挂在 GitHub Environment `pypi` 上，需先在 PyPI 后台登记 Trusted
  Publisher（OIDC），无需任何长期 token；版本号由发版流程同步 `VERSION` /
  `pyproject.toml` / `CHANGELOG.md` 三处，tag 与 `VERSION` 不一致时门禁直接拒绝发布。

### 交付收尾

- 文档口径同步：`docs/进阶/部署上线.md`、`docs/进阶/双核心架构.md`、
  `framework/iipc/README.md` 与 `framework/iipc/go_kernel/README.md` 中残留的
  `python main.py` 启动命令统一改为 `python startup.py`；CHANGELOG 中「未提交、未推送」
  的过期状态按实际推送结果更正。CHANGELOG 历史条目描述当时的事实，保持原样不改。
- 清理本机工作区运行缓存：删除 53 个缓存目录（52 个 `__pycache__` + `.pytest_cache/`），
  其中含 280 个 `*.pyc`；清理后以 `-B`（不写字节码）终检，双入口 `--version` 均输出
  1.21.0 / rc=0，缓存残留为 0。这些文件均被 `.gitignore` 忽略，不属于源码，也不涉及
  `config.yaml`、`data/*.db`、日志等有状态资产。

### 按文档踩坑点补祈福注释（纯注释，不改运行时行为）

通读 `docs/` 全部 43 份 Markdown（通读时 `CHANGELOG.md` 为 2086 行）及
README / LLM.md / 各插件 README，
抽出 105 条「设计哲学 / 踩过的坑 / 根因」记录，逐条钉到源码符号，AST 核实通过
94 个唯一符号、0 个臆造；在这些函数与类体内加完整祈福注释并标注文档出处与坑因。
另在 12 个入口与模块头部各一处。Python 统一用 `#`，`docs/.vitepress/config.mjs`
保留 `//`；全仓共 106 处「佛祖保佑」。

- 密度最高的是 `framework/core/event_buffer.py`：设计哲学 §6 明写
  `L1(512KB) → L4(4MB 攒批) → L2(sqlite) → L3(4MB 兜底)`、"绝不因为缓冲满而阻塞适配器"，
  CHANGELOG 对这个文件记了多条正确性与性能坑，去重后落 10 处（全仓密度最高）。
- 注释放在函数体首语句之后、`__doc__` 之后，避开装饰器与那些读源码文本做断言的安全测试；
  `py_compile` / `compileall` / `node --check` 全过，12 处注释块逐行回比原文一致。

### 体检顺带修掉的既有问题

- `framework/cli/commands/adapter.py` 缺 `os` 导入：`zb adapter enable/disable` 直接 `NameError`。
- `framework/blackbox.py` 帧体积记账漂移：trim 时按「淘汰后」的帧重新 `repr` 计算并扣减，
  与 append 时记的字节数对不上，声称 256KB 上限实测常驻 3.45MB；改为 append 时一次估算、
  帧内自带记账值，trim 只扣该值。热路径同时从 31µs/帧降到约 8µs/帧。
- `pyproject.toml` / `requirements-dev.txt` 声明 `pytest-asyncio`：此前 28 条裸 `async`
  用例因缺插件被判失败，等于静默失去覆盖。
- `tests/test_plugin_imports.py` 顶层 `sys.exit()` 让 pytest 收集阶段直接 `INTERNALERROR`；
  改为脚本直跑才退出。
- `tests/test_image_renderer_assets.py` 两个用例只 mock 了 `main._CTX`，而 `_read_cfg()`
  读的是 `framework.config.get_config()` 全局单例，前面的 config 测试把
  `image_renderer.backend` 设成 `rust` 就漏进来，导致「单独跑 32 passed、全量跑挂 2 条」；
  补隔离 fixture 后复现组合 310 passed。

### 文档口径校准（10 处）

发版前按站点规则重扫全部内部链接与文档↔源码符号，修掉 10 处漂移；**CHANGELOG 历史条目
描述当时的事实，保持原样不改**。

| 位置 | 问题 | 依据 |
| --- | --- | --- |
| `docs/速查/框架结构.md` | 指向不存在的 `docs/core-philosophy.md` | 现文档为 `docs/进阶/设计哲学.md` |
| `docs/速查/框架结构.md` | 缓冲层数写成 `L1→sqlite→L4 兜底` | 设计哲学 §6 的四层与顺序 |
| `docs/进阶/架构总览.md` | 启动入口仍写 `main.py` | `startup.py` 为唯一权威入口，`main.py` 退为 shim |
| `docs/进阶/架构总览.md` | 心跳口径「仅重新 `register`，改函数体需手动完全重载」 | `plugin.hot_reload` 默认 `true`，`framework/loader/runtime.py` 走 `unload → load → register` |
| `docs/进阶/双核心架构.md` | `_ipc_dispatch` 记在 `core_runtime.py` | 实际定义在 `framework/ipc/core_services.py` |
| `docs/进阶/可靠性与限流.md` | 入队链路漏掉 L4 | 同上四层口径 |
| `docs/速查/安全加固.md` | 引用已不存在的 `llm-framework.md` / `llm-iipc.md` | 改为 `框架结构.md` / `IIPC内核链路.md` |
| `CHANGELOG.md` ×2 | `docs/llm-security.md`、`docs/dual-core.md` 两个真实断链 | 改为现文档路径 |

校准后按 VitePress 规则复扫：真实断链 **0 条**。

### 验证

- 全量 `py -m pytest tests -q`：**989 passed / 1 skipped / 0 failed**（0:03:14）。
- `compileall` 全过；双入口 `--version` 与 `python -m framework.migration --inspect`
  读到的内核版本与 `VERSION` 一致。

---

## [1.21.0] - 2026-11-07

> 主题：**把「以为能用」变成「真的能用」**。
> 这一版修的多数问题都不报错：路由表静静空着、索引静静没建、配置写了静静不生效、
> 媒体静静发不出去。它们共同的特征是**看起来一切正常**，所以不能靠加功能解决，
> 只能一处一处把「看似成功、实际没发生」找出来。

### 接入端：出站媒体从「写了但发不出去」补齐

改之前 `kook` / `matrix` 的 `capabilities()` 写死 `outbound=[CAP_TEXT]`，注释分别是
「外部 URL 会被拒」和「本端暂未实现」——入站能收图、出站发不了图，插件以为能发，
实际发一条失败一条。

- **KOOK**：按官方要求先 `POST /api/v3/asset/create` 上传再引用 url；外部地址先代下再
  重传，已是 kookapp.cn / kaiheila.cn 的地址直接发。入站补 `type=8` 语音。
- **Matrix**：先 `/_matrix/media/v3/upload` 再发 `m.image` / `m.file`；入站把 `mxc://`
  本地化到 `data/matrix_media/`。原因是 spec 已把 `/_matrix/media/v3/download` 标为
  deprecated，替代端点必须带 `Authorization` 头、token 不许拼 URL——旧写法在服务器
  冻结匿名访问后拿到的是 401 的空 URL，整条消息只是「没图」，不报错。
- **Telegram**：`parse_mode`、4096/1024 切分、编辑/删除/表情回应、论坛话题 `thread_id`、
  `callback_query` 订阅。
- **Discord**：2000 上限按 UTF-16 码元切分；空消息本地拦截；心跳未 ACK 用 4000 关闭
  （zombied 连接不会自己断）。附件超过 10 个、或全部读取失败时留下明确日志——原先
  静默截断，调用方只看到「发送成功」。
- **QQ 官方**：`msg_type=8` 图文卡片，按官方 MessageCard 约束做本地预检（tuwen 必须含
  `title` 1-50 / `description` 1-200 / `url` 与 `pic_url` 为 https 且 ≤1024）。官方超限
  只回 `304061 消息内容无效`，不说是哪个字段，本地拦才能报出具体字段和当前长度。
  另补 @全体、提示键盘、分片上传。

顺带修掉会让请求白跑或把内存打穿的地方：`kook` / `matrix` 的 base64 分支不设上限就直接
`b64decode`（峰值是原文加明文两份）；`kook` 流式下载没有 `with`，连接泄漏；`telegram`
的 caption 从来不读 `parse_mode`（用户开了 HTML，正文是 HTML、媒体 caption 仍是纯文本，
解析失败还会让整条消息 400），现在跟随配置并在失败时与正文一样降级重发一次。

### 图片渲染：产物移出源码，改为运行时获取

字体子集（约 8MB）与 7 个平台的原生扩展（约 7MB）不再进仓库，也不进 wheel——多平台
二进制里任何一台机器只会用到其中一个。产物托管在独立仓库
`kuangxing6367/zcbot-render-assets`，插件按 `asset_url` 取到 `data/` 下。

默认 `backend: pil`，**不需要这份资源**；只有切到 `rust` 才需要。`asset_consent` 默认
`ask`：只打日志说清楚「多少流量、放哪里」，一个字节都不下。

资源层同时修掉四个会让渲染整体失效的问题：

- `os.makedirs` 在 `try` 之外：目录不可创建时 `PermissionError` 直冒到插件初始化，
  连带本来能跑的 PIL 一起挂掉。改为先探测，全部转成状态字段。
- sha256 校验不存在：注释与 README 都写「校验后落盘」，实际只读了 manifest 里的
  `version`。补 `_verify_extracted()`，逐条比对 size 与 sha256。
- 正式目录只读被报成「下载失败」：`rmtree(ignore_errors=True)` 吞掉删除失败，接着
  `os.replace` 必抛。改为 rename / 删后 rename / 就地覆盖三态。
- 就地覆盖留下混合态：`manifest.json` 先写成功、字体写失败，出现「版本号 2.0、
  内容是 1.0」，字表与 ttf 还可能对不上。改为其余文件全部成功才写 manifest。

### WebUI：官方插件配置终于能在面板里改

`/api/config/yaml` 读的是 `config.yaml` 原始文件，而官方插件配置在 `core_plugins.yaml`，
启动时才合并进主配置的同名段；`/api/config/yaml/<section>` 又有一份写死的段白名单。
两者叠加的结果是：面板上看不到 `image_renderer` 这类段，只能去手改 yaml。

- 读：把合并后的官方插件段补进返回（`enabled` 除外——它的唯一权威是
  `core_plugins.yaml`，混进 `config.yaml` 就是第二处真相）。
- 写：白名单动态加入各官方插件的配置段；白名单外的段仍然拒绝。
- 设置页新增「图片渲染」分组，可行内改 `backend` / `asset_consent` / `asset_url` /
  `asset_timeout`，保存后提示需重启。
- 新增 `GET /api/plugins/image_renderer/status`。打开后台时顶部横幅按它决定是否提示
  资源缺失；横幅**纯读**，不会因为「打开了管理页」就花掉用户 9MB 流量。

顺带修掉一个静默 bug：`image_renderer` 原先用 `ctx.get_config()` 读配置，而那个接口
查的是 `plugin_configs` 表——只服务 userspace 插件（值由 WebUI 写表），核心插件从没
往表里写过任何行。于是 `backend` 永远读成默认值 `pil`，`core_plugins.yaml` 里写什么
都不生效，且不报错。改为读合并后的主配置段。

### 数据库：方言翻译丢索引、丢函数、占位符被误改

- **索引丢失**：SQLite 的索引名是库内全局唯一，`init.sql` 里跨表重名——37 个索引只建出
  21 个，其余静默失败。改为索引名加表名前缀，并拆出 `extract_plain_indexes()` /
  `build_sqlite_index_ddl()` 供两个初始化入口复用。实测 20 万行：`is_blacklist` 过滤
  99.34 → 0.74ms（135×），`last_active_at` 134.49 → 9.71ms（13.9×）。
- **函数翻译只覆盖 DDL/DML 分支**：`SELECT` 里的 `NOW()` / `IFNULL()` 原样发给 SQLite
  直接语法错。改为先预判再进分支。
- **嵌套 IF 只翻译一层**：改成迭代到稳定。
- **占位符转换不认引号**：`strftime('%s','now')` 被翻成 `strftime('?','now')`。
- **MySQL 5.5**：补 767 字节前缀索引上限（支持 `UNIQUE KEY`、按累计字节预算、排除 ENUM）。
- **连接池 ping 改为按需**：`DBUtils` 的 `ping(reconnect=True)` 与 `pymysql` 同名参数均已
  弃用，且每次取连接都 ping 是白付一次往返。改为按 `ping_interval` 判空闲，事务路径强制
  探活。实测 195.0 → 238.9 ops/s（+22.5%）。

新增 `tests/db_conformance/`：requirements 驱动的一致性套件，`--db` 选择
`sqlite` / `mysql`，同一组用例跑两个后端。

### 路由器：两个能让整批命令消失的输入

- 路由签名改为 `(DB 签名, 已加载插件名元组)`。原先在「只装了核心插件」的部署里签名恒等
  不变，路由表构建被跳过——命令列表永久为空，且没有任何报错。
- 新增 `_norm_priority()`：字符串 `priority` 会让 `sorted()` 抛 `TypeError` 打死整张表；
  `priority=0` 会被 `or 50` 吞成 50。

### 打包：wheel 里塞着 10MB 用不上的二进制

`MANIFEST.in` 的 `recursive-include core_plugins` 带了 `*.so` / `*.pyd` / `*.ttf` /
`*.otf`，而 `include-package-data = true` 时 MANIFEST.in 直接决定 wheel 内容——
`package-data` 里只写 `.gitkeep` 根本不管用。结果是资源已经移出源码，pip 安装却仍要
多下 10MB，且 7 个平台里只有一个能用。同时 `recursive-include` 的 `*.json` / `*.txt`
把 cargo 的 `.fingerprint` 与 `build` 中间产物一起捞了进来，比代码还大。

wheel：10M → 1.8M，文件数 592 → 329。

### CI：两条长期红的灯，互相盖着

1. `test_smoke.py` 断言图片上传带的是 multipart `files`，而 `_upload_group_image` 早已改走
   `file_data(base64)`（官方请求体只列 `file_data` / `url` / `upload_id`）。改断言。
2. pytest 步骤只 `pip install pytest`，缺 `pytest-asyncio`，`pyproject.toml` 里的
   `asyncio_mode=auto` 因此不生效，7 个裸 async 用例全部判失败。

第 1 个先挂会让第 2 个整步被 skip，所以此前只看得到一个。

---

## [1.20.2] - 2026-11-06

> 主题：**许可证合规**。代码没动，只把协议文件按识别标准重新组织。
> v1.20.0/1.20.1 的 `LICENSE` 把 MIT 与 Apache-2.0 两份全文混在一个文件里，
> 还盖了 3 行中文说明 —— GitHub 与各类扫描器一律识别成 `NOASSERTION`
>（查 `repos/.../license` 返回 `spdx_id: "NOASSERTION"`），企业法务流程里
> 等同于「许可不明」。

### 拆分成识别器认识的结构

- `LICENSE` 只放 MIT 全文（逐字对齐 SPDX 模板，仅替换版权行）→ GitHub 现在
  能正确识别为 MIT，不再显示 Other。
- 新增 `LICENSE-APACHE`：直接从 apache.org 的 `LICENSE-2.0.txt` 取原文，
  只替换 APPENDIX 里的版权占位符。原文件有一处 4(a) 条款的**换行位置**与
  官方不同（措辞相同但被重排过），Apache 要求原样保留，已一并纠正。
- 版权人统一为 `zgric`（原来是 `Copyright (c) 2026 ZCBOT` —— ZCBOT 是项目名，
  不是法律主体，这类写法在合规审查里会被打回）。

### 第三方组件声明

WebUI 打包了 element-plus 2.14.4 / @element-plus/icons-vue 2.3.2 / vue 3.5.41 /
vue-router 4.6.4，全是 MIT。但构建时 Terser `format.comments: false` 加混淆器
把版权注释删得干干净净（产物 js 里 `license` 出现次数为 **0**），而 MIT 明确
要求副本保留版权声明。

- 新增 `THIRD-PARTY-NOTICES.md`（仓库根，给人看）与
  `core_plugins/webui/web/THIRD-PARTY-NOTICES.txt`（随产物分发的那份）。
- 产物 `index.html` 顶部加注释指向它，避免只拷 `js/` 就漏掉声明。
- `tools/check_webui_artifact.py` 增加门禁：产物目录必须存在 NOTICES 且
  声明了 element-plus / vue / vue-router，否则 CI 失败 —— 防止重建时又丢。
- 构建侧加了 `licensingPlugin`（closeBundle 阶段，跑在压缩与混淆之后），
  下次重建会自动生成同一份文件。

### 打包元数据

- `pyproject.toml` 补 `license-files = ["LICENSE", "LICENSE-APACHE"]`，
  `authors` 改为 `zgric`。实测 `pip wheel` 后：dist-info 里两份协议齐全，
  `License-Expression: MIT OR Apache-2.0`、两条 `License-File`，
  `core_plugins/webui/web/THIRD-PARTY-NOTICES.txt` 也在包里。
- `MANIFEST.in` 显式 include 三份许可文件，sdist 不会再漏 Apache 那份。

### 顺带纠正一处错误表述

`framework/terminal/panel.py` 的模块文档原写「zcbot 为 MIT/Apache-2.0 双协议，
与 GPL 不兼容」，这话不准确：MIT 分支与 GPLv2/GPLv3 都兼容，只有 Apache-2.0
分支因专利条款与 GPLv2 不兼容（与 GPLv3 兼容）。已改为准确表述，并注明需要
并入 GPLv2 项目的下游应选 MIT 分支。

## [1.20.1] - 2026-11-05

> 主题：**把 v1.20.0 留下的洞补上**。这一版没有新功能，全部是一次针对源码的
> 逐层审查结果：发版链路漏文件导致新 clone 白屏、IIPC 双进程零鉴权、
> 会话令牌明文落库、几处「数据会丢但日志说成功」的缓冲与迁移问题，以及
> 一条一直在拖启动和命令分发的同步查库路径。

### 先说最要紧的：v1.20.0 的 WebUI 装不上

- v1.20.0 的 `core_plugins/webui/web/index.html` 引用了
  `js/element-plus.C4PxGsnR.js`（1.05 MB），但这个文件**没有进仓库**。
  后果不是「少个按钮」，而是 Vue 应用整体起不来 —— 新 clone / 新部署打开
  面板就是白屏，而旧门禁只检查「index.html 存在 + js 文件数 > 0」，查不出来。
- 现在产物已重建并按最小 diff 提交（只补新 chunk、删掉引用已失效的旧孤儿），
  同时新增 `tools/check_webui_artifact.py`：校验 `index.html` 与每个 chunk 里
  所有 `./xxx.js` 引用都真实存在，并拒绝没有任何引用的旧版大文件。
  CI 的 `webui-build` 作业改为跑这个脚本。

### 安全：IIPC 双进程是零鉴权的

- `launcher.py`、`py_sdk/pluginhost.py`、各工具脚本里的 `dev-auth` 是**纯装饰常量**：
  协议帧里根本没有 auth 字段，Python 客户端收到 `auth` 参数从未发送，Go 内核
  只把它存进字段、从不比较。也就是说，任何能连上 `127.0.0.1:18730` 的进程
  都可以直接发 `db.query` / `db.exec` / `debug.inject`。
- 现在改为：启动器生成 256 bit 随机 token（可用 `ZCBOT_IIPC_AUTH` 显式指定，
  不存在固定回退串），经**环境变量**传给子进程（不再走命令行，避免出现在进程列表里）；
  连接首帧必须 `{"t":"auth","auth":"<token>"}`，服务端用常量时间比较，失败回
  `unauthorized` 并断开；内核缺 token 时**拒绝启动**而不是零鉴权运行。
- **协议变更，旧二进制不兼容**：Go 内核与 Rust 协议端需要重新编译
  （`go build -o zcbot_kernel .` / `cargo build --release`）。顺带修了
  `build_kernel()` / `build_rust()` 的「exe 存在就直接用」—— 现在会比较二进制
  与源码 mtime，源码更新就重编；编译不了时退回旧二进制但明确告警。
  没有工具链、只用预编译二进制的部署可设 `ZCBOT_IIPC_SKIP_BUILD=1`。

### 安全：会话令牌明文落库

- 登录时 `token_hex(1024)` 生成的 2048 字符令牌**明文**写进 `admin_users.token`，
  校验时也按明文 `WHERE token = ?` 查。库文件泄露 = 管理员会话直接可冒用。
- 改为库里只存 `sha256(token)`（新增 `token_hash` 列 + 索引），校验用
  `secrets.compare_digest`。**升级后所有存量会话会失效，需要重新登录一次** ——
  这是刻意的取舍：不把老明文令牌静默升级成可用会话。
- 顺带补上「改密不踢下线」：`change_password` 现在会清掉 `token_hash`。
- `/metrics` 未配 `observability.metrics_token` 时原本是**无条件放行**，
  等于把运行指标暴露给任何能访问 API 端口的人。现在只有 loopback 可访问，
  非本机一律 404；配了令牌则按 Bearer 校验。`/healthz`、`/readyz` 不受影响。
  注意：如果前面有同机反向代理，后端看到的 `remote_addr` 会是 127.0.0.1，
  这类部署请显式配 `metrics_token`。

### 性能：命令分发与启动期的同步查库

- 命令匹配时要判角色与权限，这条路径原先会在**事件循环线程里同步查库**
  （角色最多 2 次、权限最多 3 次往返）。DB 一慢，整个机器人的消息处理全部停住。
- 现在路由分发前先把 `(role, PermissionSet)` 解析好（缓存 TTL 5s、容量 4096、
  LRU 淘汰、线程安全），热路径零 DB、零线程切换；缓存 miss 时用
  `asyncio.to_thread`，不在循环里查。权限变更后经 `/api/perm/*` 的写请求
  统一失效（`after_request` 兜住，新增写接口自动覆盖），不用等 TTL。
- 启动期 DB 初始化：稳态重启的元数据探测从 **61 次往返降到 11 次**。
  原先 40 次逐表 `sqlite_master` 查询 + 11 次逐列 `PRAGMA table_info`，
  现在合并成 1 次 `sqlite_master LEFT JOIN pragma_table_info`；保留的 10 次是
  真实建表 DDL 与迁移账本读取（合并 DDL 会破坏幂等与双方言兼容）。
  迁移账本语义（`schema_migrations` 当前态 / `schema_migration_runs` 只追加 /
  `SELECT *` + 缺列取默认）未改动。

### 数据正确性：会丢数据但日志说成功的地方

- **L4 写缓冲**：`wait_for` 超时会取消 future 但底层线程已 commit，上层误判失败
  回落 L3 → 同一批事件被处理两次；`wait_drained()` 取消 flush 会把整批丢掉；
  `_l4_bytes` 只增不减；`close()` 不落盘。现在用 `asyncio.shield` + 拿到确定结果
  再判定、flush 防重入、直取时回扣水位、关库前同步把残留写进 L2。
- **出站超时被重试**：`TimeoutError` 意味着「可能已经送达」，重试就是重复发消息。
  闸门现在对超时不重试，其余异常保留原语义。
- **插件上传失败先删旧版**：旧实现解压/加载失败时已经把备份删了。现在只有加载
  成功才删备份，失败回滚旧版本并在返回消息里说明旧版是否恢复。
- **DB 降级标志恒为 False**：故障降级的 `SqlSimEngine` 实例没打 `degraded = True`，
  于是 `/readyz`、Prometheus、面板一路显示正常。现在降级实例会如实上报。
- **迁移账本写失败仍报「已应用」**：现在记为失败并中断，不再假成功。
- **MySQL ≤5.6 初始化**：`sql/init_mysql55.sql` 之前是死文件（从来没被加载），
  且 `_adapt_ddl_for_mysql` 会把多个 `DATETIME DEFAULT CURRENT_TIMESTAMP`
  改写成多个 TIMESTAMP 自动列 —— MySQL 5.5/5.6 一张表只允许一个，建表全失败。
  现在 ≤5.6 直接加载专用 SQL 且不做改写；该文件补了 `admin_users.token`、
  `token_created_at` 与 `ip_blacklist` 表。

### 并发：限流会污染去重、跨接入端串桶

- 入站去重与限流的判定原本分属两个 `with self._lock` 块，中间释放锁；更关键的
  是**被限流丢弃的消息会提前占用 `(bot, message_id)` 去重槽位**，重放时被判成
  重复静默吞掉。现在去重检查、限流判定、去重登记在同一把锁内完成，且只有
  「最终放行」的事件才写去重表。
- 限流桶 key 补上接入端：`(bot, group, gid)` / `(bot, user, uid)`。原先多个
  接入端共用同一个桶，一个端刷屏会把另一个端在同一群上的配额吃光。`global`
  桶保持跨端共享，它本来就是「整机上限」。
- 出站节流把「预留槽位」计入滑动窗口：并发请求同时通过配额检查再一起发送，
  瞬时必然超配额。现在预约发送时刻也进队列，后来的请求按预约时间排队。

### 契约：五个接入端的错误码

- 文档里 retcode 是 `0` 成功 / `-1` 业务失败或不确定 / `-2` 超时（可能已送达，
  不重试）/ `-3` 本地通道或网络异常（可重试）/ `-4` 限流 / `-10` 该端不支持此动作。
  实际上各端把参数缺失、前置条件不满足大量标成 `-2`（等于谎报「可能已送达」），
  Telegram 把网络异常标成 `-4`（会被当成限流）。
- 逐端改正：参数错 → `-1`，网络/连接异常 → `-3`，真超时仍 `-2`，限流仍 `-4`。
  `-10` 有文档定义，保留。**QQ 官方主动消息在无 `msg_seq`/`msg_id` 时不做内部重试**
  这条语义没动 —— 它正是为了避免重复投递。

### 资源回收：插件缓存一直涨

- 插件有两类目录：`plugins_dat/<name>/` 是用户数据（卸载也保留），
  `cache/<name>/` 是可丢弃中间产物。后者原先没人清。
- 现在 `framework/plugin_paths.py` 收敛了「插件名 → 目录名」的唯一映射
  （官方插件 `core:` 前缀 → `_`，与 `ctx.get_data_dir()` 一致，并做目录穿越加固），
  卸载 / 加载失败终态 / 从未加载成功早退三条路径统一在
  `_release_plugin_api_routes` 里清缓存（**绝不碰 plugins_dat**）。
- 另接一个内置定时任务按 TTL 扫全量（`scheduler.cache_ttl_hours`，默认 24h）。
  `<= 0` 表示**禁用**，不会退化成「无差别全删」；`cache_min_age_seconds`
  作为保底，避免删掉正在写的文件。
- 内置任务的 job_id 用 `__builtin__:` 前缀，孤儿自检必须跳过它 —— 否则每次
  心跳（60s）都会把框架自己的任务当「插件已不存在的孤儿」删掉，日志还只说
  「移除调度器孤儿任务」。这条是在真实部署上启动 60 秒后发现的，已补回归测试。

### 其它修正

- 文件浏览器读写权限倒挂：写要 super、读却谁都能读，`config.yaml`、
  `core_plugins.yaml`、`.env`、密钥类文件现在读也要 super。
- Web 终端 `shell <命令>` 原本任何 admin 都能执行（等于宿主 RCE），
  `/api/restart` 也是普通 admin 可调。现在都需要 super。
- CORS 未配白名单时会反射任意 `Origin`，配合 `Allow-Credentials: true` 等于
  任意站点可带 Cookie 打 API。现在不再反射。
- SQLite 相对路径按进程 cwd 解析（systemd / 计划任务下 cwd 不同就换了个库）；
  实例锁创建失败被当成「已有实例」直接 `sys.exit(1)`。两处都已修正。
- 同步 `call()` 在 async handler 内会自锁 15 秒（`run_coroutine_threadsafe`
  等自己所在的循环），现在立即返回 `-1` 并提示改用 `await acall()`。
- IIPC 启动器不再注入开发机的 `127.0.0.1:7892` 代理（服务器上没有这个代理，
  首次 `cargo build` 拉 crate 会全部走死代理）。
- Matrix 的 `since` 改成「先把 timeline 事件分发完再落盘」，进程被杀时重启
  会重放而不是丢批；未配 `user_id` 时自动调 `whoami` 补全（缺它会自激刷屏）。
- 文档与脚手架：`快速开始` 的端口权威改为 `config.yaml → web.port`、默认插件
  补上 `help`；CLI 模板去掉不存在的 `ctx.set_config`、`listen_port` 改 6830、
  `plugin.dat_dir` 修正；`插件开发.md` 标注 `middleware/transform` 只属于 `--iipc`。
- CI：把 11 个脚本式自测从「塞进 pytest 收集」改成 `python tests/xxx.py` 真跑
  （旧的跑法里 98 个 `test_*` 函数零 assert、失败也一律 PASS），并补上漏掉的
  测试白名单。**真跑立刻抓出两个被假绿掩盖的失败**：`test_security_hardening`
  的源码断言因本轮 `files.py` 改动过时、`test_self_heal` 假设「心跳只增量注册」
  而实现早已默认 `hot_reload=true`。

## [1.20.0] - 2026-11-04

> 主题：**让接入端跟官方现况对齐**。这一版不是加功能，是先纠正几处
> 「代码按老文档写的、平台早就改了」的问题，再补两个一直缺的接入端。

### QQ 官方：域名迁移写反了（影响每一次 API 调用）

- 官方 changelog `20260810` 写得很清楚：所有接口调用域名统一为 `api.bot.qq.com`，
  独立的沙箱域名 `sandbox.api.sgroup.qq.com` 已废弃。而接入端把 `api.sgroup.qq.com`
  当主域名、`api.bot.qq.com` 当「迁移窗口期回退」，注释还写着「旧域名 api.bot.qq.com」。
  结果就是每条请求先撞一次已下线的域名，失败后再换域名重试，**发送延迟直接翻倍**。
- 现在默认只走 `api.bot.qq.com`，`hosts` 只有一个。只有用户把 `api_base` 显式配成
  旧域名时才有回退，并在启动时告警提示清掉配置。
- 默认配置模板里的 `api_base` 同步改正（之前这里也还是旧域名，等于改了一半）。
- 取令牌地址补上另一入口：官方两处文档写法不一致（`api.bot.qq.com` 与 `bots.qq.com`），
  社区实现两者都有在用，本端两个都试。

### QQ 官方：业务错误判定不能依赖 message

- 官方原文：「失败时 HTTP 返回码仍为 200，请优先依据 code 判断是否成功」
  「message 仅用于人工排查，内容可能随时调整」。
- 原实现要求 `code` 与 `body['message']` 同时存在才算失败。官方哪天调整文案
  （改字段名、去掉 message），失败会被当成成功返回空 dict，插件看到的是
  「发了但没发出去」，日志里一片正常。现在只看业务码。

### Telegram：429 与 409

- 新增 `TelegramApiError`，解析 `ok=false` 的标准结构，读 `parameters.retry_after`
  （缺失时从 `description` 里解析 `retry after N`）。
- 轮询侧遇到 429 按官方值休眠；出站返回 `retcode=-4 / throttled / retry_after`，
  与出站闸门语义一致。
- 409 单独处理：`getUpdates` 与 webhook 互斥，配过 webhook 的 bot 会一直 409。
  这不是网络抖动，重试一万次也一样，所以只完整提示一次并给出
  `deleteWebhook` 的修法，之后长休眠。
- 401 明确指出 token 无效。

### Discord：心跳 jitter、致命关闭码、Intent 告警

- 首次心跳改为 `heartbeat_interval × random(0,1)`。原来固定用一半间隔，
  正是官方要求 jitter 想避免的「所有 bot 同一毫秒心跳/重连」。
- 新增关闭码处理：
  - `4004 / 4010 / 4011 / 4012 / 4013 / 4014` 标记为致命并**停止重连**。
    这类是配置或权限错误，继续重试只会烧 IDENTIFY 配额（官方限 1000 次/24h，
    超了直接重置 bot token），日志里只会看到反复断开。
  - `1000 / 1001` 按官方语义作废 session，下次重新 IDENTIFY，不拿旧 session 去 RESUME。
- `MESSAGE_CONTENT` 未订阅时启动告警。官方明确说过：缺失的特权 Intent 不会报错，
  事件照常到达、被门控的字段为空——现象是「收到了消息但内容是空串」，
  极易被当成解析 bug 排查半天。
- REST 失败不再把 HTTP 状态码直接当 `retcode` 透给插件（429 会被当成未知错误）：
  429 → `-4 + retry_after`，401/403 → 明确提示 token 或权限问题，其余 → `-1`。

### OneBot：过滤自身消息回显

- NapCat / go-cqhttp / Lagrange 都能把机器人自己发出的消息上报（`post_type=message_sent`，
  或 `message` 且 `user_id == self_id`）。不过滤会形成自激：自己发的消息进管线 →
  命中命令前缀 → 再发一条 → 无限循环，而日志完全正常。
- 现在这两类直接丢弃并打 debug。

### 新增接入端：KOOK

- `core_plugins/kook`：Gateway WebSocket 收事件 + REST v3 发消息。
- 协议细节按官方文档实现：`/api/v3/gateway/index?compress=0` 取网关；
  6 条信令（0 事件 / 1 HELLO / 2 PING / 3 PONG / 4 RESUME / 5 RECONNECT / 6 RESUME ACK）；
  心跳 `30s ± 5s`，6 秒无 PONG 判超时；断线用 `resume=1&sn=&session_id=` 补发离线消息。
- `sn` 重复或乱序直接丢弃（官方要求）——不丢会让同一条命令执行两次，对签到/扣费类是灾难。
- `RECONNECT` 时清空 `sn` 与会话重新取网关，否则消息会错乱。
- 过滤 `extra.author.bot` 的消息：官方专门警告过两个机器人互相回复会触发封禁。
- HELLO 的 40101/40102/40103 直接停止重连（token 问题）。
- 能力边界写清楚：频道文本、引用、撤回支持；**图片/视频发送不支持**
  （KOOK 要求资源由本机器人上传后引用，外部 URL 会被拒），主动私聊需要 `chat_code`，
  都返回明确的「不支持」而不是假装成功。

### 新增接入端：Matrix

- `core_plugins/matrix`：Client-Server API v3，`/sync` 长轮询收、
  `PUT .../send/m.room.message/{txnId}` 发。不依赖 matrix-nio。
- `since` 落盘（`data/matrix_sync.json`）：不放的话每次重启都是「首次 sync」，
  会把最近历史消息重投一遍。记录带 homeserver + user_id 指纹，换账号不沿用旧游标
  （沿用会静默跳过一段消息，比重复更危险）。
- `txnId` 作幂等键，一次调用只生成一次；重试复用同一个值，换掉就是真的发第二条。
- `M_LIMIT_EXCEEDED` 按 `retry_after_ms` 等待；`M_UNKNOWN_TOKEN` 等凭证错误停止轮询。
- 支持自建 homeserver（只换 `homeserver`，协议一致）。
- 能力边界：文本收发、媒体入站；媒体出站需先上传，暂未实现；加密房间不在范围。

### 文档

- `docs/指南/对接IM平台.md`：适配器表补 kook / matrix；删除「暂无统一 429 退避」
  （已不成立）；新增「失败是怎么报给你的」一节，给出 `retcode` 对照表与五个易踩点。
- `docs/进阶/可靠性与限流.md`：新增「平台侧的限流也要按它说的等」，列出四家的
  限流信号与处理，以及凭证错误不重试的原因。
- `docs/接口/高级/协议适配器.md`、`docs/进阶/扩展清单.md`、`docs/指南/安装.md`：补新接入端。

### 测试

- 新增 `tests/test_adapter_error_handling.py`（25 组）：四家的 429/403/409、
  业务码无 message 仍判失败、Discord 六个致命关闭码与 1000/1001 的 session 作废、
  QQ 默认单域名、Matrix 限流与致命 token。
- 新增 `tests/test_new_adapters.py`（16 组）：KOOK/Matrix 事件归一化、bot 与自身过滤、
  `sn` 去重、`since` 持久化与换账号隔离、OneBot 自身消息过滤、KOOK 私聊不支持明确报错。
- 全量 pytest **633 passed**；脚本式回归 4 个全过；真实 `main.py` 启动确认两个新接入端
  在缺凭证时正确跳过连接而不报错。

## [1.19.0] - 2026-11-04

> 主题：**卡住的时候得有人吭声**——定时任务和命令 handler 是两套执行标准，同步任务一直跑在事件循环上；阻塞发生了则完全没有日志。

### 定时任务执行模型统一

- **修正** `framework/scheduler.py` 的文档与实现不符：模块头写着「普通 def 转线程执行，不阻塞事件循环」，实际是直接调用——一个没写 timeout 的 `requests.get` 就能让整个机器人停摆。现在同步 handler 走专用线程池（`scheduler.workers`，默认 4），与命令 handler 的处理方式对齐。
- 线程池与命令 handler 用的默认线程池**互相隔离**：插件的慢任务不会挤占消息处理能力。
- 新增 `scheduler.task_timeout`（默认 300s，0=不限）与 `scheduler.slow_task_threshold`（默认 10s，0=不告警）。超时记为 `timeout` 并打 error；耗时超阈值打 warning。
- **诚实说明**：Python 无法强杀线程，超时只是「不再等待」，线程仍在后台跑完。文档里写清了这一点，避免被误当成资源回收。
- 内置任务（权限过期清理）同样改走线程池：它要扫全表，库大时同步执行会把整条事件管线冻住。

### 定时任务时区

- 新增 `scheduler.timezone`：`local`（默认，保持历史行为）/ `utc` / IANA 名（`Asia/Shanghai`）。
- **这是「任务时间差 8 小时」的唯一来源**：容器镜像的 `TZ` 通常是 UTC，写 `0 9 * * *` 会在北京时间 17:00 执行，而日志里的时间戳看不出任何异常。`CronTrigger` 现在显式带上解析后的时区。
- 启动日志打印实际生效时区**并带偏移**：`定时任务时区: 本地时区 (UTC+08:00)`。只写「本地时区」等于没说，部署到 UTC 机器上照样看不出来。
- 时区名解析失败不拒绝启动：告警并退回本地时区。Windows 缺 `tzdata` 时给出可执行的修法。

### 任务运行记录

- 每次执行记录 `last_run` / `last_duration_ms` / `last_status`（`ok`/`error`/`timeout`）/ `last_error` / `runs`，`get_jobs()` 与 `/api/runtime/stats` 的 `scheduler` 字段同源。
- cron 表达式改为严格 5 段校验：段数不对直接报明确错误（此前会走到 `CronTrigger()` 无参构造，报一个看不出原因的错）。
- 任务移除时一并清掉执行记录，面板不留已卸载插件的僵尸条目。

### 事件循环卡顿看门狗（新增）

- `framework/core/loop_watchdog.py`：守护线程周期性投递回调到事件循环，测量「从投递到执行」的延迟。超阈值打 warning，**并抓取事件循环线程的栈**，直接指到卡住的那一行。
- 这是「阻塞」唯一能在事后被定位的方式：asyncio 单线程，任何同步阻塞都会让所有插件、所有接入端、定时任务一起停，而日志里没有异常、没有报错，只有消息不回。
- 三处实现细节值得记：①**抓栈必须在卡顿进行中**，等恢复再抓只能拿到空闲栈（实测抓到 proactor 的 `_loop_self_reading.recv`，对定位毫无价值）；②**启动期暂停告警**——装依赖、建表、同步加载全部插件必然阻塞循环，每次都报警会让真卡顿被当成背景噪音，启动期延迟单独记账；③噪声帧按 **basename 精确匹配**过滤，用子串匹配会把 `test_loop_watchdog.py`、`my_events.py` 这类业务文件当成噪声滤掉，栈直接变空。
- 配置段 `framework.loop_watchdog.{enabled,interval,threshold,stall_wait}`，默认开（开销可忽略）。
- 接入 `/metrics`：`zcbot_event_loop_lag_seconds` / `_lag_max_seconds` / `_blocked` / `_stalls_total` / `_stall_seconds_total`。偶发一次性抖动与持续性阻塞处置完全不同，所以次数、累计时长、峰值三个维度都要有。
- Web「运行状态」页新增「定时任务与事件循环」卡片。

### 配置

- 默认配置模板新增 `scheduler` 段与 `framework.loop_watchdog` 段；`_YAML_SECTIONS` 白名单补 `scheduler`，否则面板读写不到。
- 配置数值改用 `float` 而非 `int`：超时常写成 `0.5`，`int()` 会把 `0.5` 截成 `0`，而 `0` 在这里的语义是「不限」——等于配置静默失效。

### 测试与文档

- 新增 `tests/test_scheduler_reliability.py`（17 组）：时区解析与回退、**同步 handler 不在事件循环线程执行**（钉住本次修复）、协程 handler 不被丢线程池、超时状态与不阻塞返回、小数阈值不被截断、慢任务计数、异常计数、非法 cron 拒注册、任务记录清理、配置兜底。
- 新增 `tests/test_loop_watchdog.py`（7 组）：空闲不误报、阻塞能测到、栈指向阻塞行、启动期屏蔽且单独记账、stop 幂等可重启。
- 文档：`docs/进阶/定时任务.md` 重写时区/执行模型/排查三节；`docs/进阶/可观测性与运维.md` 新增看门狗章节与指标表；`docs/指南/配置系统.md` 补两段配置与「只补顶层段」的边界说明。

## [1.18.0] - 2026-11-04

> 主题：**卸载要把东西收干净**——插件注册的 Web 路由以前没人回收，热重载会留下"还在跑旧代码"的接口。

### 插件 API 路由的归属与回收

- `framework/api/registry.py`：路由登记项新增 `plugin` 归属字段，`add_route` / `register_route` 接受 `plugin=`；新增 `unregister_plugin(name)`。
- 回收做两件事：摘登记项（`mount_all` 不再挂回来）+ 把已挂的 Flask 视图换成 **404 占位**。Flask 不支持运行时删规则（规则与 endpoint 绑在 matcher 里），只能换实现——不换的话，新版本删掉/改名的接口会继续用旧模块的闭包处理请求，既跑旧代码，也把旧模块钉在内存里回收不掉。
- 同路径被另一个插件接管时所有权转移：原插件卸载不会连带删掉新主人的路由。框架内部路由（`plugin=None`）不参与任何插件回收。
- **修正** 双进程宿主模式下的规则叠加：核心侧原先每次 `route.register` 都 `add_url_rule` 一条新规则，重载 N 次后 url_map 上 N 条同路径规则，第一条旧规则先注册先匹配 → 永久钉死在旧实现上。现在核心侧统一走 `registry.register_route`（按 path 派生的 endpoint 稳定，只换视图），宿主侧同路径复用 `route_id`。
- 新增 RPC `route.unregister`：宿主卸载插件时通知核心进程一并摘除 stub 路由；核心侧新增 `_on_route_unregister`。

### 加载失败也要收

- `PluginLifecycleMixin._release_plugin_api_routes()`：卸载路径与**加载失败终态**（spec 为空 / 缺 `register` / `register` 不可调用 / 导入异常 / 其它异常）都调用它，收掉 `register` 中途抛错前已登记的半初始化路由。
- `unload_plugin()` 的「从未加载成功」早退分支同样回收（禁用/卸载语义下不能留下活着的接口）。

### 测试修复：一个会掩盖真问题的假阳性

- `tests/test_db_resilience.py::test_persist_degraded_flag_warns_once_and_recovers` 原来依赖 `caplog`。在「真实 Framework 接线」类用例跑过之后，框架自带的 logging 重配会挡掉 caplog 的传播链，于是全量运行必失败、单独运行必过——这是个长期存在的假阴性来源（也说明依赖 pytest 捕获实现来断言日志并不可靠）。改为直接挂 handler 到 `zcbot` logger 收记录，只测被测行为本身。

### 测试与文档

- 新增 `tests/test_plugin_api_unmount.py`（8 组）：卸载后 404 而非旧闭包、重载同路径只留一条规则、同路径所有权转移、框架内部路由不被误删、Web 未启用时也摘登记项、远程注册器复用 `route_id` + `route.unregister` 通知、核心侧按 path 去重、loader 卸载真的去回收。
- `tests/test_task2_arch_split.py` 的 RPC 面白名单补 `route.unregister`。
- 全量 pytest 563 passed（另 4 个脚本式用例单独跑全过），boot_check 通过。
- 文档：`docs/进阶/插件加载机制.md` 新增「卸载时怎么回收插件注册的 Web 路由」（含各场景行为表与排错口径，原 5.3 顺延为 5.4，交叉引用同步）；`docs/指南/编写插件.md` 补 `@api` 生命周期说明，并修正 `@api` 与 `ctx.register_api` 的签名对照（原表漏了 `path`）。

## [1.17.0] - 2026-11-04

> 主题：**把"断线之后怎么办"补齐**——1.15/1.16 管住了事件进出，这一版管连接本身断了怎么重连。

### 断线重连：指数退避 + 抖动

- 新增 `framework/messaging/backoff.py` 的 `Backoff`：起始间隔逐次翻倍到上限（默认 60s），叠加 0~25% 抖动。四个接入端统一接入。
- **修正** `ws_client` / `discord` 的固定 5 秒重连：故障期会按固定节奏猛敲对端，多实例同频还会周期性一起撞上去。
- **修正** `qq_official` 原有的"连上就清零"：`connect_once` 一旦成功返回就把失败计数清零，导致「连上即被踢」（token 失效、被限流、协议不匹配）退化成高频重连循环——日志里全是"已连接"然后立刻断开。改为**连接存活满 30 秒才清零**。
- `telegram` 长轮询的固定 3 秒错误间隔同样改为退避（正常返回即算成功，只有连续失败才增长）。
- `ws_client` 暴露 `reconnect_max_interval` / `reconnect_jitter`；其余接入端的 `reconnect_interval` 语义从「固定间隔」变为「初始退避」，配置项名不变（老配置照常生效）。

### 断线期积压：不再"假装失败"

- `ws_client._send_json` 返回值从 bool 改为 `'ok' | 'queued' | 'failed'`。未连接时消息进有界队列（`max_queue`），调用方拿到的仍是 `status=failed`，但返回体带 `queued: true`——插件能区分「没发出去」和「彻底丢了」，不会因为以为成功而重复补发。
- 重连后冲刷积压按 50ms 间隔逐条发送，不再把上百条一口气推给对端；中途再断则剩余消息回队首，下次连接继续。
- 队列满时明确打 `warning` 并丢本条，不再静默截断。

### 扩展点优先级段位

- 新增 `HookPriority` 常量：`EARLY`(10 前置改写) / `GUARD`(20 拦截判定) / `NORMAL`(50 默认) / `LATE`(80 兜底收尾)。
- 优先级排序本来就存在，但插件普遍用默认 50，实际执行顺序退化成"注册顺序"——两个都自称守卫的插件谁先跑，取决于加载拓扑序，动一个插件的依赖就可能悄悄换掉拦截顺序。段位把这件事固定下来：跨段位先后有保证，同段位内保持注册顺序。
- `ctx.hook` / `HookRegistry.register` 的默认值改为 `HookPriority.NORMAL`（值仍是 50，零行为变化）。

### 测试与文档

- 新增 `tests/test_backoff.py`（10 组）：退避序列与封顶、抖动只向上加、`jitter=0` 确定性、存活超阈值才清零、**连上即被踢不重置**、上限小于起始值时不越退越快、`reset()`、`describe()`，以及三个接入端确实持有统一退避实例 + 源码里不再有固定间隔重连的守卫断言。
- `tests/test_hook_serial_parallel.py` 增加 2 组：段位排序（乱序注册仍按段位执行）、`GUARD` 拦截后 `LATE` 不执行。
- 全量 pytest 461 passed，脚本式回归全过，真实启动 boot_check_echo 10/10。
- 文档：`docs/进阶/可靠性与限流.md` 新增「断线重连」整节（含「连上就清零是错的」与积压语义）；`docs/接口/高级/扩展点Hook.md` 补优先级段位表；`docs/指南/配置系统.md` 补 ws_client 新字段与语义变化。

## [1.16.0] - 2026-11-04

> 主题：**入站与出站各一道闸门**——1.15.0 管住了"进来的会不会重复处理"，这一版补上对称的另一半："发出去的会不会太快、失败了会不会悄悄丢"。

### 出站发送节流（默认开启，阈值宽松）

- 新增 `framework/messaging/outbound_guard.py`：按 `global` → `group` → `user` 三级滑动窗口限制发送速率，命中任一级即等待。
- 挂载点是**动作调用的统一入口**，不是某个具体协议：`ProtocolAdapter.acall/call`（qq_official / telegram / discord / http_inject 等遵守契约的接入端一处覆盖）+ `onebot_adapter.ApiCaller.acall/call`（OneBot 自建调用器不走基类，单独接一处）。插件走 `ctx.send_msg` / `ctx.actions` / `ctx.onebot.*` / `ctx.api()` 任一路径都被同一道闸门管住。
- 只接管发送类动作（`send_msg` / `send_private_msg` / `send_group_msg` / 转发消息 / `send_like` / `upload_group_file` 等）；`get_*` 查询动作原样直通，不受节流也不重试——查询被拖慢会连带拖慢整个事件处理。
- **节流是等待，不是丢弃**：发送是插件的明确意图，静默丢弃会让插件以为已发出，这种"看起来成功"比失败更难排查。等待超过 `max_wait` 才放弃，并返回与协议失败同形的 `{'status':'failed','retcode':-4,'throttled':True}`，插件照常判断 `status`/`retcode` 即可，不用改代码。
- 默认阈值取"明显高于正常业务、低于平台惩罚线"：全局 180/分、单群 30/分、单用户 20/分、最长等待 5 秒。正常应答与定时播报碰不到，只有真正的批量刷屏才会被压下来。

### 出站失败重试（只重试"确定没发出去"的失败）

- `retcode=-3`（本地通道异常）重试，默认额外 1 次，指数退避 + 0~25% 抖动（避免多实例同时撞同一服务端）。
- `-1` 未连接**不重试**（等一会儿也不会自己好，重试只是拖时间）；`-2` 超时**不重试**——超时不代表没送达，重试只会产生重复消息，而重复消息比发送失败更难排查。
- 重试用尽仍是异常则原样抛出，不吞。

### 观测

- 新增计数快照（`sent` / `failed` / `throttled` / `retried` / `waited_ms`），同时暴露在 `/api/runtime/stats` 的 `outbound` 字段与 `/metrics` 的 `zcbot_outbound_*` 五个指标（同源，口径一致）。
- WebUI 运行状态页新增「出站发送」卡片，限流与重试次数直接在头部标签上标出。
- 节流拒绝打 `warning`（含动作名、预计等待、上限），重试打 `warning`（含第几次、原因）；闸门自身异常一律**放行**并打 `debug`——闸门出问题不能让消息发不出去。

### 配置与热应用

- 新增配置段 `outbound`（`rate_limit.*` / `retry.*`），支持热应用：`PUT /api/config/yaml/outbound` 改完即生效，不用重启。
- 补漏：`/api/config/yaml` 的允许段清单此前不含 `reliability`，上一版新增的入站守卫配置在面板里根本读不到、改不了；本次把 `reliability` 与 `outbound` 一并加入。

### 测试与文档

- 新增 `tests/test_outbound_guard.py`（14 组）：查询动作直通、三级节流与作用域隔离、窗口过期、超限等待而非丢弃、`-3` 重试成功、`-2`/`-1` 不重试、异常耗尽后原样抛出、关闭即直通、键容量淘汰、闸门自身故障放行、异步路径，以及**走真实 `ProtocolAdapter.acall` 的接线验证**（确认闸门在适配器层生效，不是只接在 onebot 调用器上）。两个 workflow 均已纳入。
- `tests/test_observability.py` 增加三条断言，确认 `/metrics` 真的输出了出站指标族。全量 pytest 451 passed，18 个脚本式回归全过，真实启动 boot_check_echo 10/10。
- 文档：`docs/进阶/可靠性与限流.md` 新增「出站闸门」整节（挂载位置图、配置、等待而非丢弃的理由、重试取舍表、观测指标、边界）；`docs/指南/配置系统.md` 补 `outbound` 段与热应用说明。

## [1.15.0] - 2026-11-04

> 主题：**先把入站打好底**——平台层真正该先做的是事件入口的可靠性，不是运维面板。

### 入站重放去重（默认开启）

- WebSocket 断线重连、客户端升级重推会让同一条消息被重复投递（`message_id` 相同），表现为机器人对同一消息回复两次、重复触发业务。
- 内核在 `dispatch_event` 中、**归一化之后入队之前**按 `(bot_name, message_id)` 滑动窗口拦截重放，默认窗口 300 秒。被拦事件不占用分层缓冲槽。
- 只对 `post_type == 'message'` 生效；`notice` / `request` / `meta_event` 本身幂等，不去重。
- 按 `bot_name` 区分来源，多接入端同时在线时 `message_id` 撞号不会误拦。
- 内存硬上限 2 万条，超限先清过期、再淘汰最早插入，不会无限增长。

### 消息流限流（默认关闭）

- 三级滑动窗口 `global` → `group` → `user`，命中任一级即丢弃；群级跨用户累计（刷屏多为多人同时）。阈值带业务属性，框架不替部署方拍板，故默认关闭。
- `action: skip` 静默丢弃；`action: notify` 丢弃并回复一条提示，**同一窗口内至多一条**（避免提示本身变成刷屏源）。
- 限流丢弃打 `warning`（含 bot 与命中层级），去重丢弃打 `debug`（重放属正常现象，不该刷生产日志）。

### 失败安全

- 守卫自身异常一律**放行**事件：宁可漏拦，也不能让消息凭空消失。这与 IIPC 侧「隐性吞」问题的处理取向一致——任何拦截都必须留日志。

### 配置

- 新增配置段 `reliability`：`dedup_window` / `rate_limit.{enabled,window,global_per_min,group_per_min,user_per_min,action,notify_text}`。
- 新增 `fill_missing_sections()`：加载配置时把**新版本引入、旧 config.yaml 里不存在**的顶层段用默认值补进内存（`observability` / `reliability` / `database.migrations` 等一并补齐）。此前升级后旧配置文件里根本没有这些段，新能力对存量部署等于不可见、不可调。
  - 只补顶层段，已存在的段一律不动（用户值优先）；**不改写用户文件**（dump 会吃掉注释），日志会列出本次补了哪些段，需要调整由用户自行补写。

### 测试与文档

- 新增 `tests/test_config_defaults.py`（4 组）：补段、不覆盖用户值、幂等、真实读旧配置文件后不改写文件。
- 新增 `tests/test_inbound_guard.py`（13 组）：重放拦截、窗口过期、notice 不去重、多接入端区分、三级限流、notify 每窗口一次、容量淘汰、`sweep()` 释放、异常字段健壮性，以及走真实 `dispatch_event` 的接线验证。两个 workflow 均已纳入。
- 新增 `docs/进阶/可靠性与限流.md`，含守卫位置图、配置语义、已知边界（内存态、单进程、不含出站节流）。

## [1.14.0] - 2026-11-04

> 主题：**结构变更可管控**——1.13.0 解决了"知道自己是哪一版"，这一版解决"谁批的、谁执行的、凭什么批、出事怎么退"。

### 迁移分级与授权闸门（对齐 SOC 2 CC8.1 / SOX ITGC / ITIL 4）

- `Migration` 新增 `level`：`additive`（加表加列，开机自动执行）/ `rewrite`（重建表、改类型，自动执行但先备份）/ `destructive`（删表删列删行，**必须人工授权**）。判断标准只有一条：回滚这条语句要不要恢复数据。
- 启动流程不再能批准 `destructive`：引擎按 `approval_gap()` 判定，未授权时拦下并停止后续迁移，error 日志直接给出可执行的 CLI 指令。这落实了 SOX 的职责分离——自动化重启不能替人做不可逆决定。
- 批准须带变更单号：`migrate up --approve CHG-xxx --reason "..."`；只传 `allow_destructive` 无单号仍被拦（没有凭据的批准不算批准）。
- **首次接管例外**（等价 Flyway `baselineOnMigrate`）：账本一条记录都没有时（新库/无账本老库升级），历史迁移一次性收敛含 destructive，避免安装卡在半途；此后的 destructive 一律需授权。流水 `reason` 记「首次接管，历史迁移一次性收敛」。
- 历史 9 版补齐分级：v2 去重删行 → `destructive`；v3 重建表 → `rewrite`；其余 `additive`。

### 执行证据链（append-only 流水表）

- 新增 `schema_migration_runs`：每次尝试一行，字段 `version` / `name` / `level` / `started_at`（UTC ISO 8601）/ `duration_ms` / `success` / `source`(boot/cli/api) / `operator` / `ticket` / `reason` / `error` / `backup` / `db_type` / `app_version`，**只增不改**。
- 账本 `schema_migrations` 补 `level` / `operator` / `ticket` / `reason` 四列（老库启动时自动补列，不重跑、不丢历史行）。两表分工：账本是"当前状态"（重试覆盖），流水是"执行证据"（审计取样查这张）。
- 修掉一个真实缺陷：账本读取原先用显式列清单，老库缺新列会让整表读失败并被误判成「一版都没应用」（会触发重跑，destructive 甚至会被首次接管规则放行）。改为 `SELECT *` + 缺列取默认值。

### 执行前备份（回退预案）

- `rewrite` / `destructive` 执行前自动备份，同一轮只备一次：SQLite 用 `VACUUM INTO` 在线快照落 `backups/`（按 `keep_backups` 轮转，旧版 SQLite 退化为文件复制并告警 WAL 风险）；MySQL 记录 `SHOW MASTER STATUS` binlog 位点作为恢复点，拿不到位点直接报 error，不假装有备份。
- 备份路径写进流水，审计可直接核对"这条不可逆变更当时有没有退路"。

### 干跑与自检

- 新增 `migrate plan`：列出将执行什么、是否需授权、会不会备份，**不执行任何迁移体**，输出可直接贴进变更单的影响评估栏。
- 新增 `migrate verify`：账本版本 vs 代码版本、必需表是否齐全、是否还有未授权迁移，返回码可进 CI。
- 新增 `migrate runs`：打印最近执行流水。

### 配置与接口

- 新增配置段 `database.migrations`：`auto_apply_destructive`（默认 false）/ `backup_before_rewrite`（默认 true）/ `keep_backups`（默认 5）/ `operator`。
- `POST /api/db/migrations/up` 支持 body `{ticket, reason}`，与 CLI 共用同一道闸门；审计日志补记 `ticket` / `reason` / `backup`；`GET /api/db/migrations` 返回 `level` / `requires_approval` / `blocked_reason` 与最近流水。

### 测试与文档

- `tests/test_db_migrations.py` 由 10 组扩到 19 组：首次接管收敛 / 有账本后 destructive 被拦 / 无单号仍被拦 / 批准后记操作人与单号且备份文件真实存在 / 流水 append-only 与账本不覆盖 / 备份轮转 / plan 不执行 / 配置默认安全 / 老账本自动补列。全量 pytest 420 passed，15 个脚本式回归全过。
- 文档 `docs/进阶/数据库迁移.md` 整篇重写：新增「企业标准到底要求什么」（SOC 2 CC8.1、SOX ITGC CM-01~06、ITIL 4 变更使能、零停机演进、回退预案逐条映射）、变更分级、职责分离、证据链分工、备份语义、expand-contract 附录。

## [1.13.0] - 2026-11-04

> 主题：**结构演进可追溯**——此前「库是哪个版本」只能靠查列是否存来反推，迁移失败还会被 `except: pass` 吞掉。

### 数据库迁移版本化（账本 + 校验和 + 失败重试）

- 新增 `framework/database/migrations.py`：账本表 `schema_migrations`（`version` / `name` / `checksum` / `applied_at` / `duration_ms` / `success` / `origin` / `note`），版本单调递增、按序执行、每版只跑一次。
- 历史 9 个 `_migrate_*` 收敛为注册表 `MIGRATIONS`（版本 1–9，顺序即原执行顺序，**已发布版本不可重排/不可改内容**）；整表由「每次启动无条件幂等执行」改为「账本驱动的增量执行」。
- **校验和**取迁移体源码的 sha256：已应用的迁移被改动时报 error 并在状态里标 drift，但**不重跑**（不可变原则），正确做法是追加新版本。
- **失败不记账**：迁移抛异常写 `success=0` + 错误摘要、停止后续迁移、下次启动自动重试；迁移体内的 `except Exception: pass` 全部改为「记录 + 抛出」，不再让失败与成功看起来一样。
- 失败**不阻塞框架启动**（优先级：可用性 > 结构最新），但启动日志以 error 级别给出当前版本、最新版本与首个失败原因。
- MySQL 多节点用命名锁 `zcbot_schema_migrations` 串行化（专用连接持有，`GET_LOCK` 是连接级锁，不能借池连接）；拿不到锁则跳过本次并告警。SQLite 走进程内锁。
- 账本表建不出来（只读库/无 DDL 权限）时退化为「不记账、照旧幂等执行」，并在每次启动告警。
- 新增运维命令 `python -m framework.database.migrate status|up|history|check|baseline`，`check` 有未应用或漂移返回 1，可直接进 CI；`baseline --version N` 用于接管无账本的老库（只记账不执行）。
- 新增接口 `GET /api/db/migrations`（登录可见）与 `POST /api/db/migrations/up`（超管，写审计日志）。
- 新增 `tests/test_db_migrations.py`（10 组：全新库/二次运行不重跑/失败停止并记账/修复后重试/漂移不重跑/基线只记账/注册表校验/账本表不属核心表清单）；`test_db_regression.py`、`test_db_resilience.py`、`test_perm.py` 原有断言不变。
- 文档：新增 `docs/进阶/数据库迁移.md`（三处同步、不可变铁律、命令行、并发与失败语义），`docs/进阶/数据库.md` 补入口。

### 结构化日志（JSON Lines）

- 新增 `framework/jsonlog.py`：`log.format: json` 时控制台与日志文件改为每行一个 JSON 对象，字段固定（`ts`/`level`/`logger`/`msg`/`pid`/`thread`/`src`/`service`，可选 `fields`/`exc`），采集端无需 multiline 配置。
- 附附加字段写法 `extra=jsonlog.fields(k=v)`，进 `fields` 对象，不污染 `msg`。
- **只换 formatter，不换 handler**：轮转、级别过滤、重复错误合并全部不变；桥接 WebUI 日志中心的 handler 不被替换（否则后台日志页会显示成 JSON 字符串）。
- `log.format` 支持热应用（`framework/hotapply.py` 的 `log` applier），切回 text 时还原原 formatter 对象；非法值回落 text 并给出提示。
- 新增 `tests/test_json_logging.py`（7 组，含真实 Framework 接线与落盘校验），`test_config_hotapply.py` 补格式切换断言；两个 workflow 均已纳入。

### 后台「运行状态」页接入健康与指标

1.12.0 只把可观测性做成了端点，看数据要 curl。这一版把同一份口径搬进后台：

- `observability.py` 的检查逻辑从 `register()` 闭包里提到模块级（`version_of` / `uptime_of` / `db_probe` / `plugin_counts` / `collect_checks` / `snapshot` / `request_counts`），`/readyz`、`/metrics` 与 WebUI 三处读同一份实现，避免口径漂移。
- 请求计数改为模块级累计，`/metrics` 与面板共享同一份数据。
- `GET /api/runtime/stats` 新增 `health`（与 `/readyz` 同构）、`event_buffer`、`requests`、`db_degraded` 四个字段；原有字段一个不动，纯新增。
- 后台「运行状态」新增三个区块：健康检查（逐项状态与详情）、事件缓冲（L1/L2/L3/L4 条目与占用、累计丢弃）、HTTP 请求分布（Top 10）。
- `tests/test_observability.py` 增加一项：断言 runtime/stats 的 `health.checks` 与 `/readyz` 完全一致，防止两处后续改漂。

## [1.12.0] - 2026-11-03

> 主题：**让框架可运维、可演进**——此前缺的不是功能，而是「外部系统怎么判断它健康」「插件怎么表达先后顺序」「改配置要不要重启」「API 破坏性变更往哪落」。

### 健康探针与指标端点（可观测性）
- 新增 `framework/api/observability.py`：`GET /healthz`（存活，不查库）、`GET /readyz`（就绪，带逐项检查明细）、`GET /metrics`（Prometheus 文本格式，零第三方依赖）。
- 此前 `/healthz`、`/readyz`、`/metrics` 只是蜜罐的诱饵（伪 nginx 404 / 假 actuator 内容），现在真实路由优先，蜜罐注册时自动跳过已占用路径；关闭开关后才回落到诱饵。
- 探针免登录 / 免限速 / 免人机验证 / 不写审计；端口隔离与纯 API 模式下 `/healthz`、`/readyz` 在两个端口都可达，`/metrics` 只在 API 端口暴露，避免公网 WebUI 端口泄露运行数据。
- `/metrics` 支持 `observability.metrics_token`（Bearer 或 `?token=`），指标覆盖构建信息、运行时长、就绪、插件加载/失败数、数据库可用性、事件缓冲四层条目与字节、丢弃计数、WS 连接数、进程 RSS/线程数、按 method+status 聚合的请求计数（不含探针自身）。
- 新增 `observability` 配置段（`health` / `metrics` / `metrics_token`），并纳入 `/api/config/yaml/observability` 可编辑段。
- 新增 `tests/test_observability.py`：覆盖免鉴权、未就绪 503、逐项检查、指标内容、探针不计入请求数、令牌鉴权、关闭开关、端口隔离 8 组场景。

### 插件声明式依赖 `requires`
- 新增 `framework/loader/graph.py`：`__plugin_meta__` 支持 `requires`（list / 逗号分隔字符串 / dict 键），用 AST 静态解析（不执行插件代码），按 mtime 缓存。
- `load_all` 改为拓扑排序加载；依赖不存在、被禁用或加载失败的插件**直接拒绝加载**并给出明确原因，不再产生「加载成功但功能异常」的半个插件；循环依赖判定为异常且不死锁。
- `load_plugin`（单插件加载 / 热重载路径）增加同样的前置校验；拦截原因可通过 `GET /api/plugins/<name>/config` 的 `dep_error` / `requires_unmet` 查询，已加载插件（含官方插件）视为满足。
- 新增 `tests/test_plugin_requires.py`：解析 / 归一化 / 拓扑排序 / 缺失与循环拒绝 / 已加载满足 / 运行时拦截 / 真实 Framework 端到端。

### 配置热应用
- 新增 `framework/hotapply.py`：`PUT /api/config/yaml/<section>` 写盘后把新值应用到运行中框架，并在响应 `data` 里返回 `live` / `restart_required` / `restart_reason` / `notes`，前端消息直接说明哪些字段已生效、哪些要重启。
- 即时生效：`log.level`（直接改 root logger）、`framework.blackbox`（开关与内存上限）、`security.rate_limit`（`RateLimitGuard.update_config` 重建限速器）、`plugin.hot_reload`。
- 需重启：`database` / `web` / `onebot` / `ssl` / `event_queue` / `dual_process` / `iipc` / `console` / `observability`，以及未注册 applier 的未知段（保守判定，不假装生效）。
- 支持第三方注册 applier：`hotapply.register(section, fn)`，`fn(framework, section_cfg) -> list[str]`。
- 新增 `tests/test_config_hotapply.py`：日志级别真变、黑匣子上限（含 16KB 下限兜底）、分段判定、部分生效段、自定义 applier、`/api/v1` 别名。

### API 版本前缀
- 所有真实 `/api/*` 路由自动挂一份 `/api/v1/*` 别名（同一 `view_func`），旧路径保持不变；蜜罐兜底与静态文件不参与别名。
- 为将来的破坏性变更预留落点：新语义走 `/api/v2/*`，`v1` 保持旧语义。

## [1.11.0] - 2026-11-03

> 主题：**开发者体验与智能层补齐**——改插件保存即生效（HMR）、内核文案外置、LLM 调用链可见、Agent 具备本地知识库。以下均为已落地并实测通过的变化。

### 热重载增强：用户插件保存即生效
- 新增 `plugin.hot_reload`（默认 `true`）：心跳检测到 `plugins/` 用户插件源码 mtime 变化后，由「仅重新注册」升级为「unload + reload 全量重载」，从源码重新编译，改动的 handler 逻辑真正即时生效；设为 `false` 则退回旧行为（仅 `register(ctx)`）。
- 复用与 Web 面板「重载」、`retry_failed_plugins` 自愈完全相同的 machinery（`unload_plugin` + `load_plugin` + `register_commands`），核心插件（`core_plugins/`）仍不参与心跳自动重载。
- 新增 `tests/test_hot_reload.py` 覆盖心跳分派逻辑（开启/关闭/无变更/加载失败四种场景）。

### i18n 词条外置到 `framework/lang/`
- 把 `framework/i18n.py` 内嵌的 `LANGS` 词条字典剥离到 `framework/lang/zh.yaml` / `en.yaml` / `ciallo.yaml` 三个数据文件，源码只保留加载逻辑与 `t()` / `set_language()` 等 API，对外契约不变。
- 加载时自动将 YAML 嵌套结构展平为「模块.语义」点号 key，与既有 `i18n.t('framework.started', ...)` 调用完全兼容；新增语言只需在 `framework/lang/` 下加一个 `<code>.yaml`，无需改代码。
- 新增 `tests/test_i18n_lang.py` 覆盖三语言切换、占位符、缺失回退与嵌套展平。
- 同步更新 `docs/指南/多语言.md`（新增词条位置 + 词条存储描述）。

### LLM 调用链追踪（llm_core 插件）
- `ToolLoopAgent.arun` 新增 `result.trace` 全链路埋点：`model` 跨度（每轮模型调用的耗时、是否带工具调用、token 用量、模型名、失败原因）+ `tool` 跨度（每个工具执行的耗时与失败标记），并在 `finally` 汇总 `total_ms` / `turns` / `finished_at`。
- `main.py` 暴露 `GET /api/llm_core/trace` 返回对话调用链，便于排障时直接看"哪一轮模型调用慢 / 哪个工具挂了"。追踪改为**进程内保留最近 30 条**（`_TRACE_HISTORY`，索引 0 为最新，超出上限自动淘汰旧记录），接口新增可选 `index` 参数查看历史批次，同时返回 `history` 摘要列表；不带参数时行为与原先一致（返回最新一次）。
- 面板新增「调用链追踪」标签页（`web/trace.html`）：指标卡（总耗时 / 轮次 / 模型 / 提供商 / 调用次数）+ 按跨度累计排布的执行时间轴（模型与工具分色，失败标红）+ 跨度明细表（含 tokens 入/出与失败原因），并可从下拉框切换历史批次。
- `llm_core.__version__` 提升至 `1.1.4`，已重打包 `llm_core.zip`（md5 变化会触发释放覆盖）。
- 新增 `tests/test_llm_trace.py` 覆盖 turns / 双跨度 / 字段完整 / 失败工具标记，以及历史保留、`index` 语义、超限淘汰，ALL PASS。

### 本地知识库（RAG，llm_core 插件）
- 新增 `embeddings.py`：可插拔 embedding 客户端，只认 OpenAI 兼容的 `POST /embeddings` 方言，换 `base_url` 即可（BGE-M3 / text-embedding-3-small 等同一套代码）；维度可配置，留 0 时按首次返回自动推断。
- 新增 `knowledge.py`：单个 SQLite 文件收纳「文档 / 切片 / 向量 / 全文索引」。向量后端双形态——`sqlite_vec`（vec0 虚拟表，向量内联进库）与 `memory`（numpy 暴力余弦），`sqlite_vec` 不可用时自动降级；检索为**混合检索**：向量语义召回 + FTS5 trigram 关键词召回（BM25），再用 RRF 倒数排名融合。
- `main.py`：新增 `rag` 配置段与 `_init_kb()` 懒初始化；把 `knowledge_search` 注册进工具总线（Agent 可自主调用检索本地资料）；新增知识库管理接口（入库 / 列表 / 删除 / 检索 / 清空）。
- 面板新增「知识库」标签页（`kb.html`）：粘贴文本入库、检索自测、文档列表与删除、一键清空。
- `core_plugins/llm_load/requirements.txt`：声明 `sqlite-vec>=0.1.9`，随插件启用自动安装；装不上时自动退 memory 后端，功能不受影响。
- `__version__` -> `1.1.3`；新增 `tests/test_llm_rag.py` 覆盖入库 / 向量召回 / 关键词召回 / 删除 / 工具函数，ALL PASS（memory 后端 + 确定性假 embedding，CI 可跑）。

### 工程与测试
- CI 测试矩阵补入本迭代新增的四个脚本式回归：`test_hot_reload.py`、`test_i18n_lang.py`、`test_llm_trace.py`、`test_llm_rag.py`（此前只在本地跑，未进 `tests.yml`）。
- `tests/test_llm_load.py` 的现网一致性检查改为**提示语义**：本机重打包后未重启时 `plugins/llm_core` 必然落后，属正常状态，由装载器下次启动按 md5 覆盖；不再因此假红。

## [1.10.0] - 2026-11-02

> 主题：**生产化成熟度跃迁**——可观测性（内存黑匣子）、韧性（崩溃守卫 + db 自愈）、多协议接入（适配器注册表 + 命名空间隔离）、启动自愈（单实例锁 + 安全模式假启动）。以下均为已落地并实测通过的变化。

### 新增

- **内存黑匣子（blackbox）**：纯内存 `deque` 环形缓冲（256KB 硬上限，满丢最旧），16 阶段事件流追踪（ingest→done），覆盖 ingest/route/match/handler/send 全链路埋点；稳定代号 `_bb_eid` 解决 `normalize_event` 浅拷贝导致的链路 id 断链；`stuck_at` 用"是否存在 done 帧"判定卡步，兼容乱序。
- **命令 handler 超时熔断 + send_msg 失败诊断**：handler 超时不再冻结整条管线，超时走熔断分支；出站在站 `send_fail` 帧记录失败原因，黑匣子可回溯"消息出去没/为何失败"。
- **崩溃重启守卫（crash_guard）**：无限重启死循环 → 一次性明确失败；`error.log.1..N` 轮转保留 9 份；启动成功 `clear_crash_markers` 复位，避免误杀正常重启；支持 `alert_webhook` 外部告警。
- **核心插件命令路由修复 + echo / help 自检插件**：修复核心插件命令不被路由的历史缺陷；新增 `echo`（回声）、`help`（内置帮助）自检插件，**默认开启并对接入端生效**，让"发 /echo 你好 收到 你好"成为可验证的接入标准。
- **多平台接入（适配器注册表 + 命名空间隔离）**：`adapter_key ↔ uuid` 落 `drive` 表 + 内存缓存（热路径零 DB）；`namespace_identity` 改写为 `<uuid>@<id>`（开关关闭退化裸号，向后兼容），`raw_id` 为 0/空直接返回避免 `<uuid>@0` 无意义键；支持双向绑定。
- **扩展点串行 / 并行执行**：串行组按优先级 `await` + 短路（首个 `False` 即终止并行组）；并行组 `asyncio.gather` 收集全部结果；并行不参与短路，需短路必须 `serial=True`。
- **事件分发拦截点告警 + 四类事件统一分类层**：事件分发拦截点由静默改为可观测告警；消息 / 通知 / 请求 / 元事件四类统一分类。
- **IIPC 加固**：示例插件 + 反向 WS `access_token` 鉴权；中间件管线；消息链路吞异常修复（极端环境下静默丢消息）。
- **db 初始化自愈**：`admin_users + plugins` 两张表探针 → 全量 `_REQUIRED_TABLES` 检测；`_apply_init_sql` 只补缺失表（幂等）；建完复验仍有缺失即报 error 列清单，不再无脑"完成"；自检本身失败（连接/权限）向上抛，避免几十行 WARN 埋根因。
- **单实例文件锁（main.py）**：跨平台 advisory lock（Linux `flock` / Windows `msvcrt.locking`），锁文件 `data/zcbot.lock` 写 PID；进程退出内核自动释放，拿不到锁的第二个实例立即 `sys.exit(1)` 且不做任何重活；Windows 端经 `CreateFileW` 双保险、冲突时明确打印「检测到另一个 ZCBOT 实例（PID X）」、atexit 兜底清理锁文件，避免退出残留误杀后续实例。
- **安全模式假启动（main.py）**：崩溃达上限且 `framework.crash.safe_mode`（默认 `true`）开启时，不退出进程、打印明确 banner 后挂起（`await stop_event.wait()`），让 `Type=simple` 守护进程误以为启动成功、停止反复拉起，把"持久错误无限冷启动"的性能税降到≈0；SIGTERM/SIGINT 优雅退出。适合 db 配置错等"永远启不动"的场景；banner 明确提示「先删除 `error.log*` 崩溃计数文件，再手动 kill 本进程（由守护进程拉起干净实例；新实例正常启动会自行清零）」，与 `safe_mode=false` 分支一致，避免不删文件导致重启仍触发守卫死循环。
- **终端黑匣子查看器（TTY `debug` 命令）**：终端输入线程键入 `debug` 进入交互式内存黑匣子查看器（`framework/terminal/blackbox_viewer.py`），支持 `stats` / `recent [n]` / `stuck [n]` / `last [n]` / `trace <event_id>` / `clear` / `exit` 子命令，可在运行时 TTY 直接排查"事件卡在第几步"；由 `input.py` 阻塞占用 stdin 避免与主循环抢终端；非 TTY / 终端未启用时无从进入，黑匣子仍由 WebUI 调试接口兜底。

### 加固（hotfix，已并入 v1.10.0 主线）

- **WebUI 登录后建议修改默认密码（`framework/api/auth.py` + `webui/src/views/Layout.vue`）**：`/login` 与 `/api/me` 返回 `default_password` 标志（比对当前哈希与 `sql/init.sql` 种子哈希常量，零数据库迁移、老部署可用）；前端挂载检测后在顶部显示可关闭提示条并带「前往修改」一键跳转 `/settings`，**仅建议、不强制**（默认账号 `admin` / `admin123`）。
- **插件加载失败「平台化」诊断（`framework/deps/diagnose.py`）**：`classify_load_error(exc)` 把 `No module named X` 归类「缺失依赖」（取顶层包名，如 `aiohttp.client`→`aiohttp`）、`cannot import name Y from X` 归类「版本不兼容」、`DLL/.so 加载失败` 归类「系统运行库缺失」，未匹配则保留原始消息；接入 `framework/loader/lifecycle.py`（用户插件）与 `framework/core/runtime.py`（官方插件）的加载异常分支，日志同时输出中文诊断 + 安装命令（`<当前解释器> -m pip install <模块>`，与框架自身包管理器一致）+ 原始错误，让缺依赖的报错不再是一串英文堆栈。包管理器经源码核实为 **pip（经框架运行解释器 `sys.executable -m pip`）**，非 uv / pipx。

### 修复

- **router `message.classified` 扩展点 ev 未定义异常**：分类扩展点调用时 `ev` 作用域错误导致 `NameError`，已在调用前正确绑定。
- **文档默认开关矛盾**：`core_plugins.yaml` 默认开启清单（原 `DEFAULT_ENABLED_CORE_PLUGINS` 与文档描述不一致）已对齐为 `{echo, help, scheduler, webui}` 默认开启；`LLM.md` / `README` 同步修正（含版本号漂移）。
- **render 原生二进制重建**：覆盖全部 7 个目标平台（含 32 位 win32 / linux-i686 / linux-armv7），UPX -9 压缩；32 位架构整数溢出加固。
- **CI `test_core_plugin_switch` 与默认开启名单脱节**：原用例假设所有缺失插件默认禁用，但 v1.10.0 起 `DEFAULT_ENABLED_CORE_PLUGINS = {echo, help, scheduler, webui}` 名单内插件自动启用，导致 CI 在 4 个 Python 版本矩阵上整片失败（exit 1）。用例改为按名单拆分断言（名单内期望 `True`、其余期望 `False`），本地复现 CI 全量 pytest（371 项全过）验证修复。
- **CI `test_event_buffer` 偶发红（tests.yml python-tests(3.12)）**：`test_restart_inherits_persisted_rows` / `test_overflow_to_sqlite_and_drain` 用固定 `sleep(0.2)` 竞态 L4 后台 flush，慢机器上 flush 未完成即断言 `sqlite_pending > 0` 为 0。加固为轮询等待（超时 3s），不涉及 `framework/core/event_buffer.py` 逻辑改动；本地复跑 13 项全过、两用例各 x3 稳定通过。

### 变更

- **默认核心插件清单**：由"发现即禁用"改为 `{echo, help, scheduler, webui}` 默认开启（其余显式启用），降低新部署的"启了但啥也不干"困惑。
- **CLI 脚手架 + i18n**：新增 ZCBOT CLI（`zcbot` 入口）+ 多语言 `zh` / `ciallo` / `en`（历史 1.8.9 已引入，本版随发布线收敛版本号）。

---

## [1.8.9] - 2026-10-20

> 主题：**扩展面自由度补齐（中间件管线 / 全局变换节点）+ 开发体验（多语言、IDE 补全）+ 文档与构建规范化**。
> 以下均为已落地并实测通过的变化。

### 新增

- **多语言（语言选项）**：新增 `framework/i18n.py`，支持 `zh`（默认，简体中文）/ `ciallo`（中文活泼版，Ciallo～(∠・ω< )⌒☆ 风格）/ `en`（English）三种提示文案语言。
  已本地化 **12 条**覆盖整条启动链路的文案：`framework.initializing` / `init_done` / `starting` / `started` / `stopping` / `stopped`、
  `app.ctrl_c`、`plugin.loaded` / `plugin.disabled`、`webui.started`、`console.ready`、`terminal.skipped`（`ciallo` 实测一次启动输出 47 处）。
  三种设置方式，优先级 **启动参数 `--language` / `-l` → 环境变量 `ZCBOT_LANG` → `config.yaml` 的 `language` → 默认 `zh`**；
  `config.yaml` 默认模板已加入 `language: zh`。
- **CLI 配置控制台**（自带配置入口）：新增 `python main.py config` 子命令 ——
  `config` 显示配置摘要（含当前语言与可用语言）、`config get <key>` 查看、`config set <key> <value>` 写回 `config.yaml`。
  `set` 采用**按行原地替换**而非 `yaml.dump`，`config.yaml` 里的中文注释完整保留；支持 `--config <path>` 指定配置文件。
- **IIPC 多阶段中间件管线**：`ctx.middleware(stage, priority, handler)`，四阶段 `pre`（入站后、命令匹配前，可改写 / 返回 `False` 短路）/ `post`（分发后）/ `send`（出站 flush 前，可改写或拦截）/ `error`（handler 抛异常时中央处理）。未注册任何中间件时热路径与原先完全一致（零成本）。
- **内核级全局变换节点**：`ctx.transform(priority, handler)`，内核在把任意事件扇出给所有订阅者**之前**按 priority 依次调用变换节点，返回新 event data 继续分发、返回 `None` 则全局丢弃 —— 一个插件做脱敏 / 翻译 / 补全，所有订阅者（命令与 `on` 处理器）都看到变换后事件。
- **IDE 补全与插件脚手架**：新增 `py_sdk/py.typed`（PEP 561）；`PluginContext` 补上 `ext.attach` 运行时动态挂载的 14 个扩展方法（`kv` / `config` / `http` / `provide` / `resolve_service` / `call_plugin` / `metric` / `transform` 等）的完整类型签名；新增 `plugins/_template/main.py` 插件模板（`register(ctx: PluginContext)` 带类型标注）。
- **内核模块地图**：新增 `go_kernel/README.md`，把 28 个平铺 `.go` 按 core / store / ext / plugin / util 五层归类，附调用路径与「为何保持单 `package main`」说明；补齐 5 个缺失首行分层注释的文件。
- **消息类型与能力扩充**：消息段类型 20 → 34（新增 card / post / contact / poll / animation / audio / video_note / venue / button / reaction / quote / system / embed / ephemeral），能力声明 22 → 38，并补别名映射。
- **测试脚本**：新增 `tools/middleware_check.py`（进程内驱动，7 项）、`tools/pipeline_check.py`（真内核 + 双插件 e2e，5 项）、`tools/ext_check.py`（扩展面 e2e，12 项）。

### 修复

- **图片渲染 PIL 回退逐行重载字体**：`_CanvasPIL._font` 在指定 `font_path` 时绕过模块级缓存、每次 `ImageFont.truetype()` 读盘加载，多行 / 多元素渲染 CJK 字体（数 MB~数十 MB）被逐行重新解析。已加实例级 `_font_cache`，按 `(size, bold)` 命中复用（实测同 size 仅加载一次）。
- **`main.py` 把 `--language` 当成配置文件路径**：`amain()` 直接读原始 `sys.argv[1]` 取配置路径，摘掉语言参数后未同步回 `sys.argv`，导致生成一个名为 `--language` 的垃圾默认配置（与历史坑「`main.py argparse` 生成叫 argparse 的配置」同源）。已在摘参后同步 `sys.argv`。
- **aiwriter 两处真实 bug**：`db_execute` 向模型泄漏内部模式说明；独立（standalone）模式下 `plugin_action` 被误拦截。两处均已修复。
- **文档改名导致 aiwriter 静默失效**：`_load_plugin_brief()` 读 `docs/llm-plugins.md`，文档目录重构后该文件不存在即静默跳过，AI 系统提示里的插件约定凭空消失。已同步修正 `aiwriter/main.py`、`LLM.md`、`README.md` 及 `framework/database/` 内的文档路径引用。
- **IIPC 加固遗留**：`isolateCorruptDB` 在 Windows 上因 SQLite 仍持有句柄导致 `os.Rename` 失败并残留坏文件；已改为 Ping 失败时 `db.Close()` 释放句柄，且复制分支结束后删除源文件。
- **pytest 配置缺失**：仓库此前无任何 pytest 配置，异步用例被 strict 模式静默判失败；已加 `asyncio_mode = "auto"`（不加 `testpaths`，避免脚本式测试文件导致收集 INTERNALERROR）。
- **原生渲染器 32 位架构整数溢出加固**：`image_renderer` 的 Rust 原生后端（`canvas.rs`/`lib.rs`）此前在 i386 / i686 / armv7 等 32 位平台存在隐患——像素缓冲按 `u32` 乘法分配、`r*r` 等平方距离按 `i32` 计算，画布尺寸或半径较大时会在 4 字节整数上回绕，导致分配出尺寸错误的缓冲或圆角判定错乱。已统一为「`u64` 计算缓冲总字节数并校验地址空间 + `usize` 线性索引 + 平方距离改 `i64`」，32/64 位行为一致；原生二进制已重建覆盖全部 7 个目标（含 `win32` / `linux-i686` / `linux-armv7`）。
- **数据库初始化"假成功"与部分表丢失不自愈**（告警可观测性 + 自愈双缺陷）：
  - `_REQUIRED_TABLES` 定义了却从未被使用，初始化探针只看 `admin_users` + `plugins` 两张表。运行中一旦丢失其它表（手工 DROP / 迁移中断 / 磁盘回滚），探针仍判"已初始化"，缺失的表**永不重建**，直到相关功能写库报错才暴露。现改为按 `_REQUIRED_TABLES` 全量检测缺失、缺哪张补哪张，建完**再复验一遍**，仍缺则 `error` 并列出清单。
  - 逐表 `try/except` 把建表失败压成 `debug`，随后无条件打「数据库自动初始化完成」——建 0 张表也报成功，首次部署的权限/方言/磁盘问题完全不可观测。现按成功/跳过/失败分类统计，失败带首条错误摘要，0 成功不再声称完成。
  - `_REQUIRED_TABLES` 边界与 `sql/init.sql` 对齐（`group_plugin_settings` 由 `schema.py` 建，不属 init 阶段），避免"永远缺一张表"的假告警。
  - 种子数据改为**仅随本次新建的表插入**，避免全量重跑时重复插 `admin_users` / `system_config`。
  - `sql/init.sql` 的 `UNIX_TIMESTAMP()` 在 SQLite 下不存在（`no such function`），导致**默认权限组种子一直插入失败**且被静默吞掉——全新 SQLite 部署根本没有 `default` 组。已在方言层补 `UNIX_TIMESTAMP()` → `strftime('%s','now')` 翻译。
- **数据库只读 / 损坏缺少进程级兜底**：
  - 运行中卷变只读（`SQLITE_READONLY`）时，此前只有各插件各自收到异常，框架自身不吭声。现 `Database` 首次遇到只读错误即打一条醒目 `error`（后续抑制，避免刷屏），并通过 `persist_degraded` 暴露降级状态；写成功且此前降级时打「已恢复可写」。
  - 数据库无法使用时降级到 `SqlSimEngine` 的告警，从含糊的「降级为本地 SQL 模拟引擎」改为明确说明【持久化已失效】、数据实际写入 `data/db/sql/*.json`、切回真实库不会自动迁移、以及如何恢复；新增 `database.fallback_to_sim: false` 供生产环境强制不降级。
  - `malformed database schema` 等自检失败此前会继续往下跑，导致 18 条建表语句各失败一次、真正根因被埋在几十行 WARN 里。现直接中止初始化并降级（实测告警从 18 条收敛为 1 条讲清根因的 error）。
  - `_rebuild_sqlite_text_column` 重建表前先 `DROP TABLE IF EXISTS` 清理上一次失败残留，避免残留表被 `IF NOT EXISTS` 静默复用、脏行混入新表。
- **IIPC Go 内核同源缺陷**：`EnsureSchema` 逐条建表失败只 `logWarn`，末尾仍打「核心表结构就绪」。现汇总失败数与首条错误、用 `sqlite_master` **复验实际存在性**，缺表即 `error`；`sqliteStore` 增加只读状态识别（首次进入只读打 `error`、恢复可写打 `info`、`ReadOnly()` 供健康检查）——与 Python 侧行为对齐。
- 新增回归测试 `tests/test_db_resilience.py`（9 项：部分表丢失自愈、种子不重复播种、只读写失败/读可用/可恢复、只读 + 缺表必须报"未完成"、真实只读文件启动）与 `framework/iipc/go_kernel/schema_resilience_test.go`（3 项）。
- **IIPC 消息链路"静默吞"缺陷排查与修复**（延续"数据库假成功"暴露的系统性兜底缺失）：继续沿事件流水线排查，定位并修复多处"吞消息/吞异常"点：
  - `iipc_client.py` 事件分发 `_dispatch_loop` 用 `except Exception: pass` 把**事件处理器异常（含整个事件分发入口 `_on_event` 在中间件/事件构造阶段炸掉）无声吞掉**，插件"收不到消息"却无线索；改为抽到 `_dispatch_one` 并向 stderr 打印异常堆栈。
  - 读循环 `_read_loop` 此前 `except: pass` 把连接断开根因（RST/EOF/解码错误）吞掉、`_read_loop_inner` 对损坏 JSON 行 `continue` 静默跳过（可能丢失一条消息）；均改为 stderr 告警留痕。
  - `sdk.py` `_invoke` 的依赖装配（`_bind_args`）失败以 `except: pass` 退回原始实参、根因被掩盖；改为打印装配失败信息。
  - Go 内核 `ipc_server.go` 读循环对损坏帧 `continue` 静默跳过（与 Python 侧同源）——补 `logWarn`。Go 侧 `sendRaw`（出站队列满丢帧）、`publishFromConn`（非适配器/载荷解析失败/缺 type）原本已打 `logWarn`，确认非静默。
  - `event_buffer.py` / `event_bus.py` 经核查已是健壮实现：`EventBuffer` 全满丢弃有 `_dropped` 计数与告警、`stats()` 可观测、L2→L3 兜底与 sqlite→file 降级；`EventBus._invoke_one` 异常 `logger.error`——均非静默，未改动。
  - 新增回归测试 `tests/test_iipc_silent_drop.py`（9 项：出站消息 flush 失败回队列重试/恢复重发/有界裁剪/顺序保持、日志批次失败计数、权限查询失败降级可见、正常不计数）与 `tests/test_iipc_client_dispatch.py`（4 项：`_dispatch_one` 异常可见、正常无噪声、无订阅者安全、损坏帧告警）。
- **主干事件分发「Hook 一票否决」可观测性修复**（对照"事件分发系统架构设计"理想图，系统盘查内核主干流水线）：此前三处扩展点会**真正丢弃/跳过消息但运维不可见**——`event.before_dispatch` 返回 False 丢弃事件仅 `logger.debug`、`router.before_route` 返回 False 跳过路由**无任何日志**、`message.before_send` 返回 False 取消发送仅 `logger.debug`。三者均已升级为 `logger.warning` 并给出明确的"消息被拦截/跳过/取消"措辞，使生产 INFO/WARNING 下"消息凭空消失"可溯源。
  - 内核主干其余环节经核查已健壮，非静默：`_process_event` 各分支、`MessageRouter._match_plugin_commands` 命令 handler 异常打 `logger.error` + 调 `on_error` 钩子、`EventBus._invoke_one` 异常打 `logger.error`、`RawMessageHandler` 超时/异常打 `logger.error`。
  - 对照理想架构确认的三类偏差：**A. Hook 无 `serial/parallel` 声明能力**；**B. 三类 before_* 拦截不可见（已修）**；**C. 四类事件（一般/正则/全量/原始）无统一分类契约层**（当前由命令引擎 + 事件总线 + raw handlers 三套并存覆盖，功能等价但结构不符图）。
  - **A 已落地**：`HookRegistry.register(..., serial=True)` 新增 `serial` 形参（默认 `True`，对现存全部插件零行为变化）；`trigger_async` 改为「串行组按优先级顺序逐个 await（保留短路语义）+ 并行组经 `asyncio.gather` 并发执行」，实现架构图要求的"声明串行→顺序执行 / 未要求串行→并行执行"；`ctx.hook(point, handler, priority, serial)` 同步打通插件入口。`trigger_sync`、`unregister`、`clear_plugin` 随 4 元组存储同步修正。`EventBus.aemit` 的 `parallel` 形参非本次范围（路由层按需调用即可）。
  - 新增回归测试 `tests/test_dispatch_drop_visibility.py`（3 项：三处 before_* 拦截均打 WARNING 且事件/路由确实被丢弃/跳过）与 `tests/test_hook_serial_parallel.py`（5 项：默认串行保序、并行 handler 真并发≈0.1s、串行仍顺序≈0.2s、串行短路阻止并行组、非短路两组结果都收集；真跑 5/5 全过）。
  - **C 已落地（四类事件统一分类契约层）**：新增 `framework/messaging/event_classify.py`（`EventCategory` 枚举 `general/regex/all/raw/none` + `initial_classification`）；`Event` 新增 `_category_command` 槽与 `category` / `categories` 属性（单一真相源）；`router_match._match_plugin_commands` 在命令**实际执行**时回填 general（静态/前缀命中）/ regex（正则命中）；`MessageRouter.route()` 的 `finally` 中经 `hooks.trigger_async` 发射 `HookPoints.MESSAGE_CLASSIFIED`（`message.classified`，无订阅者时短路、热路径零开销；必须放在路由内——`Event` 对象与分类回填都在那里，且 `finally` 能汇流「命中 / 未命中 / 终止传播」所有提前 return 路径）。四类事件自此可被 Hook 统一包装，对齐架构图。新增回归测试 `tests/test_event_classify.py`（8 项：枚举 / 初始分类 / 未命中归 all / 命中回填 general·regex / Hook 常量，含轻量 harness 真跑路由回填；真跑 8/8 全过）。
  - **C 项缺陷修复：`message.classified` 曾实现在 dispatch 侧引用未定义的 `ev`**（`NameError: name 'ev' is not defined`）：`_process_event` 里该变量名为 `event` 且是**原始 dict**（即便改名，`event.category` 也会 `AttributeError`），而 `Event` 对象由 `route()` 内部构造、分类只在那里回填。`NameError` 在**构造 payload 字典时**即抛出、与有无订阅者无关，故基准测试下**每条消息都刷一条**「message.classified 扩展点异常」后被 `except` 吞掉。已将该扩展点移入 `router.route()` 的 `finally`，并改用 `hooks.trigger_async`（它是 HookPoint，须走串行/并行语义，而非 `event_bus.aemit`）。新增回归测试 `tests/test_message_classified_hook.py`（4 项**真实驱动 `route()`**：命中普通命令→general、正则→regex、未命中→all、终止传播仍送达，并断言不再出现该 ERROR 日志）；已做基线对照——在修复前的提交上 4/4 失败、修复后 4/4 通过，确认测试真能抓到该回归（旧分类测试只直接测 `Event` 类、从不驱动 `route()`，故漏网）。
  - **启动自检依赖改为可配置**（`main.py`）：`_check_and_install_deps` 读取 `config.yaml` 的 `dependencies.allow_missing`（新增配置段），被豁免的缺失包只记 WARNING、不安装、不打断启动、不退出；仅把"必需子集"写临时 requirements 安装（不再把豁免包塞进整文件安装）。抽出纯函数 `_resolve_self_check_missing` / `_read_self_check_allow_missing` 便于测试。新增回归测试 `tests/test_main_selfcheck_config.py`（5 项：过滤豁免 / 无豁免 / 驼峰-小写归一 / 读配置 / 缺段返回空集；真跑 5/5 全过）。
- **矩形倒置坐标归一化**：`_CanvasPIL.rect` 与 Rust 原生 `rect` 此前对 `x1<x0` / `y1<y0` 行为不一致（PIL 抛 `ValueError`、原生静默 no-op）。已统一为「倒置坐标视为同一矩形」（`rect(300,200,0,0)` == `rect(0,0,300,200)`），并新增回归测试 `tests/test_image_rect_normalization.py`（原生 / PIL 双路，4 项全过）。

### 变更

- **文档站目录重构（中文化）**：46 个平铺 md 按语义重组为 `指南/`（9）、`接口/基础/`（4）+ `接口/高级/`（3）、`进阶/`（11）、`品牌/`（1）、`速查/`（5），各目录补索引页；`config.mjs` 的 nav / sidebar 与首页导航表同步更新，链接校验 225 条零死链。
- 新增 `docs/指南/多语言.md` 并接入侧边栏。

## v1.8.9-beta.3（2026-10-15）

> 主题：**修复 IIPC `mem` 模式建表失败的真实 bug**（功能残缺，非内存优化）。
> 预发布：Linux/amd64 预编译产物随 tag 提供。

### Bug 修复

- **`mem` 模式建表失败（真 bug）**：`--db mem`（字面量）此前落到「只支持极简 SQL 子集的玩具正则内存库」，
  其 `CREATE TABLE` 正则仅匹配 `IF NOT EXISTS`、且 `SELECT` 只支持单列 `WHERE`，**不支持 `CREATE INDEX`/`ORDER BY`/多列查询**。
  结果：内核核心表（`users`/`commands`/`perm_*`…）与所有插件的 `create_table` 静默建表失败 → 表不存在 → 插件启动失败。
  所谓「mem 更省内存」实为**功能残缺的假象**（数据没存进去，自然占用低）。
- **修复方式**：删除该残缺玩具库；`NewStore` 现在将 `""` / `mem` / `:memory:` 统一路由到**真 SQLite 内存引擎**
  （`modernc.org/sqlite`，纯 Go、无 cgo、支持完整 DDL/DML）。`main.go` 的初始化失败回退也改为回退到真 `:memory:` 而非玩具库。
- 新增 `go_kernel/store_test.go` 回归测试：锁死 `mem`/`:memory:`/空 必须返回 `sqliteStore` 且支持 CREATE INDEX / ORDER BY 等玩具库做不到的语句。

## v1.8.9-beta.2（2026-10-15）

> 主题：**IIPC launcher 跨平台扩展名修复** + 正确命名的 Linux 预编译产物。

- `launcher.py` 新增 `_bin_ext()`：Windows 用 `.exe`，其他平台无扩展名；Linux 启动不再寻找 `*.exe`。
- 重新打包 Linux/amd64 预编译产物（无扩展名 ELF）。

## v1.8.9-beta.1（2026-10-15）

> 主题：**IIPC 内核链路合入主框架（源码分发）+ 全 API 面穷举验收 + Web 安全加固（全局限速 / PoW 票据计量化 / 诱饵真实化 / 前端调用型诱饵）**。
> 测试版：与主框架并列的新启动路径，不带开关时主框架行为**完全不变**。

### 新功能

- **IIPC 内核链路（`framework/iipc/`）合入主框架**，以**源码方式**分发（不含编译产物，约 410KB），
  首次运行自动编译（也可 `python -c "from framework.iipc import build; build()"` 预编译）。
  进程拓扑与现有单/双进程模式并列且不同：**Go 内核**（事件总线 / SQLite / 插件编排）+
  **Rust 协议端**（OneBot 11，正向 WS 客户端或反向 WS 服务端）+ **Python 插件独立进程**
  ——DB 在 Go 侧执行，不占 Python GIL。
  - 启动：`python main.py --iipc`（等价 `python -m framework.iipc`）。
  - 插件 API 面、Event 对象、SQL 方言（`%s`→`?`）、核心表、cron、多轮会话、
    群级插件开关、节点权限均**1:1 对齐**现有框架语义（见 `framework/iipc/README.md` 现状与边界清单）。
- **IIPC 端口/参数可在 `config.yaml` 的 `iipc:` 段配置**（`ipc_addr` / `db` / `admin` /
  `plugins_dir` / `onebot_url` / `onebot_listen` / `onebot_token` / `no_rust` / `max_rss_mb`）。
  优先级：**命令行 > config.yaml > 环境变量 > 内置默认**——本机同时跑着主框架（WebUI /
  OneBot 反向 WS）时可直接改配置避开端口占用，不必动代码。
- **基准脚本端口全部改为动态分配**（`tools/portutil.py`，bind :0 向 OS 要空闲端口），
  不再写死 18777/18781/18785；一键总跑 `tools/run_all_bench.py` 末尾新增**性能三口径汇总**
  （① 轻载延迟 P50/P95/P99，取**入队时刻**起算；② 饱和吞吐条/秒，按**端到端 span**算；
  ③ 丢失率与 15s 级卡顿检测）。

### 修复（均由拖真插件 / 穷举调用暴露，非读代码推断）

- `Event.has_perm` **参数错位**：`group_id` 被当作 `context` 传入（int 调 `.items()` 抛错又被
  `except: return False` 吞掉），且漏传 `role` —— 表现为「超管也判无权限」而非报错。
  已按原框架 `context_from_event` 构造 `{bot, group, msgtype}` 并注入 `self.role`。
- IIPC 插件加载**未注入模块级 `ctx`**（原框架 `loader/base.py` 会做 `module.ctx = ctx`），
  导致官方示例「handler 里裸用 `ctx`」的写法直接 `NameError`。
- 补 `ctx.create_table`（官方签到示例建表第一句即用到，原框架经 `ctx/db.py` + `ipc/remote_db.py` 转发）。
- 补 `Event.is_group` / `is_private` / `sender_nickname` / `sender_card` / `at_all`；
  `first_image` 等 `first_*` 无值时由 `None` 改为 `{}`（对齐原框架）。
  > 该缺口在 `plugins/llm_blacklist` 上表现为「能跑」（它用 `getattr(event, "is_group", False)` 兜底），
  > 在官方签到示例上表现为 `send_msg` 静默不发——同一缺口在不同插件上症状完全不同。

### 测试

- 新增 `tools/official_plugin_check.py`（32 项）：把 `docs/writing-plugins.md` 的签到示例
  与真实 `plugins/llm_blacklist` **逐字**拖进 IIPC 内核跑。
- 新增 `tools/api_surface_check.py`（154 项）：反射枚举 SDK 全部公开面
  （PluginContext 68 + ActionProxy 24 + Event 属性 37/方法 8）逐项真实调用；
  **插件只报真实返回值，判定写死在脚本侧**，避免自证。
- 全量回归 **240 项**（e2e 25 + official 32 + surface 154 + adapter 13 + stress 16 + latency）。

### 其它

- `main.py` 新增 `--iipc` 分支（插在「未知选项报错」之前），usage 补充 IIPC 参数段；
  `.gitignore` 忽略 IIPC 编译产物（`go_kernel/*.exe`、`rust_adapter/target/`、`plugins_dat/`）。

### Web 安全加固（用户实测复现后落地）

> 四个问题均来自用户在真浏览器抓包 + 黑盒压测中的复现，已逐项源码核实并修复。详见 [安全加固](docs/速查/安全加固.md)。

- **全局限速闸门（`framework/api/ratelimit.py` + `webapp._global_rate_guard`）**：每 IP 滑动窗口 + 全局令牌桶（QPS）+ 路由分档，**覆盖全部 `/api/*`**（此前 138 个端点仅 2 个有限速，可秒刷）。超限返 `429` + `Retry-After`。配置 `security.rate_limit`，默认内网 / 回环豁免。
- **PoW 票据计量化（`human_check`）**：ticket 绑定签发 IP（跨 IP 作废）、单次签发限速、用量计数超上限（默认 200）作废需重解——解决此前「一次验证无限复用」的买断制。
- **诱饵真实化（`honeypot`）**：命名去 `/api/v1/` 改 `/api/`（真实端点零 v1，避免一条正则废整张表）；陷阱 token 长度 `512 → 2048` 对齐真实会话令牌（实测 `len==2048`）。
- **前端调用型诱饵（关键）**：新增 `webui/public/js/zc-decoy.js`，前端主动 `GET` 四个伪端点（`/api/internal/{telemetry,bootstrap,feature_flags,debug-config}`），让真实浏览器抓包流量混入真诱饵；`zc-decoy.js` 源码埋 `/js/local.debug.js` 受控 honeytoken 路径，扫描器 `fetch` 即被拉黑。此前诱饵前端从不调用，deception 等于零。
- 早期重复造轮子的 `shadow.py` / `honeytoken.py` 已废弃删除，防御逻辑统一收敛到 `honeypot` + `ratelimit` + `human_check` 三件套。
- 新增 `tools/security_check.py`（15 断言全绿）：高并发必被拦 / 票据绑 IP 计量 / 诱饵命名与 token 规格 / 前端 decoy 良性。

## v1.8.8（2026-10-13）

> 主题：**PyPI 文档修正**。

- README 的相对链接（`CHANGELOG.md` / `docs/*.md` / `LLM.md`）改为**绝对 GitHub URL**——此前在 PyPI 项目页会被解析成 `pypi.org/project/zcbot/<版本>/docs/...` 而**全部 404**（GitHub 上正常，PyPI 不解析相对路径）。
- README 补 pip 安装方式（`pip install zcbot`），版本号对齐当前发布；`LLM.md` / `docs/installation.md` 同步补 pip 说明。

## v1.8.7（2026-10-13）

> 主题：**双核心 IPC 性能续优化 + 跨进程事务修复**。

### 性能
- **宿主侧事件批量注入**：`IpcAdapter._on_event` 对批量帧**整批一次** `run_coroutine_threadsafe`，跨线程调度从「每事件一次」降为「每批一次」（实测 ~23.8µs/条 → ~0.01µs/条），且天然保序。
- 承接 v1.8.6 重发的 core→host **事件批量传输** `_EventBatcher`（满 `event_batch_max`=64 条 / `event_batch_delay_ms`=5ms 即 flush）与 **`acall` 原生异步化**；两者叠加后事件路径的帧数与线程调度开销大幅下降。

### 修复
- **跨进程事务 op 分派**：`db.transaction()` 块内调用 `table_exists` / `table_info` / `table_has_column` / `pool_status` / `execute_many` 会抛 `TypeError` 或「未知事务操作」——旧 tx RPC 固定签名 `(tx_id, op, sql, params)` 接不住元数据/批量参数，且 op 分派未覆盖这些操作。改为与 `db.*` 路径一致地**透传 kwargs** 并补齐全部 op（元数据类自建 SQL，`pool_status` 回落真实 db，`execute_many` 取 `params_list`）。新增回归 `test_remote_tx_run_all_ops`。
- CI：`test_bind_ipc_dispatch` 适配事件攒批新契约。

## v1.8.6（2026-10-12）

> 主题：**WebUI 安全加固收口——端口隔离 + 纯 API 模式 + 黑盒测试启动器 + 安全等级体系 + 文档修正**。

### 新功能
- **WebUI 端口隔离（双端口模式）**：`config.yaml` 的 `web.api_port` 可配置独立 API 端口。设置后，**WebUI 端口只提供前端页面 + 蜜罐诱饵，不再暴露任何真实 `/api/*`**；真实 API 全部搬到 `api_port`。两者同进程，黑名单 / 人机验证票据 / 会话状态天然同步——适合把 WebUI 端口暴露在公网、API 端口留在内网的部署。
  - 路由层 `_port_split_guard` 在 WebUI 端口命中 `/api/*` 时一律按诱饵处理；`webserver.py` 另起一个同进程线程只跑真实 API。
- **纯 API 模式（`web.ui_enabled: false`）**：只起 API 端口（取 `web.api_port`，未设则用 `web.port`），**完全不启动 WebUI 前端 / 蜜罐**，本进程只提供真实 `/api/*`。适合「只开 API、不开 WebUI」的纯接口部署。
- **三种部署模式**正式确立并写入文档：
  1. **单端口合并**（不配 `api_port`）：WebUI + 真实 API + 诱饵同端口（文档仍标注真实接口，不伪装）；
  2. **双端口隔离**（`api_port` 与 `web.port` 不同）：WebUI 端口无真实 API、API 端口专属真实接口；
  3. **纯 API**（`ui_enabled=false`）：仅 API 端口，无 WebUI。
- **黑盒测试启动器 `tests/webui_blackbox_launcher.py`**：给安全审计 / AI 黑盒测试用的「从零开始、凭据随机、环境隔离」启动器。随机生成管理密码与端口（均可参数覆盖），写入隔离的临时 `config.yaml` + 临时 SQLite 库（**绝不碰真实 `data/zcbot.db`**），拉起框架待端口就绪后把随机密码以 bcrypt 注入 `admin_users`，打印访问地址与账号密码并尝试打开浏览器；Ctrl-C 自动清理临时文件。支持 `--host/--port/--api-port/--security/--username/--password/--api-only/--no-open/--keep/--timeout/--print-only/--root`，跨平台（Windows / Linux / macOS）。
  - 修复框架强依赖仓库 `core_plugins.yaml` 的 `webui.port`、会覆盖 `config.yaml` 的 `web.port` 导致端口随机失效的问题：新增环境变量 **`ZCBOT_CORE_PLUGINS_YAML`** 覆盖插件配置路径，启动器借此把插件配置指向隔离副本（仅覆盖 `webui` 端口/host），**真实仓库零改动**。
- **安全等级体系（`security.level`）**：`off / low / medium / high / paranoid` 五档统管蜜罐诱饵集、tarpit 延迟、自动拉黑、安全响应头、Server 指纹隐藏、JS 人机验证、认证静默丢弃；单项（honeypot / headers / human_check）仍可显式覆盖，显式值优先。

### 修复 / 文档
- 修正安全相关文档：此前暴露了 WebUI 的 API 端点结构；现已明确「WebUI 端口不绑定真实 API、真实 API 是独立端口」的设计，并补齐三种部署模式说明。
- 默认管理员账号 `admin/admin123` 仅在隔离/首次初始化时播种；密码以 bcrypt（`gensalt(12)`）存储于 `admin_users` 表，随机密码注入即覆盖该哈希。
- `framework/config.py` 支持 `ZCBOT_CORE_PLUGINS_YAML` 环境变量覆盖 `core_plugins.yaml` 路径（默认仍读仓库根目录，行为不变）。

### 重发补充（2026-10-13）

> 本版 tag 曾因随附一份内部安全方案文档而撤回；以下为清理与后续改进，重发同一版本号。

- **移除内部文档 `webui-security-plan.html`**：该文件含完整防御设计（静默丢弃 / 人机 PoW / 蜜罐 canary / 双请求防破解 / 各档位配置键 / 三种部署模式），属内部「作战手册」，不应随公开仓库外传。已从工作树移除，并删除/重打旧 tag。
- **反向代理下获取真实客户端 IP（文档 + 配置）**：`security.trusted_proxies` 说明；`docs/deployment.md` 新增「FRP 内网穿透」小节；反向代理示例的 `X-Forwarded-For` 由追加式 `$proxy_add_x_forwarded_for` 改为覆盖式 `$remote_addr`（`get_client_ip` 取 XFF 第一段，追加式会被客户端伪造绕过风控）。
- **双核心 IPC 性能优化**：
  - `acall` 原生异步化：`asyncio.Future` 直挂 pending，去掉 `asyncio.to_thread` 线程池跳转（单次往返约 132µs → 88µs）。
  - core→host **事件批量传输**（新增 `_EventBatcher`）：满 `event_batch_max`=64 条或到 `event_batch_delay_ms`=5ms 即 flush；基准 `tools/bench_db_path.py` 显示把「每事件一帧」降为「每批一帧」，N=2000 时 87µs/次 → 1.1µs/次（约 80×）。
  - 新增配置 `dual_process.event_batch_max` / `event_batch_delay_ms`；新增基准脚本 `tools/bench_db_path.py`。

## v1.8.5-alpha.1（2026-10-10）

> 我靠我脑残发布1.8.5-alpha.0忘了推送了。
> 本版本即 alpha.0 本应发布的内容（核心架构债务拆分任务2 + aiwriter 官方插件 + 框架内置调试控制台 + CI 工作区护栏），补打 tag 并推送远端、创建 GitHub Release，无额外代码改动。

## v1.8.5-alpha.0（2026-10-09）

> 主题：**核心架构债务拆分（任务2）+ 新增 `aiwriter` 官方插件（ZCBOT 插件开发智能体）+ 框架内置调试控制台 + CI 工作区护栏**。
> 本批为纯内部收敛与新增，公开 API / 服务名 / 配置键 / 数据库 schema 全部不变，老插件零改动。
> 这是 **alpha 预发布**，用于提前试用；稳定后再出正式版。

### 重构 · 架构债务拆分（任务2）
- **调度器收敛为单一权威实现**：`framework/scheduler.py` 的 `TaskScheduler` 成为唯一实现，`core_plugins/scheduler/main.py` 退化为薄封装（再导出 + 以官方插件形态注册 `services['scheduler']`）。旧导入路径 `core_plugins.scheduler.main.TaskScheduler` 与 `framework.scheduler.TaskScheduler` 指向同一类；`start/stop/add_plugin_task/remove_plugin_tasks/remove_task/get_jobs/pause_task/resume_task` 及实例属性 `_scheduler` / `_plugin_tasks` 全部保留。插件重载（重复 `register`）先停旧实例，停用（`unregister`）同步摘除服务注册。
- **服务就绪回调机制**：`ServiceRegistry` 新增 `call_when_ready(name, callback)` / `cancel_ready(handle)`——服务已注册时立即同步触发，否则挂起待 `register` 时触发（每句柄只触发一次，回调异常隔离不外抛）；`register(name, None)` 也会触发，供等待方「收到 None 即告警跳过」而非无限挂起。取代原先的固定延迟重试。
- **session 插件改为就绪回调注册**：清理任务经「调度器服务就绪」回调注册且恰好一次；停用后无残留（挂起回调 / 已注册任务 / 原始消息处理器 / 服务注册全摘除），彻底移除固定延迟定时器（`Timer`）实现，消除「停用后迟到注册任务」的竞态。
- **LogCoalescer 停止收敛**：`stop(timeout)` 置停止信号后立即唤醒后台 flush 线程并有限等待退出（幂等）；框架 `stop()` 与宿主退出统一停掉日志合并器后台线程，不再滞留一个窗口周期。合并/去重决策不变。
- **核心进程 IPC 服务装配抽离**：新增内部模块 `framework/ipc/core_services.py`，把 `db.* / api.call / api.send_text / bots.list / tx.* / route.register / 宿主日志桥接 / 事件分发替换` 的接线从 `CoreRuntime` 抽出；`CoreRuntime` 保留同名薄委托方法，RPC 名 / 参数 / 返回值 / Flask 行为与抽取前完全一致，协议格式 / 传输 / 心跳 / 重连均不动。模块级仅依赖标准库，`flask` / `framework.api` / `framework.log_broker` 延迟导入——单进程（`dual_process.enabled=false`）路径不加载 IPC 组件。

### 新功能
- **框架内置调试控制台（无 TTY 也能接进运行中的框架）**：启动时在 `127.0.0.1` 上监听一个本地端口，**端口与超长随机 token 自动生成并存入数据库**（`console_access` 表，默认 16384 bit = 2048 字节，可配），之后每次启动复用。本地执行 **`python main.py attach`（简写 `-a`）** 即可接入，直接使用框架的终端命令（`help` / `status` / `plugins` / `reload` / …），**无需重启、无需 TTY**——systemd 托管场景下的调试入口。协议为 JSON Lines，token 用常量时间比较；只绑定回环地址。
  - 端口复用带重试（重启太快、旧进程还没释放时不会立刻换端口）；若该端口上**已有本框架的控制台在跑**（多实例场景），新实例**放弃监听且不覆盖凭证**，避免把先跑那个实例的 attach 入口打掉。
  - 配置见 `config.yaml` 的 `console` 段（`enabled` / `host` / `port` / `token_bits` / `backlog` / `read_timeout`）。
- **`config.yaml` 补 `terminal` 段**：`enabled` / `panel_autostart` / `panel_refresh` / `panel_default_view` 全部显性化（此前只在本地 config.yaml、未进默认模板）。
- **官方插件 `aiwriter`（AI 智能体 CLI，默认关）**：把 AI 编码 / 运维智能体做成**终端界面 + 独立 CLI**。两种入口共用同一界面：**独立 CLI `python main.py code`**（命令行直接启动、不启动 bot）与 **bot 运行中在终端输入 `code`**（以独立进程拉起界面）。智能体自带一份最小 LLM 客户端（OpenAI 兼容流式）+ 工具集 + Agent 工具调用闭环，**完全不依赖 `llm_core`**。
  - **全屏界面（Textual）**：用 **[Textual](https://github.com/Textualize/textual)（MIT）** 承载——成熟的 TUI 框架负责输入 / 渲染 / resize / 键位 / 弹窗，不再手搓 ANSI 重绘与按键解析（那套在 Windows 控制台上极易卡死）。布局：顶栏（人格 · 模型 · 模式）+ 对话流 `RichLog` + **右侧边栏（人格列表 + 运行状态）** + 输入框 + 键位栏；设置为模态 `SettingsScreen`。键位：Enter 发送 · **Tab 切模式** · Ctrl+C 退出 · Ctrl+L 清屏 · Ctrl+S 设置 · Ctrl+G 人格（F1/F2/F3 仅作别名——部分 Windows 控制台会抢 F 键，如 F3=重复上一条命令）。依赖见 `core_plugins/aiwriter/requirements.txt`。
  - **界面独立进程**：bot 内 `code` 命令以**独立进程**拉起界面（`python main.py code`），与「TUI 独立于后端」一致，彻底避开在 bot 线程里跑全屏 TUI 的坑；退出后回到 bot 终端。
  - **定位：专门给 ZCBOT 写插件的智能体**：系统提示重写为「运行环境 / 沟通 / 写 ZCBOT 插件 / 在代码库里工作」四节，并**自动注入仓库的插件开发要点**（`docs/llm-plugins.md` + `LLM.md`，进程内缓存截断），因此它按 `__plugin_meta__` / `ctx.command·on·hook·register_api` / `ctx.get_data_dir()` 等框架约定写插件，而不是猜。
  - **工作区缺省 = 框架根目录**（直接在项目里干活），可 `aiwriter.workspace` 改；临时文件统一写到 `data/tmp/`。二者都会写进系统提示，避免污染工作区。
  - **长期记忆 + 历史归档**（`data/plugins_dat/core_aiwriter/`）：`memory.md` 每轮注入系统提示，`/memory [add|clear]` 或新增的 `remember` 工具写入；`history.jsonl` 逐轮归档，`/history [N]` 回看。
  - **新增工具 `plugin_new`**：直接生成 `plugins/<名>/` 骨架（main.py + README.md），配合 `/plugin <名>` 与 `write/edit` 完成插件落地。
  - **设置界面**（`/settings` · Ctrl+S）覆盖：人格 / 模型 / 温度 / 最大轮数 / 模式 / 是否允许执行 / 工作区 / 压缩保留轮数 / 压缩阈值 / 历史硬上限 / 输出截断 / shell·python·网络超时 / 接口地址 / API Key，共 16 项，可滚动，保存即写回 `core_plugins.yaml` 并即时重建工具集与会话存储。
  - **人格（agent）可配置 + 编辑弹窗**：多个人格各自带附加系统提示（可选覆盖 `model` / `mode`），默认内置 `build`（ZCBOT 插件开发）。侧栏切换，或 `/agent`：`new`/`edit` 打开**编辑弹窗**（名称 / 描述 / 多行附加提示 / 覆盖模型 / 覆盖模式，`F4` 也可打开），`add`/`prompt`/`del` 走命令行。存在 `aiwriter.agents`，当前人格记在 `aiwriter.agent`。
  - **深度思考（reasoning）支持**：LLM 客户端解析 `reasoning_content` / `reasoning` / `thinking` 等字段为 `reason` 事件；`thinking` 开关按 `thinking_style`（`auto`/`anthropic`/`flag`/`openai`）注入请求字段，配合 `deepseek-reasoner` 这类模型即可。
  - **防刷屏（默认收敛）**：工具调用默认只显示一行（`⚙ write` / `✔`），不展开参数与结果；思考过程默认**折叠成一行摘要**（`💭 思考：…（共 N 字）`）。`/detail [on|off]`（或 Ctrl+T）展开细节/思考全文，`/tools [on|off]` 整体显示或隐藏工具行，`/think [on|off]` 控制是否请求深度思考。
  - **会话持久化开关**：`aiwriter.persist_session`（默认 true）。置 false 后每次启动都是全新会话；`/new` 清当前会话（不动历史归档与长期记忆）。
  - **多会话管理**：会话存储升到 **v2 格式**（`{version, sessions:{key:{name,created,updated,model,mode,messages}}}`，旧格式自动迁移）。`/sessions`（或 `Ctrl+N`）打开**会话管理弹窗**：列举旧会话（● 当前 / 轮数 / 字符数 / 更新时间 / 覆盖项），`Enter` 进入 · `n` 新建 · `r` 重命名 · `c` 配置（会话级覆盖模型与模式）· `Del` 删除；命令行版 `/session new|<名>|del|rename|model|mode`。删除当前会话会自动切到剩下的最近一个；进入会话时会回放最近几轮。
  - **独立模式也能查库**：`python main.py code` 是无框架宿主的独立进程，此前 `db_query` 只能报「数据库不可用」。现在按 `config.yaml` 的 `database` 段以**只读**连接打开库（SQLite `mode=ro`，避免与运行中的 bot 争写锁）：`db_query` 可用、`db_execute` 明确拒绝并给指引。`plugin_list` 在独立模式改读 `core_plugins.yaml` 的启用列表（真实可用），`plugin_action` 只给指引不代管。
  - **工具集（16 个）**：`read` / `list` / `glob` / `grep` / `webfetch` / `websearch` / `db_query` / `plugin_list` / `write` / `edit` / `remember` / `plugin_new` / `shell` / `python` / `db_execute` / `plugin_action`。`edit` 采用 `oldString → newString` 定点替换语义（默认须唯一命中，可 `replaceAll`）；`websearch` 默认走免 key 的 Bing（配 `FIRECRAWL_API_KEY` 则用 Firecrawl）；数据库 / 插件类工具经宿主桥复用框架能力，独立 CLI 下明确提示不可用。
  - 权限闸门按「能力 × 模式」：`readonly`（只读 + 网络 + 只读 DB）/ `workspace`（+ 写）/ `full`（+ shell / python / DB 写 / 插件管理，需 `allow_exec`）；`specs()` 只暴露当前允许的工具，调用前再强制校验；所有文件路径以工作区根为边界。界面里 **Tab** 循环模式（切到 full 自动开 `allow_exec`，离开时关），输入框占位符与侧栏同步显示当前模式。
  - 系统提示三节式（harness / communication / codebase）+ 人格附加段，按开放的工具动态拼装；长会话超阈值把较早对话压缩成摘要（compaction），`session_hard_cap` 控制历史硬上限。
  - 配置段 `aiwriter`（`base_url` / `api_key` / `model` / `temperature` / `max_rounds` / `timeout` / `mode` / `allow_exec` / `workspace` / `agent` / `agents` / `keep_turns` / `max_history_chars` / `session_hard_cap` / `max_output` / `shell_timeout` / `python_timeout` / `net_timeout` / `session_key`），`api_key` 留空时读环境变量 `AIWRITER_API_KEY`。插件归属进程为 **core**（终端在核心进程）。
  - 修掉旧「AI 编写」形态的隐患：旧插件以 `spec_from_file_location` 被框架以顶层模块加载时，`agent.py` 的相对导入会直接失败（该插件从未能被框架真正加载）；新实现用「相对导入 + 同目录绝对导入」兜底，两种加载方式均可用。

### 工程 / CI
- **工作区护栏**：新增 `.github/scripts/guard_workspace.sh`（污染守卫 + 测试残留守卫），由 `ci.yml` 与 `tests.yml` 在测试步骤后调用——拦截工作流留下的未忽略新文件/跟踪文件改动，以及 `test_perm` / `test_api_security` 的临时库残留、`render_test*` / `*.bak` / `*.orig` / `*.rej` / `*.tmp.*` 等手工/合并/编辑器残留。
- **WebUI 构建检查作业**：`ci.yml` / `tests.yml` 新增 `webui-build`（`npm ci` + `vite build`），验证锁文件驱动下前端可安装、可生产构建（不跑污染守卫，避免内容哈希漂移误报）。

### 修复
- **终端中文乱码（Windows）**：环境设了 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（Python 以 UTF-8 输出）而控制台代码页是 936（GBK）时，中文输出被按 GBK 解码即乱码；`msvcrt.getch()` 读到的也是代码页字节。新增 `framework/terminal/encoding.py`：`ensure_utf8_console()` 优先把控制台代码页切到 65001（与 Python 的 UTF-8 模式对齐），失败时 `align_stdio_to_console()` 把 stdio 对齐到控制台代码页兜底。`main.py` 启动即对齐；`code` 界面进入时再对齐一次、退出还原。
- **插件子模块同名撞车**：框架对 core 插件按文件加载且不建包，插件若用 `from agent import ...` 之类的顶层绝对导入兜底，会撞上其它插件的同名模块（如 `plugins/llm_core/agent.py` → `cannot import name 'Agent' from 'plugin_llm_core.agent'`）。框架 `_load_core_plugins` 现在按用户插件同样的方式把 core 插件建成包（`submodule_search_locations`），插件内相对导入直接可用；aiwriter 亦保留"自补包属性"的兜底。
- **全屏界面（`tui` / `code`）与终端冲突**：Windows 控制台默认的 `ENABLE_PROCESSED_INPUT` 会让 Ctrl+C 变成系统信号——在全屏界面里按 Ctrl+C 会把整个 bot 干掉；`ENABLE_LINE_INPUT` / `ENABLE_ECHO_INPUT` 则让按键要等回车、且回显与界面重影。`TerminalPanel._enter/_leave` 现统一关掉这三项、退出还原：Ctrl+C 变成普通按键（界面自己处理退出，不再影响 bot），按键即时送达、不回显。内置 `tui` 面板与 `code` 界面同时受益。
- **`code` 界面响应与可观测性**：改为「有变化立即重绘、否则最多每 0.2s 一次」，输入轮询收紧到 0.02s，键入不再迟钝；渲染异常首次记日志（不再被静默吞掉）；界面期间临时静音控制台日志处理器，避免其它线程日志与画面互相覆盖。
- **`code` 界面卡死**（render-on-demand / 最小帧更新）：
  - **增量绘制**：只重写「相对上一帧有变化的行」（绝对光标定位 + `\x1b[K`），不再每帧整屏刷——流式输出时通常只动底部 1~2 行，cmd.exe 这类终端不再被全量 ANSI 刷屏拖死；
  - **关掉鼠标上报**：界面不用鼠标，`\x1b[?1000h/\x1b[?1006h` 开着会让鼠标移动刷出大量转义序列、把输入解析拖住；
  - **收紧转义序列读取**：未知序列的最长等待从 32×0.05s≈1.6s 降到 8×0.01s≈80ms；
  - **正文只换行窗口所需的那几行**（从尾部往前凑），历史不再做每帧 O(n) 全量换行；对话历史上限 4000 行、上滚上限 5000 行；
  - **单次循环耗时看门狗**：>2s 记一条日志，便于定位卡顿。

- **SQLite 方言清理误伤 `PRIMARY KEY CHECK (...)`**：`_strip_mysql_ddl_syntax` 里「`KEY <索引名> (<列>)` → 删除」这条规则会连 `PRIMARY KEY CHECK (id = 1)` 里的 `KEY CHECK (id = 1)` 一起吃掉，把建表语句截成 `id INTEGER PRIMARY` → `near ",": syntax error`，DDL 静默失败。已给该规则加负向断言（`CHECK` / `CONSTRAINT` / `PRIMARY` / `FOREIGN` / `UNIQUE` 不吃），并补回归测试。
- **`main.py` 参数校验**：未知选项 / 不存在的配置文件不再被静默当成「配置文件路径」。历史坑：`python main.py argparse` 会在项目根目录生成一个叫 `argparse` 的默认配置文件，`python main.py -a` 则被当无事发生直接启动。现在未知选项、多余位置参数、不存在的配置路径都会打印用法并以退出码 2 结束；新增 `--help` / `--version`。
- **调试控制台在 MySQL 部署下凭证静默落库失败**：写入语句用了 `?` 占位符，而框架的方言约定是 `%s`（SQLite 由框架翻成 `?`，MySQL 保持 `%s`）。结果建表成功但 INSERT/UPDATE 抛 `not all arguments converted during string formatting`，凭证只在内存里、**没进库** → 重启后端口/token 会变、`attach` 取不到。已改用 `%s` 并补回归测试（测试替身也按框架语义翻译占位符，避免再次漏检）。
- **`python main.py attach` 只认 SQLite**：凭证读取硬编码 `data/zcbot.db`，MySQL 部署下报「未找到控制台凭证」。现在按 `config.yaml` 的 `database` 段自动分派（`sqlite` / `mysql`），无需手工填端口与 token。
- **部署工具**：新增 `tools/deploy_remote.sh`（本地 → 服务器代码部署：只同步代码、排除原生库与配置数据、备份、按目录全路径重启、自动验收），`docs/deployment.md` 补「远程部署脚本」与「无 TTY 接入运行中的框架」两节。

### 测试
- 新增 `tests/test_console_server.py`（6 例）：SQL 方言 `PRIMARY KEY CHECK (...)` 不被误删（并能真正建表）、`KEY/INDEX/UNIQUE KEY` 仍被正确清理、调试控制台端到端（端口+token 落库、错误 token 拒绝、命令回传、重启复用端口与 token）、`load_credentials` 读库。
- 新增 `tests/test_task2_arch_split.py`（32 例）：调度器单一权威实现与旧导入路径、服务就绪回调语义、session 就绪注册与停用无残留、LogCoalescer 停止流程、`CoreRuntime` / `core_services` 装配面与远程 stub 视图 Flask 契约、单进程路径不加载 IPC（子解释器验证）。
- 新增 `tests/test_aiwriter.py`（55 例，离线不联网）：框架 loader 顶层模块加载路径与模块同名撞车、工具能力闸门与 `specs` 按模式过滤、`edit` 唯一性语义、扩展工具（DB / 插件 / Python / 网络 / remember / plugin_new）、独立模式只读库与插件工具指引、插件开发要点与长期记忆注入、历史归档读写、多会话存储与 v1→v2 迁移、会话切换/删除/覆盖、人格 CRUD 与编辑弹窗、会话管理弹窗、工作区缺省为框架根目录、会话持久化开关、深度思考事件与请求体、显示开关与思考/工具渲染收敛、Textual 界面、`code` 在远程会话下拒绝。
- 全量回归：CI 显式 pytest 套件（含新增文件）全绿。

---

## v1.8.3（2026-10-06）

> 主题：**划水版**。顺手加了个小功能，其余无改动。

- **插件 WebUI 依赖归组契约**：`GET /api/menu` 的插件项新增 `host` 字段——若某 webui 插件的 `plugin.yaml` 依赖了另一个同属 webui 的插件，`host` 指向该宿主名，前端据此把子插件自动归入宿主节点下（如 `llm_draw` 依赖 `llm_core` → 归入其下）。依赖自身无侵入，前端不再需要自行反查依赖图。

---

## v1.8.2（2026-10-05）

> 主题：**凑业绩**。本版本无新功能、无行为变更——框架运行时行为与 v1.8.1 完全一致。
> 只是把工程地基上一直缺的两块补上：CI 门禁，以及安全面的真实行为回归测试。

### 工程 / 测试

- **CI 门禁**：新增 `.github/workflows/ci.yml`。Python 3.10 / 3.12 / 3.13 / 3.14 四版本矩阵，先 `compileall` 编译检查 `framework` 与 `core_plugins`，再跑 pytest。
  - 采用**显式 pytest 文件列表**而非 `pytest tests/`：仓库内混有脚本式自测（`test_perm.py`、`test_plugin_imports.py` 在模块顶层 `sys.exit`），整目录收集会直接中断。
  - 额外安装 `pytest-asyncio`（锁 `<1.0`，避开 1.0 默认模式变更）与 `pytest-timeout`，并开 `asyncio_mode=auto`——否则缓冲层的 `async def` 用例会被报 "async def functions are not natively supported"。
  - 本地按同一条命令实跑：**253 passed**，退出码 0。
- **安全面行为回归测试**：新增 `tests/test_api_security.py`（7 例）。用真实 `Framework()`（SQLite 临时库、官方插件全关、不 `start()` 以避免拉起线程）+ `create_web_app()` + `test_client()` 发真 HTTP，覆盖：无 token 一律 401；敏感表 `admin_users` / `api_tokens` 对普通管理员隐藏且 schema/rows 返回 403；super-only 高危端点收敛；登录失败文案统一（防用户名枚举）；普通管理员下载 `.db` 文件被拦。
  - 与既有的 `test_security_hardening.py`（源码字符串匹配）互补：那份验"代码里写了"，这份验"运行时真的生效"。
- **修复 pytest 收集崩溃**：`tests/test_perm.py` 原先在模块顶层执行 `sys.exit(...)`，`pytest` 一收集即崩（`INTERNALERROR ... SystemExit`）。执行逻辑已整体移入 `if __name__ == '__main__':`，`python tests/test_perm.py` 直接跑的行为不变。

---

## v1.8.1（2026-10-02）

> 主题：**运维终端能力内置化（删 `ops` 官方插件）+ LLM 子系统（装载器 + zip 载荷 + 多人格）+ 事件按会话分片并行 + debug 存储内存缓冲 + 插件市场声明式安装位 + 接入端契约收尾与三处线上修复**。

### 新功能
- **接入端契约与规范消息段**：内核新增 `framework/messaging/segments.py`（规范消息段）与 `framework/messaging/contract.py`（能力常量 / 事件补齐 / 契约自检）。各协议对"一条消息"的表达差异（OneBot 的 `record`、Telegram 的 `photo[]/voice`、Discord 的 `attachments[]`、QQ 官方的 `msg_type`+`media`）在内核收口为一套规范段：`ev.segments` 恒为 `[{'type','data'}]`，语音一律叫 `voice`，文件引用统一用 `ev.media_url(seg)` 取。插件不再需要翻 `event['raw']` 摸底层协议。
  - `ProtocolAdapter` 新增可选钩子：`capabilities()`（能力自述）/ `normalize_incoming()`（原生→规范）/ `to_native()`（规范→原生）/ `finalize_event()`（事件补齐），基类均有兜底，最小接入端不实现也能跑。
  - 能力查询 `adapter.supports('voice','in')` 让插件"问了再发"，而不是发出去才发现不支持。
  - 通知事件同时广播协议原名与规范名（如 `notice.group_increase` 与 `notice.group_member_increase`），`notice_type` 保持原名不动、规范名放 `notice_type_canonical`，既有订阅不受影响。
  - 接入端注册时自动跑 `validate_adapter()`，不合规直接点名告警（只告警不拦人）。
  - Telegram / Discord 适配器的附件不再被压成纯文本，改为产出真实消息段；Telegram 出站升级为通用媒体发送（图/语音/视频/文件/贴纸）。
  - 文档见 `docs/adapter-contract.md`，测试 `tests/test_message_contract.py`（80 条）。
  - 收尾：`media_ref()` 同时接受整个消息段或裸 `data` 字典（容错，测试 `test_media_ref_accepts_data_or_segment`）；WebUI 插件 API 路由与文档同步微调。
- **LLM 子系统（可选，默认关）**：新增官方插件 `core_plugins/llm_load`（装载器）+ 用户插件 `plugins/llm_core`（对话核心）。`llm_core` 源码真源在 `core_plugins/llm_load/src/llm_core/`，经 `python tools/build_llm_payload.py --write` 打包为 `llm_core.zip` 载荷，框架启动时由 `llm_load` 按 manifest 校验/释放/自愈到 `plugins/llm_core`（运行时目录改动会被 manifest 抹平）。提供模型提供商总线、函数（工具）调用与 Agent 循环；插件经 `fw.services.get('llm_core')` 取服务、`@svc.tool(...)` 注册函数、`svc.register_provider(...)` 注册提供商。文档见 `docs/llm-chat.md`、`docs/llm-plugins.md`。**v1.8.1 补充：多人格预设**——`_conf_schema.json` 新增 `personas` 数组（每项 `{id,name,prompt}`），会话内 `/llm人格 <id>` 即时切换、`/llm人格 reset` 恢复默认，人格提示词按会话持久生效（测试 `test_persona_override`）。
- **事件按会话分片并行**：`event_queue.workers > 1` 时启用单一分发器（`_event_distributor_loop`）从事件缓冲逐条取事件、盖内部唯一代号 `_seq`，按会话键（群/用户/bot）哈希投入对应 worker 队列——同群/同用户事件 FIFO 保序，跨会话并行处理；`workers=1` 时 worker 直连缓冲，零额外开销。停机以 `_event_pipeline_empty`（三层缓冲 + 全部分片队列）判定，`wait_drained` 的 join 语义覆盖分片在途事件，不丢。
- **插件市场声明式安装位**：`plugin.yaml` 同时声明 `install_target: core_plugins` 且 `official: true` 的市场插件可安装进 `core_plugins/`（其余默认 `plugins/`）。框架内置官方插件名（`config._CORE_PLUGIN_SCHEMA` 键）受保护，禁止经市场覆盖同名目录；下载/加载失败均回滚备份或清理残件。

### 重构
- **运维终端能力内置化**：删除 `core_plugins/ops` 官方插件，其 `restart / shell(sh) / dbdump` 命令迁入框架内置 `framework/terminal/cmd_ops.py`，`reload(rl)` 并入 `cmd_plugin.py`；新增交互式运维面板命令 `tui`（`framework/terminal/panel.py`，零第三方依赖、ANSI 备用屏、htop/NTop 交互布局独立实现）。命令配置读顶层 `config.yaml` 的 `ops` 段（`shell_timeout`/`max_output`，缺省即生效）。`config.yaml` 新增 `terminal` 段：`enabled` / `panel_autostart` / `panel_refresh` / `panel_default_view`。

### 性能 / 存储
- **debug 模式行集内存缓冲**：`framework/database/sql_sim.py` 行集常驻内存，写入先进缓冲、按 `database.debug_flush_ms`（毫秒，默认 1000，0=逐次同步落盘）由后台线程合并落盘，正常关停/进程退出（atexit）兜底全量落盘；外部/手工改动 JSON 文件按 mtime 自动重载。新增 `WHERE` 编译为闭包谓词（SELECT/UPDATE/DELETE 共用）、ORDER BY 多列 + 每列 ASC/DESC + 数值序 + NULL 殿后、`LIMIT offset,count` 变体、`INSERT IGNORE` 批内去重。群成员同步与统计写库改为 `execute_many` 批量。
- **debug 模式支持 `ON DUPLICATE KEY UPDATE`（upsert）**：`_exec_create` 解析并持久化 `PRIMARY KEY` / `UNIQUE KEY` 冲突键，`_exec_insert` 据此定位冲突行就地更新（`VALUES(col)` 取待插入值，支持 `IF(cond,a,b)`、`NOW()`、`col=col±N`），命令同步与用户/群/成员统计写库在调试模式真正去重、ID 稳定；解析失败的 ODKU 子句安全退化为普通 INSERT，绝不因不可解析的表达式丢整行插入。配套：调试模式启动即执行 `init.sql` 的 `CREATE TABLE`（`_init_debug`，幂等、不重复跑种子 INSERT），使核心表带上唯一键元数据，避免表由 `INSERT` 惰性建出而无唯一键导致重复累积。

### 修复 / 健壮性
- **qq_official WebSocket 鉴权修复（线上致命）**：Identify/Resume 的 `d.token` 由错误的 `Bot {app_id}.{access_token}` 改为 QQ 官方网关要求的 `QQBot {access_token}`——原格式每轮 Identify 均被网关以 op 9 + 关闭码 4004 拒绝，适配器清 session 后约 5.5s 无限重连并最终触发 gateway 接口频率限制（code 100017），始终进不了 READY。HTTP API 侧格式本就正确，仅长连接受影响。
- **启动自检安装器死锁修复**：`main.py` 缺依赖自检在安装前导入 `framework.loader`，而其依赖链顶层 `import yaml`——缺的恰是 pyyaml，自检器自身先崩、永远走不到安装步骤。改为只导入纯标准库的 `framework.deps.pip`，并把 `framework/deps/__init__.py` 的顶层 `import yaml` 移到唯一使用点延迟导入；模拟无 pyyaml/psutil 环境验证导入链畅通。
- **image_renderer 文本基线 bug 修复**：原生渲染 `draw_text` 的 ymin 符号写反（正确为 `baseline - ymin - height`），导致文字纵向定位偏移；同步重编七平台原生产物，CI 全部 Linux 平台改走 cargo-zigbuild、以 glibc 2.17 为兼容底线（修复 ubuntu-latest 产物要求 GLIBC_2.30 无法在 Anolis 8 / CentOS 系旧 glibc 加载的问题），产物已随本版回填仓库。
- **`/llm` 未配置提供商时回可读提示**：`handle_chat` 捕获 `ProviderError`，回一句「（LLM 未就绪）没有可用的模型提供商…」引导去面板配置 Key，而非让命令处理器抛异常静默无响应。
- **运维面板 reload 走宿主进程转发**：`framework/terminal/panel.py` 的 `_call_command` 按命令 `target` 路由，`target: host`（如 `restart`/`reload`）在核心进程经 IPC `request_host('terminal.exec', …)` 转发到宿主执行，`both` 先本地再宿主，修复双进程模型下面板重载只作用于核心进程、宿主插件不刷新的缺口。
- **文档与注释纠偏**：`llm_load`/`llm_core` 载荷释放的触发条件由「加 `__version__` 版本号」更正为「按逐文件 md5 比对，内容变化即生效」，同步修正 `build_llm_payload.py` 提示、两处 README 与 `docs/llm-chat.md`；`providers.py` 示例导入改为运行时可用的 `from plugin_llm_core.providers import …`；`/llmtools` 输出补列每个工具的必填参数，便于核对 schema 推导。

### 测试 / CI
- 新增 `tests/test_llm_core.py`（18 项，离线不联网，含「无提供商回可读提示」回归）、`tests/test_llm_load.py`（7 项：释放/校验/自愈/幂等/现网对齐）、`tests/test_terminal_panel.py`（含核心进程下 `target: host` 命令走 IPC 转发的回归）；`test_event_buffer.py` 补会话分片保序与同余群号不坍缩回归；`test_sql_sim_debug.py` 补缓冲/合并落盘/ORDER BY/LIMIT 与 `ON DUPLICATE KEY UPDATE`。`tools/build_llm_payload.py --check` 现同时校验「src 真源↔载荷 zip」与「载荷↔现网副本」两级一致性，CI 主套件纳入 `test_terminal_panel`。

## v1.8.0（2026-09-29）

> 主题：**事件缓冲引入 L4 攒批写缓冲（缓解 sqlite 单写者瓶颈）+ SQLite 写锁收敛 + 删除 file_store 冗余后端 + 声明式装饰器 API + 前端产物收归 webui 插件 + 全官方插件补文档**。

### 新功能
- **声明式装饰器 API**：新增 `framework/plugin.py`，支持 `from framework.plugin import command, on, on_message, on_raw_message, hook, task, api, dashboard_card, webui, override_webui, group_extension, user_extension` 的模块级声明式写法；装饰器在 `import` 时登记到当前模块的延迟缓冲区，`register(ctx)` 时一次性应用，行为与原 `ctx.*` 调用完全一致（同一套扩展点契约）。文档见 `docs/plugin-decorators.md` 与 `docs/writing-plugins.md`「进阶写法」。
- **官方插件全量文档**：为全部 `core_plugins` 补齐 `README.md`（onebot_adapter / qq_official / telegram / discord / ws_client / http_inject / http_api / image_renderer / rust_accel / scheduler / session / ops / html_assembler / webui），覆盖协议接入、配置项、命令/事件/API/WebUI 能力与端口，均基于真实源码、无臆造数字。

### 性能 / 架构
- **L4 攒批写缓冲（4MB）**：`EventBuffer` 在 L1 与 L2 之间插入 L4 内存攒批层。L1 满时小包先进 L4 聚合，后台 flush 任务（每 0.05s 检查，达到字节/条数阈值或空闲 0.1s 兜底）以 `executemany` 一次 `commit` 批量落 L2，把「逐条 commit」降为「攒一批一次 commit」，显著缓解 sqlite 单写者瓶颈下的磁盘 fsync 次数。分层：L1 满→L4 攒批→（后台 flush）L2；大包（size > l4_max_bytes，攒批无意义）绕过 L4 直写 L2；L4 满直写 L2；L2 写失败/超时回落 L3 内存兜底，L3 满才丢弃。
- **SQLite 写锁收敛**：移除 `framework/database/db.py` 中已无实际作用的全局 `_lock`（WAL + `busy_timeout=5000` 已具备并发保护），并新增 `synchronous=NORMAL` 提升写入吞吐；L2 sqlite 连续写失败达阈值（默认 8，可配 `sqlite_fail_threshold`）仍自动降级为自研 file 后端。
- **删除 file_store 冗余后端**：移除 `framework/database/file_store.py`（FileStore），debug/sim 模式统一由 `SqlSimEngine` 承接，避免两套近似实现长期分歧；`type: file` 现启动即硬报错，不再静默歧义。

### 重构
- **前端产物收归 webui 插件**：构建产物从根目录 `web/` 移入 `core_plugins/webui/web/`，`webui/` 自包含前端；同步更新 `framework/api/static_routes.py`（`_web_root_dir`）、`framework/api/framework_update.py`、`framework/terminal/cmd_update.py` 的更新白名单、以及 `webui/vite.config.js` 的 `outDir`、`README.md` 目录树与构建说明共 5 处引用，并重新构建验证。

### 文档
- **API 文档增强**：`docs/ctx.md` 增补「静态命令与动态命令（dynamic 参数）」小节与「场景选型：哪个 ctx 函数适合做什么」决策表；`docs/.vitepress/config.mjs` 侧边栏新增「插件装饰器 API」入口。
- debug / sim 存储模式新增 1MB 预读与可选索引（`config.json` 开关）。

### 测试
- 对齐 L4 分层语义：`test_self_heal.py`（sqlite 连续写失败降级改为按 flush 批次计数）、`test_buffer_refill.py`（中等事件溢出进 L4 攒批层）、`test_buffer_l2_file.py`（屏蔽 L4 以精准验证 L3→L2(file) 回落）。
- 回归：主套件 pytest 全过 + 直跑脚本 `test_self_heal 19`、`test_perm 43/43`、`test_plugin_imports 25`、`test_buffer_refill 15`、`test_buffer_l2_file 22`、`test_smoke 87 项` 全过；`test_core_plugin_switch 6`、`test_dual_core 4`、`test_security_hardening 22` 全过。

## v1.7.5（2026-09-28）

> 主题：**core_plugins 官方插件线路的终端扩展点（ops）+ 双进程 IPC 性能优化（发送 O(1) 化 + 宿主日志批量）+ L2 文件后端 + http 适配器并发修复 + Rust 加速接入端 rust_accel（OneBot 反向 WS 热路径原生化）+ 依赖按需化（flask 移出主依赖，API 真假节点）**。

### 新功能
- **终端扩展点（官方插件线路）**：新增 `core_plugins/ops/`（`official: True`），注册 `restart / reload(rl) / shell(sh) / dbdump` 四个命令，经框架扩展点接入，不直接侵入终端核心；handler 一律同步函数（与 `/api/terminal/exec` 同步调用约定对齐）；shell 语法错误 / 超时（`shell_timeout=10`）一律容错返回错误文本，**不卡死终端、不导致线程崩溃**；输出按 `max_output`（缺省 4000）截断。
- **Rust 加速接入端 `rust_accel`（官方插件线路）**：新增 `core_plugins/rust_accel/`（Python 壳 + Rust 子进程，`official: True`，默认 `enabled: false`）。OneBot 反向 WS 的**接收（事件）与广播（动作）**两条热路径从 Python 搬进 Rust 原生实现（tokio + tokio-tungstenite + serde_json）；Python 侧仅保留进程管理、IPC 桥接（本地 TCP + JSON Lines）与监控观测。启用前须先停用 `onebot_adapter`（双接入互斥，防 6830 端口竞争），监听独立端口 6831。构建、配置与基准详见 `core_plugins/rust_accel/README.md` 与《Rust加速_架构设计_rust_accel_20260928.md》。
- **修复存量 bug**：`framework/loader/base.py` 的 `PluginLoader` 此前缺失 `reload_plugin` / `reload_all` 方法，内置 `reload` 命令一直静默失败；补齐后热重载可用。

### 性能
- **IPC 发送 O(1) 化（热点路径）**：`JsonRpcConnection` 引入无锁 `_outbox` 队列 + 独立 `ipc-writer` 线程串行 pickle/socket 写；`_send / _safe_send` 从「持锁同步 send」改为 `put_nowait` 入队，事件循环零 socket IO。事件 3000 条 caller-side **78.5ms → 3.2ms（约 24x）**、e2e 82.3ms（吞吐 ~3.6 万 ev/s 持平）；RPC 单呼叫 ~112us 量级。
- **宿主日志批量合并**：`IpcLogHandler` 攒批（`MAX_BATCH=200` / `FLUSH_INTERVAL=0.05` + 后台 flush 线程），500 条日志压缩为 **3 帧**（原 500 帧）；`host_entry` 关闭前冲刷，核心侧 `_on_host_log` 兼容单条/批量两种格式。
- **rust_accel 端到端基准（N=2000，Windows / release 二进制）**：事件注入（WS→Rust 解析→IPC→Python 分发落点）**~30K ev/s / 33.6us**，低于 Python 框架内部单 worker 端到端 143.9us（fw_end2end 6,951 ev/s）——「Rust 收 + Python 分发」整条链路快于纯 Python 的「分发」本身，收包与 JSON 解析开销完全移出事件循环；广播并发 acall **8,894 qps**（2000/2000 成功，含 echo 回执）、串行 P50 136us / P95 272us，广播路径不阻塞事件循环。

### 修复
- **http 适配器卡死（并发串行阻塞）**：`http_api` / `http_inject` 均用单线程 `HTTPServer` 且 handler 内同步 `future.result(timeout=10)`——任一慢请求占住整条 HTTP 服务，其它请求全部排队。改为 `ThreadingHTTPServer`（每请求独立线程），真并发冒烟：slow 0.8s 挂起时 fast 0.05s 独立返回。
- **断线快速失败**：挂起中的 IPC call 在对端断开 / `close()` 时立即以 `RemoteError` / `IpcClosed` 唤醒（不再干等 30s 超时）；writer 线程退出只以哨兵为准，关闭时不丢弃已入队帧。

### 加固
- **依赖按需化：web 后台降为可选组**：flask / flask-cors / waitress 从框架主依赖移出（`pyproject.toml` `[project.dependencies]` 与 `requirements.txt` 核心段仅剩 6 项：websockets / apscheduler / pyyaml / bcrypt / requests / psutil），归入 `[project.optional-dependencies].web` 组并在 `requirements.txt` 注释可选段；`core_plugins/webui/requirements.txt` 声明插件级依赖，**启用 webui（web.enabled != false）时才由插件加载器自动补装**——未启用时框架零 flask 加载。
- **API 真假节点**：`framework/api/webserver.py` 新增 `WebServerStub` 假节点（同接口 start/stop/app/active/host/port，不创建 Flask 应用、不监听端口）；webui 停用或 flask 缺失时注册假节点而非 `None`，`services.get('web_server')` 恒非 None，调用方判空逻辑不变。
- **官方插件清单驱动加载**：新增独立扫描进程 `tools/scan_core_plugins.py`（dry-run / --write / --enable / --disable），`core_plugins.yaml` 成为官方插件启停与配置的**唯一权威**；config.yaml 的 `core_plugins` 段废弃（不再参与判定），**新发现插件一律 `enabled: false`（绝不自动启用）**，消除「未列出自动启用 / 权威段静默覆盖」两类误加载路径。
- **L2 文件后端**：`EventBuffer` 支持 `l2_backend: sqlite|file|off`，sqlite 关闭时自动落自研 append-only 文件后端（一行一事件、取出即删语义、读指针过半压缩、损坏行跳过）——debug/sim 下 L3 满仍能回落 L2，不直接丢事件。

### 测试
- 新增 `tests/test_ipc_perf.py`（5 项：notify 调用侧不阻塞、并发 RPC、批量日志少帧、断线快速失败、close 唤醒等待者）。
- 重写 `tests/test_core_plugin_switch.py` 覆盖清单驱动加载；新增 `tools/bench_ipc.py` 可复跑基准。
- 回归：主套件 **136 passed**（`-o asyncio_mode=auto`，含新增 `test_web_deps_lazy_with_stub`——flask 按需化回归：API 假节点屏蔽 flask 后仍可导入/启动/停止）；独立脚本 `test_buffer_refill.py` 15、`test_buffer_l2_file.py` 22、`test_perm.py` 43/43、`test_plugin_imports.py` 25 全过。
- rust_accel 专项：`cargo test` 单测全过、`it_smoke.py`（IPC 双向 + WS 收发）PASS、`plugin_smoke.py`（生命周期 + 事件注入 + 广播回执 + 监控，优雅停机 0 残留进程）PASS、`bench.py` 快速档 PASS。

## v1.7.4（2026-09-28）

> 主题：**事件入队热路径 O(1) 尺寸记账（约 120ns，与事件大小无关）+ 分发层字节闸门 + L2 落盘瘦身**。
> 延续 v1.7.3 的存储降级兜底，这一版把「每条事件入队都要做一次全量尺寸估算」的热点成本
> 从毫秒级压到百纳秒级，并补上 onebot 分发层「条数未满、大事件却已撑爆内存」的缺口。

### 性能
- **入队尺寸记账改 O(1) 查表（热点路径）**：`EventBuffer._size_of` 原为 `len(repr(event)) + 64`
  —— C 层递归拼整个 dict 字符串，超大事件（base64 图片）被迫拷贝几百 KB，单条 0.9~2ms。
  现改为**构造时预算 + 入队查表**：onebot 归一化 `normalize_event` 一次性算好尺寸写入 `_est_size`，
  `_size_of` 命中即返回值。四档实测（本机 managed Python 3.13，单位 ns）：
  小事件 5368 → 121、中事件 15860 → 133、大事件（200 段 / 5KB）219524 → 126、
  超大事件（1 段 / 200KB base64）2104939 → 122，**稳定约 120ns 且与事件大小无关**，
  优于改造计划设定的 170ns 基线。9ns 属 C 扩展量级，纯 Python 不可达（已说明）。
- **`_size_of` 回退路径轻量化**：无 `_est_size` 的事件（测试 fixture / 其它协议来源）不再走 `repr`，
  改为内联浅层估算（只遍历顶层字段 + `message` 段 `data` 内字符串值），
  中小事件 1.6~4.6x、超大事件 433x；估算口径与构造时预算逐字对齐，两类事件的水位判断无漂移。

### 修复
- **分发层大事件漏算（base64 大图）**：`_size_of` 与构造时预算原先只数 `message` 段的 `data.text`，
  而 base64 图片走 `data.file`，导致超大事件被按小事件记账、字节闸门形同虚设。改为数 `data` 内
  **所有字符串值**（text / file / url 等），1MB 重尾截断保留。

### 加固
- **onebot 分发层字节闸门**：`_dispatch_pending` 原本只有条数闸门（缺省 4096 条），而 onebot 原始
  payload 单条可达数 MB，条数远未到就能吃光内存。新增 `max_pending_bytes`（缺省 64MB），
  与条数闸门同时生效，超限丢弃并告警（pending / pending_bytes / dropped 一并打印）；
  WebUI「OneBot 接入」设置页可调。

### 优化
- **去双携带**：onebot 归一化 dict 不再冗余携带 `raw_message`（CQ 串）。正文以 `message` 段数组为
  唯一来源，`Event.raw_message` 已有 `_extract_text(message)` 回退（`framework/messaging/event.py`），
  需要原文的消费方语义不变；单条事件的内存与序列化体积同步下降。
- **L2 落盘瘦身**：溢出到 sqlite / 文件 L2 的事件在序列化前剥离冗余 `raw`（完整原始 payload 镜像，
  事件本身已有 message / sender / user_id 等结构化字段）。**不修改内存事件**（L1/L3 与 session 备份
  仍需 raw），只剥离落盘副本；回读侧补 `raw: {}` 占位，回读形态与内存保持一致。
  磁盘占用与 IO 随事件大小同比下降。

### 测试
- `tests/test_self_heal.py` 补 pytest 双模式：新增 `tmp` fixture（转发内置 `tmp_path`），
  此前 pytest 收集下 4 例报错，现与 `python tests/test_self_heal.py` 直接执行双通。
- 全量回归：pytest 风格 **119 例**（db_regression / event_buffer / file_store / html_assembler /
  loop_fix / qq_official / scheduler / sql_sim_debug / core_plugin_switch / dual_core /
  security_hardening / self_heal / smoke）+ 脚本式套件 **129 例**
  （buffer_l2_file 22、buffer_refill 15、self_heal 24、plugin_imports 25、perm 43）全绿。

---

## v1.7.3（2026-09-27）

> 主题：**core 插件"全关却仍加载"根因修复 + 失败插件自动自愈 + 存储降级兜底（FileStore/调试模式）+ 7 项安全加固（WebUI 权限收敛）**。

### 修复
- **core 插件开关"全关却仍加载"（根因）**：`_autoload_core_plugins` 只在 yaml 块缺
  `enabled` 键时才吸收 config.yaml 的 `core_plugins` 段；一旦 yaml 已有 `enabled: true`，
  config.yaml 段的显式关闭（`false`）被静默丢弃 → 改为**config.yaml 的 core_plugins 段
  是权威开关**：键显式出现即以它为准并回写 yaml（含禁用态），未列出的插件维持 yaml 现值。
  新增 `_as_bool()` 严格布尔解析，修复 `bool("false")==True` 导致 `enabled: "false"`
  字符串被误判为启用的隐患（5 例回归测试 `tests/test_core_plugin_switch.py`）。
- **终端禁用/启用与 WebUI 同语义**：`disable_plugin`/`enable_plugin` 统一为
  「写 `is_active` 状态 + 卸载/加载注册 + 路由缓存失效」，不再出现「终端关了、
  WebUI 又拉起来」的分叉；禁用后 `_failed_mtimes` 一并清除，心跳自愈不会把
  已禁用的插件重新拉起（`tests/test_disable_vs_selfheal_bug.py`）。
- **QQ 官方接入域名迁移**（2026-09-18 官方指引）：取令牌迁移到
  `bots.qq.com/app/getAppAccessToken`，正式 API 迁到 `api.sgroup.qq.com`
  （沙箱 `sandbox.api.sgroup.qq.com`）；旧域名 `api.bot.qq.com` 保留作迁移窗口期回退，
  新域名连不上/5xx 自动重试（qq_official / discord / telegram / ws_client 启动
  线程化延后重试 + `run_coroutine_threadsafe` 停止，规避 3.14 无事件循环崩溃）。
- **数据库初始化失败不再阻塞启动**：真实数据库不可用（缺驱动/连接失败/迁移失败）
  时降级为 `data/db/` 下 JSON/YAML 文件存储（FileStore），框架照常启动；
  新增调试模式 `database.type: debug`（SqlSimEngine 本地模拟 SQL，查询有语义），
  无数据库的开发/联调/CI 环境可用（`tests/test_db_regression.py` /
  `test_file_store_fallback.py` / `test_sql_sim_debug.py`）。
- **event_buffer sqlite 连续写失败自愈**：写失败达阈值（缺省 8 次）自动禁用
  溢出层、事件改走内存兜底，重启后自动恢复重试（不再因单点损坏静默丢事件）。
- **终端非交互跳过**：stdin 非 TTY（CI/守护进程/重定向）不再启动输入线程，
  消除对终端的硬依赖。

### 安全加固（7 项，WebUI 权限收敛）
- **敏感表保护（/api/db）**：`admin_users` / `api_tokens` 对普通管理员隐藏
  （表列表不出现、schema/rows 直接 403），仅超级管理员可见，防经数据库网关
  泄露登录令牌与凭据哈希。
- **数据库文件下载拦截（/api/files/download）**：非超级管理员下载
  `.db/.sqlite/.sqlite3/.db-wal/.db-shm` 返回 403（原本任何登录管理员均可整库下载）。
- **高危写操作收紧为 super**：插件上传、pip 依赖安装、隔离 venv 创建、
  GitHub 更新插件、市场安装插件、框架在线更新，均由 `require_auth` 收紧为
  `require_super`（这些入口可执行任意代码/改源码，普通管理员不该触碰）。
- **登录失败文案统一**：账号被禁用不再返回 403「账号已禁用」，与不存在/密码错误
  一致返回 401「用户名或密码错误」（防用户名枚举；audit_log 保留真实原因）。
- **500 回显收敛**：framework/api 全部 93 处 `str(e)`/`{e}` 错误回显收敛为
  「服务器内部错误」，杜绝异常细节/文件路径/堆栈泄露给前端；无日志的 except
  分支自动补 `logger.error`，排障不降级。
- **ZIP 路径穿越与符号链接校验**：插件上传与框架更新 ZIP 拒绝 `..`、`/` 开头、
  `\` 反斜杠条目；框架更新额外拒绝符号链接条目（防解压逃逸/链接指向敏感文件）。
- **文件浏览符号链接防逃逸**：`_safe_file_path` 用 `os.path.realpath` 解析
  真实路径后再做根目录包含校验，符号链接无法借道读出允许目录之外的文件。

### 测试
- 新增 `tests/test_security_hardening.py` 22 例（7 项安全修复逐项锁定）、
  `tests/test_core_plugin_switch.py` 5 例、`tests/test_self_heal.py` 24 例
  （插件损坏自愈 / yaml 备份重建 / FileStore 降级 / event_buffer 自愈），
  CI workflow 接入 db_regression / loop_fix / file_store / sql_sim_debug 套件。
- 全量回归：pytest 风格 58 例 + 9 组脚本式套件（self_heal 24、plugin_imports 25、
  perm 43 等）全绿。

---

## v1.7.2（2026-09-27）

> 主题：**四框架横评（2026-09-26~27）暴露缺陷全部合入——qq_official 适配器六项修复 + 压测三行级 bug + scheduler 启动修复 + 接入端旋钮配置化**。

### 修复
- **qq_official 适配器（体检发现，六项）**：
  - 发送成败判定：原把整个响应 `str()` 后查 `'ret'` 子串且不查 HTTP 状态码，失败可被
    报成成功 → `_api_request` 非 2xx 抛错（带状态码与响应片段），2xx 才 ok，群/单聊统一；
  - 主动消息出口：`call_api` 新增 `active=True` 不挂 msg_id（默认行为不变，仍自动补被动 id）；
  - 被动窗口按会话类型清理：群 5 分钟 / 单聊 60 分钟（原统一 5 分钟，单聊窗口被无声缩短）；
  - token 刷新加锁并统一提前阈值 60s（原 async 60s / sync 30s 不一致，可并发重刷）；
  - 启动加固：loop 未就绪由守护线程等待后线程安全派发（旧 `get_event_loop().call_soon`
    在 3.10+ 静默失效，适配器可能永不启动）；`unregister` 弃用 API 移除 + 同步收尾兜底；
  - 事件幂等去重（Resume 重放/服务重发只进一次）、白名单补 `DIRECT_MESSAGE_CREATE`、
    附件结构化透传 `event['attachments']`、c2c 图片上传按 mime 定扩展名（原硬编码 .png）、
    Content-Type 仅在 JSON 请求时设置。base64:// 透传设计保持不变。
- **EventBuffer 溢出不唤醒（压测 probe 实锤）**：`get_async` 全空时只阻塞在 `L1.get()`，
  事件落 sqlite 不唤醒消费者，积压滞留到下一条 L1 事件才被顺带吐出（"有回显时有时无"）。
  改为 `asyncio.Event` 入队信号，任一层（L1/L3/sqlite）落盘均唤醒；清信号后复查闭合
  丢失唤醒竞态。回归测试复现压测 probe 时序 + 关 sqlite 强制走 L3 的确定性用例
  （CI py3.10 曾暴露 L3 分支漏挂信号，已补）。
- **官方 scheduler 启动必报错**：`fw.loop` 未就绪时兜底在当前线程
  `AsyncIOScheduler.start()`，无运行循环必炸 → 守护线程等就绪后
  `call_soon_threadsafe` 派发（register 工作线程时序的既有约定），两例回归测试。
- **image_renderer PyO3 二次初始化崩溃**：原生扩展加载加每进程一次守卫
  （`sys.modules` 占位 + 复用，失败摘除换候选），自愈/热重载不再触发
  init-once 崩溃导致渲染静默回退 PIL（干净进程实测 3.1ms/张）。

### 优化（配置化）
- **接入端旋钮进配置中心**（`core_plugins.yaml`，原硬编码）：
  `onebot.max_pending_events`（有序分发积压上限，256 → 缺省 4096，压测中
  257 条瞬发静默丢的元凶）、`onebot.max_frame_size`（WS 单帧上限，websockets
  默认 1MiB → 缺省 16MB，1MB 报文 1009 断链的根因）。WebUI 连接页同步露出。

### 测试
- 新增 `tests/test_qq_official.py` 11 例（全离线）、`tests/test_scheduler.py` 2 例、
  `tests/test_event_buffer.py` 扩至 10 例（唤醒路径全覆盖），全部纳入 CI。

---

## v1.7.1（2026-09-26）

> 主题：**事件缓冲热路径修复（净开销 ~173μs → 6.2μs/事件，端到端 no-op 5.6k → 82k msg/s）+ core_plugins.yaml 退出版本跟踪**。

### 修复
- **事件缓冲热路径（v1.7.0 引入的性能回归，`event_buffer.py`）**：
  - 字节记账每事件两次全量 `json.dumps`（入队、出队各一次，~17μs）→ 改为入队时以
    `repr` 估算一次（~4μs，形状无关且能抓住 base64 大图等重尾）、随条目携带、出队复用；
  - `get_async` 每次空转都无条件发起一次 `asyncio.to_thread` sqlite 空查询
    （wait=True 乒乓节奏下每事件白付 ~150μs）→ 维护 `_sqlite_rows` 内存计数，
    仅表内有已知积压才发起批量回取；停机排空 / 统计不再在事件循环里做阻塞 COUNT；
  - 突发注入（风暴 bench / 批量回调不逐事件 yield）下 L1 一满即把本可留内存的事件
    全压进 sqlite 磁盘层（每条 ~200μs 磁盘往返）→ 溢出前先 `sleep(0)` 让消费者
    排空一次再重试，仍满才落盘；wait=True 的阻塞背压语义不变；
  - 启动时清点 sqlite 表内遗留行数，进程重启后持久化事件照常承接。
- **`core_plugins.yaml` 退出版本跟踪**：该文件是启动扫描 / WebUI 开关会自动回写的
  运行时状态，入库导致 fresh clone 继承开发机的「全关」状态（11 个 `enabled: false`），
  与文档承诺的默认开关（onebot_adapter / webui / session / scheduler / html_assembler
  开，其余关）矛盾，cloner 无所适从。改为 gitignore（与 config.yaml 同策略），首次
  启动由配置中心按 schema 默认值自动生成；本机开发配置不受影响。

### 测试
- 新增 `tests/test_event_buffer.py` 7 例并纳入 CI pytest 步骤：L1 往返与记账清零、
  wait=True done 契约、溢出承接与排空、重启遗留承接、无积压时不触 sqlite、全满丢弃、
  重尾事件记账。

---

## v1.7.0（2026-09-26）

> 主题：**事件三层缓冲（防丢失/防内存暴涨）+ 新官方插件 html_assembler + 全库文档去人群定位重写**。

### 新增
- **事件三层缓冲**：L1 内存主队列（512KB，消息流入直处理）→ L2 sqlite 持久化溢出
  （`data/event_buffer.db`，独立文件独立连接，不阻塞主库，防内存暴涨、防事件丢失）
  → L3 内存兜底（4MB，sqlite 写超时/失败时应急暂存）；全满日志告警并丢弃新事件
  （保老弃新），丢弃计数经 `stats()` 可查。消费优先级 L1（最新热数据）→ L3 → L2
  （已持久化历史积压最后消化）；`wait=True` 同步语义事件保持阻塞进 L1 不参与溢出，
  保留 done future 契约。配置 `config.yaml → buffer`（l1_max_bytes / l1_max_items /
  sqlite_enabled / sqlite_path / sqlite_write_timeout / sqlite_batch / l3_max_bytes /
  full_action）。无新增三方依赖。
- **官方插件 `html_assembler`（单文件 HTML 装配引擎）**：占位符替换
  （`{{ key }}` 自动 HTML 转义、嵌套 `a.b.c` 与列表下标 `items.0.name`；
  `{{ img:key }}` 图片转 base64 内嵌；`{{ raw:key }}` 原文插入）+ 缺失策略
  `on_missing`（keep/empty/raise）。内存可控：模板/单图/输出字节上限 + 单次图片数
  上限；模板 UTF-8/GBK 自适应；纯标准库零第三方依赖，`engine.py` 可脱离框架独立使用。
  接入三通道：官方插件配置中心自动同步（`core_plugins.yaml → html_assembler`）、
  框架服务 `fw.services.get('html_assembler')`、模块
  `sys.modules['core_plugin_html_assembler']`（兼容别名 `plugin_html_assembler`）。
  `/html_asm` 自测命令渲染内置 demo 页。官方插件清单（根 README / 文档站目录速查）补齐。

### 修复
- **官方插件 `get_data_dir()` 冒号路径**：ctx 名带 `core:` 前缀，冒号进入目录名在
  Windows 非法，官方插件取数据目录即崩；替换为下划线（`core_html_assembler`）。
- **v1.6.0 预览体检硬伤（P0，随 v1.6.0 标签入库，此处补录）**：
  - `framework/core/dispatch.py` 补 `log_broker` 等导入，消息分发不再 `NameError`；
  - `framework/core/runtime.py` / `base.py` 路径 `dirname` 由 2 层改为 3 层，`core_plugins` / `plugins` / `config.yaml` 指向仓库根，官方插件可被发现；`runtime` 补 `asyncio`/`gc`/`importlib.util`；
  - `framework/database/db_conn.py` 去掉顶层 `import pymysql`（干净 SQLite 环境可启动），重连常量下沉并由 `db.py` re-export；
  - `framework/loader/config.py` 定义 `_CONFIG_FILE_EXTS`（`base.py` re-export）；`lifecycle.py` 补 `pip_install_all`；`ui.py` 补 logging/os/time。
- **多接入端串线**：`ServiceRegistry.adapter_for_source` 按事件来源选适配器；`reply_text` 与 `ctx.actions` 优先走来源对应端（多端并存时不再固定 onebot / 后加载者）。
- **http_api 群管**：`kick`/`ban`/`unban` 改走 `api.acall` 并检查 `status`，不再对 `api_caller` 直调方法导致 `AttributeError`→500；终端 `ban`/`kick` 检查返回结果，失败不假报成功。
- **ws_client**：`websockets` 14+ 用 `additional_headers`，12/13 用 `extra_headers`，装 12/13 不再建连即 `TypeError`。
- **qq_official**：`msg_seq` 改进程内计数器（同秒多回复不碰撞）；本地图片读取经 `asyncio.to_thread`，不再阻塞事件循环。

### 测试
- 新增 `tests/test_html_assembler.py` 23 例：引擎行为（占位符/转义/嵌套/图片/缺失策略/上限）+ 官方插件装载（register/服务/命令/配置生效）+ `core:` ctx 名数据目录 Windows 安全。
- 新增 `tests/test_smoke.py`：路径解析、关键模块 import、分发+回复冒烟、按来源路由、适配器补丁断言。

### 文档
- 删除「适合人群 / 按身份导路」定位写法，全部重写为形态化表述：README「三类目标用户」
  →「三种典型形态」；文档站首页去「你的情况选路」改「解决什么问题/文档导航」；
  guide 总入口去分人群三段路线改单一推荐阅读顺序；getting-started「选一条路线」
  →「确定事件来源」。
- 首页/对接 IM 诚实化：群管等协议专有动作按端能力说明，去掉「换接入端插件不用改」等超前概括。

---

## v1.6.0（2026-09-24）

> 主题：**多协议接入端 + 协议中立内核 + 全库「事件驱动 IM 平台」定位改写**。

### 新增
- **官方接入端 `ws_client`（出站 WebSocket）**：与反向 WS 的 onebot_adapter 互补，
  作为**客户端**主动连出到外部 WS 服务；远端 JSON 事件归一化入核，`send_msg` /
  `send_text` 出站，**图片消息段支持 `base64://`**（CQ 码与消息段数组均可解析）。
  完整实现 5 抽象方法 + `get_connection_info()`（连接页动态表单），断线自动重连、
  连接前出站有界积压。默认 `enabled: false`（外连安全），配置块
  `core_plugins.yaml → ws_client`（url / bot_name / token / reconnect_interval）。
  无新增三方依赖（复用 `websockets`）。
- **官方接入端 `qq_official`（QQ 官方机器人）**：`getAppAccessToken` 自动刷新
  access_token → `GET /gateway/bot` 取 WSS → Hello/Identify(op2)/心跳(op1)/Resume(op6)；
  群 `GROUP_AT_MESSAGE_CREATE` / 单聊 `C2C_MESSAGE_CREATE` 归一化入核，
  出站 `send_msg` 走 OpenAPI（群 `/v2/groups/{openid}/messages`、单聊
  `/v2/users/{openid}/messages`），**图片 base64:// 经文件上传 → msg_type=7**，
  被动回复带 `msg_id`/`msg_seq`。默认 `enabled: false`，配置块
  `app_id`/`app_secret`/`bot_name`/`intents`（默认 33554432=GROUP_AND_C2C_EVENT）。
  无新增三方依赖（`websockets`+`requests`，手写协议不依赖 botpy）。
- **官方接入端 `telegram`**：`getUpdates` 长轮询（offset 自增、断线重试）+
  `sendMessage`/`sendPhoto`；群/超级群 → `message_type=group`，私聊 → `private`；
  出站图片 `base64://`/`file://`/http(s)/本地路径 → multipart `sendPhoto`。
  默认 `enabled: false`，配置块 `token`/`bot_name`/`polling_timeout`。
  无新增三方依赖（`requests`）。
- **官方接入端 `discord`**：Gateway WSS（Hello/Identify/Heartbeat/Resume、
  心跳 ACK 丢失自动重连）+ REST v10 `POST /channels/{id}/messages`；
  `MESSAGE_CREATE` 归一化（guild→group，DM→private），出站图片 `base64://`
  → multipart `files[0]`。默认 intents `37377`
  （GUILDS|GUILD_MESSAGES|DIRECT_MESSAGES|MESSAGE_CONTENT，特权 Intent 需门户开启）。
  默认 `enabled: false`，配置块 `token`/`bot_name`/`intents`/`reconnect_interval`。
  无新增三方依赖（`websockets`+`requests`）。
  三者均：**enabled=true 时即使凭证未填也注册到连接页**（WebUI 可补配，填完重启后才建连）。

### 重构
- **内核彻底去 OneBot 硬编码（连接页/状态/发送路径协议中立）**：
  - `ProtocolAdapter` 新增可选 `get_connection_info()` 连接自描述
    （id/name/config_section/fields/restart_keys/endpoint_hint/guide/status_extra），
    `ServiceRegistry` 按 `adapter_id` 汇总多接入端（`protocol_adapters()` / `primary_adapter()`）。
  - `/api/connection` GET 改为返回 `adapters[]` 动态描述（兼容保留扁平 `config`/`status`）；
    PUT 按 `body.adapter` 与适配器声明的字段白名单写入，不再写死 `onebot` 段与
    `listen_host/listen_port/access_token`。仪表盘/运行状态/终端 `status` 的在线列表
    一律取 `protocol_adapter.get_connected_bots()`，`fw.ws_server` 降为兼容别名
    （优先接入端自报的 `ws_server`）。
  - `fw.protocol_backend` 属性别名改为 `fw.protocol_adapter`；停止顺序纳入
    `protocol_adapter`（兼容保留 `ws_server` 键）。
  - `ctx.actions` 成为协议中立动作面（推荐新代码）；`ctx.onebot` 保留为别名。
    `ctx.send_msg/ban/kick/...` 快捷方法改走 `ctx.actions`。
  - 双进程宿主 `IpcAdapter.send_text` 经新增 RPC `api.send_text` 转发核心接入端，
    不再硬编码 `send_group_msg/send_private_msg`；`http_api` 发送/广播/状态错误文案中立化。
  - 适配器实现：`onebot_adapter` / `http_inject` / `ipc` 均提供 `get_connection_info()`。
  - WebUI：连接页按 `adapters[]` 多卡片动态渲染配置表单与在线状态；
    仪表盘标题用 `adapter_name`，运行状态改为「接入端连接」，侧边栏改为「接入端连接」，
    设置页 OneBot 标签改为「接入端（onebot 段）」。
  - 顺带修复：`framework/messaging/protocol.py` 缺 `HookPoints` 导入、
    `framework/core/dispatch.py` 缺 `asyncio`/`HookPoints`/`ProtocolAdapter` 导入。
- **framework/ 根目录按域分包（第七轮）**：根目录 29 个散落 .py 收拢为
  `core/`（base · dispatch · runtime · stats_writer）、`ctx/`（base · messaging ·
  events · webui · db）、`loader/`（base · config · lifecycle · runtime · ui）、
  `deps/`（__init__ · pip）、`perm/`（__init__ · admin · groups · tracks）五个子包；
  根目录仅保留 `config/hooks/log_broker/dual_auth/scheduler/runtime/tls/apis/stats_writer`
  等入口与 shim。所有旧导入路径（`from framework.core import Framework`、
  `from framework.loader import pip_install_*`、`from framework.ctx import PluginContext`、
  `from framework.perm import resolve` 及懒加载属性、`from framework.stats_writer import
  AsyncStatsWriter` 等）经子包 `__init__.py` re-export 保持兼容。
- **core 史山剥离（第六轮）**：
  - `framework/core.py` 按职责拆为 base + 两个 mixin：事件/命令/通知分派与中立回复
    → `core_dispatch.py`（`FrameworkDispatchMixin`）；核心插件加载、依赖自愈、心跳/内存看门狗、
    内置任务 → `core_runtime.py`（`FrameworkRuntimeMixin`）；`Framework` 继承两者，
    公开方法与双进程角色分派（`_read_plugin_process_tag` / `_core_plugin_is_core_side`）行为不变。
  - `framework/deps.py` 中 pip 镜像安装/requirements 解析/版本说明符 → `deps_pip.py`
    （`_PIP_MIRRORS` / `_RE_PKG_NAME` 等常量随迁）；`deps.py` 保留 `PluginDepsMixin`
    与底部 `pip_install_*` re-export，`from framework.deps import pip_install_*` 兼容。
  - `framework/database/db.py` 连接管理/查询执行 mixin → `db_conn.py`（`DatabaseConnMixin`）；
    模块级 `init_db` / `_parse_sqlite_type` / 单例 `db` 与 `from framework.database.db import Database, init_db`
    保持兼容。
  - `framework/messaging/router.py` 匹配子系统 → `router_match.py`（`RouterMatchMixin` +
    `SimpleMatch` / `_PluginRoute`）；`MessageRouter` 继承 `RouterMatchMixin` + 关键词子系统
    `KeywordReplyMixin`，热路径行为不变。
  - `framework/loader.py` 加载/卸载/合成包预载/字节码清理 → `loader_lifecycle.py`
    （`PluginLifecycleMixin`）；`_PluginSourceLoader` 随迁并经 `loader.py` re-export，
    `from framework.loader import pip_install_*` 与加载器公开面不变。
- **core 史山剥离（第五轮）**：
  - `framework/ctx.py`（893 行 `PluginContext` 上帝对象，77 方法）按域拆为
    base + 四个 mixin：`ctx_messaging.py`（OneBot 快捷动作/身份/群级开关）、
    `ctx_events.py`（命令/任务/事件/扩展点/协议 API）、`ctx_webui.py`（仪表盘/WebUI/
    管理页扩展）、`ctx_db.py`（同步/异步数据库）；`PluginContext` 继承四个 mixin，
    全部公开方法名与签名不变（311 行 base）。
  - `framework/perm.py`（846 行）拆为 core 解析（`resolve`/`PermissionSet`/缓存，
    531 行）+ `perm_admin.py`（审计/过期清理/上下文构造）+ `perm_groups.py`
    （组 CRUD + 节点管理）+ `perm_tracks.py`（升降级轨道）；子模块单向依赖 core，
    `perm.py` 经模块级 `__getattr__` 懒加载 re-export，`from framework.perm import X`
    与 `perm.X` 属性访问均保持兼容。
- **core 史山剥离（第四轮）**：
  - `framework/terminal/builtins.py`（669 行 `register_builtins` 巨型函数）按域拆为
    `cmd_core.py`（help/status/plugins/log/clear/exit）+ `cmd_plugin.py`
    （enable/disable/config/reload）+ `cmd_msg.py`（send/recv/ban/unban/kick/broadcast）
    + `cmd_info.py`（users/groups/tasks）+ `cmd_update.py`（update）+ `helper.py`；
    `register_builtins(fw)` 保留为编排入口，20 条内置命令注册行为不变。
  - `framework/api/framework_ops.py`（343 行）：检查更新 / 源码更新拆至
    `framework_update.py`（262 行）；`framework_ops.py`（109 行）保留 version /
    restart / terminal_exec 并编排子模块，webapp 调用点不变。
  - `framework/loader.py`（1282 → 933 行）：配置 schema 能力（`read_config_schema` /
    `init_plugin_configs` / `get_plugin_config_files`）→ `loader_config.PluginConfigMixin`；
    运行时监控（`_start_memory_monitor` / `heartbeat_register` / `self_check_orphans`）
    → `loader_runtime.PluginRuntimeMixin`。
  - `framework/messaging/router.py`（707 → 571 行）：关键词自动回复子系统
    （`_KeywordRule` + 加载/匹配/handler/命中计数）→ `router_keywords.KeywordReplyMixin`；
    `MessageRouter` 改为继承该 mixin，热路径行为不变。
- **core 史山剥离（第三轮）**：
  - `framework/api/webapp.py`（1000 行）：`WebServer` 拆至 `webserver.py`；版本/yaml/
    插件市场/GitHub 下载等 24 个共享辅助闭包外提为 `app_helpers.make_app_helpers()` 工厂
    （键名与原 ctx 完全一致，1000 → 约 400 行）。
  - `framework/api/plugins.py`（1074 行）按域拆为 `plugins.py`（生命周期 + 依赖/venv +
    编排入口）+ `plugin_market.py`（GitHub 更新 / 市场源 / 安装）+ `plugin_meta.py`
    （README / 配置 / schema / 命令 / 依赖图）；`plugins.register(ctx)` 继续作唯一入口。
  - `framework/core.py` 的 `AsyncStatsWriter` 剥离至 `framework/stats_writer.py`
    （`from framework.core import AsyncStatsWriter` re-export 兼容）。
  - `framework/loader.py`（1604 → 1282 行）UI 能力抽为 `loader_ui.py` 三个 mixin：
    `PluginUiExtensionsMixin` / `PluginWebuiMixin` / `PluginGroupSettingsMixin`。
  - `framework/api/perm_api.py`：20 处函数内 `from framework import perm as perm_mod`
    提升为模块级单次导入。
- **core 史山剥离（第二轮）**：`framework/database/db.py`（1306 行，五职合一）拆为
  `dialect.py`（SQL 方言翻译，纯函数）/ `schema.py`（自动建表 + 运行时迁移）/
  `db.py`（连接管理 + 查询执行 + 事务 + 单例，523 行）；写路径 `execute` /
  `execute_many` / `insert` 合并为统一 `_write` 管线，消除三段复制粘贴与散落的
  `if db_type == 'sqlite'` 方言分支（统一走 `_translate_sql` / `_translate_write_sql`）。
- **loader 依赖职责剥离**：`framework/loader.py`（2177 → 1604 行）中的 pip 镜像安装、
  requirements 解析、版本说明符解析与插件依赖检查/隔离 venv 迁至新模块
  `framework/deps.py`（自由函数 + `PluginDepsMixin` 混入）；
  `from framework.loader import pip_install_*` 等旧导入路径经 re-export 保持兼容。
- **新增 `pyproject.toml`（PEP 621）**：项目元数据与依赖声明；`requirements.txt`
  继续作为 `main.py` 启动自检的安装入口，两处依赖同步维护（兼容保留）。

### 文档 / 配置
- **明确 SQLite 适用边界**：SQLite **仅适合小环境与开发环境**（单写多读、单文件），
  **不适合大环境**（多群、高并发、多进程部署）——`config.yaml` 注释、默认配置模板、
  启动日志、`docs/database.md`、`docs/configuration.md`、README 统一标注，
  大环境一律切 `database.type: mysql`。
- 同步过期文档路径：`docs/database.md` 改指 `framework/database/*`；
  `architecture.md` / `event.md` / `services.md` / `protocol_adapter.md` / `session.md` /
  `loader.md` / `best-practices.md` 中的 `framework/{event,protocol}.py` 旧路径改指
  `framework/messaging/*`；README 目录树补齐 `pyproject.toml`、
  `framework/{database,messaging,terminal,deps,loader_*,stats_writer,api/{webserver,app_helpers,framework_update,plugin_market,plugin_meta}}`；
  `docs/loader.md` 维护者速查注明 deps / loader_ui / loader_config / loader_runtime 分工。
- **全库定位改写为「事件驱动的 IM 平台」**：README / CHANGELOG / pyproject / docs /
  vitepress config / main.py / framework 注释 / sql / WebUI 登录与权限页文案统一口径；
  统一历史文档与注释措辞为中性表述（装饰器 API 与扩展点作为框架一等公民呈现，不与任何第三方项目做对比）；
  权限系统与 SQL 注释改为「节点式权限」中性表述。
- **微内核设计归属说明**：README 注明微内核设计独立在项目
  [zernus / Zero_Nexus](https://github.com/kuangxing6367/Zero_Nexus)，
  本仓库是构建于其上的 IM 平台与官方插件集；文中自称改为「平台内核 / 极简内核」。
- WebUI 登录页副标题改为「事件驱动 IM 平台 · 统一管理后台」，权限页去掉品牌风格提法。

---

## v1.5.1（2026-09-16）

> 主题：**群内 @机器人 命令修复**——修正消息归一化把 @机器人 前缀编码为 `[@self_id]` 污染命令匹配，导致群内以 `@机器人 命令` 触发的指令被静默吞掉。

### 修复
- **@机器人 命令被前缀吞掉**：`framework/messaging/event.py` 的 `_extract_text()` 将 `at` 段编码为 `[@qq]` 拼入文本，`router.py` 的 `route()` 又自行调用 `_extract_text(event.get('message'))` 而非复用 `Event.message`，导致群内 `@机器人 查订单` 变成 `[@机器人]查订单`，前缀无法匹配任何命令而被丢弃。
  改为在 `Event` 构造后剥离 leading `[@self_id]` 及其尾随空格，并让路由层复用已处理过的 `ev.message`；`segments` / `has_at_bot` / `at_list` 等原始信息均保留，@他人、文本在前 @ 在后、纯 @机器人 等场景行为不变。
- 验证：`@机器人 查订单`、`@机器人  查订单`（带空格）、`@机器人` 前置纯文本、@他人、纯 @机器人 五种用例均符合预期；`test_perm` 43/43、`test_dual_core` 4/4、`test_plugin_imports` 25/25 全过。

---

## v1.5.0（2026-09-12）

> 主题：**框架路径与静态资源路由修复**——修正 `init_db` 的 SQL 文件查找路径与插件 WebUI 静态资源路由，消除数据库全表缺失与插件页面资源 404 引发的连锁报错。

### 修复
- **`init_db` SQL 路径查找**：`_find_sql_file()` 原只上溯到 `framework/` 目录，找不到项目根 `sql/init.sql`，导致 SQLite 自动建表被跳过、系统表一张未建，进而 `plugins` / `admin_users` / `tasks` / `commands` 等全链路报 `no such table`。改为从 `framework/database` 向上回溯至多 4 级，优先匹配 `项目根/sql/<file>`，已实测命中。
- **插件 WebUI 静态资源路由 404**：`/api/plugin_webui/<plugin>/assets/<path:filename>` 路由中 `/assets/` 为字面段，Flask 将 `filename` 解析为 `mf.js`（不含 `assets/`），原 handler 却去 `web/mf.js` 查找（真实文件在 `web/assets/mf.js`）返回 404，导致复用 `mf_core/assets` 的业务插件页 `MF` 全局对象未定义、`MF.crudPage(...)` 抛 `ReferenceError`。改为拼到 `web/assets/<filename>` 下返回，URL 契约不变。

---

## v1.4.0（2026-09-11）

> 主题：**HTTPS/WSS 与可自定义界面**——开启 HTTPS/WSS（SSL 证书路径可配，相对/绝对），
> WebUI 左侧栏支持自定义显示，双核心补上跨进程终端，并让原生图片渲染扩展可在 CI 构建。

### 新增
- **HTTPS / WSS（SSL 证书）**：新增顶层 `ssl` 配置段（`enabled` / `cert` / `key`）。`cert`/`key` 支持
  **相对项目根目录**或**绝对路径**；启用后 Web 管理后台走 **https**、OneBot 反向 WS 走 **wss**（共用同一证书）。
  可在后台「设置 → SSL / TLS」修改（改动需重启生效）。实现：`framework/tls.py` 构建 SSLContext；
  Web 在启用 SSL 时改用 werkzeug 提供 TLS（waitress 本身不支持 TLS），未启用时行为不变。
- **WebUI 左侧栏支持自定义显示**：`web.sidebar`（`order` 顺序 / `hidden` 隐藏）控制官方菜单项的显示与顺序；
  后台新增「设置 → 侧边栏」可视化调整（勾选显示 + 上移/下移），保存后即时生效。
  插件 `ctx.webui(..., sidebar=True)` 注册的入口仍自动入栏；`/api/menu` 一并返回该配置。
- **原生扩展 CI 构建**：`.github/workflows/build-zcbot-render.yml`，在 GitHub Actions 上构建
  `image_renderer` 的 Rust 扩展（Windows 出 `zcbot_render.pyd`、Linux 出 `zcbot_render.so`），
  补上 README 已引用但仓库中缺失的自动构建工作流；产物已回填 `native/bin/`，win64 `.pyd` 经 Python 3.13 实测可加载并出图。
- **文档站首页（`bot.zgric.top` 宣传页）重写**为事件驱动 IM 平台定位：明确「使用人群 / 使用范围」，
  参考示例补齐非 IM 场景（纯定时任务、HTTP Webhook 事件源、自写接入端、扩展点切面）。

### 修复
- **文档站（`bot.zgric.top`）排版错乱**：VitePress `base` 由 `'/zcbot/'`（面向旧的 `kuangxing6367.github.io/zcbot/` 项目页）
  改为 `'/'`。站点已切到自定义域名 `bot.zgric.top`（根路径托管），沿用旧的 `/zcbot/` 前缀会让 css/js 与站内链接
  全部指向 `bot.zgric.top/zcbot/...` 而 404，页面因此失去样式；改为根路径后恢复正常。
- **双进程终端交互修复（跨进程终端）**：核心进程此前不走 `fw.start()`，导致双核心下**终端从未启动**。
  现在核心进程显式注册并启动终端；终端命令按 `target`（`core` / `host` / `both`）路由，
  `plugins` / `enable` / `disable` / `reload` / `tasks` 经 IPC `terminal.exec` 转发到宿主进程执行，
  `status` / `plugins` 两侧合并展示；单进程（`standard`）行为不变。见[双核心架构](docs/进阶/双核心架构.md) 5.3 节。

---

## v1.3.8（2026-09-11）

> 主题：**扩展点契约 + 可插拔侧边栏**——内核正式确立「最小核心 + 扩展点」契约，
> WebUI 侧边栏开放给插件注册。

### 新增
- **扩展点系统（HookRegistry，`framework/hooks.py`）**：内核核心契约。内核在运行流程预留 12 个标准扩展点
  （`lifecycle.startup/shutdown`、`http.before/after_request`、`event.before/after_dispatch`、
  `command.before/after`、`message.before/after_send`、`action.before/after`），扩展用 `ctx.hook(point, handler)`
  往任意环节插入逻辑；支持 sync/async handler、优先级、同名去重、插件卸载自动清理。
  - `action.before/after` 覆盖每一次协议动作（send_msg / 禁言 / 查询…），适合统一审计 / 限流 / 中间件；
  - `event.before_dispatch` 返回 `False` 可丢弃事件；`command.before` 返回 `False` 跳过该命令；`http.before_request` 返回 Response 可短路请求。
- **Ctx 新增 `ctx.hook()` / `ctx.unhook()`**：插件侧统一注册 / 注销扩展点。
- **WebUI 可插拔侧边栏**：插件现在可以用 `ctx.webui(title, entry, icon, order, sidebar=True)` 在
  **侧边栏注册独立入口**（点击跳转 `/plugin/<插件名>` 直接打开该插件页面），不再局限于「插件页面」聚合页；
  `sidebar=False`（默认）保持向后兼容，仍归入聚合入口。前端新增 `/plugin/:name` 路由，`GET /api/menu` 返回侧边栏结构。
- **官方默认侧边栏支持开关**：新增 `config.yaml` → `web.official_sidebar`（默认 `true`），
  关闭后侧边栏仅显示插件注册项与「设置」；可在「设置 → Web 服务 → 显示官方侧边栏」中切换，保存后**即时生效，无需重启**。
- **文档与 README 内核叙事重构**：
  - README 从「插件化框架」升级为「内核 + 扩展点」叙事，新增「扩展点（Extension Points）」章节，保留全部原有详解（快速开始、权限、API Key、目录结构、双核心等）。
  - `docs/api/` 重组为 **基础参考**（`basic/`：ctx / event / framework / services）与 **进阶扩展**（`advanced/`：扩展点 / 协议适配器）两大块，原有详解完整保留。
  - 新增 `docs/hooks.md`（扩展点完整文档）与 `docs/api-index.md`（API 总览）。

### 修复
- **管理后台前端（WebUI）构建改用相对路径**：`base` 由 `'/'` 改为 `'./'`，logo 采用 `import.meta.env.BASE_URL` 拼接，
  使 `web/` 产物部署到任意子路径时都不会因绝对路径 `/js/`、`/css/` 404 而丢样式。

---

## v1.3.7（2026-09-10）

> 主题：**双核心实验版进入文档与发版流程（默认关闭）+ 修复终端命令在异步线程中的协程调度错误**。
> 双核心是长期实验线，默认单进程行为完全不变；终端修复解决了 `send`/`recv`/`ban`/`unban`/`kick`/`broadcast` 在 worker 线程里因无事件循环而「发送失败 / coroutine never awaited」的问题。

### 新增
- **双核心架构（core/host 双进程，实验特性）**：把一次启动拆成「核心进程 + 宿主进程」，经标准库 IPC（回环 TCP + authkey）通信，零第三方依赖。
  - `config.yaml` 暴露 `dual_process` 开关（`enabled` / `core_plugins` / `max_restarts` / `restart_interval`），**默认关闭，单进程行为完全不变**。
  - 官方插件按 `__plugin_meta__['process']` 标记自动分派：`onebot_adapter` / `http_inject` / `http_api` / `webui` 留在核心进程，其余与用户插件在宿主进程加载。
  - 新增开发文档 `docs/dual-core.md`，README 增加「十二、双核心实验版」章节。
- 新增 `tests/test_dual_core.py`：插件归属解析、IPC 协议往返、远程数据库约束；实测双进程可正常拉起（核心 spawn 宿主、IPC 握手成功、RemoteDatabase 代理生效）。

### 修复
- **终端命令异步调度修复**（`framework/terminal.py`）：`send`/`recv`/`ban`/`unban`/`kick`/`broadcast` 原在 `asyncio.to_thread` 的 worker 线程内定义闭包协程并 `ensure_future`，该线程无事件循环，导致 `There is no current event loop in thread 'asyncio_0'` 与 `coroutine was never awaited`。现改为 `async def` 直接在事件循环内 `await`，同步 DB 操作包进 `asyncio.to_thread`，不再抛错。

### 兼容性
- 双核心默认关闭，单进程启动、配置键、服务名、数据库 schema 全部不变；老业务插件零改动。
- 终端命令对外行为（命令名、参数格式、输出文案）不变，仅内部调度方式修正。

### 测试
- `compileall` 全量编译通过；`tests/test_dual_core.py` 4/4 通过。

---

# 第五代 · 通用插件化能力沉淀（v1.3.x）

## v1.3.6（2026-09-10）

> 主题：**框架层去除 QQ / OneBot 品牌绑定**——框架本体不再自称或被描述为「QQ 机器人框架」，
> OneBot 只以 `onebot_adapter` 插件的身份存在，框架自身保持协议中立。

### 变更
- 启动入口与内核文案中立化：`main.py`、`framework/__init__.py`、`framework/config.py` 顶部的
  「OneBot QQ机器人框架」改为「事件驱动 IM 平台」。
- 源码注释与文档字符串中的 QQ 品牌词全部改为中立表述：`QQ 号` → `用户 ID`、`QQ 群` → `群组`、
  `QQ 特有字段` → `平台特有字段`、`不局限于 QQ` → `不局限于单一平台` 等。
- 数据库脚本注释同步中立化（`sql/init.sql`、`sql/init_mysql55.sql`）：**表结构、字段名、索引均不变**，老库无需迁移。
- WebUI 文案：`QQ号` → `用户 ID`；登录页「OneBot QQ 机器人统一管理平台」改为「事件驱动 IM 平台 · 统一管理后台」。
- README 与 docs 去除 QQ 品牌字样；OneBot 仅作为默认接入端插件（`onebot_adapter`）的协议名出现。

### 修复
- 重新构建前端产物，清掉 `web/js/Connection.*.js` 里残留的合并冲突标记（`>>>>>>>> b1464e2...`）。

### 兼容性
- **纯文案 / 注释层变更**，无 API、配置键、服务名、数据库 schema 变更。
- `ctx.onebot`、配置段 `onebot`、服务名 `onebot_api`、`onebot_adapter` 插件目录名全部保持不变，老业务插件零改动。
- `core_plugins/onebot_adapter` 内部保留 OneBot 11 协议相关字样——它是该协议的实现端插件，与框架本体无关。

## v1.3.5（2026-09-10）

> 说明：本版本在研阶段以 v1.3.4 记录，发布时统一并入 v1.3.5（版本号由 v1.3.3 直接跳至 v1.3.5）。

> 主题：**官方插件配置中心 + 协议层彻底解耦，框架核心不再包含任何 OneBot 实现**。
> 全程保持前后向兼容：老插件的 `ctx.onebot` / `send_msg` / `ban` / `kick`、`bot=` 关键字、
> 事件结构、服务名、`fw.config.get('onebot')` 等公开用法全部不变。

### 新增
- **官方插件配置中心 `core_plugins.yaml`**：独立文件集中管理全部官方插件的 `enabled` 开关与配置项；
  启动时自动扫描 `core_plugins/` 目录——新装补块、卸载删块、自动回写，并合并进主配置，
  插件既有的 `fw.config.get('onebot')` 读取方式不变。`config.yaml` 中失效的旧 `core_plugins` 段已清理。
- **`framework/runtime.py` 运行时上下文**：新增协议无关的 `current_source_var`（当前事件来源），
  并保留 `current_bot_var` 别名以兼容旧代码。
- **协议接入抽象增强**（`framework/protocol.py`）：`ProtocolAdapter` 基类提供统一的
  `call/acall/send_text` 默认实现，以及协议无关的 `ActionProxy` 与 `ServiceRegistry`，
  自定义接入端只需实现最小契约即可获得 `ctx.api()` 与框架中立回复能力。
- **中立回复通道 `reply_text`**：路由层不再硬编码 OneBot `send_msg`，改走"当前接入端 → 通用回复"，
  IPC 适配器同样补齐 `send_text`。
- **基于插件元信息的进程分派**：官方插件通过 `__plugin_meta__['process']`（`core`/`host`）静态标记
  由 AST 静态分析自动分派到双进程，取代原先硬编码的 `_core_whitelist` 白名单；
  `dual_process.core_plugins` 显式名单仍可覆盖。

### 变更 / 重构
- **OneBot 实现整体迁出框架内核**：删除 `framework/onebot_api.py`、`framework/onebot_caller.py`、
  `framework/websocket_handler.py`；38 个 OneBot 动作迁入 `core_plugins/onebot_adapter/onebot_api.py`，
  `onebot_adapter/main.py` 自包含反向 WS 连接、事件归一化与 ApiCaller，并实现 `send_text`。
- **`ctx.onebot` 变为纯服务查找**：优先取 `onebot_api` 服务，缺失时以协议无关 `ActionProxy` 兜底，
  连接入端都没有才抛错；`ctx.api/aapi` 走通用 `api_caller`，不再与某个协议绑定。
- 终端命令的硬编码插件名单改为目录扫描，并修复原本调用不存在方法的 send/ban/kick/broadcast。
- 6 个官方插件补齐 `process` 标记：`onebot_adapter/http_inject/http_api/webui = core`，
  `session/scheduler = host`。

### 兼容性
- 所有既有公开 API、服务名、配置键、事件结构保持不变；老业务插件**零改动**即可运行。
- 关闭 `onebot_adapter` 后，框架可作为纯定时 / HTTP Webhook / 其它接入端的可扩展 IM 平台运行。

### 测试
- `compileall` 全量编译通过；`tests/test_plugin_imports.py` 31/31、`tests/test_perm.py` 43/43；
- 23 项解耦专项冒烟、三角色加载矩阵与旧白名单逐插件等价核对全部通过；
- 真实冷启动验证：Web 后台（8080）、反向 WS（6830）握手与事件注入正常，优雅停机退出码 0。

### 文档 / 表述
- 全项目文档去除 emoji：README 与 6 篇 docs 统一改为文字标记（必填 / 推荐 / 兼容、提示 / 注意、正确与错误示例），表格与列表结构不变。
- 版本来源统一：`VERSION` 文件是唯一版本出处（WebUI、`/api/version`、终端 `version`、`http_api` 元数据均读它），本次发布 v1.3.5。

## v1.3.3（2026-09-10）

> 主题：彻底修掉"完全热重载后仍跑旧代码"的字节码缓存问题。

### 修复
- CPython 默认按「源码整数秒 mtime + 文件大小」校验 `.pyc`，同一秒内把模块改成相同字节数时会误判字节码有效、复用旧代码。
- 新增 `_PluginSourceLoader`：主模块与顶层子模块每次加载都现场从 `.py` 源码编译，且不写 `__pycache__`，从根上绕开字节码缓存，同秒、同尺寸改写也能立即生效。
- 保留 `_clear_plugin_bytecode_cache()`：建包前清掉本插件各层 `__pycache__` 并 `invalidate_caches()`，覆盖由原生导入机制懒加载的深层嵌套包。
- 卸载/重载的 `sys.modules` 清理（`_purge_plugin_modules`）经压测确认无残留、不误伤其他同名插件。

### 测试 / 文档
- `test_plugin_imports.py` 新增「同秒同尺寸快速重载」回归，共 31 项全过；新增卸载/重载压测（双插件同名模块交错卸载、反复重载、运行时懒加载嵌套包）全部通过。
- `docs/loader.md` 增补「为什么插件不使用 `__pycache__`」并扩充"改了代码不生效"FAQ。

## v1.3.2（2026-09-10）

> 主题：插件支持 Python 原生相对导入，文档体系全面扩写。

### 新增 / 修复
- 插件加载支持原生相对导入（`from .xxx import Y` / `from . import xxx`），不再报 `attempted relative import with no known parent package`。
- 主模块 `plugin_<名>` 同时充当带 `__package__/__path__` 的合成包，作为插件内部相对导入的父包。
- 子模块规范名改为点分层级 `plugin_<名>.<模块>`，并保留旧下划线唯一名与短名别名（指向同一模块对象），旧的短名绝对导入写法完全兼容；多插件同名模块零冲突。
- 新增统一的 `_purge_plugin_modules`，卸载、重试与各失败路径都会回滚 `sys.modules`，避免半初始化模块残留；合入调度器为 None 时的守卫，避免空指针。

### 文档 / 测试
- 新增《插件加载与模块机制》（`docs/loader.md`）：合成包、三层模块名、相对/绝对导入规则、热重载与卸载清理、排错 FAQ。
- 全面扩写编写插件、ctx、Event、架构、数据库、定时任务、权限、会话、配置、部署等 16 篇文档，并订正与代码不符的旧描述。
- 新增 `tests/test_plugin_imports.py`（29 项断言全过）。

## v1.3.1（2026-09-09）

### 新增
- 独立 HTTP API 插件 `core_plugins/http_api`（**默认关闭**，默认 `127.0.0.1:1145`，共享 token 认证）。
- 终端新增 `update` 命令，支持框架一键更新。
- WebUI 日志页新增终端命令输入框与 `/api/terminal/exec` 接口。

### 修复
- 路由表 `post_type/type` 字段名不匹配导致消息丢失。
- 更新白名单补充 `core_plugins/webui`；心跳跳过核心插件。
- `session` 定时任务 handler 找不到；`core_plugins` 缺 `priority` 字段导致路由表刷新 KeyError。

## v1.3.0（2026-09-09）

> 主题：**插件化架构重构**——把协议接入、WebUI、会话、调度等官方能力从内核拆为 `core_plugins`，框架回归"壳 + 插件"；同期落地终端交互。
> （内部经历过 `v1.3.0-beta.0`、`v1.3.0-beta.0-alpha.0` 两个未单独外发的里程碑。）

### 新增
- 官方能力插件化：`onebot_adapter`（反向 WS 接入）、`webui`、`session`、`scheduler` 等以 `core_plugins` 形式加载、可在配置中开关。
- 框架终端交互（`terminal`）：可在控制台执行命令；修复 `exit` 不退出进程、`_start_time` 未定义、uptime 显示等问题。

### 重构
- 内核按"加载器 / 路由 / 事件 / 上下文 / 协议适配"重新分层，为后续协议解耦（v1.3.5）打基础。

---

# 第四代 · 权限与治理（v1.2.0-beta）

## v1.2.0-beta.1（2026-09-08，预发布）—— 权限系统 + 接口令牌

### 新增
- **节点式权限系统**（`framework/perm.py`）：权限节点模型、三态判定、通配 `*`、显式否决、组继承 `group.*`、上下文隔离（group/bot/msgtype）、临时权限（expire_at）、Tracks 晋升轨道、审计日志、60s 缓存。
- 内置角色组 `__member <- __admin <- __owner <- __super`（映射 `zcbot.role.*`）。
- `Event`/`ctx` 新增 `has_perm / check_perm / perms / perm_groups / primary_group`；`command(require_perm=)` 与既有 `require_level` 双轨并存。
- **接口令牌 API Key**：`api_tokens` 表 + `/api/apikeys` 创建/吊销（需超管，token 仅明文返回一次）；鉴权兼容 2048 位会话 token 与 ≥40 位 API Key。
- Web 后台新增「权限管理」（5 标签页）与「接口令牌」页；框架定时清理过期权限节点。

### 修复 / 文档
- 修复 FileBrowser 面包屑丢失前导 `/` 导致相对路径 400。
- 补权限系统开发文档与 API 文档的权限/接口令牌章节。

## v1.2.0-beta.0（2026-08-30，预发布）—— 插件孤儿任务自检

### 新增
- **插件孤儿任务自动自检校正**：`PluginLoader.self_check_orphans()` 随心跳（默认每分钟，受 `plugin.heartbeat_interval` 控制）执行——
  - 清理 `asks/commands` 表中代码目录已不存在的孤儿条目（任务同时移出调度器）；
  - 移除调度器中属于"当前未加载插件"的任务；
  - 清理后令路由缓存失效，保证内存与数据库对齐。
- 基线包含 v1.1.2 全部修复（重启加载禁用插件、ZIP 上传符号链接、A1 事件顺序、A13 黑名单拦截）。

---

# 第三代 · 正式版与健壮性（v1.0.x / v1.1.x）

## v1.1.2（2026-08-30）

### 修复
- 修复重启后仍加载已禁用插件等一批已知问题（含 ZIP 上传符号链接校验、A1 事件顺序、A13 黑名单拦截，详见 v1.2.0-beta.0 基线说明）。

## v1.1.1（2026-08-22）—— 插件管理修复

### 修复
- **插件上传不再残留 `.bak` 目录**：原先每次上传都把旧目录备份为 `.bak.{时间戳}` 且从不清理，反复上传会堆积、Windows 上难删除；现上传前清理历史 `.bak`，上传后立即删除当前 `.bak`。
- **插件更新后配置项正确同步**：`_conf_schema.json` 旧逻辑在 `plugins_dat` 已有时不覆盖，导致升级后新增/修改的配置项不显示；现随 `plugin.yaml` 始终覆盖，并自动删除废弃 key、补全新字段默认值、保留用户已改值。
- 清理遗留：删除已被 `webui/` 完全替代的 `web_legacy/` 目录。

## v1.1.0-beta.0（2026-08-22，预发布）—— 内存看门狗

### 新增
- 进程 RSS 看门狗：每 30s 检查进程内存，超过 `memory.limit_mb`（默认 120MB）时清空框架级角色缓存、统计聚合计数并强制 `gc.collect()`，打印清理前后 RSS；阈值/频率可在 `config.yaml → memory` 调整。

## v1.0.1（2026-08-22）—— 稳定性 / 内存 / 安全

### 优化
- 图片渲染字体缓存改为 LRU 上限，修复无限增长的内存泄漏（约 8MB/字号）。
- `ctx.get_config` 增加 30s TTL 缓存，async handler 不再每次同步查库阻塞事件循环。

### 修复 / 安全
- 不再直接信任 `X-Forwarded-For`：仅采信 `security.trusted_proxies` 白名单内反代的 XFF，防伪造 IP 绕过黑名单/限速/远程封禁。
- Web API 增加 CORS 支持，可用 `security.cors_allowed_origins` 收紧来源。

## v1.0.0（2026-08-20）—— 首个正式版

> 重点强化 Web 前端的可扩展性、健壮性与个性化。

### 重大特性
- 插件可整体接管前端（`override_webui`）：`/` 自动重定向到插件网页入口，插件被禁用/卸载时自动回退默认前端。
- custom_ui 个性化前端插件：从 GitHub 拉取网页模板（zip），支持模板选择/下载/安装/切换，刷新网页生效。
- 快刷检测与恢复页：同一 IP 5 秒内刷新 ≥5 次自动重定向到 `/reset` 恢复页。

### 框架更新
- 更新支持指定版本号（可下拉选择，默认最新）；本地已最新也允许更新；移除备份/回滚。

### 修复
- 插件管理页 `applyFilter` 崩溃；插件更新后 `plugin.yaml` 元信息被旧缓存遮挡；custom_ui 路由热重载时 `add_url_rule` 报错。

---

# 第二代 · 公测与界面现代化（v0.0.1-beta / v0.1.0-beta）

## v0.1.0-beta.2（2026-08-20，预发布）

### 修复
- 插件管理页崩溃：`Plugins.vue` 模板引用未定义的 `applyFilter`，组件渲染时报 `withKeys undefined`、页面空白；已补全定义并重建前端产物。

## v0.1.0-beta.1（2026-08-20，预发布）

### 界面体验
- 亮/暗主题一键切换，跟随系统偏好并记忆选择（localStorage），全站适配（含登录页）。
- 路由切换淡入淡出 + 位移动效；卡片悬浮、按钮按压、侧边菜单过渡、滚动条等微交互。
- 恢复品牌 Logo（登录页与侧边栏），新增 `/img/` 静态资源路由。

### 修复
- 移除命令管理中的"关键词回复"界面（该能力交由插件，如 keyword_api）；前端构建产物同步。

## v0.1.0-beta.0（2026-08-20，预发布）—— WebUI 现代化重写

### 重大变化
- Web 管理面板**全量重写**：原生 HTML/JS → Vue 3 + Vite + Element Plus，覆盖全部 14 个管理页面（仪表盘/插件市场/插件管理/命令管理/用户管理/群组管理/定时任务/运行状态/连接管理/文件管理/运行日志/数据库管理/插件 WebUI/全局设置）。
- 构建产物按需分包（element-plus、vue 独立 chunk），首页 JS 仅约 8KB；旧前端保留为 `web_legacy/`，`webui/` 为需 Node 构建的源码工程。

### 文档 / 修复
- 新增 KNOWN_ISSUES.md、debugging.md、plugin-tutorial.md，扩写 best-practices、重写 INDEX、更新 README。
- 修复插件页切换路由 key 解析失败；修复 `Event.role` 缓存缺 global 声明导致的权限降级。

## v0.0.1-beta.1（2026-08-19，预发布）

### 框架修复
- contextvars 多 bot 并发修复（回复不错发）；事件订阅去重（心跳重注册不再重复回复）；多 bot 连接注册表泄漏修复 + X-Self-ID 识别。
- OneBot 协议补齐（`send_like` 封装 / meta 事件广播）；修复内存泄漏 7 处（角色缓存/任务引用/队列/nonce 等）。
- 图片渲染内存优化（字体缓存 + malloc_trim 定期归还 OS）；仪表盘卡片超时隔离、数据库统计缓存。

### 新增基础插件
- `session_waiter`（多轮会话，等待用户下一条消息）、`message_guard`（唤醒词/白名单/限流/敏感词）、
  `plugin_depgraph`（插件依赖图，WebUI + `/依赖` `/依赖图`）、`ui_ext_demo`（群组/用户管理页扩展演示）。

### 其他
- `/status` 图片版状态卡；文档全面重写（手把手入门）；WebUI 群组/用户管理页插件扩展接口。

## v0.0.1-beta.0（2026-08-11，公测版）

公测首发。基于 OneBot v11 协议的事件驱动的 IM 平台，提供插件化架构、Web 管理面板、双数据库支持等核心能力。

### 框架核心
- 全异步架构（消息处理/API 调用/定时任务均不阻塞事件循环）；插件 handler 支持 `async def`，旧同步插件自动桥接到线程池。
- 插件热加载/热卸载、动态注册指令；反向 WebSocket 服务端，兼容 Lagrange/NapCat/go-cqhttp 等 OneBot v11 客户端。
- 内存路由表（热路径零 DB 查询、零线程切换）；统计批量异步写库；数据库专用线程池与消息处理隔离。

### 数据库
- 默认 SQLite 零配置；可选 MySQL，自动翻译方言 SQL（`ON DUPLICATE KEY UPDATE`→`ON CONFLICT`、`NOW()`→参数化、ENUM→TEXT 等）。
- MySQL DBUtils 连接池（有上限、空闲回收、坏连接重建）、自动重连；`ctx.create_table()` 统一建表入口，长列索引自动改前缀索引。

### Web 管理面板 / 安全 / 插件开发
- 侧边导航 + 深色主题，覆盖仪表盘/插件/命令/用户/群组/任务/日志/设置；ZIP 安装、热重载、依赖安装、隔离虚拟环境、GitHub 更新检查。
- 系统级关键词自动回复（动态命令，四种匹配方式）；SSE 实时日志；审计日志；仪表盘卡片；插件 WebUI；群级插件开关。
- Bearer Token（2048 位 hex）+ 双请求防破解（蜜罐探针 + nonce 挑战）；登录防爆破、IP 黑名单持久化、内网豁免；角色体系 超管>群主>管理员>普通用户>黑名单。
- `PluginContext(ctx)` 全能力 + 同步/异步双 API；代码与数据分离；依赖自动安装（多镜像回退）与独立虚拟环境；`plugin.yaml`/`_conf_schema.json`；生命周期钩子。

### 内置插件
- echo、help、image_renderer（Rust+pyo3 原生渲染，按架构绑定、缺失回退 PIL）、restart_manager、runtime_status。

### 已知限制
- 公测版可能不稳定；SSE 日志推送需用 fetch + ReadableStream（EventSource 不能自定义请求头）；未实现 API 限流（建议反代限流）；原生渲染扩展仅支持 Win x64 / Linux x64 / Linux aarch64。

---

# 第一代 · 诞生（v0.0.1-alpha，持续构建）

> 2026-08-04 首发，随后以 `build.N` 高频迭代（08-06 ~ 08-10）。下列构建号均有对应 Git Tag（无 build.12 / build.16 标签）。

## v0.0.1-alpha.0-build.4（2026-08-04）—— 首版

- OneBot v11（反向 WebSocket）全异步架构，同步插件自动兼容；插件化热加载/热卸载、依赖冲突检测与隔离虚拟环境。
- 内置 Web 管理面板（仪表盘/插件/命令/用户/群组/任务/日志/设置）；角色权限 + 登录防爆破、SSE 实时日志。
- SQLite 零配置，可选 MySQL 自动翻译方言；内置插件 echo / help / runtime_status / image_renderer。

## v0.0.1-alpha.1-build.5 ~ build.23（2026-08-06 ~ 08-10）

| 构建号 | 日期 | 要点 |
|--------|------|------|
| build.5 | 08-06 | 数据库自动重连；框架 Web 面板一键更新（ZIP，仅覆盖框架代码、自动备份）；VERSION 文件驱动的 Release 版本检测；公开 `/api/version`；插件市场改按 GitHub API 文件树索引下载；IP 黑名单持久化 + 内网豁免；双请求防破解认证 |
| build.6 | 08-06 | image_renderer 引入 Rust+pyo3 原生渲染（Win x64/Linux x64/aarch64，缺失回退 PIL），三平台二进制入库；第三方插件与 llm_plugin_gen 移出框架仓库、改走官方插件市场 |
| build.7 | 08-06 | 加载 main.py 时注册 `sys.modules`（修复热重载残留）；更新检测走 GitHub Release |
| build.8 | 08-06 | 日志页 SSE 改轮询，修复占满 waitress 线程导致 WebUI 卡死；插件市场改进（GitHub 加速、磁盘判定"已安装"、失败回滚、缓存兜底、幽灵插件标记） |
| build.9 | 08-07 | MySQL 改用 DBUtils `PooledDB`，修复连接只增不减、全 Sleep；连接参数真正生效、坏连接透明重建 |
| build.10 | 08-07 | 框架更新走 Release tag ZIP；下载逐候选校验 ZIP 魔数，跳过镜像返回的 HTML 错误页 |
| build.11 | 08-07 | **重要修复**：卸载插件不再删除 `plugin_configs`，重载/更新/禁用不再重置用户配置（仅真正删除插件时清配置） |
| build.13 | 08-07 | **框架防堵塞**：连接池有界等待（`blocking=False` + 借连超时）、新增 DB 专用线程池与默认线程池隔离，DB 繁忙不再拖垮消息处理 |
| build.14 | 08-07 | 内存路由表（5s 快照、热路径零 DB）；群开关/插件启用内存缓存与失效钩子；角色 60s TTL 缓存；生命周期钩子 `on_loaded/on_error/after_message_sent`；更新自动装依赖、备份/回滚入口 |
| build.15 | 08-07 | 修复插件 WebUI iframe 401：token 同步 HttpOnly Cookie 兜底鉴权 |
| build.17 | 08-07 | 深/浅主题切换（CSS 变量、跟随系统、无闪烁）；文件管理升级为"宝塔风"（面包屑/新建/上传/重命名/删除/下载/搜索）及对应 `/api/files/*` 接口 |
| build.18 | 08-07 | 401 自动种 Cookie 免重登；文件浏览右键菜单；新增 `/api/files/copy` |
| build.19 | 08-08 | 无文本消息（分享卡片等）广播 `message.<段类型>` / `message.media`；`Event` 新增 `has_share/share` |
| build.20 | 08-08 | **系统级动态命令（关键词自动回复）落地**：exact/prefix/contains/regex 四种匹配，正则预编译进内存路由表；`/api/dynamic-commands` 与命令管理页卡片 |
| build.21 | 08-08 | 框架改进 6 项：分享/JSON 卡片链接提取、`message` 统一文本事件、`continue_route()`、关键词 handler 回调、MySQL 前缀索引、`ctx.create_table()` 统一入口 |
| build.22 | 08-10 | 修复插件页切换路由 key 解析失败（先截断 `?` 再取段）；侧边栏进入插件页自动带 name |
| build.23 | 08-10 | 修复 GitHub 加速候选 URL 双重协议拼接 bug；默认加速代理改为 `gh.jasonzeng.dev`，失败回退内置镜像与直连 |

---

## 版本编号与发布说明

- 正式 Release 与历史构建号均可在 GitHub Tags/Releases 查到：<https://github.com/kuangxing6367/zcbot/releases>。
- `VERSION` 文件记录当前框架版本；插件通过 WebUI/`/api/version` 看到的版本以此为准。
- 文档与代码如有出入，以当前代码与最新 Release 说明为准，并欢迎在 Issue 指出。
