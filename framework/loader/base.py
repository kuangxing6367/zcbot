"""
插件加载器
负责：发现插件目录、动态导入 main.py、调用 register(ctx)、1分钟心跳刷新
同时支持读取 plugin.yaml 配置文件（GitHub 更新源、配置项、文档）
支持读取 _conf_schema.json 配置 schema

依赖管理（pip 镜像安装 / 版本说明符解析 / 插件依赖检查与隔离 venv）已剥离到
framework/deps/，经 PluginDepsMixin 混入本类；公开函数继续从本模块 re-export，
旧导入路径（from framework.loader import pip_install_*）保持兼容。
"""
import logging
import os
import threading
from typing import Dict

import yaml

from framework.loader.config import PluginConfigMixin, _CONFIG_FILE_EXTS  # noqa: F401  _CONFIG_FILE_EXTS 定义在 config，此处 re-export
from framework.loader.lifecycle import PluginLifecycleMixin, _PluginSourceLoader  # noqa: F401
from framework.loader.runtime import PluginRuntimeMixin
from framework.loader.ui import (
    PluginUiExtensionsMixin, PluginWebuiMixin, PluginGroupSettingsMixin,
)
from framework.deps import (  # noqa: F401  兼容旧导入路径
    PluginDepsMixin,
    _check_version_compatible,
    _parse_requirements_file,
    _parse_ver,
    _parse_version_spec,
    pip_install_all,
    pip_install_requirements,
    pip_install_with_mirror,
)

logger = logging.getLogger('zcbot')

# 仪表盘卡片执行线程池（共享，避免每次请求创建线程；慢卡片隔离在此池）
_cards_executor = None

# ── pip 安装工具 / 版本说明符解析 已剥离到 framework/deps/ ──────
# （从本模块 re-export，见文件头部 import）

# ── 配置文件后缀定义（这些文件存放在 plugins_dat，而非 plugins） ──
# 后缀匹配（.txt 不自动归类，因为可能是数据文件/requirements.txt）
# _CONFIG_FILE_EXTS 定义在 framework.loader.config（避免循环），见上方 import re-export
# 明确的配置文件名（无论后缀，都归类为配置文件）
_CONFIG_FILE_NAMES = {
    'plugin.yaml', '_conf_schema.json', 'metadata.yaml',
    'README.md', 'README_zh.md', 'README_ru.md',
    'CHANGELOG.md', 'LICENSE',
}
# 排除名单：这些文件虽然后缀匹配，但属于代码/构建文件，跟代码走
_CODE_FILE_NAMES = {
    'requirements.txt', 'package.json', 'package-lock.json',
    'pyproject.toml', 'setup.cfg', 'tox.ini',
}

