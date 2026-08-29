# 调试指南

> 插件出了 bug 怎么定位？本文按"从轻到重"的顺序教你：先看日志 → 加日志 → 断点调试 → 热重载试错。每一步都能在真实环境里落地。

## 1. 先看日志（90% 的问题在这一步）

所有日志统一写在 `data/logs/zcbot.log`，控制台也会实时打印。

**怎么看：**

```bash
# 看最后 50 行
tail -50 data/logs/zcbot.log
# Windows PowerShell
Get-Content data/logs/zcbot.log -Tail 50
```

也可以打开网页后台 →「日志」页面实时滚动查看。

**日志里搜什么：**

| 你想知道 | 搜什么 |
| ---- | ---- |
| 插件有没有加载成功 | 日志里插件名 + `已加载` / `ERROR` |
| 命令有没有被路由命中 | `消息由 [插件名] 处理` 或 `未匹配任何命令` |
| 插件抛了什么异常 | `ERROR` + 插件名，后面往往跟 traceback |
| 有没有发消息 | 配置 `log.log_sent_message: true` 后搜 `发送` |

## 2. 打开 DEBUG 日志（看到更多细节）

默认日志级别是 `INFO`，很多调试信息被过滤了。改成 `DEBUG`：

```yaml
# config.yaml
log:
  level: DEBUG
```

**改完必须重启框架生效**（`log.level` 是启动时读取的，热更新暂不支持，见 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) A6）。

DEBUG 级别下你会看到：消息接收/分发、命令匹配、任务执行、DB 操作等大量细节。排查完记得改回 `INFO`，否则日志增长很快。

## 3. 在插件里加日志（最推荐的调试方式）

别急着打断点，先在怀疑的位置打日志，看实际走到哪一步、变量是什么值：

```python
def handle_xxx(event, match):
    ctx.logger.debug(f"进入 handler，group={event.group_id}, 参数={match.group(1)}")
    try:
        r = ctx.db_query_one("SELECT * FROM t WHERE id=%s", (1,))
        ctx.logger.debug(f"查询结果: {r}")
    except Exception as e:
        ctx.logger.exception(f"查询失败: {e}")   # 带完整 traceback
```

注意 `ctx.logger` 会**自动加插件名前缀**，在日志里一眼就能看到是哪家的日志。`ctx.logger.debug` 只有 DEBUG 级别才显示（见第 2 步）。

## 4. 用 pdb 打断点（重度调试才用）

> ⚠️ 警告：ZCBOT 是异步框架，**在 async handler 里打断点会卡死整个事件循环**（pdb 等输入时，其他消息全部停住）。只建议在**同步 handler**（普通 `def`）里用，且断点期间机器人不响应其他消息是正常现象。

**同步 handler 里打断点：**

```python
def handle_debug(event, match):
    import pdb; pdb.set_trace()     # 停在这里，进入交互
    ...
```

运行 `python main.py`，触发该命令后控制台会进入 `(Pdb)` 提示符，可用命令：

| 命令 | 作用 |
| ---- | ---- |
| `p 变量名` | 打印变量值 |
| `l` | 显示当前位置附近的源码 |
| `n` | 执行下一行 |
| `s` | 进入函数内部 |
| `c` | 继续执行到下一个断点 |
| `q` | 退出 pdb（会抛异常，别用在生产） |

**async handler 里的替代方案：** 别打断点，改用第 3 节的日志，或者把要调试的代码临时抽成同步函数再打断点。

## 5. 热重载（改代码不用重启框架）

两个途径，**注意它们的区别**：

### 5.1 网页后台「插件」→ 重载按钮（推荐）

在后台点插件的「重载」，框架会完整卸载再重新加载：**handler 代码改动会生效**（重新 import）。

改完代码 → 点重载 → 立刻验证，是调试循环最快的方式。

### 5.2 心跳自动增量注册（只重注册，不改代码）

框架每 60 秒检查插件文件变化，自动重新执行 `register(ctx)`。但这是**增量注册**，只重新注册命令/任务/订阅，**不会重新 import**——如果你只改了 handler 的**函数体**，心跳不会让它生效（见 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) A2）。

> 结论：**改了函数体代码，必须用 5.1 的重载按钮或重启框架**；只改命令注册（`ctx.command` 行）心跳会自动跟上。

## 6. 常见调试场景对照

| 症状 | 排查路径 |
| ---- | ---- |
| 命令没反应 | ① 日志看是否 `未匹配任何命令`；② 确认命令带 `/` 前缀；③ 确认插件已加载 |
| 插件加载失败 | 日志里找 `ERROR` + 插件名，看 traceback；多半是语法错误或缺依赖 |
| 只有部分人能用 | 检查 `require_admin` / `require_superuser` 参数 |
| 数据库报错 | 看日志 SQL 片段；确认表已建（`ctx.create_table`）；`%s` 占位符 |
| 定时任务没跑 | 日志搜任务名；确认 cron 表达式；任务函数**不能带 event 参数** |
| 改了代码不生效 | 见第 5 节：函数体改动需手动重载 |
| 消息乱序/丢消息 | 可能命中 A1（并发乱序），见 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) |

## 7. 调试完记得

- 把 `log.level` 改回 `INFO`
- 删掉 `pdb.set_trace()`
- 删掉临时 `ctx.logger.debug`（或改成正式日志）
- 顺手更新 [KNOWN_ISSUES.md](./KNOWN_ISSUES.md) 里的相关状态（如果发现新坑）