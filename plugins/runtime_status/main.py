"""
运行状态插件 - 框架运行状态监控与查询
提供 /status /info /help 等命令，支持 Web UI 配置项动态修改
配置项由 _conf_schema.json 定义，通过 ctx.get_config() 读取
"""
import os
import sys
import psutil
import platform
import time
import socket

__plugin_meta__ = {
    "name": "运行状态",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "框架运行状态监控，提供 /status /info /help 命令，支持 Web UI 配置",
    "priority": 5,
}

# 框架启动时间（插件加载时记录）
_start_time = time.time()
# 缓存的系统信息（避免频繁读取）
_cache = {}
_cache_time = 0


def register(ctx):
    """插件注册入口"""
    ctx.command("/status", handle_status, priority=5, description="查看框架运行状态概览")
    ctx.command("/info", handle_info, priority=5, description="查看系统详细信息（CPU/内存/磁盘）")
    ctx.command("/uptime", handle_uptime, priority=5, description="查看框架运行时间")
    ctx.command("/plugins", handle_plugins, priority=10, description="查看已加载插件列表")

    # 注册定时任务：每 5 分钟自动清理缓存
    ctx.task("*/5 * * * *", task_cleanup, description="清理状态缓存")

    # 注册仪表盘卡片
    ctx.dashboard_card("运行时间", _dashboard_uptime, icon="&#9200;", priority=10)
    ctx.dashboard_card("系统负载", _dashboard_system_load, icon="", priority=20)


def _get_config(ctx, key, default=None):
    """读取配置（不缓存，Web UI 修改后即时生效）"""
    return ctx.get_config(key, default)


