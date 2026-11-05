# -*- coding: utf-8 -*-
"""
PluginLoader 运行时监控能力（自 framework/loader/ 剥离）

PluginRuntimeMixin：
  _start_memory_monitor  内存监控线程（超限自动卸载）
  heartbeat_register     心跳：mtime 变化时增量重新 register
  self_check_orphans     孤儿任务/命令自检
"""
import logging
import os
import sys
import threading

logger = logging.getLogger('zcbot')


class PluginRuntimeMixin:
    """内存监控 / 心跳刷新 / 孤儿自检"""

    def _start_memory_monitor(self):
        """
        启动内存监控线程（每 3 秒采样一次）
        监控进程总内存和估算每个插件模块的内存占用
        连续两次超过阈值则自动卸载插件
        """
        if self._memory_monitor_running:
            return
        self._memory_monitor_running = True

        cfg = self.framework.config.get('plugin', {})
        max_mb = cfg.get('max_memory_mb', 64)

        def monitor():
            import psutil  # 延迟导入：psutil 导入较慢，仅在启动内存监控线程时加载
            process = psutil.Process(os.getpid())

            while self._memory_monitor_running:
                threading.Event().wait(3)

                try:
                    # 进程级内存监控
                    proc_mem = process.memory_info().rss / 1024 / 1024
                    if proc_mem > max_mb * 1.5:  # 进程总内存超过 1.5 倍阈值
                        logger.warning(
                            f"[内存监控] 进程内存 {proc_mem:.1f}MB 超过警戒线 "
                            f"({max_mb * 1.5:.0f}MB)，可能存在插件泄漏"
                        )

                    # 逐个插件粗略估计内存（通过模块全局变量大小）
                    with self._lock:
                        for name, info in list(self._loaded_plugins.items()):
                            try:
                                module = info['module']
                                # 估算：模块的 __dict__ 里所有对象大小之和
                                module_size = sum(
                                    sys.getsizeof(v) for v in
                                    vars(module).values()
                                    if not v.__class__.__name__.startswith(('module', 'function', 'type'))
                                ) / 1024 / 1024

                                if module_size > max_mb:
                                    count = self._memory_violations.get(name, 0) + 1
                                    self._memory_violations[name] = count
                                    if count >= 2:
                                        logger.error(
                                            f"[内存监控] [{name}] 连续 {count} 次超限 "
                                            f"({module_size:.1f}MB > {max_mb}MB)，自动卸载"
                                        )
                                        # 异步卸载（不在此线程内执行耗时操作）
                                        threading.Thread(
                                            target=self.unload_plugin,
                                            args=(name,),
                                            daemon=True,
                                        ).start()
                                    else:
                                        logger.warning(
                                            f"[内存监控] [{name}] 内存使用 {module_size:.1f}MB "
                                            f"超过限制 {max_mb}MB（第 {count} 次警告）"
                                        )
                                else:
                                    # 恢复正常，清除违规计数
                                    self._memory_violations.pop(name, None)

                            except Exception:
                                pass  # 单个插件估算失败不影响其他

                except Exception:
                    pass  # 监控异常不干扰主流程

        t = threading.Thread(target=monitor, daemon=True, name="memory_monitor")
        t.start()
        logger.info(f"内存监控线程已启动 (采样间隔 3s, 单插件上限 {max_mb}MB)")



    def _reload_plugin(self, plugin_name: str) -> bool:
        """
        对运行中的用户插件执行全量热重载：unload → load → register。

        与 /api/plugins/<name>/reload 及失败自愈（retry_failed_plugins）使用完全相同的
        machinery：unload_plugin 负责干净拆除（事件订阅/钩子/原始处理器/调度任务/路由缓存/
        sys.modules/sys.path/gc），load_plugin 经 _PluginSourceLoader 从源码现场重编译
        （不读 .pyc，且加载前清本插件 __pycache__），故磁盘上改动的 handler 逻辑真正生效。

        仅由 heartbeat_register 在 hot_reload 启用时调用；核心插件不参与。
        """
        logger.info(f"[{plugin_name}] 检测到源码变更，执行热重载...")
        try:
            self.unload_plugin(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 热重载卸载阶段异常（继续尝试重新加载）: {e}")

        if self.load_plugin(plugin_name):
            if self.register_commands(plugin_name):
                # 重载完成后显式校正状态（与 retry_failed_plugins 自愈路径保持一致）
                try:
                    self.db.execute(
                        "UPDATE plugins SET status='running', has_register=1 "
                        "WHERE plugin_name=%s", (plugin_name,)
                    )
                except Exception:
                    pass
                try:
                    self.framework.router._invalidate_cache()
                except Exception:
                    pass
                logger.info(f"[{plugin_name}] 热重载完成（代码已更新）")
                return True
            else:
                logger.error(f"[{plugin_name}] 热重载注册阶段失败，已登记自愈")
                return False
        else:
            logger.error(f"[{plugin_name}] 热重载加载失败，已登记自愈")
            return False

    def heartbeat_register(self):
        """
        心跳检查：仅对用户插件（plugins/ 目录）中文件发生变化的插件应用变更。
        核心插件（core_plugins/）已在启动时注册，不参与心跳。

        变更应用方式由 plugin.hot_reload 决定：
          - true（默认）：全量热重载 unload+reload，磁盘改动真正生效；
          - false：沿用旧行为，仅重新 register(ctx)（模块代码不重跑，适合对重载副作用敏感的插件）。
        """
        hot_reload = self.framework.config.get('plugin', {}).get('hot_reload', True)

        with self._lock:
            plugin_names = list(self._loaded_plugins.keys())

        changed = []
        for name in plugin_names:
            info = self._loaded_plugins.get(name, {})
            plugin_path = info.get('path', '')
            # 只处理用户插件目录下的插件
            if not plugin_path.startswith(self.plugins_dir):
                continue
            snap = self._snapshot_mtime(name)
            if snap == self._plugin_mtimes.get(name):
                continue
            try:
                if hot_reload:
                    ok = self._reload_plugin(name)
                else:
                    ok = self.register_commands(name)
                if ok:
                    self._plugin_mtimes[name] = snap
                    changed.append(name)
                    logger.debug(
                        f"[{name}] 心跳已应用文件变更（"
                        f"{'热重载' if hot_reload else '重新注册'}）"
                    )
                # 失败时已写入 _failed_mtimes，交由 retry_failed_plugins 自愈，
                # 不更新 mtime 快照，避免同快照下反复重试同一损坏文件
            except Exception as e:
                logger.error(f"[{name}] 心跳变更应用异常: {e}")

        # 失败插件自愈：曾加载/注册失败（含文件损坏、register 异常）的插件，
        # 若文件快照发生变化（用户已修复），自动重新加载并注册，无需重启框架
        self.retry_failed_plugins()

        # 心跳后使路由缓存失效（命令可能有变化）
        if changed:
            try:
                self.framework.router._invalidate_cache()
            except Exception:
                pass
        # 无论是否发生变化，心跳后也刷新一次路由表（兜底：DB 与内存对齐）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass



    def self_check_orphans(self):
        """
        周期性自检（默认随心跳每分钟执行一次）：清理不应存在的孤儿任务/命令。

        判定规则：
        - 数据库中 tasks / commands 表存在「插件代码目录已不存在（未被 discover）」的
          条目时，视为孤儿，直接从库表删除（任务同时移除调度器注册）。
        - 调度器中属于「当前未加载插件」的任务（无法执行），从调度器移除。

        这样即使插件被手动删除、卸载异常或禁用流程未完全清理，也能自动校正，
        避免「幽灵任务」继续触发。已禁用/已卸载插件在 unload 时已清过库表，
        此处作为兜底，不会误删仍存在的插件数据。
        """
        try:
            with self._lock:
                loaded = set(self._loaded_plugins.keys())
            # 「存在」的判定必须同时覆盖三个来源，缺一个就会误删：
            #   1) plugins/        用户插件（discover() 只扫这里）
            #   2) core_plugins/   官方插件（echo/help/webui… 全在这）
            #   3) _loaded_plugins 正在内存里跑着的（兜底，绝不误删活插件）
            # 此前只用了 discover()，于是每次心跳（60s）都把 echo/help 等
            # 官方插件注册的 commands 行当孤儿删掉，命令随之失效。
            discovered = set(self.discover())
            try:
                core_dir = self.framework._get_core_plugins_dir()
                if os.path.isdir(core_dir):
                    for n in os.listdir(core_dir):
                        if n.startswith('_'):
                            continue
                        if os.path.isfile(os.path.join(core_dir, n, 'main.py')):
                            discovered.add(n)
            except Exception as e:
                logger.debug(f"[自检] 扫描 core_plugins 失败: {e}")
            discovered |= set(self._loaded_plugins.keys())
            if not discovered and not loaded:
                return

            # 1) 数据库孤儿清理：插件目录已不存在的 tasks / commands
            #    表名来自固定白名单，非外部输入，可安全格式化
            for tbl in ('tasks', 'commands'):
                try:
                    rows = self.db.query(
                        f"SELECT DISTINCT plugin_name FROM {tbl}"
                    )
                except Exception as e:
                    logger.warning(f"[自检] 查询 {tbl} 失败: {e}")
                    continue
                for r in rows:
                    pn = r.get('plugin_name')
                    if not pn or pn in discovered:
                        continue
                    try:
                        self.db.execute(
                            f"DELETE FROM {tbl} WHERE plugin_name = %s", (pn,)
                        )
                        if tbl == 'tasks' and self.framework.scheduler is not None:
                            self.framework.scheduler.remove_plugin_tasks(pn)
                        logger.info(f"[自检] 清理孤儿 {tbl}: 插件 [{pn}] 已不存在")
                    except Exception as e:
                        logger.warning(f"[自检] 删除 {tbl} [{pn}] 失败: {e}")

            # 2) 调度器孤儿清理：任务所属插件当前未加载，无法执行则移除
            try:
                scheduler = self.framework.scheduler
                if scheduler is None:
                    pass
                else:
                    stale = [
                        tid for tid, info in scheduler._plugin_tasks.items()
                        if info.get('plugin_name') not in loaded
                    ]
                    for tid in stale:
                        try:
                            scheduler._scheduler.remove_job(tid)
                            scheduler._plugin_tasks.pop(tid, None)
                            logger.info(f"[自检] 移除调度器孤儿任务: {tid}")
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"[自检] 调度器孤儿清理失败: {e}")

            # 3) 路由表兜底刷新（孤儿命令删除后保证内存与 DB 对齐）
            try:
                self.framework.router._invalidate_cache()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[自检] 异常: {e}")

    def retry_failed_plugins(self):
        """
        失败插件自愈：周期（随心跳调用）检查曾加载/注册失败的插件，
        若其源码文件快照已变化（用户修复了 main.py / 依赖补齐 / 代码修正），
        自动重新加载并注册，使插件无需重启框架即可恢复。

        判定依据：_failed_mtimes 记录失败时刻的 mtime 快照。
        - 快照为 -1.0（目录不存在）或与当前一致 → 未修复，跳过；
        - 当前快照更大（文件被改动）→ 尝试 load_plugin + register_commands，
          成功则清除失败记录；再次失败则更新快照，等待下一次修复。
        """
        try:
            with self._lock:
                failed = list(self._failed_mtimes.items())
            if not failed:
                return
            for name, failed_snap in failed:
                try:
                    # 插件已被禁用（is_active=0）：放弃自愈，不再自动重载
                    if not self.is_plugin_active_in_db(name):
                        logger.info(f"[{name}] 插件已禁用，取消自动自愈重试")
                        with self._lock:
                            self._failed_mtimes.pop(name, None)
                        continue
                    cur = self._snapshot_mtime(name)
                    # 目录都不存在：无法修复，放弃记录（避免永久占用表项）
                    if cur < 0:
                        with self._lock:
                            self._failed_mtimes.pop(name, None)
                        continue
                    # 文件未变化：用户还没修复，耐心等待
                    if cur == failed_snap:
                        continue
                    logger.info(f"[{name}] 检测到文件变化（失败快照 {failed_snap} → {cur}），尝试自动恢复...")
                    if self.load_plugin(name):
                        if self.register_commands(name):
                            with self._lock:
                                self._failed_mtimes.pop(name, None)
                            try:
                                self.db.execute(
                                    "UPDATE plugins SET status='running', has_register=1 "
                                    "WHERE plugin_name=%s", (name,)
                                )
                            except Exception:
                                pass
                            logger.info(f"[{name}] 自修复成功：插件已自动重新加载并注册")
                            try:
                                self.framework.router._invalidate_cache()
                            except Exception:
                                pass
                        else:
                            # load 成功但 register 失败：登记新快照，下次文件再变化时重试
                            # （load_plugin 成功会覆盖 _loaded_plugins；register 失败会更新 _failed_mtimes）
                            with self._lock:
                                self._failed_mtimes[name] = self._snapshot_mtime(name)
                    else:
                        # load 失败：更新失败快照为当前值，避免每次心跳重复无效加载
                        with self._lock:
                            self._failed_mtimes[name] = cur
                except Exception as e:
                    logger.error(f"[{name}] 失败插件自愈检查异常: {e}")
        except Exception as e:
            logger.error(f"[自愈] 失败插件自愈流程异常: {e}")
