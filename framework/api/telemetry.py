"""遥测状态接口：查看开关 / 上次上报，并允许超管手动上报一次。"""
import logging

from flask import jsonify

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    require_super = ctx.require_super

    @app.route('/api/telemetry/status', methods=['GET'])
    @require_auth
    def telemetry_status():
        try:
            from framework import telemetry
            return jsonify({'code': 0, 'data': telemetry.status(framework)})
        except Exception as e:
            logger.error('[telemetry] 读取状态失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取状态失败'}), 500

    @app.route('/api/telemetry/report', methods=['POST'])
    @require_super
    def telemetry_report():
        try:
            import asyncio

            from framework import telemetry
            asyncio.run_coroutine_threadsafe(
                telemetry.report_now(framework), framework.loop).result(timeout=30)
            return jsonify({'code': 0, 'data': telemetry.status(framework)})
        except Exception as e:
            logger.warning('[telemetry] 手动上报失败: %s', e)
            return jsonify({'code': 400, 'msg': str(e)}), 400
