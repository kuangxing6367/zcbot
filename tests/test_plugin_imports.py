# -*- coding: utf-8 -*-
"""
插件加载器导入机制回归测试
================================
覆盖 framework/loader.py 的「合成包 + 三层模块名」机制：

1. main.py 与子模块支持相对导入（from .x import Y / from . import x）；
2. 子模块内部、嵌套包内部的多级相对导入可用（from ..x import Y）；
3. 旧写法「短名绝对导入」（from x import Y）继续可用；
4. 同一模块对象同时注册三个名字：plugin_<p>.<m> / plugin_<p>_<m> / <m>；
5. 两个插件存在同名子模块时互不污染（点分层级名隔离）；
6. _purge_plugin_modules 能清掉全部别名，热重载后拿到新代码；
7. 对仓库真实插件 plugins/help（main + draw.py 短名导入）向后兼容。

运行：python tests/test_plugin_imports.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.loader import PluginLoader

ok = fail = 0


def chk(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}  {extra}")


def bare_loader(plugins_dir):
    """构造不走 __init__（无需 framework/db）的加载器，只测模块装载相关方法"""
    loader = PluginLoader.__new__(PluginLoader)
    loader.plugins_dir = plugins_dir
    return loader


def load_like_framework(loader, plugin_name, plugin_path):
    """复刻 load_plugin 中「合成包 → 预载子模块 → 执行 main」的顺序（不含依赖/DB）"""
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    module = loader._ensure_plugin_package(plugin_name, plugin_path)
    loader._preload_plugin_submodules(plugin_name, plugin_path)
    spec = importlib.util.spec_from_file_location(
        f"plugin_{plugin_name}", os.path.join(plugin_path, "main.py"))
    module.__spec__ = spec
    spec.loader.exec_module(module)
    return module


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ----------------------------------------------------------------------
print("== 1. 合成包机制：相对导入 / 短名绝对导入 / 嵌套包 ==")
root = tempfile.mkdtemp(prefix="zcb_import_")
try:
    pdir = os.path.join(root, "chatroom")
    write(os.path.join(pdir, "utils.py"),
          "U = 'utils-value'\n"
          "def util_fn():\n    return 'fn'\n")
    write(os.path.join(pdir, "draw.py"),
          "D = 'draw-value'\nclass Drawer: pass\n")
    # 子模块内部相对导入兄弟模块
    write(os.path.join(pdir, "ws_server.py"),
          "from . import utils as _u\n"
          "from .utils import U\n"
          "class WsServer:\n"
          "    def ok(self):\n"
          "        return _u.util_fn() == 'fn' and U == 'utils-value'\n")
    # 嵌套包：__init__ 相对导入子模块，子模块二级相对导入回到顶层
    write(os.path.join(pdir, "core", "__init__.py"),
          "from .sub import X\n")
    write(os.path.join(pdir, "core", "sub.py"),
          "from ..utils import U\nX = 'sub:' + U\n")
    # main：混用相对导入与短名绝对导入
    write(os.path.join(pdir, "main.py"),
          "from .ws_server import WsServer\n"
          "from . import utils\n"
          "from .core.sub import X\n"
          "from draw import D, Drawer\n"
          "__plugin_meta__ = {'name': 'chatroom'}\n"
          "def register(ctx): pass\n"
          "RESULT = (WsServer().ok(), utils.U, X, D, issubclass(Drawer, object))\n")

    loader = bare_loader(root)
    main_mod = load_like_framework(loader, "chatroom", pdir)

    chk("main 相对导入 .ws_server 成功", main_mod.RESULT[0] is True, main_mod.RESULT)
    chk("main `from . import utils`", main_mod.RESULT[1] == "utils-value")
    chk("嵌套包二级相对导入 ..utils", main_mod.RESULT[2] == "sub:utils-value", main_mod.RESULT[2])
    chk("短名绝对导入 from draw import D", main_mod.RESULT[3] == "draw-value")
    chk("main.__package__ 指向合成包", main_mod.__package__ == "plugin_chatroom")
    chk("main.__path__ 指向插件目录",
        os.path.normpath(main_mod.__path__[0]) == os.path.normpath(pdir))

    dotted = sys.modules.get("plugin_chatroom.ws_server")
    legacy = sys.modules.get("plugin_chatroom_ws_server")
    short = sys.modules.get("ws_server")
    chk("三层名字全部注册", dotted is not None and legacy is not None and short is not None)
    chk("三层名字指向同一模块对象", dotted is legacy is short)
    chk("嵌套包 plugin_chatroom.core 已注册", "plugin_chatroom.core" in sys.modules)
    chk("懒加载的深层模块 plugin_chatroom.core.sub 已注册",
        "plugin_chatroom.core.sub" in sys.modules)
    chk("子模块 __package__ 正确", dotted.__package__ == "plugin_chatroom")

    # ------------------------------------------------------------------
    print("== 2. 双插件同名子模块隔离 ==")
    pdir2 = os.path.join(root, "chatroom2")
    write(os.path.join(pdir2, "draw.py"), "D = 'draw-value-2'\n")
    write(os.path.join(pdir2, "main.py"),
          "from .draw import D\n"
          "from draw import D as D_SHORT\n"
          "def register(ctx): pass\n")
    main2 = load_like_framework(loader, "chatroom2", pdir2)
    chk("插件2 相对导入拿到自己的 draw", main2.D == "draw-value-2")
    chk("插件1 的点分层级模块未被覆盖",
        sys.modules["plugin_chatroom.draw"].D == "draw-value")
    chk("插件2 的点分层级模块独立",
        sys.modules["plugin_chatroom2.draw"].D == "draw-value-2")
    chk("短名槽位由后加载者占据（旧机制语义不变）",
        sys.modules["draw"].D == "draw-value-2")
    chk("插件1 main 已绑定的引用不受短名覆盖影响",
        main_mod.RESULT[3] == "draw-value")

    # ------------------------------------------------------------------
    print("== 3. purge 清理 + 热重载拿新代码 ==")
    loader._purge_plugin_modules("chatroom", pdir)
    gone = [k for k in sys.modules if k == "plugin_chatroom"
            or k.startswith("plugin_chatroom.")
            or k.startswith("plugin_chatroom_")]
    chk("purge 后点分层级/下划线名全部移除", gone == [], gone)
    # 短名 draw 现属于插件2，purge 插件1 不应误删
    chk("purge 不误伤其他插件的短名", "draw" in sys.modules)

    write(os.path.join(pdir, "draw.py"),
          "D = 'draw-reloaded'\nclass Drawer: pass\n")
    main_re = load_like_framework(loader, "chatroom", pdir)
    chk("热重载后相对导入拿到新代码",
        sys.modules["plugin_chatroom.draw"].D == "draw-reloaded")
    chk("热重载产生新的 main 模块对象", main_re is not main_mod)
    loader._purge_plugin_modules("chatroom", pdir)
    loader._purge_plugin_modules("chatroom2", pdir2)

    # ------------------------------------------------------------------
    print("== 4. 预载失败回滚：半初始化模块不残留 ==")
    pdir3 = os.path.join(root, "broken")
    write(os.path.join(pdir3, "bad.py"), "raise RuntimeError('boom at import')\n")
    write(os.path.join(pdir3, "main.py"), "def register(ctx): pass\n")
    loader._ensure_plugin_package("broken", pdir3)
    loader._preload_plugin_submodules("broken", pdir3)  # bad.py 会告警并回滚
    chk("失败子模块的点分层级名已回滚", "plugin_broken.bad" not in sys.modules)
    chk("失败子模块的下划线名已回滚", "plugin_broken_bad" not in sys.modules)
    # 合成包 __path__ 兜底：显式相对导入仍可由原生 finder 重新解析并复现错误
    chk("合成包仍在，可供兜底解析", "plugin_broken" in sys.modules)
    loader._purge_plugin_modules("broken", pdir3)

    # ------------------------------------------------------------------
    print("== 5. 同秒同尺寸快速热重载：不得复用旧字节码 ==")
    pdir5 = os.path.join(root, "rapid")
    write(os.path.join(pdir5, "main.py"),
          "from .val import V\n"
          "def register(ctx): pass\n"
          "R = V\n")
    rapid_fresh = True
    for n in range(3, 9):  # V = 3..8 均为单字节，文件尺寸恒为 6
        write(os.path.join(pdir5, "val.py"), f"V = {n}\n")
        loader._purge_plugin_modules("rapid", pdir5)
        m5 = load_like_framework(loader, "rapid", pdir5)
        if m5.R != n:
            rapid_fresh = False
    chk("同秒同尺寸改写后每次重载都拿到新源码", rapid_fresh)
    chk("插件模块不生成 __pycache__",
        not os.path.isdir(os.path.join(pdir5, "__pycache__")))
    loader._purge_plugin_modules("rapid", pdir5)

finally:
    # 清理测试期间加入的 sys.path
    for p in list(sys.path):
        if os.path.normpath(p).startswith(os.path.normpath(root)):
            sys.path.remove(p)
    shutil.rmtree(root, ignore_errors=True)

# ----------------------------------------------------------------------
print("== 5. 真实仓库插件 plugins/help 向后兼容（main + draw.py）==")
proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
help_dir = os.path.join(proj_root, "plugins", "help")
if os.path.isdir(help_dir):
    loader = bare_loader(os.path.join(proj_root, "plugins"))
    help_mod = load_like_framework(loader, "help", help_dir)
    chk("help main 可执行并含 register", callable(getattr(help_mod, "register", None)))
    chk("help/draw.py 点分层级名注册", "plugin_help.draw" in sys.modules)
    chk("help/draw.py 下划线别名注册", "plugin_help_draw" in sys.modules)
    chk("help/draw.py 短名注册", sys.modules.get("draw") is sys.modules.get("plugin_help.draw"))
    chk("短名绝对导入 from draw import ZcbotHelpDrawer 可用",
        hasattr(sys.modules["plugin_help.draw"], "ZcbotHelpDrawer"))
    loader._purge_plugin_modules("help", help_dir)
    chk("purge 后 help 模块清空", "plugin_help" not in sys.modules
        and "plugin_help.draw" not in sys.modules and "plugin_help_draw" not in sys.modules)
    sys.path[:] = [p for p in sys.path if os.path.normpath(p) != os.path.normpath(help_dir)]
else:
    print("  SKIP  未找到 plugins/help")

print(f"\n结果: PASS={ok}  FAIL={fail}")
sys.exit(1 if fail else 0)
