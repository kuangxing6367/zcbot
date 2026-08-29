"""
WebUI 群组/用户管理页扩展演示插件 (ui_ext_demo)
=================================================
演示框架的群组/用户管理页插件扩展接口：

    ctx.register_group_extension(key, title, handler, ext_type)
    ctx.register_user_extension(key, title, handler, ext_type)

- ext_type='column'：在管理页表格中增加一列（handler 返回字符串）
- ext_type='panel' ：在管理页行内「详情」弹窗中增加面板（handler 返回 {label: value}）

效果：Web UI → 用户管理 / 群组管理，即可看到本插件添加的列与详情面板。
实际插件可在此查询自己的业务表（如签到记录、积分表）并返回统计。
"""
import time

__plugin_meta__ = {
    "name": "UI 扩展演示",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "演示群组/用户管理页插件扩展（列 + 详情面板）",
    "priority": 200,
}


def register(ctx):
    # ---- 群组管理页扩展 ----
    # column：表格加一列（handler(group_id) -> 字符串）
    ctx.register_group_extension(
        "demo_group_level", "群等级",
        lambda gid: f"Lv.{int(gid) % 9 + 1}", "column")

    # panel：详情弹窗面板（handler(group_id) -> {label: value}）
    ctx.register_group_extension(
        "demo_group_panel", "演示面板",
        lambda gid: {
            "群号": gid,
            "模拟成员数": (gid * 7) % 200,
            "模拟活跃度": ["低", "中", "高"][(gid * 3) % 3],
            "数据来源": "ui_ext_demo 插件（演示）",
        }, "panel")

    # ---- 用户管理页扩展 ----
    ctx.register_user_extension(
        "demo_user_level", "用户等级",
        lambda uid: f"Lv.{int(uid) % 10 + 1}", "column")

    ctx.register_user_extension(
        "demo_user_panel", "用户演示面板",
        lambda uid: {
            "QQ": uid,
            "模拟积分": (uid * 13) % 9999,
            "注册时长": f"{(int(uid) % 300) + 1} 天",
        }, "panel")

    ctx.log("[ui_ext_demo] 群组/用户管理页扩展已注册（演示用，可删除）")
