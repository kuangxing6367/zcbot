# -*- coding: utf-8 -*-
"""
统计图表 + 环境信息
"""
import logging
import os
import sys

from flask import jsonify, request

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    _project_root = ctx._project_root

    # ---- 统计图表 ----

    @app.route('/api/stats/commands', methods=['GET'])
    @require_auth
    def stats_commands():
        """命令命中统计：按插件分组，返回 TopN 命令"""
        try:
            top = min(int(request.args.get('top', 20)), 100)
            rows = db.query(
                "SELECT plugin_name, pattern, hit_count, description FROM commands "
                "WHERE is_active = 1 ORDER BY hit_count DESC LIMIT %s", (top,)
            )
            total = sum(r['hit_count'] for r in rows) if rows else 0
            return jsonify({'code': 0, 'data': {
                'total_hits': total,
                'commands': rows,
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/stats/messages', methods=['GET'])
    @require_auth
    def stats_messages():
        """消息统计：按天统计消息量（最近 30 天）"""
        try:
            rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
            if not rows:
                return jsonify({'code': 0, 'data': {'days': [], 'total': 0}})
            if framework.config.get('database', {}).get('type') == 'mysql':
                day_rows = db.query(
                    "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
                    "FROM messages WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
                    "GROUP BY DATE(created_at) ORDER BY day ASC"
                )
            else:
                day_rows = db.query(
                    "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
                    "FROM messages WHERE created_at >= datetime('now', '-30 days') "
                    "GROUP BY DATE(created_at) ORDER BY day ASC"
                )
            return jsonify({'code': 0, 'data': {
                'days': day_rows,
                'total': sum(r['cnt'] for r in day_rows) if day_rows else 0,
            }})
        except Exception:
            return jsonify({'code': 0, 'data': {'days': [], 'total': 0}})

    # ---- 环境信息 ----

    @app.route('/api/envinfo', methods=['GET'])
    @require_auth
    def envinfo():
        """获取系统环境信息（参考 Koishi envinfo 命令）"""
        import psutil  # 延迟导入，避免拖慢框架启动
        try:
            import platform
            import distro
            has_distro = True
        except ImportError:
            has_distro = False

        try:
            disk = psutil.disk_usage(_project_root())
            net = psutil.net_io_counters()
            proc = psutil.Process(os.getpid())
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()

            # Python 包信息
            packages = []
            try:
                import pkg_resources
                for pkg in sorted(pkg_resources.working_set, key=lambda x: x.key):
                    if pkg.key in ('pip', 'setuptools', 'wheel'):
                        continue
                    packages.append({'name': pkg.key, 'version': pkg.version})
            except Exception:
                try:
                    import importlib.metadata as im
                    for dist in im.distributions():
                        if dist.metadata['Name'] and dist.metadata['Name'] not in ('pip', 'setuptools', 'wheel'):
                            packages.append({'name': dist.metadata['Name'], 'version': dist.version})
                except Exception:
                    pass

            return jsonify({'code': 0, 'data': {
                'os': {
                    'system': platform.system(),
                    'release': platform.release(),
                    'version': platform.version(),
                    'machine': platform.machine(),
                    'arch': platform.architecture()[0],
                    'distro': distro.name(pretty=True) if has_distro else platform.platform(),
                },
                'cpu': {
                    'count': cpu_count,
                    'physical_count': psutil.cpu_count(logical=False) or cpu_count,
                    'freq_mhz': round(cpu_freq.current / 1000, 2) if cpu_freq else None,
                    'percent': psutil.cpu_percent(interval=None),
                },
                'memory': {
                    'total_mb': round(psutil.virtual_memory().total / 1024 / 1024, 1),
                    'available_mb': round(psutil.virtual_memory().available / 1024 / 1024, 1),
                },
                'disk': {
                    'total_gb': round(disk.total / 1024 / 1024 / 1024, 1),
                    'used_gb': round(disk.used / 1024 / 1024 / 1024, 1),
                    'free_gb': round(disk.free / 1024 / 1024 / 1024, 1),
                    'percent': disk.percent,
                },
                'network': {
                    'bytes_sent_mb': round(net.bytes_sent / 1024 / 1024, 1),
                    'bytes_recv_mb': round(net.bytes_recv / 1024 / 1024, 1),
                },
                'python': {
                    'version': sys.version.split()[0],
                    'executable': sys.executable,
                    'packages': packages[:50],  # 最多 50 个
                    'packages_total': len(packages),
                },
                'process': {
                    'pid': os.getpid(),
                    'threads': proc.num_threads(),
                    'open_files': len(proc.open_files()),
                    'connections': len(proc.connections()),
                    'create_time': proc.create_time(),
                },
                'database': {
                    'type': framework.config.get('database', {}).get('type', 'unknown'),
                },
            }})
        except Exception as e:
            logger.error(f"获取环境信息失败: {e}")
            return jsonify({'code': 500, 'msg': str(e)}), 500
