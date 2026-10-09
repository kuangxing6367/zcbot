# -*- coding: utf-8 -*-
"""工作区工具集（智能体的「手」）。

工具集与语义，全部以工作区根目录为边界，越界一律拒绝：

  read / list / glob / grep     只读
  write / edit                  写入（workspace / full 模式）
  shell                         执行（仅 full 模式且 allow_exec=True）

权限模型（「能力 × 模式」的静态闸门）：
每个工具声明自己需要的能力（read / write / exec），由 ToolRegistry 依据
mode 推导当前允许的能力集合，调用前强制校验；越权直接抛 PermissionDenied，
不会因为模型「点名」就放行。specs() 也只暴露当前允许的工具。

edit 语义：oldString 必须精确命中且默认唯一，newString 必须与
oldString 不同；命中多处需显式 replaceAll=True。匹配失败时做一次归一化探测
（弯引号 / 破折号 / 不间断空格），给出更明确的报错，减少模型反复试错。
"""
import fnmatch
import os
import re
import subprocess

# 能力常量
CAP_READ = 'read'
CAP_WRITE = 'write'
CAP_EXEC = 'exec'


class ToolError(Exception):
    """工具执行错误（会作为 tool 结果回灌给模型）"""


class PermissionDenied(ToolError):
    """权限不足（当前模式不允许该工具）"""


def _normalize(value: str) -> str:
    """匹配前归一化：弯引号 / 破折号 / 特殊空白 → ASCII 等价物。"""
    return (value
            .replace('\u2018', "'").replace('\u2019', "'")
            .replace('\u201c', '"').replace('\u201d', '"')
            .replace('\u2013', '-').replace('\u2014', '-')
            .replace('\u00a0', ' ').replace('\u3000', ' '))