class PluginLoader(PluginDepsMixin, PluginUiExtensionsMixin, PluginWebuiMixin, PluginGroupSettingsMixin, PluginConfigMixin, PluginRuntimeMixin, PluginLifecycleMixin):
    """插件加载器，管理插件生命周期"""

    def __init__(self, plugins_dir: str, framework, plugins_dat_dir: str = None):
        self.plugins_dir = plugins_dir
        self.plugins_dat_dir = plugins_dat_dir or os.path.join(
            os.path.dirname(plugins_dir.rstrip(os.sep)), 'data', 'plugins_dat'
        )
        self.framework = framework
        self.db = framework.db
        self._loaded_plugins: Dict[str, dict] = {}  # plugin_name -> {module, register_func, ...}
        self._lock = threading.Lock()
        self._override_webui: str = None  # 接管整个前端的插件名（None 表示用框架默认前端）
        self._missing_deps: Dict[str, list] = {}   # plugin_name -> [缺失的包列表]
        self._conflict_deps: Dict[str, list] = {}  # plugin_name -> [{name, required, installed}, ...]
        self._isolated_plugins: set = set()         # plugin_name -> 已启用隔离环境的插件
        self._memory_monitor_running = False
        self._memory_violations: Dict[str, int] = {}  # plugin_name -> 连续超限次数

        # ── 群级插件开关缓存 {group_id: {plugin_name: enabled}} ──
        self._group_plugin_cache = {}
        self._group_plugin_cache_time = 0
        self._group_cache_ttl = 30  # 缓存 30 秒

        # ── 插件文件 mtime 快照（心跳增量注册用）──
        self._plugin_mtimes: Dict[str, float] = {}

    # ── 依赖检查 / 自动安装 / 隔离 venv ──────────────────────
    # 已剥离到 framework/deps/ 的 PluginDepsMixin（混入本类），
    # 方法名与签名不变：check_dependencies / install_missing_deps /
    # create_isolated_env / scan_venv_usage 等。

    def discover(self) -> list:
        """扫描插件目录，返回所有插件目录名列表"""
        plugins = []
        if not os.path.isdir(self.plugins_dir):
            os.makedirs(self.plugins_dir, exist_ok=True)
            return plugins

        for name in os.listdir(self.plugins_dir):
            main_path = os.path.join(self.plugins_dir, name, 'main.py')
            if os.path.isfile(main_path):
                plugins.append(name)
        return plugins

    def _plugin_dat_dir(self, plugin_name: str) -> str:
        """获取插件的数据/配置目录路径"""
        return os.path.join(self.plugins_dat_dir, plugin_name)

    def _plugin_code_dir(self, plugin_name: str) -> str:
        """获取插件的代码目录路径"""
        return os.path.join(self.plugins_dir, plugin_name)

    def ensure_plugins_dat_dir(self, plugin_name: str):
        """确保插件的 plugins_dat 子目录存在"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir
    @staticmethod
    def _is_config_file(filename: str) -> bool:
        """判断文件是否属于配置/数据文件（应存放在 plugins_dat）"""
        lower = filename.lower()
        # 排除名单优先（代码/构建文件跟代码走）
        if lower in _CODE_FILE_NAMES:
            return False
        # 明确的配置文件名
        if lower in _CONFIG_FILE_NAMES:
            return True
        # 后缀匹配
        return lower.endswith(_CONFIG_FILE_EXTS)

    def migrate_legacy_configs(self):
        """
        迁移旧版插件：将 plugins/ 下所有插件的配置文件迁移到 plugins_dat/
        在框架启动时调用一次，兼容升级
        """
        if not os.path.isdir(self.plugins_dir):
            return
        migrated = 0
        for name in os.listdir(self.plugins_dir):
            plugin_dir = os.path.join(self.plugins_dir, name)
            if not os.path.isdir(plugin_dir):
                continue
            # 检查是否有配置文件需要迁移
            has_config = any(
                os.path.isfile(os.path.join(plugin_dir, f)) and self._is_config_file(f)
                for f in os.listdir(plugin_dir)
            )
            if has_config:
                self.split_installed_files(name)
                migrated += 1
        if migrated > 0:
            logger.info(f"已将 {migrated} 个插件的配置文件迁移到 plugins_dat/")

    def read_plugin_yaml(self, plugin_name: str) -> dict:
        """
        读取插件的 plugin.yaml 配置文件
        读取顺序（优先级从高到低）：
          1. plugins_dat/<name>/plugin.yaml    — 用户数据目录（配置会被迁移到此）
          2. plugins/<name>/plugin.yaml        — 代码目录（首次加载或未迁移时的 fallback）
        返回 dict，如果不存在返回空 dict
        """
        # 1. 优先读取 plugins_dat（用户可编辑版本）
        yaml_path = os.path.join(self.plugins_dat_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取 plugins_dat/plugin.yaml 失败: {e}，回退到代码目录")

        # 2. fallback: 读取代码目录下的 plugin.yaml（首次加载未迁移时）
        yaml_path = os.path.join(self.plugins_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取代码目录 plugin.yaml 失败: {e}")
                return {}

        return {}
    def read_plugin_file(self, plugin_name: str, filename: str) -> str:
        """读取插件数据目录（plugins_dat）下的指定文件内容"""
        # 安全检查：防止路径穿越
        if '..' in filename or '/' in filename or '\\' in filename:
            raise ValueError('非法文件名')
        fpath = os.path.join(self.plugins_dat_dir, plugin_name, filename)
        if not os.path.isfile(fpath):
            raise FileNotFoundError(f'文件不存在: {filename}')
        with open(fpath, 'r', encoding='utf-8') as f:
            return f.read()

    def _upsert_plugin_db(self, plugin_name: str, meta: dict):
        """写入/更新插件信息到 plugins 表"""
        try:
            existing = self.db.query_one(
                "SELECT id FROM plugins WHERE plugin_name = %s", (plugin_name,)
            )
            if existing:
                self.db.execute(
                    "UPDATE plugins SET version=%s, author=%s, description=%s, priority=%s, "
                    "has_register=1, status='running', loaded_at=NOW() WHERE plugin_name=%s",
                    (meta['version'], meta['author'], meta['desc'], meta['priority'], plugin_name)
                )
            else:
                self.db.execute(
                    "INSERT INTO plugins (plugin_name, version, author, description, priority, "
                    "has_register, status, loaded_at) VALUES (%s,%s,%s,%s,%s,1,'running',NOW())",
                    (plugin_name, meta['version'], meta['author'], meta['desc'], meta['priority'])
                )
        except Exception as e:
            logger.error(f"写入插件数据库失败 [{plugin_name}]: {e}")

    def register_commands(self, plugin_name: str) -> bool:
        """
        调用插件的 register(ctx)，收集其注册的命令
        由心跳或加载时调用
        """
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if not info:
                return False

        from framework.ctx import PluginContext
        ctx = PluginContext(plugin_name, self.framework)

        # 将 ctx 注入到插件模块的全局变量中
        # 这样插件的处理函数可以直接使用 ctx.api() 等
        module = info['module']
        module.ctx = ctx

        try:
            info['register_func'](ctx)
        except Exception as e:
            logger.error(f"[{plugin_name}] register(ctx) 执行异常: {e}", exc_info=True)
            return False

        # 获取注册的命令和任务
        commands = ctx._get_commands()
        tasks = ctx._get_tasks()
        dashboard_cards = ctx._get_dashboard_cards()

        # 注册原始消息处理器（收到原始消息事件，可选择性接管）
        raw_handlers = ctx._get_raw_message_handlers()
        if raw_handlers:
            _priority = info.get('priority', 50)
            for _h in raw_handlers:
                self.framework.register_raw_message_handler(plugin_name, _h, _priority)

        # 收集 WebUI 群组/用户管理页插件扩展
        group_exts = ctx._get_group_extensions()
        if group_exts:
            with self._lock:
                info['group_extensions'] = group_exts
        user_exts = ctx._get_user_extensions()
        if user_exts:
            with self._lock:
                info['user_extensions'] = user_exts

        # 写入 commands 表
        if commands:
            self._sync_commands(plugin_name, commands)

        # 注册定时任务
        if tasks:
            self._sync_tasks(plugin_name, tasks)

        # 存储仪表盘卡片
        if dashboard_cards:
            self._sync_dashboard_cards(plugin_name, dashboard_cards)

        logger.info(f"[{plugin_name}] 注册完成: {len(commands)} 命令, {len(tasks)} 定时任务")

        # 生命周期钩子：插件首次加载完成后触发一次（重载会重新触发）
        try:
            if not info.get('hook_loaded'):
                on_loaded = getattr(module, 'on_loaded', None)
                if callable(on_loaded):
                    on_loaded(ctx)
                info['hook_loaded'] = True
        except Exception as e:
            logger.error(f"[{plugin_name}] on_loaded 钩子异常: {e}")

        # 命令已写入 DB，让路由表立即重建（热路径内存快照要求一致）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass
        return True

    def _sync_commands(self, plugin_name: str, commands: list):
        """
        同步命令到数据库
        心跳策略：INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
        保留用户在 Web 端修改的 alias/description/is_active 覆盖（通过 handler_name 匹配回填）
        注意：is_dynamic 标记仅表示命令由插件以 dynamic=True 注册，不影响同步策略
              真正的动态命令（关键词回复）存储在 dynamic_commands 表，不受此处影响
        """
        try:
            # 查询当前数据库中所有命令的用户覆盖（按 handler_name 索引）
            existing_overrides = {}
            try:
                rows = self.db.query(
                    "SELECT handler, alias, description, is_active FROM commands "
                    "WHERE plugin_name = %s",
                    (plugin_name,)
                )
                for r in rows:
                    existing_overrides[r['handler']] = {
                        'alias': r.get('alias'),
                        'description': r.get('description'),
                        'is_active': r.get('is_active', 1),
                    }
            except Exception:
                pass

            # INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
            if commands:
                # require_perm 列由 db 迁移添加；极老库可能没有，失败则回退到不含该列的写法
                base_cols = ("plugin_name, pattern, alias, description, "
                             "priority, handler, is_dynamic, require_level, is_active")
                base_upd = ("pattern = VALUES(pattern), alias = VALUES(alias), "
                            "description = VALUES(description), priority = VALUES(priority), "
                            "is_dynamic = VALUES(is_dynamic), require_level = VALUES(require_level), "
                            "is_active = VALUES(is_active)")
                params = []
                for c in commands:
                    handler_name = c['handler_name']
                    override = existing_overrides.get(handler_name, {})
                    # 优先使用用户在 Web 端设置的 alias，否则用代码注册的 alias
                    final_alias = override.get('alias') if override.get('alias') is not None else c.get('alias')
                    final_desc = override.get('description') if override.get('description') is not None else c.get('description')
                    final_active = override.get('is_active', 1)
                    params.append((
                        c['plugin_name'], c['pattern'], final_alias, final_desc,
                        c['priority'], handler_name, c.get('is_dynamic', 0),
                        c.get('require_level', ''), final_active
                    ))

                if self._commands_has_require_perm():
                    sql = (
                        f"INSERT INTO commands ({base_cols}, require_perm) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE {base_upd}, "
                        "require_perm = VALUES(require_perm)"
                    )
                    params = [p + ((c.get('require_perm') or ''),)
                              for p, c in zip(params, commands)]
                else:
                    sql = (
                        f"INSERT INTO commands ({base_cols}) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE {base_upd}"
                    )
                self.db.execute_many(sql, params)
        except Exception as e:
            logger.error(f"[{plugin_name}] 同步命令失败: {e}")

    def _commands_has_require_perm(self) -> bool:
        """commands 表是否已有 require_perm 列（兼容未执行迁移的极老库）"""
        cached = getattr(self, '_cmd_has_perm_col', None)
        if cached is not None:
            return cached
        try:
            has = self.db.table_has_column('commands', 'require_perm')
        except Exception:
            has = False
        self._cmd_has_perm_col = has
        return has

    def _sync_tasks(self, plugin_name: str, tasks: list):
        """同步定时任务到数据库和调度器"""
        try:
            # 先移除调度器中的旧任务（避免僵尸残留 + 重复添加报错）
            self.framework.scheduler.remove_plugin_tasks(plugin_name)

            # 删除旧任务
            self.db.execute(
                "DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,)
            )
            # 插入新任务
            for t in tasks:
                task_id = self.db.insert(
                    "INSERT INTO tasks (plugin_name, cron_expression, handler, description) VALUES (%s,%s,%s,%s)",
                    (t['plugin_name'], t['cron_expression'], t['handler_name'], t['description'])
                )
                t['id'] = task_id
                # 注册到调度器
                self.framework.scheduler.add_plugin_task(t)

        except Exception as e:
            logger.error(f"[{plugin_name}] 同步任务失败: {e}")
    def is_plugin_active_in_db(self, plugin_name: str) -> bool:
        """
        检查插件在数据库中是否处于「启用」状态
        无记录视为启用（首次发现、尚未写入 plugins 表的插件默认启用）
        """
        try:
            row = self.db.query_one(
                "SELECT is_active FROM plugins WHERE plugin_name = %s", (plugin_name,)
            )
        except Exception:
            return True
        if row is None:
            return True
        return bool(row.get('is_active', 1))

    def load_all(self) -> list:
        """加载所有已发现插件，返回成功列表

        已在数据库中标记为禁用（is_active=0）的插件会被跳过，
        避免「框架重启后仍然加载已禁用插件」的问题。
        """
        discovered = self.discover()
        success = []
        for name in discovered:
            if not self.is_plugin_active_in_db(name):
                logger.info(f"[{name}] 插件已被禁用（is_active=0），跳过加载")
                continue
            if self.load_plugin(name):
                success.append(name)
        # 启动内存监控
        self._start_memory_monitor()
        return success

    def get_loaded_plugins(self) -> Dict[str, dict]:
        """获取已加载插件列表"""
        with self._lock:
            return {
                name: {
                    'meta': info['meta'],
                    'priority': info['priority'],
                    'yaml': info.get('yaml', {}),
                }
                for name, info in self._loaded_plugins.items()
            }

    def get_plugin_module(self, plugin_name: str):
        """获取插件模块引用"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            return info['module'] if info else None
