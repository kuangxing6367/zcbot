"""
真·官方插件：plugins/llm_blacklist（逐字照抄，仅摘除 host 进程专属依赖）

原插件唯一无法跨进程的部分是 `_wire_chat_gate()` 里的
    ctx._framework.services.get('llm_core')
—— IIPC 插件运行在独立进程，没有 `_framework`。此处按插件自身的 try/except
语义等效降级（挂不上闸门静默跳过），其余代码（建表 SQL、正则命令、
require_superuser、async handler、裸 ctx、users 表超管兜底）一字未改。

验证点：MySQL 风格 DDL（VARCHAR/TEXT 无 DEFAULT）在 SQLite 侧的兼容、
正则命令 + require_superuser 权限链路、async handler、ctx.get_config。
"""
import time

__plugin_meta__ = {
    "name": "LLM黑名单",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "LLM 对话黑名单管理（超管用 /插件拉黑 <QQ号> 拉黑，预防上下文滥用）",
    "priority": 55,
}

# 兼容说明：
#   - MySQL 5.7 及以下不允许 TEXT/BLOB/JSON 列带 DEFAULT（错误码 1101）
#   - TEXT 不能直接作为 PRIMARY KEY，故 user_id 用 VARCHAR(64)
#   - reason/operator 去掉 DEFAULT，改由代码层空串兜底（见 handle_add）
_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS llm_blacklist ("
    "user_id VARCHAR(64) PRIMARY KEY, "
    "reason TEXT, "
    "operator TEXT, "
    "created_at INTEGER DEFAULT 0)"
)


def register(ctx):
    """插件注册入口"""
    # 建表（幂等；失败不影响加载，闸门侧也有异常兜底）
    try:
        ctx.db_execute(_TABLE_SQL, ())
        ctx.log("llm_blacklist 建表就绪")
    except Exception as e:
        ctx.log(f"llm_blacklist 建表失败(不影响加载): {e}", level="warning")

    # 挂到 llm_core 的对话闸门（llm_core 未加载时监听加载完成事件重试）
    if not _wire_chat_gate():
        try:
            ctx.on("system.plugin.loaded", _wire_chat_gate)
        except Exception:
            pass

    ctx.command(r"^/插件拉黑\s+(\d+)\s*(.*)$", handle_add, priority=55,
                require_superuser=True,
                description="拉黑用户(禁止使用LLM对话)，用法: /插件拉黑 <QQ号> [原因]")
    ctx.command(r"^/插件取消拉黑\s+(\d+)\s*$", handle_remove, priority=55,
                require_superuser=True,
                description="解除拉黑，用法: /插件取消拉黑 <QQ号>")
    ctx.command(r"^/插件黑名单\s*$", handle_list, priority=55,
                require_superuser=True,
                description="查看 LLM 对话黑名单列表")
    ctx.log("llm_blacklist 已注册：/插件拉黑 /插件取消拉黑 /插件黑名单")



def _make_gate():
    """生成 llm_core 对话闸门：命中黑名单返回拒绝提示，否则放行"""
    def gate(user_id):
        try:
            row = ctx.db_query_one(
                "SELECT 1 FROM llm_blacklist WHERE user_id=%s", (str(user_id),))
        except Exception:
            return None  # 数据库故障不拦截对话
        if row:
            return str(_get_config("blacklist_tip", "") or "你已被拉黑，无法使用 LLM 对话。")
        return None
    return gate


def _wire_chat_gate(*_args):
    """把闸门挂到 llm_core 服务（对方未加载时挂不上，静默等下次事件重试）

    IIPC 说明：插件在独立进程，无 ctx._framework；原插件此处用
    ctx._framework.services.get('llm_core')，跨进程不可达，故按
    「对方未加载」分支静默返回 False（与原插件该分支行为一致）。
    """
    return False


def _get_config(key, default=None):
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default

# ===================== 工具函数 =====================

def _reply(event, message):
    """回复发起者（群内回群，私聊回私聊）"""
    try:
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if getattr(event, "is_group", False) else None,
            message=message,
        )
    except Exception as e:
        ctx.log(f"llm_blacklist 回复失败: {e}", level="warning")


