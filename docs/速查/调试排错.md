# 调试与测试（给 LLM）

## 跑测试

```bash
# pytest 用例(必须显式列文件)
python -m pytest tests/test_smoke.py tests/test_html_assembler.py -q
# 脚本式回归(直跑;模块级 sys.exit,被 pytest 收集会 INTERNALERROR)
python tests/test_plugin_imports.py   # 插件导入回归
python tests/test_dual_core.py        # 双进程
python tests/test_perm.py             # 权限引擎
python tests/test_smoke.py            # 冒烟(双模式兼容)
```

CI(`.github/workflows/tests.yml`):py3.10/3.11/3.12 矩阵 = 脚本回归 + pytest;
`build-zcbot-render.yml` 编译原生扩展(仅 native/** 变更);`deploy-docs.yml` 部署文档站。

### IIPC 链路基准(`framework/iipc/tools/`)

```bash
python framework/iipc/tools/run_all_bench.py              # 全跑(含 go/cargo 编译)
python framework/iipc/tools/run_all_bench.py --skip-build  # 只跑基准
python framework/iipc/tools/run_all_bench.py --only e2e,stress
python framework/iipc/tools/stress_check.py --soak-n 1000  # 加大吞吐样本看是否线性
```

脚本(e2e 25 / official 32 / surface 154 / adapter 13 / stress 16 + latency)**端口全部动态分配**
(`tools/portutil.py`,bind :0 向 OS 要空闲端口),不会抢本机正在跑的 WebUI(:8080)或
OneBot 反向 WS(:6830)。要固定端口才设 `ZCBOT_E2E_ADDR` / `ZCBOT_OFFICIAL_ADDR` / `ZCBOT_SURFACE_ADDR`。

性能**三个口径互不替代**,末尾汇总里分开报:① 轻载延迟(起点取**入队时刻**,不是取出时刻)
② 饱和吞吐(按**端到端 span** 算,不是首尾回复时间差) ③ 丢失率 + 15s 级卡顿检测。

## 症状 → 排查

| 症状 | 先查 |
| ---- | ---- |
| 插件没加载 | `core_plugins.yaml` 开关;日志「官方插件 [x] 已加载/加载失败」;目录里有 main.py |
| 命令不触发 | pattern 是否含正则元字符(决定前缀/正则);`is_dynamic=1` 不参与匹配;别名逗号分隔;插件 priority |
| 命令被吞 | 某插件 raw handler 返回 True 接管了(session 插件等待中的用户) |
| 回复发不出 | 无可用接入端;`adapter_for_source` 按事件来源选端,多端并存不串线 |
| 消息没日志 | `config.yaml → log.log_raw_message` |
| 内存堆积 | 事件缓冲 `EventBuffer.stats()`(l1/l3 字节、sqlite 积压、丢弃计数);workers 默认 1,慢 handler 会排队 |
| Web 500 | http_api 群管等走 `api.acall` 并查 `status`,失败不假报成功 |

## IIPC 链路排查(`python main.py --iipc`)

> 端口/DB/管理员:改 `config.yaml` 的 `iipc:` 段(优先级 命令行 > config > 环境变量 > 默认),
> 别去改代码里的默认值。

| 症状 | 先查 |
| ---- | ---- |
| 命令没命中,但消息被广播了 | **这是正确行为**:命令命中后不广播 `message`(对齐 `router.py`)。查 pattern 是否被判成正则(含正则元字符按 re 处理)、富媒体是否被拼进提取文本 |
| handler 跑了但 `send_msg` 静默不发 | **插件进程的 stdout 不进内核日志**,`AttributeError` 之类根本看不到。让插件自己落一个日志文件再看;先怀疑 Event 属性缺失(如 `is_group`) |
| 权限判断一律 False | 分清两个 API:`ctx.has_perm(uid, node)` **不传 role 本来就是 False**(原框架 `perm.resolve` 只做 `ROLE_TO_GROUP.get(role or '')`,不查 users 表);`Event.has_perm` 必须注入自身 role |
| 整条链路卡 15s 才回 | `api.call` 没拿到 echo 响应 → 内核干等 `defaultCallTimeout=15s`。假 OneBot 必须**每个 api.call 都带 echo 应答**(与 `qpsbench.py` 同口径) |
| 突发下间歇卡顿(轻载测不出) | 适配器需先登记 echo 再发请求(pending.insert 在 send **之前**),反序会在高并发下丢响应 |
| 适配器反复重连 / 握手失败 | 反向 WS 取 `Sec-WebSocket-Key` 是否正确;`wss` 不被支持,需前置反代 |
| 吞吐数字忽高忽低 | 口径错了:延迟取**入队时刻**、吞吐取**端到端 span**;用「首尾回复时间差」会虚高一个量级 |
| 端口被占 / 连到别的进程 | 测试脚本默认动态端口;固定端口用 `ZCBOT_*_ADDR`;业务端口改 `config.yaml` 的 `iipc.ipc_addr` / `iipc.onebot_listen` |
| 插件依赖 `ctx._framework` | 内建服务型插件(如 `core_plugins/scheduler`、`session`)跑不了,IIPC 插件在独立进程没有 `_framework`;只能拖 `plugins/*` 用户态插件 |

## 日志

`data/logs/zcbot.log`;启动成功标志:`框架启动完成，等待消息...`;后台
`http://127.0.0.1:8080`(admin/admin123,首次必改密)看仪表盘/日志/在线改配置。
IIPC 链路日志走同一出口(`config.yaml → log:` 段),内核/协议端/插件输出统一汇总到启动器;
**插件自身异常需插件落文件**,不会出现在内核日志里。

## 版本发布流程

提交 `release: vX.Y.Z`(同步 VERSION + pyproject.toml + README 版本行 + CHANGELOG)
→ 打同名 tag 推送 → `gh release create`。
