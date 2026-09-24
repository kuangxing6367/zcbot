# -*- coding: utf-8 -*-
"""终端命令共享 helper"""
import os


def installed_core_plugins() -> set:
    """扫描 core_plugins/ 目录得到已安装官方插件名"""
    plugins_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'core_plugins')
    try:
        return {
            n for n in os.listdir(plugins_dir)
            if not n.startswith('_') and os.path.isfile(os.path.join(plugins_dir, n, 'main.py'))
        }
    except Exception:
        return set()
