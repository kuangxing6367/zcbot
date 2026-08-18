"""
插件依赖图插件 (plugin_depgraph)
================================
扫描所有插件之间的依赖关系（插件级 + Python 包级），统一存入数据库管理，
提供文本树与图片两种可视化，并检测缺失依赖与循环依赖。

依赖来源（自动扫描）：
1. 插件级：代码中的 sys.modules.get("plugin_<插件名>") / sys.modules["plugin_<插件名>"]
   / from plugin_<插件名> import / import plugin_<插件名>（image_renderer ← help 等）
2. Python 包级：requirements.txt 与 plugin.yaml 的 dependencies.python

数据表 plugin_deps：
  plugin_name  VARCHAR 依赖方插件
  dep_type     VARCHAR  'plugin' | 'python'
  dep_name     VARCHAR 被依赖插件名或 Python 包名
  source_file  VARCHAR 来源文件（仅 plugin 类型）

指令：
  /依赖           文本依赖树（含缺失/循环检测）
  /依赖图         图片版依赖图（image_renderer 渲染）
  /依赖 刷新      立即重新扫描（也可等 60s 自动刷新）
"""
import os
import re
import sys
import time

__plugin_meta__ = {
    "name": "插件依赖图",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "扫描插件间依赖关系（DB 统一管理），/依赖 文本树 /依赖图 图片",
    "priority": 10,
}

_DEP_TABLE = "plugin_deps"
_SCAN_TTL = 60  # 扫描结果缓存秒数
_last_scan = 0
_PLUGIN_DIR = None

# 插件引用匹配：sys.modules.get("plugin_<插件名>") / sys.modules["plugin_<插件名>"]
#            / from plugin_<插件名> import y / import plugin_<插件名>
_RE_MODULES_GET = re.compile(
    r"sys\.modules(?:\s*\.get)?\s*\(?\s*[\"']plugin_([A-Za-z0-9_]+)[\"']")
_RE_SYS_MODULES = re.compile(r"sys\.modules\s*\[\s*[\"']plugin_([A-Za-z0-9_]+)[\"']")
_RE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+plugin_([A-Za-z0-9_]+)(?:\s+import\s+[A-Za-z0-9_]+)?\b",
    re.MULTILINE)
_RE_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def register(ctx):
    global _PLUGIN_DIR
    _PLUGIN_DIR = ctx._framework.plugin_loader.plugins_dir
    ctx.command("/依赖", handle_deps, priority=10,
                description="查看插件依赖树（/依赖 刷新 重新扫描）")
    ctx.command("/依赖图", handle_deps_image, priority=10,
                description="生成插件依赖关系图片")
    ctx.task("*/1 * * * *", _scan_task, description="刷新插件依赖扫描")
    _ensure_table()
    ctx.log("[plugin_depgraph] 插件依赖图已就绪")


# ---------------- 数据库 ----------------

def _ensure_table():
    try:
        ctx.db_execute(
            "CREATE TABLE IF NOT EXISTS " + _DEP_TABLE + " ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "plugin_name VARCHAR(64) NOT NULL, "
            "dep_type VARCHAR(16) NOT NULL, "
            "dep_name VARCHAR(128) NOT NULL, "
            "source_file VARCHAR(255) DEFAULT '', "
            "UNIQUE(plugin_name, dep_type, dep_name))")
    except Exception as e:
        ctx.log(f"[plugin_depgraph] 建表失败: {e}", level="error")


def _scan_task():
    """定时刷新扫描（防内存/DB 膨胀：每次全量重建）"""
    try:
        _scan_all(force=True)
    except Exception:
        pass


# ---------------- 扫描 ----------------

def _plugin_files(plugin_name: str):
    """插件目录下所有 .py 文件路径"""
    base = os.path.join(_PLUGIN_DIR, plugin_name)
    files = []
    if not os.path.isdir(base):
        return files
    for root, _dirs, fnames in os.walk(base):
        for fn in fnames:
            if fn.endswith(".py"):
                files.append(os.path.join(root, fn))
    return files


