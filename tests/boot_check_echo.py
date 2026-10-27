# -*- coding: utf-8 -*-
"""echo 自检插件 · 真实启动验证。

真实启动 Framework，校验：
  1. echo 插件被加载（core_plugins.yaml 默认 enabled: true）
  2. /echo 命令已注册（命令表里有 /echo）
  3. echo 不声明 adapters 限制 → 对所有接入端（onebot/qq_official/telegram/
     discord/ws_client/http_inject）均允许（adapter_plugin_allowed 返回 True）
  4. 直接调用 handler：/echo 你好 → 回 "你好"；/echo 空 → 用法提示
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
        print(f"  [FAIL] {name}  {extra}")


async def main():
    print("== 真实启动验证（echo 自检插件）==")
    fw = Framework()
    await fw.start(wait_ready=True)
    print("  [INFO] 框架已启动")

    loaded = fw.plugin_loader.get_loaded_plugins()
    check("echo 插件已加载", 'echo' in loaded, list(loaded.keys()))

    if 'echo' in loaded:
        meta = loaded['echo'].get('meta') or {}
        check("echo 插件 meta 无 adapters 限制（覆盖全部适配器）",
              not (meta.get('adapters')), repr(meta.get('adapters')))

        # /echo 命令已写入 commands 表（命令落库是路由生效的前置）
        rows = fw.db.query("SELECT pattern FROM commands WHERE plugin_name=%s",
                           ('echo',))
        patterns = [r['pattern'] for r in rows]
        check("/echo 命令已注册（commands 表）", '/echo' in patterns, patterns)

        # 跨适配器绑定判定：echo 不限制 → 全部 True
        adapters = ['onebot', 'qq_official', 'telegram', 'discord',
                    'ws_client', 'http_inject']
        allowed = {a: fw.adapter_plugin_allowed('echo', a) for a in adapters}
        check("echo 对所有接入端均允许（adapter_plugin_allowed 全 True）",
              all(allowed.values()), allowed)

        # 直接验证 handler 真实逻辑
        mod = fw.plugin_loader.get_plugin_module('echo')
        check("拿到 echo 插件模块", mod is not None)
        ctx = getattr(mod, 'ctx', None)
        check("handler 可用模块级 ctx（加载器注入）", ctx is not None)

        if ctx is not None:
            recorded = []
            orig = ctx.send_msg
            ctx.send_msg = lambda **kw: recorded.append(kw)

            class _Match:
                def group(self, n=0):
                    return "你好" if n == 1 else "/echo 你好"

            class _Event:
                user_id = 10001
                group_id = None

            recorded.clear()
            mod.handle_echo(_Event(), _Match())
            check("/echo 你好 → 回显 \"你好\"",
                  len(recorded) == 1 and recorded[0].get('message') == "你好",
                  recorded)

            class _MatchEmpty:
                def group(self, n=0):
                    return "" if n == 1 else "/echo"

            recorded.clear()
            mod.handle_echo(_Event(), _MatchEmpty())
            check("/echo 空参数 → 回用法提示",
                  len(recorded) == 1
                  and "用法" in (recorded[0].get('message') or ""),
                  recorded)

            ctx.send_msg = orig

        # 真实路由验证：/echo 你好 经 router.route 应命中 echo 并回显「你好」
        # （验证根因修复：核心插件命令已进 commands 表 + 路由排序）
        # 注意：route() 接收原始 dict（内部自行构造 Event），不要传 Event 对象。
        raw_event = {
            "type": "message", "message_type": "private", "user_id": 10001,
            "group_id": None, "message": "/echo 你好", "adapter": "onebot",
        }
        recorded2 = []
        orig2 = ctx.send_msg
        ctx.send_msg = lambda **kw: recorded2.append(kw)
        try:
            await fw.router.route(raw_event, "onebot")
        except Exception as e:
            print(f"  [WARN] route 异常: {e}")
        ctx.send_msg = orig2
        check("router.route(/echo 你好) → 回显「你好」（真实路由命中 echo）",
              any((r.get('message') == "你好") for r in recorded2), recorded2)

        # 反向验证：echo 已纳入路由排序（修复前不在 plugin_order）
        order_names = list(getattr(fw.router, '_plugin_order', []) or [])
        check("echo 已进入路由 plugin_order（修复前缺失）",
              'echo' in order_names, order_names[:5])

    await fw.stop()
    print("  [INFO] 框架已停止")
    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 0
    try:
        code = asyncio.run(main()) or 0
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
