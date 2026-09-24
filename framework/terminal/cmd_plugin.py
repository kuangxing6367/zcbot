# -*- coding: utf-8 -*-
"""
终端命令：插件启停 / 配置 / 重载（enable / disable / config / reload）
"""
from .helper import installed_core_plugins

from .command import terminal_commands


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

    def cmd_reload(args):
        """重载插件: reload [插件名]"""
        try:
            if args.strip():
                plugin_name = args.strip()
                success = fw.plugin_loader.reload_plugin(plugin_name)
                if success:
                    print(f"插件 [{plugin_name}] 重载成功")
                else:
                    print(f"插件 [{plugin_name}] 重载失败")
            else:
                loaded = fw.plugin_loader.reload_all()
                print(f"已重载 {len(loaded)} 个插件")
        except Exception as e:
            print(f"重载失败: {e}")



    # ---- 注册 ----
    terminal_commands.register("enable", cmd_enable, "启用插件: enable <插件名>", target="host")
    terminal_commands.register("disable", cmd_disable, "禁用插件: disable <插件名>", target="host")
    terminal_commands.register("config", cmd_config, "查看/修改配置: config [key] [value]")
    terminal_commands.register("reload", cmd_reload, "重载插件: reload [插件名]", target="host")

