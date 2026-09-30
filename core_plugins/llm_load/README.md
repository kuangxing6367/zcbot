# llm_load —— LLM 核心装载器

保证 `plugins/llm_core` 存在且和官方载荷**逐字节一致**，不一致就释放并立刻重启框架。

## 为什么拆成装载器 + 运行时主体

LLM 这套东西变化快：新模型、新 MCP server、新工具，不可能每次都等框架发版。
把它放在 `core_plugins/` 等于锁死；放在 `plugins/` 又没人保证它会不会被改坏或删掉。
所以：

| 位置 | 角色 | 要不要改 |
| ---- | ---- | ---- |
| `core_plugins/llm_load/` | 校验 / 释放 / 重启（很薄） | 一般不用 |
| `core_plugins/llm_load/src/llm_core/` | 运行时源码（真源） | **改这里** |
| `core_plugins/llm_load/llm_core.zip` | 打包产物（含 md5 manifest） | 由脚本生成 |
| `plugins/llm_core/` | 运行时主体 | 别手改，会被覆盖 |

## 启动流程

```
读 llm_core.zip 里的 manifest.json
        │
        ├── 逐文件比对 plugins/llm_core 的 md5
        │        一致 → 放行，继续启动
        │        不一致 / 缺失 → 解压覆盖 → 再校验 → 一致即刻 os.execv 重启
        │                                        不一致 → 只报错，不重启
```

写得对整个链路是幂等的：**释放完不通过校验就不重启**，所以不存在重启死循环。

## 改源码的正确姿势

```bash
# 1. 改 core_plugins/llm_load/src/llm_core/ 下的文件
# 2. 重新打包（释放按逐文件 md5 比对触发，内容变了即生效，不必手动加版本号）
python tools/build_llm_payload.py --write
# 3. 下次启动自动释放 + 重启；也可以先看一眼差异
python tools/build_llm_payload.py --check
```

## 启用 / 命令

```bash
python tools/scan_core_plugins.py --enable llm_load
```

| 命令 | 作用 |
| ---- | ---- |
| `/llmload status` | 版本 / 文件数 / 差异概览 |
| `/llmload verify` | 立即校验并列出不一致的文件 |
| `/llmload reinstall` | 强制重新释放（会抹掉目标目录里被改过的文件） |

## 配置（core_plugins.yaml 的 llm_load 段）

| 键 | 默认 | 说明 |
| ---- | ---- | ---- |
| `enabled` | false | 官方插件「发现即禁用」，需显式启用 |
| `plugin_name` | `llm_core` | 释放到 plugins/ 下的目录名 |
| `auto_restart` | true | 释放完成后是否立刻重启 |
| `payload` | 插件目录下 `llm_core.zip` | 载荷路径，一般不用动 |

对齐策略说明：manifest 收录的文件一旦被改动就会被覆盖回去（哪怕只是手滑删了个空格）。
manifest 没收录的文件不会被动——所以你自己在目录里放的数据文件是安全的。
