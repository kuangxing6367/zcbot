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
            logger.error(f"list_admins 内部错误: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

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
            logger.error(f"add_admin 内部错误: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

    @app.route('/api/admins/<int:admin_id>', methods=['PUT'])
    @require_super
    def update_admin(admin_id):
        """修改管理员用户名 / 角色 / 启用状态"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        try:
            row = db.query_one(
                "SELECT id, username, role, is_active FROM admin_users WHERE id = %s", (admin_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '管理员不存在'}), 404

            username = str(data.get('username') or row['username']).strip()
            role = data.get('role', row['role'])
            is_active = data.get('is_active', bool(row['is_active']))
            if not username:
                return jsonify({'code': 400, 'msg': '用户名不能为空'}), 400
            if role not in ('super', 'admin'):
                return jsonify({'code': 400, 'msg': '角色无效'}), 400
            if not isinstance(is_active, bool):
                return jsonify({'code': 400, 'msg': '状态无效'}), 400

            # 不允许把自己降级或禁用，避免锁死管理面。
            if admin_id == admin['id'] and (role != 'super' or not is_active):
                return jsonify({'code': 400, 'msg': '不能降级或禁用当前账号'}), 400

            dup = db.query_one(
                "SELECT id FROM admin_users WHERE username = %s AND id <> %s", (username, admin_id))
            if dup:
                return jsonify({'code': 409, 'msg': '用户名已存在'}), 409

            # 最后一个 super 不允许降级 / 禁用。
            if row['role'] == 'super' and (role != 'super' or not is_active):
                count = db.query_one(
                    "SELECT COUNT(*) AS c FROM admin_users WHERE role = 'super' AND is_active = 1")
                if count and count['c'] <= 1:
                    return jsonify({'code': 400, 'msg': '至少保留一个启用的超级管理员'}), 400

            db.execute(
                "UPDATE admin_users SET username = %s, role = %s, is_active = %s WHERE id = %s",
                (username, role, 1 if is_active else 0, admin_id))
            audit_log(admin['id'], admin['username'], 'update_admin', 'admin', username,
                      {'role': role, 'is_active': is_active})
            return jsonify({'code': 0, 'msg': f'管理员 [{username}] 已更新'})
        except Exception as e:
            logger.error(f"update_admin 内部错误: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

    @app.route('/api/admins/<int:admin_id>/password', methods=['POST'])
    @require_super
    def reset_admin_password(admin_id):
        """超级管理员重置指定管理员密码；明文不落库，令牌同时失效。"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        password = data.get('password', '')
        if len(password) < 6:
            return jsonify({'code': 400, 'msg': '密码至少6位'}), 400
        try:
            row = db.query_one("SELECT username FROM admin_users WHERE id = %s", (admin_id,))
            if not row:
                return jsonify({'code': 404, 'msg': '管理员不存在'}), 404
            pwd_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
            db.execute(
                "UPDATE admin_users SET password_hash = %s, token_hash = NULL, "
                "token_created_at = NULL WHERE id = %s", (pwd_hash, admin_id))
            audit_log(admin['id'], admin['username'], 'reset_admin_password', 'admin', row['username'])
            return jsonify({'code': 0, 'msg': f'管理员 [{row["username"]}] 密码已重置'})
        except Exception as e:
            logger.error(f"reset_admin_password 内部错误: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

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
            logger.error(f"delete_admin 内部错误: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
