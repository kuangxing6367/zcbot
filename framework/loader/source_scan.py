"""插件源码静态扫描（AST）：启动期体检插件的阻塞与退出写法。

只做保守的静态判断，不执行插件代码：

- 模块作用域（含类体、装饰器、默认参数）里的同步退出 / 同步阻塞调用；
- ``async def`` 函数体内**直接**调用的同步阻塞函数与退出调用；
- 普通函数体内的 ``os._exit`` / ``os.abort``：插件跑在核心进程内，这类调用一旦
  被 handler 触发就会带走整个框架进程；``main`` / ``cli`` / ``__main__`` 这类
  脚本入口与 ``if __name__`` 守卫块豁免。

嵌套的同步函数是「定义时无害、调用时才生效」，故意不报，避免误伤官方插件里
``async handler`` 临时切线程池执行的 ``time.sleep``。扫描失败（语法错误、文件
丢失）只记录调试日志并返回空清单，真正的加载错误仍由后续 exec_module 报出。
"""
import ast
import logging
import os

logger = logging.getLogger('zcbot')

# 不可拦截的退出：跳过程序自身的清理（finally、atexit、锁释放），进程当场消失。
# 插件 handler 跑在核心进程内，因此函数体里的这类调用同样是致命写法。
_HARD_EXIT = frozenset({'os._exit', 'os.abort'})

# 可拦截的退出：抛 SystemExit，上层有机会捕获。
_SOFT_EXIT = frozenset({'exit', 'quit', 'sys.exit', 'builtins.exit', 'builtins.quit'})

# 退出：模块作用域执行到就会直接结束进程，装饰器同理。
EXIT_CALLS = _HARD_EXIT | _SOFT_EXIT

# 脚本入口函数：插件自带的自测/CLI 入口允许自行退出，不按框架内运行处理。
_SCRIPT_ENTRY_NAMES = frozenset({'main', 'cli', '__main__'})

# 同步阻塞：当前线程会停在这里；事件循环里尤其致命。
BLOCKING_CALLS = frozenset({
    'input', 'time.sleep', 'os.system',
    'subprocess.run', 'subprocess.call', 'subprocess.check_call',
    'subprocess.check_output', 'subprocess.getoutput', 'subprocess.getstatusoutput',
    'subprocess.Popen',
    'requests.get', 'requests.post', 'requests.put', 'requests.delete',
    'requests.patch', 'requests.head', 'requests.request',
    'urllib.request.urlopen', 'urlopen',
    'sqlite3.connect', 'pymysql.connect',
})

# 同步等待：``thread.join()`` / ``event.wait()`` / ``future.result()`` / ``lock.acquire()``
# 这类方法调用没有模块前缀，静态上只能看「方法名 + 接收者名字」。名字里带下面任一
# 关键词才判定为同步等待，避免把 dict.get / list.get 这类同名方法全打成告警。
_WAIT_METHODS = frozenset({'join', 'wait', 'acquire', 'result', 'communicate'})
_WAIT_OBJECT_HINTS = (
    'thread', 'process', 'proc', 'thr', 'event', 'cond',
    'lock', 'sem', 'queue', 'pool', 'executor', 'future', 'fut',
    'task', 'ev', 'lk',
)

_SCOPE_MODULE = 'module'
_SCOPE_ASYNC = 'async_func'

# 这些调用自带 timeout 参数：显式传了超时就不会永久挂住，不算无限阻塞。
# 只覆盖真有该参数的四类；subprocess.Popen 的 timeout 在 wait()/communicate()
# 上而不是构造参数，os.system 根本没有超时概念，因此都不在此列。
_TIMEOUT_AWARE_CALLS = frozenset({
    'subprocess.run', 'subprocess.call', 'subprocess.check_call',
    'subprocess.check_output',
})


def _has_timeout(call: ast.Call) -> bool:
    """调用是否显式传了 timeout=（关键字形式）。"""
    return any(getattr(kw, 'arg', None) == 'timeout' for kw in call.keywords)


def _is_main_guard(test) -> bool:
    """识别 ``if __name__ == '__main__'`` 及其反向写法。

    这类块只在「直接运行该文件」时执行，插件被 import 时根本不会走到，
    因此不能按导入期副作用处理——否则每个带自测入口的插件都会被拒绝加载。
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    names = []
    for side in sides:
        if isinstance(side, ast.Constant) and side.value == '__main__':
            names.append('const')
        elif isinstance(side, ast.Name) and side.id == '__name__':
            names.append('name')
        else:
            return False
    return sorted(names) == ['const', 'name']


class SourceIssue(str):
    """一条静态扫描命中；保持 str 语义便于直接拼进日志。"""

    def __new__(cls, path, node, kind, detail):
        text = (f'{path}:{getattr(node, "lineno", 0)}:'
                f'{getattr(node, "col_offset", 0) + 1} [{kind}] {detail}')
        obj = super().__new__(cls, text)
        obj.path = str(path)
        obj.line = getattr(node, 'lineno', 0)
        obj.column = getattr(node, 'col_offset', 0)
        obj.kind = kind
        obj.detail = detail
        return obj

    def as_dict(self):
        return {
            'path': self.path,
            'line': self.line,
            'column': self.column,
            'kind': self.kind,
            'detail': self.detail,
        }


def _dotted(node):
    """把 Name/Attribute 链还原成点分名字；参数、下标等动态形式返回空串。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ''
    parts.append(node.id)
    return '.'.join(reversed(parts))


