# -*- coding: utf-8 -*-
"""Framework 核心插件加载 / 依赖自愈 / 心跳·内存看门狗 / 内置任务（自 core.py 剥离的 mixin）"""
import ast
import asyncio
import gc
import importlib.util
import logging
import os
import sys
import time

from framework.memory import trim_memory, log_malloc_hint

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

            # 检查配置开关。core_plugins.yaml（经 _autoload_core_plugins 合并）
            # 是唯一权威：未在清单中列出的插件视为未配置——一律禁用并提示
            # 运行扫描工具，杜绝"未列出自动启用"的误加载（历史缺省 True 之坑）。
            enabled = core_cfg.get(name)
            if enabled is None:
                logger.warning(
                    f"官方插件 [{name}] 未在 core_plugins.yaml 中配置，不加载；"
                    "如需启用请运行: python tools/scan_core_plugins.py --enable "
                    f"{name}")
                continue
            # 兼容两种配置形态：{name: bool}（_autoload_core_plugins 合并产物）
            # 与 {name: {enabled: ...}}（手写配置块 / 非标准注入路径），
            # 以及 'false'/'0'/None 等非布尔值——缺失一律按禁用处理，
            # 避免 enabled:false 的插件被 eager import 白白占用内存（约 10MB/个）。
            if isinstance(enabled, dict):
                enabled = enabled.get('enabled', True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in ('1', 'true', 'yes', 'on', 'y')
            if not enabled:
                logger.info(f"官方插件 [{name}] 已禁用 (core_plugins.{name}: {enabled!r})")
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"core_plugin_{name}", main_file,
                    submodule_search_locations=[plugin_dir])
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"core_plugin_{name}"] = module
                spec.loader.exec_module(module)

                # 调用 register(ctx)
                from framework.ctx import PluginContext
                ctx = PluginContext(f"core:{name}", self)
                module.ctx = ctx

                if hasattr(module, 'register'):
                    module.register(ctx)
                    # 应用官方插件模块级装饰器登记的注册项（@command/@on/@hook/...）
                    try:
                        from framework.plugin import flush as _flush_plugin
                        _flush_plugin(module.__name__, ctx)
                    except Exception as e:
                        logger.warning(f"官方插件 [{name}] 装饰器 flush 失败: {e}")
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
        """内存看门狗：自动内存回收策略（无需任何手动配置）。

        三层防御，全部后台自动运行：
        1. 超限硬清：RSS 超过 memory_limit_mb 时清框架级缓存 + GC + trim；
        2. 峰值回落主动 trim：大峰值（如解闸 50w）排空后趁释放窗口把空闲块还 OS；
        3. 自动回收模式：跟踪运行基线，一旦检测到「内存地板」（RSS 持续高于基线且
           不回落），进入持续回收模式，周期性 GC + trim 直到内存回到基线附近，
           全程无需人工干预。
        """
        import psutil  # 延迟导入：psutil 导入较慢，仅在启用看门狗时加载
        process = psutil.Process()
        while self._running:
            try:
                await asyncio.sleep(self._memory_check_interval)
                if not self._running:
                    break
                rss_mb = process.memory_info().rss / 1024 / 1024
                # 懒初始化：基线 / 峰值 / 节流时间戳 / 回收状态机 / 分配器提示
                if not hasattr(self, '_baseline_rss'):
                    self._baseline_rss = rss_mb
                    self._baseline_until = time.monotonic() + 120.0  # 基线采样窗口 120s
                    self._peak_rss = rss_mb
                    self._last_trim = 0.0
                    self._reclaim_mode = False
                    self._reclaim_since = 0.0
                    self._reclaim_cooldown = 0.0
                    self._last_reclaim = 0.0
                    log_malloc_hint(logger)
                now = time.monotonic()
                self._peak_rss = max(self._peak_rss, rss_mb)
                # 基线采样：启动后前 120s 取 RSS 滚动最低值作为「健康基线」
                if now < self._baseline_until:
                    self._baseline_rss = min(self._baseline_rss, rss_mb)
                # 内存地板阈值：高于基线 1.5 倍 或 高出 100MB 即视为异常残留
                floor = max(self._baseline_rss * 1.5, self._baseline_rss + 100.0)

                # —— 层 3：自动回收模式（地板检测 + 持续回收）——
                if not self._reclaim_mode:
                    if rss_mb > floor:
                        if self._reclaim_since == 0.0:
                            self._reclaim_since = now
                        elif now - self._reclaim_since > 30.0:  # 持续 30s 高于地板才判定
                            self._reclaim_mode = True
                            logger.info(
                                f"[内存看门狗] 检测到内存地板（基线 {self._baseline_rss:.0f}MB，"
                                f"当前 {rss_mb:.0f}MB），进入自动回收模式"
                            )
                    else:
                        self._reclaim_since = 0.0
                else:
                    # 回收模式：每 60s 强制 GC + trim，持续把空闲内存还 OS
                    if now - self._last_reclaim > 60.0:
                        collected = gc.collect()
                        trimmed = trim_memory()
                        after_rss = process.memory_info().rss / 1024 / 1024
                        self._last_reclaim = now
                        logger.info(
                            f"[内存看门狗] 自动回收中：GC {collected} 对象，trim={trimmed}，"
                            f"RSS {rss_mb:.1f}→{after_rss:.1f}MB"
                        )
                    # 退出条件：RSS 回落到基线 1.2 倍内持续 60s
                    if rss_mb < self._baseline_rss * 1.2:
                        if self._reclaim_cooldown == 0.0:
                            self._reclaim_cooldown = now
                        elif now - self._reclaim_cooldown > 60.0:
                            self._reclaim_mode = False
                            self._reclaim_cooldown = 0.0
                            logger.info("[内存看门狗] 内存已回落到基线，退出自动回收模式")
                    else:
                        self._reclaim_cooldown = 0.0

                # —— 层 1：超限硬清（独立于回收模式的兜底）——
                if rss_mb > self._memory_limit_mb:
                    logger.warning(
                        f"[内存看门狗] RSS {rss_mb:.1f}MB 超过限制 {self._memory_limit_mb}MB，触发清理"
                    )
                    from framework.messaging.event import _user_role_cache, _group_role_cache
                    cache_before = len(_user_role_cache) + len(_group_role_cache)
                    _user_role_cache.clear()
                    _group_role_cache.clear()
                    if hasattr(self, 'stats_writer'):
                        self.stats_writer._cmd_hits.clear()
                        self.stats_writer._kw_hits.clear()
                    collected = gc.collect()
                    trimmed = trim_memory()
                    after_rss = process.memory_info().rss / 1024 / 1024
                    self._last_trim = now
                    logger.info(
                        f"[内存看门狗] 清理完成：缓存 {cache_before} 条，GC 回收 {collected} 对象，"
                        f"trim={trimmed}，RSS {rss_mb:.1f}→{after_rss:.1f}MB（节省 {rss_mb - after_rss:.1f}MB）"
                    )
                else:
                    # —— 层 2：峰值回落后主动归还（趁释放窗口）——
                    fallen = self._peak_rss - rss_mb
                    if (fallen > max(50.0, self._peak_rss * 0.25)
                            and rss_mb > self._memory_limit_mb * 0.6
                            and now - self._last_trim > 60.0):
                        trimmed = trim_memory()
                        self._last_trim = now
                        after_rss = process.memory_info().rss / 1024 / 1024
                        logger.debug(
                            f"[内存看门狗] 峰值回落主动 trim：{trimmed}，"
                            f"RSS {rss_mb:.1f}→{after_rss:.1f}MB"
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
