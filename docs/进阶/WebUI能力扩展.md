# WebUI 能力扩展

本文覆盖管理面板新增的几项能力：管理员账号完整管理、AI 智能体对话、Web 聊天、
富媒体登记、卡片任意排列、遥测与安全入口。

## 管理员账号完整管理

`/api/admins` 在原有的添加 / 列表 / 删除之外增加：

- `PUT /api/admins/<id>`：改名、改角色、启用或禁用；
- `POST /api/admins/<id>/password`：超管重置指定账号密码，同时清空其令牌哈希。

两条约束：不能降级或禁用当前登录账号；最后一个启用的超级管理员不能被降级或禁用。
设置页「管理员账号」卡片可直接编辑、切换状态与重置密码。

## AI 智能体对话

页面依赖 `aiwriter` 插件：插件未启用时侧边栏入口不显示，直接访问该路由会被重定向。
启用后可在 `/aiwriter` 中新建 / 切换 / 删除会话，发消息并查看执行过程与本轮改动，
支持整轮回滚。后端 `POST /api/aiwriter/chat` 调用的就是终端 `code` 使用的同一个
`svc.run`，会话、工具与改动账本口径一致。

智能体新增两个工具：

- `blackbox`：查看运行中框架的内存黑匣子（stats / recent / trace / stuck）；
- `simulate_message`：模拟一条消息进入真实处理链，用于复现问题（full 模式）。

## Web 聊天

`/webchat` 提供面板内收发消息：收到的 QQ 消息经 `EVENT_BEFORE_DISPATCH` 镜像到
`webchat_messages` 表；从面板发送的消息走真实的 `dispatch.dispatch_event(wait=True)`，
插件收到的事件与 QQ 消息同构。页面按 3 秒轮询刷新。

## 富媒体消息登记

语音、视频、文件、名片、位置等非文本 / 图片段会由 `EVENT_BEFORE_DISPATCH` 登记到
`media_messages` 表，在 `/media` 页可查看。媒体内容下载仍走 `media_io` 的统一安全
闸门，本模块不另开文件读取口子。

## 仪表盘卡片排列

仪表盘的插件卡片支持卡片式 / 列表式 / 紧凑式三种布局，并可用拖拽调整任意位置。
顺序与布局保存在 `web.dashboard` 配置段，重启后保持。插件未提供卡片时页面为空态。

## 遥测

默认关闭。配置：

```yaml
telemetry:
  enabled: false
  endpoint: ""
  interval_days: 15
```

只有 `enabled: true` 且 `endpoint` 非空时才会创建后台任务并上报。上报内容包含
系统信息、框架版本、错误日志与框架生成的随机唯一标识（UUID4），不含管理员账号、
令牌与 IP。关闭时每次登录面板会收到一条提醒，不阻断登录。

## 安全入口

`security.honeypot.entry_paths` 可列出自定义安全入口路径，默认为空。列出的路径
按蜜罐陷阱注册：命中即审计、拉黑并按配置延迟响应；已有真实路由不会被覆盖。
