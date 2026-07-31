"""
ZCBOT 示例插件 - 演示完整插件开发流程

涵盖功能：
- 静态命令注册（含别名、权限要求）
- 动态命令注册
- 定时任务注册
- 事件订阅
- 配置读取
- 数据库操作
- 仪表盘卡片
- 日志与审计
- WebUI 注册

详细开发文档见 docs/PLUGIN_DEV.md
"""

__plugin_meta__ = {
    "name": "示例插件",
    "version": "1.0.0",
    "author": "zcbot",
    "desc": "演示 ZCBOT 插件开发完整流程，包含命令、任务、事件、配置等",
    "priority": 50,
}


def register(ctx):
    """插件注册入口（必需）"""

    # ── 1. 静态命令 ──
    ctx.command("/hello", handle_hello, alias="/hi", description="打招呼")
    ctx.command("/time", handle_time, description="获取当前时间")
    ctx.command("/admin", handle_admin, require_admin=True, description="管理员测试命令")
    ctx.command("/super", handle_super, require_superuser=True, description="超管测试命令")
    # ── 2. 动态命令（仅在 Web UI 展示，不参与路由匹配）──
    ctx.command("/dynamic_demo", handle_dynamic, dynamic=True, description="动态命令示例")

    # ── 3. 定时任务 ──
    ctx.task("0 8 * * *", daily_morning, description="每日 8:00 早安问候")

    # ── 4. 事件订阅 ──
    ctx.on("group_member_increase", on_new_member)

    # ── 5. 仪表盘卡片 ──
    ctx.dashboard_card("示例统计", get_stats, icon="chart", priority=20)

    # ── 6. 插件 WebUI ──
    ctx.webui(title="示例插件面板", entry="index.html", icon="settings", order=50)

    ctx.logger.info("示例插件已加载")


# ============================================================
# 命令处理函数
# ============================================================

def handle_hello(event, match):
    """打招呼

    用法：/hello 或 /hi
    """
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="你好！我是 ZCBOT 示例插件。",
    )


def handle_time(event, match):
    """获取当前服务器时间"""
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"当前服务器时间：{now}",
    )


def handle_admin(event, match):
    """管理员测试命令（仅群主/管理员/超管可用）"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=f"你好，管理员！你的身份是：{event.role}",
    )


def handle_super(event, match):
    """超管测试命令（仅框架超管可用）"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message="你好，超级管理员！",
    )


def handle_dynamic(event, match):
    """动态命令示例（实际不会被路由匹配到，仅作展示）"""
    pass


# ============================================================
# 定时任务处理函数
# ============================================================

def daily_morning():
    """每日 8:00 早安问候（不接收 event 参数）"""
    ctx.logger.info("执行每日早安问候任务")
    # 实际场景：可以查询数据库获取目标群列表，然后发送问候
    # groups = ctx.db_query("SELECT group_id FROM groups_info WHERE is_active = 1")
    # for g in groups:
    #     ctx.send_msg(group_id=g['group_id'], message="早安！新的一天加油！")


# ============================================================
# 事件处理函数
# ============================================================

def on_new_member(payload):
    """新成员入群事件处理"""
    group_id = payload.get("group_id")
    user_id = payload.get("user_id")
    if group_id and user_id:
        ctx.send_msg(
            group_id=group_id,
            message=f"欢迎新成员 {user_id} 加入本群！",
        )
        ctx.logger.info(f"新成员 {user_id} 加入群 {group_id}")


# ============================================================
# 仪表盘卡片
# ============================================================

def get_stats():
    """返回仪表盘卡片数据"""
    # 从配置读取展示数据
    greet_count = ctx.get_config("greet_count", default=0)
    return {
        "title": "示例统计",
        "value": greet_count,
        "label": "累计问候次数",
        "icon": "chart",
        "color": "#007aff",
    }


# ============================================================
# 生命周期钩子（可选）
# ============================================================

def on_load(ctx):
    """插件加载时调用（register 之前）"""
    ctx.logger.info("示例插件正在加载...")


def on_unload(ctx):
    """插件卸载时调用"""
    ctx.logger.info("示例插件正在卸载，清理资源...")
