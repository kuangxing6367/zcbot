# -*- coding: utf-8 -*-
"""
系统配置 / OneBot 连接 / 运行状态 / config.yaml 分组读写
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

    # ---- OneBot 连接设置 ----

    @app.route('/api/connection', methods=['GET'])
    @require_auth
    def get_connection():
        """获取 OneBot 连接配置与实时连接状态"""
        cfg = _read_yaml_section('onebot') or (framework.config.get('onebot') or {})
        bots = framework.ws_server.get_connected_bots()
        return jsonify({'code': 0, 'data': {
            'config': cfg,
            'status': {
                'connected_bots': bots,
                'total': len(bots),
                'ws_port': framework.config.get('onebot', {}).get('listen_port', 6830),
            },
        }})

    @app.route('/api/connection', methods=['PUT'])
    @require_super
    def update_connection():
        """更新 OneBot 连接配置（写入 config.yaml 并同步内存）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        allowed = {k: data[k] for k in ('listen_host', 'listen_port', 'access_token') if k in data}

        if not allowed:
            return jsonify({'code': 400, 'msg': '没有可更新的字段'}), 400
        if 'listen_port' in allowed:
            try:
                allowed['listen_port'] = int(allowed['listen_port'])
            except (TypeError, ValueError):
                return jsonify({'code': 400, 'msg': 'listen_port 必须是整数'}), 400

        merged = dict(_read_yaml_section('onebot'))
        merged.update(allowed)
        if not _update_yaml_section('onebot', merged):
            return jsonify({'code': 500, 'msg': '写入 config.yaml 失败'}), 500

        # 同步内存配置（端口/监听地址改动需重启生效，access_token 立即生效）
        onebot = framework.config.setdefault('onebot', {})
        onebot.update(allowed)
        needs_restart = [k for k in allowed if k in ('listen_host', 'listen_port')]

        audit_log(admin['id'], admin['username'], 'update_connection', 'config', 'onebot', allowed)
        msg = '连接配置已保存'
        if needs_restart:
            msg += '，监听地址/端口改动需重启框架后生效'
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
            bots = framework.ws_server.get_connected_bots()
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
