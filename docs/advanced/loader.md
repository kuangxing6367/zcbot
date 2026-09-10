# 插件加载与模块机制

> **本篇面向**：角色 B/C。**插件要拆成多个文件前必读**，讲清合成包、相对导入与可靠热重载。

本文讲清楚 ZCBOT 到底是怎么把 `plugins/xxx/main.py` 变成一个运行中插件的，
尤其是**插件内部多个文件之间该怎么互相 import**。这是新人最容易踩坑的地方，
建议在写「不止一个 `main.py`」的插件前先读完本文。

:::tip 一句话结论
插件内部互相引用，**优先用相对导入** `from .ws_server import WsServer`；
旧插件里的短名绝对导入 `from ws_server import WsServer` 仍然兼容。
两种写法最终拿到的是同一个模块对象。
:::

## 一、插件生命周期总览

用户插件由 `framework/loader.py` 的 `PluginLoader` 管理，完整生命周期如下：

```
discover()                扫描 plugins/，凡是含 main.py 的子目录就算一个插件
   │
   ▼
load_plugin(name)         依赖检查/自动安装 → 建“合成包” → 预载子模块 → 执行 main.py
   │                      （只执行模块顶层代码、读取 __plugin_meta__、定位 register）
   ▼
register_commands(name)   调用 register(ctx)，收集命令/任务/事件订阅/卡片并落库
   │
   ▼
heartbeat_register()      每 60s 检查 .py 文件 mtime，变了就重新 register(ctx)
   │
   ▼
unload_plugin(name)       调 on_unload → 清理命令/任务/事件/sys.modules/sys.path → gc
```

启动顺序（`framework/core.py → Framework.start()`）：

1. 先加载 `core_plugins/` 官方插件（提供协议适配、调度器、会话等基础服务）；
2. 建立 `data/plugins_dat/` 数据目录并迁移旧配置；
3. `load_all()` 逐个加载 `plugins/` 用户插件（数据库里被禁用的跳过）；
4. 依赖自愈：首轮因缺依赖失败的插件，补装依赖后再试一次；
5. 逐个 `register_commands()`；
6. 启动路由表、统计写库器、心跳、内存看门狗，广播 `system.plugin.loaded`。

## 二、模块命名机制（重点）

### 2.1 为什么以前相对导入会报错

Python 判断 `from .xxx import Y` 能不能用，靠的是**当前模块的 `__package__`**：
它必须指向一个真正存在于 `sys.modules`、且带 `__path__` 的包。

早期加载器用 `importlib.util.spec_from_file_location("plugin_chatroom", main.py)`
直接按文件路径加载 `main.py`，得到的是一个**顶层模块**：

- `__name__ = "plugin_chatroom"`，但 `__package__` 为空、没有 `__path__`；
- 它在 Python 眼里不是任何包的成员，于是 `main.py` 里写
  `from .ws_server import WsServer` 会直接报：

```
ImportError: attempted relative import with no known parent package
```

当时的折中方案，是把每个子模块以 `plugin_chatroom_ws_server` 的唯一名加载，
再额外往 `sys.modules` 塞一个短名 `ws_server`，逼开发者用
`from ws_server import WsServer` 这种“短名绝对导入”。能用，但不符合 Python
原生包的直觉，也没人写进文档，于是新人一写 `from .` 就踩坑。

### 2.2 现在的设计：main 模块同时充当“合成包”

现在加载一个插件时，加载器会先创建模块 `plugin_<插件名>`，并给它补上包属性：

```python
module.__name__    = "plugin_chatroom"
module.__package__ = "plugin_chatroom"   # 自己就是自己的父包
module.__path__    = [".../plugins/chatroom"]  # 关键：让导入系统把它当“包”
module.__file__    = ".../plugins/chatroom/main.py"
```

随后 `main.py` 的代码直接执行进这个模块对象。于是它身兼两职：

1. **插件主模块**：`register`、`__plugin_meta__` 都在它身上，
   `sys.modules["plugin_chatroom"]` 仍然指向主模块（旧约定不变，跨插件访问靠它）；
2. **插件内部的父包**：因为有了 `__path__`，`from .ws_server import ...`
   会沿着 `__path__`（也就是插件目录）找到子模块。

