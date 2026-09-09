# 安装

## 环境要求

- Python 3.10 或更高版本（开发验证环境为 3.10–3.14）；
- 操作系统：Windows / Linux / macOS；
- 一个 OneBot 11 协议端（如 [NapCat](https://github.com/NapNeko/NapCatQQ)、Lagrange），
  用于真正连接 QQ；ZCBOT 本身只实现框架与 WebSocket 服务端。

## 下载代码

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
```

## 创建虚拟环境（推荐）

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

## 安装依赖

```bash
pip install -r requirements.txt
```

核心依赖包括 `websockets`（协议连接）、`flask` + `waitress`（管理后台）、
`apscheduler`（定时任务）、`bcrypt`（密码哈希）、`psutil`（内存监控）等。

:::tip 依赖会自愈
即使跳过手动安装，启动时 `main.py` 也会自检 `requirements.txt`，
缺失的依赖会走内置镜像源（清华→阿里→豆瓣→官方）自动补装；
插件自己的依赖在加载时也会按 `requirements.txt` 自动安装。
:::

MySQL 用户额外需要 `pymysql`、`DBUtils`（切换到 MySQL 时框架会提示/自动安装）。

## 目录结构

```
zcbot/
├── main.py                 # 启动入口（python main.py [自定义配置路径]）
├── config.yaml             # 全局配置（首次启动自动生成）
├── requirements.txt        # 核心依赖
├── framework/              # 框架核心（加载器/路由/事件/上下文/数据库…）
├── core_plugins/           # 官方插件（可在 config.yaml 开关）
│   ├── onebot_adapter/     #   OneBot 11 协议适配器（WebSocket 服务端）
│   ├── webui/              #   Web 管理后台服务
│   ├── session/            #   多轮会话管理器
│   ├── scheduler/          #   定时任务调度器
│   └── http_api/           #   独立 HTTP API（默认关闭）
├── plugins/                # 用户插件（每个一个子目录，含 main.py）
├── data/                   # 运行数据（自动创建）
│   ├── logs/               #   日志
│   └── plugins_dat/        #   各插件的配置/缓存/私有数据
├── web/                    # 管理后台默认前端静态资源
├── sql/                    # 数据库脚本
├── tests/                  # 自测脚本
└── docs/                   # 本文档
```

## 启动

```bash
python main.py
```

首次启动会生成 `config.yaml` 与 `data/` 目录。下一步见
[开始使用](./getting-started.md)。

## 升级

```bash
git pull
pip install -r requirements.txt   # 补全新依赖
```

插件配置、数据库都在 `data/` 下，升级代码不会清空；插件更新时
`plugins/<名>/` 下的配置文件会自动迁移到 `data/plugins_dat/<名>/`。
