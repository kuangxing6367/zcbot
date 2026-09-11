# -*- coding: utf-8 -*-
"""
Web 应用核心：Flask app 构建 + 共享基础设施 + WebServer

原 framework/apis.py 的 create_web_app 是一个 4600 行的上帝函数，把所有
鉴权/CORS/限速/辅助函数/约 180 个端点全塞在一个闭包里。这里把它拆成：

    webapp.py     本文件：Flask app + 鉴权装饰器 + CORS/限速/黑名单 + 全部共享辅助函数
                  + WebServer，并把共享上下文（ctx）分发给各功能域模块
    auth.py       登录 / 登出 / 当前用户 / 改密 / 双请求认证 / 安全黑名单
    admins.py     管理员增删查
    apikeys.py    接口令牌（API Key）管理
    dashboard.py  仪表盘 + WebUI 扩展 + 群级插件开关
    plugins.py    插件管理 / 插件市场 / 插件配置 / 依赖图
    commands.py   静态/动态命令 + 关键词自动回复
    users_groups.py 用户 / 群管理
    tasks.py      定时任务
    logs.py       审计日志 + 运行日志（SSE）
    config.py     系统配置 / OneBot 连接 / 运行状态 / config.yaml
    db_gateway.py 内嵌数据库管理
    framework_ops.py 版本 / 框架更新 / 重启 / 终端
    webui.py      插件 WebUI 内嵌
    files.py      文件浏览器
    stats.py      统计图表 + 环境信息
    perm_api.py   权限系统管理接口
    static_routes.py 前端静态文件
    apikeys 之外的接口令牌见 apikeys.py

向后兼容：framework/apis.py 仍导出 create_web_app / WebServer。
"""
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace

import requests
import yaml
from flask import Flask, Response, jsonify, request

from framework.dual_auth import DualRequestAuthSystem
from framework.log_broker import log_broker

logger = logging.getLogger('zcbot')

