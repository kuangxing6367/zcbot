# 示例

## 完整示例：每日签到插件

以下是一个功能完整的示例插件，展示命令注册、配置读取、定时任务、事件订阅、数据库操作的组合用法：

```python
# plugins/sign_in/main.py
"""
每日签到插件 - 演示 ZCBOT 插件开发完整流程
"""
import datetime
import random

__plugin_meta__ = {
    "name": "每日签到",
    "version": "1.0.0",
    "author": "zcbot",
    "desc": "每日签到领积分，连续签到有奖励",
    "priority": 50,
}


def register(ctx):
    # 静态命令
    ctx.command("/签到", handle_sign_in, alias="/sign", description="每日签到")
    ctx.command("/签到排行", handle_rank, alias="/rank", description="签到排行榜")
    ctx.command("/我的积分", handle_my_score, description="查看我的积分")

    # 定时任务：每天 00:01 重置签到状态
    ctx.task("1 0 * * *", reset_daily_signin, description="每日重置签到")

    # 订阅事件：新成员入群时赠送初始积分
    ctx.on("group_member_increase", on_new_member)


def handle_sign_in(event, match):
    """每日签到，随机获得 1-10 积分"""
    today = datetime.date.today().isoformat()

    # 检查今日是否已签到
    signed = ctx.db_query_one(
        "SELECT id FROM sign_in_records WHERE user_id = %s AND sign_date = %s",
        (event.user_id, today)
    )
    if signed:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message="你今天已经签到过了",
        )
        return

    # 计算积分（连续签到加成）
    last_sign = ctx.db_query_one(
        "SELECT sign_date, continuous_days FROM sign_in_records "
        "WHERE user_id = %s ORDER BY sign_date DESC LIMIT 1",
        (event.user_id,)
    )
    if last_sign:
        last_date = datetime.date.fromisoformat(last_sign['sign_date'])
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        continuous = last_sign['continuous_days'] + 1 if last_date == yesterday else 1
    else:
        continuous = 1

    base_score = random.randint(1, 10)
    bonus = min(continuous - 1, 5)  # 连签加成，最多 +5
    total = base_score + bonus

    # 写入数据库
    ctx.db_execute(
        "INSERT INTO sign_in_records (user_id, sign_date, score, continuous_days) "
        "VALUES (%s, %s, %s, %s)",
        (event.user_id, today, total, continuous)
    )

    # 更新用户总积分
    ctx.db_execute(
        "INSERT INTO user_scores (user_id, total_score) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE total_score = total_score + %s",
        (event.user_id, total, total)
    )

    # 审计日志
    ctx.audit_log(
        action="sign_in",
        target_type="user",
        target_name=str(event.user_id),
        detail={"score": total, "continuous": continuous},
    )

    # 发布事件
    ctx.emit("user_sign_in", {"user_id": event.user_id, "score": total})

    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"签到成功！获得 {total} 积分（基础 {base_score} + 连签加成 {bonus}）\n"
                f"已连续签到 {continuous} 天",
    )


def handle_rank(event, match):
    """签到排行榜"""
    rows = ctx.db_query(
        "SELECT user_id, total_score FROM user_scores "
        "ORDER BY total_score DESC LIMIT 10"
    )
    if not rows:
        ctx.send_msg(group_id=event.group_id, message="暂无排行数据")
        return

    lines = ["积分排行榜 TOP 10:"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. QQ {row['user_id']} - {row['total_score']} 分")
    ctx.send_msg(group_id=event.group_id, message="\n".join(lines))


def handle_my_score(event, match):
    """查看我的积分"""
    row = ctx.db_query_one(
        "SELECT total_score FROM user_scores WHERE user_id = %s",
        (event.user_id,)
    )
    score = row['total_score'] if row else 0
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"你当前积分：{score}",
    )


def reset_daily_signin():
    """每日 00:01 执行（无 event 参数）"""
    ctx.logger.info("开始执行每日签到重置")
    # 可在此清理过期数据、生成报表等


def on_new_member(payload):
    """新成员入群事件"""
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    if group_id and user_id:
        ctx.db_execute(
            "INSERT INTO user_scores (user_id, total_score) VALUES (%s, 100) "
            "ON DUPLICATE KEY UPDATE total_score = total_score + 100",
            (user_id,)
        )
        ctx.send_msg(group_id=group_id, message=f"欢迎新成员 {user_id}，赠送 100 初始积分")


def on_unload(ctx):
    """插件卸载时清理资源"""
    ctx.logger.info("签到插件已卸载")
```

### 配套配置文件

`plugins_dat/sign_in/_conf_schema.json`：

```json
{
  "base_score_max": {
    "description": "单次签到最高基础积分",
    "type": "number",
    "default": 10,
    "hint": "签到随机积分范围 1 到此值"
  },
  "continuous_bonus_max": {
    "description": "连签加成上限",
    "type": "number",
    "default": 5,
    "hint": "连续签到每日额外加成上限"
  }
}
```

`plugins_dat/sign_in/plugin.yaml`：

```yaml
name: sign_in
version: 1.0.0
author: zcbot
description: 每日签到领积分
priority: 50

github:
  repo: your-name/sign_in_plugin
  branch: main
  path: /
  auto_check: true

config:
  - key: base_score_max
    label: 最高基础积分
    type: number
    default: 10
  - key: continuous_bonus_max
    label: 连签加成上限
    type: number
    default: 5

dependencies:
  python: []

docs:
  - file: README.md
    title: 使用说明
```

## 内置插件参考

| 插件 | 源码 | 说明 |
| ---- | ---- | ---- |
| Echo | [plugins/echo/main.py](../plugins/echo/main.py) | 最简单的命令回显 |
| Help | [plugins/help/main.py](../plugins/help/main.py) | 帮助菜单图片生成 + plugin.yaml 配置 |
| Runtime Status | [plugins/runtime_status/main.py](../plugins/runtime_status/main.py) | 系统监控 + 仪表盘卡片 |
| Image Renderer | [plugins/image_renderer/main.py](../plugins/image_renderer/main.py) | Rust 原生扩展 + PIL 回退 |
