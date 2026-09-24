# -*- coding: utf-8 -*-
"""
插件市场 / GitHub 更新：check_update / update_from_github / 市场源管理 / 安装

依赖 helper 经 ctx 分发（_github_url_candidates / _download_and_extract_plugin 等）。
"""
import json
import logging
import os
import re
import shutil
import time
import zipfile

import requests
import yaml
from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    plugins_dir = ctx.plugins_dir
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    _data_dir = ctx._data_dir
    _read_market_sources_custom = ctx._read_market_sources_custom
    _save_market_sources_custom = ctx._save_market_sources_custom
    _DEFAULT_MARKET = ctx._DEFAULT_MARKET
    _MIRROR_MARKETS = ctx._MIRROR_MARKETS
    _github_url_candidates = ctx._github_url_candidates
    _fetch_market_source = ctx._fetch_market_source
    _market_installed_set = ctx._market_installed_set
    _download_and_extract_plugin = ctx._download_and_extract_plugin
    @app.route('/api/plugins/<plugin_name>/check_update', methods=['GET'])
    @require_auth
    def check_plugin_update(plugin_name):
        """检查插件是否有 GitHub 更新（基于版本号对比）"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')
            branch = github.get('branch', 'main')

            current_version = yaml_data.get('version', 'unknown')

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
                has_update = _version_gt(latest_version, current_version)
            else:
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
        """安装/更新第三方源插件后，把 github 源信息写回 plugin.yaml 的 github 段"""
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

            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            target_dir = os.path.join(plugins_dir, plugin_name)

            framework.plugin_loader.unload_plugin(plugin_name)

            backup_dir = None
            if os.path.isdir(target_dir):
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            try:
                ok_dl, dl_msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
                if not ok_dl:
                    raise RuntimeError(f'更新下载失败: {dl_msg}')
            except Exception as e:
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                raise e

            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理更新前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'success')
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已从 GitHub 更新并重新加载'})
            else:
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
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                framework.plugin_loader.unload_plugin(plugin_name)
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            ok, msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
            if not ok:
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    shutil.move(backup_dir, target_dir)
                    logger.warning(f"[{plugin_name}] 安装下载失败，已恢复旧代码")
                return jsonify({'code': 500, 'msg': f'下载失败: {msg}'}), 500

            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                _persist_plugin_github_meta(plugin_name, repo, branch, sub_path)
                if backup_dir and os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    logger.info(f"[{plugin_name}] 已清理安装前的代码备份: {backup_dir}")
                audit_log(admin['id'], admin['username'], 'install_plugin_market',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch, 'sub_path': sub_path})
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 安装成功并已加载'})
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

