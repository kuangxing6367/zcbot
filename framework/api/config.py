# -*- coding: utf-8 -*-
"""
系统配置 / 接入端连接 / 运行状态 / config.yaml 分组读写
"""
import json
import logging
import os
import sys
import time

import yaml
from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    _read_yaml_section = ctx._read_yaml_section
    _update_yaml_section = ctx._update_yaml_section
    _yaml_config_path = ctx._yaml_config_path

    # ---- 系统配置 ----

    @app.route('/api/config', methods=['GET'])
    @require_auth
    def list_config():
        """获取系统配置"""
        try:
            rows = db.query("SELECT config_key, config_value, description, updated_by FROM system_config")
            return jsonify({'code': 0, 'data': rows})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/config/<key>', methods=['PUT'])
    @require_super
    def update_config(key):
        """更新系统配置（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        value = data.get('value')

        if value is None:
            return jsonify({'code': 400, 'msg': '缺少 value'}), 400

        try:
            db.execute(
                "UPDATE system_config SET config_value = %s, updated_by = %s WHERE config_key = %s",
                (json.dumps(value, ensure_ascii=False), admin['username'], key)
            )
            audit_log(admin['id'], admin['username'], 'update_config', 'config', key, {'value': value})
            return jsonify({'code': 0, 'msg': '配置已更新'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 接入端连接设置（协议中立，由适配器 get_connection_info 自描述）----

    def _collect_connection_adapters() -> list:
        """汇总全部已注册接入端的连接描述 + 实时状态"""
        services = framework.services
        adapters = getattr(services, 'protocol_adapters', None)
        adapters = adapters() if callable(adapters) else {}
        if not adapters:
            primary = services.get('protocol_adapter')
            if primary is not None:
                try:
                    adapters = {primary._connection_id(): primary}
                except Exception:
                    adapters = {}
        out = []
        for aid, adapter in adapters.items():
            try:
                meta = adapter.get_connection_info()
            except Exception:
                meta = None
            if not isinstance(meta, dict):
                continue
            section = str(meta.get('config_section') or '')
            cfg = {}
            if section:
                cfg = dict(_read_yaml_section(section) or (framework.config.get(section) or {}))
            try:
                bots = adapter.get_connected_bots() or []
            except Exception:
                bots = []
            status = {'connected_bots': bots, 'total': len(bots)}
            extra = meta.get('status_extra')
            if isinstance(extra, dict):
                status.update(extra)
            fields = meta.get('fields') or []
            out.append({
                'id': str(meta.get('id') or aid),
                'name': str(meta.get('name') or aid),
                'config_section': section,
                'config': cfg,
                'fields': fields,
                'restart_keys': list(meta.get('restart_keys') or
                                     [f.get('key') for f in fields
                                      if isinstance(f, dict) and f.get('type') == 'number'][:1]),
                'endpoint_hint': meta.get('endpoint_hint'),
                'guide': meta.get('guide'),
                'status': status,
            })
        return out

    @app.route('/api/connection', methods=['GET'])
    @require_auth
    def get_connection():
        """获取全部接入端的连接配置与实时状态（由适配器自描述）"""
        adapters = _collect_connection_adapters()
        # 兼容旧 WebUI：扁平取主接入端
        primary = adapters[0] if adapters else {
            'config': {}, 'status': {'connected_bots': [], 'total': 0},
        }
        return jsonify({'code': 0, 'data': {
            'adapters': adapters,
            'config': primary.get('config') or {},
            'status': primary.get('status') or {'connected_bots': [], 'total': 0},
        }})

    @app.route('/api/connection', methods=['PUT'])
    @require_super
    def update_connection():
        """更新接入端连接配置（body.adapter 指定接入端，缺省取第一个）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        adapter_id = data.get('adapter')
        adapters = _collect_connection_adapters()
        target = None
        if adapter_id:
            target = next((a for a in adapters if a['id'] == adapter_id), None)
            if target is None:
                return jsonify({'code': 404, 'msg': f'未知接入端: {adapter_id}'}), 404
        else:
            target = adapters[0] if adapters else None
        if not target or not target.get('config_section'):
            return jsonify({'code': 400, 'msg': '当前无接入端可配置'}), 400

        section = target['config_section']
        field_keys = [f.get('key') for f in (target.get('fields') or [])
                      if isinstance(f, dict) and f.get('key')]
        if not field_keys:
            return jsonify({'code': 400, 'msg': '该接入端未声明可编辑字段'}), 400

        payload = data.get('data') if isinstance(data.get('data'), dict) else data
        allowed = {k: payload[k] for k in field_keys if k in payload}
        # 兼容旧客户端：直接平铺字段
        if not allowed and not payload.get('data'):
            allowed = {k: data[k] for k in field_keys if k in data}
        if not allowed:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400
        if 'listen_port' in allowed:
            try:
                allowed['listen_port'] = int(allowed['listen_port'])
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'listen_port 必须是整数'}), 400

        merged = dict(_read_yaml_section(section))
        merged.update(allowed)
        if not _update_yaml_section(section, merged):
            return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500

        # 同步内存配置（端口/监听地址改动需重启生效，access_token 立即生效）
        framework.config.setdefault(section, {}).update(allowed)
        restart_keys = set(target.get('restart_keys') or [])
        needs_restart = [k for k in allowed if k in restart_keys]

        audit_log(admin['id'], admin['username'], 'update_connection', 'config', section, allowed)
        msg = '连接配置已保存'
        if needs_restart:
            msg += '，标注「需重启」的字段改动需重启框架后生效'
        return jsonify({'code': 0, 'msg': msg, 'data': {'needs_restart': needs_restart}})

    # ---- 运行状态 ----

    @app.route('/api/runtime/stats', methods=['GET'])
    @require_auth
    def runtime_stats():
        """进程/系统运行状态（WebUI 实时轮询）"""
        import psutil  # 延迟导入，避免拖慢框架启动
        try:
            proc = psutil.Process(os.getpid())
            mem = psutil.virtual_memory()
            boot = proc.create_time()
            uptime = max(0, int(time.time() - boot))
            bots = []
            try:
                primary = framework.services.primary_adapter() \
                    if hasattr(framework.services, 'primary_adapter') else None
                if primary is None:
                    primary = framework.services.get('protocol_adapter')
                if primary is not None and hasattr(primary, 'get_connected_bots'):
                    bots = primary.get_connected_bots() or []
            except Exception:
                bots = []
            db_type = framework.config.get('database', {}).get('type', 'unknown')

            # 插件内存占用（已加载插件）
            plugin_mem = {}
            try:
                for pname, mod in framework.plugin_loader.get_loaded_plugins().items():
                    try:
                        plugin_mem[pname] = round(
                            psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
                    except Exception:
                        pass
            except Exception:
                pass

            return jsonify({'code': 0, 'data': {
                'cpu_percent': round(psutil.cpu_percent(interval=None) or 0, 1),
                'memory': {
                    'used_mb': round(mem.used / 1024 / 1024, 1),
                    'total_mb': round(mem.total / 1024 / 1024, 1),
                    'percent': round(mem.percent, 1),
                },
                'process_memory_mb': round(proc.memory_info().rss / 1024 / 1024, 1),
                'threads': proc.num_threads(),
                'uptime_seconds': uptime,
                'python_version': sys.version.split()[0],
                'db_type': db_type,
                'ws': {'connected': len(bots), 'bots': bots},
            }})
        except Exception as e:
            logger.error(f"获取运行状态失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500

    # ---- 系统配置（config.yaml 分组读写）----

    _YAML_SECTIONS = ('database', 'onebot', 'web', 'ssl', 'plugin', 'log', 'system', 'security')

    @app.route('/api/config/yaml', methods=['GET'])
    @require_auth
    def get_yaml_config():
        """读取 config.yaml 全量配置"""
        path = _yaml_config_path()
        if not os.path.isfile(path):
            return jsonify({'code': 404, 'msg': 'config.yaml 不存在'}), 404
        try:
            with open(path, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f) or {}
            return jsonify({'code': 0, 'data': doc})
        except Exception as e:
            return jsonify({'code': 500, 'msg': f'解析失败: {e}'}), 500

    @app.route('/api/config/yaml/<section>', methods=['PUT'])
    @require_super
    def update_yaml_config(section):
        """分组更新 config.yaml（合并更新，保留未提交字段）"""
        admin = request.admin
        if section not in _YAML_SECTIONS:
            return jsonify({'code': 400, 'msg': f'非法配置段，可选: {", ".join(_YAML_SECTIONS)}'}), 400

        data = request.get_json(silent=True) or {}
        values = data.get('data')
        if not isinstance(values, dict) or not values:
            return jsonify({'code': 400, 'msg': 'data 必须是非空对象'}), 400

        merged = dict(_read_yaml_section(section))
        merged.update(values)
        if not _update_yaml_section(section, merged):
            return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500

        framework.config.setdefault(section, {}).update(values)
        audit_log(admin['id'], admin['username'], 'update_yaml_config', 'config', section, values)
        return jsonify({'code': 0, 'msg': f'配置段 [{section}] 已保存，部分字段需重启生效'})
