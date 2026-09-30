# -*- coding: utf-8 -*-
"""
终端命令：插件启停 / 配置 / 重载（enable / disable / config / reload）
"""
import logging
import os
import sys

from .command import terminal_commands
from .helper import installed_core_plugins

logger = logging.getLogger('zcbot')


def register(fw):
    """注册本组终端命令"""
    def cmd_enable(args):
        """启用插件: enable <插件名>"""
        plugin_name = args.strip()
        if not plugin_name:
            print("用法: enable <插件名>")
            print("示例: enable onebot_adapter")
            return

        # 检查是否是核心插件
        core_plugins = installed_core_plugins()
        if plugin_name in core_plugins:
            # 更新配置
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                if 'core_plugins' not in config:
                    config['core_plugins'] = {}
                config['core_plugins'][plugin_name] = True
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已启用核心插件 [{plugin_name}]，重启后生效")
            except Exception as e:
                print(f"启用失败: {e}")
        else:
            # 用户插件
            try:
                fw.plugin_loader.enable_plugin(plugin_name)
                print(f"已启用插件 [{plugin_name}]")
            except Exception as e:
                print(f"启用失败: {e}")

    def cmd_disable(args):
        """禁用插件: disable <插件名>"""
        plugin_name = args.strip()
        if not plugin_name:
            print("用法: disable <插件名>")
            print("示例: disable onebot_adapter")
            return

        # 检查是否是核心插件
        core_plugins = installed_core_plugins()
        if plugin_name in core_plugins:
            # 更新配置
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                if 'core_plugins' not in config:
                    config['core_plugins'] = {}
                config['core_plugins'][plugin_name] = False
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已禁用核心插件 [{plugin_name}]，重启后生效")
            except Exception as e:
                print(f"禁用失败: {e}")
        else:
            # 用户插件
            try:
                fw.plugin_loader.disable_plugin(plugin_name)
                print(f"已禁用插件 [{plugin_name}]")
            except Exception as e:
                print(f"禁用失败: {e}")

    def cmd_config(args):
        """查看/修改配置: config [key] [value]"""
        parts = args.split(maxsplit=1)
        if not parts:
            # 显示所有配置
            print("当前配置:")
            print("-" * 50)
            import yaml
            with open(fw.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            for section, values in config.items():
                if isinstance(values, dict):
                    print(f"  {section}:")
                    for k, v in values.items():
                        print(f"    {k}: {v}")
                else:
                    print(f"  {section}: {values}")
            print("-" * 50)
            return

        key = parts[0]
        if len(parts) == 1:
            # 查看单个配置
            import yaml
            with open(fw.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            # 支持点号分隔的路径
            keys = key.split('.')
            value = config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                else:
                    value = None
                    break
            if value is not None:
                print(f"{key} = {value}")
            else:
                print(f"配置项 {key} 不存在")
        else:
            # 修改配置
            value = parts[1]
            import yaml
            config_path = fw.config_path
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                # 支持点号分隔的路径
                keys = key.split('.')
                target = config
                for k in keys[:-1]:
                    if k not in target:
                        target[k] = {}
                    target = target[k]
                # 尝试转换类型
                if value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        try:
                            value = float(value)
                        except ValueError:
                            pass
                target[keys[-1]] = value
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                print(f"已设置 {key} = {value}")
            except Exception as e:
                print(f"设置失败: {e}")

    def _reload_core_plugin(name):
        """官方插件（core_plugins/）重载：合成模块名重导入 + register + 回填 loader。

        plugin_loader.load_plugin 只认 plugins/ 目录；官方插件必须走本线路。
        （修复历史缺陷：内置 reload 调用的 reload_plugin/reload_all 方法并不存在，
        旧命令每次都静默失败。）"""
        import importlib.util
        main_file = os.path.join(fw._get_core_plugins_dir(), name, 'main.py')
        if not os.path.isfile(main_file):
            return False
        try:
            fw.plugin_loader.unload_plugin(name)
            sys.modules.pop(f"core_plugin_{name}", None)
            spec = importlib.util.spec_from_file_location(f"core_plugin_{name}", main_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"core_plugin_{name}"] = module
            spec.loader.exec_module(module)
            from framework.ctx import PluginContext
            ctx = PluginContext(f"core:{name}", fw)
            module.ctx = ctx
            if hasattr(module, 'register'):
                module.register(ctx)
                try:
                    from framework.plugin import flush as _flush_plugin
                    _flush_plugin(module.__name__, ctx)
                except Exception:
                    pass
            with fw.plugin_loader._lock:
                meta = getattr(module, '__plugin_meta__', {})
                fw.plugin_loader._loaded_plugins[name] = {
                    'module': module,
                    'path': os.path.dirname(main_file),
                    'meta': meta,
                    'priority': meta.get('priority', 50),
                    'yaml': {},
                }
            return True
        except Exception as e:
            logger.error(f"[{name}] 官方插件重载异常: {e}")
            return False

    def _reload_one(name):
        # 官方插件专用线路（core_plugins/ 有、plugins/ 没有同名目录）
        if os.path.isfile(os.path.join(fw._get_core_plugins_dir(), name, 'main.py')) and                 not os.path.isdir(os.path.join(fw._get_plugins_dir(), name)):
            return _reload_core_plugin(name)
        fw.plugin_loader.unload_plugin(name)
        if not fw.plugin_loader.load_plugin(name):
            return False
        try:
            fw.plugin_loader.register_commands(name)
        except Exception:
            pass
        try:
            fw.router._invalidate_cache()
        except Exception:
            pass
        return True

    def cmd_reload(args):
        """重载插件: reload [插件名]（缺省重载全部已加载插件）"""
        try:
            target = args.strip()
            if target:
                ok = _reload_one(target)
                print(f"插件 [{target}] 重载{'成功' if ok else '失败'}")
                return
            with fw.plugin_loader._lock:
                loaded = list(fw.plugin_loader._loaded_plugins.keys())
            if not loaded:
                print("没有已加载的插件")
                return
            ok_list, fail_list = [], []
            for name in loaded:
                (ok_list if _reload_one(name) else fail_list).append(name)
            print(f"重载完成：成功 {len(ok_list)}/{len(loaded)}"
                  + (f"，失败: {', '.join(fail_list)}" if fail_list else ""))
        except Exception as e:
            print(f"重载失败: {e}")



    # ---- 注册 ----
    terminal_commands.register("enable", cmd_enable, "启用插件: enable <插件名>", target="host")
    terminal_commands.register("disable", cmd_disable, "禁用插件: disable <插件名>", target="host")
    terminal_commands.register("config", cmd_config, "查看/修改配置: config [key] [value]")
    terminal_commands.register("reload", cmd_reload, "重载插件: reload [插件名]",
                               aliases=["rl"], target="host")

