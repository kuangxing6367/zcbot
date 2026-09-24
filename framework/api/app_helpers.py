# -*- coding: utf-8 -*-
"""
create_web_app 共享辅助函数（自 framework/api/webapp.py 剥离）

工厂 make_app_helpers(framework, db, plugins_dir) 返回 dict，
由 webapp.py 展开进 ctx，键名与原闭包完全一致。
"""
import json
import logging
import os
import re
import sys
import tempfile
import zipfile

import requests
import yaml

logger = logging.getLogger('zcbot')

# 项目根目录（framework/api/app_helpers.py 向上三级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PRE_RELEASE_PRIORITY = {
    'alpha': 0,
    'beta': 1,
    'rc': 2,
    'pre': 2, 'preview': 2,
}

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


def make_app_helpers(framework, db, plugins_dir) -> dict:
    """构建 create_web_app 的共享工具闭包（键名 = 原 ctx 属性名）"""

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

    return {
        '_project_root': _project_root,
        '_get_framework_local_version': _get_framework_local_version,
        '_install_new_requirements': _install_new_requirements,
        '_parse_version_tuple': _parse_version_tuple,
        '_latest_release_tag': _latest_release_tag,
        '_data_dir': _data_dir,
        '_quote_ident': _quote_ident,
        '_yaml_config_path': _yaml_config_path,
        '_read_yaml_section': _read_yaml_section,
        '_yaml_scalar': _yaml_scalar,
        '_yaml_block_lines': _yaml_block_lines,
        '_update_yaml_section': _update_yaml_section,
        '_read_market_sources_custom': _read_market_sources_custom,
        '_save_market_sources_custom': _save_market_sources_custom,
        '_DEFAULT_MARKET': _DEFAULT_MARKET,
        '_MIRROR_MARKETS': _MIRROR_MARKETS,
        '_DEFAULT_GITHUB_PROXY': _DEFAULT_GITHUB_PROXY,
        '_github_proxy': _github_proxy,
        '_github_url_candidates': _github_url_candidates,
        '_download_zip_file': _download_zip_file,
        '_fetch_market_source': _fetch_market_source,
        '_market_installed_set': _market_installed_set,
        '_download_plugin_tree': _download_plugin_tree,
        '_download_and_extract_plugin': _download_and_extract_plugin,
    }
