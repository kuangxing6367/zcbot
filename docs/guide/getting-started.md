# 开始使用

## 启动框架

```bash
python main.py
# 也可以指定配置文件：python main.py D:\config\zcbot.yaml
```

看到类似输出表示启动成功：

```
==================================================
ZCBOT 框架 启动中...
==================================================
官方插件 [onebot_adapter] 已加载
官方插件 [scheduler] 已加载
官方插件 [session] 已加载
官方插件 [webui] 已加载
已加载 8 个用户插件: ['echo', 'help', ...]
框架启动完成，等待消息...
```

最后一行 `框架启动完成，等待消息...` 出现后，框架开始监听 OneBot 连接。
启动日志同时写入 `data/logs/zcbot.log`。

## 连接 QQ

ZCBOT 作为 WebSocket **服务端**运行（默认端口 6830），需要 OneBot 客户端
（NapCat、Lagrange 等）以**反向 WebSocket** 连入。

### NapCat 配置示例

在 NapCat 的网络配置中新增「反向 WebSocket 客户端」：

- 目标地址：`ws://127.0.0.1:6830`
- AccessToken：与 `config.yaml → onebot.access_token` 保持一致（本地调试可留空）

等价 JSON 片段：

```json
{
  "WebSocketReverse": {
    "Enable": true,
    "URL": "ws://127.0.0.1:6830",
    "AccessToken": ""
  }
}
```

### 验证连接

框架日志出现下面这行即代表客户端已连入：

```
[bot_xxxxxx] OneBot 客户端已连接
```

多账号场景下每个客户端相互独立，发送消息时框架会自动选用消息来源账号，
插件无需手动指定 bot。

## 第一次对话

在 QQ 中向机器人发送：

```
/echo 你好
```

机器人回复 `你好` 即链路正常。`/help`（或 `/帮助`、`/菜单`）可查看命令列表。

## Web 管理面板

浏览器访问 `http://127.0.0.1:8080`（地址端口以 `config.yaml → web` 为准），
默认账号：

- 用户名：`admin`
- 密码：`admin123`

面板里可以：启用/禁用/重载插件、管理命令与定时任务、管理用户/群组权限、
查看仪表盘与日志、在线编辑插件配置。

:::warning 首次登录必做
登录后立即到个人设置修改默认密码；若面板需要公网访问，
把 `web.host` 保持在 `127.0.0.1` 并通过反向代理暴露，同时设置好
`onebot.access_token` 与 IP 白名单。
:::

## 下一步

- [编写你的第一个插件](./writing-plugins.md)
- [多文件插件与模块导入机制](../advanced/loader.md)
- [配置详解](./configuration.md)
- [部署上线](../advanced/deployment.md)
