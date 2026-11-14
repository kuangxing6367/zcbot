"""安全模式（假启动）：启动崩溃达上限且守护进程持续重启时，不退出进程、不做事，仅挂起。"""
import logging
import os

logger = logging.getLogger('zcbot')


async def enter_safe_mode(stop_event, crash_cfg, root):
    """安全模式（假启动）：启动崩溃达上限且守护进程持续重启时，不退出进程、不做事，
    仅挂起，让 systemd/pm2（Type=simple 只看进程存在）误以为已启动、停止反复拉起，
    从而消除无限冷启动性能消耗。运维修复根因后，需先删除 error.log* 崩溃计数文件，
    再 kill 本进程（由守护进程拉起干净实例；新实例正常启动会自行清零计数）。
    """
    # 【踩坑保佑】v1.10.0 安全模式假启动
    # 坑：挂起让Type=simple守护进程停止反复拉起  →  enter_safe_mode
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

    crash = (crash_cfg.get('framework', {}) or {}).get('crash', {}) or {}
    path = crash.get('path') or os.path.join(root, 'error.log')
    print("\n" + "=" * 60)
    print("[安全模式] 启动崩溃次数已达上限，且检测到守护进程持续重启策略。")
    print("          为避免无限冷启动消耗性能，框架进入安全模式（假启动）：")
    print("          进程保持运行但不执行任何工作，使守护进程误以为已启动、停止拉起。")
    print(f"          请排查崩溃日志（{path} 及其 .N 轮转副本）修复根因，")
    print("          确认无误后先删除这些 error.log* 文件，再手动 kill 本进程")
    print("          （由守护进程拉起干净实例；新实例正常启动后会自行清零崩溃计数）。")
    print("=" * 60)
    try:
        from framework.crash_guard import fire_alert
        fire_alert(crash_cfg)
    except Exception as _exc:
        logger.debug('[enter_safe_mode] 捕获异常后按降级路径继续: %s', _exc)
    # 持有实例锁挂起，直到收到 SIGTERM/SIGINT 优雅退出
    await stop_event.wait()
