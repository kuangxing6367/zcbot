# -*- coding: utf-8 -*-
"""
审计日志 + 运行日志（支持 SSE 实时推送）
"""
import json
import logging
import queue

from flask import Response, jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    db = ctx.db
    require_auth = ctx.require_auth
    audit_log = ctx.audit_log
    log_broker = ctx.log_broker
    _extract_token = ctx._extract_token
    _verify_token = ctx._verify_token

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
