# 第 8 轮侦察材料：framework/loader/lifecycle.py 第 350~479 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
 350         删除插件目录下的 __pycache__，保证（完全）重载时总是从源码重新编译。
 351 
 352         CPython 依据“源码整数秒 mtime + 文件大小”校验 .pyc：若在同一秒内把
 353         文件改成相同字节数（自动化改码/快速热重载场景），会误判字节码仍有效而
 354         复用旧代码。插件加载并不频繁，直接清掉本插件的字节码缓存最稳妥；
 355         只删除本插件目录内的 __pycache__，不影响其他插件。
 356         """
 357         try:
 358             for root, dirs, _files in os.walk(plugin_path):
 359                 if "__pycache__" in dirs:
 360                     shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
 361             importlib.invalidate_caches()
 362         except Exception as e:
 363             logger.debug(f"清理插件字节码缓存异常: {e}")
 364 
 365     def load_plugin(self, plugin_name: str, plugin_path: str = None) -> bool:
 366         """加载单个插件，返回是否成功。
 367 
 368         plugin_path 缺省为 plugins/<name>；市场插件声明 install_target=core_plugins
 369         时会传 core_plugins/<name>，其余加载流程不变。
 370         """
 371         plugin_path = plugin_path or os.path.join(self.plugins_dir, plugin_name)
 372 
 373         # ====== 声明式依赖校验（__plugin_meta__['requires']）======
 374         # 依赖未加载就不加载本插件：半个插件（register 跑完但拿不到依赖的服务）
 375         # 比明确失败更难排查。load_all 已拓扑排序，这里是单插件加载/热重载的兜底。
 376         unmet = self.check_requires(plugin_name, plugin_path)
 377         if unmet:
 378             reason = f"缺少依赖插件: {', '.join(unmet)}"
 379             logger.error(f"[{plugin_name}] 依赖未满足，拒绝加载：{reason}"
 380                          f"（__plugin_meta__['requires']）")
 381             self._record_dep_error(plugin_name, reason)
 382             return False
 383         self.clear_dep_error(plugin_name)
 384 
 385         # 将插件目录加入 sys.path
 386         if plugin_path not in sys.path:
 387             sys.path.insert(0, plugin_path)
 388 
 389         # 读取插件配置文件
 390         yaml_data = self.read_plugin_yaml(plugin_name)
 391 
 392         # ====== 依赖检查 + 自动安装（基于全局环境） ======
 393         # 说明：框架不再为插件自动创建/重建隔离虚拟环境；
 394         # 依赖自动安装到全局环境，存在版本冲突的依赖自动跳过（不覆盖全局包），
 395         # 如需隔离请手动点击「创建虚拟环境」。
 396         dep_result = self.check_dependencies(plugin_name)
 397         if dep_result.get('missing'):
 398             pip_install_all(plugin_name, dep_result['missing'])
 399 
 400             # 重新检查依赖
 401             dep_result = self.check_dependencies(plugin_name)
 402             if dep_result.get('missing'):
 403                 missing = ', '.join(dep_result['missing'])
 404                 logger.warning(f"[{plugin_name}] 仍有缺失依赖: {missing}，尝试加载...")
 405 
 406         # 记录依赖状态供 Web UI 展示
 407         self._record_dep_status(
 408             plugin_name,
 409             dep_result.get('missing', []),
 410             dep_result.get('conflicts', [])
 411         )
 412 
 413         # ====== 动态导入 main.py（带重试）======
 414         main_path = os.path.join(plugin_path, 'main.py')
 415 
 416         # 源码静态扫描：先审后执行。默认 warn —— 命中只写日志并照常加载，
 417         # 把「插件写错就整个起不来」换成「能看到问题、行为不变」；
 418         # 需要硬拦截时把 plugins.source_scan.mode 改成 block。
 419         _scan_cfg = getattr(self.framework, 'config', None)
 420         scan_issues = scan_plugin(plugin_name, main_path, _scan_cfg)
 421         if scan_issues:
 422             detail = '；'.join(str(i) for i in scan_issues[:5])
 423             if scan_mode(_scan_cfg) == 'block':
 424                 logger.error(f"[{plugin_name}] 源码静态检查未通过，拒绝加载：{detail}")
 425                 self._purge_plugin_modules(plugin_name, plugin_path)
 426                 self._release_plugin_api_routes(plugin_name)
 427                 self._record_dep_error(plugin_name, f'源码静态检查未通过: {detail}')
 428                 self._failed_mtimes[plugin_name] = self._snapshot_mtime(plugin_name)
 429                 return False
 430             logger.warning(
 431                 f"[{plugin_name}] 源码静态检查命中 {len(scan_issues)} 处可疑写法，"
 432                 f"按 warn 模式继续加载：{detail}")
 433 
 434         for attempt in range(2):  # 最多重试1次
 435             try:
 436                 # 第二次尝试前清掉首次残留的半初始化模块，保证重试幂等
 437                 if attempt == 1:
 438                     self._purge_plugin_modules(plugin_name, plugin_path)
 439 
 440                 # 0) 清掉本插件 __pycache__，避免同秒同尺寸改动复用旧字节码
 441                 self._clear_plugin_bytecode_cache(plugin_path)
 442 
 443                 # 1) 先建合成包 plugin_<插件名>（含 __package__/__path__）：
 444                 #    它既是 main.py 的执行载体，也是插件相对导入的父包
 445                 module = self._ensure_plugin_package(plugin_name, plugin_path)
 446 
 447                 # 2) 预加载顶层子模块（点分层级名 + 下划线唯一名 + 短名，同名模块互不污染）
 448                 self._preload_plugin_submodules(plugin_name, plugin_path)
 449 
 450                 # 3) main.py 直接执行进合成包模块（不能再 module_from_spec，
 451                 #    否则刚设置的 __package__/__path__ 会重置为普通顶层模块，相对导入又会失效）
 452                 spec = importlib.util.spec_from_file_location(
 453                     f"plugin_{plugin_name}",
 454                     main_path,
 455                     loader=_PluginSourceLoader(f"plugin_{plugin_name}", main_path)
 456                 )
 457                 if spec is None or spec.loader is None:
 458                     logger.error(f"[{plugin_name}] 导入失败: spec 为空")
 459                     self._purge_plugin_modules(plugin_name, plugin_path)
 460                     # 半初始化可能已登记 API 路由：失败终态一并回收，避免旧闭包驻留
 461                     self._release_plugin_api_routes(plugin_name)
 462                     self._failed_mtimes[plugin_name] = self._snapshot_mtime(plugin_name)
 463                     return False
 464 
 465                 module.__spec__ = spec
 466                 module.__loader__ = spec.loader
 467                 spec.loader.exec_module(module)
 468 
 469                 # 检查 register 函数
 470                 # 文档承诺「用了装饰器可以省略 def register(ctx)」：导入期已经
 471                 # 用 @command/@on 等登记过的插件，即使没有 register 也允许加载，
 472                 # 由 register_commands 的 flush 落账。两者都没有才是空插件，拒绝。
 473                 if not hasattr(module, 'register'):
 474                     from framework.plugin import has_pending as _has_pending
 475                     if _has_pending(module.__name__):
 476                         register_func = None
 477                         logger.info(
 478                             f"[{plugin_name}] 未定义 register，使用装饰器登记项加载")
 479                     else:
```
