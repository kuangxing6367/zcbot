# -*- coding: utf-8 -*-
"""
接口令牌（API Key）管理：与用户会话 token 解耦，专供外部程序调用 REST API
"""
import logging
import secrets
import time

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    db = ctx.db
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    # ---- 接口令牌（API Key）：与用户会话 token 解耦，专供外部程序调用 REST API ----
    # 管理类接口要求超级管理员；令牌本身按自身 role 通过普通鉴权。

    @app.route('/api/apikeys', methods=['GET'])
    @require_super
    def apikeys_list():
        """列出全部接口令牌（不返回 raw token）"""
        try:
            rows = db.query(
                "SELECT id, name, role, created_by, created_at, expires_at, "
                "last_used_at, is_active FROM api_tokens ORDER BY id DESC"
            )
            items = []
            now = int(time.time())
            for r in rows:
                expired = bool(r['expires_at']) and now > float(r['expires_at'])
                items.append({
                    'id': r['id'],
                    'name': r['name'],
                    'role': r['role'],
                    'created_by': r['created_by'],
                    'created_at': r['created_at'],
                    'expires_at': r['expires_at'],
                    'last_used_at': r['last_used_at'],
                    'is_active': r['is_active'],
                    'expired': expired,
                })
            return jsonify({'code': 0, 'data': items})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/apikeys', methods=['POST'])
    @require_super
    def apikeys_create():
        """创建接口令牌（token 仅此一次返回，请妥善保存）"""
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name or len(name) > 100:
            return jsonify({'code': 400, 'msg': '名称必填且不超过 100 字符'}), 400
        role = (data.get('role') or request.admin.get('role') or 'admin')
        if role not in ('admin', 'super'):
            role = 'admin'
        expires_in = data.get('expires_in')  # 秒；None / 0 = 永不过期
        expires_at = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = str(int(time.time()) + int(expires_in))
        token = secrets.token_hex(32)  # 64 字符，>=40 且 != 2048，避开会话 token 分支
        now = str(int(time.time()))
        try:
            db.execute(
                "INSERT INTO api_tokens (token, name, role, created_by, created_at, expires_at, is_active) "
                "VALUES (%s, %s, %s, %s, %s, %s, 1)",
                (token, name, role, request.admin.get('username'), now, expires_at)
            )
            audit_log(request.admin['id'], request.admin['username'], 'apikey_create',
                      'api_token', name, {'role': role, 'expires_at': expires_at})
            return jsonify({
                'code': 0,
                'msg': '创建成功，token 仅显示一次',
                'data': {
                    'token': token,
                    'name': name,
                    'role': role,
                    'expires_at': expires_at,
                }
            })
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/apikeys/<int:kid>/revoke', methods=['POST'])
    @require_super
    def apikeys_revoke(kid):
        """吊销接口令牌（软删除：is_active=0）"""
        try:
            row = db.query_one("SELECT name FROM api_tokens WHERE id = %s", (kid,))
            if not row:
                return jsonify({'code': 404, 'msg': '令牌不存在'}), 404
            db.execute("UPDATE api_tokens SET is_active = 0 WHERE id = %s", (kid,))
            audit_log(request.admin['id'], request.admin['username'], 'apikey_revoke',
                      'api_token', row['name'], {'id': kid})
            return jsonify({'code': 0, 'msg': '已吊销'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500
