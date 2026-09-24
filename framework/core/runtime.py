# -*- coding: utf-8 -*-
"""Framework 核心插件加载 / 依赖自愈 / 心跳·内存看门狗 / 内置任务（自 core.py 剥离的 mixin）"""
import ast
import asyncio
import gc
import importlib.util
import logging
import os
import sys

logger = logging.getLogger('zcbot')

class FrameworkRuntimeMixin:
    """核心插件加载 / 依赖自愈 / 心跳·内存看门狗 / 内置任务"""

    # 以下方法依赖 Framework.__init__ 建立的实例字段

    def _load_core_plugins(self):
        """加载官方插件（core_plugins/ 目录）"""
        core_plugins_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'core_plugins')
        if not os.path.isdir(core_plugins_dir):
            logger.warning(f"core_plugins 目录不存在: {core_plugins_dir}")
            return

        core_cfg = self.config.get('core_plugins', {})

        # 双进程角色过滤（按插件 __plugin_meta__['process'] 自动分派，不再硬编码名单）：
        #  - process='core'：协议/Web 基础设施，只在核心进程与单进程加载，宿主进程排除
        #  - 其余（插件侧能力）：单进程与宿主进程加载，纯核心进程排除
        # 仍兼容 dual_process.core_plugins 显式名单覆盖（配置了就以配置为准）。
        dual = self.config.get('dual_process', {})
        _explicit_core = dual.get('core_plugins')
        if isinstance(_explicit_core, list) and _explicit_core:
            _explicit_core = set(_explicit_core)
        else:
            _explicit_core = None
        _role = getattr(self, '_role', 'standard')

        for name in os.listdir(core_plugins_dir):
            if name.startswith('_'):
                continue
            plugin_dir = os.path.join(core_plugins_dir, name)
            main_file = os.path.join(plugin_dir, 'main.py')
            if not os.path.isfile(main_file):
                continue

            # 双进程角色分派（单进程 standard 不过滤，全部加载）
            if _role in ('core', 'host'):
                _core_side = self._core_plugin_is_core_side(name, main_file, _explicit_core)
                if _role == 'core' and not _core_side:
                    continue
                if _role == 'host' and _core_side:
                    continue

            # 检查配置开关（默认启用）
            enabled = core_cfg.get(name, True)
            if enabled is False:
                logger.info(f"官方插件 [{name}] 已禁用 (core_plugins.{name}: false)")
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"core_plugin_{name}", main_file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"core_plugin_{name}"] = module
                spec.loader.exec_module(module)

                # 调用 register(ctx)
                from framework.ctx import PluginContext
                ctx = PluginContext(f"core:{name}", self)
                module.ctx = ctx

                if hasattr(module, 'register'):
                    module.register(ctx)
                    logger.info(f"官方插件 [{name}] 已加载")

                    # 存入 plugin_loader，使调度器能通过 get_plugin_module 获取模块
                    with self.plugin_loader._lock:
                        meta = getattr(module, '__plugin_meta__', {})
                        self.plugin_loader._loaded_plugins[name] = {
                            'module': module,
                            'path': plugin_dir,
                            'meta': meta,
                            'priority': meta.get('priority', 50),
                            'yaml': {},
                        }
                else:
                    logger.warning(f"官方插件 [{name}] 无 register 函数")
            except Exception as e:
                logger.error(f"官方插件 [{name}] 加载失败: {e}", exc_info=True)
    @staticmethod
    def _read_plugin_process_tag(main_file: str):
        """不执行插件，静态解析 __plugin_meta__ 里的 process 进程归属标记。
        解析失败返回 None（按插件侧能力处理）。"""
        try:
            with open(main_file, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=main_file)
        except Exception:
            return None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '__plugin_meta__':
                        try:
                            meta = ast.literal_eval(node.value)
                        except Exception:
                            return None
                        if isinstance(meta, dict):
                            return meta.get('process')
        return None

    def _core_plugin_is_core_side(self, name: str, main_file: str, explicit_core) -> bool:
        """判断官方插件是否属于核心进程侧（协议/Web 基础设施）"""
        if explicit_core is not None:
            return name in explicit_core
        return self._read_plugin_process_tag(main_file) == 'core'

    def _register_builtin_jobs(self):
        """注册框架内置定时任务（与插件任务互不干扰）"""
        try:
            from framework import perm as perm_mod

            def _cleanup_expired_perms():
                try:
                    perm_mod.cleanup_expired(self.db)
                except Exception as e:
                    logger.warning(f"权限过期清理任务异常: {e}")

            sched = getattr(self.scheduler, '_scheduler', None)
            if sched is None:
                return
            sched.add_job(
                _cleanup_expired_perms,
                'cron', minute=17, id='builtin_perm_cleanup',
                replace_existing=True, misfire_grace_time=600,
            )
            logger.debug("内置定时任务已注册: 权限过期清理（每小时第 17 分钟）")
        except Exception as e:
            logger.warning(f"注册内置定时任务失败: {e}")

    def _warn_insecure_config(self):
        """启动安全提示（协议中立；各接入端自身的令牌提示由适配器注册时给出）"""
        web_cfg = self.config.get('web', {})
        web_host = web_cfg.get('host', '0.0.0.0')
        if web_host in ('0.0.0.0', '::'):
            logger.warning(
                "⚠ 安全提示: Web 面板监听 0.0.0.0，公网部署请确认已设置访问凭据，"
                "并按需将 web.host 改为 127.0.0.1。"
            )

    async def _heartbeat_loop(self):
        """插件注册心跳：周期性检查插件文件变更并重新注册（不阻塞事件循环）"""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                await asyncio.to_thread(self.plugin_loader.heartbeat_register)
                # 每分钟自检：清理不应存在的孤儿任务/命令（插件已删除时自动校正）
                await asyncio.to_thread(self.plugin_loader.self_check_orphans)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"插件注册心跳异常: {e}")

    async def _memory_watchdog_loop(self):
        """内存看门狗：周期检查 RSS，超限时清理框架级缓存并强制 GC"""
        import psutil  # 延迟导入：psutil 导入较慢，仅在启用看门狗时加载
        process = psutil.Process()
        while self._running:
            try:
                await asyncio.sleep(self._memory_check_interval)
                if not self._running:
                    break
                rss_mb = process.memory_info().rss / 1024 / 1024
                if rss_mb > self._memory_limit_mb:
                    logger.warning(
                        f"[内存看门狗] RSS {rss_mb:.1f}MB 超过限制 {self._memory_limit_mb}MB，触发清理"
                    )
                    # 1. 清理框架级角色缓存
                    from framework.messaging.event import _user_role_cache, _group_role_cache
                    cache_before = len(_user_role_cache) + len(_group_role_cache)
                    _user_role_cache.clear()
                    _group_role_cache.clear()
                    # 2. 清理 stats_writer 聚合计数（高频但不关键）
                    if hasattr(self, 'stats_writer'):
                        self.stats_writer._cmd_hits.clear()
                        self.stats_writer._kw_hits.clear()
                    # 3. 强制 GC 回收循环引用
                    collected = gc.collect()
                    after_rss = process.memory_info().rss / 1024 / 1024
                    logger.info(
                        f"[内存看门狗] 清理完成：缓存 {cache_before} 条，GC 回收 {collected} 对象，"
                        f"RSS {rss_mb:.1f}→{after_rss:.1f}MB（节省 {rss_mb - after_rss:.1f}MB）"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[内存看门狗] 异常: {e}")

    def _auto_heal_plugin_deps(self):
        """
        插件依赖自愈：启动时为缺失依赖的插件尝试自动安装
        - 默认开启，可通过 config.yaml → plugin.auto_install_deps_on_startup: false 关闭
        - 只处理 missing 依赖，不处理版本冲突（避免覆盖全局包）
        - 安装失败不影响框架启动，仅记录警告
        """
        cfg = self.config.get('plugin', {})
        auto_install = cfg.get('auto_install_deps_on_startup', True)
        if not auto_install:
            logger.info("插件依赖自愈已关闭 (plugin.auto_install_deps_on_startup: false)")
            return

        with self.plugin_loader._lock:
            missing_snapshot = {
                name: list(deps)
                for name, deps in self.plugin_loader._missing_deps.items()
                if deps
            }

        if not missing_snapshot:
            return

        logger.info(
            f"检测到 {len(missing_snapshot)} 个插件依赖缺失，"
            f"启动自愈流程: {list(missing_snapshot.keys())}"
        )

        for plugin_name, deps in missing_snapshot.items():
            try:
                logger.info(f"[{plugin_name}] 自愈：尝试自动安装缺失依赖: {deps}")
                result = self.plugin_loader.install_missing_deps(plugin_name)
                if result['success']:
                    if result.get('installed'):
                        logger.info(
                            f"[{plugin_name}] 自愈完成，已安装: "
                            f"{', '.join(result['installed'])}"
                        )
                    else:
                        logger.info(f"[{plugin_name}] 自愈完成，依赖已满足")
                else:
                    failed = result.get('failed', [])
                    conflicts = result.get('conflicts', [])
                    if failed:
                        logger.warning(
                            f"[{plugin_name}] 自愈部分失败，未能安装: "
                            f"{', '.join(failed)}。请在 Web UI 手动处理。"
                        )
                    if conflicts:
                        logger.warning(
                            f"[{plugin_name}] 存在版本冲突（不会自动覆盖全局包），"
                            f"请在 Web UI 创建隔离虚拟环境: "
                            f"{', '.join(c['name'] + ' ' + c['required'] + ' (已安装 ' + c['installed'] + ')' for c in conflicts)}"
                        )
            except Exception as e:
                logger.error(f"[{plugin_name}] 依赖自愈异常: {e}")
