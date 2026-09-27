# -*- coding: utf-8 -*-
"""
内核自修复能力故障注入验证。

覆盖四个缺口：
  1. 插件 main.py 损坏 → load_plugin 失败登记 _failed_mtimes；
     修复后 heartbeat_register 自动重新加载并注册（无需重启框架）。
  2. core_plugins.yaml 损坏 → 备份 .bak 留证 + 自动重建默认配置。
  3. FileStore 表文件损坏 → 改名 .corrupt 留证 + 返回 {}，不崩溃。
  4. event_buffer sqlite 写连续失败达阈值 → 自动禁用溢出层，事件转走内存兜底。

运行：python tests/test_self_heal.py  （直接 python 执行，非 pytest）
"""
import os
import sys
import shutil
import tempfile
import time
import types
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_framework(db):
    """构造最小可用 mock framework"""
    fw = types.SimpleNamespace(
        db=db,
        config={'plugin': {}},
        router=types.SimpleNamespace(_invalidate_cache=lambda: None),
    )
    fw.register_raw_message_handler = lambda *a, **k: None
    return fw


class MockDB:
    """记录 execute/query 调用的最小 DB mock"""
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append(('execute', sql, params))
        return None

    def query(self, sql, params=None):
        self.calls.append(('query', sql, params))
        return []

    def query_one(self, sql, params=None):
        self.calls.append(('query_one', sql, params))
        return None


def test_plugin_self_reload(tmp):
    """场景1：插件 main.py 损坏 → 失败登记 → 修复 → 心跳自愈重载"""
    print("场景1: 插件 main.py 损坏自愈重载")
    plugins_dir = os.path.join(tmp, 'plugins')
    dat_dir = os.path.join(tmp, 'plugins_dat')
    plug_dir = os.path.join(plugins_dir, 'plug_broken')
    os.makedirs(plug_dir)

    # 语法错误的 main.py
    bad_main = os.path.join(plug_dir, 'main.py')
    with open(bad_main, 'w', encoding='utf-8') as f:
        f.write("def register(ctx):\n    syntax error here\n")

    db = MockDB()
    from framework.loader.base import PluginLoader
    loader = PluginLoader(plugins_dir, make_framework(db), plugins_dat_dir=dat_dir)
    # 阻断 pip 安装链路（测试环境不联网装依赖）
    loader.check_dependencies = lambda name: {'missing': [], 'conflicts': []}
    loader._record_dep_status = lambda *a, **k: None

    # 1) 损坏文件加载失败
    ok = loader.load_plugin('plug_broken')
    check("损坏 main.py 加载失败", ok is False)
    check("失败插件已登记 _failed_mtimes", 'plug_broken' in loader._failed_mtimes)

    # 修复 main.py：寄存器一个合法的 register（不依赖任何外部 ctx API，聚焦自愈链路）
    fixed_main = "def register(ctx):\n    return True\n"
    time.sleep(0.05)  # 保证 mtime 严格变大
    with open(bad_main, 'w', encoding='utf-8') as f:
        f.write(fixed_main)
    time.sleep(0.05)

    # 2) 心跳检查 → 应自动重新加载并注册
    loader.heartbeat_register()
    check(
        "修复后插件已自愈（_failed_mtimes 清除）",
        'plug_broken' not in loader._failed_mtimes,
    )
    check(
        "修复后插件进入 _loaded_plugins",
        'plug_broken' in loader._loaded_plugins,
    )
    if 'plug_broken' in loader._loaded_plugins:
        check("register_func 已就位", callable(loader._loaded_plugins['plug_broken']['register_func']))
    # DB 状态应为 running
    ups = [c for c in db.calls if c[0] == 'execute' and 'status=\'running\'' in c[1]]
    check("DB 插件状态已更新为 running", bool(ups))

# 3) 直接替换 register_func 为抛异常版本 + touch 文件触发心跳
    #    （心跳只增量回调 register，不重载模块，因此改写 main.py 内容不会生效）
    def boom_register(ctx):
        raise RuntimeError('boom')
    with loader._lock:
        loader._loaded_plugins['plug_broken']['register_func'] = boom_register
    with open(bad_main, 'w', encoding='utf-8') as f:
        f.write("def register(ctx):\n    return True\n")
    time.sleep(0.05)
    loader.heartbeat_register()
    check("register 异常后登记失败快照", 'plug_broken' in loader._failed_mtimes)
    # 快照未经修复变化，再次心跳不重复加载（retry 因 cur == failed_snap 跳过）
    loader.heartbeat_register()
    check("同快照不重复无效重载", 'plug_broken' in loader._failed_mtimes)