def _scan_one(plugin_name: str):
    """扫描单个插件，返回 {plugin: set, python: set}"""
    plugin_deps = set()
    python_deps = set()
    for fp in _plugin_files(plugin_name):
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except Exception:
            continue
        # 跳过扫描器自身的正则定义行（避免自匹配误报）
        src_lines = [ln for ln in src.split("\n") if "re.compile" not in ln]
        src = "\n".join(src_lines)
        for rx in (_RE_MODULES_GET, _RE_SYS_MODULES, _RE_IMPORT):
            for m in rx.finditer(src):
                dep = m.group(1)
                if dep and dep != plugin_name:
                    plugin_deps.add(dep)
    # requirements.txt
    req = os.path.join(_PLUGIN_DIR, plugin_name, "requirements.txt")
    if os.path.isfile(req):
        try:
            with open(req, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = _RE_REQ.match(line)
                    if m:
                        python_deps.add(m.group(1))
        except Exception:
            pass
    # plugin.yaml dependencies.python
    yaml_file = os.path.join(_PLUGIN_DIR, plugin_name, "plugin.yaml")
    if os.path.isfile(yaml_file):
        try:
            import yaml as _yaml
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            for d in (data.get("dependencies") or {}).get("python", []) or []:
                if isinstance(d, str):
                    m = _RE_REQ.match(d.strip())
                    if m:
                        python_deps.add(m.group(1))
        except Exception:
            pass
    return {"plugin": plugin_deps, "python": python_deps}


def _scan_all(force=False):
    """扫描全部插件并写入 DB（全量重建，幂等）"""
    global _last_scan
    now = time.time()
    if not force and (now - _last_scan) < _SCAN_TTL:
        return
    try:
        plugin_names = sorted(
            d for d in os.listdir(_PLUGIN_DIR)
            if os.path.isdir(os.path.join(_PLUGIN_DIR, d)) and not d.startswith("_")
        )
        ctx.db_execute("DELETE FROM " + _DEP_TABLE)
        for name in plugin_names:
            deps = _scan_one(name)
            for dep in sorted(deps["plugin"]):
                ctx.db_insert(
                    "INSERT INTO " + _DEP_TABLE
                    + " (plugin_name, dep_type, dep_name) VALUES (%s, 'plugin', %s)",
                    (name, dep))
            for dep in sorted(deps["python"]):
                ctx.db_insert(
                    "INSERT INTO " + _DEP_TABLE
                    + " (plugin_name, dep_type, dep_name) VALUES (%s, 'python', %s)",
                    (name, dep))
        _last_scan = now
    except Exception as e:
        ctx.log(f"[plugin_depgraph] 扫描失败: {e}", level="error")


def _load_deps():
    """从 DB 读取依赖关系：{plugin: {"plugin": set, "python": set}}"""
    _scan_all()
    rows = ctx.db_query(
        "SELECT plugin_name, dep_type, dep_name FROM " + _DEP_TABLE + " ORDER BY plugin_name", ()) or []
    out = {}
    for r in rows:
        p = r["plugin_name"]
        out.setdefault(p, {"plugin": set(), "python": set()})
        out[p][r["dep_type"]].add(r["dep_name"])
    return out


def _detect_issues(deps: dict):
    """检测缺失依赖与循环依赖"""
    all_plugins = set(deps.keys())
    missing = []
    for p, d in deps.items():
        for dep in d["plugin"]:
            if dep not in all_plugins:
                missing.append((p, dep))
    # 循环依赖检测（DFS 三色标记）
    cycles = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {p: WHITE for p in all_plugins}
    stack = []

    def dfs(node):
        color[node] = GRAY
        stack.append(node)
        for dep in deps.get(node, {}).get("plugin", set()):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                idx = stack.index(dep)
                cycle = stack[idx:] + [dep]
                if cycle not in cycles:
                    cycles.append(cycle)
            elif color[dep] == WHITE:
                dfs(dep)
        stack.pop()
        color[node] = BLACK

    for p in all_plugins:
        if color[p] == WHITE:
            dfs(p)
    return missing, cycles


# ---------------- 文本树 ----------------

def handle_deps(event, match):
    """查看插件依赖树"""
    try:
        text = (event.message or "").strip()
        if "刷新" in text:
            _scan_all(force=True)
            ctx.log("[plugin_depgraph] 手动触发重新扫描完成")
        deps = _load_deps()
        missing, cycles = _detect_issues(deps)
        lines = ["🔗 插件依赖关系:"]
        if not deps:
            lines.append("（无插件数据）")
        for p in sorted(deps):
            d = deps[p]
            bits = []
            if d["plugin"]:
                bits.append("插件: " + ", ".join(sorted(d["plugin"])))
            if d["python"]:
                bits.append("Py: " + ", ".join(sorted(d["python"]))[:120])
            lines.append(f"• {p}" + ((" — " + "；".join(bits)) if bits else ""))
        if missing:
            lines.append("\n⚠️ 缺失插件依赖:")
            for p, dep in missing:
                lines.append(f"  {p} → 引用不存在的插件「{dep}」")
        if cycles:
            lines.append("\n🔄 循环依赖:")
            for c in cycles:
                lines.append("  " + " → ".join(c))
        if not missing and not cycles:
            lines.append("\n✅ 未发现缺失/循环依赖")
        lines.append("\n/依赖图 查看图片版")
        ctx.api("send_msg",
                user_id=event.user_id,
                group_id=event.group_id if event.is_group else None,
                message="\n".join(lines))
    except Exception as e:
        ctx.log(f"[plugin_depgraph] /依赖 失败: {e}", level="error")


# ---------------- 图片版 ----------------

def handle_deps_image(event, match):
    """生成插件依赖图图片"""
    try:
        deps = _load_deps()
        missing, cycles = _detect_issues(deps)
        png = _draw_dep_image(deps, missing, cycles)
        if png is None:
            ctx.api("send_msg",
                    user_id=event.user_id,
                    group_id=event.group_id if event.is_group else None,
                    message="图片渲染失败（image_renderer 未加载？）")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png)
            path_str = tmp.name.replace("\\", "/")
        try:
            ctx.api("send_msg",
                    user_id=event.user_id,
                    group_id=event.group_id if event.is_group else None,
                    message=f"[CQ:image,file=file:///{path_str}]")
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    except Exception as e:
        ctx.log(f"[plugin_depgraph] /依赖图 失败: {e}", level="error")


