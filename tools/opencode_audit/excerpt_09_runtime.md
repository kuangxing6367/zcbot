# 第 9 轮侦察材料：framework/core/runtime.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """Framework 核心插件加载 / 依赖自愈 / 心跳·内存看门狗 / 内置任务（自 core.py 剥离的 mixin）"""
   2 import ast
   3 import asyncio
   4 import gc
   5 import importlib.util
   6 import logging
   7 import os
   8 import sys
   9 import time
  10 
  11 from framework import i18n
  12 from framework.deps import classify_load_error
  13 from framework.loader.source_scan import scan_mode, scan_plugin
  14 from framework.memory import log_malloc_hint, process_memory_snapshot, trim_memory
  15 
  16 logger = logging.getLogger('zcbot')
  17 
  18 class FrameworkRuntimeMixin:
  19     """核心插件加载 / 依赖自愈 / 心跳·内存看门狗 / 内置任务"""
  20 
  21     # 以下方法依赖 Framework.__init__ 建立的实例字段
  22 
  23     def _load_core_plugins(self, only=None):
  24         """加载官方插件（core_plugins/ 目录）
  25 
  26         :param only: 只加载这些插件名（依赖自愈后重试单个插件用）；None = 全部。
  27                      已加载的插件一律跳过，保证可重复调用而不重复 register。
  28         """
  29         from framework.paths import core_plugins_dir as _core_plugins_dir
  30         core_plugins_dir = _core_plugins_dir()
  31         if not os.path.isdir(core_plugins_dir):
  32             logger.warning(f"core_plugins 目录不存在: {core_plugins_dir}")
  33             return
  34 
  35         core_cfg = self.config.get('core_plugins', {})
  36 
  37         # 双进程角色过滤（按插件 __plugin_meta__['process'] 自动分派，不再硬编码名单）：
  38         #  - process='core'：协议/Web 基础设施，只在核心进程与单进程加载，宿主进程排除
  39         #  - 其余（插件侧能力）：单进程与宿主进程加载，纯核心进程排除
  40         # 仍兼容 dual_process.core_plugins 显式名单覆盖（配置了就以配置为准）。
  41         dual = self.config.get('dual_process', {})
  42         _explicit_core = dual.get('core_plugins')
  43         if isinstance(_explicit_core, list) and _explicit_core:
  44             _explicit_core = set(_explicit_core)
  45         else:
  46             _explicit_core = None
  47         _role = getattr(self, '_role', 'standard')
  48 
  49         for name in os.listdir(core_plugins_dir):
  50             if name.startswith('_'):
  51                 continue
  52             if only is not None and name not in only:
  53                 continue
  54             # 幂等：自愈后会带着 only= 再调一次，已加载的不能重复 register
  55             if name in self.plugin_loader._loaded_plugins:
  56                 continue
  57             plugin_dir = os.path.join(core_plugins_dir, name)
  58             main_file = os.path.join(plugin_dir, 'main.py')
  59             if not os.path.isfile(main_file):
  60                 continue
  61 
  62             # 双进程角色分派（单进程 standard 不过滤，全部加载）
  63             if _role in ('core', 'host'):
  64                 _core_side = self._core_plugin_is_core_side(name, main_file, _explicit_core)
  65                 if _role == 'core' and not _core_side:
  66                     continue
  67                 if _role == 'host' and _core_side:
  68                     continue
  69 
  70             # 检查配置开关。core_plugins.yaml（经 _autoload_core_plugins 合并）
  71             # 是唯一权威：未在清单中列出的插件视为未配置——一律禁用并提示
  72             # 运行扫描工具，杜绝"未列出自动启用"的误加载（历史缺省 True 之坑）。
  73             enabled = core_cfg.get(name)
  74             if enabled is None:
  75                 logger.warning(
  76                     f"官方插件 [{name}] 未在 core_plugins.yaml 中配置，不加载；"
  77                     "如需启用请运行: python tools/scan_core_plugins.py --enable "
  78                     f"{name}")
  79                 continue
  80             # 兼容两种配置形态：{name: bool}（_autoload_core_plugins 合并产物）
  81             # 与 {name: {enabled: ...}}（手写配置块 / 非标准注入路径），
  82             # 以及 'false'/'0'/None 等非布尔值——缺失一律按禁用处理，
  83             # 避免 enabled:false 的插件被 eager import 白白占用内存（约 10MB/个）。
  84             if isinstance(enabled, dict):
  85                 enabled = enabled.get('enabled', True)
  86             if isinstance(enabled, str):
  87                 enabled = enabled.strip().lower() in ('1', 'true', 'yes', 'on', 'y')
  88             if not enabled:
  89                 logger.info(i18n.t('plugin.disabled', name=name, enabled=enabled))
  90                 continue
  91 
  92             try:
  93                 scan_issues = scan_plugin(name, main_file, self.config)
  94                 if scan_issues:
  95                     detail = '；'.join(str(i) for i in scan_issues[:5])
  96                     if scan_mode(self.config) == 'block':
  97                         logger.error(
  98                             f"官方插件 [{name}] 源码静态检查未通过，跳过加载：{detail}")
  99                         continue
 100                     logger.warning(
 101                         f"官方插件 [{name}] 源码静态检查命中 {len(scan_issues)} 处可疑写法，"
 102                         f"按 warn 模式继续加载：{detail}")
 103                 spec = importlib.util.spec_from_file_location(
 104                     f"core_plugin_{name}", main_file,
 105                     submodule_search_locations=[plugin_dir])
 106                 module = importlib.util.module_from_spec(spec)
 107                 sys.modules[f"core_plugin_{name}"] = module
 108                 spec.loader.exec_module(module)
 109 
 110                 # 调用 register(ctx)
 111                 from framework.ctx import PluginContext
 112                 ctx = PluginContext(f"core:{name}", self)
 113                 module.ctx = ctx
 114                 meta = getattr(module, '__plugin_meta__', {})
 115 
 116                 if hasattr(module, 'register'):
 117                     module.register(ctx)
 118                     # 应用官方插件模块级装饰器登记的注册项（@command/@on/@hook/...）
 119                     try:
 120                         from framework.plugin import flush as _flush_plugin
 121                         _flush_plugin(module.__name__, ctx)
 122                     except Exception as e:
 123                         logger.warning(f"官方插件 [{name}] 装饰器 flush 失败: {e}")
 124 
 125                     # 黑匣子：记录官方插件加载期调用的 ctx 函数（与用户插件 register_commands
 126                     # 行为对齐——回答"加载时调了哪些 ctx 函数 / 订阅了哪些事件 / 注册了哪些命令任务"）。
 127                     # module.register(ctx) 与 flush 已完成，此刻 ctx._register_journal 仅含加载期调用；
 128                     # 读完即清空，避免运行时调用混入。
 129                     try:
 130                         from framework.blackbox import record as _bb_record
```
