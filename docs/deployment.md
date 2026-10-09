# 部署

本地 `python main.py` 跑通之后，下一步通常是把它放到服务器上长期运行：进程要能自拉起、端口要只对必要来源开放、数据要定期备份。按目标环境挑一条路径即可。

## 上线前测试门禁

发布到服务器前先跑测试，不要让回归靠线上日志暴露：

```bash
# 依赖安装完成后，跑完整 pytest 回归（CI 与本地同一条命令）
python -m pip install pytest
python -m pytest tests/test_smoke.py tests/test_html_assembler.py \
  tests/test_event_buffer.py tests/test_qq_official.py \
  tests/test_scheduler.py tests/test_db_regression.py tests/test_loop_fix_regression.py -q
```

- 仓库 `.github/workflows/tests.yml` 在每次 push / pull_request 时自动执行上述套件，未通过的分支不应合入主干；
- 手动部署（服务器 `git pull`、Docker 构建、systemd 重启）前，在目标机器或 CI 上跑一遍 pytest 全绿后再切换流量；
- 其余 `test_*.py`（如 `test_plugin_imports.py`、`test_dual_core.py`、`test_perm.py`）是模块级脚本式回归，CI 中单独执行，本地可在部署前一并跑完。

## 直接运行（开发/小规模）

```bash
python main.py
```

适合本地调试与小规模使用。管理后台默认由 **waitress**（生产级 WSGI 服务器，
依赖缺失时回退到 werkzeug）在独立线程提供服务。

## 远程部署脚本（宝塔 / Linux）

仓库自带一条从本地到服务器的部署链路，把线上代码更新成当前工作区版本：

```bash
SSH_KEY=/path/to/id_ed25519 \
SSH_HOST=root@1.2.3.4 \
REMOTE_DIR=/www/wwwroot/bot.zgric.top/zcbot \
WEB_PORT=6080 \
bash tools/deploy_remote.sh
```

它做的事，以及为什么这么做：

- **只同步代码**：`config.yaml` / `core_plugins.yaml` / `data/` / `plugins/` 一律不动 ——
  线上这些是真实配置与数据（含数据库口令），覆盖即事故；
- **排除原生库**（`*.so` / `*.pyd` / `image_renderer/native`）：本地可能是别的平台构建，
  覆盖会让线上渲染 / 加速模块直接崩；
- **用 tar 解包**（不带删除语义），线上独有文件（如 `start.sh`）不会被删；
- **重启用目录全路径匹配进程**：宽泛的 `zcbot` 会连同机其它站点（如 `dbcj_zcbot`）一起杀掉；
- 部署前在 `/root/zcbot-backup-<时间戳>/` 留一份代码 + 配置备份，可回滚；
- `web.port` 被宝塔占用时（8080 默认归 BT-Panel）自动改成 `WEB_PORT`。

`DRY_RUN=1` 只打包并自检，不上传。

## 无 TTY 接入运行中的框架（调试控制台）

systemd / 宝塔托管时进程没有交互终端。框架启动会在 **`127.0.0.1` 上开一个调试控制台端口**，
**端口与超长随机 token 自动生成并写入数据库**（`console_access` 表），之后每次启动复用：

```bash
# 在服务器上（无需 TTY，进程照常在跑）
python main.py attach              # 交互式：直接敲终端命令
python main.py attach status       # 一次性执行一条命令
python main.py -a plugins          # 同上，-a 是 attach 简写
python main.py -a --agent          # 文本通道里跟 AI 智能体对话
```

- 凭证存放位置**跟随 `database` 配置**：SQLite 存 `data/zcbot.db`，MySQL 存对应库的
  `console_access` 表 —— 两种部署 `attach` 都能用（无需手工填端口/token）；
- 只绑回环地址，token 用常量时间比较；`config.yaml` 的 `console` 段可配
  （`enabled` / `host` / `port` / `token_bits` / `backlog` / `read_timeout`）；
- 端口被占用会重试；若该端口上**已有本框架的控制台在跑**（多实例），新实例放弃监听且
  **不覆盖凭证**，避免把先跑那个实例的接入入口打掉。

## Linux systemd 常驻

```ini
# /etc/systemd/system/zcbot.service
[Unit]
Description=ZCBOT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/zcbot
ExecStart=/opt/zcbot/.venv/bin/python /opt/zcbot/main.py
Restart=always
RestartSec=5
# 安全加固（按需）
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zcbot
journalctl -u zcbot -f          # 查看实时日志
```

## Windows 后台运行

- 简单方式：`pythonw main.py`（无控制台窗口），或用「任务计划程序」设置开机启动；
- 稳定方式：用 [NSSM](https://nssm.cc/) 把 `python main.py` 注册为 Windows 服务，
  可设置崩溃自动重启。

## Docker（自行构建）

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY . .
EXPOSE 8080 6830
CMD ["python", "main.py"]
```

```bash
docker build -t zcbot .
docker run -d --name zcbot \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/plugins:/app/plugins \
  -p 127.0.0.1:8080:8080 \
  -p 6830:6830 \
  zcbot
```

:::tip OneBot 容器互通
OneBot 客户端要能反向连到容器的 6830 端口；若客户端在宿主机/其他容器，
注意网络与地址（容器内 `127.0.0.1` 只指向容器自身）。
:::

## 反向代理与 HTTPS

生产环境建议让 `web.host=127.0.0.1`，由 Nginx/Caddy 终结 TLS 后反代到 8080：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

WebSocket 反向连接端口（6830）按需单独放行或代理（需要 `Upgrade` 头）。

## 多账号部署

- 多个 OneBot 客户端（不同账号）都反向连到同一个 6830，框架按连接区分 `bot_name`；
- 发送消息时不传 `bot` 会自动跟随消息来源账号；主动任务可用
  `get_connected_bots()` 枚举在线实例。

## MySQL 部署

默认的 SQLite 只适合小环境与开发环境，不适合大环境：群多、并发高、
多进程部署时把 `database.type` 切到 `mysql`，提前建库（如 `zcbot`，
`utf8mb4`），框架会自动适配 DDL 并建表；需要 `pymysql`、`DBUtils`
（切换时会提示/自动安装）。

## 安全清单

1. 在 `core_plugins.yaml` 给 `onebot_adapter.access_token` 设强随机令牌，OneBot 客户端保持一致；
2. 管理后台默认密码 `admin/admin123` 首次登录立即修改；
3. `web.host` 保持 `127.0.0.1`，公网访问走反代 + HTTPS + IP 白名单；
4. 配置 `security.whitelist_ips` 限制管理接口来源；
5. 定期备份 `data/`（SQLite 文件、日志、插件数据）与 MySQL 数据库；
6. 以非 root 用户运行 systemd/Docker 服务。

## 备份与迁移

需要备份的内容：

- `data/`：SQLite 数据库、日志、`plugins_dat/` 插件数据；
- `config.yaml`：全局配置；
- `core_plugins.yaml`：官方插件的开关与配置；
- `plugins/`：自行开发/定制的插件代码。

迁移到新机器：复制上述内容 → 安装依赖 → `python main.py` 即可。
