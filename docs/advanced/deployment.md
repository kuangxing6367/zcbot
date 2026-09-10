# 部署

> **本篇面向**：角色 A（要把宿主长期稳定跑起来的部署者）。

## 直接运行（开发/小规模）

```bash
python main.py
```

适合本地调试与小规模使用。管理后台默认由 **waitress**（生产级 WSGI 服务器，
依赖缺失时回退到 werkzeug）在独立线程提供服务。

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

- 多个 OneBot 客户端（不同 QQ）都反向连到同一个 6830，框架按连接区分 `bot_name`；
- 发送消息时不传 `bot` 会自动跟随消息来源账号；主动任务可用
  `get_connected_bots()` 枚举在线实例。

## MySQL 部署

群多、并发高时把 `database.type` 切到 `mysql`，提前建库（如 `zcbot`，
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
