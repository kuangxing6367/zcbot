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

    # ---- 双请求防破解认证系统 ----
    dual_auth = DualRequestAuthSystem(framework.config.get('security', {}))

    @app.before_request
    def _global_blacklist_guard():
        """全局黑名单拦截：被封禁的 IP 无法访问任何路由（白名单除外）"""
        ip = get_client_ip()
        if dual_auth.is_whitelisted(ip) or not dual_auth.is_blacklisted(ip):
            return None
        return jsonify({'code': 403, 'msg': '访问被拒绝'}), 403

    # ---- 工具函数 ----

    def _project_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        """当前已安装的插件名集合（来自 DB plugins 表）"""
        try:
            rows = db.query("SELECT plugin_name FROM plugins")
            return {r['plugin_name'] for r in rows}
        except Exception:
            return set()

    def _download_and_extract_plugin(repo: str, branch: str, sub_path: str, target_dir: str):
        """
        从 GitHub 下载仓库 ZIP 并解压到目标目录（可指定子目录）。
        返回 (ok, msg)
        """
        if repo.startswith('https://github.com/'):
            repo = repo.replace('https://github.com/', '').rstrip('/')
        elif repo.startswith('http://github.com/'):
            repo = repo.replace('http://github.com/', '').rstrip('/')
        if not repo or '..' in repo:
            return False, '非法仓库地址'

        zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
        urls_to_try = [zip_url]
        for mirror in _MIRROR_MARKETS:
            mirror_host = mirror['url'].split('/')[2]
            urls_to_try.append(zip_url.replace('https://github.com/', f'https://{mirror_host}/https://github.com/'))

        last_err = ''
        for url in urls_to_try:
            try:
                logger.info(f"正在下载插件: {url}")
                resp = requests.get(url, timeout=60, stream=True)
                if resp.status_code == 404:
                    return False, f'仓库或分支不存在: {repo}@{branch}'
                if resp.status_code != 200:
                    last_err = f'下载失败: HTTP {resp.status_code}'
                    continue

                tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
                for chunk in resp.iter_content(chunk_size=8192):
                    tmp_zip.write(chunk)
                tmp_zip.close()

                os.makedirs(target_dir, exist_ok=True)
                try:
                    with zipfile.ZipFile(tmp_zip.name, 'r') as zf:
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
                except zipfile.BadZipFile:
                    return False, '下载的 ZIP 文件无效'
                finally:
                    try:
                        os.unlink(tmp_zip.name)
                    except Exception:
                        pass
                return True, ''
            except Exception as e:
                last_err = str(e)
                continue

        return False, last_err or '所有下载尝试均失败'

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
        """从 Authorization: Bearer xxx 头提取 token"""
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        return None

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

    def require_auth(fn):
        """登录验证装饰器（基于 token）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            request.admin = admin  # 将 admin 信息附加到 request
            return fn(*args, **kwargs)
        return wrapper

    def require_super(fn):
        """超级管理员验证装饰器（基于 token）"""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _extract_token(request)
            if not token:
                return jsonify({'code': 401, 'msg': '未提供认证令牌'}), 401
            admin = _verify_token(token)
            if not admin:
                return jsonify({'code': 401, 'msg': '令牌无效或已过期'}), 401
            if admin.get('role') != 'super':
                return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
            request.admin = admin
            return fn(*args, **kwargs)
        return wrapper

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

        return jsonify({
            'code': 0,
            'msg': '登录成功',
            'data': {'token': token, 'username': username, 'role': row['role']}
        })

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
        return jsonify({'code': 0, 'msg': '已退出'})

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

    @app.route('/api/dashboard', methods=['GET'])
    @require_auth
    def dashboard():
        """仪表盘数据"""
        data = {}

        # 插件统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins WHERE is_active = 1")
            data['plugins_active'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM plugins")
            data['plugins_total'] = row['cnt'] if row else 0
        except Exception:
            data['plugins_active'] = 0
            data['plugins_total'] = 0

        # 命令统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM commands")
            data['commands_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM dynamic_commands WHERE is_active = 1")
            data['dynamic_commands'] = row['cnt'] if row else 0
        except Exception:
            data['commands_total'] = 0
            data['dynamic_commands'] = 0

        # 用户/群统计
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM users")
            data['users_total'] = row['cnt'] if row else 0
            row = db.query_one("SELECT COUNT(*) as cnt FROM groups_info WHERE is_active = 1")
            data['groups_active'] = row['cnt'] if row else 0
        except Exception:
            data['users_total'] = 0
            data['groups_active'] = 0

        # OneBot 连接状态
        data['bots'] = framework.ws_server.get_connected_bots()
        data['ws_port'] = framework.config.get('onebot', {}).get('listen_port', 6830)

        # 定时任务
        try:
            row = db.query_one("SELECT COUNT(*) as cnt FROM tasks WHERE is_active = 1")
            data['tasks_active'] = row['cnt'] if row else 0
        except Exception:
            data['tasks_active'] = 0

        # 框架信息
        data['framework_name'] = 'ZCBOT'
        data['framework_version'] = '1.0.0'
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
        请求体携带 {"delete_data": true} 时一并删除插件数据目录。
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

            audit_log(admin['id'], admin['username'], 'delete_plugin', 'plugin', plugin_name,
                      {'delete_data': delete_data}, 'success')
            msg = f'插件 [{plugin_name}] 已删除'
            if not delete_data and os.path.isdir(dat_dir) and os.listdir(dat_dir):
                msg += '（配置文件已保留在 plugins_dat，可手动清理）'
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
        """检查插件是否有 GitHub 更新"""
        if not plugin_name.replace('_', '').replace('-', '').isalnum():
            return jsonify({'code': 400, 'msg': '非法插件名'}), 400

        try:
            yaml_data = framework.plugin_loader.read_plugin_yaml(plugin_name)
            github = yaml_data.get('github', {})
            repo = github.get('repo', '')

            if not repo:
                return jsonify({'code': 400, 'msg': '该插件未配置 GitHub 更新源（plugin.yaml 中缺少 github.repo）'}), 400

            branch = github.get('branch', 'main')

            # 规范化 repo 地址
            if repo.startswith('https://github.com/'):
                repo = repo.replace('https://github.com/', '').rstrip('/')
            elif repo.startswith('http://github.com/'):
                repo = repo.replace('http://github.com/', '').rstrip('/')

            # 查询 GitHub API 获取最新 commit
            api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
            headers = {'Accept': 'application/vnd.github.v3+json'}
            resp = requests.get(api_url, headers=headers, timeout=15)

            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': f'GitHub 仓库或分支不存在: {repo}@{branch}'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'GitHub API 返回 {resp.status_code}'}), 500

            data = resp.json()
            latest_sha = data.get('sha', '')[:7]
            commit_msg = data.get('commit', {}).get('message', '').split('\n')[0]
            commit_date = data.get('commit', {}).get('author', {}).get('date', '')
            author = data.get('commit', {}).get('author', {}).get('name', '')

            # 当前版本
            current_version = yaml_data.get('version', 'unknown')

            return jsonify({
                'code': 0,
                'data': {
                    'plugin_name': plugin_name,
                    'repo': repo,
                    'branch': branch,
                    'current_version': current_version,
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': True,  # 简化：总是允许更新
                }
            })
        except requests.exceptions.Timeout:
            return jsonify({'code': 500, 'msg': 'GitHub API 请求超时'}), 500
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

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

            # 下载 ZIP
            zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
            logger.info(f"正在从 GitHub 下载插件: {zip_url}")
            resp = requests.get(zip_url, timeout=60, stream=True)

            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': f'仓库或分支不存在: {repo}@{branch}'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'下载失败: HTTP {resp.status_code}'}), 500

            # 保存到临时文件
            tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            for chunk in resp.iter_content(chunk_size=8192):
                tmp_zip.write(chunk)
            tmp_zip.close()

            target_dir = os.path.join(plugins_dir, plugin_name)

            # 卸载当前插件
            framework.plugin_loader.unload_plugin(plugin_name)

            # 备份旧代码
            if os.path.isdir(target_dir):
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            os.makedirs(target_dir, exist_ok=True)

            # 解压
            with zipfile.ZipFile(tmp_zip.name, 'r') as zf:
                # GitHub ZIP 内会有一层 repo-branch/ 目录
                names = zf.namelist()
                # 找到前缀目录名
                prefix = names[0].split('/')[0] if names else ''

                for name in names:
                    if name.endswith('/'):
                        continue
                    # 去掉前缀目录
                    if prefix and name.startswith(prefix + '/'):
                        rel_path = name[len(prefix) + 1:]
                    else:
                        rel_path = name

                    if not rel_path:
                        continue

                    # 如果配置了子目录，只提取该目录下的文件
                    if sub_path and sub_path != '/':
                        sp = sub_path.lstrip('/')
                        if not rel_path.startswith(sp + '/') and rel_path != sp:
                            continue
                        rel_path = rel_path[len(sp) + 1:] if rel_path.startswith(sp + '/') else rel_path

                    if not rel_path:
                        continue

                    # 安全检查
                    if '..' in rel_path or rel_path.startswith('/'):
                        continue

                    dest = os.path.join(target_dir, rel_path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True) if os.path.dirname(dest) else None
                    with open(dest, 'wb') as f:
                        f.write(zf.read(name))

            # 清理临时文件
            os.unlink(tmp_zip.name)

            # 分离配置文件到 plugins_dat（保留用户已有配置不被覆盖）
            framework.plugin_loader.split_installed_files(plugin_name)

            # 重新加载插件
            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                audit_log(admin['id'], admin['username'], 'update_plugin_github',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch}, 'success')
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 已从 GitHub 更新并重新加载'})
            else:
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
                all_plugins.extend(_fetch_market_source(src))
            except Exception as e:
                err_msg = f"{src.get('name', src.get('url', ''))}: {str(e)[:120]}"
                # 主源失败时，尝试镜像源
                if src.get('url') == _DEFAULT_MARKET['url']:
                    mirror_ok = False
                    for mirror in _MIRROR_MARKETS:
                        try:
                            all_plugins.extend(_fetch_market_source(mirror))
                            mirror_ok = True
                            break
                        except Exception:
                            continue
                    if not mirror_ok:
                        errors.append(err_msg)
                else:
                    errors.append(err_msg)

        installed = _market_installed_set()
        for p in all_plugins:
            p['installed'] = p.get('name') in installed

        result = {'plugins': all_plugins, 'errors': errors, 'sources': sources}
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'data': result}, f, ensure_ascii=False)
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
            # 已存在则先卸载并备份
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                framework.plugin_loader.unload_plugin(plugin_name)
                backup_dir = target_dir + f'.bak.{int(time.time())}'
                shutil.move(target_dir, backup_dir)

            ok, msg = _download_and_extract_plugin(repo, branch, sub_path, target_dir)
            if not ok:
                return jsonify({'code': 500, 'msg': f'下载失败: {msg}'}), 500

            # 分离配置文件到 plugins_dat
            framework.plugin_loader.split_installed_files(plugin_name)

            if framework.plugin_loader.load_plugin(plugin_name):
                framework.plugin_loader.register_commands(plugin_name)
                audit_log(admin['id'], admin['username'], 'install_plugin_market',
                          'plugin', plugin_name, {'repo': repo, 'branch': branch, 'sub_path': sub_path})
                return jsonify({'code': 0, 'msg': f'插件 [{plugin_name}] 安装成功并已加载'})
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

    # ---- 插件配置读写 API（参考 AstrBot _conf_schema.json 配置系统）----

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
        """更新静态命令的别名/描述/权限（参考 AstrBot CommandConfig）"""
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
        """获取指定插件注册的所有命令（参考 AstrBot 插件详情组件展示）"""
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
        'start.sh', '.gitignore', 'README.md', 'LICENSE',
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

    @app.route('/api/framework/check_update', methods=['GET'])
    @require_auth
    def check_framework_update():
        """检查框架是否有更新（对比 GitHub main 分支最新提交）"""
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        try:
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

            local_sha = _get_framework_local_commit()

            return jsonify({
                'code': 0,
                'data': {
                    'repo': repo,
                    'branch': branch,
                    'local_commit': local_sha or '未知',
                    'latest_commit': latest_sha,
                    'commit_message': commit_msg,
                    'commit_date': commit_date,
                    'author': author,
                    'has_update': bool(local_sha) and local_sha != latest_sha,
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
        从 GitHub 更新框架源码
        只覆盖框架代码（framework/web/sql/main.py 等），
        保留用户数据（plugins/、data/、config.yaml、*.db 等），
        更新前自动备份 framework/ 到 data/backups/，更新后需重启生效。
        """
        admin = request.admin
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        root = _project_root()

        try:
            zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
            logger.info(f"正在下载框架更新: {zip_url}")
            resp = requests.get(zip_url, timeout=180, stream=True)
            if resp.status_code == 404:
                return jsonify({'code': 404, 'msg': f'仓库或分支不存在: {repo}@{branch}'}), 404
            if resp.status_code != 200:
                return jsonify({'code': 500, 'msg': f'下载失败: HTTP {resp.status_code}'}), 500

            tmp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            for chunk in resp.iter_content(chunk_size=8192):
                tmp_zip.write(chunk)
            tmp_zip.close()

            # 解压到临时目录
            tmp_dir = tempfile.mkdtemp(prefix='zcbot_fw_')
            with zipfile.ZipFile(tmp_zip.name, 'r') as zf:
                zf.extractall(tmp_dir)

            # GitHub ZIP 内含一层 repo-branch/ 目录
            entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
            src_root = os.path.join(tmp_dir, entries[0]) if entries else tmp_dir

            # 备份旧 framework 目录
            backup_dir = os.path.join(root, 'data', 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            fw_backup = os.path.join(backup_dir, f'framework.{int(time.time())}')
            old_fw = os.path.join(root, 'framework')
            if os.path.isdir(old_fw):
                shutil.copytree(old_fw, fw_backup)

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

            # 清理临时文件
            os.unlink(tmp_zip.name)
            shutil.rmtree(tmp_dir, ignore_errors=True)

            audit_log(admin['id'], admin['username'], 'update_framework', 'system', 'framework',
                      {'files': updated, 'backup': fw_backup}, 'success')
            return jsonify({
                'code': 0,
                'msg': f'框架已更新（{len(updated)} 项），请重启框架生效。\n已备份旧代码到 data/backups/',
                'data': {'updated': updated, 'backup': fw_backup},
            })
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

    @app.route('/css/<path:filename>')
    def serve_css(filename):
        web_css = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web', 'css')
        return send_from_directory(web_css, filename)

    @app.route('/js/<path:filename>')
    def serve_js(filename):
        web_js = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web', 'js')
        return send_from_directory(web_js, filename)

    @app.route('/<page>.html')
    def serve_page(page):
        """提供 HTML 页面"""
        web_static = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')
        html_file = os.path.join(web_static, f'{page}.html')
        if os.path.isfile(html_file):
            return send_from_directory(web_static, f'{page}.html')
        return jsonify({'code': 404, 'msg': '页面不存在'}), 404

    @app.route('/')
    def serve_index():
        """根路径返回 index.html"""
        web_static = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'web')
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
        """运行 Web 服务器"""
        try:
            # 使用 waitress 或 werkzeug 开发服务器
            try:
                from waitress import serve as waitress_serve
                waitress_serve(self.app, host=self.host, port=self.port, threads=8)
            except ImportError:
                # 回退到 Flask 开发服务器
                self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"Web UI 异常: {e}")

    def stop(self):
        """停止 Web 服务器"""
        self._running = False
        logger.info("Web UI 已停止")