def _source_config(config=None):
    if isinstance(config, dict):
        plugins = config.get('plugins')
        if isinstance(plugins, dict) and isinstance(plugins.get('source_scan'), dict):
            return plugins['source_scan']
        return {}
    try:
        # 只读已经加载好的全局配置，不调用 get_config()：扫描发生在启动早期，
        # 不该为了拿开关而再触发一次配置加载/迁移/日志告警。
        from framework import config as config_mod

        current = getattr(config_mod, '_config', None) or {}
        plugins = (current.get('plugins') or {}) if isinstance(current, dict) else {}
        return plugins.get('source_scan') or {}
    except Exception:
        return {}


def should_scan(plugin_name, config=None):
    """是否对该插件做扫描：总开关打开且不在 allowlist 中。"""
    cfg = _source_config(config)
    if not cfg.get('enabled', True):
        return False
    allowlist = cfg.get('allowlist') or []
    if isinstance(allowlist, str):
        allowlist = [allowlist]
    return str(plugin_name) not in {str(item) for item in allowlist}


def _receiver_name(func):
    """取方法调用的接收者名字：``t.join()`` → t，``threading.Thread().join()`` → Thread。"""
    node = func.value
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ''


def _is_sync_wait(call) -> str:
    """同步等待调用返回被调方法名，否则返回空串。

    只做「方法名 + 接收者关键词」两层判断，不认识的对象一律放行——静态阶段
    无法区分 ``q`` 是队列还是 dict，宁可少报也不刷日志。
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return ''
    if func.attr not in _WAIT_METHODS:
        return ''
    if _has_timeout(call) or call.args:
        # 带超时的等待有上限（位置参数即各类 wait/join/result 的 timeout，
        # 各签名位置不同，静态上不细分），不算「无限挂住」。
        return ''
    receiver = _receiver_name(func).lower()
    if not receiver:
        return ''
    for hint in _WAIT_OBJECT_HINTS:
        if hint in receiver:
            return func.attr
    return ''


def _classify(call, scope, func_name=None):
    name = _dotted(call.func)
    if not name:
        return None
    if name in EXIT_CALLS:
        if scope == _SCOPE_MODULE:
            return 'exit', f'模块加载期调用 {name}() 会直接结束进程'
        if scope == _SCOPE_ASYNC:
            # handler 在核心进程的事件循环里跑，一句 os._exit 会带走探针与全部适配器。
            return 'exit_handler', f'async 函数里调用 {name}() 会结束整个框架进程'
        if name in _HARD_EXIT and func_name not in _SCRIPT_ENTRY_NAMES:
            return 'exit_function', f'函数体内调用 {name}() 会在被调用时结束整个框架进程'
        return None
    if name in BLOCKING_CALLS:
        if name in _TIMEOUT_AWARE_CALLS and _has_timeout(call):
            # 有上限的等待不是「永久阻塞」，放行（子进程超时后由调用方处理）。
            return None
        if scope == _SCOPE_MODULE:
            return 'blocking_import', f'模块加载期调用 {name}() 会同步阻塞'
        if scope == _SCOPE_ASYNC:
            return 'blocking_handler', f'async 函数里直接调用 {name}() 会阻塞事件循环'
        return None
    method = _is_sync_wait(call)
    if method:
        if scope == _SCOPE_MODULE:
            return 'blocking_import', f'模块加载期调用 {method}() 会同步等待'
        if scope == _SCOPE_ASYNC:
            return 'blocking_handler', f'async 函数里调用 {method}() 会阻塞事件循环'
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.issues = []

    def visit_Call(self, node):
        # ``await`` 内部的调用是协程调用（``await asyncio.wait_for(ev.wait())``），
        # 由事件循环调度、本来就让出控制权，不能按同步阻塞处理。
        if not getattr(self, '_await_depth', 0):
            hit = _classify(node, getattr(self, '_scope', _SCOPE_MODULE),
                            getattr(self, '_func_name', None))
            if hit:
                kind, detail = hit
                self.issues.append(SourceIssue(self.path, node, kind, detail))
        self.generic_visit(node)

    def visit_Await(self, node):
        self._await_depth = getattr(self, '_await_depth', 0) + 1
        try:
            self.visit(node.value)
        finally:
            self._await_depth -= 1

    def _visit_function(self, node, scope):
        # 装饰器与默认参数在定义时（模块加载期）求值，按当前作用域处理。
        for item in list(node.decorator_list) + list(node.args.defaults):
            self.visit(item)
        for item in node.args.kw_defaults:
            if item is not None:
                self.visit(item)
        # 函数体按新作用域；同步函数体里的阻塞调用不报。
        original = getattr(self, '_scope', _SCOPE_MODULE)
        original_name = getattr(self, '_func_name', None)
        self._scope = scope
        self._func_name = getattr(node, 'name', None)
        for stmt in node.body:
            self.visit(stmt)
        self._scope = original
        self._func_name = original_name

    def visit_If(self, node):
        # __main__ 守卫块：import 期不走，按延迟执行的作用域处理。
        if _is_main_guard(node.test):
            original = getattr(self, '_scope', _SCOPE_MODULE)
            original_name = getattr(self, '_func_name', None)
            self._scope = 'sync_func'
            # 守卫块只在直接运行该文件时执行，允许其中的自测入口自行退出。
            self._func_name = '__main__'
            for stmt in node.body:
                self.visit(stmt)
            self._scope = original
            self._func_name = original_name
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Raise(self, node):
        exc = node.exc
        name = _dotted(exc.func) if isinstance(exc, ast.Call) else _dotted(exc)
        scope = getattr(self, '_scope', _SCOPE_MODULE)
        if name in ('SystemExit', 'builtins.SystemExit'):
            if scope == _SCOPE_MODULE:
                self.issues.append(SourceIssue(
                    self.path, node, 'exit', '模块加载期抛出 SystemExit 会直接结束进程'))
            elif scope == _SCOPE_ASYNC:
                self.issues.append(SourceIssue(
                    self.path, node, 'exit_handler',
                    'async 函数里抛出 SystemExit 会结束整个框架进程'))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._visit_function(node, 'sync_func')

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node, _SCOPE_ASYNC)

    def visit_Lambda(self, node):
        # lambda 体延迟执行，不按模块作用域判定阻塞。
        original = getattr(self, '_scope', _SCOPE_MODULE)
        self._scope = 'sync_func'
        self.visit(node.body)
        self._scope = original

    def visit_ClassDef(self, node):
        # 类体在 import 期执行；方法体由 visit_FunctionDef 单独处理。
        for item in node.decorator_list:
            self.visit(item)
        for stmt in node.body:
            self.visit(stmt)

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            self.visit(child)


def scan_mode(config=None) -> str:
    """命中后的处置策略。

    - ``warn``（默认）：只把命中项写进日志，插件照常加载；
    - ``block``：命中即拒绝加载该插件（旧行为）。

    非法取值一律回落到 ``warn``：扫描的目的是让问题可见，不该因为一个拼错的
    开关把整批插件挡在启动之外。
    """
    mode = str(_source_config(config).get('mode') or 'warn').strip().lower()
    return 'block' if mode == 'block' else 'warn'


def _filter_issues(issues, cfg):
    """``block`` 模式下按细项开关收窄；``warn`` 模式一律全量上报。"""
    if str(cfg.get('mode') or 'warn').strip().lower() != 'block':
        return issues
    if not cfg.get('block_on_import_side_effects', True):
        issues = [i for i in issues if i.kind not in ('exit', 'blocking_import')]
    if not cfg.get('block_on_blocking_handler', True):
        issues = [i for i in issues if i.kind != 'blocking_handler']
    return issues


def scan_file(path, config=None):
    """扫描单个源码文件，返回命中的 SourceIssue 列表。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as e:
        logger.debug('插件源码静态扫描跳过（无法解析 %s）: %s', path, e)
        return []
    visitor = _Visitor(path)
    visitor._scope = _SCOPE_MODULE
    for stmt in tree.body:
        visitor.visit(stmt)
    return _filter_issues(visitor.issues, _source_config(config))


