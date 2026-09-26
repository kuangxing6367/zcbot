# ZCBOT LLM 索引

> 给 LLM 的按需加载入口。**先读本页,再按任务只加载对应的一份文档**,不要全读。

## 硬事实

- 事件驱动的 IM 平台:内核(framework/) + 官方插件(core_plugins/) + 用户插件(plugins/)
- Python 3.10+,启动:`python main.py`;默认无 OneBot 代码,OneBot 11 只是默认接入端
- 数据在 `data/`(db/logs/plugins_dat),代码目录更新会被覆盖,数据禁止写代码目录
- 配置两份:`config.yaml`(全局) + `core_plugins.yaml`(官方插件开关,启动自动同步)
- 适用版本 v1.7.0;文档站 docs/(VitePress)

## 按任务加载

| 任务 | 读 |
| ---- | ---- |
| 写插件 / 调插件 API | [docs/llm/plugins.md](docs/llm/plugins.md) |
| 改框架 / 找模块 / 理解事件流 | [docs/llm/framework.md](docs/llm/framework.md) |
| 跑测试 / 排查报错 / 避坑 | [docs/llm/debugging.md](docs/llm/debugging.md) |

人类文档:[README.md](README.md) · [docs/](docs/guide/)
