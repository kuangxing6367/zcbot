# 第 15 轮侦察材料：framework/api/webapp.py 第 500~629 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
 500                 stale = [k for k, v in _auth_failures.items()
 501                          if not v or now - v[-1] >= _AUTH_FAIL_WINDOW]
 502                 for k in stale:
 503                     _auth_failures.pop(k, None)
 504             if _AUTH_FAIL_LIMIT > 0 and len(lst) >= _AUTH_FAIL_LIMIT:
 505                 hit = True
 506         if hit:
 507             try:
 508                 expires_at = (time.time() + _AUTH_BLACKLIST_TTL) if _AUTH_BLACKLIST_TTL > 0 else None
 509                 dual_auth.blacklist(ip, reason=f'认证连续失败 {len(lst)} 次',
 510                                     source='honeypot', expires_at=expires_at, soft=True)
 511                 logger.warning(f"[认证防护] IP 因连续认证失败被软封禁: {ip}")
 512             except Exception as e:
 513                 logger.warning(f"[认证防护] 拉黑失败 {ip}: {e}")
 514 
 515     def _deny_auth():
 516         """统一的认证拒绝：延迟 + （按配置）静默丢弃 / 中性文案"""
 517         _record_auth_failure(get_client_ip())
 518         _tarpit_sleep(_AUTH_TARPIT_MS)
 519         if _AUTH_DECOY:
 520             # 诱饵模式：返回看似成功的假数据（会让前端无法感知过期，谨慎开启）
 521             return jsonify({'code': 0, 'msg': 'ok',
 522                             'data': {'server': 'node-01', 'token': secrets.token_hex(16)}})
 523         if _SILENT_AUTH:
 524             # 「不对就不理」：不返回任何有意义内容，仅关闭连接
 525             return _silent_drop()
 526         return jsonify({'code': 401, 'msg': 'Unauthorized'}), 401
 527 
 528     # ---- 刷新过快检测（同一 IP 5 秒内刷新页面 ≥2 次 → 引导到 /reset）----
 529     _refresh_windows = {}          # ip -> list[timestamp]（仅记录页面导航请求）
 530     _refresh_lock = threading.Lock()
 531     _REFRESH_WINDOW = 5            # 秒
 532     _REFRESH_LIMIT = 5             # 窗口内触发阈值
 533 
 534     @app.before_request
 535     def _detect_rapid_reload():
 536         """同一 IP 在 5 秒内刷新页面超过阈值时，重定向到 /reset 恢复页。"""
 537         if request.method != 'GET':
 538             return None
 539         path = request.path or '/'
 540         is_page = (path == '/' or path.endswith('.html') or path == '/reset')
 541         if not is_page or path.startswith('/api/'):
 542             return None
 543         ip = get_client_ip()
 544         now = time.time()
 545         with _refresh_lock:
 546             ts = [t for t in _refresh_windows.get(ip, []) if now - t < _REFRESH_WINDOW]
 547             ts.append(now)
 548             _refresh_windows[ip] = ts
 549             count = len(ts)
 550             if len(_refresh_windows) > 2048:
 551                 stale = [k for k, v in _refresh_windows.items() if not v or now - v[-1] >= 600]
 552                 for k in stale:
 553                     _refresh_windows.pop(k, None)
 554         if count >= _REFRESH_LIMIT and path != '/reset':
 555             from flask import redirect
 556             return redirect('/reset', code=302)
 557         return None
 558 
 559     # ---- 扩展点：Web 请求前后（插件可在此做鉴权增强/审计/改写响应）----
 560     @app.before_request
 561     def _hook_before_request():
 562         """请求前扩展点：handler 可返回 Flask Response 以短路请求。"""
 563         fw = getattr(app, '_framework', None)
 564         hooks = getattr(fw, 'hooks', None) if fw else None
 565         if hooks is None:
 566             return None
 567         try:
 568             results = hooks.trigger_sync('http.before_request', request)
 569             for r in (results or []):
 570                 if hasattr(r, 'status_code'):   # 形如 Flask Response
 571                     return r
 572         except Exception as e:
 573             logger.warning(f"http.before_request 扩展点异常: {e}")
 574         return None
 575 
 576     @app.after_request
 577     def _hook_after_request(resp):
 578         """请求后扩展点：可读取/改写响应，必须返回响应对象。"""
 579         fw = getattr(app, '_framework', None)
 580         hooks = getattr(fw, 'hooks', None) if fw else None
 581         if hooks is not None:
 582             try:
 583                 hooks.trigger_sync('http.after_request', request, resp)
 584             except Exception as e:
 585                 logger.warning(f"http.after_request 扩展点异常: {e}")
 586         return resp
 587 
 588     # ---- 共享工具函数（版本 / yaml / 插件市场 / GitHub 下载）----
 589     from framework.api.app_helpers import make_app_helpers
 590     helpers = make_app_helpers(framework, db, plugins_dir)
 591 
 592     _trusted_proxies = set(framework.config.get('security', {}).get('trusted_proxies', []))
 593 
 594     def get_client_ip():
 595         # 【踩坑保佑】部署上线 反向代理XFF
 596         # 坑：让风控把攻击者识别成本机  →  get_client_ip
 597         #                    _ooOoo_
 598         #                   o8888888o
 599         #                   88" . "88
 600         #                   (| -_- |)
 601         #                    O\ = /O
 602         #                ____/`---'\____
 603         #              .   ' \\| |// `.
 604         #               / \\||| : |||// \
 605         #             / _||||| -:- |||||- \
 606         #               | | \\\ - /// | |
 607         #             | \_| ''\---/'' | |
 608         #              \ .-\__ `-` ___/-. /
 609         #           ___`. .' /--.--\ `. . __
 610         #        ."" '< `.___\_<|>_/___.' >'"".
 611         #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
 612         #         \ \ `-. \_ __\ /__ _/ .-` / /
 613         # ======`-.____`-.___\_____/___.-`____.-'======
 614         #                    `=---='
 615         #
 616         # .............................................
 617         #          佛祖保佑             永无BUG
 618 
 619         remote = request.remote_addr or 'unknown'
 620         if _trusted_proxies and remote in _trusted_proxies:
 621             xff = request.headers.get('X-Forwarded-For', '')
 622             if xff:
 623                 parts = [p.strip() for p in xff.split(',') if p.strip()]
 624                 if parts:
 625                     return parts[0]
 626         return remote
 627 
 628     def audit_log(admin_id, admin_name, action, target_type=None, target_name=None,
 629                   detail=None, result='success', error_message=None):
```
