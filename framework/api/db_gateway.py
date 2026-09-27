# -*- coding: utf-8 -*-
"""
内嵌数据库管理（WebUI 浏览表 / 执行 SQL）
"""
import logging
import re

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    _quote_ident = ctx._quote_ident

    # 敏感表：普通管理员不可见内容、不可读数据（仅超级管理员），
    # 防止经 /api/db 泄露登录令牌与凭据哈希（token / password_hash / 密钥）。
    _SENSITIVE_TABLES = {'admin_users', 'api_tokens'}

    def _is_super() -> bool:
        """当前登录管理员是否为 super 角色"""
        return (getattr(request, 'admin', None) or {}).get('role') == 'super'

    @app.route('/api/db/tables', methods=['GET'])
    @require_auth
    def db_tables():
        """列出数据库所有表及行数（敏感表对普通管理员隐藏）"""
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
                if not _is_super() and t in _SENSITIVE_TABLES:
                    continue
                try:
                    row = db.query_one(f"SELECT COUNT(*) AS c FROM {_quote_ident(t)}")
                    cnt = row['c'] if row else 0
                except Exception:
                    cnt = None
                result.append({'name': t, 'rows': cnt})
            return jsonify({'code': 0, 'data': result})
        except Exception as e:
            logger.error(f"列出数据库表失败: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

    @app.route('/api/db/tables/<table>/schema', methods=['GET'])
    @require_auth
    def db_table_schema(table):
        """查看表结构（敏感表仅超级管理员）"""
        if not re.match(r'^[\w$]+$', table):
            return jsonify({'code': 400, 'msg': '非法表名'}), 400
        if table in _SENSITIVE_TABLES and not _is_super():
            return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
        try:
            if framework.config.get('database', {}).get('type') == 'mysql':
                rows = db.query(f"SHOW COLUMNS FROM `{table}`")
                return jsonify({'code': 0, 'data': rows})
            rows = db.query(f"PRAGMA table_info({_quote_ident(table)})")
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            logger.error(f"查看表结构失败 [{table}]: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

    @app.route('/api/db/tables/<table>/rows', methods=['GET'])
    @require_auth
    def db_table_rows(table):
        """分页查询表数据（敏感表仅超级管理员）"""
        if not re.match(r'^[\w$]+$', table):
            return jsonify({'code': 400, 'msg': '非法表名'}), 400
        if table in _SENSITIVE_TABLES and not _is_super():
            return jsonify({'code': 403, 'msg': '权限不足，需要超级管理员'}), 403
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
            logger.error(f"查询表数据失败 [{table}]: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500

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
            logger.error(f"执行 SQL 失败: {e}")
            return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