def _is_super(event):
    """判断发起者是否为超级管理员（多来源兜底）"""
    if getattr(event, "is_superuser", False):
        return True
    if str(getattr(event, "role", "") or "") == "super":
        return True
    try:
        rows = ctx.db_query(
            "SELECT 1 FROM users WHERE user_id=%s AND role='super'",
            (str(event.user_id),),
        ) or []
        return bool(rows)
    except Exception:
        return False


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts or 0)))
    except Exception:
        return "-"


# ===================== 命令处理 =====================

async def handle_add(event, match):
    """拉黑用户：/插件拉黑 <QQ号> [原因]"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    uid = (match.group(1) or "").strip()
    reason = (match.group(2) or "").strip()
    if not uid or not uid.isdigit():
        await _reply(event, "❌ 用法错误：/插件拉黑 <QQ号> [原因]，QQ号必须是数字。")
        return

    now = int(time.time())
    op = str(event.user_id)
    try:
        # 先查再决定更新/插入（兼容 MySQL 与 SQLite）
        exists = ctx.db_query_one(
            "SELECT 1 FROM llm_blacklist WHERE user_id=%s", (uid,))
        if exists:
            ctx.db_execute(
                "UPDATE llm_blacklist SET reason=%s, operator=%s, created_at=%s WHERE user_id=%s",
                (reason, op, now, uid),
            )
        else:
            ctx.db_execute(
                "INSERT INTO llm_blacklist (user_id, reason, operator, created_at) VALUES (%s,%s,%s,%s)",
                (uid, reason, op, now),
            )
    except Exception as e:
        ctx.log(f"llm_blacklist 拉黑失败: {e}", level="error")
        await _reply(event, f"❌ 拉黑失败：{e}")
        return

    await _reply(
        event,
        f"🚫 已将 {uid} 加入 LLM 对话黑名单，无法再使用对话功能。"
        + (f"\n原因：{reason}" if reason else "")
        + "\n解除请发：/插件取消拉黑 " + uid,
    )


async def handle_remove(event, match):
    """解除拉黑：/插件取消拉黑 <QQ号>"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    uid = (match.group(1) or "").strip()
    if not uid or not uid.isdigit():
        await _reply(event, "❌ 用法错误：/插件取消拉黑 <QQ号>，QQ号必须是数字。")
        return

    try:
        ctx.db_execute("DELETE FROM llm_blacklist WHERE user_id=%s", (uid,))
    except Exception as e:
        ctx.log(f"llm_blacklist 解除拉黑失败: {e}", level="error")
        await _reply(event, f"❌ 解除拉黑失败：{e}")
        return

    # 确认是否真的删掉了（防止 QQ 号从未在黑名单中）
    try:
        still = ctx.db_query_one(
            "SELECT 1 FROM llm_blacklist WHERE user_id=%s", (uid,))
    except Exception:
        still = None
    if still:
        await _reply(event, f"❌ 解除失败：{uid} 仍在黑名单中，请重试。")
        return
    await _reply(event, f"✅ 已解除 {uid} 的拉黑，可以正常使用 LLM 对话了。")


async def handle_list(event, match):
    """查看黑名单列表：/插件黑名单"""
    if not _is_super(event):
        await _reply(event, "❌ 权限不足：仅超级管理员可执行此操作。")
        return
    try:
        rows = ctx.db_query(
            "SELECT user_id, reason, operator, created_at FROM llm_blacklist ORDER BY created_at DESC",
            (),
        ) or []
    except Exception as e:
        ctx.log(f"llm_blacklist 查询失败: {e}", level="error")
        await _reply(event, f"❌ 查询失败：{e}")
        return

    if not rows:
        await _reply(event, "📭 当前 LLM 对话黑名单为空。")
        return

    limit = int(ctx.get_config("list_limit", 20) or 20)
    lines = [f"🚫 LLM 对话黑名单（共 {len(rows)} 人）："]
    for i, r in enumerate(rows[:limit], 1):
        uid = r.get("user_id") if isinstance(r, dict) else r[0]
        reason = (r.get("reason") if isinstance(r, dict) else r[1]) or ""
        op = (r.get("operator") if isinstance(r, dict) else r[2]) or ""
        ts = (r.get("created_at") if isinstance(r, dict) else r[3]) or 0
        line = f"{i}. {uid}"
        if reason:
            line += f"（{reason}）"
        line += f"  [{_fmt_time(ts)}]"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"…… 还有 {len(rows) - limit} 条未显示")
    await _reply(event, "\n".join(lines))
