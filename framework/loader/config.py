# -*- coding: utf-8 -*-
"""
PluginLoader 配置 schema 能力（自 framework/loader/ 剥离）

PluginConfigMixin：
  read_config_schema        读 _conf_schema.json + 源码/DB 兜底生成
  init_plugin_configs       按 schema 初始化 plugin_configs 表
  get_plugin_config_files   插件数据目录中的配置文件列表
"""
import json
import logging
import os
import re

logger = logging.getLogger('zcbot')


class PluginConfigMixin:
    """插件配置 schema 与配置文件"""

    def read_config_schema(self, plugin_name: str) -> dict:
        """
        读取插件的 _conf_schema.json 配置 schema（从 plugins_dat 读取）
        返回 {key: {type, description, default, hint, options, ...}} 格式
        如果不存在返回空 dict

        兜底：插件没有 _conf_schema.json 时，扫描 plugin_configs 表中
        该插件已有的配置项，自动生成基础 schema（type 按值类型推断），
        让 Web 配置页仍能显示并编辑插件运行中写入的配置项。
        """
        schema_path = os.path.join(self.plugins_dat_dir, plugin_name, '_conf_schema.json')
        if os.path.isfile(schema_path):
            try:
                with open(schema_path, 'r', encoding='utf-8') as f:
                    schema = json.loads(f.read())
                if schema:
                    return schema
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取 _conf_schema.json 失败: {e}")

        # 兜底一：从插件源码静态提取 get_config("key", default) 调用
        try:
            code_dir = os.path.join(self.plugins_dir, plugin_name)
            schema_from_src = {}
            for root, _, fnames in os.walk(code_dir):
                for fn in fnames:
                    if not fn.endswith('.py'):
                        continue
                    src_path = os.path.join(root, fn)
                    try:
                        with open(src_path, 'r', encoding='utf-8', errors='ignore') as f:
                            src = f.read()
                    except Exception:
                        continue
                    # 匹配 get_config("key", default) 及带别名的 _get_config("key", default)
                    for m in re.finditer(
                        r'(?:ctx\.|self\.|plugin\.)?get_config\(\s*(["\'])([^"\']+)\1\s*(?:,\s*(.*?))?\s*\)',
                        src
                    ):
                        key = m.group(2)
                        if not key or key in schema_from_src:
                            continue
                        default = m.group(3)
                        spec = {'type': 'string', 'description': key}
                        if default is not None:
                            d = default.strip()
                            if d == 'True' or d == 'False':
                                spec['type'] = 'bool'
                                spec['default'] = d == 'True'
                            elif d == 'None' or d == '':
                                spec['default'] = None
                            elif d.lstrip('-').isdigit():
                                spec['type'] = 'int'
                                spec['default'] = int(d)
                            elif d.replace('.', '', 1).lstrip('-').isdigit():
                                spec['type'] = 'float'
                                spec['default'] = float(d)
                            elif d.startswith('"') and d.endswith('"'):
                                spec['default'] = d[1:-1]
                            elif d.startswith("'") and d.endswith("'"):
                                spec['default'] = d[1:-1]
                        spec['description'] = f'自动发现（源码中 {fn} 调用 get_config）'
                        schema_from_src[key] = spec
            if schema_from_src:
                return schema_from_src
        except Exception as e:
            logger.warning(f"[{plugin_name}] 源码分析生成 schema 失败: {e}")

        # 兜底二：从 plugin_configs 表已有配置项生成基础 schema
        try:
            rows = self.db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s ORDER BY id",
                (plugin_name,)
            )
            if not rows:
                return {}
            schema = {}
            for r in rows:
                key = r['config_key']
                raw = r['config_value']
                val = None
                try:
                    val = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    val = raw
                if isinstance(val, bool):
                    val_type = 'bool'
                elif isinstance(val, int):
                    val_type = 'int'
                elif isinstance(val, float):
                    val_type = 'float'
                elif isinstance(val, (list, dict)):
                    val_type = 'text'
                else:
                    val_type = 'string'
                schema[key] = {
                    'type': val_type,
                    'description': f'当前值: {val}（由插件运行时配置自动生成）',
                    'default': val if not isinstance(val, (list, dict)) else None,
                }
            return schema
        except Exception as e:
            logger.warning(f"[{plugin_name}] 扫描 plugin_configs 生成 schema 失败: {e}")
            return {}



    def init_plugin_configs(self, plugin_name: str):
        """
        根据 _conf_schema.json 初始化插件配置到 plugin_configs 表
        - 新增字段：插入默认值
        - 不再存在于 schema 的旧字段：删除
        - 已有字段：保持用户已设的值不动
        """
        schema = self.read_config_schema(plugin_name)
        if not schema:
            return

        schema_keys = set(schema.keys())
        try:
            rows = self.db.query(
                "SELECT config_key FROM plugin_configs WHERE plugin_name = %s",
                (plugin_name,)
            )
            existing_keys = {r['config_key'] for r in (rows or [])}
        except Exception as e:
            logger.error(f"[{plugin_name}] 读取现有配置失败: {e}")
            return

        for key in existing_keys - schema_keys:
            try:
                self.db.execute(
                    "DELETE FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                    (plugin_name, key)
                )
                logger.debug(f"[{plugin_name}] 已删除废弃配置: {key}")
            except Exception as e:
                logger.warning(f"[{plugin_name}] 删除废弃配置 {key} 失败: {e}")

        for key, spec in schema.items():
            if not isinstance(spec, dict) or key in existing_keys:
                continue
            default = spec.get('default')
            cv = json.dumps(default, ensure_ascii=False) if default is not None else 'null'
            try:
                self.db.execute(
                    "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
                    "VALUES (%s, %s, %s)",
                    (plugin_name, key, cv)
                )
            except Exception as e:
                logger.error(f"[{plugin_name}] 初始化配置 {key} 失败: {e}")



    def get_plugin_config_files(self, plugin_name: str) -> list:
        """获取插件数据目录（plugins_dat）下所有配置/文档文件列表"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            return []
        result = []
        for name in os.listdir(dat_dir):
            fpath = os.path.join(dat_dir, name)
            if os.path.isfile(fpath) and name.endswith(_CONFIG_FILE_EXTS):
                size = os.path.getsize(fpath)
                result.append({'name': name, 'size': size})
        return result

