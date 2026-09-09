# 安装

## 环境要求

- Python 3.10 或更高版本
- 操作系统：Windows / Linux / macOS

## 下载代码

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
```

## 安装依赖

```bash
pip install -r requirements.txt
```

:::tip 提示
依赖会在首次启动时自动检查并补装，无需手动处理。
:::

## 目录结构

```
zcbot/
├── main.py                 # 启动入口
├── config.yaml             # 配置文件（首次启动自动生成）
├── framework/              # 框架核心
├── core_plugins/           # 官方插件（可选）
│   ├── onebot_adapter/     # OneBot 11 适配器
│   ├── webui/              # Web 管理后台
│   ├── session/            # 多轮会话管理器
│   └── scheduler/          # 定时任务调度器
├── plugins/                # 用户插件
├── data/                   # 运行数据（自动创建）
└── web/                    # Web 前端静态资源
```
