# -*- coding: utf-8 -*-
"""
用户 / 群管理
"""
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    db = ctx.db
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    # ---- 用户管理 ----

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

    # ---- 群管理 ----

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
