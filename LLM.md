# ZCBOT LLM 索引

> 给 LLM 的按需加载入口。**先读本页,再按任务只加载对应的一份文档**,不要全读。

## 硬事实

- 通用化 IM 平台(协议无关):内核(framework/) + 官方插件(core_plugins/) + 用户插件(plugins/)
- Python 3.10+,启动:`python startup.py`;内核不含任何协议接入代码;OneBot 11 只是**可选接入端之一**,不预设任何接入端
- **官方插件默认 `{echo, help, scheduler, webui}` 开启**,其余需显式启用:`core_plugins.yaml` 改 true 或 `python tools/scan_core_plugins.py --enable <名>`
- 数据在 `data/`(db/logs/plugins_dat),代码目录更新会被覆盖,数据禁止写代码目录
- 配置两份:`config.yaml`(全局) + `core_plugins.yaml`(官方插件开关,启动自动同步)
- 适用版本以仓库根 `VERSION` 为准（不在本文写死版本号，避免文档漂移）;文档站 docs/(VitePress);可 pip 安装(`pip install zcbot`,入口 `zcbot`,状态落 `~/.zcbot`)
- **三条启动路径并存**:单进程(默认) / `dual_process` 双 Python 进程 / **`python startup.py --iipc`**
  (IIPC:Go 内核 + Rust 协议端 + 独立进程插件)。后两者**不是一回事**,别混:IIPC 内核是 Go 进程,
  DB 在 Go 侧执行不占 GIL;不带 `--iipc` 时主框架行为完全不变
- **IIPC 已搁置**:新功能只在主框架落地,IIPC 侧不跟进(见 [docs/速查/IIPC内核链路.md](docs/速查/IIPC内核链路.md) 顶部)
- Web 终端与 AI 智能体:`/api/console/{commands,exec,agent}` 直接复用 `ConsoleServer.run_command` / `run_agent`,
  超管专用 + 审计 + 超时;不要再写第二套执行器
- **文件改动只有一个记账点**:`core_plugins/aiwriter/changeset.py`。智能体的 `write`/`edit`/`plugin_new`
  经 `ToolRegistry` 落到 changeset(写前快照 → diff → 整轮回滚),别在别处另记一份、
  也别绕过 `ToolRegistry` 直接 `open(...,'w')`——那样这轮改动就退不回去
- 会话与改动的 Web 入口在 `framework/api/aiwriter.py`(只做 HTTP + 权限 + 审计,逻辑在 aiwriter 服务里)
- **数据库自动备份只有一处实现**:`framework/database/backup.py`(`run_backup` 全流程 +
  `BackupService` 守护线程,默认每 6 小时)。`framework/api/backup.py` 只做 HTTP 适配。
  别在别处另写快照/压缩/轮转——迁移前备份(`migrations.backup_snapshot`)共用同一套
  路径解析(`backup.resolve_db_path`)与「保留 2 份」口径,改一处即可
- **空间探测是「探到边界就撤」,不是「填满磁盘」**:`elastic_probe` 写到可用空间只剩
  `min_free_after_mb`(默认 512KB)立刻停手并在 `finally` 里删探针。任何「把盘写满」
  的改法都是错的——正在跑的数据库会直接写不进去
- **备份先写 `.part`,校验通过才 rename**:在 `.part` 上校验时按「去掉 `.part` 后的逻辑
  扩展名」判断格式,否则 CRC/quick_check 会被静默跳过(看起来验过,其实只比了体积)
- MySQL 不走文件快照(锁表 + 体积不可控),`run_backup` 直接返回 `skipped`

## 内核设计哲学

改 `framework/` 或写扩展前,先读 [docs/速查/框架结构.md](docs/速查/框架结构.md) 顶部的「内核设计哲学」段,以及人类版 [docs/进阶/设计哲学.md](docs/进阶/设计哲学.md)。核心一条:**扩展功能必须走 hook/服务注册,禁止往内核塞业务或特判某个接入端**。

## 按任务加载

| 任务 | 读 |
| ---- | ---- |
| 写插件 / 调插件 API | [docs/速查/插件开发.md](docs/速查/插件开发.md) |
| 改框架 / 找模块 / 理解事件流 | [docs/速查/框架结构.md](docs/速查/框架结构.md) |
| 跑测试 / 排查报错 / 避坑 | [docs/速查/调试排错.md](docs/速查/调试排错.md) |
| 查操作审计 / 探针 / 指标 / 结构化日志 | [docs/进阶/可观测性与运维.md](docs/进阶/可观测性与运维.md) |
| **IIPC 链路（`--iipc`）** | [docs/速查/IIPC内核链路.md](docs/速查/IIPC内核链路.md) |
| **加固 API / 前端安全** | [docs/速查/安全加固.md](docs/速查/安全加固.md) |

人类文档:[README.md](README.md) · [docs/](docs/index.md)