class ToolRegistry:
    """工作区工具注册表 + 执行器"""

    # 工具名 -> 需要的能力
    _CAPS = {
        'read': CAP_READ, 'list': CAP_READ, 'glob': CAP_READ, 'grep': CAP_READ,
        'webfetch': CAP_READ, 'websearch': CAP_READ,
        'db_query': CAP_READ, 'plugin_list': CAP_READ,
        'write': CAP_WRITE, 'edit': CAP_WRITE, 'remember': CAP_WRITE, 'plugin_new': CAP_WRITE,
        'shell': CAP_EXEC, 'python': CAP_EXEC,
        'db_execute': CAP_EXEC, 'plugin_action': CAP_EXEC,
    }

    def __init__(self, root, mode='workspace', allow_exec=False,
                 max_output=8000, shell_timeout=60, host=None,
                 python_timeout=60, net_timeout=20):
        self.root = os.path.abspath(root)
        self.mode = mode
        self.allow_exec = bool(allow_exec)
        self.max_output = int(max_output)
        self.shell_timeout = float(shell_timeout)
        self.python_timeout = float(python_timeout)
        self.net_timeout = float(net_timeout)
        # 宿主桥：提供 db / 插件管理等框架侧能力（独立 CLI 下可为 None）
        self.host = host

    # ── 权限 ───────────────────────────────────────────────────────────
    def allowed_caps(self) -> set:
        caps = {CAP_READ}
        if self.mode in ('workspace', 'full'):
            caps.add(CAP_WRITE)
        if self.mode == 'full' and self.allow_exec:
            caps.add(CAP_EXEC)
        return caps

    def can(self, tool: str) -> bool:
        cap = self._CAPS.get(tool)
        return cap is not None and cap in self.allowed_caps()

    # ── 路径边界 ───────────────────────────────────────────────────────
    def _resolve(self, path):
        raw = path or ''
        p = os.path.abspath(os.path.join(self.root, raw))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ToolError("路径越界（超出工作区）: %s" % path)
        return p

    # ── 工具清单（给模型的 JSON Schema）────────────────────────────────
    def specs(self):
        """只暴露当前模式允许的工具——模型看不到越权工具，避免无谓尝试。"""
        all_specs = {
            'read': {
                "name": "read",
                "description": "读取工作区内文本文件，返回带行号的内容。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "offset": {"type": "integer", "description": "起始行，默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 400"},
                }, "required": ["path"]},
            },
            'list': {
                "name": "list",
                "description": "列出目录内容（最多两层）。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "子目录，默认工作区根"},
                }, "required": []},
            },
            'glob': {
                "name": "glob",
                "description": "按 glob 模式匹配工作区内文件路径（如 '**/*.py'）。",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                    "path": {"type": "string", "description": "限定子目录，可选"},
                }, "required": ["pattern"]},
            },
            'grep': {
                "name": "grep",
                "description": "按正则搜索工作区内文本，返回匹配行（文件:行号:内容）。",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "限定子目录，可选"},
                    "include": {"type": "string", "description": "只搜匹配该 glob 的文件，可选"},
                }, "required": ["pattern"]},
            },
            'webfetch': {
                "name": "webfetch",
                "description": "抓取一个 http/https 链接的内容（截断返回）。",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string", "description": "完整 URL"},
                }, "required": ["url"]},
            },
            'websearch': {
                "name": "websearch",
                "description": "网页搜索（DuckDuckGo），返回标题与链接。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                }, "required": ["query"]},
            },
            'db_query': {
                "name": "db_query",
                "description": "只读查询 ZCBOT 数据库（仅允许 SELECT/PRAGMA/WITH/EXPLAIN 等），返回行。",
                "parameters": {"type": "object", "properties": {
                    "sql": {"type": "string", "description": "SQL（只读）"},
                    "limit": {"type": "integer", "description": "最多返回行数，默认 50"},
                }, "required": ["sql"]},
            },
            'plugin_list': {
                "name": "plugin_list",
                "description": "列出已加载的插件（官方 + 用户）。",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            'write': {
                "name": "write",
                "description": "写入文件（覆盖整个文件，父目录自动创建）。创建新文件或整体重写用它；局部改动优先用 edit。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                }, "required": ["path", "content"]},
            },
            'edit': {
                "name": "edit",
                "description": ("对已有文本文件做定点替换：把 oldString 精确替换为 newString。"
                                "oldString 必须精确命中且默认只允许出现一次；出现多次时需补足上下文，"
                                "或置 replaceAll=true 替换全部。newString 必须与 oldString 不同。"),
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "oldString": {"type": "string", "description": "要被替换的原文"},
                    "newString": {"type": "string", "description": "替换后的文本（须与 oldString 不同）"},
                    "replaceAll": {"type": "boolean", "description": "是否替换全部出现，默认 false"},
                }, "required": ["path", "oldString", "newString"]},
            },
            'remember': {
                "name": "remember",
                "description": ("把一条值得跨会话保留的事实/偏好/结论记入长期记忆（memory.md），"
                                "之后每轮都会注入系统提示。适合记「用户偏好、项目约定、踩过的坑」，"
                                "不要记临时信息。"),
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "要记住的一句话"},
                }, "required": ["text"]},
            },
            'plugin_new': {
                "name": "plugin_new",
                "description": ("在 `plugins/<名>/` 生成一个 ZCBOT 插件骨架（main.py + README.md）。"
                                "名字全小写下划线。之后用 write/edit 填实现。"),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "插件名（全小写下划线）"},
                }, "required": ["name"]},
            },
            'shell': {
                "name": "shell",
                "description": "在工作区内执行 shell 命令，返回标准输出与错误。高风险，仅在 full 模式开放。",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "workdir": {"type": "string", "description": "相对工作区的工作目录，可选"},
                }, "required": ["command"]},
            },
            'python': {
                "name": "python",
                "description": "执行一段 Python 代码（子进程），返回 stdout/stderr。高风险，仅 full 模式开放。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"},
                }, "required": ["code"]},
            },
            'db_execute': {
                "name": "db_execute",
                "description": "对 ZCBOT 数据库执行写语句（INSERT/UPDATE/DELETE/DDL），返回影响行数。高风险，仅 full 模式开放。",
                "parameters": {"type": "object", "properties": {
                    "sql": {"type": "string", "description": "SQL 写语句"},
                }, "required": ["sql"]},
            },
            'plugin_action': {
                "name": "plugin_action",
                "description": "管理插件：action 为 enable / disable / reload，name 为插件名。高风险，仅 full 模式开放。",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "description": "enable | disable | reload"},
                    "name": {"type": "string", "description": "插件名"},
                }, "required": ["action", "name"]},
            },
        }
        return [spec for name, spec in all_specs.items() if self.can(name)]

    # ── 执行 ───────────────────────────────────────────────────────────
    def call(self, name, args):
        fn = getattr(self, '_t_' + name, None)
        if fn is None:
            raise ToolError("未知工具：%s" % name)
        if not self.can(name):
            raise PermissionDenied(
                "工具 [%s] 在当前模式（mode=%s, allow_exec=%s）下不可用"
                % (name, self.mode, self.allow_exec))
        return fn(args or {})

    def _clip(self, text: str) -> str:
        text = text or ''
        if len(text) <= self.max_output:
            return text
        head = self.max_output // 2
        tail = self.max_output - head - 40
        return "%s\n…[已截断，共 %d 字符]…\n%s" % (text[:head], len(text), text[-tail:])

    # ---- 只读 ----
    def _t_read(self, a):
        if not a.get('path'):
            raise ToolError("read 需要 path")
        p = self._resolve(a['path'])
        if os.path.isdir(p):
            return self._t_list({'path': a['path']})
        if not os.path.isfile(p):
            raise ToolError("文件不存在：%s" % a['path'])
        offset = max(1, int(a.get('offset', 1) or 1))
        limit = int(a.get('limit', 400) or 400)
        with open(p, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        sel = lines[offset - 1: offset - 1 + limit]
        out = "".join("%d\t%s" % (offset + i, ln) for i, ln in enumerate(sel))
        return self._clip(out) if out else "（空文件）"

    def _t_list(self, a):
        base = self._resolve(a.get('path') or '.')
        if not os.path.isdir(base):
            raise ToolError("目录不存在：%s" % (a.get('path') or '.'))
        out = []
        for root, dirs, files in os.walk(base):
            depth = root[len(self.root):].count(os.sep)
            if depth >= 2:
                dirs[:] = []
                continue
            rel = os.path.relpath(root, self.root)
            out.append("%s/" % ('.' if rel == '.' else rel))
            for fn in sorted(files)[:100]:
                out.append("  %s" % fn)
        return "\n".join(out) if out else "（空）"

    def _t_glob(self, a):
        pattern = a.get('pattern')
        if not pattern:
            raise ToolError("glob 需要 pattern")
        base = self._resolve(a.get('path') or '.')
        hits = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules')]
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), self.root)
                if fnmatch.fnmatch(rel.replace(os.sep, '/'), pattern) or \
                        fnmatch.fnmatch(fn, pattern):
                    hits.append(rel)
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n…(更多结果已省略)"
        return "\n".join(hits) if hits else "无匹配"

    def _t_grep(self, a):
        pattern = a.get('pattern')
        if not pattern:
            raise ToolError("grep 需要 pattern")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError("正则非法：%s" % e)
        include = a.get('include')
        base = self._resolve(a.get('path') or '.')
        hits = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules')]
            for fn in files:
                if include and not fnmatch.fnmatch(fn, include):
                    continue
                fp = os.path.join(root, fn)
                try:
                    with open(fp, encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                rel = os.path.relpath(fp, self.root)
                                hits.append("%s:%d: %s" % (rel, i, line.rstrip()[:300]))
                                if len(hits) >= 200:
                                    return "\n".join(hits) + "\n…(更多结果已省略)"
                except OSError:
                    continue
        return "\n".join(hits) if hits else "无匹配"

    # ---- 写入 ----
    def _t_write(self, a):
        if 'path' not in a:
            raise ToolError("write 需要 path")
        p = self._resolve(a['path'])
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        content = a.get('content', '')
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        return "已写入 %s（%d 字节）" % (a['path'], len(content.encode('utf-8')))

    def _t_edit(self, a):
        path = a.get('path')
        old = a.get('oldString')
        new = a.get('newString')
        if path is None or old is None or new is None:
            raise ToolError("edit 需要 path / oldString / newString")
        if old == new:
            raise ToolError("oldString 与 newString 必须不同")
        p = self._resolve(path)
        if not os.path.isfile(p):
            raise ToolError("文件不存在：%s" % path)
        with open(p, encoding='utf-8', errors='replace') as f:
            content = f.read()

        replace_all = bool(a.get('replaceAll'))
        count = content.count(old)
        if count == 0:
            # 归一化后探测（弯引号/破折号/空白差异），给出更明确的报错
            if old and _normalize(old) in _normalize(content):
                raise ToolError(
                    "oldString 未精确命中（检测到排版字符差异，请按文件实际字符重写）")
            raise ToolError("oldString 未在文件中找到")
        if count > 1 and not replace_all:
            raise ToolError(
                "oldString 命中 %d 处，需补足上下文使其唯一，或置 replaceAll=true" % count)
        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(new_content)
        return "已编辑 %s（替换 %d 处）" % (path, count if replace_all else 1)

    # ---- 执行 ----
    def _t_shell(self, a):
        cmd = a.get('command')
        if not cmd:
            raise ToolError("shell 需要 command")
        cwd = self._resolve(a.get('workdir') or '.')
        if not os.path.isdir(cwd):
            cwd = self.root
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace',
                                  timeout=self.shell_timeout)
        except subprocess.TimeoutExpired:
            return "命令超时（>%ss），已终止" % self.shell_timeout
        except Exception as e:
            raise ToolError("执行异常：%s" % e)
        out = (proc.stdout or '') + (proc.stderr or '')
        prefix = "[退出码 %d]\n" % proc.returncode if proc.returncode else ""
        return prefix + (self._clip(out) if out.strip() else "（无输出）")

    def _t_python(self, a):
        code = a.get('code')
        if not code:
            raise ToolError("python 需要 code")
        import sys
        try:
            proc = subprocess.run([sys.executable, '-c', code], cwd=self.root,
                                  capture_output=True, text=True,
                                  encoding='utf-8', errors='replace',
                                  timeout=self.python_timeout)
        except subprocess.TimeoutExpired:
            return "执行超时（>%ss），已终止" % self.python_timeout
        except Exception as e:
            raise ToolError("执行异常：%s" % e)
        out = (proc.stdout or '') + (proc.stderr or '')
        prefix = "[退出码 %d]\n" % proc.returncode if proc.returncode else ""
        return prefix + (self._clip(out) if out.strip() else "（无输出）")

    # ---- 网络 ----
    def _t_webfetch(self, a):
        url = (a.get('url') or '').strip()
        if not url:
            raise ToolError("webfetch 需要 url")
        if not url.startswith(('http://', 'https://')):
            raise ToolError("只支持 http/https 链接")
        import html as _html
        import re as _re
        import urllib.request
        req = urllib.request.Request(url, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120 Safari/537.36'),
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        try:
            with urllib.request.urlopen(req, timeout=self.net_timeout) as resp:
                raw = resp.read(self.max_output * 6)
                charset = resp.headers.get_content_charset() or 'utf-8'
                ctype = (resp.headers.get_content_type() or '').lower()
        except Exception as e:  # noqa: BLE001
            raise ToolError("抓取失败：%s" % e)
        try:
            text = raw.decode(charset, 'replace')
        except Exception:
            text = raw.decode('utf-8', 'replace')
        if 'html' in ctype:
            # 粗略转文本：脚本/样式整块去掉，其余标签换空格，实体解码
            text = _re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', text)
            text = _re.sub(r'(?s)<[^>]+>', ' ', text)
            text = _html.unescape(text)
            text = _re.sub(r'[ \t\r\f\v]+', ' ', text)
            text = _re.sub(r'\n\s*\n+', '\n\n', text)
        return self._clip(text.strip())

    def _t_websearch(self, a):
        """联网搜索：默认走免 key 的 Bing（cn.bing.com，国内可达、零配置）。

        解析 <li class="b_algo"> 结果块（<h2><a href>标题</a></h2> + <p>摘要</p>），
        去掉 Bing 的追踪参数。可选：环境变量 FIRECRAWL_API_KEY 存在时优先用
        Firecrawl /v1/search（结果更干净）。
        """
        q = (a.get('query') or '').strip()
        if not q:
            raise ToolError("websearch 需要 query")
        limit = int(a.get('max_results', 5) or 5)
        limit = max(1, min(10, limit))
        key = os.environ.get('FIRECRAWL_API_KEY', '').strip()
        if key:
            try:
                return self._firecrawl_search(q, limit, key)
            except Exception:  # 降级到免 key Bing，不把「请配 key」甩给模型
                pass
        return self._bing_search(q, limit)

    def _bing_search(self, query, limit):
        import html as _html
        import re as _re
        import urllib.parse
        import urllib.request
        url = 'https://cn.bing.com/search?q=' + urllib.parse.quote(query) + '&mkt=zh-CN'
        req = urllib.request.Request(url, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/120 Safari/537.36'),
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept': 'text/html',
        })
        try:
            with urllib.request.urlopen(req, timeout=self.net_timeout) as resp:
                data = resp.read(4 << 20).decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001
            raise ToolError("联网搜索失败：%s" % e)
        out, rest = [], data
        while len(out) < limit:
            i = rest.find('<li class="b_algo"')
            if i < 0:
                break
            j = rest.find('</li>', i)
            if j < 0:
                break
            block, rest = rest[i:j], rest[j + 5:]
            m = _re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, _re.S)
            if not m:
                continue
            link = _html.unescape(m.group(1))
            title = _html.unescape(_re.sub(r'<[^>]+>', '', m.group(2))).strip()
            sn = _re.search(r'<p[^>]*>(.*?)</p>', block, _re.S)
            snippet = _html.unescape(_re.sub(r'<[^>]+>', '', sn.group(1))).strip() if sn else ''
            link = _re.sub(r'[?&](mkt|pc|form|qpvt|qs|sk|sp|sc|ensearch|filters)=[^&]*', '', link)
            out.append('%d. %s\n   %s%s' % (len(out) + 1, title, link,
                                            ('\n   ' + snippet) if snippet else ''))
        if not out:
            return "没有找到结果（可换个说法再试）"
        return self._clip('\n'.join(out))

    def _firecrawl_search(self, query, limit, key):
        import json
        import urllib.request
        body = json.dumps({'query': query, 'limit': limit}).encode('utf-8')
        req = urllib.request.Request(
            'https://api.firecrawl.dev/v1/search', data=body, method='POST',
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(req, timeout=self.net_timeout + 10) as resp:
            obj = json.loads(resp.read(1 << 20).decode('utf-8', 'replace'))
        rows = obj.get('data') if isinstance(obj, dict) else None
        if not isinstance(rows, list) or not rows:
            return "没有找到结果（可换个说法再试）"
        out = []
        for r in rows[:limit]:
            out.append('%d. %s\n   %s%s' % (
                len(out) + 1, r.get('title', ''), r.get('url', ''),
                ('\n   ' + str(r.get('description', ''))[:400]) if r.get('description') else ''))
        return self._clip('\n'.join(out))

    # ---- 数据库 / 插件（宿主桥）----
    def _host(self):
        if self.host is None:
            raise ToolError("当前环境没有框架宿主（独立 CLI 模式），该工具不可用")
        return self.host

    def _t_db_query(self, a):
        sql = (a.get('sql') or '').strip()
        if not sql:
            raise ToolError("db_query 需要 sql")
        head = sql.lower().lstrip('(').lstrip()
        if not head.startswith(('select', 'pragma', 'with', 'explain', 'show', 'desc')):
            raise ToolError("db_query 只允许只读语句（SELECT/PRAGMA/WITH/EXPLAIN/SHOW/DESC）")
        try:
            rows = self._host().db_query(sql)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError("查询失败：%s" % e)
        limit = int(a.get('limit', 50) or 50)
        rows = list(rows or [])[:limit]
        if not rows:
            return "（0 行）"
        return self._clip("\n".join(str(r) for r in rows))

    def _t_db_execute(self, a):
        sql = (a.get('sql') or '').strip()
        if not sql:
            raise ToolError("db_execute 需要 sql")
        try:
            n = self._host().db_execute(sql)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError("执行失败：%s" % e)
        return "已执行，影响 %s 行" % n

    def _t_plugin_list(self, a):
        names = self._host().plugins()
        return "\n".join(names) if names else "（无插件）"

    def _t_plugin_action(self, a):
        action = (a.get('action') or '').strip().lower()
        name = (a.get('name') or '').strip()
        if action not in ('enable', 'disable', 'reload'):
            raise ToolError("action 必须是 enable / disable / reload")
        if not name:
            raise ToolError("plugin_action 需要 name")
        return self._host().plugin_action(action, name) or "已执行"

    def _t_remember(self, a):
        text = (a.get('text') or '').strip()
        if not text:
            raise ToolError("remember 需要 text")
        return self._host().memory_add(text) or "已记住"

    def _t_plugin_new(self, a):
        name = (a.get('name') or '').strip()
        if not name:
            raise ToolError("plugin_new 需要 name")
        ok, msg = self._host().plugin_new(name)
        if not ok:
            raise ToolError(msg)
        return msg