### 2.3 一个子模块的三个名字

预载顶层子模块时（`_load_plugin_submodule`），**同一个模块对象**会同时注册到
`sys.modules` 的三个键上：

| `sys.modules` 键 | 例子 | 作用 |
|---|---|---|
| `plugin_<插件名>.<模块名>` | `plugin_chatroom.ws_server` | **规范点分层级名**，相对导入 `from .ws_server import X` 解析到它 |
| `plugin_<插件名>_<模块名>` | `plugin_chatroom_ws_server` | 旧版下划线唯一名，保留兼容，同时保证多插件零冲突 |
| `<模块名>` | `ws_server` | **短名**，兼容旧写法 `from ws_server import X` / `import ws_server` |

可以验证三者是同一对象：

```python
import sys
sys.modules["plugin_chatroom.ws_server"] is sys.modules["ws_server"]  # True
```

### 2.4 加载顺序

`load_plugin()` 内部严格按下面顺序执行，顺序错了相对导入会找不到父包：

1. 插件目录插入 `sys.path[0]`；
2. 依赖检查、自动安装；
3. **先建合成包** `plugin_<名>`（含 `__package__` / `__path__`）；
4. 预载顶层子模块：先所有顶层 `.py`，再所有包目录（含 `__init__.py` 的目录）；
5. 执行 `main.py`；
6. 校验 `register` 可调用，写入元数据与数据库。

嵌套的包（如 `core/sub.py`）不需要预载：父包一旦带了 `__path__`，
Python 原生导入机制会在运行到 `from .core.sub import X` 时自动按目录找到它，
规范名是 `plugin_chatroom.core.sub`。

### 2.5 一张图看懂解析过程

以 `plugins/chatroom/` 为例：

```
plugins/chatroom/
├── main.py            → sys.modules["plugin_chatroom"]（合成包，__path__ 指向本目录）
├── ws_server.py       → plugin_chatroom.ws_server / plugin_chatroom_ws_server / ws_server
├── utils.py           → plugin_chatroom.utils      / plugin_chatroom_utils     / utils
└── core/
    ├── __init__.py    → plugin_chatroom.core（包，__path__ 指向 core/）
    └── sub.py         → plugin_chatroom.core.sub（运行时按需懒加载）
```

`main.py` 里各种写法的解析结果：

| 写法 | 解析到 | 是否推荐 |
|---|---|---|
| `from .ws_server import WsServer` | `plugin_chatroom.ws_server` | 推荐 |
| `from . import utils` | `plugin_chatroom.utils` | 推荐 |
| `from .core.sub import X` | `plugin_chatroom.core.sub` | 推荐 |
| `from ws_server import WsServer` | 短名 `ws_server`（同一对象） | 兼容可用 |
| `import utils` | 短名 `utils`（同一对象） | 兼容可用 |
| `from ..utils import U`（在 `core/sub.py` 内） | `plugin_chatroom.utils` | 推荐 |

## 三、插件内导入规则

### 3.1 推荐：Python 原生相对导入

多文件插件请像写普通包一样写相对导入，清晰、无歧义、不怕重名：

```python
# plugins/chatroom/main.py
from .ws_server import WsServer      # 导入同目录模块里的类
from . import utils                  # 导入整个兄弟模块
from .core.sub import X              # 导入子包里的模块
```

```python
# plugins/chatroom/core/sub.py
from ..utils import U                # 回到上一层
from . import helper                 # 同包内兄弟
```

### 3.2 兼容：短名绝对导入

因为插件目录被插入了 `sys.path`，且顶层模块注册了短名，这种旧写法继续可用：

```python
# plugins/chatroom/main.py
from ws_server import WsServer
import utils
```

:::warning 短名的“最后加载者占槽”语义
短名 `ws_server` 是全局唯一的槽位。如果两个插件都有 `ws_server.py`，
后加载的插件会把这个短名槽位覆盖成自己的模块。**但不影响先加载插件已经绑定好的引用**
（`from ws_server import WsServer` 在导入那一刻就绑定了对象）。
这也是为什么新代码推荐相对导入——它永远只命中本插件，不依赖全局槽位顺序。
:::

### 3.3 引用框架与第三方库

