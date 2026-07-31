# 示例插件

演示 ZCBOT 框架插件开发的完整流程，包含命令注册、定时任务、事件订阅、配置读取、数据库操作、仪表盘卡片、WebUI 等所有核心功能。

## 功能演示

### 命令

| 命令       | 别名 | 权限要求     | 说明                |
| ---------- | ---- | ------------ | ------------------- |
| `/hello`   | `/hi`| 所有用户     | 打招呼              |
| `/time`    | -    | 所有用户     | 获取当前服务器时间  |
| `/echo`    | -    | 所有用户     | 原样返回输入文本    |
| `/admin`   | -    | 群管理员以上 | 管理员测试命令      |
| `/super`   | -    | 框架超管     | 超管测试命令        |

### 定时任务

- 每日 8:00 执行早安问候（`0 8 * * *`）

### 事件订阅

- `group_member_increase`：新成员入群时发送欢迎消息

### 配置项

| 配置键                    | 类型    | 默认值                                      | 说明             |
| ------------------------- | ------- | ------------------------------------------- | ---------------- |
| `greet_count`             | number  | 0                                           | 累计问候次数     |
| `welcome_message`         | string  | 欢迎新成员 {user_id} 加入本群！             | 欢迎语模板       |
| `enable_morning_greeting` | boolean | true                                        | 启用每日早安问候 |

## 目录结构

```
plugins/example_plugin/        # 代码目录
├── main.py                    # 插件入口
├── requirements.txt           # 依赖声明
└── README.md                  # 本文件

plugins_dat/example_plugin/    # 数据/配置目录
├── plugin.yaml                # 插件元信息、GitHub 源、配置项
└── _conf_schema.json          # 配置 schema（Web UI 渲染表单用）
```

## 开发文档

完整的插件开发文档见 [docs/PLUGIN_DEV.md](../../docs/PLUGIN_DEV.md)。