def _format_uptime(seconds):
    """格式化运行时间"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if mins > 0:
        parts.append(f"{mins}分钟")
    parts.append(f"{secs}秒")
    return "".join(parts)


def _get_system_info(ctx):
    """收集系统信息（根据配置项控制显示内容）"""
    global _cache, _cache_time
    interval = _get_config(ctx, 'status_interval', 30)

    now = time.time()
    if _cache and (now - _cache_time) < interval:
        return _cache

    try:
        vm = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.5)
        disk = psutil.disk_usage('/')

        info = {
            'cpu_percent': cpu_percent,
            'cpu_count': psutil.cpu_count(logical=True),
            'mem_total': vm.total / 1024 / 1024 / 1024,
            'mem_used': vm.used / 1024 / 1024 / 1024,
            'mem_percent': vm.percent,
            'disk_total': disk.total / 1024 / 1024 / 1024,
            'disk_used': disk.used / 1024 / 1024 / 1024,
            'disk_percent': disk.percent,
            'platform': platform.platform(),
            'python': platform.python_version(),
            'hostname': socket.gethostname(),
            'boot_time': _format_uptime(now - psutil.boot_time()),
        }
        _cache = info
        _cache_time = now
        return info
    except Exception as e:
        return {'error': str(e)}


def _get_framework_info(ctx):
    """收集框架信息"""
    try:
        framework = ctx._framework
        bots = framework.ws_server.get_connected_bots()
        loaded_plugins = framework.plugin_loader.get_loaded_plugins()

        db = ctx._db
        cmd_count = db.query_one("SELECT COUNT(*) as cnt FROM commands")['cnt']
        dyn_count = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active=1")['cnt']
        user_count = db.query_one("SELECT COUNT(*) as cnt FROM users")['cnt']
        group_count = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active=1")['cnt']

        return {
            'uptime': _format_uptime(time.time() - _start_time),
            'ws_port': framework.config.get('onebot', {}).get('listen_port', 6830),
            'web_port': framework.config.get('web', {}).get('port', 8080),
            'bot_count': len(bots),
            'bots': bots,
            'plugin_count': len(loaded_plugins),
            'plugins': list(loaded_plugins.keys()),
            'cmd_count': cmd_count,
            'dyn_count': dyn_count,
            'user_count': user_count,
            'group_count': group_count,
        }
    except Exception as e:
        return {'error': str(e)}


def _render_status_image(fw, proc_mem, sys_info, show_cpu):
    """用 image_renderer 渲染框架状态卡片图；失败返回 None"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
        return None
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'help', 'DouyinSansBold.otf')
    font_path = fp if os.path.isfile(fp) else None
    try:
        W = 560
        row_h = 30
        rows = [
            ("运行时间", fw.get('uptime', '-')),
            ("进程内存", f"{proc_mem:.1f} MB"),
        ]
        if show_cpu and 'error' not in (sys_info or {}):
            rows.append(("CPU 使用率", f"{sys_info['cpu_percent']}% ({sys_info['cpu_count']}核)"))
        rows += [
            ("OneBot 客户端", f"{fw.get('bot_count', 0)} 个在线"),
            ("已加载插件", f"{fw.get('plugin_count', 0)} 个"),
            ("注册命令", f"{fw.get('cmd_count', 0)} 条"),
            ("动态命令", f"{fw.get('dyn_count', 0)} 条"),
            ("用户数", f"{fw.get('user_count', 0)}"),
            ("活跃群", f"{fw.get('group_count', 0)}"),
            ("WebSocket", f":{fw.get('ws_port', '-')}"),
            ("Web UI", f":{fw.get('web_port', '-')}"),
        ]
        H = 44 + len(rows) * row_h + 26
        canvas = mod._get_native_or_pil_canvas(W, H, None, font_path)
        canvas.rect(0, 0, W, H, radius=0, fill="#0f1420")
        canvas.rect(0, 0, 6, H, radius=0, fill="#4a90d9")
        canvas.text(24, 18, "📊 框架运行状态", font_size=22, color="#FFFFFF")
        y = 58
        right_margin = W - 28   # 数值右对齐的右边距（右边缘不超过此处）
        label_right = 150       # 标签允许的最大右侧（预留给数值）
        for label, value in rows:
            label = str(label)
            value = str(value)
            # 标签（左对齐，超过 label_right 截断）
            try:
                lw, _ = canvas.text_metrics(label, 16)
            except Exception:
                lw = len(label) * 8
            if lw > label_right - 28:
                label = label[:10] + "…"
            canvas.text(28, y, label, font_size=16, color="#9fb3cc")
            # 数值：用 text_metrics 算宽度，左对齐放置使右边缘 = right_margin，超宽截断
            try:
                vw, _ = canvas.text_metrics(value, 16)
            except Exception:
                vw = len(value) * 8
            max_vw = right_margin - label_right
            if vw > max_vw:
                while len(value) > 1 and vw > max_vw:
                    value = value[:-1]
                    try:
                        vw, _ = canvas.text_metrics(value + "…", 16)
                    except Exception:
                        vw = len(value) * 8
                value = value + "…"
            canvas.text(right_margin - vw, y, value, font_size=16, color="#7fd8a8")
            y += row_h
        return canvas.to_png()
    except Exception as e:
        ctx.log(f"[runtime_status] 状态图渲染失败: {e}", level="error")
        return None


def handle_status(event, match):
    """框架运行状态概览（根据 show_cpu 配置决定是否显示 CPU）"""
    fw = _get_framework_info(ctx)

    if 'error' in fw:
        ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"获取状态失败: {fw['error']}"
        )
        return

    # 获取进程内存
    try:
        proc = psutil.Process(os.getpid())
        proc_mem = proc.memory_info().rss / 1024 / 1024
    except Exception:
        proc_mem = 0

    show_cpu = _get_config(ctx, 'show_cpu', True)
    sys_info = _get_system_info(ctx) if show_cpu else {}

    # 优先渲染状态卡片图（image_renderer），失败回退文本
    png = _render_status_image(fw, proc_mem, sys_info, show_cpu)
    if png:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png)
            path_str = tmp.name.replace("\\", "/")
        try:
            ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id,
                message=f"[CQ:image,file=file:///{path_str}]")
            return
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    lines = [
        " 框架运行状态",
        "━━━━━━━━━━━━━━━",
        f"运行时间: {fw['uptime']}",
        f"进程内存: {proc_mem:.1f} MB",
    ]
    if show_cpu and 'error' not in sys_info:
        lines.append(f"CPU 使用率: {sys_info['cpu_percent']}% ({sys_info['cpu_count']}核)")
    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"OneBot 客户端: {fw['bot_count']} 个在线",
        f"已加载插件: {fw['plugin_count']} 个",
        f"注册命令: {fw['cmd_count']} 条",
        f"动态命令: {fw['dyn_count']} 条",
        "━━━━━━━━━━━━━━━",
        f"用户数: {fw['user_count']}",
        f"活跃群: {fw['group_count']}",
        "━━━━━━━━━━━━━━━",
        f"WebSocket: :{fw['ws_port']}",
        f"Web UI: :{fw['web_port']}",
        f"发送 /info 查看系统详情",
    ])

    ctx.api("send_msg",
        user_id=event.user_id,
        group_id=event.group_id,
        message="\n".join(lines)
    )


