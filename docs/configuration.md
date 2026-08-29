# 配置系统

## plugin.yaml 配置文件

放在 `plugins_dat/<plugin_name>/plugin.yaml`，提供 Web UI 识别所需的元信息、GitHub 更新源、依赖声明等：

```yaml
# 插件元信息（覆盖 __plugin_meta__）
name: my_plugin
version: 1.0.0
author: your-name
description: 我的插件
priority: 50

# GitHub 更新源
github:
  repo: your-name/my_plugin   # 支持 user/repo 简写或完整 URL
  branch: main                # 分支名
  path: /                     # 仓库内插件所在子目录
  auto_check: true            # 是否启用自动更新检查

# 插件配置项（Web UI 可展示和修改）
config:
  - key: api_key
    label: API Key
    type: string
    default: ""
    description: 第三方 API 密钥
  - key: timeout
    label: 请求超时
    type: number
    default: 10
    description: HTTP 请求超时秒数

# 依赖
dependencies:
  python:
    - requests>=2.28.0
    - beautifulsoup4

# 插件创建的业务表（删除插件时可选清理）
# 仅在 Web UI 删除插件并勾选"删除数据"时，框架才会 DROP 这些表
# 热卸载/重载/禁用不会触发清理，数据始终保留
managed_tables:
  - sign_in_records
  - user_scores

# 配置文档（Web UI 可查看）
docs:
  - file: README.md
    title: 使用说明
```

## _conf_schema.json 配置 Schema

配置 schema，用于 Web UI 动态渲染配置表单。放在 `plugins_dat/<plugin_name>/_conf_schema.json`：

```json
{
  "api_key": {
    "description": "第三方 API 密钥",
    "type": "string",
    "default": "",
    "hint": "在 https://example.com 申请"
  },
  "timeout": {
    "description": "请求超时（秒）",
    "type": "number",
    "default": 10,
    "hint": "建议 5-30 秒"
  },
  "enable_feature": {
    "description": "启用实验功能",
    "type": "boolean",
    "default": false,
    "hint": "开启后可使用 /experimental 命令"
  }
}
```

**支持的 type**：`string`、`number`、`boolean`。

插件代码中通过 `ctx.get_config("api_key", default="")` 读取。

## 插件依赖声明

### 方式一：requirements.txt

在 `plugins/<plugin_name>/requirements.txt` 中声明：

```
requests>=2.28.0
beautifulsoup4>=4.11.0
lxml>=4.9.0
```

### 方式二：plugin.yaml

在 `plugins_dat/<plugin_name>/plugin.yaml` 中：

```yaml
dependencies:
  python:
    - requests>=2.28.0
    - beautifulsoup4
```

**安装时机**：
- 启动时自愈（`plugin.auto_install_deps_on_startup: true`）：自动安装缺失依赖
- Web UI 手动安装：在插件管理页点击「安装依赖」按钮

**镜像源**：默认使用清华源，失败自动回退到阿里云、华为云、官方源。可在 Web UI 设置中切换。

**依赖冲突**：当多个插件依赖同一包的不同版本时，框架可为冲突插件创建独立虚拟环境（`.venv`），在 Web UI 插件管理页点击「创建隔离环境」即可。
