# -*- coding: utf-8 -*-
"""PluginLoader 插件加载/卸载生命周期（自 loader.py 剥离的 mixin）

load_plugin / unload_plugin / 合成包子模块预载 / 字节码清理 / venv path / 文件拆分。
依赖 PluginLoader.__init__ 与 deps/config/runtime mixin 能力。
"""
import logging
import importlib.machinery
import importlib.util
import gc
import os
import shutil
import sys
import types

logger = logging.getLogger('zcbot')


class _PluginSourceLoader(importlib.machinery.SourceFileLoader):
    """
    插件模块专用加载器：始终从 .py 源码现场编译，不读取也不写入 __pycache__。

    背景：CPython 默认按“源码整数秒 mtime + 文件大小”校验 .pyc。热重载/自动
    改码时若在同一秒内把文件改成相同字节数，会误判字节码仍有效而执行旧代码。
    插件加载频率很低，直接每次从源码编译最稳妥，从根上保证“磁盘是什么就跑什么”。
    （深层嵌套包由原生 finder 沿合成包 __path__ 懒加载，其 __pycache__ 另由
    _clear_plugin_bytecode_cache 在加载前清理。）
    """

    def get_code(self, fullname):
        source_path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(source_path), source_path)

    def set_data(self, *args, **kwargs):
        # 不生成 .pyc
        return None


