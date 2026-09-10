# -*- coding: utf-8 -*-
"""
管理员管理接口（仅超级管理员）
"""
import logging

import bcrypt
from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    db = ctx.db
    require_super = ctx.require_super
    audit_log = ctx.audit_log

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