# 项目根目录（framework/api/webapp.py 向上三级：api → framework → 根目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def create_web_app(framework) -> Flask:
    """
    创建 Flask 应用并注册所有路由
    :param framework: Framework 实例
    """
    app = Flask(__name__, static_folder=None)
    app._framework = framework
    web_cfg = framework.config.get('web', {})

    db = framework.db
    plugins_dir = framework.plugin_loader.plugins_dir

    # ---- 跨域支持（自定义网页 / 第三方面板跨源调用 API 用）----
    _cors_cfg = (
        framework.config.get('security', {}).get('cors_allowed_origins')
        or framework.config.get('web', {}).get('cors_allowed_origins')
    )

    def _resolve_cors_origin():
        origin = request.headers.get('Origin')
        if not origin:
            return None
        if _cors_cfg:
            if isinstance(_cors_cfg, str):
                allowed = [o.strip() for o in _cors_cfg.split(',') if o.strip()]
            else:
                allowed = [str(o).strip() for o in _cors_cfg]
            return origin if origin in allowed else None
        return origin

    @app.after_request
    def _cors_headers(resp):
        origin = _resolve_cors_origin()
        if not origin:
            return resp
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Access-Control-Allow-Headers'] = \
            'Content-Type, Authorization, X-Requested-With'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Vary'] = 'Origin'
        return resp

    @app.before_request
    def _cors_preflight():
        if request.method == 'OPTIONS':
            return ('', 204)

    # ---- 登录防爆破（内存限速：同一 IP 10 分钟内最多失败 5 次）----
    _login_failures = {}  # ip -> list[timestamp]
    _login_lock = threading.Lock()

    def _check_login_rate(ip: str) -> bool:
        now = time.time()
        with _login_lock:
            ts_list = [t for t in _login_failures.get(ip, []) if now - t < 600]
            return len(ts_list) < 5

    def _record_login_failure(ip: str):
        now = time.time()
        with _login_lock:
            _login_failures.setdefault(ip, []).append(now)
            _login_failures[ip] = [t for t in _login_failures[ip] if now - t < 600]

    def _clear_login_failures(ip: str):
        with _login_lock:
            _login_failures.pop(ip, None)

    # ---- 公开接口限速（/api/version 等无需认证端点防刷）----
    _pub_rate = {}            # ip -> list[timestamp]
    _pub_rate_lock = threading.Lock()
    _PUB_RATE_MAX = 30        # 每窗口最多请求数
    _PUB_RATE_WINDOW = 60     # 窗口秒数

    def _check_public_rate(ip: str) -> bool:
        """滑动窗口限速：同一 IP 每 60 秒最多 _PUB_RATE_MAX 次"""
        now = time.time()
        with _pub_rate_lock:
            ts_list = [t for t in _pub_rate.get(ip, []) if now - t < _PUB_RATE_WINDOW]
            if len(ts_list) >= _PUB_RATE_MAX:
                return False
            ts_list.append(now)
            _pub_rate[ip] = ts_list
            return True

    # ---- 双请求防破解认证系统 ----
    dual_auth = DualRequestAuthSystem(framework.config.get('security', {}), db=db)

    @app.before_request
    def _global_blacklist_guard():
        """全局黑名单拦截：被封禁的 IP 无法访问任何路由（白名单除外）"""
        ip = get_client_ip()
        if dual_auth.is_whitelisted(ip) or not dual_auth.is_blacklisted(ip):
            return None
        return jsonify({'code': 403, 'msg': '访问被拒绝'}), 403

    # ---- 刷新过快检测（同一 IP 5 秒内刷新页面 ≥2 次 → 引导到 /reset）----
    _refresh_windows = {}          # ip -> list[timestamp]（仅记录页面导航请求）
    _refresh_lock = threading.Lock()
    _REFRESH_WINDOW = 5            # 秒
    _REFRESH_LIMIT = 5             # 窗口内触发阈值

    @app.before_request
    def _detect_rapid_reload():
        """同一 IP 在 5 秒内刷新页面超过阈值时，重定向到 /reset 恢复页。"""
        if request.method != 'GET':
            return None
        path = request.path or '/'
        is_page = (path == '/' or path.endswith('.html') or path == '/reset')
        if not is_page or path.startswith('/api/'):
            return None
        ip = get_client_ip()
        now = time.time()
        with _refresh_lock:
            ts = [t for t in _refresh_windows.get(ip, []) if now - t < _REFRESH_WINDOW]
            ts.append(now)
            _refresh_windows[ip] = ts
            count = len(ts)
            if len(_refresh_windows) > 2048:
                stale = [k for k, v in _refresh_windows.items() if not v or now - v[-1] >= 600]
                for k in stale:
                    _refresh_windows.pop(k, None)
        if count >= _REFRESH_LIMIT and path != '/reset':
            from flask import redirect
            return redirect('/reset', code=302)
        return None

    # ---- 扩展点：Web 请求前后（插件可在此做鉴权增强/审计/改写响应）----
    @app.before_request
    def _hook_before_request():
        """请求前扩展点：handler 可返回 Flask Response 以短路请求。"""
        fw = getattr(app, '_framework', None)
        hooks = getattr(fw, 'hooks', None) if fw else None
        if hooks is None:
            return None
        try:
            results = hooks.trigger_sync('http.before_request', request)
            for r in (results or []):
                if hasattr(r, 'status_code'):   # 形如 Flask Response
                    return r
        except Exception as e:
            logger.warning(f"http.before_request 扩展点异常: {e}")
        return None

    @app.after_request
    def _hook_after_request(resp):
        """请求后扩展点：可读取/改写响应，必须返回响应对象。"""
        fw = getattr(app, '_framework', None)
        hooks = getattr(fw, 'hooks', None) if fw else None
        if hooks is not None:
            try:
                hooks.trigger_sync('http.after_request', request, resp)
            except Exception as e:
                logger.warning(f"http.after_request 扩展点异常: {e}")
        return resp

    # ---- 工具函数 ----

    def _project_root() -> str:
        return _PROJECT_ROOT

    def _get_framework_local_version() -> str:
        """读取本地 VERSION 文件（随 ZIP 更新自动覆盖，不依赖 .git）"""
        try:
            with open(os.path.join(_project_root(), 'VERSION'), 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return ''

    def _install_new_requirements():
        """扫描新 requirements.txt，自动安装缺失依赖（框架更新后调用）"""
        import importlib.metadata
        req_file = os.path.join(_project_root(), 'requirements.txt')
        if not os.path.isfile(req_file):
            return
        missing = []
        with open(req_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                m = re.match(r'^([a-zA-Z0-9_.\-]+)', line)
                if not m:
                    continue
                try:
                    importlib.metadata.version(m.group(1))
                except importlib.metadata.PackageNotFoundError:
                    missing.append(m.group(1))
        if not missing:
            return
        logger.info(f"框架更新：检测到 {len(missing)} 个新依赖: {', '.join(missing)}，开始安装...")
        from framework.loader import pip_install_requirements
        result = pip_install_requirements(sys.executable, req_file, timeout=300)
        if result['success']:
            logger.info(f"框架更新：依赖安装完成（镜像: {result['mirror']}）")
        else:
            raise RuntimeError(f"依赖安装失败: {result.get('error')}")

    _PRE_RELEASE_PRIORITY = {
        'alpha': 0,
        'beta': 1,
        'rc': 2,
        'pre': 2, 'preview': 2,
    }

    def _parse_version_tuple(v: str):
        """版本字符串 → 可比较元组，支持语义化版本预发布标签比较"""
        if not v:
            return None
        if '-' in v:
            base, _, pre = v.partition('-')
        else:
            base, pre = v, ''
        base_nums = re.findall(r'\d+', base)
        if not base_nums:
            return None
        base_tuple = tuple(int(n) for n in base_nums)
        pre_priority = 3  # 无预发布标签 = stable，最高优先级
        if pre:
            m = re.match(r'^([a-zA-Z]+)', pre)
            if m:
                tag = m.group(1).lower()
                pre_priority = _PRE_RELEASE_PRIORITY.get(tag, 3)
        pre_nums = tuple(int(n) for n in re.findall(r'\d+', pre)) if pre else ()
        return base_tuple + (pre_priority,) + pre_nums

    def _latest_release_tag(repo: str) -> str:
        """返回仓库版本号最高的 Release tag（含 pre-release）；无 Release 返回空串"""
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/releases?per_page=30",
                headers={'Accept': 'application/vnd.github+json'},
                timeout=15,
            )
            if resp.status_code != 200:
                return ''
            releases = resp.json()
            if not isinstance(releases, list) or not releases:
                return ''
            best, best_t = '', None
            for rel in releases:
                t = rel.get('tag_name') or ''
                tv = _parse_version_tuple(t[1:] if t.startswith('v') else t)
                if tv is None:
                    continue
                if best_t is None or tv > best_t:
                    best, best_t = t, tv
            return best
        except Exception:
            return ''

    def _data_dir() -> str:
        d = os.path.join(_project_root(), 'data')
        os.makedirs(d, exist_ok=True)
        return d

    def _quote_ident(name: str) -> str:
        """按数据库类型安全引用标识符（MySQL 反引号 / SQLite 双引号）"""
        if framework.config.get('database', {}).get('type') == 'mysql':
            return f"`{name}`"
        return f'"{name}"'

    def _yaml_config_path() -> str:
        """返回框架实际加载的配置文件路径（支持自定义 config 启动）"""
        return getattr(framework, 'config_path', None) or os.path.join(_project_root(), 'config.yaml')

    def _read_yaml_section(section: str) -> dict:
        """读取 config.yaml 指定段的原始 dict（不做环境变量替换）"""
        path = _yaml_config_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f) or {}
        except Exception:
            return {}
        return doc.get(section, {}) if isinstance(doc, dict) else {}

    def _yaml_scalar(v):
        """将单个值序列化为 YAML 标量（避免 PyYAML safe_dump 追加 '...' 的问题）"""
        if v is None:
            return 'null'
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        if s == '':
            return "''"
        if (any(ch in s for ch in ':#{}[],&*!|>\'"%@`\n')
                or s != s.strip()
                or s.lower() in ('true', 'false', 'null', 'yes', 'no', 'on', 'off')):
            return json.dumps(s, ensure_ascii=False)
        return s

    def _yaml_block_lines(data: dict, indent: int = 2, level: int = 0) -> list:
        """将 dict 序列化为带缩进的 YAML 块文本行（保证类型可被安全加载）"""
        lines = []
        prefix = ' ' * (level * indent)
        for k, v in data.items():
            if isinstance(v, dict):
                lines.append(f"{prefix}{k}:\n")
                lines.extend(_yaml_block_lines(v, indent, level + 1))
            elif isinstance(v, (list, tuple)):
                lines.append(f"{prefix}{k}:\n")
                for item in v:
                    if isinstance(item, dict):
                        sub = _yaml_block_lines(item, indent, level + 2)
                        lines.append(f"{' ' * ((level + 1) * indent)}- " + sub[0].strip() + "\n")
                        for extra in sub[1:]:
                            lines.append(extra)
                    else:
                        lines.append(f"{' ' * ((level + 1) * indent)}- {_yaml_scalar(item)}\n")
            else:
                lines.append(f"{prefix}{k}: {_yaml_scalar(v)}\n")
        return lines

    def _update_yaml_section(section: str, values: dict) -> bool:
        """重写 config.yaml 中指定段（保留其他段及注释），返回是否成功。"""
        path = _yaml_config_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            return False

        start = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and not stripped.startswith('---'):
                m = re.match(rf'^({re.escape(section)})\s*:', stripped)
                if m and not m.group(0)[len(section) + 1:].lstrip().startswith(('{', '[')):
                    start = i
                    break
        if start is None:
            return False

        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped and not stripped.startswith('#') and not stripped.startswith('---') \
                    and not lines[j][:1].isspace():
                end = j
                break

        merged = dict(_read_yaml_section(section))
        merged.update(values or {})

        new_lines = [f"{section}:\n"] + _yaml_block_lines(merged, indent=2, level=1)
        lines[start:end] = new_lines
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            return True
        except Exception:
            return False

    def _read_market_sources_custom() -> list:
        """读取用户自定义插件源列表（存 system_config 表）"""
        try:
            row = db.query_one("SELECT config_value FROM system_config WHERE config_key = 'plugin_market_sources'")
            if row and row['config_value']:
                parsed = json.loads(row['config_value'])
                if isinstance(parsed, list):
                    return parsed
        except Exception:
            pass
        return []

    def _save_market_sources_custom(sources: list) -> None:
        """保存用户自定义插件源列表"""
        try:
            existing = db.query_one("SELECT config_value FROM system_config WHERE config_key = 'plugin_market_sources'")
            if existing:
                db.execute(
                    "UPDATE system_config SET config_value = %s WHERE config_key = 'plugin_market_sources'",
                    (json.dumps(sources, ensure_ascii=False),)
                )
            else:
                db.execute(
                    "INSERT INTO system_config (config_key, config_value, description) VALUES (%s, %s, %s)",
                    ('plugin_market_sources', json.dumps(sources, ensure_ascii=False), 'WebUI 自定义插件源列表')
                )
        except Exception as e:
            logger.error(f"保存自定义插件源失败: {e}")

    _DEFAULT_MARKET = {
        'name': 'ZCBOT 官方插件源',
        'url': 'https://raw.githubusercontent.com/kuangxing6367/zcbot_plugins/main/registry.json',
    }

    _MIRROR_MARKETS = [
        {
            'name': 'ZCBOT 镜像源 (ghproxy)',
            'url': 'https://ghproxy.net/https://raw.githubusercontent.com/kuangxing6367/zcbot_plugins/main/registry.json',
        },
        {
            'name': 'ZCBOT 镜像源 (ghproxy.cn)',
            'url': 'https://ghproxy.cn/https://raw.githubusercontent.com/kuangxing6367/zcbot_plugins/main/registry.json',
        },
    ]

    _DEFAULT_GITHUB_PROXY = 'https://gh.jasonzeng.dev'

    def _github_proxy() -> str:
        """读取配置的 GitHub 加速代理地址，未配置时使用默认值"""
        try:
            proxy = str(framework.config.get('github_proxy', '') or '').strip().rstrip('/')
            return proxy or _DEFAULT_GITHUB_PROXY
        except Exception:
            return _DEFAULT_GITHUB_PROXY

    def _github_url_candidates(url: str) -> list:
        """生成候选下载地址（按优先级）：配置加速代理 → 内置 ghproxy 镜像 → 直连 GitHub"""
        def _strip_scheme(u: str) -> str:
            for p in ('https://', 'http://'):
                if u.startswith(p):
                    return u[len(p):]
            return u

        candidates = []
        proxy = _github_proxy()
        if proxy:
            if proxy.endswith('/https://') or proxy.endswith('/http://'):
                candidates.append(f"{proxy}{_strip_scheme(url)}")
            else:
                candidates.append(f"{proxy}/https://{_strip_scheme(url)}")
        for mirror in _MIRROR_MARKETS:
            mirror_host = mirror['url'].split('/')[2]
            candidates.append(f"https://{mirror_host}/https://{_strip_scheme(url)}")
        candidates.append(url)
        seen, out = set(), []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _download_zip_file(urls: list) -> str:
        """依次尝试候选 URL 下载 ZIP 并校验魔数 PK；返回有效 ZIP 临时路径，全失败返回 None"""
        for url in urls:
            try:
                logger.info(f"正在下载 ZIP: {url}")
                resp = requests.get(url, timeout=180, stream=True)
                if resp.status_code != 200:
                    continue
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
                try:
                    for chunk in resp.iter_content(chunk_size=8192):
                        tmp.write(chunk)
                finally:
                    tmp.close()
                with open(tmp.name, 'rb') as f:
                    head = f.read(4)
                if len(head) >= 2 and head[:2] == b'PK':
                    return tmp.name
                logger.warning(f"候选返回非 ZIP 内容，跳过: {url}")
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"ZIP 候选下载失败 {url}: {e}")
                continue
        return None

    def _fetch_market_source(source: dict) -> list:
        """拉取单个 registry 源的插件列表"""
        url = (source.get('url') or '').strip()
        if not url:
            return []
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        plugins = data.get('plugins', []) if isinstance(data, dict) else []
        result = []
        for p in plugins:
            if not isinstance(p, dict) or not p.get('name'):
                continue
            p = dict(p)
            p['source'] = source.get('name', '')
            result.append(p)
        return result

    def _market_installed_set() -> set:
        """当前已安装的插件名集合（磁盘为准：目录存在且含 main.py）"""
        try:
            result = set()
            if os.path.isdir(plugins_dir):
                for name in os.listdir(plugins_dir):
                    if os.path.isfile(os.path.join(plugins_dir, name, 'main.py')):
                        result.add(name)
            result.update(framework.plugin_loader.get_loaded_plugins().keys())
            return result
        except Exception:
            return set()

    def _download_plugin_tree(repo: str, branch: str, sub_path: str, target_dir: str):
        """通过 GitHub API 获取仓库文件树，仅下载 sub_path 目录下的文件。返回 (ok, msg)。"""
        try:
            api_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
            headers = {'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'zcbot'}
            tree = None
            api_err = ''
            for cand in _github_url_candidates(api_url):
                try:
                    resp = requests.get(cand, headers=headers, timeout=30)
                    if resp.status_code == 404:
                        return False, f'仓库或分支不存在: {repo}@{branch}'
                    if resp.status_code != 200:
                        api_err = f'HTTP {resp.status_code}'
                        continue
                    try:
                        tree = resp.json().get('tree', [])
                    except ValueError:
                        api_err = f'非 JSON 响应: {cand}'
                        continue
                    if tree:
                        break
                    api_err = f'空响应: {cand}'
                except Exception as e:
                    api_err = str(e)
                    continue
            if tree is None:
                return False, f'GitHub API 获取文件树失败: {api_err or "所有候选均失败"}'

            sub = (sub_path or '/').lstrip('/').rstrip('/')
            files = []
            for item in tree:
                if item.get('type') != 'blob':
                    continue
                path = item.get('path', '')
                if sub:
                    if path == sub:
                        rel = os.path.basename(path)
                    elif path.startswith(sub + '/'):
                        rel = path[len(sub) + 1:]
                    else:
                        continue
                else:
                    rel = path
                if not rel or '..' in rel or rel.startswith('/') or '\\' in rel:
                    continue
                files.append((path, rel))

            if not files:
                return False, f'子目录不存在或无文件: {sub_path or "/"}'

            raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}"
            os.makedirs(target_dir, exist_ok=True)
            for src_path, rel in files:
                raw_url = f"{raw_base}/{src_path}"
                urls = _github_url_candidates(raw_url)
                got = False
                last_err = ''
                for u in urls:
                    try:
                        r = requests.get(u, timeout=30)
                        if r.status_code == 200:
                            dest = os.path.join(target_dir, rel)
                            parent = os.path.dirname(dest)
                            if parent:
                                os.makedirs(parent, exist_ok=True)
                            with open(dest, 'wb') as f:
                                f.write(r.content)
                            got = True
                            break
                        last_err = f'HTTP {r.status_code}'
                    except Exception as e:
                        last_err = str(e)
                if not got:
                    return False, f'下载文件失败 {src_path}: {last_err}'

            logger.info(f"已通过文件树下载插件 {repo}@{branch} 子目录 {sub or '/'}（{len(files)} 个文件）")
            return True, ''
        except Exception as e:
            return False, str(e)

    def _download_and_extract_plugin(repo: str, branch: str, sub_path: str, target_dir: str):
        """从 GitHub 下载插件代码到目标目录（可指定子目录）。返回 (ok, msg)"""
        if repo.startswith('https://github.com/'):
            repo = repo.replace('https://github.com/', '').rstrip('/')
        elif repo.startswith('http://github.com/'):
            repo = repo.replace('http://github.com/', '').rstrip('/')
        if not repo or '..' in repo:
            return False, '非法仓库地址'

        plugin_zip = None
        if sub_path:
            sp = sub_path.lstrip('/').rstrip('/')
            pkg_name = sp.split('/')[-1]
            if pkg_name and not pkg_name.startswith('.'):
                pkg_url = f"https://raw.githubusercontent.com/{repo}/gh-pages/packages/{pkg_name}.zip"
                plugin_zip = _download_zip_file(_github_url_candidates(pkg_url))

        if plugin_zip is not None:
            try:
                with zipfile.ZipFile(plugin_zip, 'r') as zf:
                    names = zf.namelist()
                    for name in names:
                        if name.endswith('/'):
                            continue
                        if '..' in name or name.startswith('/') or '\\' in name:
                            continue
                        dest = os.path.join(target_dir, name)
                        parent = os.path.dirname(dest)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        with open(dest, 'wb') as f:
                            f.write(zf.read(name))
                logger.info(f"已通过单插件 zip 安装 {pkg_name}（gh-pages/packages/{pkg_name}.zip）")
                return True, ''
            except Exception as e:
                logger.warning(f"单插件 zip 解压失败，回退文件树: {e}")
            finally:
                try:
                    os.unlink(plugin_zip)
                except Exception:
                    pass

        ok, msg = _download_plugin_tree(repo, branch, sub_path, target_dir)
        if ok:
            return True, ''

        zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
        tmp_zip = _download_zip_file(_github_url_candidates(zip_url))
        if tmp_zip is None:
            return False, f'下载的 ZIP 文件无效（{msg or "所有下载尝试均失败"}）'
        try:
            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                names = zf.namelist()
                prefix = names[0].split('/')[0] if names else ''
                sub = (sub_path or '/').lstrip('/').rstrip('/')
                for name in names:
                    if name.endswith('/'):
                        continue
                    rel_path = name
                    if prefix and rel_path.startswith(prefix + '/'):
                        rel_path = rel_path[len(prefix) + 1:]
                    if sub:
                        if rel_path == sub:
                            continue
                        if not rel_path.startswith(sub + '/'):
                            continue
                        rel_path = rel_path[len(sub) + 1:]
                    if not rel_path:
                        continue
                    if '..' in rel_path or rel_path.startswith('/') or '\\' in rel_path:
                        continue
                    dest = os.path.join(target_dir, rel_path)
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(dest, 'wb') as f:
                        f.write(zf.read(name))
            return True, ''
        finally:
            try:
                os.unlink(tmp_zip)
            except Exception:
                pass

    _trusted_proxies = set(framework.config.get('security', {}).get('trusted_proxies', []))

    def get_client_ip():
        remote = request.remote_addr or 'unknown'
        if _trusted_proxies and remote in _trusted_proxies:
            xff = request.headers.get('X-Forwarded-For', '')
            if xff:
                parts = [p.strip() for p in xff.split(',') if p.strip()]
                if parts:
                    return parts[0]
        return remote

    def audit_log(admin_id, admin_name, action, target_type=None, target_name=None,
                  detail=None, result='success', error_message=None):
        """记录审计日志"""
        try:
            db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, ip_address, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (admin_id, admin_name, action, target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 get_client_ip(), result, error_message)
            )
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def _extract_token(req):
        """从 Authorization: Bearer xxx 头或 Cookie 提取 token"""
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        return req.cookies.get('zcbot_token')

    def _verify_token(token):
        """验证 token，返回 admin 字典或 None（支持用户会话 token 与接口令牌 API Key）"""
        if not token:
            return None
        if len(token) == 2048:
            row = db.query_one(
                "SELECT id, username, role, is_active, token_created_at FROM admin_users WHERE token = %s",
                (token,)
            )
            if row and row['is_active']:
                timeout = web_cfg.get('token_timeout') or web_cfg.get('session_timeout', 86400)
                if row['token_created_at']:
                    created = row['token_created_at']
                    if isinstance(created, str):
                        try:
                            created = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            return None
                    expiry = created + timedelta(seconds=timeout)
                    if datetime.now() > expiry:
                        return None
                return {'id': row['id'], 'username': row['username'], 'role': row['role']}
        if len(token) >= 40:
            row = db.query_one(
                "SELECT id, name, role, is_active, expires_at, last_used_at FROM api_tokens WHERE token = %s",
                (token,)
            )
            if row and row['is_active']:
                if row['expires_at']:
                    try:
                        if time.time() > float(row['expires_at']):
                            return None
                    except (ValueError, TypeError):
                        return None
                try:
                    db.execute(
                        "UPDATE api_tokens SET last_used_at = %s WHERE id = %s",
                        (str(int(time.time())), row['id'])
                    )
                except Exception:
                    pass
                return {'id': 'api:' + str(row['id']), 'username': 'api:' + row['name'], 'role': row['role']}
        return None

    def _sync_token_cookie(resp, token: str):
        """将登录 token 同步到 HttpOnly Cookie（SameSite=Lax），供 iframe 场景兜底鉴权"""
        try:
            timeout = web_cfg.get('token_timeout') or web_cfg.get('session_timeout', 86400)
        except Exception:
            timeout = 86400
        resp.set_cookie(
            'zcbot_token', token or '',
            max_age=timeout, path='/',
            httponly=True, samesite='Lax',
            secure=bool(request.is_secure),
        )

    def _auth_wrap(fn, super_only=False):
        """鉴权装饰器工厂：校验通过后把 token 同步种到 Cookie（iframe 场景兜底）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            if super_only and admin.get('role') != 'super':
                return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
            request.admin = admin
            result = fn(*args, **kwargs)
            if isinstance(result, tuple):
                resp, status = result[0], (result[1] if len(result) > 1 else None)
            else:
                resp, status = result, None
            if isinstance(resp, Response):
                _sync_token_cookie(resp, token)
            return (resp, status) if status else resp
        return wrapper

    def require_auth(fn):
        """登录验证装饰器（基于 token）"""
        return _auth_wrap(fn, super_only=False)

    def require_super(fn):
        """超级管理员验证装饰器（基于 token）"""
        return _auth_wrap(fn, super_only=True)

    # ---- 构建共享上下文，分发给各功能域模块 ----
    ctx = SimpleNamespace(
        app=app,
        framework=framework,
        db=db,
        plugins_dir=plugins_dir,
        web_cfg=web_cfg,
        dual_auth=dual_auth,
        require_auth=require_auth,
        require_super=require_super,
        get_client_ip=get_client_ip,
        audit_log=audit_log,
        # token 相关
        _extract_token=_extract_token,
        _verify_token=_verify_token,
        _sync_token_cookie=_sync_token_cookie,
        _auth_wrap=_auth_wrap,
        # 限速 / 登录防爆破
        _check_login_rate=_check_login_rate,
        _record_login_failure=_record_login_failure,
        _clear_login_failures=_clear_login_failures,
        _check_public_rate=_check_public_rate,
        # 通用工具
        _project_root=_project_root,
        _get_framework_local_version=_get_framework_local_version,
        _install_new_requirements=_install_new_requirements,
        _parse_version_tuple=_parse_version_tuple,
        _latest_release_tag=_latest_release_tag,
        _data_dir=_data_dir,
        _quote_ident=_quote_ident,
        # yaml 配置
        _yaml_config_path=_yaml_config_path,
        _read_yaml_section=_read_yaml_section,
        _yaml_scalar=_yaml_scalar,
        _yaml_block_lines=_yaml_block_lines,
        _update_yaml_section=_update_yaml_section,
        # 插件市场 / GitHub
        _read_market_sources_custom=_read_market_sources_custom,
        _save_market_sources_custom=_save_market_sources_custom,
        _DEFAULT_MARKET=_DEFAULT_MARKET,
        _MIRROR_MARKETS=_MIRROR_MARKETS,
        _DEFAULT_GITHUB_PROXY=_DEFAULT_GITHUB_PROXY,
        _github_proxy=_github_proxy,
        _github_url_candidates=_github_url_candidates,
        _download_zip_file=_download_zip_file,
        _fetch_market_source=_fetch_market_source,
        _market_installed_set=_market_installed_set,
        _download_plugin_tree=_download_plugin_tree,
        _download_and_extract_plugin=_download_and_extract_plugin,
        # 日志经纪
        log_broker=log_broker,
    )

    # ---- 按功能域注册路由（各模块 import 后调用 register(ctx)）----
    from framework.api import (
        auth, admins, apikeys, dashboard, plugins, commands, users_groups, tasks,
        logs, config, db_gateway, framework_ops, webui, files, stats, perm_api,
        static_routes,
    )
    auth.register(ctx)
    admins.register(ctx)
    apikeys.register(ctx)
    dashboard.register(ctx)
    plugins.register(ctx)
    commands.register(ctx)
    users_groups.register(ctx)
    tasks.register(ctx)
    logs.register(ctx)
    config.register(ctx)
    db_gateway.register(ctx)
    framework_ops.register(ctx)
    webui.register(ctx)
    files.register(ctx)
    stats.register(ctx)
    perm_api.register(ctx)
    static_routes.register(ctx)

    # ---- 插件自定义 API 路由挂载（framework.api.registry）----
    from framework.api import registry as _api_registry
    _api_registry.set_web_context(app, require_auth)
    _api_registry.mount_all(app, require_auth)

    return app


class WebServer:
    """Web UI 服务器，在独立线程中运行"""

    def __init__(self, framework):
        self.framework = framework
        self.app = create_web_app(framework)
        web_cfg = framework.config.get('web', {})
        self.host = web_cfg.get('host', '0.0.0.0')
        self.port = web_cfg.get('port', 8080)
        # SSL：与 OneBot WSS 共用 config['ssl']；证书路径支持相对/绝对。
        # waitress 不支持 TLS，故启用 SSL 时改用 werkzeug（支持 ssl_context）。
        self.ssl_context = None
        try:
            self.ssl_context = framework.build_ssl_context()
        except Exception as e:
            logger.error(f"SSL 配置无效，Web 后台回退为 http: {e}")
        self.scheme = 'https' if self.ssl_context else 'http'
        self._thread = None
        self._server = None
        self._running = False

    def start(self):
        """启动 Web 服务器"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-server")
        self._thread.start()
        logger.info(f"Web UI 已启动: {self.scheme}://{self.host}:{self.port}")

    def _run(self):
        """运行 Web 服务器（保存 server 句柄，供 stop() 真正停止并释放端口）"""
        try:
            if self.ssl_context is not None:
                # HTTPS：waitress 不支持 TLS，使用 werkzeug 的 ssl_context
                from werkzeug.serving import make_server
                self._server = make_server(self.host, self.port, self.app,
                                           threaded=True, ssl_context=self.ssl_context)
                self._server.serve_forever()
                return
            try:
                from waitress.server import create_server as waitress_create_server
                self._server = waitress_create_server(
                    self.app, host=self.host, port=self.port, threads=8)
                self._server.run()
            except ImportError:
                from werkzeug.serving import make_server
                self._server = make_server(self.host, self.port, self.app)
                self._server.serve_forever()
        except Exception as e:
            self._server = None
            if getattr(e, 'errno', None) == 98 or 'Address already in use' in str(e):
                logger.error(
                    f"Web UI 启动失败: 端口 {self.port} 已被占用。"
                    f"可能残留了旧实例，请先停止旧进程（如: ss -tlnp | grep {self.port}）")
            else:
                logger.error(f"Web UI 异常: {e}")

    def stop(self):
        """停止 Web 服务器（真正关闭监听，避免优雅停机后端口残留）"""
        self._running = False
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                if hasattr(srv, 'close'):
                    srv.close()      # waitress WSGIServer
                elif hasattr(srv, 'shutdown'):
                    srv.shutdown()   # werkzeug
            except Exception as e:
                logger.warning(f"Web 服务器关闭异常: {e}")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Web UI 已停止")