```python
from framework.ctx import ...          # 框架代码：正常绝对导入（项目根在 sys.path）
from framework.event import Event
import requests                       # 第三方库：写进 requirements.txt 自动安装
```

### 3.4 跨插件访问主模块

插件 A 想调用插件 B 暴露在主模块上的能力，用规范主模块名：

```python
import sys
img = sys.modules.get("plugin_image_renderer")   # 拿不到返回 None，不要用 [] 直接取
if img is not None and hasattr(img, "_send_image"):
    img._send_image(ctx, event, data)
```

注意拿到的是对方的**主模块**（`plugin_<名>`），不是子模块。是否加载、加载顺序
不要写死，必要时订阅 `system.plugin.loaded` 事件后再取。

### 3.5 不要做的事

- 不要假设 `__file__` 的上级目录是标准安装包、用 `import plugins.chatroom.xxx`；
- 不要依赖别的插件的**短名**（`import ws_server`）来跨插件引用，短名会被覆盖；
- 不要在模块顶层执行耗时操作或立刻调用尚未就绪的服务（服务可能还没注册，
  改为在 `register(ctx)` 或订阅 `system.plugin.loaded` 后使用）；
- 避免循环导入：A 顶层导入 B、B 顶层又导入 A。把共用逻辑下沉到第三个模块。

## 四、多插件同名模块隔离

两个插件都叫 `db.py` / `utils.py` 是常态。隔离靠两点：

1. **规范名带插件前缀**：`plugin_a.db` 与 `plugin_b.db` 是两个互不干扰的键；
2. **预载发生在执行 main 之前**：本插件 `main.py` 执行导入语句时，
   `sys.modules` 里已经躺着本插件自己的 `plugin_<本插件>.db`，不会误命中别人。

即使预载失败或漏载，合成包的 `__path__` 仍会让原生导入机制按**本插件目录**
兜底重新解析，不会串到别的插件目录。

## 五、热重载与卸载清理

### 5.1 两种“重载”要分清

| 机制 | 触发 | 是否重新 import 代码 | 适用 |
|---|---|---|---|
| 心跳增量注册 `heartbeat_register()` | 每 60s 发现 `.py` 的 mtime 变化 | **否**，只用已加载模块里的 `register` 再跑一遍 `register(ctx)` | 刷新注册结构（增删命令/任务声明） |
| 完全重载 `unload_plugin()` + `load_plugin()` | Web 面板「重载」、管理接口、终端 `reload` | **是**，清空模块后重新从磁盘加载 | 修改了函数体/类逻辑后要看到新代码 |

:::warning 改了函数逻辑但没生效？
心跳只重新执行 `register(ctx)`，handler 仍是旧模块里的旧函数对象。
**改了处理函数内部逻辑，请在 Web 面板点「重载」（或调用完全重载），而不是等心跳。**
:::

### 5.2 卸载时怎么清理 sys.modules

`_purge_plugin_modules()` 会遍历 `sys.modules`，把所有 `__file__` 位于本插件
目录下的模块全部弹出——无论它挂在点分层级名、下划线唯一名还是短名下，
因为它们是同一对象、`__file__` 相同，一次扫净；最后再兜底弹出
`plugin_<插件名>`。随后移除 `sys.path` 里的插件目录并连续 `gc.collect()`。

这套清理同时被三处复用，保证不留“幽灵模块”：

- `unload_plugin()`：正常卸载/完全重载；
- `load_plugin()` 第二次尝试前：清掉首轮半初始化残留，让重试幂等；
- `load_plugin()` 各失败分支：加载失败也回滚，不污染后续加载。

### 5.3 为什么插件不使用 `__pycache__`（热重载必定取最新源码）

CPython 默认按「源码**整数秒** mtime + 文件大小」校验 `.pyc`：如果在同一秒内
把文件改成**相同字节数**（自动化改码、脚本快速热重载很常见），即使内容变了，
校验也会误判字节码有效，进而执行旧代码——这是 CPython 通用行为，与模块命名无关。

框架用两道手段保证「磁盘上是什么，跑的就是什么」：