def scan_plugin(plugin_name, main_path, config=None):
    """按插件名/配置决定是否扫描 main.py；跳过时返回空清单。"""
    if not should_scan(plugin_name, config):
        return []
    return scan_file(main_path, config=config)


# 目录遍历时跳过的名字：缓存、虚拟环境、隐藏目录（.git/.venv）与测试夹具。
_SKIP_DIRS = frozenset({'__pycache__', 'node_modules', '.git', '.venv', 'venv', 'site-packages'})


def iter_plugin_mains(root):
    """枚举插件目录下的 ``(插件名, main.py 路径)``。

    只认「子目录/main.py」这一种布局，与加载器的识别口径一致；目录不存在或
    不可读时返回空列表，由调用方决定是否提示。
    """
    out = []
    root = str(root or '')
    if not root or not os.path.isdir(root):
        return out
    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        logger.debug('插件目录无法读取（%s）: %s', root, e)
        return out
    for name in names:
        if name.startswith('.') or name in _SKIP_DIRS:
            continue
        main_path = os.path.join(root, name, 'main.py')
        if os.path.isfile(main_path):
            out.append((name, main_path))
    return out


def scan_tree(root, config=None):
    """扫描整个插件目录，返回 ``(扫描文件数, 命中清单)``。

    与 ``scan_file`` 的区别：不做 allowlist/开关判断，纯体检——启动期预检要的是
    「这个目录里有哪些可疑写法」，是否跳过某个插件由调用方决定。
    """
    issues = []
    entries = iter_plugin_mains(root)
    for _name, main_path in entries:
        issues.extend(scan_file(main_path, config=config))
    return len(entries), issues
