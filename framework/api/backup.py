"""数据库备份：状态查询与手动触发。

逻辑全在 ``framework/database/backup.py``，这里只做 HTTP 适配：
- ``GET  /api/db-backup``        看最近一次结果、现有备份、生效配置
- ``POST /api/db-backup/run``    立刻跑一次（超管 + 审计）
- ``POST /api/db-backup/config`` 改几个常用阈值（写回 config.yaml）

为什么要「改配置」这个端点：手写稿的要求是「以上所有内容均可通过配置文件
修改及自定义」。能改 config.yaml 就满足了，但改完要重启才生效；这里把最常调的
几项做成即时生效，省一次重启。
"""
import logging

from flask import jsonify, request

logger = logging.getLogger('zcbot')

# 允许通过接口即时修改的键。刻意只放「调阈值」，不放 enabled 之外的开关类
# 变更（例如 allow_large）——那些改错会让备份直接失控，留给配置文件改。
TUNABLE = {
    'enabled': bool,
    'interval_hours': float,
    'keep': int,
    'compress_threshold_kb': float,
    'compress_level': int,
    'refuse_above_mb': float,
    'allow_large': bool,
    'min_free_after_mb': float,
    'headroom_ratio': float,
    'verify': bool,
}


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log

    def _svc():
        return getattr(framework, 'backup_service', None)

    @app.route('/api/db-backup', methods=['GET'])
    @require_auth
    def db_backup_status():
        """状态：生效配置 + 最近一次结果 + 现有备份文件。"""
        from framework.database import backup as bk
        svc = _svc()
        if svc is None:
            return jsonify({'code': 503, 'msg': '自动备份服务不可用'}), 503
        try:
            data = svc.status()
        except Exception as e:
            logger.error('[db_backup_status] 读取状态失败: %s', e)
            return jsonify({'code': 500, 'msg': '读取备份状态失败: %s' % e}), 500
        data['defaults'] = {k: v for k, v in bk.DEFAULTS.items()
                            if not isinstance(v, float) or v != float('inf')}
        return jsonify({'code': 0, 'data': data})

    @app.route('/api/db-backup/run', methods=['POST'])
    @require_super
    def db_backup_run():
        """立刻跑一次。同步返回结果——备份完才回，前端据此显示 note。

        注意：超大库上这一步可能几十秒。前端应带 loading，不要当它是瞬时接口。
        """
        svc = _svc()
        if svc is None:
            return jsonify({'code': 503, 'msg': '自动备份服务不可用'}), 503
        admin = request.admin
        report = svc.run_once('manual')
        try:
            audit_log(admin['id'], admin['username'], 'db_backup_run', 'database',
                      (report.get('note') or '')[:512],
                      {'ok': bool(report.get('ok')), 'status': report.get('status')})
        except Exception as e:
            # 审计写不进去不该让备份结果丢失，但必须留痕
            logger.warning('[db_backup_run] 审计写入失败: %s', e)
        return jsonify({'code': 0, 'data': report})

    @app.route('/api/db-backup/config', methods=['POST'])
    @require_super
    def db_backup_config():
        """改常用阈值并即时生效（同时写回 config.yaml，重启后仍有效）。"""
        svc = _svc()
        if svc is None:
            return jsonify({'code': 503, 'msg': '自动备份服务不可用'}), 503
        from framework.database import backup as bk

        data = request.get_json(silent=True) or {}
        patch = {}
        for key, cast in TUNABLE.items():
            if key not in data:
                continue
            raw = data[key]
            try:
                if cast is bool:
                    if isinstance(raw, str):
                        raw = raw.strip().lower() in ('1', 'true', 'yes', 'on', 'y')
                    patch[key] = bool(raw)
                else:
                    patch[key] = cast(raw)
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': '配置项 %s 的值不合法: %r'
                                % (key, raw)}), 400
        if not patch:
            return jsonify({'code': 400, 'msg': '没有可更新的配置项'}), 400

        merged = dict(svc._cfg or {})
        merged.update(patch)
        conf = bk.load_config(merged)  # 先过一遍钳制，非法值在这里就被拉回范围内

        # 写回 config.yaml：复用 webapp 里那套「按行替换、保注释保键序」的写法，
        # 不自己 yaml.dump —— 那会把用户手写的注释与键序全冲掉。
        try:
            section = dict(ctx._read_yaml_section('database') or {})
            section['backup'] = conf
            if not ctx._update_yaml_section('database', section):
                raise RuntimeError('写入 config.yaml 失败')
        except Exception as e:
            # 写盘失败就只在内存生效 —— 要说清楚，不能让人以为重启后还在
            logger.warning('[db_backup_config] 写回 config.yaml 失败: %s', e)
            return jsonify({'code': 500, 'msg': '写回配置文件失败（本次修改未持久化）: %s'
                            % e}), 500

        svc._cfg = conf
        svc._conf = conf
        try:
            admin = request.admin
            audit_log(admin['id'], admin['username'], 'db_backup_config', 'database',
                      str(patch)[:512], {'ok': True})
        except Exception as e:
            logger.warning('[db_backup_config] 审计写入失败: %s', e)
        return jsonify({'code': 0, 'data': {'config': conf, 'applied': patch,
                                            'note': '已写回 config.yaml 并即时生效'}})