def handle_info(event, match):
    """系统详细信息（根据 show_disk 配置决定是否显示磁盘）"""
    sys_info = _get_system_info(ctx)

    if 'error' in sys_info:
        ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"获取系统信息失败: {sys_info['error']}"
        )
        return

    show_disk = _get_config(ctx, 'show_disk', True)

    lines = [
        " 系统信息",
        "━━━━━━━━━━━━━━━",
        f"主机名: {sys_info['hostname']}",
        f"系统: {sys_info['platform']}",
        f"Python: {sys_info['python']}",
        "━━━━━━━━━━━━━━━",
        f"CPU 使用率: {sys_info['cpu_percent']}%",
        f"CPU 核心数: {sys_info['cpu_count']}",
        "━━━━━━━━━━━━━━━",
        f"内存: {sys_info['mem_used']:.1f} / {sys_info['mem_total']:.1f} GB ({sys_info['mem_percent']}%)",
    ]
    if show_disk:
        lines.append(f"磁盘: {sys_info['disk_used']:.1f} / {sys_info['disk_total']:.1f} GB ({sys_info['disk_percent']}%)")
    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"系统运行: {sys_info['boot_time']}",
    ])

    ctx.api("send_msg",
        user_id=event.user_id,
        group_id=event.group_id,
        message="\n".join(lines)
    )


def handle_uptime(event, match):
    """查看运行时间"""
    uptime = _format_uptime(time.time() - _start_time)
    ctx.api("send_msg",
        user_id=event.user_id,
        group_id=event.group_id,
        message=f"⏱ 框架已运行: {uptime}"
    )


def handle_plugins(event, match):
    """查看已加载插件列表"""
    fw = _get_framework_info(ctx)
    plugins = fw.get('plugins', [])

    if not plugins:
        ctx.api("send_msg",
            user_id=event.user_id,
            group_id=event.group_id,
            message="当前没有已加载的插件"
        )
        return

    loaded_info = ctx._framework.plugin_loader.get_loaded_plugins()
    lines = [f" 已加载插件 ({len(plugins)} 个)\n━━━━━━━━━━━━━━━"]
    for name in plugins:
        meta = loaded_info.get(name, {}).get('meta', {})
        version = meta.get('version', '?')
        desc = meta.get('desc', '')
        lines.append(f"• {name} v{version}\n  {desc}")

    ctx.api("send_msg",
        user_id=event.user_id,
        group_id=event.group_id,
        message="\n".join(lines)
    )


def task_cleanup():
    """定时清理状态缓存（每5分钟执行一次）"""
    global _cache, _cache_time
    _cache = {}
    _cache_time = 0
    ctx.log("状态缓存已清理")


def _dashboard_uptime():
    """仪表盘卡片：运行时间"""
    return {
        'value': _format_uptime(time.time() - _start_time),
        'label': '框架已运行',
    }


def _dashboard_system_load():
    """仪表盘卡片：系统负载"""
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory().percent
        return {
            'value': f"CPU {cpu}% / 内存 {mem}%",
            'label': '系统负载',
        }
    except Exception:
        return {'value': 'N/A', 'label': '系统负载'}


def on_unload():
    """插件卸载时的清理"""
    ctx.log("运行状态插件已卸载")