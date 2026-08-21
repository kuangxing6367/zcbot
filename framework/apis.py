"""
API 接口模块
提供：登录认证、仪表盘、插件管理（ZIP上传、GitHub更新）、命令管理、审计日志
基于 Flask，运行在独立线程中
"""
import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import psutil
import requests
import yaml
from flask import Flask, request, jsonify, send_from_directory, Response

from framework.log_broker import log_broker
from framework.dual_auth import DualRequestAuthSystem

logger = logging.getLogger('zcbot')


def create_web_app(framework) -> Flask:
    """
    创建 Flask 应用并注册所有路由
    :param framework: Framework 实例
    """
    app = Flask(__name__, static_folder=None)
    web_cfg = framework.config.get('web', {})

    db = framework.db
    plugins_dir = framework.plugin_loader.plugins_dir

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
        """同一 IP 在 5 秒内刷新页面超过阈值时，重定向到 /reset 恢复页。
        仅统计页面导航请求（GET /、路径以 .html 结尾、/reset），排除 /api/ 与静态资源，
        以避开前端 JS 轮询造成的误判。"""
        if request.method != 'GET':
            return None
        path = request.path or '/'
        # 只统计 HTML 页面导航请求
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
            # 内存占用保护：定期清理过期项
            if len(_refresh_windows) > 2048:
                stale = [k for k, v in _refresh_windows.items() if not v or now - v[-1] >= 600]
                for k in stale:
                    _refresh_windows.pop(k, None)
        # 命中阈值：本 IP 已在该窗口内刷新达到阈值，且当前不是 /reset 页面本身时重定向
        if count >= _REFRESH_LIMIT and path != '/reset':
            from flask import redirect
            # 记录本次仍算一次，但页面重定向到恢复页
            return redirect('/reset', code=302)
        return None

    # ---- 工具函数 ----

    def _project_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

    # 预发布标签优先级（数字越小越早期）：alpha < beta < rc < stable
    _PRE_RELEASE_PRIORITY = {
        'alpha': 0,
        'beta': 1,
        'rc': 2,
        'pre': 2, 'preview': 2,
    }

    def _parse_version_tuple(v: str):
        """版本字符串 → 可比较元组，支持语义化版本预发布标签比较

        预发布优先级：alpha(0) < beta(1) < rc(2) < stable(3)

        示例:
          0.0.1-alpha.1-build.23 → (0, 0, 1, 0, 1, 23)
          0.0.1-beta.0           → (0, 0, 1, 1, 0)
          0.0.1                  → (0, 0, 1, 3)
        """
        if not v:
            return None

        # 拆分主版本号和预发布部分
        # 格式: 0.0.1-alpha.1-build.23 或 0.0.1-beta.0 或 0.0.1
        if '-' in v:
            base, _, pre = v.partition('-')
        else:
            base, pre = v, ''

        # 解析基础版本号
        base_nums = re.findall(r'\d+', base)
        if not base_nums:
            return None
        base_tuple = tuple(int(n) for n in base_nums)

        # 解析预发布优先级：提取预发布部分的第一个英文单词作为标签
        pre_priority = 3  # 无预发布标签 = stable，最高优先级
        if pre:
            m = re.match(r'^([a-zA-Z]+)', pre)
            if m:
                tag = m.group(1).lower()
                pre_priority = _PRE_RELEASE_PRIORITY.get(tag, 3)

        # 预发布部分的数字（如 alpha.1 中的 1, build.23 中的 23）
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
        # 含 YAML 特殊字符或前后空格、或可能是保留字时，用 JSON 引号包裹（JSON 是 YAML 子集）
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
                        # 首行改为 "- " 开头（列表项 dict 的第一行）
                        lines.append(f"{' ' * ((level + 1) * indent)}- " + sub[0].strip() + "\n")
                        for extra in sub[1:]:
                            lines.append(extra)
                    else:
                        lines.append(f"{' ' * ((level + 1) * indent)}- {_yaml_scalar(item)}\n")
            else:
                lines.append(f"{prefix}{k}: {_yaml_scalar(v)}\n")
        return lines

    def _update_yaml_section(section: str, values: dict) -> bool:
        """
        重写 config.yaml 中指定段（保留其他段及注释），返回是否成功。
        段内原有子 key 保留，被 values 中的同名 key 覆盖。
        """
        path = _yaml_config_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            return False

        # 定位段起始行（行首无缩进、非注释的 "section:"）
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

        # 定位段结束（下一个行首无缩进的非注释行）
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped and not stripped.startswith('#') and not stripped.startswith('---') \
                    and not lines[j][:1].isspace():
                end = j
                break

        merged = dict(_read_yaml_section(section))
        merged.update(values or {})

        # 段内子 key 需要一级缩进（level=1）
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
        """
        读取配置的 GitHub 加速代理地址（config.yaml github_proxy）
        未配置时默认使用 https://gh.jasonzeng.dev（优先加速），仍不可用则回退内置 ghproxy 镜像
        """
        try:
            proxy = str(framework.config.get('github_proxy', '') or '').strip().rstrip('/')
            return proxy or _DEFAULT_GITHUB_PROXY
        except Exception:
            return _DEFAULT_GITHUB_PROXY

    def _github_url_candidates(url: str) -> list:
        """
        生成候选下载地址（按优先级）：配置的加速代理 → 内置 ghproxy 镜像 → 直连 GitHub。
        GitHub 加速方案：代理前缀形式为 {host}/https://{原地址（去协议）}
        修复：原地址已含 https://，直接拼接会得到 https://ghproxy.cn/https://https://...（双重协议）
        """
        def _strip_scheme(u: str) -> str:
            for p in ('https://', 'http://'):
                if u.startswith(p):
                    return u[len(p):]
            return u

        candidates = []
        proxy = _github_proxy()
        if proxy:
            if proxy.endswith('/https://') or proxy.endswith('/http://'):
                # 代理已带 /https:// 前缀，直接拼去协议后的地址
                candidates.append(f"{proxy}{_strip_scheme(url)}")
            else:
                candidates.append(f"{proxy}/https://{_strip_scheme(url)}")
        for mirror in _MIRROR_MARKETS:
            mirror_host = mirror['url'].split('/')[2]
            candidates.append(f"https://{mirror_host}/https://{_strip_scheme(url)}")
        candidates.append(url)
        # 去重保序
        seen, out = set(), []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _download_zip_file(urls: list) -> str:
        """
        依次尝试候选 URL 下载 ZIP 文件，并校验内容确实为 ZIP（魔数 PK）。
        镜像/代理返回的 HTML 错误页等无效内容会被跳过，继续尝试下一个候选。
        返回有效 ZIP 的临时文件路径；全部失败返回 None（调用方负责 unlink 清理）。
        """
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
                # 校验 ZIP 魔数：PK\x03\x04（正常）或 PK\x05\x06（空 ZIP）
                with open(tmp.name, 'rb') as f:
                    head = f.read(4)
                if len(head) >= 2 and head[:2] == b'PK':
                    return tmp.name
                # 无效内容（如镜像返回的 HTML 错误页），尝试下一个候选
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
        """
        当前已安装的插件名集合（磁盘为准：目录存在且含 main.py）。
        不用 DB plugins 表：目录被删/损坏时 DB 残留会导致市场显示"已安装"但实际没装。
        """
        try:
            result = set()
            if os.path.isdir(plugins_dir):
                for name in os.listdir(plugins_dir):
                    if os.path.isfile(os.path.join(plugins_dir, name, 'main.py')):
                        result.add(name)
            # 兜底：已加载的插件
            result.update(framework.plugin_loader.get_loaded_plugins().keys())
            return result
        except Exception:
            return set()

    def _download_plugin_tree(repo: str, branch: str, sub_path: str, target_dir: str):
        """
        通过 GitHub API 获取仓库文件树，仅下载 sub_path 目录下的文件（raw 单文件拉取），
        避免整仓 ZIP 下载。返回 (ok, msg)。
        """
        try:
            # 获取仓库文件树（递归）。api.github.com 直连可能被墙/限流，
            # 通过 _github_url_candidates 生成代理/镜像候选逐个尝试。
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
                    # 校验返回确实是 JSON（代理/镜像可能返回 HTML 错误页）
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

            # 逐个 raw 下载（代理 → 镜像 → 直连）
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
        """
        从 GitHub 下载插件代码到目标目录（可指定子目录）。
        优先使用 GitHub API 文件树逐个拉取文件（省流量），失败回退整仓 ZIP。
        返回 (ok, msg)
        """
        if repo.startswith('https://github.com/'):
            repo = repo.replace('https://github.com/', '').rstrip('/')
        elif repo.startswith('http://github.com/'):
            repo = repo.replace('http://github.com/', '').rstrip('/')
        if not repo or '..' in repo:
            return False, '非法仓库地址'

        # 方式零：优先下载"单个插件 zip"（由插件仓库 Actions 打包发布到 gh-pages/packages/）。
        # 单文件、小体积，可走代理/镜像加速；失败则回退到文件树/整仓 ZIP。
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
                    # 安全校验 + 解压（zip 根目录直接是插件文件）
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

        # 方式一：GitHub API 文件树 + raw 单文件下载（只拉插件目录，不下载整仓）
        ok, msg = _download_plugin_tree(repo, branch, sub_path, target_dir)
        if ok:
            return True, ''

        # 方式二：整仓 ZIP 回退（代理 → 镜像 → 直连，逐个校验 ZIP 有效性）
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

    def get_client_ip():
        return request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')

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
        # iframe 直接导航的插件页面/资源无法携带自定义 Header，走同源 Cookie 兜底
        return req.cookies.get('zcbot_token')

    def _verify_token(token):
        """验证 token，返回 admin 字典或 None"""
        if len(token) != 2048:
            return None
        row = db.query_one(
            "SELECT id, username, role, is_active, token_created_at FROM admin_users WHERE token = %s",
            (token,)
        )
        if not row or not row['is_active']:
            return None
        # 检查过期（SQLite 返回字符串，MySQL 返回 datetime，统一解析）
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

    def _sync_token_cookie(resp, token: str):
        """将登录 token 同步到 HttpOnly Cookie（SameSite=Lax），供 iframe/页面直接导航场景兜底鉴权。
        token 传空串则清除 Cookie。每次请求仍走 _verify_token 全量校验，不绕过鉴权。"""
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
        """鉴权装饰器工厂：校验通过后把 token 同步种到 Cookie（iframe 场景兜底），
        已登录的老会话无需重新登录即可获得 Cookie。"""
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
            request.admin = admin  # 将 admin 信息附加到 request
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

    # ---- 认证接口 ----

    @app.route('/api/login', methods=['POST'])
    def login():
        """管理员登录"""
        client_ip = get_client_ip()

        # 登录限速
        if not _check_login_rate(client_ip):
            return jsonify({'code': 429, 'msg': '尝试过于频繁，请 10 分钟后再试'}), 429

        data = request.get_json(silent=True) or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({'code': 400, 'msg': '用户名和密码不能为空'}), 400

        try:
            row = db.query_one(
                "SELECT id, username, password_hash, role, is_active FROM admin_users WHERE username = %s",
                (username,)
            )
        except Exception as e:
            logger.error(f"登录查询失败: {e}")
            return jsonify({'code': 500, 'msg': f'数据库错误: {e}'}), 500

        if not row:
            _record_login_failure(client_ip)
            audit_log(None, username, 'login', result='failure', error_message='用户不存在')
            return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401

        if not row['is_active']:
            _record_login_failure(client_ip)
            audit_log(row['id'], username, 'login', result='failure', error_message='账号已禁用')
            return jsonify({'code': 403, 'msg': '账号已禁用'}), 403

        # 验证密码
        try:
            stored_hash = row['password_hash']
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode('utf-8')
            if isinstance(password, str):
                password = password.encode('utf-8')
            if not bcrypt.checkpw(password, stored_hash):
                _record_login_failure(client_ip)
                audit_log(row['id'], username, 'login', result='failure', error_message='密码错误')
                return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401
        except Exception as e:
            logger.error(f"密码验证异常: {e}")
            return jsonify({'code': 500, 'msg': '密码验证失败'}), 500

        # 登录成功，清除失败计数
        _clear_login_failures(client_ip)

        # 生成 2048 位随机 token
        token = secrets.token_hex(1024)  # 2048 字符
        db.execute(
            "UPDATE admin_users SET token = %s, token_created_at = NOW(), last_login_at = NOW(), last_login_ip = %s WHERE id = %s",
            (token, get_client_ip(), row['id'])
        )
        audit_log(row['id'], username, 'login', result='success')

        resp = jsonify({
            'code': 0,
            'msg': '登录成功',
            'data': {'token': token, 'username': username, 'role': row['role']}
        })
        _sync_token_cookie(resp, token)
        return resp

    @app.route('/api/logout', methods=['POST'])
    @require_auth
    def logout():
        """退出登录"""
        admin = request.admin
        try:
            db.execute(
                "UPDATE admin_users SET token = NULL, token_created_at = NULL WHERE id = %s",
                (admin['id'],)
            )
        except Exception as e:
            logger.error(f"清除 token 失败: {e}")
        audit_log(admin['id'], admin['username'], 'logout')
        resp = jsonify({'code': 0, 'msg': '已退出'})
        _sync_token_cookie(resp, '')
        return resp

    @app.route('/api/me', methods=['GET'])
    @require_auth
    def me():
        """获取当前登录信息"""
        return jsonify({'code': 0, 'data': request.admin})

    @app.route('/api/change_password', methods=['POST'])
    @require_auth
    def change_password():
        """修改密码"""
        data = request.get_json(silent=True) or {}
        old_pwd = data.get('old_password', '')
        new_pwd = data.get('new_password', '')

        if not old_pwd or not new_pwd:
            return jsonify({'code': 400, 'msg': '旧密码和新密码不能为空'}), 400
        if len(new_pwd) < 6:
            return jsonify({'code': 400, 'msg': '新密码至少6位'}), 400

        admin = request.admin
        row = db.query_one("SELECT password_hash FROM admin_users WHERE id = %s", (admin['id'],))

        try:
            stored = row['password_hash']
            if isinstance(stored, str):
                stored = stored.encode('utf-8')
            if not bcrypt.checkpw(old_pwd.encode('utf-8'), stored):
                return jsonify({'code': 401, 'msg': '旧密码错误'}), 401
        except Exception:
            return jsonify({'code': 500, 'msg': '密码验证失败'}), 500

        new_hash = bcrypt.hashpw(new_pwd.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
        db.execute("UPDATE admin_users SET password_hash = %s WHERE id = %s", (new_hash, admin['id']))
        audit_log(admin['id'], admin['username'], 'change_password', result='success')

        return jsonify({'code': 0, 'msg': '密码已修改'})

    # ---- 双请求防破解认证 ----

    @app.route('/api/auth', methods=['POST'])
    def dual_request_auth():
        """
        双请求防破解认证入口
        第一次请求：{"token": "<8位任意字符串>"}        → 返回 nonce
        第二次请求：{"token": "<8192位Token>", "nonce": "<上一步nonce>"} → 校验通过返回迷惑性数据
        """
        client_ip = get_client_ip()
        data = request.get_json(silent=True) or {}
        token = data.get('token')
        nonce = data.get('nonce')
        status, resp = dual_auth.handle_request(client_ip, token, nonce)
        return jsonify(resp), status

    @app.route('/api/security/status', methods=['GET'])
    @require_super
    def security_status():
        """查看双请求认证系统状态（风险 IP / 黑名单 / 配置）"""
        return jsonify({'code': 0, 'data': dual_auth.get_status()})

    @app.route('/api/security/blacklist', methods=['GET'])
    @require_super
    def security_blacklist():
        """查看 IP 黑名单列表（含来源/原因/过期时间）"""
        return jsonify({'code': 0, 'data': dual_auth.get_blacklist()})

    @app.route('/api/security/blacklist', methods=['POST'])
    @require_super
    def security_blacklist_add():
        """手动将 IP 加入黑名单（可设置过期时间，不填则永久）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        ip = str(data.get('ip') or '').strip()
        if not ip:
            return jsonify({'code': 400, 'msg': '缺少 ip'}), 400
        reason = str(data.get('reason') or '手动拉黑').strip()
        # 支持过期时间：expires_in 秒 或 expires_at 时间字符串
        expires_at = None
        expires_in = data.get('expires_in')
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = time.time() + float(expires_in)
        dual_auth.add_manual_blacklist(ip, reason, expires_at=expires_at)
        audit_log(admin['id'], admin['username'], 'security_blacklist_add', 'security', ip,
                  {'reason': reason, 'expires_in': expires_in}, 'success')
        msg = f'IP [{ip}] 已加入黑名单'
        if expires_in:
            msg += f'（{int(expires_in)} 秒后自动解封）'
        return jsonify({'code': 0, 'msg': msg})

    @app.route('/api/security/unban', methods=['POST'])
    @require_super
    def security_unban():
        """从永久黑名单中移除指定 IP"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        ip = str(data.get('ip') or '').strip()
        if not ip:
            return jsonify({'code': 400, 'msg': '缺少 ip'}), 400
        ok = dual_auth.unblacklist(ip)
        audit_log(admin['id'], admin['username'], 'security_unban', 'security', ip, {'existed': ok})
        return jsonify({'code': 0, 'msg': f'IP [{ip}] 已{"从黑名单移除" if ok else "不在黑名单中"}'})

    # ---- 仪表盘 ----

    _dashboard_stats_cache = {'data': None, 't': 0}

    def _collect_dashboard_stats() -> dict:
        """仪表盘统计（MySQL COUNT 可能全表扫描，调用方需缓存）"""
        data = {}
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins WHERE is_active = 1")
            data['plugins_active'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins")
            data['plugins_total'] = row['cnt'] if row else 0
        except Exception:
            data['plugins_active'] = 0
            data['plugins_total'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM commands")
            data['commands_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active = 1")
            data['dynamic_commands'] = row['cnt'] if row else 0
        except Exception:
            data['commands_total'] = 0
            data['dynamic_commands'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM users")
            data['users_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active = 1")
            data['groups_active'] = row['cnt'] if row else 0
        except Exception:
            data['users_total'] = 0
            data['groups_active'] = 0

        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM tasks WHERE is_active = 1")
            data['tasks_active'] = row['cnt'] if row else 0
        except Exception:
            data['tasks_active'] = 0
        return data

    @app.route('/api/dashboard', methods=['GET'])
    @require_auth
    def dashboard():
        """仪表盘数据（统计部分 10s 缓存，避免 MySQL COUNT 全表扫描拖慢页面）"""
        data = {}
        now = time.time()
        cached = _dashboard_stats_cache.get('data')
        if cached is not None and (now - _dashboard_stats_cache['t']) < 10:
            data.update(cached)
        else:
            data.update(_collect_dashboard_stats())
            _dashboard_stats_cache['data'] = dict(data)
            _dashboard_stats_cache['t'] = now

        # OneBot 连接状态（实时）
        data['bots'] = framework.ws_server.get_connected_bots()
        data['ws_port'] = framework.config.get('onebot', {}).get('listen_port', 6830)

        # 框架信息
        data['framework_name'] = 'ZCBOT'
        fw_ver = _get_framework_local_version()
        data['framework_version'] = fw_ver or 'unknown'
        data['framework_alpha'] = 'alpha' in fw_ver
        data['github_repo'] = 'https://github.com/kuangxing6367/zcbot'

        return jsonify({'code': 0, 'data': data})

    @app.route('/api/dashboard/cards', methods=['GET'])
    @require_auth
    def dashboard_cards():
        """获取仪表盘插件卡片"""
        try:
            cards = framework.plugin_loader.get_dashboard_cards()
            return jsonify({'code': 0, 'data': cards})
        except Exception as e:
            logger.error(f"获取仪表盘卡片失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- WebUI 群组/用户管理页插件扩展 ----

    @app.route('/api/extensions/groups', methods=['GET'])
    @require_auth
    def ui_ext_groups_meta():
        """群组管理页插件扩展元信息"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.get_ui_extensions('groups')})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/extensions/users', methods=['GET'])
    @require_auth
    def ui_ext_users_meta():
        """用户管理页插件扩展元信息"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.get_ui_extensions('users')})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/extensions/data', methods=['POST'])
    @require_auth
    def ui_ext_groups_data():
        """批量获取多个群的扩展数据（当前页渲染用）"""
        try:
            body = request.get_json(silent=True) or {}
            ids = [int(x) for x in (body.get('ids') or []) if str(x).isdigit()]
            out = {}
            for gid in ids:
                out[str(gid)] = framework.plugin_loader.call_ui_extensions('groups', gid)
            return jsonify({'code': 0, 'data': out})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/extensions/data', methods=['POST'])
    @require_auth
    def ui_ext_users_data():
        """批量获取多个用户的扩展数据（当前页渲染用）"""
        try:
            body = request.get_json(silent=True) or {}
            ids = [int(x) for x in (body.get('ids') or []) if str(x).isdigit()]
            out = {}
            for uid in ids:
                out[str(uid)] = framework.plugin_loader.call_ui_extensions('users', uid)
            return jsonify({'code': 0, 'data': out})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/<int:gid>/extensions', methods=['GET'])
    @require_auth
    def ui_ext_group_detail(gid):
        """单个群的扩展详情（详情弹窗用，含 column+panel 全量）"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.call_ui_extensions('groups', gid)})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:uid>/extensions', methods=['GET'])
    @require_auth
    def ui_ext_user_detail(uid):
        """单个用户的扩展详情（详情弹窗用）"""
        try:
            return jsonify({'code': 0, 'data': framework.plugin_loader.call_ui_extensions('users', uid)})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 群级插件开关 ----

    @app.route('/api/plugins/group-settings', methods=['GET'])
    @require_auth
    def list_group_plugin_settings():
        """获取所有群级插件开关设置"""
        try:
            rows = framework.plugin_loader.get_group_plugin_settings()
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error(f"获取群级插件设置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/group/<int:group_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_group_plugin(plugin_name, group_id):
        """启用/禁用插件在指定群的状态"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        data = request.get_json(silent=True) or {}
        enabled = data.get('enabled', True)

        try:
            framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, enabled)
            action = 'enable_group_plugin' if enabled else 'disable_group_plugin'
            audit_log(admin['id'], admin['username'], action, 'plugin', plugin_name,
                      {'group_id': group_id})
            return jsonify({
                'code': 0,
                'msg': f"插件 [{plugin_name}] 在群 {group_id} 已{'启用' if enabled else '禁用'}"
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件管理 ----

    @app.route('/api/plugins', methods=['GET'])
    @require_auth
    def list_plugins():
        """获取插件列表（含命令/任务计数）"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, version, author, description, priority, "
                "status, is_active, has_register, loaded_at, created_at "
                "FROM plugins ORDER BY priority ASC, created_at ASC"
            )
            # 补充运行时状态
            loaded = framework.plugin_loader.get_loaded_plugins()
            for r in rows:
                r['is_loaded'] = r['plugin_name'] in loaded
                # 幽灵插件标记：DB 有记录但代码目录/main.py 缺失（如 .bak 误注册后目录被清）
                r['dir_missing'] = not os.path.isfile(
                    os.path.join(framework.plugin_loader.plugins_dir, r['plugin_name'], 'main.py')
                )
                r['has_readme'] = os.path.isfile(
                    os.path.join(framework.plugin_loader.plugins_dat_dir, r['plugin_name'], 'README.md')
                )
                # 补充 plugin.yaml 信息
                yaml_data = framework.plugin_loader.read_plugin_yaml(r['plugin_name'])
                r['has_yaml'] = bool(yaml_data)
                r['has_github'] = bool(yaml_data.get('github', {}).get('repo'))
                r['github_repo'] = yaml_data.get('github', {}).get('repo', '')
                r['config_items'] = yaml_data.get('config', [])
                # 补充 _conf_schema.json 信息
                schema = framework.plugin_loader.read_config_schema(r['plugin_name'])
                r['has_schema'] = bool(schema)
                # 补充依赖检查信息
                dep_info = framework.plugin_loader.get_dep_status(r['plugin_name'])
                r['has_missing_deps'] = dep_info['has_missing']
                r['missing_deps'] = dep_info['missing']
                r['has_conflict'] = dep_info['has_conflict']
                r['conflicts'] = dep_info['conflicts']
                # 补充命令/任务计数
                try:
                    cmd_cnt = db.query_one(
                        "SELECT COUNT(*) as cnt FROM commands WHERE plugin_name = %s AND is_active = 1",
                        (r['plugin_name'],)
                    )
                    r['command_count'] = cmd_cnt['cnt'] if cmd_cnt else 0
                except Exception:
                    r['command_count'] = 0
                try:
                    task_cnt = db.query_one(
                        "SELECT COUNT(*) as cnt FROM tasks WHERE plugin_name = %s AND is_active = 1",
                        (r['plugin_name'],)
                    )
                    r['task_count'] = task_cnt['cnt'] if task_cnt else 0
                except Exception:
                    r['task_count'] = 0
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/upload', methods=['POST'])
    @require_auth
    def upload_plugin():
        """上传 ZIP 插件包"""
        admin = request.admin

        if 'file' not in request.files:
            return jsonify({'code': 400, 'msg': '未选择文件'}), 400

        file = request.files['file']
        if not file.filename or not file.filename.lower().endswith('.zip'):
            return jsonify({'code': 400, 'msg': '仅支持 .zip 文件'}), 400

        # 安全检查文件名
        filename = os.path.basename(file.filename)
        plugin_name = filename[:-4]  # 去掉 .zip

        # 验证插件名合法性
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '插件名只能包含字母、数字、下划线、横杠'}), 400

        target_dir = os.path.join(plugins_dir, plugin_name)

        try:
            # 读取 ZIP 内容到内存
            file_stream = io.BytesIO(file.read())

            # 验证 ZIP 结构：必须包含 main.py
            with zipfile.ZipFile(file_stream, 'r') as zf:
                names = zf.namelist()
                # 检查是否有 main.py（可能在子目录中）
                has_main = any(n.endswith('main.py') for n in names)
                if not has_main:
                    return jsonify({'code': 400, 'msg': 'ZIP 包中未找到 main.py'}), 400

                # 检查路径穿越攻击
                for name in names:
                    if '..' in name or name.startswith('/'):
                        return jsonify({'code': 400, 'msg': f'非法路径: {name}'}), 400

                # 备份旧插件（如果存在）
                if os.path.isdir(target_dir):
                    backup_dir = target_dir + f'.bak.{int(time.time())}'
                    shutil.move(target_dir, backup_dir)

                os.makedirs(target_dir, exist_ok=True)

                # 解压到目标目录
                zf.extractall(target_dir)

                # 如果解压后多了一层目录，提上来
                entries = os.listdir(target_dir)
                if len(entries) == 1 and os.path.isdir(os.path.join(target_dir, entries[0])):
                    inner = os.path.join(target_dir, entries[0])
                    for item in os.listdir(inner):
                        shutil.move(os.path.join(inner, item), os.path.join(target_dir, item))
                    os.rmdir(inner)

            file_stream.close()

            # 分离配置文件到 plugins_dat（代码留 plugins，配置留 plugins_dat）
            framework.plugin_loader.split_installed_files(plugin_name)

            # 尝试加载插件
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                dep_info = framework.plugin_loader.get_missing_deps(plugin_name)
                msg = f'插件 [{plugin_name}] 上传并加载成功'
                if dep_info['has_missing']:
                    msg += f'，但缺少依赖: {", ".join(dep_info["missing"])}'
                audit_log(admin['id'], admin['username'], 'upload_plugin',
                          'plugin', plugin_name, {'filename': filename}, 'success')
                return jsonify({'code': 0, 'msg': msg})
            else:
                # 检查是否因依赖缺失导致加载失败
                dep_info = framework.plugin_loader.get_missing_deps(plugin_name)
                if dep_info['has_missing']:
                    err_msg = f'插件 [{plugin_name}] 缺少依赖: {", ".join(dep_info["missing"])}'
                else:
                    err_msg = f'插件 [{plugin_name}] 加载失败，请检查 main.py'
                audit_log(admin['id'], admin['username'], 'upload_plugin',
                          'plugin', plugin_name, {'filename': filename}, 'failure', err_msg)
                return jsonify({'code': 500, 'msg': err_msg}), 500

        except zipfile.BadZipFile:
            return jsonify({'code': 400, 'msg': '无效的 ZIP 文件'}), 400
        except Exception as e:
            logger.error(f"上传插件失败: {e}", exc_info=True)
            audit_log(admin['id'], admin['username'], 'upload_plugin',
                      'plugin', plugin_name, None, 'failure', str(e))
            return jsonify({'code': 500, 'msg': f'上传失败: {e}'}), 500

    @app.route('/api/plugins/<plugin_name>/reload', methods=['POST'])
    @require_auth
    def reload_plugin(plugin_name):
        """重新加载插件"""
        admin = request.admin

        # 安全检查
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 先卸载
            framework.plugin_loader.unload_plugin(plugin_name)
            # 重新加载
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                # 清空路由缓存
                framework.router._invalidate_cache()
                audit_log(admin['id'], admin['username'], 'reload_plugin', 'plugin', plugin_name)
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已重新加载'})
            else:
                return jsonify({'code': 500, 'msg': '加载失败'}), 500
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/toggle', methods=['POST'])
    @require_auth
    def toggle_plugin(plugin_name):
        """启用/禁用插件"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            db.execute(
                "UPDATE plugins SET is_active = %s WHERE plugin_name = %s",
                (is_active, plugin_name)
            )
            action = 'enable_plugin' if is_active else 'disable_plugin'
            audit_log(admin['id'], admin['username'], action, 'plugin', plugin_name)

            if not is_active:
                framework.plugin_loader.unload_plugin(plugin_name)
            else:
                framework.plugin_loader.load_plugin(plugin_name)
                framework.plugin_loader.register_commands(plugin_name)

            # 清空路由缓存
            framework.router._invalidate_cache()

            return jsonify({'code': 0, 'msg': f'插件已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>', methods=['DELETE'])
    @require_auth
    def delete_plugin(plugin_name):
        """
        删除插件
        默认保留插件数据（plugins_dat/<插件名>/ 下的配置文件等）；
        请求体携带 {"delete_data": true} 时一并删除插件数据目录及 plugin.yaml 中
        声明的 managed_tables 业务表。
        """
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        body = request.get_json(silent=True) or {}
        delete_data = bool(body.get('delete_data'))

        target_dir = os.path.join(plugins_dir, plugin_name)
        dat_dir = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name)
        venv_dir = os.path.join(dat_dir, '.venv')
        try:
            # 卸载
            framework.plugin_loader.unload_plugin(plugin_name)
            # 删除代码目录
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            # 插件虚拟环境随代码清理（venv 属于运行产物，不属于用户配置）
            if os.path.isdir(venv_dir):
                shutil.rmtree(venv_dir, ignore_errors=True)
            # 是否连带删除插件数据/配置目录
            if delete_data and os.path.isdir(dat_dir):
                shutil.rmtree(dat_dir, ignore_errors=True)
            # 删除数据库记录
            db.execute("DELETE FROM plugins WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            db.execute("DELETE FROM plugin_configs WHERE plugin_name = %s", (plugin_name,))

            # 删除插件声明管理的业务表（仅在 delete_data=true 时执行）
            # 插件通过 plugin.yaml 的 managed_tables 字段声明自己创建的表
            dropped_tables = []
            if delete_data:
                yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
                managed_tables = yaml_data.get('managed_tables', []) if isinstance(yaml_data, dict) else []
                if isinstance(managed_tables, str):
                    managed_tables = [managed_tables]
                for table_name in managed_tables:
                    if not isinstance(table_name, str):
                        continue
                    # 严格校验表名：仅允许字母、数字、下划线，防 SQL 注入
                    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', table_name):
                        logger.warning(f"[{plugin_name}] 跳过非法表名: {table_name}")
                        continue
                    try:
                        db.execute(f"DROP TABLE IF EXISTS {table_name}")
                        dropped_tables.append(table_name)
                        logger.info(f"[{plugin_name}] 已删除插件业务表: {table_name}")
                    except Exception as e:
                        logger.warning(f"[{plugin_name}] 删除表 {table_name} 失败: {e}")

            audit_log(admin['id'], admin['username'], 'delete_plugin', 'plugin', plugin_name,
                      {'delete_data': delete_data, 'dropped_tables': dropped_tables}, 'success')
            msg = f'插件 [{plugin_name}] 已删除'
            if not delete_data and os.path.isdir(dat_dir) and os.listdir(dat_dir):
                msg += '（配置文件已保留在 plugins_dat，可手动清理）'
            if dropped_tables:
                msg += f'（已清理业务表: {", ".join(dropped_tables)}）'
            return jsonify({'code': 0, 'msg': msg})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/install_deps', methods=['POST'])
    @require_auth
    def install_plugin_deps(plugin_name):
        """一键安装插件缺失的 Python 依赖"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            result = framework.plugin_loader.install_missing_deps(plugin_name)
            if result['success']:
                if result['installed']:
                    audit_log(admin['id'], admin['username'], 'install_deps',
                              'plugin', plugin_name, {'installed': result['installed']}, 'success')
                    return jsonify({
                        'code': 0,
                        'msg': f"已安装依赖: {', '.join(result['installed'])}"
                    })
                else:
                    return jsonify({'code': 0, 'msg': '所有依赖已满足'})
            else:
                audit_log(admin['id'], admin['username'], 'install_deps',
                          'plugin', plugin_name,
                          {'installed': result['installed'], 'failed': result['failed']}, 'failure')
                msg = ''
                if result['installed']:
                    msg += f"已安装: {', '.join(result['installed'])}；"
                msg += f"安装失败: {', '.join(result['failed'])}"
                return jsonify({'code': 500, 'msg': msg}), 500
        except Exception as e:
            logger.error(f"安装依赖失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/create_isolated_env', methods=['POST'])
    @require_auth
    def create_plugin_isolated_env(plugin_name):
        """为插件创建隔离虚拟环境（解决版本冲突）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            result = framework.plugin_loader.create_isolated_env(plugin_name)
            if result['success']:
                audit_log(admin['id'], admin['username'], 'create_isolated_env',
                          'plugin', plugin_name,
                          {'venv_path': result.get('venv_path', ''),
                           'installed': result.get('installed', [])}, 'success')
                msg = f"隔离环境已创建"
                if result.get('installed'):
                    msg += f"，已安装依赖: {', '.join(result['installed'])}"
                return jsonify({'code': 0, 'msg': msg, 'data': result})
            else:
                audit_log(admin['id'], admin['username'], 'create_isolated_env',
                          'plugin', plugin_name,
                          {'error': result.get('error', '')}, 'failure')
                return jsonify({'code': 500, 'msg': f"创建隔离环境失败: {result.get('error', '')}"}), 500
        except Exception as e:
            logger.error(f"创建隔离环境失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/venv_usage', methods=['GET'])
    @require_auth
    def venv_usage():
        """获取所有插件的 .venv 隔离环境磁盘占用情况"""
        try:
            data = framework.plugin_loader.scan_venv_usage()
            return jsonify({'code': 0, 'data': data})
        except Exception as e:
            logger.error(f"扫描 venv 占用失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/isolated_env', methods=['DELETE'])
    @require_auth
    def delete_plugin_isolated_env(plugin_name):
        """删除插件的隔离虚拟环境（清理磁盘空间）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 先确保插件已卸载，避免删除正在使用的 venv
            loaded = framework.plugin_loader.get_loaded_plugins()
            if plugin_name in loaded:
                return jsonify({
                    'code': 400,
                    'msg': f'插件 [{plugin_name}] 正在运行，请先卸载再清理隔离环境'
                }), 400

            result = framework.plugin_loader.remove_isolated_env(plugin_name)
            if result['success']:
                audit_log(admin['id'], admin['username'], 'delete_isolated_env',
                          'plugin', plugin_name, {}, 'success')
                return jsonify({'code': 0, 'msg': result.get('msg', '隔离环境已删除')})
            else:
                audit_log(admin['id'], admin['username'], 'delete_isolated_env',
                          'plugin', plugin_name,
                          {'error': result.get('error', '')}, 'failure')
                return jsonify({
                    'code': 500,
                    'msg': f"清理失败: {result.get('error', '未知错误')}"
                }), 500
        except Exception as e:
            logger.error(f"删除隔离环境失败 [{plugin_name}]: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/readme', methods=['GET'])
    @require_auth
    def get_plugin_readme(plugin_name):
        """获取插件 README"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        readme_path = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name, 'README.md')
        if not os.path.isfile(readme_path):
            return jsonify({'code': 404, 'msg': '该插件没有 README.md'}), 404

        try:
            with open(readme_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return jsonify({'code': 0, 'data': {'content': content, 'plugin_name': plugin_name}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/config', methods=['GET'])
    @require_auth
    def get_plugin_config(plugin_name):
        """获取插件的 plugin.yaml 配置及配置文件列表"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 读取 plugin.yaml
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            # 获取配置文件列表
            config_files = framework.plugin_loader.get_plugin_config_files(plugin_name)

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'yaml': yaml_data,
                    'has_yaml': bool(yaml_data),
                    'github': yaml_data.get('github', {}),
                    'config_items': yaml_data.get('config', []),
                    'docs': yaml_data.get('docs', []),
                    'dependencies': yaml_data.get('dependencies', {}),
                    'config_files': config_files,
                    # 补充依赖安装状态
                    'dep_status': framework.plugin_loader.get_missing_deps(plugin_name),
                }
            })
        except Exception as e:
            logger.error(f"获取插件配置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/file/<filename>', methods=['GET'])
    @require_auth
    def get_plugin_file(plugin_name, filename):
        """读取插件目录下的指定文件内容"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400
        # 安全检查
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'code': 400, 'msg': '非法文件名'}), 400

        try:
            content = framework.plugin_loader.read_plugin_file(plugin_name, filename)
            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'filename': filename,
                    'content': content,
                }
            })
        except FileNotFoundError:
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/check_update', methods=['GET'])
    @require_auth
    def check_plugin_update(plugin_name):
        """检查插件是否有 GitHub 更新（基于版本号对比，借鉴 Koishi registry 版本机制）"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')
            branch = github.get('branch', 'main')

            # 当前本地版本（来自 plugin.yaml 或 __plugin_meta__）
            current_version = yaml_data.get('version', 'unknown')

            # 从 registry.json 读取官方最新版本（优先），否则回退 GitHub commit
            latest_version = None
            latest_sha = ''
            commit_msg = ''
            commit_date = ''
            author = ''
            has_update = False

            reg = _load_market_registry()
            reg_entry = next((p for p in reg.get('plugins', []) if p['name'] == plugin_name), None)
            if reg_entry and reg_entry.get('version'):
                latest_version = reg_entry['version']
                # 版本号对比（简单字符串/semver 比较）
                has_update = _version_gt(latest_version, current_version)
            else:
                # registry 无该插件：回退到 GitHub commits API（带代理候选）
                if not repo:
                    return jsonify({'code': 400, 'msg': '该插件未在官方市场注册，且 plugin.yaml 缺少 github.repo'}), 400
                if repo.startswith('https://github.com/'):
                    repo = repo.replace('https://github.com/', '').rstrip('/')
                elif repo.startswith('http://github.com/'):
                    repo = repo.replace('http://github.com/', '').rstrip('/')
                api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
                headers = {'Accept': 'application/vnd.github.v3+json'}
                last_err = ''
                for cand in _github_url_candidates(api_url):
                    try:
                        resp = requests.get(cand, headers=headers, timeout=15)
                        if resp.status_code == 200:
                            try:
                                data = resp.json()
                            except ValueError:
                                last_err = f'非 JSON 响应: {cand}'
                                continue
                            latest_sha = data.get('sha', '')[:7]
                            commit_msg = data.get('commit', {}).get('message', '').split('\n')[0]
                            commit_date = data.get('commit', {}).get('author', {}).get('date', '')
                            author = data.get('commit', {}).get('author', {}).get('name', '')
                            has_update = True
                            break
                        last_err = f'HTTP {resp.status_code}'
                    except Exception as e:
                        last_err = str(e)
                if not latest_sha:
                    return jsonify({'code': 500, 'msg': f'GitHub API 获取失败: {last_err}'}), 500

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'repo': repo,
                    'branch': branch,
                    'current_version': current_version,
                    'latest_version': latest_version or 'unknown',
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': has_update,
                }
            })
        except requests.exceptions.Timeout:
            return jsonify({'code': 500, 'msg': 'GitHub API 请求超时'}), 500
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    def _version_gt(a, b):
        """语义化版本比较：a > b 返回 True（无法解析时按字符串比较）"""

        def _parse(v):
            v = str(v).lstrip('vV^~>=< ').strip()
            parts = re.split(r'[.\-+]', v)
            out = []
            for p in parts:
                m = re.match(r'(\d+)', p)
                out.append(int(m.group(1)) if m else 0)
            return out

        try:
            pa, pb = _parse(a), _parse(b)
            n = max(len(pa), len(pb))
            pa += [0] * (n - len(pa))
            pb += [0] * (n - len(pb))
            return pa > pb
        except Exception:
            return str(a) != str(b)

    def _load_market_registry() -> dict:
        """加载官方插件市场 registry（默认源 + 镜像源 + 自定义源兜底），返回 {'plugins': [...]}"""
        sources = [_DEFAULT_MARKET] + _MIRROR_MARKETS + _read_market_sources_custom()
        last_err = ''
        for src in sources:
            try:
                plugins = _fetch_market_source(src)
                if plugins:
                    return {'plugins': plugins}
            except Exception as e:
                last_err = str(e)
        logger.warning(f"加载官方市场 registry 失败: {last_err}")
        return {'plugins': []}

    def _persist_plugin_github_meta(plugin_name: str, repo: str, branch: str, sub_path: str) -> None:
        """安装/更新第三方源插件后，把 github 源信息写回 plugins_dat/<name>/plugin.yaml 的 github 段，
        使后续 check_plugin_update / update 能定位仓库（借鉴 AstrBot：安装即记录来源）"""
        try:
            dat_yaml = os.path.join(framework.plugin_loader.plugins_dat_dir, plugin_name, 'plugin.yaml')
            data = {}
            if os.path.isfile(dat_yaml):
                with open(dat_yaml, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
            gh = data.get('github') or {}
            if not isinstance(gh, dict):
                gh = {}
            gh.setdefault('repo', repo)
            gh.setdefault('branch', branch or 'main')
            gh.setdefault('path', sub_path or '/')
            data['github'] = gh
            os.makedirs(os.path.dirname(dat_yaml), exist_ok=True)
            with open(dat_yaml, 'w', encoding='utf-8') as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.warning(f"写回插件 github 元信息失败 {plugin_name}: {e}")

    @app.route('/api/plugins/<plugin_name>/update', methods=['POST'])
    @require_auth
    def update_plugin_from_github(plugin_name):
        """从 GitHub 更新插件代码"""
        admin = request.admin

        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')
            branch = github.get('branch', 'main')
            sub_path = github.get('path', '/')

            if not repo:
                return jsonify({'code': 400, 'msg': '该插件未配置 GitHub 更新源'}), 400

            # 规范化 repo
            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            # 下载插件代码（优先文件树方式，回退 ZIP）
            target_dir = os.path.join(plugins_dir, plugin_name)

            # 卸载当前插件
            framework.plugin_loader.unload_plugin(plugin_name)

            # 备份旧代码
            backup_dir = None
            if os.path.isdir(target_dir):
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            try:
                # 复用市场安装逻辑：优先单插件 zip（gh-pages/packages/<name>.zip），失败回退文件树/整仓
                ok_dl, dl_msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
                if not ok_dl:
                    raise RuntimeError(f'更新下载失败: {dl_msg}')
            except Exception as e:
                # 下载失败：回滚备份
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                raise e

            # 分离配置文件到 plugins_dat（保留用户已有配置不被覆盖）
            framework.plugin_loader.split_installed_files(plugin_name)

            # 重新加载插件
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                # 更新成功：清理 .bak 残留，避免被 discover() 当成插件加载
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理更新前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'success')
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已从 GitHub 更新并重新加载'})
            else:
                # 新代码加载失败：恢复旧代码备份
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                    logger.warning(f"[{plugin_name}] 新代码加载失败，已恢复旧代码备份")
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'failure', '加载失败')
                return jsonify({'code': 500, 'msg': f'代码已下载但加载失败，请检查 main.py'}), 500

        except zipfile.BadZipFile:
            return jsonify({'code': 400, 'msg': '下载的 ZIP 文件无效'}), 400
        except Exception as e:
            logger.error(f"GitHub 更新插件失败: {e}", exc_info=True)
            audit_log(admin['id'], admin['username'], 'update_plugin_github',
                      'plugin', plugin_name, None, 'failure', str(e))
            return jsonify({'code': 500, 'msg': f'更新失败: {e}'}), 500

    # ---- 插件市场（Registry JSON）----

    _MARKET_CACHE_FILE = 'plugin_market_cache.json'
    _MARKET_CACHE_TTL = 300  # 秒

    @app.route('/api/plugins/market', methods=['GET'])
    @require_auth
    def plugin_market():
        """获取在线插件市场列表（默认源 + 自定义源，带缓存）"""
        force = request.args.get('force_refresh', 'false').lower() == 'true'
        cache_path = os.path.join(_data_dir(), _MARKET_CACHE_FILE)

        if not force and os.path.isfile(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if time.time() - cache.get('ts', 0) < _MARKET_CACHE_TTL:
                    return jsonify({'code': 0, 'data': cache['data']})
            except Exception:
                pass

        sources = [_DEFAULT_MARKET] + _read_market_sources_custom()
        all_plugins, errors = [], []
        for src in sources:
            try:
                if src.get('url') == _DEFAULT_MARKET['url']:
                    # 默认源：按 配置加速代理 → 内置 ghproxy 镜像 → 直连 顺序尝试
                    ok_src = False
                    for cand in _github_url_candidates(_DEFAULT_MARKET['url']):
                        try:
                            all_plugins.extend(_fetch_market_source(
                                {'name': src.get('name', '默认源'), 'url': cand}))
                            ok_src = True
                            break
                        except Exception:
                            continue
                    if not ok_src:
                        errors.append(f"{src.get('name', src.get('url', ''))}: 所有源均获取失败")
                else:
                    # 第三方自定义源：github 类地址同样走加速代理候选，普通 HTTP 源直连
                    src_url = src.get('url', '')
                    cands = _github_url_candidates(src_url) if 'github' in src_url else [src_url]
                    ok_src = False
                    for cand in cands:
                        try:
                            all_plugins.extend(_fetch_market_source(
                                {'name': src.get('name', '自定义源'), 'url': cand}))
                            ok_src = True
                            break
                        except Exception:
                            continue
                    if not ok_src:
                        errors.append(f"{src.get('name', src_url)}: 所有源均获取失败")
            except Exception as e:
                errors.append(f"{src.get('name', src.get('url', ''))}: {str(e)[:120]}")

        installed = _market_installed_set()
        for p in all_plugins:
            p['installed'] = p.get('name') in installed

        result = {'plugins': all_plugins, 'errors': errors, 'sources': sources}
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'data': result}, f, ensure_ascii=False)
        except Exception:
            pass
        # 网络全挂时回退缓存（缓存 + 提示可能不是最新）
        if not all_plugins and os.path.isfile(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if cache.get('data', {}).get('plugins'):
                    cache_data = cache['data']
                    cache_data['errors'] = (cache_data.get('errors') or []) + ['网络获取失败，展示缓存数据（可能不是最新）']
                    return jsonify({'code': 0, 'data': cache_data, 'cached': True})
            except Exception:
                pass
        return jsonify({'code': 0, 'data': result})

    @app.route('/api/plugins/market/sources', methods=['GET'])
    @require_auth
    def plugin_market_sources_get():
        """获取插件源列表（默认源 + 自定义源）"""
        return jsonify({'code': 0, 'data': {
            'default': _DEFAULT_MARKET,
            'custom': _read_market_sources_custom(),
        }})

    @app.route('/api/plugins/market/sources', methods=['POST'])
    @require_super
    def plugin_market_sources_save():
        """保存自定义插件源列表"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        sources = data.get('sources', [])
        if not isinstance(sources, list):
            return jsonify({'code': 400, 'msg': 'sources 必须是数组'}), 400
        cleaned = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            name = str(s.get('name') or '').strip()
            url = str(s.get('url') or '').strip()
            if name and url.startswith(('http://', 'https://')):
                cleaned.append({'name': name, 'url': url})
        _save_market_sources_custom(cleaned)
        audit_log(admin['id'], admin['username'], 'update_market_sources', 'plugin', 'market', {'sources': cleaned})
        return jsonify({'code': 0, 'msg': f'已保存 {len(cleaned)} 个自定义插件源'})

    @app.route('/api/plugins/market/install', methods=['POST'])
    @require_auth
    def plugin_market_install():
        """从市场安装插件（下载 ZIP 到插件目录并加载）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        plugin_name = str(data.get('name') or '').strip()
        repo = str(data.get('repo') or '').strip()
        branch = str(data.get('branch') or 'main').strip() or 'main'
        sub_path = str(data.get('sub_path') or '/')

        if not plugin_name or not re.match(r'^[\w\-]+$', plugin_name):
            return jsonify({'code': 400, 'msg': '非法的插件名'}), 400
        if not repo:
            return jsonify({'code': 400, 'msg': '缺少仓库地址（repo）'}), 400

        try:
            target_dir = os.path.join(plugins_dir, plugin_name)
            backup_dir = None
            # 已存在则先卸载并备份
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                framework.plugin_loader.unload_plugin(plugin_name)
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            ok, msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
            if not ok:
                # 下载失败：恢复旧代码备份
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                    logger.warning(f"[{plugin_name}] 安装下载失败，已恢复旧代码")
                return jsonify({'code': 500, 'msg': f'下载失败: {msg}'}), 500

            # 分离配置文件到 plugins_dat
            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                # 记录插件来源（repo/branch/sub_path），供后续 check/update 定位仓库
                _persist_plugin_github_meta(plugin_name, repo, branch, sub_path)
                # 安装成功：清理 .bak 残留，避免被 discover() 当成插件加载
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理安装前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'install_plugin_market',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch, 'sub_path': sub_path})
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 安装成功并已加载'})
            # 加载失败：恢复旧代码备份
            if backup_dir and os.path.isdir(backup_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
                shutil.move(backup_dir, target_dir)
                logger.warning(f"[{plugin_name}] 新代码加载失败，已恢复旧代码备份")
            audit_log(admin['id'], admin['username'], 'install_plugin_market',
                      'plugin', plugin_name, {'repo': repo}, 'failure', '加载失败')
            return jsonify({'code': 500, 'msg': '代码已下载但加载失败，请检查 main.py'}), 500
        except Exception as e:
            logger.error(f"市场安装插件失败: {e}", exc_info=True)
            return jsonify({'code': 500, 'msg': f'安装失败: {e}'}), 500

    # ---- 命令管理 ----

    @app.route('/api/commands', methods=['GET'])
    @require_auth
    def list_commands():
        """获取静态命令列表（包含别名、描述、启停状态、权限要求）"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, pattern, alias, description, priority, handler, "
                "is_dynamic, is_active, hit_count, require_level, created_at "
                "FROM commands ORDER BY plugin_name, priority ASC, created_at ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/commands/dynamic', methods=['GET'])
    @require_auth
    def list_dynamic_commands():
        """
        获取动态命令列表（插件注册的 dynamic=True 命令，只读展示）
        动态命令由插件通过 ctx.command(dynamic=True) 注册，存储在 commands 表
        """
        try:
            rows = db.query(
                "SELECT id, plugin_name, pattern, alias, description, priority, handler, "
                "is_dynamic, is_active, hit_count, created_at "
                "FROM commands WHERE is_dynamic = 1 "
                "ORDER BY plugin_name, priority ASC, created_at ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 关键词自动回复管理（dynamic_commands 表，系统级动态命令）----

    _KEYWORD_MATCH_TYPES = ('exact', 'prefix', 'contains', 'regex')

    def _refresh_router_keywords():
        """使 router 关键词规则表立即重建（增删改后调用）"""
        try:
            router = getattr(framework, 'router', None)
            if router is not None:
                router._invalidate_cache()
        except Exception:
            pass

    def _validate_keyword_payload(data: dict, partial: bool = False) -> tuple:
        """校验关键词回复参数，返回 (error_msg_or_None, cleaned_dict)"""
        out = {}
        if 'keyword' in data or not partial:
            keyword = (data.get('keyword') or '').strip()
            if not keyword:
                return '触发关键词不能为空', None
            if len(keyword) > 200:
                return '触发关键词过长（最多 200 字符）', None
            out['keyword'] = keyword
        if 'response' in data or not partial:
            response = (data.get('response') or '').strip()
            if not response:
                return '回复内容不能为空', None
            out['response'] = response
        if 'match_type' in data or not partial:
            match_type = (data.get('match_type') or 'exact').strip().lower()
            if match_type not in _KEYWORD_MATCH_TYPES:
                return f"无效的匹配方式（允许: {'/'.join(_KEYWORD_MATCH_TYPES)}）", None
            if match_type == 'regex':
                try:
                    re.compile(out.get('keyword', data.get('keyword') or ''))
                except re.error as e:
                    return f'正则表达式无效: {e}', None
            out['match_type'] = match_type
        if 'plugin_name' in data:
            out['plugin_name'] = (data.get('plugin_name') or 'system').strip()[:50] or 'system'
        if 'handler' in data:
            handler = (data.get('handler') or '').strip()
            if len(handler) > 100:
                return 'handler 过长（最多 100 字符，格式 plugin:func）', None
            if handler and ':' not in handler:
                return 'handler 格式应为 plugin:func', None
            out['handler'] = handler
        if 'is_active' in data:
            out['is_active'] = 1 if data.get('is_active') else 0
        return None, out

    @app.route('/api/dynamic-commands', methods=['GET'])
    @require_auth
    def list_keyword_replies():
        """获取关键词自动回复列表（dynamic_commands 表）"""
        try:
            rows = db.query(
                "SELECT id, keyword, response, match_type, handler, plugin_name, "
                "is_active, hit_count, created_at, updated_at "
                "FROM dynamic_commands ORDER BY id DESC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands', methods=['POST'])
    @require_auth
    def create_keyword_reply():
        """新增关键词自动回复规则"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        err, cleaned = _validate_keyword_payload(data)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        try:
            kw_id = db.insert(
                "INSERT INTO dynamic_commands "
                "(keyword, response, match_type, handler, plugin_name, is_active) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (cleaned['keyword'], cleaned['response'], cleaned['match_type'],
                 cleaned.get('handler', ''),
                 cleaned.get('plugin_name', 'system'),
                 cleaned.get('is_active', 1))
            )
            audit_log(admin['id'], admin['username'], 'create_keyword_reply',
                      'dynamic_command', str(kw_id), cleaned)
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '关键词回复已添加', 'data': {'id': kw_id}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>', methods=['PUT'])
    @require_auth
    def update_keyword_reply(kw_id):
        """更新关键词自动回复规则"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        err, cleaned = _validate_keyword_payload(data, partial=True)
        if err:
            return jsonify({'code': 400, 'msg': err}), 400
        if not cleaned:
            return jsonify({'code': 400, 'msg': '没有需要更新的字段'}), 400
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            sets = ", ".join(f"{k} = %s" for k in cleaned)
            db.execute(
                f"UPDATE dynamic_commands SET {sets} WHERE id = %s",
                (*cleaned.values(), kw_id)
            )
            audit_log(admin['id'], admin['username'], 'update_keyword_reply',
                      'dynamic_command', str(kw_id), cleaned)
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '关键词回复已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_keyword_reply(kw_id):
        """启用/禁用关键词自动回复"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            db.execute(
                "UPDATE dynamic_commands SET is_active = %s WHERE id = %s",
                (is_active, kw_id))
            action = 'enable' if is_active else 'disable'
            audit_log(admin['id'], admin['username'], f'{action}_keyword_reply',
                      'dynamic_command', str(kw_id), {'keyword': row['keyword']})
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/dynamic-commands/<int:kw_id>', methods=['DELETE'])
    @require_auth
    def delete_keyword_reply(kw_id):
        """删除关键词自动回复规则"""
        admin = request.admin
        try:
            row = db.query_one(
                "SELECT id, keyword FROM dynamic_commands WHERE id = %s", (kw_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '规则不存在'}), 404
            db.execute("DELETE FROM dynamic_commands WHERE id = %s", (kw_id,))
            audit_log(admin['id'], admin['username'], 'delete_keyword_reply',
                      'dynamic_command', str(kw_id), {'keyword': row['keyword']})
            _refresh_router_keywords()
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件配置读写 API ----

    @app.route('/api/plugins/<plugin_name>/config_schema', methods=['GET'])
    @require_auth
    def get_plugin_config_schema(plugin_name):
        """获取插件的配置 schema 和当前配置值"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 读取 _conf_schema.json
            schema = framework.plugin_loader.read_config_schema(plugin_name)
            # 读取当前配置值
            config_rows = db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s",
                (plugin_name,)
            )
            config_values = {}
            for r in config_rows:
                try:
                    config_values[r['config_key']] = json.loads(r['config_value'])
                except (json.JSONDecodeError, TypeError):
                    config_values[r['config_key']] = r['config_value']

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'schema': schema,
                    'values': config_values,
                    'has_schema': bool(schema),
                }
            })
        except Exception as e:
            logger.error(f"获取插件配置 schema 失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/plugins/<plugin_name>/config_schema', methods=['PUT'])
    @require_auth
    def update_plugin_config(plugin_name):
        """更新插件配置值（用户在 Web UI 修改配置项）"""
        admin = request.admin
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({'code': 400, 'msg': '缺少配置数据'}), 400

        try:
            # 获取 schema 用于校验
            schema = framework.plugin_loader.read_config_schema(plugin_name)

            updated_keys = []
            for key, value in data.items():
                # 校验 key 是否在 schema 中
                if schema and key not in schema:
                    continue

                # 类型转换校验
                if schema:
                    spec = schema.get(key, {})
                    val_type = spec.get('type', 'string')
                    if val_type == 'int':
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            value = spec.get('default', 0)
                    elif val_type == 'float':
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            value = spec.get('default', 0.0)
                    elif val_type == 'bool' or val_type == 'boolean':
                        if isinstance(value, str):
                            value = value.lower() in ('true', '1', 'yes', 'on')
                        else:
                            value = bool(value)

                config_value = json.dumps(value, ensure_ascii=False)
                # UPSERT
                existing = db.query_one(
                    "SELECT id FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                    (plugin_name, key)
                )
                if existing:
                    db.execute(
                        "UPDATE plugin_configs SET config_value = %s WHERE plugin_name = %s AND config_key = %s",
                        (config_value, plugin_name, key)
                    )
                else:
                    db.execute(
                        "INSERT INTO plugin_configs (plugin_name, config_key, config_value) VALUES (%s, %s, %s)",
                        (plugin_name, key, config_value)
                    )
                updated_keys.append(key)

            audit_log(admin['id'], admin['username'], 'update_plugin_config',
                      'plugin', plugin_name, {'keys': updated_keys})
            return jsonify({'code': 0, 'msg': f'已更新 {len(updated_keys)} 项配置'})
        except Exception as e:
            logger.error(f"更新插件配置失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 静态命令管理（别名/启停）----

    @app.route('/api/commands/<int:cmd_id>/alias', methods=['PUT'])
    @require_auth
    def update_command_alias(cmd_id):
        """更新静态命令的别名/描述/权限"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        alias = data.get('alias', '').strip()
        description = data.get('description', '').strip()
        require_level = data.get('require_level', '').strip()

        # 校验权限等级
        if require_level and require_level not in ('', 'admin', 'super'):
            return jsonify({'code': 400, 'msg': '无效的权限等级（允许: admin/super）'}), 400

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM commands WHERE id = %s", (cmd_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '命令不存在'}), 404

            db.execute(
                "UPDATE commands SET alias = %s, description = %s, require_level = %s WHERE id = %s",
                (alias if alias else None, description if description else None,
                 require_level, cmd_id)
            )
            audit_log(admin['id'], admin['username'], 'update_command_alias',
                      'command', str(cmd_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler'],
                       'alias': alias, 'description': description, 'require_level': require_level})
            return jsonify({'code': 0, 'msg': '命令已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/commands/<int:cmd_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_static_command(cmd_id):
        """启用/禁用静态命令"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM commands WHERE id = %s", (cmd_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '命令不存在'}), 404

            db.execute("UPDATE commands SET is_active = %s WHERE id = %s", (is_active, cmd_id))
            action = 'enable' if is_active else 'disable'
            audit_log(admin['id'], admin['username'], f'{action}_static_command',
                      'command', str(cmd_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件命令查看（插件配置弹窗展示触发命令）----

    @app.route('/api/plugins/<plugin_name>/commands', methods=['GET'])
    @require_auth
    def get_plugin_commands(plugin_name):
        """获取指定插件注册的所有命令"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            # 静态命令（is_dynamic=0）
            static_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, require_level, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 0 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )

            # 动态命令（is_dynamic=1，插件以 dynamic=True 注册）
            dynamic_cmds = db.query(
                "SELECT id, pattern, alias, description, priority, handler, "
                "is_active, hit_count, created_at FROM commands "
                "WHERE plugin_name = %s AND is_dynamic = 1 "
                "ORDER BY priority ASC, created_at ASC",
                (plugin_name,)
            )

            # 定时任务
            tasks = db.query(
                "SELECT id, cron_expression, handler, description, is_active, "
                "last_run_at, run_count, last_status FROM tasks "
                "WHERE plugin_name = %s ORDER BY id ASC",
                (plugin_name,)
            )

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'static_commands': static_cmds,
                    'dynamic_commands': dynamic_cmds,
                    'tasks': tasks,
                }
            })
        except Exception as e:
            logger.error(f"获取插件命令失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 用户/群管理 ----

    @app.route('/api/users', methods=['GET'])
    @require_auth
    def list_users():
        """获取用户列表（含角色信息）"""
        page = int(request.args.get('page', 1))
        size = min(int(request.args.get('size', 50)), 200)
        offset = (page - 1) * size
        keyword = request.args.get('keyword', '').strip()

        try:
            if keyword:
                rows = db.query(
                    "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
                    "first_seen_at, last_active_at FROM users "
                    "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s "
                    "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', size, offset)
                )
                total_row = db.query_one(
                    "SELECT COUNT(*) as cnt FROM users "
                    "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s",
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%')
                )
            else:
                rows = db.query(
                    "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
                    "first_seen_at, last_active_at FROM users "
                    "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
                    (size, offset)
                )
                total_row = db.query_one("SELECT COUNT(*) as cnt FROM users")

            return jsonify({
                'code': 0,
                'data': rows,
                'total': total_row['cnt'] if total_row else 0,
                'page': page,
                'size': size
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:user_id>/role', methods=['PUT'])
    @require_super
    def set_user_role(user_id):
        """设置用户角色（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        role = data.get('role', '').strip()

        if role not in ('super', ''):
            return jsonify({'code': 400, 'msg': '角色值无效，仅支持 super 或空字符串'}), 400

        try:
            row = db.query_one("SELECT id, nickname FROM users WHERE user_id = %s", (user_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '用户不存在'}), 404

            db.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))
            label = '超级管理员' if role == 'super' else '普通用户'
            audit_log(admin['id'], admin['username'], 'set_user_role', 'user', str(user_id),
                      {'role': role, 'nickname': row['nickname']})
            return jsonify({'code': 0, 'msg': f'用户 [{row["nickname"]}] 角色已设为 {label}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/users/<int:user_id>/blacklist', methods=['POST'])
    @require_auth
    def toggle_user_blacklist(user_id):
        """拉黑/取消拉黑用户"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_blacklist = 1 if data.get('is_blacklist') else 0

        try:
            db.execute("UPDATE users SET is_blacklist = %s WHERE user_id = %s", (is_blacklist, user_id))
            action = 'blacklist_user' if is_blacklist else 'unblacklist_user'
            audit_log(admin['id'], admin['username'], action, 'user', str(user_id))
            return jsonify({'code': 0, 'msg': f'已{"拉黑" if is_blacklist else "取消拉黑"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups', methods=['GET'])
    @require_auth
    def list_groups():
        """获取群列表"""
        try:
            rows = db.query(
                "SELECT id, group_id, group_name, member_count, max_member_count, "
                "is_active, is_blacklist, join_at FROM groups_info ORDER BY is_active DESC, group_id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/groups/<int:group_id>/blacklist', methods=['POST'])
    @require_auth
    def toggle_group_blacklist(group_id):
        """拉黑/取消拉黑群"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_blacklist = 1 if data.get('is_blacklist') else 0

        try:
            db.execute("UPDATE groups_info SET is_blacklist = %s WHERE group_id = %s", (is_blacklist, group_id))
            action = 'blacklist_group' if is_blacklist else 'unblacklist_group'
            audit_log(admin['id'], admin['username'], action, 'group', str(group_id))
            return jsonify({'code': 0, 'msg': f'已{"拉黑" if is_blacklist else "取消拉黑"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 定时任务 ----

    @app.route('/api/tasks', methods=['GET'])
    @require_auth
    def list_tasks():
        """获取定时任务列表"""
        try:
            rows = db.query(
                "SELECT id, plugin_name, cron_expression, handler, description, "
                "is_active, last_run_at, next_run_at, run_count, last_status, created_at "
                "FROM tasks ORDER BY plugin_name, id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>/toggle', methods=['POST'])
    @require_auth
    def toggle_task(task_id):
        """启用/禁用定时任务"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        is_active = 1 if data.get('is_active') else 0

        try:
            row = db.query_one("SELECT id, plugin_name, handler FROM tasks WHERE id = %s", (task_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            db.execute("UPDATE tasks SET is_active = %s WHERE id = %s", (is_active, task_id))

            # 通过调度器实际启停
            scheduler = framework.scheduler
            if scheduler:
                task_key = f"plugin_{row['plugin_name']}_{task_id}"
                if is_active:
                    scheduler.resume_task(task_key)
                else:
                    scheduler.pause_task(task_key)

            action = 'enable_task' if is_active else 'disable_task'
            audit_log(admin['id'], admin['username'], action, 'task', str(task_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': f'已{"启用" if is_active else "禁用"}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>/trigger', methods=['POST'])
    @require_auth
    def trigger_task(task_id):
        """手动触发定时任务立即执行"""
        admin = request.admin

        try:
            row = db.query_one(
                "SELECT id, plugin_name, handler, is_active FROM tasks WHERE id = %s",
                (task_id,)
            )
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            # 获取插件模块的 handler 函数
            module = framework.plugin_loader.get_plugin_module(row['plugin_name'])
            if module is None:
                return jsonify({'code': 500, 'msg': f'插件 [{row["plugin_name"]}] 未加载'}), 500

            handler = getattr(module, row['handler'], None)
            if handler is None or not callable(handler):
                return jsonify({'code': 500, 'msg': f'处理函数 {row["handler"]} 不存在'}), 500

            # 执行任务（支持 async handler）
            import asyncio
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    loop = getattr(framework, 'loop', None)
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(result, loop).result(timeout=120)
                    else:
                        asyncio.run(result)
                db.execute(
                    "UPDATE tasks SET last_run_at=NOW(), run_count=run_count+1, last_status='success' WHERE id=%s",
                    (task_id,)
                )
            except Exception as e:
                db.execute(
                    "UPDATE tasks SET last_run_at=NOW(), run_count=run_count+1, last_status='failure' WHERE id=%s",
                    (task_id,)
                )
                return jsonify({'code': 500, 'msg': f'执行失败: {e}'}), 500

            audit_log(admin['id'], admin['username'], 'trigger_task', 'task', str(task_id),
                      {'plugin': row['plugin_name'], 'handler': row['handler']})
            return jsonify({'code': 0, 'msg': '任务已触发'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks', methods=['POST'])
    @require_auth
    def create_task():
        """创建自定义定时任务"""
        admin = request.admin
        data = request.get_json(silent=True) or {}

        cron_expression = (data.get('cron_expression') or '').strip()
        description = (data.get('description') or '').strip()
        handler_name = (data.get('handler') or '').strip()

        if not cron_expression:
            return jsonify({'code': 400, 'msg': 'Cron 表达式不能为空'}), 400
        if not description:
            return jsonify({'code': 400, 'msg': '任务描述不能为空'}), 400

        parts = cron_expression.split()
        if len(parts) != 5:
            return jsonify({'code': 400, 'msg': 'Cron 表达式必须为 5 段格式（分 时 日 月 周）'}), 400

        try:
            task_id = db.insert(
                "INSERT INTO tasks (plugin_name, cron_expression, handler, description, is_active) "
                "VALUES (%s, %s, %s, %s, 1)",
                ('__web__', cron_expression, handler_name or 'custom_task', description)
            )
            audit_log(admin['id'], admin['username'], 'create_task', 'task', str(task_id),
                      {'cron': cron_expression, 'description': description})
            return jsonify({'code': 0, 'msg': '任务已创建', 'data': {'id': task_id}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
    @require_auth
    def delete_task(task_id):
        """删除定时任务"""
        admin = request.admin

        try:
            row = db.query_one("SELECT id, plugin_name, description FROM tasks WHERE id = %s", (task_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '任务不存在'}), 404

            # 插件创建的任务给出提示
            plugin_name = row['plugin_name']
            if plugin_name != '__web__':
                return jsonify({
                    'code': 400,
                    'msg': f'该任务由插件 [{plugin_name}] 注册，请在插件管理页卸载插件或联系插件开发者'
                }), 400

            # 从调度器移除
            scheduler = framework.scheduler
            if scheduler:
                task_key = f"plugin_{plugin_name}_{task_id}"
                try:
                    scheduler.pause_task(task_key)
                except Exception:
                    pass

            db.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            audit_log(admin['id'], admin['username'], 'delete_task', 'task', str(task_id),
                      {'plugin': plugin_name, 'description': row['description']})
            return jsonify({'code': 0, 'msg': '任务已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 审计日志 ----

    @app.route('/api/audit_logs', methods=['GET'])
    @require_auth
    def list_audit_logs():
        """获取审计日志"""
        page = int(request.args.get('page', 1))
        size = min(int(request.args.get('size', 50)), 200)
        offset = (page - 1) * size

        try:
            rows = db.query(
                "SELECT id, admin_id, admin_name, action, target_type, target_name, "
                "detail, ip_address, result, error_message, created_at "
                "FROM audit_logs ORDER BY id DESC LIMIT %s OFFSET %s",
                (size, offset)
            )
            total_row = db.query_one("SELECT COUNT(*) as cnt FROM audit_logs")
            return jsonify({
                'code': 0,
                'data': rows,
                'total': total_row['cnt'] if total_row else 0,
                'page': page,
                'size': size
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 运行日志（消息/连接/插件/框架，支持 SSE 实时推送）----

    @app.route('/api/runtime_logs', methods=['GET'])
    @require_auth
    def get_runtime_logs():
        """获取运行日志（支持分类/级别/关键词过滤和增量轮询）"""
        category = request.args.get('category', '')  # message|connection|plugin|system|framework
        level = request.args.get('level', '')  # DEBUG|INFO|WARN|ERROR
        keyword = request.args.get('keyword', '')
        limit = min(int(request.args.get('limit', 100)), 500)
        after_seq = int(request.args.get('after_seq', 0))

        try:
            logs = log_broker.get_logs(
                category=category or None,
                level=level or None,
                keyword=keyword or None,
                limit=limit,
                after_seq=after_seq,
            )
            return jsonify({
                'code': 0,
                'data': logs,
                'latest_seq': log_broker.get_stats().get('latest_seq', 0),
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/runtime_logs/stats', methods=['GET'])
    @require_auth
    def get_runtime_log_stats():
        """获取运行日志统计"""
        try:
            return jsonify({'code': 0, 'data': log_broker.get_stats()})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/runtime_logs/clear', methods=['POST'])
    @require_auth
    def clear_runtime_logs():
        """清空运行日志"""
        admin = request.admin
        try:
            log_broker.clear()
            audit_log(admin['id'], admin['username'],
                      'clear_runtime_logs', 'logs', None, None, 'success', None)
            return jsonify({'code': 0, 'msg': '已清空'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/logs/sse')
    def logs_sse():
        """SSE 实时日志推送端点
        支持 token 认证方式：
        1. Authorization Header（普通 fetch）
        2. ?token=xxx 查询参数（EventSource 无法传自定义 Header）
        """
        # 从查询参数取 token（兼容 EventSource）
        token = _extract_token(request) or request.args.get('token', '')
        if not token:
            return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
        admin = _verify_token(token)
        if not admin:
            return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401

        def generate(q):
            try:
                # 先发送缓存中的历史日志（最近 50 条）
                history = log_broker.get_logs(limit=50)
                for entry in history:
                    data = json.dumps(entry, ensure_ascii=False)
                    yield f"id: {entry['seq']}\ndata: {data}\n\n"

                # 持续推送新日志
                while True:
                    try:
                        entry = q.get(timeout=30)
                        data = json.dumps(entry, ensure_ascii=False)
                        yield f"id: {entry['seq']}\ndata: {data}\n\n"
                    except queue.Empty:
                        # 发送心跳保持连接
                        yield ": heartbeat\n\n"
            finally:
                log_broker.unsubscribe(q)

        # 订阅前检查上限，超出直接拒绝，避免资源耗尽
        sub_q = log_broker.subscribe()
        if sub_q is None:
            return jsonify({'code': 503, 'msg': '实时日志订阅者过多，请稍后重试'}), 503

        return Response(
            generate(sub_q),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            }
        )

    # ---- 系统配置 ----

    @app.route('/api/config', methods=['GET'])
    @require_auth
    def list_config():
        """获取系统配置"""
        try:
            rows = db.query("SELECT config_key, config_value, description, updated_by FROM system_config")
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/config/<key>', methods=['PUT'])
    @require_super
    def update_config(key):
        """更新系统配置（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        value = data.get('value')

        if value is None:
            return jsonify({'code': 400, 'msg': '缺少 value'}), 400

        try:
            db.execute(
                "UPDATE system_config SET config_value = %s, updated_by = %s WHERE config_key = %s",
                (json.dumps(value, ensure_ascii=False), admin['username'], key)
            )
            audit_log(admin['id'], admin['username'], 'update_config', 'config', key, {'value': value})
            return jsonify({'code': 0, 'msg': '配置已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- OneBot 连接设置 ----

    @app.route('/api/connection', methods=['GET'])
    @require_auth
    def get_connection():
        """获取 OneBot 连接配置与实时连接状态"""
        cfg = _read_yaml_section('onebot') or (framework.config.get('onebot') or {})
        bots = framework.ws_server.get_connected_bots()
        return jsonify({'code': 0, 'data': {
            'config': cfg,
            'status': {
                'connected_bots': bots,
                'total': len(bots),
                'ws_port': framework.config.get('onebot', {}).get('listen_port', 6830),
            },
        }})

    @app.route('/api/connection', methods=['PUT'])
    @require_super
    def update_connection():
        """更新 OneBot 连接配置（写入 config.yaml 并同步内存）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        allowed = {k: data[k] for k in ('listen_host', 'listen_port', 'access_token') if k in data}

        if not allowed:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400
        if 'listen_port' in allowed:
            try:
                allowed['listen_port'] = int(allowed['listen_port'])
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'listen_port 必须是整数'}), 400

        merged = dict(_read_yaml_section('onebot'))
        merged.update(allowed)
        if not _update_yaml_section('onebot', merged):
            return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500

        # 同步内存配置（端口/监听地址改动需重启生效，access_token 立即生效）
        onebot = framework.config.setdefault('onebot', {})
        onebot.update(allowed)
        needs_restart = [k for k in allowed if k in ('listen_host', 'listen_port')]

        audit_log(admin['id'], admin['username'], 'update_connection', 'config', 'onebot', allowed)
        msg = '连接配置已保存'
        if needs_restart:
            msg += '，监听地址/端口改动需重启框架后生效'
        return jsonify({'code': 0, 'msg': msg, 'data': {'needs_restart': needs_restart}})

    # ---- 运行状态 ----

    @app.route('/api/runtime/stats', methods=['GET'])
    @require_auth
    def runtime_stats():
        """进程/系统运行状态（WebUI 实时轮询）"""
        try:
            proc = psutil.Process(os.getpid())
            mem = psutil.virtual_memory()
            boot = proc.create_time()
            uptime = max(0, int(time.time() - boot))
            bots = framework.ws_server.get_connected_bots()
            db_type = framework.config.get('database', {}).get('type', 'unknown')

            # 插件内存占用（已加载插件）
            plugin_mem = {}
            try:
                for pname, mod in framework.plugin_loader.get_loaded_plugins().items():
                    try:
                        plugin_mem[pname] = round(
                            psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
                    except Exception:
                        pass
            except Exception:
                pass

            return jsonify({'code': 0, 'data': {
                'cpu_percent': round(psutil.cpu_percent(interval=None) or 0, 1),
                'memory': {
                    'used_mb': round(mem.used / 1024 / 1024, 1),
                    'total_mb': round(mem.total / 1024 / 1024, 1),
                    'percent': round(mem.percent, 1),
                },
                'process_memory_mb': round(proc.memory_info().rss / 1024 / 1024, 1),
                'threads': proc.num_threads(),
                'uptime_seconds': uptime,
                'python_version': sys.version.split()[0],
                'db_type': db_type,
                'ws': {'connected': len(bots), 'bots': bots},
            }})
        except Exception as e:
            logger.error(f"获取运行状态失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 系统配置（config.yaml 分组读写）----

    _YAML_SECTIONS = ('database', 'onebot', 'web', 'plugin', 'log', 'system', 'security')

    @app.route('/api/config/yaml', methods=['GET'])
    @require_auth
    def get_yaml_config():
        """读取 config.yaml 全量配置"""
        path = _yaml_config_path()
        if not os.path.isfile(path):
            return jsonify({'code': 404, 'msg': 'config.yaml 不存在'}), 404
        try:
            with open(path, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f) or {}
            return jsonify({'code': 0, 'data': doc})
        except Exception as e:
            return jsonify({'code': 500, 'msg': f'解析失败: {e}'}), 500

    @app.route('/api/config/yaml/<section>', methods=['PUT'])
    @require_super
    def update_yaml_config(section):
        """分组更新 config.yaml（合并更新，保留未提交字段）"""
        admin = request.admin
        if section not in _YAML_SECTIONS:
            return jsonify({'code': 400, 'msg': f'非法配置段，可选: {", ".join(_YAML_SECTIONS)}'}), 400

        data = request.get_json(silent=True) or {}
        values = data.get('data')
        if not isinstance(values, dict) or not values:
            return jsonify({'code': 400, 'msg': 'data 必须是非空对象'}), 400

        merged = dict(_read_yaml_section(section))
        merged.update(values)
        if not _update_yaml_section(section, merged):
            return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500

        framework.config.setdefault(section, {}).update(values)
        audit_log(admin['id'], admin['username'], 'update_yaml_config', 'config', section, values)
        return jsonify({'code': 0, 'msg': f'配置段 [{section}] 已保存，部分字段需重启生效'})

    # ---- 数据库管理（内嵌 WebUI）----

    @app.route('/api/db/tables', methods=['GET'])
    @require_auth
    def db_tables():
        """列出数据库所有表及行数"""
        try:
            if framework.config.get('database', {}).get('type') == 'mysql':
                rows = db.query("SHOW TABLES")
                tables = [list(r.values())[0] for r in rows]
            else:
                rows = db.query(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")
                tables = [r['name'] for r in rows]

            result = []
            for t in tables:
                try:
                    row = db.query_one(f"SELECT COUNT(*) AS c FROM {_quote_ident(t)}")
                    cnt = row['c'] if row else 0
                except Exception:
                    cnt = None
                result.append({'name': t, 'rows': cnt})
            return jsonify({'code': 0, 'data': result})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/db/tables/<table>/schema', methods=['GET'])
    @require_auth
    def db_table_schema(table):
        """查看表结构"""
        if not re.match(r'^[\w$]+$', table):
            return jsonify({'code': 400, 'msg': '非法表名'}), 400
        try:
            if framework.config.get('database', {}).get('type') == 'mysql':
                rows = db.query(f"SHOW COLUMNS FROM `{table}`")
                return jsonify({'code': 0, 'data': rows})
            rows = db.query(f"PRAGMA table_info({_quote_ident(table)})")
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/db/tables/<table>/rows', methods=['GET'])
    @require_auth
    def db_table_rows(table):
        """分页查询表数据"""
        if not re.match(r'^[\w$]+$', table):
            return jsonify({'code': 400, 'msg': '非法表名'}), 400
        try:
            page = max(1, int(request.args.get('page', 1)))
            page_size = min(200, max(1, int(request.args.get('page_size', 50))))
            offset = (page - 1) * page_size
            total_row = db.query_one(f"SELECT COUNT(*) AS c FROM {_quote_ident(table)}")
            total = total_row['c'] if total_row else 0
            rows = db.query(f"SELECT * FROM {_quote_ident(table)} LIMIT {page_size} OFFSET {offset}")
            # 大字段截断展示
            for r in rows:
                for k, v in r.items():
                    if isinstance(v, str) and len(v) > 200:
                        r[k] = v[:200] + '…'
            return jsonify({'code': 0, 'data': {
                'table': table, 'total': total,
                'page': page, 'page_size': page_size,
                'rows': rows,
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/db/query', methods=['POST'])
    @require_super
    def db_query_sql():
        """执行 SQL（仅超级管理员，非 SELECT 需要确认）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        sql = str(data.get('sql') or '').strip()
        write_flag = data.get('write', False)
        if not sql:
            return jsonify({'code': 400, 'msg': '缺少 SQL'}), 400
        lowered = sql.lstrip().lower()
        is_readonly = lowered.startswith('select') or lowered.startswith('show') \
            or lowered.startswith('pragma') or lowered.startswith('explain')
        if not is_readonly and not write_flag:
            return jsonify({'code': 400, 'msg': 'WRITE_CONFIRM_NEEDED', 'write': True}), 400
        try:
            if is_readonly:
                rows = db.query(sql)
                if rows:
                    for r in rows:
                        for k, v in r.items():
                            if isinstance(v, str) and len(v) > 200:
                                r[k] = v[:200] + '…'
            else:
                db.execute(sql)
                rows = None
            audit_log(admin['id'], admin['username'], 'db_query', 'database', None, {'sql': sql[:200]})
            return jsonify({'code': 0, 'data': {'rows': rows, 'count': len(rows) if rows else 0, 'write': not is_readonly}})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 管理员管理（仅超级管理员）----

    @app.route('/api/admins', methods=['GET'])
    @require_super
    def list_admins():
        """获取管理员列表"""
        try:
            rows = db.query(
                "SELECT id, username, role, is_active, last_login_at, last_login_ip, created_at "
                "FROM admin_users ORDER BY id ASC"
            )
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/admins', methods=['POST'])
    @require_super
    def add_admin():
        """添加管理员"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        role = data.get('role', 'admin')

        if not username or not password:
            return jsonify({'code': 400, 'msg': '用户名和密码不能为空'}), 400
        if role not in ('super', 'admin'):
            return jsonify({'code': 400, 'msg': '角色无效'}), 400
        if len(password) < 6:
            return jsonify({'code': 400, 'msg': '密码至少6位'}), 400

        try:
            existing = db.query_one("SELECT id FROM admin_users WHERE username = %s", (username,))
            if existing:
                return jsonify({'code': 409, 'msg': '用户名已存在'}), 409

            pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
            db.execute(
                "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, pwd_hash, role)
            )
            audit_log(admin['id'], admin['username'], 'add_admin', 'admin', username, {'role': role})
            return jsonify({'code': 0, 'msg': f'管理员 [{username}] 已添加'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/admins/<int:admin_id>', methods=['DELETE'])
    @require_super
    def delete_admin(admin_id):
        """删除管理员"""
        admin = request.admin
        if admin_id == admin['id']:
            return jsonify({'code': 400, 'msg': '不能删除自己'}), 400

        try:
            row = db.query_one("SELECT username FROM admin_users WHERE id = %s", (admin_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '管理员不存在'}), 404

            db.execute("DELETE FROM admin_users WHERE id = %s", (admin_id,))
            audit_log(admin['id'], admin['username'], 'delete_admin', 'admin', row['username'])
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 框架操作 ----

    # 框架源码更新白名单：只覆盖这些代码/配置文件，用户数据一律跳过
    _FW_UPDATE_INCLUDE = {
        'framework', 'web', 'sql', 'main.py', 'requirements.txt',
        'start.sh', '.gitignore', 'README.md', 'LICENSE', 'VERSION',
    }

    def _get_framework_local_commit() -> str:
        """获取本地 git 当前提交短 SHA（无 .git 时返回空）"""
        git_dir = os.path.join(_project_root(), '.git')
        if not os.path.isdir(git_dir):
            return ''
        try:
            head_file = os.path.join(git_dir, 'HEAD')
            if not os.path.isfile(head_file):
                return ''
            with open(head_file, 'r', encoding='utf-8') as f:
                ref = f.read().strip()
            if ref.startswith('ref:'):
                ref_path = os.path.join(git_dir, ref[5:].strip().replace('/', os.sep))
                if os.path.isfile(ref_path):
                    with open(ref_path, 'r', encoding='utf-8') as f:
                        return f.read().strip()[:7]
            return ref[:7]
        except Exception:
            return ''

    @app.route('/api/version', methods=['GET'])
    def public_version():
        """公开版本信息（登录页/未登录页展示，无需认证；限速防刷）"""
        if not _check_public_rate(get_client_ip()):
            return jsonify({'code': 429, 'msg': '请求过于频繁，请稍后再试'}), 429
        ver = _get_framework_local_version()
        return jsonify({
            'code': 0,
            'data': {
                'name': 'ZCBOT',
                'version': ver or 'unknown',
                'alpha': 'alpha' in ver,
            }
        })

    @app.route('/api/framework/check_update', methods=['GET'])
    @require_auth
    def check_framework_update():
        """检查框架是否有更新（主依据 GitHub 最新 Release 版本号；无 Release 时回退 commit 对比）"""
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        try:
            local_ver = _get_framework_local_version()
            local_sha = _get_framework_local_commit()
            lt = _parse_version_tuple(local_ver)

            # 主依据：GitHub Release 列表（tag_name 即版本号，含 pre-release）
            # 不用 /releases/latest：它会跳过 pre-release，导致 alpha 版检测不到。
            # 从所有 Release 中取版本号最高的一个（而非发布时间最新，避免低版本覆盖）。
            release = None
            all_releases = []
            try:
                rresp = requests.get(
                    f"https://api.github.com/repos/{repo}/releases?per_page=30",
                    headers={'Accept': 'application/vnd.github+json'},
                    timeout=15,
                )
                if rresp.status_code == 200:
                    releases = rresp.json()
                    if isinstance(releases, list) and releases:
                        best = None
                        best_t = None
                        for rel in releases:
                            t = rel.get('tag_name') or ''
                            tv = _parse_version_tuple(t[1:] if t.startswith('v') else t)
                            if tv is None:
                                continue
                            all_releases.append({
                                'tag': t,
                                'version': t[1:] if t.startswith('v') else t,
                                'name': rel.get('name') or t,
                                'published_at': rel.get('published_at', ''),
                            })
                            if best_t is None or tv > best_t:
                                best, best_t = rel, tv
                        release = best
            except Exception:
                pass

            if release:
                tag = release.get('tag_name', '') or ''
                remote_version = tag[1:] if tag.startswith('v') else tag
                rt = _parse_version_tuple(remote_version)
                if lt is not None and rt is not None:
                    n = max(len(lt), len(rt))
                    has_update = (rt + (0,) * (n - len(rt))) > (lt + (0,) * (n - len(lt)))
                else:
                    has_update = None  # 本地版本缺失，无法判断
                body = release.get('body') or ''
                name = (release.get('name') or '').strip()
                commit_msg = name or (body.split('\n')[0] if body else '') or f"Release {tag}"
                # 可用版本列表（按版本号从高到低），供前端指定版本更新
                all_releases.sort(
                    key=lambda x: _parse_version_tuple(x['version']) or (0,),
                    reverse=True)
                return jsonify({
                    'code': 0,
                    'data': {
                        'repo': repo,
                        'branch': branch,
                        'local_version': local_ver or '未知',
                        'latest_version': remote_version or '未知',
                        'local_commit': local_sha or '未知',
                        'latest_commit': tag,
                        'commit_message': commit_msg,
                        'commit_date': release.get('published_at', ''),
                        'author': (release.get('author') or {}).get('login', ''),
                        'has_update': has_update,
                        'available_versions': all_releases,
                    }
                })

            # 回退：仓库无任何 Release → 按 main 分支最新提交对比
            api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
            resp = requests.get(api_url, headers={'Accept': 'application/vnd.github.v3+json'}, timeout=15)
            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': '仓库或分支不存在'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'GitHub API 返回 {resp.status_code}'}), 500

            data = resp.json()
            latest_sha = data.get('sha', '')[:7]
            commit_msg = data.get('commit', {}).get('message', '').split('\n')[0]
            commit_date = data.get('commit', {}).get('author', {}).get('date', '')
            author = data.get('commit', {}).get('author', {}).get('name', '')
            has_update = bool(local_sha) and local_sha != latest_sha

            return jsonify({
                'code': 0,
                'data': {
                    'repo': repo,
                    'branch': branch,
                    'local_version': local_ver or '未知',
                    'latest_version': '未知（仓库无 Release）',
                    'local_commit': local_sha or '未知',
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': has_update,
                }
            })
        except requests.exceptions.Timeout:
            return jsonify({'code': 500, 'msg': 'GitHub API 请求超时'}), 500
        except Exception as e:
            logger.error(f"检查框架更新失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/framework/update', methods=['POST'])
    @require_auth
    def update_framework():
        """
        从 GitHub 更新框架源码到指定版本（默认最新 Release）
        只覆盖框架代码（framework/web/sql/main.py 等），
        保留用户数据（plugins/、data/、config.yaml、*.db 等），
        更新后需重启生效。
        请求体可选 version 指定目标版本号（如 1.0.0）。
        """
        admin = request.admin
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        root = _project_root()
        req_version = (request.json or {}).get('version') or ''

        try:
            # 指定版本 → 用对应 tag；否则用最新 Release tag；无 Release 回退 main 分支 ZIP
            tag = ''
            if req_version:
                want = req_version[1:] if req_version.startswith('v') else req_version
                tag = f'v{want}' if want and not want.startswith('v') else want
                zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
                logger.info(f"正在下载框架更新（指定版本 {tag}）: {zip_url}")
            else:
                tag = _latest_release_tag(repo)
                if tag:
                    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
                    logger.info(f"正在下载框架更新（Release {tag}）: {zip_url}")
                else:
                    zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                    logger.info(f"仓库无 Release，回退下载分支 ZIP: {zip_url}")

            # 代理 → 镜像 → 直连，逐个候选下载并校验 ZIP 有效性
            tmp_zip = _download_zip_file(_github_url_candidates(zip_url))
            if tmp_zip is None:
                return jsonify({'code': 500, 'msg': '框架更新 ZIP 下载失败（代理/镜像/直连均不可用）'}), 500

            # 解压到临时目录
            tmp_dir = tempfile.mkdtemp(prefix='zcbot_fw_')
            try:
                with zipfile.ZipFile(tmp_zip, 'r') as zf:
                    zf.extractall(tmp_dir)

                # GitHub ZIP 内含一层 repo-tag/ 目录
                entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
                src_root = os.path.join(tmp_dir, entries[0]) if entries else tmp_dir

                # 覆盖白名单内的代码/配置文件
                updated = []
                for name in os.listdir(src_root):
                    if name not in _FW_UPDATE_INCLUDE:
                        continue  # 保护 plugins/ data/ config.yaml 等用户数据
                    src = os.path.join(src_root, name)
                    dst = os.path.join(root, name)
                    if os.path.isdir(src):
                        # 删除旧目录再整体复制，避免残留旧文件
                        if os.path.isdir(dst):
                            shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(src, dst)
                    elif os.path.isfile(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                        shutil.copy2(src, dst)
                    updated.append(name)

                # 新版本可能引入新增依赖：自动安装缺失项（不覆盖已安装包）
                try:
                    _install_new_requirements()
                except Exception as e:
                    logger.warning(f"框架更新：依赖自动安装异常: {e}")

                audit_log(admin['id'], admin['username'], 'update_framework', 'system', 'framework',
                          {'files': updated, 'target': tag or branch}, 'success')
                return jsonify({
                    'code': 0,
                    'msg': f'框架已更新到 {tag or "最新"}（{len(updated)} 项），请重启框架生效。',
                    'data': {'updated': updated, 'target': tag or 'latest'},
                })
            finally:
                try:
                    os.unlink(tmp_zip)
                except Exception:
                    pass
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except zipfile.BadZipFile:
            return jsonify({'code': 400, 'msg': '下载的 ZIP 文件无效'}), 400
        except Exception as e:
            logger.error(f"更新框架失败: {e}", exc_info=True)
            audit_log(admin['id'], admin['username'], 'update_framework', 'system', 'framework',
                      {}, 'failure', str(e))
            return jsonify({'code': 500, 'msg': f'更新失败: {e}'}), 500

    @app.route('/api/restart', methods=['POST'])
    @require_auth
    def restart_framework():
        """重启框架：用 os.execv 原地替换进程，不依赖外部进程管理器"""
        admin = request.admin
        audit_log(admin['id'], admin['username'], 'restart_framework', 'system', 'framework')

        # 异步停止并重启，先返回响应
        def _do_restart():
            import time
            time.sleep(1)
            try:
                loop = getattr(framework, 'loop', None)
                if loop is not None and loop.is_running():
                    import asyncio
                    fut = asyncio.run_coroutine_threadsafe(framework.stop(), loop)
                    fut.result(timeout=15)
                else:
                    import asyncio
                    asyncio.run(framework.stop())
            except Exception:
                pass
            # os.execv 用当前 Python 解释器重载 main.py，原地替换进程
            python = sys.executable
            main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py')
            os.chdir(os.path.dirname(main_py))
            os.execv(python, [python, main_py] + sys.argv[1:])

        threading.Thread(target=_do_restart, daemon=False).start()
        return jsonify({'code': 0, 'msg': '框架正在重启...'})

    # ---- 插件 WebUI 内嵌 ----

    @app.route('/api/plugin_webuis', methods=['GET'])
    @require_auth
    def list_plugin_webuis():
        """获取所有已注册的插件 WebUI 列表"""
        webuis = framework.plugin_loader.get_plugin_webuis()
        return jsonify({'code': 0, 'data': webuis})

    @app.route('/api/plugin_webui/<plugin_name>', methods=['GET'])
    @require_auth
    def serve_plugin_webui(plugin_name):
        """
        获取插件 WebUI 入口页面
        查询参数 ?entry=xxx.html 可指定入口文件（默认 index.html）
        """
        from flask import request as flask_request
        entry = flask_request.args.get('entry', 'index.html')
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': f'插件 {plugin_name} 未提供 WebUI'}), 404
        entry_path = os.path.join(web_dir, entry)
        if not os.path.isfile(entry_path):
            return jsonify({'code': 404, 'msg': f'入口文件 {entry} 不存在'}), 404
        return send_from_directory(web_dir, entry)

    @app.route('/api/plugin_webui/<plugin_name>/assets/<path:filename>', methods=['GET'])
    @require_auth
    def serve_plugin_webui_assets(plugin_name, filename):
        """提供插件 WebUI 的静态资源文件（JS/CSS/图片等）"""
        web_dir = framework.plugin_loader.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return jsonify({'code': 404, 'msg': 'WebUI 目录不存在'}), 404
        file_path = os.path.join(web_dir, filename)
        if not os.path.isfile(file_path):
            return jsonify({'code': 404, 'msg': '文件不存在'}), 404
        return send_from_directory(web_dir, filename)

    # ---- 文件浏览器 ----

    _FILE_BROWSER_ALLOWED_ROOTS = []  # 懒初始化

    def _get_file_browser_roots():
        """获取文件浏览器允许访问的根目录列表"""
        if not _FILE_BROWSER_ALLOWED_ROOTS:
            _FILE_BROWSER_ALLOWED_ROOTS.append(_project_root())
            _FILE_BROWSER_ALLOWED_ROOTS.append(framework.plugin_loader.plugins_dir)
            _FILE_BROWSER_ALLOWED_ROOTS.append(framework.plugin_loader.plugins_dat_dir)
            _FILE_BROWSER_ALLOWED_ROOTS.append(_data_dir())
        return _FILE_BROWSER_ALLOWED_ROOTS

    def _safe_file_path(relative_path: str) -> str:
        """将路径解析为绝对路径，并检查是否在允许的根目录内"""
        roots = _get_file_browser_roots()
        # 如果已经是绝对路径，直接规范化
        if os.path.isabs(relative_path):
            abs_path = os.path.normpath(relative_path)
        else:
            abs_path = os.path.normpath(os.path.join(roots[0], relative_path))
        # 检查是否在任意允许的根目录下
        for root in roots:
            root_norm = os.path.normpath(root)
            if os.path.commonpath([root_norm, abs_path]) == root_norm:
                return abs_path
        return None

    @app.route('/api/files/list', methods=['GET'])
    @require_auth
    def file_browser_list():
        """列出指定目录下的文件和子目录"""
        path = request.args.get('path', '').strip()
        if not path:
            # 返回根目录列表
            roots = _get_file_browser_roots()
            return jsonify({'code': 0, 'data': {
                'entries': [
                    {'name': '项目根目录', 'path': _project_root(), 'is_dir': True, 'root': True},
                    {'name': '插件目录', 'path': framework.plugin_loader.plugins_dir, 'is_dir': True, 'root': True},
                    {'name': '插件数据目录', 'path': framework.plugin_loader.plugins_dat_dir, 'is_dir': True, 'root': True},
                    {'name': '数据目录', 'path': _data_dir(), 'is_dir': True, 'root': True},
                ]
            }})
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isdir(abs_path):
            return jsonify({'code': 400, 'msg': '无效的目录路径'}), 400
        try:
            entries = []
            for name in sorted(os.listdir(abs_path)):
                fpath = os.path.join(abs_path, name)
                is_dir = os.path.isdir(fpath)
                # 跳过隐藏文件和 .venv
                if name.startswith('.') and name != '.':
                    continue
                if name == '.venv' and is_dir:
                    continue
                stat = os.stat(fpath)
                entries.append({
                    'name': name,
                    'path': fpath,
                    'is_dir': is_dir,
                    'size': stat.st_size if not is_dir else 0,
                    'mtime': stat.st_mtime,
                })
            return jsonify({'code': 0, 'data': {
                'current_path': abs_path,
                'entries': entries,
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/read', methods=['GET'])
    @require_auth
    def file_browser_read():
        """读取文件内容"""
        path = request.args.get('path', '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isfile(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        try:
            ext = os.path.splitext(abs_path)[1].lower()
            binary_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.zip', '.pyc', '.db', '.sqlite'}
            if ext in binary_exts:
                return jsonify({'code': 400, 'msg': '不支持预览二进制文件'}), 400
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return jsonify({'code': 0, 'data': {
                'path': abs_path,
                'content': content,
                'size': os.path.getsize(abs_path),
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/write', methods=['PUT'])
    @require_super
    def file_browser_write():
        """写入文件内容（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        content = data.get('content', '')
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path:
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        ext = os.path.splitext(abs_path)[1].lower()
        if ext in {'.pyc', '.db', '.sqlite'}:
            return jsonify({'code': 400, 'msg': '不允许写入该类型文件'}), 400
        try:
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)
            audit_log(admin['id'], admin['username'], 'file_write', 'file', path,
                      {'size': len(content)})
            return jsonify({'code': 0, 'msg': '文件已保存'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/mkdir', methods=['POST'])
    @require_super
    def file_browser_mkdir():
        """新建目录（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path:
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        if os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '目录已存在'}), 400
        try:
            os.makedirs(abs_path, exist_ok=True)
            audit_log(admin['id'], admin['username'], 'file_mkdir', 'dir', abs_path)
            return jsonify({'code': 0, 'msg': '目录已创建'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/rename', methods=['POST'])
    @require_super
    def file_browser_rename():
        """重命名文件/目录（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        new_name = str(data.get('new_name') or '').strip()
        if not path or not new_name:
            return jsonify({'code': 400, 'msg': '缺少 path 或 new_name'}), 400
        if '/' in new_name or '\\' in new_name or new_name in ('.', '..'):
            return jsonify({'code': 400, 'msg': '非法名称'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        new_abs = os.path.join(os.path.dirname(abs_path), new_name)
        if not _safe_file_path(new_abs):
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        if os.path.exists(new_abs):
            return jsonify({'code': 400, 'msg': '目标已存在'}), 400
        try:
            os.rename(abs_path, new_abs)
            audit_log(admin['id'], admin['username'], 'file_rename', 'file', f"{path} -> {new_name}")
            return jsonify({'code': 0, 'msg': '重命名成功'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/copy', methods=['POST'])
    @require_super
    def file_browser_copy():
        """复制文件/目录到指定目录（自动避重名，仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        src = str(data.get('src') or '').strip()
        dest_dir = str(data.get('dest_dir') or '').strip()
        if not src or not dest_dir:
            return jsonify({'code': 400, 'msg': '缺少 src 或 dest_dir'}), 400
        abs_src = _safe_file_path(src)
        abs_dest = _safe_file_path(dest_dir)
        if not abs_src or not os.path.exists(abs_src):
            return jsonify({'code': 400, 'msg': '源文件不存在'}), 400
        if not abs_dest or not os.path.isdir(abs_dest):
            return jsonify({'code': 400, 'msg': '目标目录不存在'}), 400
        # 禁止把目录复制进自身内部
        if os.path.isdir(abs_src):
            src_real = os.path.realpath(abs_src)
            dest_real = os.path.realpath(abs_dest)
            if dest_real != src_real and os.path.commonpath([src_real, dest_real]) == src_real:
                return jsonify({'code': 400, 'msg': '不能复制到自身内部'}), 400
        name = os.path.basename(abs_src.rstrip('/\\'))
        if not name:
            return jsonify({'code': 400, 'msg': '非法路径'}), 400
        target = os.path.join(abs_dest, name)
        if os.path.exists(target):
            base, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(target):
                target = os.path.join(abs_dest, f"{base}({i}){ext}")
                i += 1
        try:
            if os.path.isdir(abs_src):
                shutil.copytree(abs_src, target)
            else:
                shutil.copy2(abs_src, target)
            audit_log(admin['id'], admin['username'], 'file_copy', 'file',
                      f"{src} -> {target}")
            return jsonify({'code': 0, 'msg': f'已复制为 {os.path.basename(target)}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/delete', methods=['POST'])
    @require_super
    def file_browser_delete():
        """删除文件/目录（递归，仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        if os.path.abspath(abs_path) == os.path.normpath(_project_root()):
            return jsonify({'code': 400, 'msg': '禁止删除项目根目录'}), 400
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
            audit_log(admin['id'], admin['username'], 'file_delete', 'file', path)
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/upload', methods=['POST'])
    @require_super
    def file_browser_upload():
        """上传文件到指定目录（multipart/form-data：字段 dir + 文件列表，仅超级管理员）"""
        admin = request.admin
        target = str(request.form.get('dir') or '').strip()
        if not target:
            return jsonify({'code': 400, 'msg': '缺少 dir'}), 400
        abs_dir = _safe_file_path(target)
        if not abs_dir or not os.path.isdir(abs_dir):
            return jsonify({'code': 400, 'msg': '目标目录不存在'}), 400
        files = request.files.getlist('files')
        if not files:
            return jsonify({'code': 400, 'msg': '未选择文件'}), 400
        saved, failed = [], []
        for f in files:
            name = os.path.basename(f.filename or '')
            if not name or name in ('.', '..'):
                failed.append({'name': f.filename, 'err': '非法文件名'})
                continue
            dest = os.path.join(abs_dir, name)
            if os.path.exists(dest):
                base, ext = os.path.splitext(name)
                i = 1
                while os.path.exists(dest):
                    dest = os.path.join(abs_dir, f"{base}({i}){ext}")
                    i += 1
            try:
                f.save(dest)
                saved.append(os.path.basename(dest))
            except Exception as e:
                failed.append({'name': name, 'err': str(e)})
        if saved:
            audit_log(admin['id'], admin['username'], 'file_upload', 'dir', target,
                      {'saved': saved, 'failed': failed})
        if failed:
            return jsonify({'code': 0, 'msg': f'上传完成：成功 {len(saved)}，失败 {len(failed)}', 'saved': saved}), 200
        return jsonify({'code': 0, 'msg': f'上传成功 {len(saved)} 个文件', 'saved': saved})

    @app.route('/api/files/download', methods=['GET'])
    @require_auth
    def file_browser_download():
        """下载文件"""
        path = request.args.get('path', '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isfile(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        return send_from_directory(
            os.path.dirname(abs_path),
            os.path.basename(abs_path),
            as_attachment=True,
        )

    # ---- 统计图表 ----

    @app.route('/api/stats/commands', methods=['GET'])
    @require_auth
    def stats_commands():
        """命令命中统计：按插件分组，返回 TopN 命令"""
        try:
            top = min(int(request.args.get('top', 20)), 100)
            rows = db.query(
                "SELECT plugin_name, pattern, hit_count, description FROM commands "
                "WHERE is_active = 1 ORDER BY hit_count DESC LIMIT %s", (top,)
            )
            total = sum(r['hit_count'] for r in rows) if rows else 0
            return jsonify({'code': 0, 'data': {
                'total_hits': total,
                'commands': rows,
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/stats/messages', methods=['GET'])
    @require_auth
    def stats_messages():
        """消息统计：按天统计消息量（最近 30 天）"""
        try:
            rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
            if not rows:
                return jsonify({'code': 0, 'data': {'days': [], 'total': 0}})
            if framework.config.get('database', {}).get('type') == 'mysql':
                day_rows = db.query(
                    "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
                    "FROM messages WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
                    "GROUP BY DATE(created_at) ORDER BY day ASC"
                )
            else:
                day_rows = db.query(
                    "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
                    "FROM messages WHERE created_at >= datetime('now', '-30 days') "
                    "GROUP BY DATE(created_at) ORDER BY day ASC"
                )
            return jsonify({'code': 0, 'data': {
                'days': day_rows,
                'total': sum(r['cnt'] for r in day_rows) if day_rows else 0,
            }})
        except Exception:
            return jsonify({'code': 0, 'data': {'days': [], 'total': 0}})

    # ---- 环境信息 ----

    @app.route('/api/envinfo', methods=['GET'])
    @require_auth
    def envinfo():
        """获取系统环境信息（参考 Koishi envinfo 命令）"""
        try:
            import platform
            import distro
            has_distro = True
        except ImportError:
            has_distro = False

        try:
            disk = psutil.disk_usage(_project_root())
            net = psutil.net_io_counters()
            proc = psutil.Process(os.getpid())
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()

            # Python 包信息
            packages = []
            try:
                import pkg_resources
                for pkg in sorted(pkg_resources.working_set, key=lambda x: x.key):
                    if pkg.key in ('pip', 'setuptools', 'wheel'):
                        continue
                    packages.append({'name': pkg.key, 'version': pkg.version})
            except Exception:
                try:
                    import importlib.metadata as im
                    for dist in im.distributions():
                        if dist.metadata['Name'] and dist.metadata['Name'] not in ('pip', 'setuptools', 'wheel'):
                            packages.append({'name': dist.metadata['Name'], 'version': dist.version})
                except Exception:
                    pass

            return jsonify({'code': 0, 'data': {
                'os': {
                    'system': platform.system(),
                    'release': platform.release(),
                    'version': platform.version(),
                    'machine': platform.machine(),
                    'arch': platform.architecture()[0],
                    'distro': distro.name(pretty=True) if has_distro else platform.platform(),
                },
                'cpu': {
                    'count': cpu_count,
                    'physical_count': psutil.cpu_count(logical=False) or cpu_count,
                    'freq_mhz': round(cpu_freq.current / 1000, 2) if cpu_freq else None,
                    'percent': psutil.cpu_percent(interval=None),
                },
                'memory': {
                    'total_mb': round(psutil.virtual_memory().total / 1024 / 1024, 1),
                    'available_mb': round(psutil.virtual_memory().available / 1024 / 1024, 1),
                },
                'disk': {
                    'total_gb': round(disk.total / 1024 / 1024 / 1024, 1),
                    'used_gb': round(disk.used / 1024 / 1024 / 1024, 1),
                    'free_gb': round(disk.free / 1024 / 1024 / 1024, 1),
                    'percent': disk.percent,
                },
                'network': {
                    'bytes_sent_mb': round(net.bytes_sent / 1024 / 1024, 1),
                    'bytes_recv_mb': round(net.bytes_recv / 1024 / 1024, 1),
                },
                'python': {
                    'version': sys.version.split()[0],
                    'executable': sys.executable,
                    'packages': packages[:50],  # 最多 50 个
                    'packages_total': len(packages),
                },
                'process': {
                    'pid': os.getpid(),
                    'threads': proc.num_threads(),
                    'open_files': len(proc.open_files()),
                    'connections': len(proc.connections()),
                    'create_time': proc.create_time(),
                },
                'database': {
                    'type': framework.config.get('database', {}).get('type', 'unknown'),
                },
            }})
        except Exception as e:
            logger.error(f"获取环境信息失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 插件依赖图 ----

    import re as _re

    def _parse_version_spec(dep: str):
        """解析依赖版本说明符，返回 (包名, 运算符, 版本号)"""
        dep = dep.strip()
        m = _re.match(r'^([a-zA-Z_][a-zA-Z0-9_.-]*)', dep)
        if not m:
            return (dep, None, None)
        pkg = m.group(1)
        spec_part = dep[len(pkg):].strip()
        if not spec_part:
            return (pkg, None, None)
        m2 = _re.match(r'^([><=!~]+)\s*([a-zA-Z0-9.*_+-]+)', spec_part)
        if m2:
            return (pkg, m2.group(1), m2.group(2))
        return (pkg, None, None)

    @app.route('/api/plugins/deps/graph', methods=['GET'])
    @require_auth
    def plugin_deps_graph():
        """获取插件依赖关系图数据（节点 + 边）"""
        try:
            plugin_rows = db.query("SELECT plugin_name, version FROM plugins ORDER BY plugin_name")
            plugins = {r['plugin_name']: r for r in plugin_rows}

            nodes = []
            edges = []
            # 已安装插件集合
            installed = set(plugins.keys())
            # 所有出现在依赖中的包名
            pkg_to_plugins = {}  # pkg_name -> [plugin_name]

            for pname in installed:
                yaml_data = framework.plugin_loader.read_plugin_yaml(pname)
                deps = yaml_data.get('dependencies', {}).get('python', []) if yaml_data else []
                dep_info = framework.plugin_loader.get_dep_status(pname)
                missing = set(dep_info.get('missing', []))
                conflicts = dep_info.get('conflicts', [])

                # 解析依赖包名
                parsed_deps = []
                for dep in deps:
                    pkg_name, op, ver = _parse_version_spec(dep)
                    if pkg_name:
                        parsed_deps.append({
                            'raw': dep,
                            'pkg_name': pkg_name,
                            'operator': op,
                            'version': ver,
                            'missing': dep in missing,
                            'conflict': any(c['name'] == pkg_name for c in conflicts),
                        })
                        pkg_to_plugins.setdefault(pkg_name, []).append(pname)

                nodes.append({
                    'id': pname,
                    'type': 'plugin',
                    'deps': parsed_deps,
                    'dep_count': len(parsed_deps),
                    'missing_count': len([d for d in parsed_deps if d['missing']]),
                    'version': plugins[pname].get('version', '?'),
                })

            # 构建插件间依赖边（如果两个插件依赖同一个包，建立关联）
            for pkg_name, plugin_list in pkg_to_plugins.items():
                if len(plugin_list) >= 2:
                    # 多个插件共享同一个依赖 → 建立关联边
                    for i in range(len(plugin_list)):
                        for j in range(i + 1, len(plugin_list)):
                            edges.append({
                                'source': plugin_list[i],
                                'target': plugin_list[j],
                                'label': pkg_name,
                                'shared': True,
                            })

            return jsonify({'code': 0, 'data': {
                'nodes': nodes,
                'edges': edges,
                'total_plugins': len(nodes),
                'total_edges': len(edges),
            }})
        except Exception as e:
            logger.error(f"获取依赖图数据失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 前端静态文件 ----

    def _web_root_dir():
        """前端根目录（框架默认 web/ 目录）"""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')

    def _override_entry_url() -> str:
        """
        返回接管前端的插件入口 URL。
        插件接管时根路径 / redirect 到该 URL，由插件路由服务其模板网页。
        无接管返回 None。
        """
        name = framework.plugin_loader.get_override_webui()
        if not name:
            return None
        return f'/{name}/'

    @app.route('/css/<path:filename>')
    def serve_css(filename):
        return send_from_directory(os.path.join(_web_root_dir(), 'css'), filename)

    @app.route('/js/<path:filename>')
    def serve_js(filename):
        return send_from_directory(os.path.join(_web_root_dir(), 'js'), filename)

    @app.route('/img/<path:filename>')
    def serve_img(filename):
        return send_from_directory(os.path.join(_web_root_dir(), 'img'), filename)

    @app.route('/<page>.html')
    def serve_page(page):
        """提供 HTML 页面"""
        web_static = _web_root_dir()
        html_file = os.path.join(web_static, f'{page}.html')
        if os.path.isfile(html_file):
            return send_from_directory(web_static, f'{page}.html')
        return jsonify({'code': 404, 'msg': '页面不存在'}), 404

    @app.route('/')
    def serve_index():
        """根路径：若插件接管了前端则 redirect 到插件入口，否则返回框架默认 index.html"""
        entry = _override_entry_url()
        if entry:
            from flask import redirect
            return redirect(entry, code=302)
        return send_from_directory(_web_root_dir(), 'index.html')

    @app.route('/reset')
    def serve_reset():
        """前端恢复页：若插件接管了前端则返回其 reset.html；
        无接管页时回退框架默认 web/reset.html（不存在则返回默认 index.html）。"""
        web_static = _web_root_dir()
        reset_file = os.path.join(web_static, 'reset.html')
        if os.path.isfile(reset_file):
            return send_from_directory(web_static, 'reset.html')
        return send_from_directory(web_static, 'index.html')

    return app


class WebServer:
    """Web UI 服务器，在独立线程中运行"""

    def __init__(self, framework):
        self.framework = framework
        self.app = create_web_app(framework)
        web_cfg = framework.config.get('web', {})
        self.host = web_cfg.get('host', '0.0.0.0')
        self.port = web_cfg.get('port', 8080)
        self._thread = None
        self._server = None
        self._running = False

    def start(self):
        """启动 Web 服务器"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-server")
        self._thread.start()
        logger.info(f"Web UI 已启动: http://{self.host}:{self.port}")

    def _run(self):
        """运行 Web 服务器（保存 server 句柄，供 stop() 真正停止并释放端口）"""
        try:
            # 使用 waitress（生产级）或 werkzeug 开发服务器
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