1. **主模块与顶层子模块**由专用的 `_PluginSourceLoader` 加载，它重写 `get_code()`
   直接从 `.py` 源码现场编译，并重写 `set_data()` 不写 `.pyc`，因此完全绕开字节码
   缓存，同秒、同尺寸改写也能立即生效；
2. **深层嵌套包**由原生导入机制沿合成包 `__path__` 懒加载，框架在每次 `load_plugin()`
   建包前调用 `_clear_plugin_bytecode_cache()` 清掉本插件各层 `__pycache__`，
   并 `importlib.invalidate_caches()`，覆盖到这些懒加载模块。

代价仅是插件每次加载多一次编译（插件加载频率很低，可忽略），换来完全确定的热重载。

## 六、依赖与加载失败重试

- 依赖来源合并顺序：`requirements.txt` → `plugins_dat/<名>/plugin.yaml`
  → 代码目录 `plugin.yaml`（同名以更靠后的约束为准）；
- 缺依赖时自动用内置镜像源列表（清华→阿里→豆瓣→官方）安装到**当前环境**，
  冲突版本不覆盖全局包；需要隔离可在 Web 面板为插件单独建虚拟环境；
- `load_plugin()` 遇到 `ImportError` 最多重试 1 次（补装依赖后再来一遍），
  其他异常立即失败；无论哪条失败路径都会回滚 `sys.modules` 并把插件状态置为 `error`。

## 七、排错 FAQ

### `ImportError: attempted relative import with no known parent package`

说明模块没有被当作包成员加载。确认：

1. 你的框架版本是否为本文所述的合成包机制（`loader.py` 里有
   `_ensure_plugin_package`）；旧版本请改用短名绝对导入或升级；
2. 你是不是绕过框架、自己用 `python plugins/xxx/main.py` 直接跑了？
   插件必须由框架加载，直接运行脚本时没有合成包上下文。

### `ModuleNotFoundError: No module named 'xxx'`

- 若是第三方库：写进 `requirements.txt`，看启动日志是否自动安装成功；
- 若是本插件子模块：检查文件名拼写、是否放在插件目录顶层、
  包目录是否缺 `__init__.py`。

### 两个插件的同名模块好像串了

规范名（`plugin_<名>.<模块>`）不会串；如果你用短名导入又在**运行期动态 import**，
短名槽位可能已被后加载者占据。改成相对导入即可彻底规避。

### 改了代码不生效

- 先分清重载类型，见 [5.1](#51-两种重载要分清)：函数体改动要走「完全重载」，
  心跳（1 分钟）只重新调用 `register()`，不会重新执行模块代码；
- 确认是「完全重载」仍不生效：本框架已绕开 `.pyc` 校验（见
  [5.3](#53-为什么插件不使用-__pycache__热重载必定取最新源码)），若仍复现，
  检查是否有别处缓存了旧模块对象（如全局单例、已绑定的类引用），
  或改动落在了未被重新导入的深层懒加载模块上。

### 子模块预载失败日志：`子模块 xxx 预加载失败（回退到全局查找）`

该模块顶层代码抛了异常。加载器会回滚它的登记并继续；如果 `main.py` 确实要用到它，
相对导入会沿 `__path__` 再加载一次并把真实错误抛出来——按堆栈修掉子模块自身的问题即可。

## 八、维护者速查

相关方法均在 `framework/loader.py` 的 `PluginLoader`：

| 方法 | 职责 |
|---|---|
| `discover()` | 扫描含 `main.py` 的插件目录 |
| `load_plugin()` | 依赖处理 + 建包 + 预载 + 执行 main（含一次重试与失败回滚） |
| `_ensure_plugin_package()` | 创建合成包 `plugin_<名>`，设置 `__package__`/`__path__` |
| `_preload_plugin_submodules()` | 预载顶层 `.py` 与包目录 |
| `_load_plugin_submodule()` | 为一个子模块注册三个名字 |
| `_purge_plugin_modules()` | 按 `__file__` 清理某插件在 `sys.modules` 的全部模块 |
| `register_commands()` | 执行 `register(ctx)`，同步命令/任务/卡片，触发 `on_loaded` |
| `unload_plugin()` | 完整卸载（钩子/任务/事件/模块/路径/gc） |
| `heartbeat_register()` | mtime 变化时增量重新 `register(ctx)`（不重新 import） |
