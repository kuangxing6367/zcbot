# -*- coding: utf-8 -*-
"""多平台接入 · 真实启动冒烟验证。

真实构造并启动 Framework（加载插件 / 注册适配器 / 自动建扩展表），然后校验：
  1. 框架能正常启动（无致命异常）
  2. drive 表被自动创建
  3. 适配器在启动期被自动登记（drive 表有行、内存缓存有 uuid）
  4. multi_platform.namespace 默认读取为 False（旧行为不变）

运行：python tests/boot_check_multi_platform.py
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
    print("== 真实启动冒烟验证（多平台接入）==")
    fw = Framework()
    await fw.start(wait_ready=True)
    print("  [INFO] 框架已启动")

    # 1. drive 表自动创建
    has = fw.db.table_exists('drive')
    check("drive 表已自动创建", has)
    if not has:
        print("  [WARN] drive 表缺失，后续检查跳过")
        await fw.stop()
        return

    # 2. 适配器自动登记
    adapters = getattr(fw.services, '_adapters', {}) or {}
    print(f"  [INFO] 已注册适配器(ServiceRegistry): {list(adapters.keys())}")
    print(f"  [INFO] on_adapter_registered 回调: "
          f"{getattr(fw.services, 'on_adapter_registered', None)}")
    for r in fw.adapter_registry.all():
        print(f"         · 启动期已登记 adapter_key={r['adapter_key']!r} "
              f"uuid={r['uuid'][:8]}… kind={r.get('kind')!r}")

    # 3. 真实注册链路：向真实 ServiceRegistry 注册桩适配器，验证回调→自动登记
    #    （本环境 core_plugins.onebot_adapter / qq_official / ws_client 均为 False，
    #     故用桩适配器走完整的 register → on_adapter_registered → drive 写入链路）
    from framework.messaging.protocol import ProtocolAdapter

    class _StubAdapter(ProtocolAdapter):
        adapter_id = ''

        def __init__(self, cid):
            self.adapter_id = cid

        def _connection_id(self):
            return self.adapter_id

        async def handle_event(self, raw_event, bot_name):
            return None

        async def call_api(self, action, bot=None, **params):
            return {}

        def get_connected_bots(self):
            return [self.adapter_id]

        def start(self):
            pass

        async def stop(self):
            pass

    print("  [INFO] 注册桩适配器（走真实 register 链路）…")
    fw.services.register('protocol_adapter', _StubAdapter('qq_official'))
    fw.services.register('protocol_adapter', _StubAdapter('qq_official_2'))

    rows = fw.adapter_registry.all()
    keys = [r['adapter_key'] for r in rows]
    print(f"  [INFO] drive 表现有登记: {[(r['adapter_key'], r['uuid'][:8]) for r in rows]}")
    check("适配器注册后 drive 表出现登记行", bool(rows), repr(rows))
    check("多实例各自登记（qq_official / qq_official_2）",
          'qq_official' in keys and 'qq_official_2' in keys, repr(keys))
    if 'qq_official' in keys and 'qq_official_2' in keys:
        u_map = {r['adapter_key']: r['uuid'] for r in rows}
        check("多实例 uuid 互不相同",
              u_map['qq_official'] != u_map['qq_official_2'], u_map)
        check("内存缓存与库一致",
              fw.adapter_registry.uuid_for('qq_official') == u_map['qq_official']
              and fw.adapter_registry.uuid_for('qq_official_2') == u_map['qq_official_2'])

    # 4. 命名空间开关默认关闭
    ns = fw.multi_platform_namespace()
    check("multi_platform.namespace 默认 False", ns is False, repr(ns))

    # 清理：删除本次验证写入的桩登记行，不污染真实库
    try:
        fw.db.execute("DELETE FROM drive WHERE kind='_StubAdapter'")
        print("  [INFO] 已清理本次验证写入的桩登记行")
    except Exception as e:
        print(f"  [WARN] 清理桩登记行失败: {e}")

    await fw.stop()
    print("  [INFO] 框架已停止")

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 0
    try:
        code = asyncio.run(main()) or 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
