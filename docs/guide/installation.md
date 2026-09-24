# 安装

想把 ZCBOT 跑起来，按下面 4 步走完就行。不需要编程基础；依赖装漏了启动时也会自动补。

## 开始前先备好这些

| 需要 | 说明 |
| ---- | ---- |
| Python 3.10+ | 建议 3.10–3.14；Windows / Linux / macOS 均可 |
| （可选）聊天接入端 | 只有要收发聊天消息才需要；纯定时 / Webhook 可以先不接。对接见 [对接 IM 平台](./connect-im.md) |

## 第 1 步：下载代码

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
```

没有 git 的话，也可以在 GitHub 页面点 **Code → Download ZIP**，解压后进入目录。

## 第 2 步：虚拟环境（推荐，可跳过）

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

## 第 3 步：装依赖

```bash
pip install -r requirements.txt
```

> 依赖也写在 `pyproject.toml` 里，两处保持同步：日常启动自检读 `requirements.txt`，工具链读 `pyproject.toml`。
> **改依赖时两处都要改**；只使用的话装 `requirements.txt` 就够了。

常用核心包：`websockets`（连接）、`flask` + `waitress`（后台）、`apscheduler`（定时）、`bcrypt`（密码）、`psutil`（监控）。

:::tip 跳过这步也行
`python main.py` 启动时会自检 `requirements.txt`，缺包会走内置镜像（清华→阿里→豆瓣→官方）自动装。
插件自己的依赖也会在加载时自动装。
:::

MySQL 用户额外需要 `pymysql`、`DBUtils`（切到 MySQL 时平台内核会提示并安装）。

## 第 4 步：启动

```bash
python main.py
```

首次启动会生成 `config.yaml`、`core_plugins.yaml` 和 `data/`。

看到类似输出即成功：

```
ZCBOT 框架 启动中...
官方插件 [onebot_adapter] 已加载
官方插件 [webui] 已加载
...
框架启动完成，等待事件...
```

**接着做：**

1. 浏览器打开 `http://127.0.0.1:8080` → 进 [开始使用](./getting-started.md)
2. 要连聊天软件 → 进 [对接 IM 平台](./connect-im.md)
3. 要写功能 → 进 [编写插件](./writing-plugins.md)

---

## 目录结构（以后会用到的）

```
zcbot/
├── main.py                 # 启动入口：python main.py [自定义配置路径]
├── config.yaml             # 全局配置（首次启动自动生成）
├── core_plugins.yaml       # 官方插件开关/配置（启动自动同步）
├── requirements.txt        # 依赖清单（启动自检读它）
├── pyproject.toml          # 项目元数据（与 requirements 同步）
├── framework/              # 平台内核
├── core_plugins/           # 官方插件（在 yaml 里开关）
│   ├── onebot_adapter/     #   OneBot 11（默认开）
│   ├── webui/              #   Web 后台（默认开）
│   ├── session/            #   多轮会话（默认开）
│   ├── scheduler/          #   定时任务（默认开）
│   ├── telegram/ discord/  #   其它接入端（默认关，填凭证后开）
│   ├── qq_official/ ws_client/ http_inject/ http_api/
├── plugins/                # 你的插件（每个一个文件夹）
├── data/                   # 运行数据：logs/、数据库、plugins_dat/
├── web/                    # 后台前端静态资源
├── sql/                    # 建表脚本
├── tests/                  # 自测
└── docs/                   # 本文档
```

日常最常碰：`plugins/`（写功能）、`core_plugins.yaml`（开关/接平台）、`data/`（日志与数据）。

## 升级

```bash
git pull
pip install -r requirements.txt   # 补全新依赖
```

插件配置和数据库都在 `data/` 下，**升级不会清空**。旧版散落在插件目录里的配置会自动迁到 `data/plugins_dat/<名>/`。

## 下一步

- [开始使用](./getting-started.md) —— 验证启动、第一次对话、进后台
- [对接 IM 平台](./connect-im.md) —— 连上真实聊天软件
- [编写插件](./writing-plugins.md) —— 写你的第一条命令
