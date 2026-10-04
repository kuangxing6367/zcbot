# 扩展清单

> **官方扩展默认全部关闭**（`core_plugins.yaml` 中 `enabled: false`，发现即禁用）。
> 启用方式：把对应块 `enabled` 改为 `true`，或 `python tools/scan_core_plugins.py --enable <名>`，然后重启。

## 官方内置扩展（core_plugins/）

| 扩展 | 作用 |
| ---- | ---- |
| **onebot_adapter** | OneBot 11 反向 WS 接入端（协议端连入） |
| **rust_accel** | OneBot 的 **Rust 加速接入端**（启用前先停 `onebot_adapter`，详见 `core_plugins/rust_accel/README.md`） |
| **ws_client** | 正向 WS 接入端（主动连协议端） |
| **qq_official** | QQ 官方机器人接入（需 AppID/AppSecret） |
| **telegram** / **discord** | Telegram / Discord Bot 接入（需 Token） |
| **webui** | Web 管理后台（端口/仪表盘/插件市场） |
| **session** | 多轮会话：`ctx.wait_for()` / `ctx.create_session()` |
| **scheduler** | 定时任务调度（APScheduler cron） |
| **http_api** | 独立对外 HTTP API |
| **http_inject** | HTTP 事件注入端 |
| **image_renderer** | 通用图片渲染引擎（卡片/文字图，Rust 原生加速、缺失回退 PIL） |
| **html_assembler** | 单文件 HTML 装配引擎：占位符替换 + 图片 base64 内嵌 |

## 随项目内置的用户扩展（plugins/）

| 扩展 | 作用 |
| ---- | ---- |
| **echo** | `/echo 内容` 原样返回，链路自测 |
| **help** | `/help` 生成图片帮助菜单 |
| **runtime_status** | `/status` `/info` 运行状态（含图片状态卡） |
| **message_guard** | 消息防护：唤醒词/白名单/限流/敏感词 |
| **plugin_depgraph** | 插件依赖关系扫描（`/依赖`、`/依赖图`） |
| **session_waiter** | 多轮会话基础设施示例 |
| **ui_ext_demo** | 群组/用户管理页扩展演示 |

## 官方扩展仓库（20+ 现成扩展）

把 `config.yaml → plugin.dir` 指向 [zcbot_plugins](https://github.com/kuangxing6367/zcbot_plugins) 仓库，或在后台插件市场安装。

| 扩展 | 作用 | 扩展 | 作用 |
| ---- | ---- | ---- | ---- |
| `llm_chat` | 大模型对话 | `qqadmin` | 群管理全套（禁言/踢人/撤回/审批/违禁词） |
| `llm_plugin_gen` | AI 写插件 | `fun_score` | 签到积分/排行榜 |
| `video_parse` | 视频链接解析卡片 | `send_like` | 点赞/自动点赞 |
| `broadcast` | 消息批量广播 | `file` | 服务器文件管理 |
| `custom_ui` | 接管/换肤后台 | `hitokoto` | 随机一言 |
| `plugin_memmon` | 插件内存监控 | `llm_blacklist` | LLM 对话黑名单 |

> 完整命令与用法见插件仓库说明。
