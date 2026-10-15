# IIPC_onebot_adapter —— Rust 协议端（核心插件，高级实验性）

> **实验性内容，需自行编译运行。** 二进制不随源码分发。

这是 IIPC 全栈里的**协议层**，对应原框架的 `core_plugins/onebot_adapter`（Python 实现）。
命名遵循约定：核心插件 + `IIPC_` 前缀。

## 职责

- 连入 Go 内核（IIPC TCP），`adapter.register` 登记角色与 `self_id`；
- 协议侧事件 → 归一化 → IIPC `evt` 推送内核 → 插件；
- 插件动作（`msg.send` / `api.call`）→ 内核路由到本协议端 → OneBot API；
- 自身的运行日志经 stdout 汇入 launcher 统一出口（与 zcbot 框架同一个日志文件）。

## 当前状态（如实）

- **stdio 桩模式（已验证）**：stdin 每行当作一条入站 message 事件推送；`api.call`
  以桩应答（message_id 自增）。用于在没有真实 OneBot 客户端时验证全链。
- **真实 OneBot 11 反向 WS（未做）**：计划用 tungstenite 实现 WS 服务端 +
  `Authorization: Bearer` / `?access_token` 鉴权 + `X-Self-ID` 多连接路由 +
  `post_type` 全类型归一化 + echo 回协。对应原框架 `onebot.listen_port` /
  `onebot.access_token` 配置语义。

## 编译与运行

```bash
cargo build --release
# 产物：target/release/zcbot_adapter.exe
# 启动器会自动拉起；手动：
zcbot_adapter.exe --addr 127.0.0.1:18730 --self-id rustbot
```

## 移植映射（原 onebot_adapter → 本协议端）

| 原框架（Python） | 本协议端（Rust） | 状态 |
|---|---|---|
| 反向 WS 服务端 + token 鉴权 | tungstenite WS 服务端 | 待做 |
| `post_type` 分发（message/notice/request/meta_event） | 事件归一化 → IIPC evt | 待做 |
| API 调用（send_msg 等） | `api.call` 处理 + echo 回协 | 桩（已验证链路） |
| 连接管理/心跳 | 同上 | 待做 |