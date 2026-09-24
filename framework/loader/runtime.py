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



    def heartbeat_register(self):
        """
        心跳检查：仅对用户插件（plugins/ 目录）中文件发生变化的插件重新 register(ctx)
        核心插件（core_plugins/）已在启动时注册，不参与心跳
        """
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
                self.register_commands(name)
                self._plugin_mtimes[name] = snap
                changed.append(name)
                logger.debug(f"[{name}] 心跳增量注册完成")
            except Exception as e:
                logger.error(f"[{name}] 心跳注册异常: {e}")

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
            discovered = set(self.discover())
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
                        if tbl == 'tasks':
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

