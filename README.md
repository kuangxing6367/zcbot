# ZCBOT 🤖

> **一个开箱即用的 QQ 机器人框架**——装上就能跑，会写 Python 就能写插件。
> 基于 OneBot v11 协议，全异步，自带 Web 管理面板。

**当前版本：v0.0.1-beta.1**

📚 项目地址：https://github.com/kuangxing6367/zcbot
💬 反馈交流：QQ 群 **1060129201**

---

## ✨ 它是什么？

ZCBOT 让你**用 Python 快速做一个 QQ 机器人**：

- 群里发 `/echo 你好`，机器人回复"你好"
- 接上大模型（LLM），@机器人 就能聊天
- 写个插件，就能自定义任何功能（签到、查询、群管……）

它已经把**最难的部分**都做好了：

| 能力 | 说明 |
| ---- | ---- |
| 🚀 全异步 | 消息处理不卡顿，响应快 |
| 🔌 插件化 | 一个插件 = 一个文件夹，写好即加载，支持热加载/热卸载 |
| 🖥️ Web 管理面板 | 可视化管插件/命令/用户/群组/任务/日志 |
| 🗄️ 数据库 | 默认 SQLite 零配置，可切换 MySQL（自动翻译方言 SQL） |
| 🔐 安全 | 权限体系 + 登录防爆破 + 后台防破解 |
| 🧩 开箱即用 | 自带 echo/help/状态/图片渲染 等常用插件 |

---

## 🚀 快速开始（5 步）

> 全程约 5 分钟，跟着做就行。

### 第 1 步：准备 Python

需要 **Python 3.10+**。装好后在终端确认：

```bash
python --version   # 看到 Python 3.10.x 或更高即可
```

### 第 2 步：获取代码

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
pip install -r requirements.txt
```

> 💡 依赖缺了不用慌，框架启动时会**自动补装**。

### 第 3 步：配置（可选）

首次启动会自动生成 config.yaml。一般不用改，直接跑。想改的话：

```yaml
database:
  type: sqlite          # 默认 SQLite，零配置；想用 MySQL 改成 mysql + 填连接信息
onebot:
  listen_port: 6830     # OneBot 客户端连这里
  access_token: "自己设一个token"   # ⚠️ 务必设置，否则任何人都能接入
web:
  host: 127.0.0.1       # Web 面板地址（本机访问）
  port: 8080
```

### 第 4 步：启动

```bash
python main.py
```

看到 `框架启动完成，等待消息...` 就成功了 🎉

### 第 5 步：连接机器人 + 访问面板

1. **连接 OneBot 客户端**：用 NapCat / Lagrange / go-cqhttp 等，配**反向 WebSocket** 连到 `ws://你的地址:6830`，token 填上面设的。
2. **Web 面板**：浏览器打开 `http://localhost:8080`，默认账号 `admin`，密码 `admin123`（⚠️ 上线前一定改掉！）

---

## 🎯 写你的第一个插件（手把手）

> 目标：让机器人回复 `/你好` → "你也好呀！"

### ① 建文件夹

在 `plugins/` 下建一个文件夹，名字就是插件名（用英文）：

```
plugins/
└── hello/          ← 新建这个文件夹
    └── main.py     ← 插件入口文件
```

### ② 写代码

在 `plugins/hello/main.py` 里粘贴：

```python
# 插件元信息：告诉框架这个插件是干嘛的
__plugin_meta__ = {
    "name": "你好插件",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "回复 /你好",
    "priority": 50,
}

def register(ctx):
    """注册入口：告诉框架有哪些命令"""
    ctx.command("/你好", handle_hi, description="打个招呼")

def handle_hi(event, match):
    """命令处理函数：收到 /你好 时执行"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="你也好呀！👋",
    )
```

### ③ 生效

在 Web 面板「插件」页点「重载」，或重启框架。然后群里发 **`/你好`** → 机器人回 **`你也好呀！👋`** ✅

### ④ 小练习（理解机制）

试试把命令改成带参数：

```python
def handle_hi(event, match):
    arg = match.group(1).strip() if match else ""   # 命令后的内容
    msg = f"你也好呀 {arg}！" if arg else "你也好呀！"
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=msg,
    )
```

发 `/你好 小明` → 回复 `你也好呀 小明！` 🎉

---

## 📖 插件还能干什么？

| 能力 | 代码示例 |
| ---- | ---- |
| 💬 发消息 | `ctx.send_msg(user_id=..., group_id=..., message="...")` |
| 📷 发图片 | `ctx.send_msg(..., message="[CQ:image,file=file:///路径]")` |
| ⏰ 定时任务 | `ctx.task("*/5 * * * *", my_job, description="每5分钟")` |
| 🔔 订阅事件 | `ctx.on("message", on_msg)` |
| 🗄️ 查数据库 | `ctx.db_query("SELECT * FROM users")` |
| ⚙️ 读配置 | `ctx.get_config("key", default)` |
| 🛠️ 调 API | `ctx.api("get_group_list")` 或 `ctx.onebot.send_group_msg(...)` |
| 🎨 生成图片 | 接 `image_renderer` 画卡片/文字图 |

> 📚 完整 API 看 `docs/api-reference.md`；更多示例看 `docs/examples.md`（含签到插件完整实现）。

---

## 📦 内置插件

| 插件 | 说明 |
| ---- | ---- |
| **echo** | `/echo 内容` 原样返回 |
| **help** | `/help` 生成图片帮助菜单 |
| **image_renderer** | 通用图片渲染引擎（Rust 原生优先，缺失回退 PIL） |
| **restart_manager** | 框架重启管理 |
| **runtime_status** | `/status` `/info` 运行状态（含图片版状态卡） |

## 🧩 更多插件（插件仓库）

需要 LLM 对话、群管、签到等？克隆插件仓库，把 `config.yaml` 的 `plugin.dir` 指向它：

```yaml
plugin:
  dir: ../zgric_onebot11_plugins/plugins
```

仓库含：`llm_chat`（AI 对话，支持人格）、`qqadmin`（群管理）、`fun_score`（签到积分）、`minecraftconsole`（MC 控制台）等。

---

## ❓ 常见问题

**Q: 面板打不开？**
A: 确认 `web.host` 是 `127.0.0.1`（本机）或 `0.0.0.0`（局域网）。重启后仍打不开，看日志"端口被占用"（`ss -tlnp | grep 8080` 查残留进程）。

**Q: 机器人不回复？**
A: ① OneBot 客户端是否连上（面板/日志看"客户端已连接"）② `access_token` 是否匹配 ③ 消息是否是命令开头。

**Q: 改插件代码没生效？**
A: Web 面板插件页点「重载」，或重启框架。

**Q: 忘了管理员密码？**
A: 看 `data/logs/` 日志提示，或删 `data/zcbot.db` 重新初始化（会重置数据，慎用）。

**Q: 内存涨了怎么办？**
A: 框架会自动定期释放空闲内存；持续上涨可发 `/memdiag` 诊断。

---

## 📚 进阶文档

- [文档索引](docs/INDEX.md)
- [快速入门](docs/getting-started.md) — 手把手第一个插件
- [插件目录结构](docs/plugin-structure.md) — 代码/数据分离约定
- [API 参考](docs/api-reference.md) — ctx 全部方法
- [配置系统](docs/configuration.md) — 插件配置项
- [最佳实践](docs/best-practices.md)
- [示例合集](docs/examples.md) — 签到插件完整实现

---

## 📜 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目代码由 AI 完成为主、人工辅助完成。用着顺手的话，给个 ⭐ 吧！