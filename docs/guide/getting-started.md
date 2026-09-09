# 开始使用

## 启动框架

```bash
python main.py
```

看到以下输出表示启动成功：

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

## Web 管理面板

浏览器访问 `http://127.0.0.1:8080`，使用默认账号登录：

- 用户名：`admin`
- 密码：`admin123`

⚠️ 首次登录后请立即修改默认密码！

## 连接 QQ（可选）

如果需要连接 QQ，启动一个 OneBot 客户端（如 NapCat、Lagrange），配置反向 WebSocket 连接到 `ws://127.0.0.1:6830`。

### NapCat 配置

在 NapCat 的 `config.json` 中添加：

```json
{
  "WebSocket": {
    "Enable": true,
    "Host": "0.0.0.0",
    "Port": 6830,
    "AccessToken": ""
  },
  "WebSocketReverse": {
    "Enable": true,
    "URL": "ws://127.0.0.1:6830",
    "AccessToken": ""
  }
}
```

### 验证连接

框架启动后，看到以下日志表示客户端已连接：

```
[bot_1] OneBot 客户端已连接
```

## 第一次对话

在 QQ 中发送：

```
/echo 你好
```

框架会回复：

```
你好
```
