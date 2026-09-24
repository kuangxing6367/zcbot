# -*- coding: utf-8 -*-
"""
框架操作：版本 / 重启 / 终端命令（编排入口）

检查更新与源码更新见 framework_update.py；
register 同时注册两域路由（webapp 仅调用 framework_ops.register）。
"""
import asyncio
import io
import contextlib
import logging
import os
import sys
import threading

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    # 编排：版本检查 / 源码更新
    from framework.api import framework_update
    framework_update.register(ctx)

    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    audit_log = ctx.audit_log
    get_client_ip = ctx.get_client_ip
    _project_root = ctx._project_root
    _get_framework_local_version = ctx._get_framework_local_version
    _check_public_rate = ctx._check_public_rate

    @app.route('/api/version', methods=['GET'])
    def public_version():
        """公开版本信息（登录页/未登录页展示，无需认证；限速防刷）"""
        if not _check_public_rate(get_client_ip()):
            return jsonify({'code': 429, 'msg': '请求过于频繁，请稍后再试'}), 429
        ver = _get_framework_local_version()
        return jsonify({
            'code': 0,
            'data': {
                'name': 'ZCBOT',
                'version': ver or 'unknown',
                'alpha': 'alpha' in ver,
            }
        })

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
                    fut = asyncio.run_coroutine_threadsafe(framework.stop(), loop)
                    fut.result(timeout=15)
                else:
                    asyncio.run(framework.stop())
            except Exception:
                pass
            # os.execv 用当前 Python 解释器重载 main.py，原地替换进程
            python = sys.executable
            main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'main.py')
            os.chdir(os.path.dirname(main_py))
            os.execv(python, [python, main_py] + sys.argv[1:])

        threading.Thread(target=_do_restart, daemon=False).start()
        return jsonify({'code': 0, 'msg': '框架正在重启...'})

    @app.route('/api/terminal/exec', methods=['POST'])
    @require_auth
    def terminal_exec():
        """执行终端命令并返回输出"""
        admin = request.admin
        data = request.json or {}
        command = (data.get('command') or '').strip()
        if not command:
            return jsonify({'code': 400, 'msg': '命令不能为空'})

        from framework.terminal import terminal_commands

        # 查找命令处理器
        cmd_name = command.split()[0].lower()
        handler = terminal_commands.get(cmd_name)
        if handler is None:
            return jsonify({'code': 404, 'msg': f'未知命令: {cmd_name}，输入 help 查看可用命令'})

        args = command[len(cmd_name):].strip()

        # 捕获 stdout
        output_buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(output_buffer):
                handler(args)
        except Exception as e:
            output_buffer.write(f"\n执行异常: {e}")

        output = output_buffer.getvalue()
        audit_log(admin['id'], admin['username'], 'terminal_exec', 'system', 'terminal', {'command': command})
        return jsonify({'code': 0, 'data': {'output': output, 'command': command}})