def test_core_plugins_yaml_backup(tmp):
    """场景2：core_plugins.yaml 损坏 → .bak 备份 + 自动重建默认配置"""
    print("场景2: core_plugins.yaml 损坏自修复")
    import framework.config as cfg_mod

    yaml_path = os.path.join(tmp, 'core_plugins.yaml')
    with open(yaml_path, 'w', encoding='utf-8') as f:
        f.write(": bad yaml [\n  {{unclosed")

    orig_yaml, orig_scan = cfg_mod.CORE_PLUGINS_YAML, cfg_mod._scan_core_plugins
    cfg_mod.CORE_PLUGINS_YAML = yaml_path
    cfg_mod._scan_core_plugins = lambda: ['http_api']  # 假装已安装 http_api

    try:
        out = cfg_mod._autoload_core_plugins({'core_plugins': {}})
    finally:
        cfg_mod.CORE_PLUGINS_YAML = orig_yaml
        cfg_mod._scan_core_plugins = orig_scan

    bak = f"{yaml_path}.bak"
    check("损坏文件已备份为 .bak", os.path.isfile(bak))
    check("已自动重建合法 yaml", os.path.isfile(yaml_path))
    with open(yaml_path, 'r', encoding='utf-8') as f:
        content = f.read()
    check("重建内容为合法 YAML（可被 safe_load）", bool(cfg_mod.yaml.safe_load(content)))
    check("重建后 http_api 配置块被补全并返回", 'http_api' in out)
    check("重建后 http_api enabled 为默认值 false", out.get('http_api', {}).get('enabled') is False)


def test_filestore_corrupt(tmp):
    """场景3：FileStore 表文件损坏 → .corrupt 留证 + 返回 {}"""
    print("场景3: FileStore 表文件损坏留证")
    from framework.database.file_store import FileStore

    store_dir = os.path.join(tmp, 'filestore')
    os.makedirs(store_dir)
    tbl = os.path.join(store_dir, 'plugins.json')
    with open(tbl, 'w', encoding='utf-8') as f:
        f.write("{ broken json !!!")

    store = FileStore({'fallback_dir': store_dir})
    data = store._read_table('plugins')
    check("损坏表读取返回 {}", data == {})
    check("损坏文件已改为 .corrupt", os.path.isfile(f"{tbl}.corrupt"))
    check("原文件已移走（不再反复报错）", not os.path.isfile(tbl))
    # 再次读取应稳定返回 {} 且不抛异常
    data2 = store._read_table('plugins')
    check("重复读取稳定 {} 不崩溃", data2 == {})

    # 写入应能在留证后重新建立新表
    store.put('plugins', 'k1', {'a': 1})
    check("留证后写入重建新表", store.get('plugins', 'k1') == {'a': 1})


def test_event_buffer_sqlite_downgrade(tmp):
    """场景4：event_buffer sqlite 写连续失败 → 自动禁用溢出层 → 走 L3 兜底"""
    print("场景4: event_buffer sqlite 写失败自动降级")
    from framework.core.event_buffer import EventBuffer

    data_dir = os.path.join(tmp, 'bufdata')
    os.makedirs(data_dir)
    # maxsize=1：首个事件占满 L1，后续事件强制溢出到 sqlite 失败分支
    buf = EventBuffer({'maxsize': 1, 'buffer': {'sqlite_enabled': True,
                                                'sqlite_fail_threshold': 4}}, data_dir)
    check("sqlite 层初始启用", buf.sqlite_enabled is True)

    # 注入故障：让 sqlite 连接不可用（None 连接 → execute 抛 AttributeError → 走失败分支）
    try:
        buf._sqlite_conn.close()
    except Exception:
        pass
    buf._sqlite_conn = None

    import asyncio

    async def drive():
        # 6 个事件：第 1 个占 L1，第 2-6 个溢出触达 sqlite 连续失败（5 次 ≥ 阈值 4）
        for i in range(6):
            await buf.put({'e': i})
        await asyncio.sleep(0.05)

    asyncio.run(drive())
    check("连续写失败后 sqlite 溢出层自动禁用", buf.sqlite_enabled is False)
    check("连接已释放", buf._sqlite_conn is None)
    check("失败计数达到阈值", buf._sqlite_fail_streak >= 4)
    check("事件未丢失（转入 L3 内存兜底）", buf._l3_bytes > 0 or buf._l1_bytes > 0)

    # 禁用后 put_async 仍可正常入队（走 L3）
    async def drive2():
        ok = False
        for i in range(10, 14):
            ok = await buf.put({'e': i})
        return ok

    ok = asyncio.run(drive2())
    check("禁用后事件仍成功入队（L3 兜底生效）", ok is True)


def main():
    tmp = tempfile.mkdtemp(prefix='zcbot_selfheal_')
    print(f"临时工作目录: {tmp}")
    try:
        test_plugin_self_reload(tmp)
        print()
        test_core_plugins_yaml_backup(tmp)
        print()
        test_filestore_corrupt(tmp)
        print()
        test_event_buffer_sqlite_downgrade(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()