def _draw_dep_image(deps: dict, missing, cycles):
    """用 image_renderer 画依赖图：被依赖的基础插件在左，消费者在右"""
    mod = sys.modules.get("plugin_image_renderer")
    if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
        return None
    if not deps:
        return None
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "help", "DouyinSansBold.otf")
    font_path = fp if os.path.isfile(fp) else None

    # 拓扑分层：level[p] = max(level[dep]+1)，无依赖的为 0（基础层在左）
    level = {}
    for _ in range(len(deps) + 1):
        changed = False
        for p in deps:
            d = deps[p]["plugin"]
            if not d:
                if level.get(p, 0) == 0:
                    changed = True
            else:
                lv = max((level.get(dep, 0) + 1) for dep in d if dep in deps)
                if level.get(p, 0) != lv:
                    level[p] = lv
                    changed = True
        if not changed:
            break
    for p in deps:
        level.setdefault(p, 0)

    max_level = max(level.values()) if level else 0
    col_w, row_h = 190, 46
    pad_x, pad_y = 30, 26
    header_h = 44
    width = pad_x * 2 + (max_level + 1) * col_w
    rows_per_col = {}
    for p in deps:
        rows_per_col[level[p]] = rows_per_col.get(level[p], 0) + 1
    max_rows = max(rows_per_col.values()) if rows_per_col else 1
    height = header_h + pad_y * 2 + max_rows * row_h + 30

    canvas = mod._get_native_or_pil_canvas(width, height, None, font_path)
    canvas.rect(0, 0, width, height, radius=0, fill="#F6F8FB")

    canvas.text(pad_x, 12, f"插件依赖图（{len(deps)} 个插件）", font_size=20, color="#14325C")
    if missing:
        canvas.text(pad_x + 260, 14, f"⚠ {len(missing)} 缺失", font_size=15, color="#C0392B")
    if cycles:
        canvas.text(pad_x + 380, 14, f"🔄 {len(cycles)} 循环", font_size=15, color="#B7950B")

    col_y = {}
    for lv, cnt in rows_per_col.items():
        col_y[lv] = header_h + pad_y + max(0, (max_rows - cnt) * row_h // 2)

    pos = {}
    for lv in range(max_level + 1):
        y = col_y.get(lv, header_h + pad_y)
        for p in sorted(deps):
            if level.get(p) != lv:
                continue
            pos[p] = (pad_x + lv * col_w, y)
            y += row_h

    # 画箭头线（先线后节点）
    for p, d in deps.items():
        if p not in pos:
            continue
        x1, y1 = pos[p]
        for dep in d["plugin"]:
            if dep not in pos or x1 <= pos[dep][0]:
                continue
            x0, y0 = pos[dep]
            mid_x = (x0 + x1) // 2
            canvas.rect(mid_x - 1, y0 + 22, mid_x + 2, y1 + 22, radius=0, fill="#7FA8D9")
            canvas.rect(x0 + 22, y1 + 21, mid_x + 1, y1 + 24, radius=0, fill="#7FA8D9")
            canvas.rect(mid_x - 1, y0 + 21, mid_x + 2, y0 + 25, radius=0, fill="#7FA8D9")

    # 节点
    for p, (x, y) in pos.items():
        d = deps.get(p, {"plugin": set()})
        is_leaf = not d["plugin"]
        fill = "#DCEBFA" if is_leaf else "#FFFFFF"
        outline = "#4A90D9" if not is_leaf else "#7FA8D9"
        canvas.rect(x, y, x + col_w - 14, y + row_h - 14, radius=8, fill=fill, outline=outline, width=1)
        canvas.text(x + 10, y + 6, p, font_size=15, color="#14325C")
        py_deps = d.get("python", set())
        if py_deps:
            canvas.text(x + 10, y + 26, "Py: " + ", ".join(sorted(py_deps))[:24],
                        font_size=10, color="#7A8A9E")

    return canvas.to_png()
