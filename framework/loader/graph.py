# -*- coding: utf-8 -*-
"""
插件声明式依赖（加载顺序与缺失拦截）
====================================
此前框架只有 `priority`（影响命令路由与排序），没有「插件 A 必须先于插件 B 加载」
的表达能力。插件想拿到另一个插件的服务，只能靠 `system.plugin.loaded` 事件里撞运气、
或在 `register()` 里 try/except 静默降级 —— 表现为「加载成功但功能异常」，
是最难排查的一类问题。

本模块给 `__plugin_meta__` 增加一个可选字段：

    __plugin_meta__ = {
        'name': 'mc_whitelist',
        'requires': ['llm_core'],          # 或 'llm_core, session'
    }

语义（严格、可预测）
--------------------
- `requires` 里的名字必须是**插件目录名**（与 `get_loaded_plugins()` 的键一致）；
- 依赖在本次加载序列中排在前面：`load_all` 做拓扑排序，保证先加载被依赖方；
- 依赖未启用/不存在/加载失败 → **拒绝加载本插件**并打明确错误日志，
  不再「先加载再说」（半个插件比失败更危险）；
- 循环依赖 → 环上所有插件都判定为依赖异常，拒绝加载（不会死锁）；
- 已经加载过的插件（含官方插件）视为满足，所以可以 `requires: ['llm_core']`；
- 字段缺失、类型不对、写错名字都只影响该插件自身，不影响其它插件加载。

实现要点
--------
`__plugin_meta__` 在 `main.py` 顶层字面量定义，因此**不执行插件代码**也能用 AST
`literal_eval` 静态读出依赖（与 `core/runtime.py::_read_plugin_process_tag` 同一手法）。
解析失败（动态构造的 meta）按「未声明依赖」处理，保持向后兼容；结果按 mtime 缓存，
保存即重新解析。
"""
import ast
import logging
import os
import re
import threading
from typing import Dict, List, Tuple

logger = logging.getLogger('zcbot')


def parse_meta_literal(main_file: str) -> dict:
    """静态解析 main.py 顶层的 `__plugin_meta__` 字典字面量。

    不执行插件代码；解析失败或不是字典字面量时返回空 dict。
    """
    try:
        with open(main_file, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=main_file)
    except Exception:  # noqa: BLE001
        return {}

    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == '__plugin_meta__':
                try:
                    meta = ast.literal_eval(value)
                except Exception:  # noqa: BLE001
                    return {}
                return meta if isinstance(meta, dict) else {}
    return {}


def normalize_requires(raw) -> List[str]:
    """把 requires 的各种写法归一为去重的插件名列表。

    支持：list/tuple/set、逗号或空白分隔的字符串、{'name': '>=1.0'} 形式的 dict。
    """
    if not raw:
        return []
    items = []
    if isinstance(raw, str):
        items = [x for x in re.split(r'[,\s]+', raw.strip()) if x]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, dict):
        items = [str(k).strip() for k in raw.keys() if str(k).strip()]
    else:
        return []

    out = []
    for name in items:
        # 只接受目录名安全字符，避免误把表达式/版本号当插件名（首字符须字母/下划线）
        if name and name not in out and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.\-]*', name):
            out.append(name)
    return out