class PluginLifecycleMixin:
    """插件加载 / 卸载 / 模块清理"""


    def split_installed_files(self, plugin_name: str):
        """
        将 plugins/<name>/ 下的配置文件迁移到 plugins_dat/<name>/
        在插件上传/更新后调用，确保代码和配置分离
        """
        code_dir = self._plugin_code_dir(plugin_name)
        dat_dir = self.ensure_plugins_dat_dir(plugin_name)

        if not os.path.isdir(code_dir):
            return

        for name in os.listdir(code_dir):
            fpath = os.path.join(code_dir, name)
            if not os.path.isfile(fpath):
                continue
            if self._is_config_file(name):
                dest = os.path.join(dat_dir, name)
                # plugin.yaml 是插件元信息（版本/更新源/依赖声明），随插件更新：
                # 始终用代码里的新版覆盖 plugins_dat 旧版，避免旧元信息遮挡新配置。
                if name.lower() == 'plugin.yaml':
                    if os.path.exists(dest):
                        try:
                            os.remove(dest)
                            logger.debug(f"[{plugin_name}] 覆盖旧 plugin.yaml")
                        except Exception as e:
                            logger.warning(f"[{plugin_name}] 删除旧 plugin.yaml 失败: {e}")
                    shutil.move(fpath, dest)
                    logger.debug(f"[{plugin_name}] plugin.yaml 已更新")
                    continue
                # 其他配置文件（_conf_schema.json / README 等）：随插件更新始终覆盖
                # 保证 WebUI 能显示新版配置字段
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except Exception as e:
                        logger.warning(f"[{plugin_name}] 删除旧 {name} 失败: {e}")
                shutil.move(fpath, dest)
                logger.debug(f"[{plugin_name}] 配置文件已更新: {name}")

    def _add_venv_to_path(self, plugin_name: str) -> bool:
        """
        将插件的 venv site-packages 加入 sys.path，使其依赖在主进程中可见。
        venv 位于 plugins_dat/<插件名>/.venv。
        返回 True 表示 venv 可用，False 表示 venv 不可用。
        """
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return False

        # 计算 site-packages 路径
        if sys.platform == 'win32':
            site_pkg = os.path.join(venv_path, 'Lib', 'site-packages')
        else:
            # 先找 python3.x/site-packages
            import glob as _glob
            python_dirs = _glob.glob(os.path.join(venv_path, 'lib', 'python*'))
            site_pkg = os.path.join(python_dirs[0], 'site-packages') if python_dirs else None

        if not site_pkg or not os.path.isdir(site_pkg):
            return False

        if site_pkg not in sys.path:
            sys.path.insert(0, site_pkg)
        return True

    def _ensure_plugin_package(self, plugin_name: str, plugin_path: str):
        """
        创建插件的「合成包」模块 plugin_<插件名> 并返回。

        框架不把插件目录作为常规包安装，而是用 importlib 按文件路径加载，
        这个模块身兼两职：
        1. main.py 的执行载体 —— 保持 sys.modules['plugin_<插件名>'] 指向插件主模块
           这一既有约定（跨插件可用 sys.modules.get('plugin_xxx') 访问主模块）；
        2. 相对导入的父包 —— 通过设置 __package__ 与 __path__，让导入系统把它识别为包，
           main.py 及子模块中的相对导入（from .xxx import Y / from . import xxx）
           就能沿着 __path__（即插件目录）解析到本插件自己的模块。

        必须在预加载任何子模块之前创建：否则子模块执行相对导入时会找不到父包。
        """
        pkg_name = f"plugin_{plugin_name}"
        main_file = os.path.join(plugin_path, 'main.py')
        module = types.ModuleType(pkg_name)
        module.__name__ = pkg_name
        # 包的 __package__ 指向自身；main.py 里的 from .xxx 以它为父包
        module.__package__ = pkg_name
        # 关键：__path__ 让导入系统把该模块当作包，按插件目录查找子模块
        module.__path__ = [plugin_path]
        module.__file__ = main_file
        sys.modules[pkg_name] = module
        return module

    def _load_plugin_submodule(self, plugin_name: str, mod_name: str, file_path: str):
        """
        加载插件的一个顶层子模块（.py 文件，或包目录的 __init__.py），
        并在 sys.modules 中为「同一个模块对象」注册三个名字：

        1. plugin_<插件名>.<模块名> —— 规范的点分层级名，挂在合成包下，
           插件内相对导入（from .mod import X / from . import mod）解析到它；
        2. plugin_<插件名>_<模块名> —— 旧版下划线唯一名（向后兼容，
           同时保证多个插件存在同名子模块时互不冲突）；
        3. <模块名> —— 短名，兼容 main.py 的绝对导入（import mod / from mod import X）。

        调用前必须已通过 _ensure_plugin_package() 创建父包 plugin_<插件名>。
        """
        pkg_name = f"plugin_{plugin_name}"
        dotted_name = f"{pkg_name}.{mod_name}"
        legacy_name = f"{pkg_name}_{mod_name}"
        try:
            spec = importlib.util.spec_from_file_location(
                dotted_name, file_path,
                loader=_PluginSourceLoader(dotted_name, file_path))
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            # 先登记再执行：模块执行期间触发的导入即可命中本插件自身
            sys.modules[dotted_name] = module
            sys.modules[legacy_name] = module
            spec.loader.exec_module(module)
            # 短名覆盖：main.py 的 'import mod' / 'from mod import X' 在导入时绑定，
            # 后续其他插件覆盖短名不影响本插件已绑定的引用
            sys.modules[mod_name] = module
        except Exception as e:
            # 回滚半初始化登记，避免挡住原生导入机制沿 __path__ 的兜底解析
            sys.modules.pop(dotted_name, None)
            sys.modules.pop(legacy_name, None)
            logger.warning(f"[{plugin_name}] 子模块 {mod_name} 预加载失败（回退到全局查找）: {e}")

    def _preload_plugin_submodules(self, plugin_name: str, plugin_path: str):
        """
        预加载插件目录下的顶层子模块（.py 文件与包目录），实现同名模块短名隔离。
        解决多个插件存在同名模块（如 ban_word.py / db.py / core/）时，
        后加载插件从 sys.modules 命中其他插件模块导致的 ImportError。

        即使此处遗漏或预加载失败，合成包的 __path__ 仍会让 Python 原生导入机制
        按插件目录兜底解析，因此本方法只负责「提前、正确地登记」。
        """
        try:
            entries = os.listdir(plugin_path)
        except OSError:
            return

        # 先加载顶层 .py 模块，再加载包目录（包内可能绝对导入顶层模块）
        py_files = []
        pkg_dirs = []
        for fname in entries:
            fpath = os.path.join(plugin_path, fname)
            if (os.path.isfile(fpath) and fname.endswith('.py')
                    and fname not in ('main.py', '__init__.py')):
                py_files.append((fname[:-3], fpath))
            elif (os.path.isdir(fpath)
                    and os.path.isfile(os.path.join(fpath, '__init__.py'))):
                pkg_dirs.append((fname, os.path.join(fpath, '__init__.py')))

        for mod_name, fpath in py_files:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)
        for mod_name, fpath in pkg_dirs:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)

    def _purge_plugin_modules(self, plugin_name: str, plugin_path: str = None):
        """
        从 sys.modules 移除属于某插件目录的全部模块。
        点分层级名 / 下划线唯一名 / 短名指向同一模块对象（__file__ 相同），
        按 __file__ 前缀扫描即可一次清净；最后兜底移除合成包主模块。
        卸载、加载失败回滚、重试前清理共用此方法。
        """
        plugin_path = plugin_path or os.path.join(self.plugins_dir, plugin_name)
        try:
            abs_plugin = os.path.abspath(plugin_path)
            for mod_name in list(sys.modules):
                mod = sys.modules.get(mod_name)
                mod_file = getattr(mod, '__file__', '') or ''
                if mod_file and os.path.abspath(mod_file).startswith(abs_plugin + os.sep):
                    sys.modules.pop(mod_name, None)
            sys.modules.pop(f"plugin_{plugin_name}", None)
        except Exception as e:
            logger.debug(f"[{plugin_name}] 清理 sys.modules 异常: {e}")

    def _clear_plugin_bytecode_cache(self, plugin_path: str):
        """
        删除插件目录下的 __pycache__，保证（完全）重载时总是从源码重新编译。

        CPython 依据“源码整数秒 mtime + 文件大小”校验 .pyc：若在同一秒内把
        文件改成相同字节数（自动化改码/快速热重载场景），会误判字节码仍有效而
        复用旧代码。插件加载并不频繁，直接清掉本插件的字节码缓存最稳妥；
        只删除本插件目录内的 __pycache__，不影响其他插件。
        """
        try:
            for root, dirs, _files in os.walk(plugin_path):
                if "__pycache__" in dirs:
                    shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            importlib.invalidate_caches()
        except Exception as e:
            logger.debug(f"清理插件字节码缓存异常: {e}")

    def load_plugin(self, plugin_name: str) -> bool:
        """加载单个插件，返回是否成功"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)

        # 将插件目录加入 sys.path
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)

        # 读取插件配置文件
        yaml_data = self.read_plugin_yaml(plugin_name)

        # ====== 依赖检查 + 自动安装（基于全局环境） ======
        # 说明：框架不再为插件自动创建/重建隔离虚拟环境；
        # 依赖自动安装到全局环境，存在版本冲突的依赖自动跳过（不覆盖全局包），
        # 如需隔离请手动点击「创建虚拟环境」。
        dep_result = self.check_dependencies(plugin_name)
        if dep_result.get('missing'):
            pip_install_all(plugin_name, dep_result['missing'])

            # 重新检查依赖
            dep_result = self.check_dependencies(plugin_name)
            if dep_result.get('missing'):
                missing = ', '.join(dep_result['missing'])
                logger.warning(f"[{plugin_name}] 仍有缺失依赖: {missing}，尝试加载...")

        # 记录依赖状态供 Web UI 展示
        self._record_dep_status(
            plugin_name,
            dep_result.get('missing', []),
            dep_result.get('conflicts', [])
        )

        # ====== 动态导入 main.py（带重试）======
        main_path = os.path.join(plugin_path, 'main.py')
        for attempt in range(2):  # 最多重试1次
            try:
                # 第二次尝试前清掉首次残留的半初始化模块，保证重试幂等
                if attempt == 1:
                    self._purge_plugin_modules(plugin_name, plugin_path)

                # 0) 清掉本插件 __pycache__，避免同秒同尺寸改动复用旧字节码
                self._clear_plugin_bytecode_cache(plugin_path)

                # 1) 先建合成包 plugin_<插件名>（含 __package__/__path__）：
                #    它既是 main.py 的执行载体，也是插件相对导入的父包
                module = self._ensure_plugin_package(plugin_name, plugin_path)

                # 2) 预加载顶层子模块（点分层级名 + 下划线唯一名 + 短名，同名模块互不污染）
                self._preload_plugin_submodules(plugin_name, plugin_path)

                # 3) main.py 直接执行进合成包模块（不能再 module_from_spec，
                #    否则刚设置的 __package__/__path__ 会重置为普通顶层模块，相对导入又会失效）
                spec = importlib.util.spec_from_file_location(
                    f"plugin_{plugin_name}",
                    main_path,
                    loader=_PluginSourceLoader(f"plugin_{plugin_name}", main_path)
                )
                if spec is None or spec.loader is None:
                    logger.error(f"[{plugin_name}] 导入失败: spec 为空")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                module.__spec__ = spec
                module.__loader__ = spec.loader
                spec.loader.exec_module(module)

                # 检查 register 函数
                if not hasattr(module, 'register'):
                    logger.error(f"[{plugin_name}] 缺少 register(ctx) 函数")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                register_func = getattr(module, 'register')
                if not callable(register_func):
                    logger.error(f"[{plugin_name}] register 不可调用")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                # 读取元数据
                meta = getattr(module, '__plugin_meta__', {})
                plugin_meta = {
                    'name': meta.get('name', plugin_name),
                    'version': meta.get('version', '0.0.0'),
                    'author': meta.get('author', 'unknown'),
                    'desc': meta.get('desc', ''),
                    'priority': meta.get('priority', 50),
                }

                # 读取 plugin.yaml 覆盖元数据
                if yaml_data:
                    if 'version' in yaml_data:
                        plugin_meta['version'] = yaml_data['version']
                    if 'author' in yaml_data:
                        plugin_meta['author'] = yaml_data['author']
                    if 'description' in yaml_data:
                        plugin_meta['desc'] = yaml_data['description']
                    if 'priority' in yaml_data:
                        plugin_meta['priority'] = yaml_data['priority']

                with self._lock:
                    self._loaded_plugins[plugin_name] = {
                        'module': module,
                        'register_func': register_func,
                        'meta': plugin_meta,
                        'priority': plugin_meta['priority'],
                        'path': plugin_path,
                        'yaml': yaml_data,
                    }

                self._upsert_plugin_db(plugin_name, plugin_meta)
                self.init_plugin_configs(plugin_name)
                # 记录文件快照，避免首个心跳周期重复注册
                self._plugin_mtimes[plugin_name] = self._snapshot_mtime(plugin_name)

                logger.info(f"[{plugin_name}] 加载成功 v{plugin_meta['version']}")
                return True

            except ImportError as e:
                logger.warning(f"[{plugin_name}] 导入失败（第{attempt + 1}次）: {e}")
                if attempt == 0:
                    # 第一次失败：尝试重新安装缺失依赖（全局环境）再试一次
                    logger.info(f"[{plugin_name}] 尝试重新安装缺失依赖...")
                    dep_result2 = self.check_dependencies(plugin_name)
                    if dep_result2.get('missing'):
                        pip_install_all(plugin_name, dep_result2['missing'])
                    continue
                else:
                    logger.error(
                        f"[{plugin_name}] 加载失败，依赖可能未正确安装\n"
                        f"  请检查: pip install {' '.join(dep_result.get('missing', []))}\n"
                        f"  或在 Web UI 插件管理页查看详情"
                    )
                    # 回滚 sys.modules 中残留的合成包与子模块
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    # 更新 DB 状态为 error
                    try:
                        self.db.execute(
                            "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                            (plugin_name,)
                        )
                    except Exception:
                        pass
                    return False

            except Exception as e:
                logger.error(f"[{plugin_name}] 加载失败: {e}", exc_info=True)
                # 回滚 sys.modules 中残留的合成包与子模块
                self._purge_plugin_modules(plugin_name, plugin_path)
                # 更新 DB 状态为 error
                try:
                    self.db.execute(
                        "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                        (plugin_name,)
                    )
                except Exception:
                    pass
                return False

    def _snapshot_mtime(self, plugin_name: str) -> float:
        """计算插件目录下所有 .py 文件的最大修改时间，用于变更检测"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)
        if not os.path.isdir(plugin_path):
            return -1.0
        latest = 0.0
        try:
            for root, _dirs, files in os.walk(plugin_path):
                for f in files:
                    if f.endswith('.py'):
                        try:
                            latest = max(latest, os.path.getmtime(os.path.join(root, f)))
                        except OSError:
                            pass
        except OSError:
            pass
        return latest
    def unload_plugin(self, plugin_name: str):
        """卸载插件"""
        with self._lock:
            info = self._loaded_plugins.pop(plugin_name, None)
            if not info:
                return

        try:
            # 调用 on_unload（如果存在）
            module = info['module']
            if hasattr(module, 'on_unload') and callable(module.on_unload):
                module.on_unload()
        except Exception as e:
            logger.warning(f"[{plugin_name}] on_unload 异常: {e}")

        # 清理调度器中的定时任务
        try:
            self.framework.scheduler.remove_plugin_tasks(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理调度器任务失败: {e}")

        # 清理数据库
        try:
            self.db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            self.db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            # 注意：不删除 plugin_configs —— 卸载/重载/更新/禁用都应保留用户配置，
            # 只有真正删除插件（delete_plugin）时才清配置
            self.db.execute("UPDATE plugins SET status='stopped', has_register=0 WHERE plugin_name=%s", (plugin_name,))
        except Exception as e:
            logger.error(f"[{plugin_name}] 卸载清理失败: {e}")

        # 清理缺失依赖记录
        with self._lock:
            self._missing_deps.pop(plugin_name, None)
            self._conflict_deps.pop(plugin_name, None)
            self._isolated_plugins.discard(plugin_name)

        # 若该插件接管了前端，卸载后回退框架默认前端
        self.clear_override_webui(plugin_name)

        # 移除事件订阅
        self.framework.event_bus.unsubscribe_plugin(plugin_name)

        # 移除原始消息处理器
        try:
            self.framework.unregister_raw_message_handlers(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理原始消息处理器失败: {e}")

        # 清理扩展点（hook）处理器，避免卸载后残留空引用
        try:
            self.framework.hooks.clear_plugin(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理扩展点失败: {e}")

        # 清理 sys.modules：删除该插件目录下的所有模块（点分层级名/下划线别名/短名
        # 指向同一模块对象，按 __file__ 一次清净，避免热重载污染）
        self._purge_plugin_modules(
            plugin_name, info.get('path') or os.path.join(self.plugins_dir, plugin_name))

        # 清理 sys.path：移除该插件的目录（避免路径污染其他插件）
        try:
            plugin_path = info.get('path') or os.path.join(self.plugins_dir, plugin_name)
            norm = os.path.normpath(plugin_path)
            sys.path = [p for p in sys.path if os.path.normpath(p) != norm]
        except Exception:
            pass

        # 清理文件快照
        with self._lock:
            self._plugin_mtimes.pop(plugin_name, None)

        # 强制清理模块引用，触发垃圾回收
        # 防御插件未关闭的文件句柄 / socket 连接 / 长连接残留
        try:
            del module
        except NameError:
            pass
        # 连续两次 gc.collect()：第一次回收循环引用，第二次回收析构链
        collected = gc.collect()
        if collected > 0:
            logger.debug(f"[{plugin_name}] gc.collect() 回收了 {collected} 个对象")
        gc.collect()

        logger.info(f"[{plugin_name}] 已卸载")

        # 插件已从内存移除，让路由表立即重建，避免路由到已卸载插件
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass
