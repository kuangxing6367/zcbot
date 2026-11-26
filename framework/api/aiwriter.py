"""AI 智能体的 Web 接口：会话与改动。

分两个域，因为这是用户视角里的两件事：

- **会话**：这轮对话存在哪、之前聊过什么。以前只有终端里能看见，
  浏览器里只有一个「发一句话」的输入框——本次把会话摆到明面上；
- **改动**：这一轮动了哪些文件、diff 长什么样、能不能一键退回去。

实现边界：
- 真本事都在 aiwriter 服务里（`framework.services['aiwriter']`），这里只做
  HTTP 适配 + 权限 + 审计，不重复实现任何 diff/回滚逻辑；
- 全部要求超级管理员：diff 里就是源码，跟文件浏览器同级敏感；
- 插件未启用时返回 503 而不是 500，前端据此给出「去启用 aiwriter」的指引。
"""
import json
import logging
import queue
import threading
import time

from flask import Response, jsonify, request

logger = logging.getLogger('zcbot')

# 会话 → 进行中的一轮。同一会话同时只允许一轮，避免两轮抢同一份会话历史。
_JOBS = {}
_JOBS_LOCK = threading.Lock()
# 全局串行执行：会话存储（sessions.json）与改动账本不是为并发写的，
# 同时跑两轮会互相覆盖历史。排队比并发安全，且能在前端显示「排队中」。
_RUN_LOCK = threading.Lock()


def _svc(framework):
    """取 aiwriter 服务；未启用返回 (None, 原因)。"""
    try:
        svc = framework.services.get('aiwriter')
    except Exception as e:  # 服务注册表异常不该把整个 WebUI 带崩
        logger.debug('[aiwriter] 取服务失败: %s', e)
        svc = None
    if svc is None:
        return None, '未启用 aiwriter 插件（core_plugins.yaml → aiwriter.enabled: true）'
    return svc, None


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    def _guard():
        svc, why = _svc(framework)
        if svc is None:
            return None, (jsonify({'code': 503, 'msg': why}), 503)
        return svc, None

    # ── 流式对话（真正的体验所在：边说边看，随时能停）──────────────────
    def _job_alive(key):
        with _JOBS_LOCK:
            return key in _JOBS

    @app.route('/api/aiwriter/chat/stream', methods=['POST'])
    @require_super
    def aiwriter_chat_stream():
        """SSE：把智能体的过程实时推给浏览器。

        为什么不是普通请求：一轮可能跑几十秒甚至几分钟，普通请求在这期间
        只有一个「正在处理」，用户既看不到进展，也没法中断。
        """
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return jsonify({'code': 400, 'msg': '消息不能为空'}), 400
        if len(text) > 8000:
            return jsonify({'code': 400, 'msg': '消息过长（上限 8000 字符）'}), 400
        session = str(data.get('session') or '').strip()
        key = session or svc.active_session()

        with _JOBS_LOCK:
            if key in _JOBS:
                return jsonify({
                    'code': 409,
                    'msg': '该会话已有一轮正在执行，先停止或等它结束',
                    'data': {'running': True, 'session': key},
                }), 409
            job = {
                'stop': threading.Event(),
                'queue': queue.Queue(maxsize=4000),
                'started_at': time.time(),
            }
            _JOBS[key] = job

        def _emit(kind, payload=None):
            """事件入队；队列满（客户端早断且没人读）时丢弃，不阻塞工作线程。"""
            try:
                job['queue'].put_nowait({'kind': str(kind), 'payload': payload})
            except queue.Full:
                pass

        def _worker():
            started = time.time()
            elapsed = 0.0
            try:
                # 排队：先等全局执行权，期间用户仍可取消
                _emit('queued', {'session': key})
                while not _RUN_LOCK.acquire(timeout=0.3):
                    if job['stop'].is_set():
                        _emit('cancelled', {'elapsed': 0})
                        return
                try:
                    if job['stop'].is_set():
                        _emit('cancelled', {'elapsed': 0})
                        return
                    _emit('start', {'session': key, 'model': getattr(svc, 'model_name', lambda: '')()})
                    final = svc.run(text, on_event=_emit, session=key,
                                    should_stop=job['stop'].is_set)
                    elapsed = round(time.time() - started, 1)
                    _emit('done', {
                        'text': final or '',
                        'change_id': getattr(svc, 'last_change_id', None),
                        'session': key,
                        'elapsed': elapsed,
                        'cancelled': job['stop'].is_set(),
                    })
                finally:
                    _RUN_LOCK.release()
            except Exception as e:
                logger.error('[aiwriter] 流式对话失败: %s', e)
                _emit('error', {'message': str(e)[:400]})
            finally:
                try:
                    audit_log(admin['id'], admin['username'], 'aiwriter_chat', 'aiwriter',
                              text[:120], {'ok': True, 'stream': True,
                                           'elapsed': elapsed,
                                           'change_id': getattr(svc, 'last_change_id', None)})
                except Exception as _exc:
                    logger.debug('[aiwriter] 审计写入失败: %s', _exc)
                try:
                    job['queue'].put_nowait(None)   # 哨兵：告诉生成器可以收尾
                except queue.Full:
                    pass

        threading.Thread(target=_worker, daemon=True, name='aiwriter-run').start()

        def _stream():
            q = job['queue']
            try:
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        yield ': ping\n\n'   # 心跳：防止代理把空闲连接掐掉
                        continue
                    if item is None:
                        break
                    yield 'data: ' + json.dumps(item, ensure_ascii=False) + '\n\n'
            finally:
                # 客户端断开（关页面/点停止后前端 abort）→ 让工作线程尽快收口，
                # 否则它会继续烧 token，而没人看得到结果。
                job['stop'].set()
                with _JOBS_LOCK:
                    if _JOBS.get(key) is job:
                        _JOBS.pop(key, None)

        resp = Response(_stream(), mimetype='text/event-stream')
        resp.headers['Cache-Control'] = 'no-cache, no-transform'
        resp.headers['X-Accel-Buffering'] = 'no'   # Nginx 不要缓冲
        return resp

    @app.route('/api/aiwriter/chat/cancel', methods=['POST'])
    @require_super
    def aiwriter_chat_cancel():
        """中断当前这一轮。已生成的内容仍会落进会话历史。"""
        svc, bad = _guard()
        if bad:
            return bad
        data = request.get_json(silent=True) or {}
        key = str(data.get('session') or '').strip() or svc.active_session()
        with _JOBS_LOCK:
            job = _JOBS.get(key)
        if job is None:
            return jsonify({'code': 0, 'data': {'cancelled': False, 'msg': '没有正在执行的一轮'}})
        job['stop'].set()
        return jsonify({'code': 0, 'data': {'cancelled': True, 'session': key}})

    @app.route('/api/aiwriter/chat/status', methods=['GET'])
    @require_super
    def aiwriter_chat_status():
        """当前是否有一轮在跑（页面刷新后据此恢复状态）。"""
        svc, bad = _guard()
        if bad:
            return bad
        key = request.args.get('session') or svc.active_session()
        with _JOBS_LOCK:
            job = _JOBS.get(key)
            running = job is not None
            started = job['started_at'] if job else None
        return jsonify({'code': 0, 'data': {'running': running, 'session': key,
                                            'started_at': started}})

    # ── 配置：页面内直接改，不用跑去设置页/yaml ─────────────────────────
    @app.route('/api/aiwriter/config', methods=['GET'])
    @require_super
    def aiwriter_config():
        svc, bad = _guard()
        if bad:
            return bad
        try:
            return jsonify({'code': 0, 'data': svc.config()})
        except Exception as e:
            logger.error('[aiwriter] 读配置失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取配置失败: %s' % e}), 500

    @app.route('/api/aiwriter/config', methods=['PUT'])
    @require_super
    def aiwriter_config_update():
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        ok, res = svc.configure(data)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_config', 'aiwriter',
                      ','.join(sorted(k for k in data if k != 'api_key')),
                      {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': svc.config(),
                        'msg': '已保存并立即生效'})

    # ── 对话 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/chat', methods=['POST'])
    @require_super
    def aiwriter_chat():
        """把一句话交给智能体，返回最终回复与执行事件。

        与终端 `code` 走同一个 `svc.run`，会话、工具、改动账本口径完全一致。
        `on_event` 收集中间事件（思考 / 工具调用 / 改动），前端据此展示过程。
        """
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return jsonify({'code': 400, 'msg': '消息不能为空'}), 400
        if len(text) > 8000:
            return jsonify({'code': 400, 'msg': '消息过长（上限 8000 字符）'}), 400
        session = str(data.get('session') or '').strip()

        events = []

        def _on_event(kind, payload):
            try:
                events.append({'kind': str(kind),
                               'payload': str(payload)[:2000]})
            except Exception:
                pass

        try:
            final = svc.run(text, on_event=_on_event, session=session or None)
        except Exception as e:
            logger.error('[aiwriter] 对话失败: %s', e)
            try:
                audit_log(admin['id'], admin['username'], 'aiwriter_chat', 'aiwriter',
                          text[:120], {'ok': False, 'err': str(e)[:200]})
            except Exception as _exc:
                logger.debug('[aiwriter] 审计写入失败: %s', _exc)
            return jsonify({'code': 500, 'msg': '智能体执行失败: %s' % e}), 500

        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_chat', 'aiwriter',
                      text[:120], {'ok': True, 'events': len(events),
                                   'change_id': getattr(svc, 'last_change_id', None)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        return jsonify({'code': 0, 'data': {
            'text': final or '',
            'events': events,
            'change_id': getattr(svc, 'last_change_id', None),
            'active': svc.active_session(),
        }})

    # ── 会话 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/sessions', methods=['GET'])
    @require_super
    def aiwriter_sessions():
        svc, bad = _guard()
        if bad:
            return bad
        try:
            data = {'active': svc.active_session(), 'sessions': svc.sessions(),
                    'workspace': svc.workspace()}
        except Exception as e:
            logger.error('[aiwriter] 列会话失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取会话失败: %s' % e}), 500
        return jsonify({'code': 0, 'data': data})

    @app.route('/api/aiwriter/sessions', methods=['POST'])
    @require_super
    def aiwriter_session_new():
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        ok, res = svc.new_session(name)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_new',
                      'aiwriter', name[:128] or '(默认名)', {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'key': res}})

    @app.route('/api/aiwriter/sessions/<path:key>', methods=['GET'])
    @require_super
    def aiwriter_session_detail(key):
        svc, bad = _guard()
        if bad:
            return bad
        try:
            msgs = svc.history(key) or []
        except Exception as e:
            logger.error('[aiwriter] 读取会话历史失败 %s: %s', key, e)
            return jsonify({'code': 500, 'msg': '读取会话历史失败: %s' % e}), 500
        return jsonify({'code': 0, 'data': {
            'key': key,
            'messages': [{'role': m.get('role'), 'content': m.get('content')} for m in msgs],
        }})

    @app.route('/api/aiwriter/sessions/<path:key>/activate', methods=['POST'])
    @require_super
    def aiwriter_session_activate(key):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        ok, res = svc.set_session(key)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_activate',
                      'aiwriter', key[:128], {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'key': res}})

    @app.route('/api/aiwriter/sessions/<path:key>', methods=['DELETE'])
    @require_super
    def aiwriter_session_delete(key):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        ok, res = svc.delete_session(key)
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_session_delete',
                      'aiwriter', key[:128], {'ok': bool(ok)})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        if not ok:
            return jsonify({'code': 400, 'msg': str(res)}), 400
        return jsonify({'code': 0, 'data': {'msg': str(res), 'active': svc.active_session()}})

    # ── 改动 ───────────────────────────────────────────────────────────
    @app.route('/api/aiwriter/changes', methods=['GET'])
    @require_super
    def aiwriter_changes():
        svc, bad = _guard()
        if bad:
            return bad
        try:
            limit = int(request.args.get('limit', 50) or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))
        try:
            return jsonify({'code': 0, 'data': svc.changes(limit=limit)})
        except Exception as e:
            logger.error('[aiwriter] 列改动失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取改动记录失败: %s' % e}), 500

    @app.route('/api/aiwriter/changes/<change_id>', methods=['GET'])
    @require_super
    def aiwriter_change_detail(change_id):
        svc, bad = _guard()
        if bad:
            return bad
        try:
            return jsonify({'code': 0, 'data': svc.change(change_id)})
        except Exception as e:
            logger.warning('[aiwriter] 读取改动记录失败 %s: %s', change_id, e)
            return jsonify({'code': 404, 'msg': str(e)}), 404

    @app.route('/api/aiwriter/changes/<change_id>/rollback', methods=['POST'])
    @require_super
    def aiwriter_change_rollback(change_id):
        svc, bad = _guard()
        if bad:
            return bad
        admin = request.admin
        try:
            result = svc.rollback(change_id)
        except Exception as e:
            logger.warning('[aiwriter] 回滚失败 %s: %s', change_id, e)
            try:
                audit_log(admin['id'], admin['username'], 'aiwriter_rollback',
                          'aiwriter', change_id, {'ok': False, 'err': str(e)[:200]})
            except Exception as _exc:
                logger.debug('[aiwriter] 审计写入失败: %s', _exc)
            return jsonify({'code': 400, 'msg': str(e)}), 400
        try:
            audit_log(admin['id'], admin['username'], 'aiwriter_rollback', 'aiwriter',
                      change_id, {'ok': True, 'restored': len(result.get('restored') or []),
                                  'skipped': len(result.get('skipped') or [])})
        except Exception as _exc:
            logger.debug('[aiwriter] 审计写入失败: %s', _exc)
        return jsonify({'code': 0, 'data': result})