class PluginGraphMixin:
    """混入 PluginLoader：静态依赖解析 + 拓扑加载顺序 + 缺失拦截。

    缓存与锁由 PluginLoader.__init__ 初始化，这里全部走 getattr 兜底，
    保证单独测试该混入时也不会 AttributeError。
    """

    # ── 静态读取 ─────────────────────────────────────────────
    def _meta_cache(self) -> Dict[str, tuple]:
        cache = getattr(self, '_declared_meta_cache', None)
        if cache is None:
            cache = {}
            self._declared_meta_cache = cache
        return cache

    def _meta_lock(self):
        lock = getattr(self, '_graph_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._graph_lock = lock
        return lock

    def declared_meta(self, plugin_name: str, plugin_path: str = None) -> dict:
        """读取插件声明的 `__plugin_meta__`（静态解析，按 mtime 缓存）"""
        main_file = os.path.join(plugin_path or os.path.join(self.plugins_dir, plugin_name),
                                 'main.py')
        if not os.path.isfile(main_file):
            return {}
        try:
            mtime = os.path.getmtime(main_file)
        except OSError:
            return {}

        cache = self._meta_cache()
        hit = cache.get(plugin_name)
        if hit and hit[0] == mtime:
            return hit[1]
        meta = parse_meta_literal(main_file)
        with self._meta_lock():
            cache[plugin_name] = (mtime, meta)
        return meta

    def declared_requires(self, plugin_name: str, plugin_path: str = None) -> List[str]:
        """读取插件声明的必需插件列表（规范化后）"""
        try:
            meta = self.declared_meta(plugin_name, plugin_path)
        except Exception:  # noqa: BLE001
            return []
        return normalize_requires(meta.get('requires'))

    # ── 运行时校验 ───────────────────────────────────────────
    def check_requires(self, plugin_name: str, plugin_path: str = None) -> List[str]:
        """返回**尚未满足**的依赖插件名列表（空列表=全部满足或未声明）"""
        requires = self.declared_requires(plugin_name, plugin_path)
        if not requires:
            return []
        with self._lock:
            loaded = set(self._loaded_plugins.keys())
        return [d for d in requires if d != plugin_name and d not in loaded]

    def get_dep_error(self, plugin_name: str) -> str:
        """取该插件上一次依赖拦截原因（无则空串）"""
        return (getattr(self, '_dep_errors', {}) or {}).get(plugin_name, '')

    def _record_dep_error(self, plugin_name: str, reason: str):
        if not hasattr(self, '_dep_errors') or self._dep_errors is None:
            self._dep_errors = {}
        self._dep_errors[plugin_name] = reason

    def clear_dep_error(self, plugin_name: str):
        if getattr(self, '_dep_errors', None):
            self._dep_errors.pop(plugin_name, None)

    # ── 排序 ────────────────────────────────────────────────
    def resolve_load_order(self, names: List[str]) -> Tuple[List[str], Dict[str, str]]:
        """按依赖关系给出加载顺序。

        :return: (可加载顺序, {被拒绝的插件名: 原因})
                 —— 被拒绝的插件不出现在顺序里，也不会因依赖它的插件而继续传递。
        """
        names = list(dict.fromkeys(names))   # 去重保序
        name_set = set(names)
        with self._lock:
            loaded = set(self._loaded_plugins.keys())

        deps: Dict[str, List[str]] = {}
        problems: Dict[str, str] = {}
        for name in names:
            try:
                declared = [d for d in self.declared_requires(name) if d != name]
            except Exception:  # noqa: BLE001
                declared = []
            deps[name] = declared
            missing = [d for d in declared if d not in name_set and d not in loaded]
            if missing:
                problems[name] = f"缺少依赖插件: {', '.join(missing)}"

        # 依赖被拒 → 被依赖方也拒（迭代到稳定，避免传递链只报一层）
        changed = True
        while changed:
            changed = False
            for name in names:
                if name in problems:
                    continue
                for dep in deps[name]:
                    if dep in problems:
                        problems[name] = f"依赖插件未加载: {dep}"
                        changed = True
                        break

        # Kahn 拓扑排序（保持声明顺序稳定，便于日志可读）
        pending = [n for n in names if n not in problems]
        order: List[str] = []
        while pending:
            progressed = False
            for name in list(pending):
                if all((d in order) or (d in loaded) or (d not in name_set)
                       for d in deps[name]):
                    order.append(name)
                    pending.remove(name)
                    progressed = True
            if not progressed:
                # 剩下的是环：整环拒绝，不死锁
                for name in pending:
                    problems[name] = "循环依赖: " + ' -> '.join(pending + [pending[0]])
                break

        return order, problems
