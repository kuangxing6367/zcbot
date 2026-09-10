# -*- coding: utf-8 -*-
"""
定时任务管理
"""
import asyncio
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    audit_log = ctx.audit_log

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
