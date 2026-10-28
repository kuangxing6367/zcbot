# -*- coding: utf-8 -*-
"""help 内置帮助插件 · 真实启动验证。

真实启动 Framework，校验：
  1. help 插件被加载（core_plugins.yaml 默认 enabled: true）
  2. /help（及别名 /帮助、/菜单）已注册到 commands 表
  3. help 不声明 adapters 限制 → 对所有接入端均允许
  4. 真实 router.route("/help") 命中 help 并回显命令清单（含真实命令）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.core.base import Framework

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  ->  {extra}")


async def main():
    print("== 真实启动验证（help 内置帮助插件）==")
    fw = Framework()
    await fw.start(wait_ready=True)
    print("  [INFO] 框架已启动")

    loaded = fw.plugin_loader.get_loaded_plugins()
    check("help 插件已加载", 'help' in loaded, list(loaded.keys()))

    if 'help' in loaded:
        rows = fw.db.query(
            "SELECT pattern, alias FROM commands WHERE plugin_name=%s", ('help',))
        patterns = [r['pattern'] for r in rows]
        aliases = ",".join((r.get('alias') or "") for r in rows)
        check("/help 命令已注册（commands 表）", '/help' in patterns, patterns)
        check("别名 /帮助 已注册（alias 列）", '/帮助' in aliases, aliases)
        check("别名 /菜单 已注册（alias 列）", '/菜单' in aliases, aliases)

        adapters = ['onebot', 'qq_official', 'telegram', 'discord',
                    'ws_client', 'http_inject']
        allowed = {a: fw.adapter_plugin_allowed('help', a) for a in adapters}
        check("help 对所有接入端均允许（adapter_plugin_allowed 全 True）",
              all(allowed.values()), allowed)

        mod = fw.plugin_loader.get_plugin_module('help')
        ctx = getattr(mod, 'ctx', None)
        check("拿到 help 插件模块与模块级 ctx", mod is not None and ctx is not None)

        # 真实路由验证：/help 经 router.route 命中 help 并返回命令清单
        raw_event = {
            "type": "message", "message_type": "private", "user_id": 10001,
            "group_id": None, "message": "/help", "adapter": "onebot",
        }
        recorded = []
        orig = ctx.send_msg
        ctx.send_msg = lambda **kw: recorded.append(kw)
        try:
            await fw.router.route(raw_event, "onebot")
        except Exception as e:
            print(f"  [WARN] route 异常: {e}")
        ctx.send_msg = orig
        joined = "\n".join((r.get('message') or "") for r in recorded)
        check("router.route(/help) → 返回命令清单",
              len(recorded) >= 1 and "命令清单" in joined, joined[:200])
        # 清单应至少含一条真实命令（如 echo / help 自身）
        check("命令清单含真实命令（echo 或 help）",
              ("echo" in joined or "help" in joined), joined[:200])

        order_names = list(getattr(fw.router, '_plugin_order', []) or [])
        check("help 已进入路由 plugin_order", 'help' in order_names,
              order_names[:5])

    await fw.stop()
    print("  [INFO] 框架已停止")
    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
