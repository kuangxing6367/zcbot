"""Web 终端：把框架的终端命令表与 aiwriter 智能体接到 WebUI。

为什么复用 `ConsoleServer` 而不是新写一个执行器：
`framework/console_server.py` 已经把「终端命令」抽象成 `run_command()`（捕获 stdout、
注入 remote_session），把「智能体」抽象成 `run_agent()`（收集 tool 事件 + 最终答复）。
Web 只是换了一个前端外壳，能力必须与运维终端**完全一致**——另写一套必然会漂移。

安全边界：
- 两个端点都要求超级管理员：`restart` / `update` / 插件热重载这些命令能直接改运行状态；
- 命令在**框架进程内**执行（不是起新 shell），没有参数拼接、没有 `shell=True`；
- 每次执行写审计（命令截断到 512 字符，输出不入库）；
- 执行有超时上限：超时只返回错误，不会阻塞 Web 线程无限久（线程本身无法强杀，
  这一点在文档里写明）。
"""
import concurrent.futures
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')

# 命令与智能体的超时上限（秒）。智能体要读仓库、跑工具、等模型流式输出，给得宽一些。
CMD_TIMEOUT = 120
AGENT_TIMEOUT = 900


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    def _server():
        return getattr(framework, 'console_server', None)

    def _run(fn, args, timeout):
        """在独立线程里跑，带超时；异常统一转成错误文本。

        `args` 是位置参数元组——智能体要区分「说什么」和「在哪个会话里说」，
        单参数版本早晚要改成可变参数，不如一开始就说清。
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(fn, *(args or ()))
            return fut.result(timeout=timeout), None
        except concurrent.futures.TimeoutError:
            return None, f'执行超时（>{timeout}s）：任务仍在后台线程继续，可稍后查看日志'
        except Exception as e:
            logger.error('[console] 执行失败: %s', e)
            return None, f'执行失败: {e}'
        finally:
            # 不等待任务：超时的任务还要继续跑完，不能让 Web 请求挂在这里
            pool.shutdown(wait=False)

    @app.route('/api/console/commands', methods=['GET'])
    @require_auth
    def console_commands():
        """终端命令清单（供前端补全 / 帮助面板）"""
        try:
            from framework.terminal import terminal_commands
            reg = terminal_commands.list_commands()
        except Exception as e:
            logger.debug('[console] 命令表不可用: %s', e)
            return jsonify({'code': 0, 'data': [], 'msg': '终端命令模块不可用'})
        data = [{'name': k, 'description': v or ''} for k, v in sorted(reg.items())]
        return jsonify({'code': 0, 'data': data})

    @app.route('/api/console/exec', methods=['POST'])
    @require_super
    def console_exec():
        """执行一条终端命令，返回它打印的内容。"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        command = str(data.get('command') or '').strip()
        if not command:
            return jsonify({'code': 400, 'msg': '命令不能为空'}), 400
        srv = _server()
        if srv is None:
            return jsonify({'code': 503, 'msg': '调试控制台未启动，无法在 Web 执行命令'}), 503

        output, err = _run(srv.run_command, (command,), CMD_TIMEOUT)
        try:
            audit_log(admin['id'], admin['username'], 'console_exec', 'console',
                      command[:512], {'ok': err is None})
        except Exception as _exc:
            logger.debug('[console] 审计写入失败: %s', _exc)
        if err:
            return jsonify({'code': 500, 'msg': err, 'data': {'command': command}}), 500
        return jsonify({'code': 0, 'data': {'command': command, 'output': output or ''}})

    @app.route('/api/console/agent', methods=['POST'])
    @require_super
    def console_agent():
        """把一句话交给 aiwriter 智能体，返回「工具过程 + 最终答复」。

        可选 `session`：指定在哪个会话里说。不传则用服务当前会话——
        与运维终端里 `code` 打开的界面共享同一份历史。
        """
        admin = request.admin
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        session = str(data.get('session') or '').strip() or None
        if not text:
            return jsonify({'code': 400, 'msg': '内容不能为空'}), 400
        srv = _server()
        if srv is None:
            return jsonify({'code': 503, 'msg': '调试控制台未启动，智能体不可用'}), 503

        output, err = _run(srv.run_agent, (text, session), AGENT_TIMEOUT)
        change_id = getattr(srv, 'last_agent_change', None)
        try:
            audit_log(admin['id'], admin['username'], 'console_agent', 'console',
                      text[:512], {'ok': err is None, 'session': session,
                                   'change': change_id})
        except Exception as _exc:
            logger.debug('[console] 审计写入失败: %s', _exc)
        if err:
            return jsonify({'code': 500, 'msg': err}), 500
        return jsonify({'code': 0, 'data': {'text': text, 'output': output or '',
                                            'session': session, 'change': change_id}})
