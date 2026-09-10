"""
配置加载模块
支持环境变量替换：${VAR_NAME} 或 ${VAR_NAME:-default_value}
首次启动时 config.yaml 不存在则自动生成默认配置
"""
import os
import re
import yaml

logger = None  # 延迟初始化，避免循环导入


def _get_logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger('zcbot')
    return logger


# 默认配置模板（首次启动自动生成）
_DEFAULT_CONFIG = """\
# ============================================================
# ZCBOT 插件化服务宿主 配置文件
# 首次启动自动生成，可按需修改后重启
# ============================================================

# ── 数据库配置 ──────────────────────────────────────────────
# SQLite 模式（默认，零配置开箱即用）
# MySQL 模式请改为: database: { type: mysql, host: 127.0.0.1, port: 3306, user: root, password: '', database: zcbot }
database:
  type: sqlite
  path: data/zcbot.db
  # ── MySQL 连接保活/自动重连（type: mysql 时生效） ──
  # 数据库断开（wait_timeout/服务重启/网络中断）后自动重连，不再卡死
  # ping_interval: 5        # 连接空闲超过该秒数后自动 ping 检测存活
  # connect_timeout: 10     # 建立连接超时（秒）
  # read_timeout: 30        # 读超时（秒），避免断连后无限阻塞
  # write_timeout: 30       # 写超时（秒）
  # max_reconnect: 3        # 单次操作最大自动重连次数

# ── OneBot WebSocket 服务端 ─────────────────────────────────
# OneBot 客户端反向连接此端口（如 NapCat、Lagrange 等）
onebot:
  listen_port: 6830
  access_token: ""           # 必须设置！留空则不校验 token，任何客户端都能接入

# ── Web UI 管理后台 ─────────────────────────────────────────
web:
  host: 127.0.0.1            # 仅本机访问；需要局域网/公网访问请改为 0.0.0.0（注意安全）
  port: 8080
  # secret_key: ""          # 留空则每次重启随机生成，填入后重启保持登录态
  session_timeout: 3600      # 登录会话超时（秒），同时作为登录 token 的有效期

# ── HTTP API（可选，需同时开启 core_plugins.http_api）────────
# 给外部程序用的 RESTful 接口，默认监听 1145 端口，token 认证
# ⚠️ db/query 与 db/execute 是「数据库网关」，可执行任意 SQL（含写库）
#    默认关闭；确实需要时再设 allow_db: true，且务必给 token 限权
http_api:
  # enabled: false           # 与 core_plugins.http_api 配合；核心插件默认关闭
  host: 127.0.0.1
  port: 1145
  token: ""                  # 留空则每次启动自动生成（打印到日志）
  allow_db: false            # 是否开放 db/query、db/execute（默认关闭，高危）

# ── HTTP 事件注入接入端（可选，需同时开启 core_plugins.http_inject）──
# 外部系统 POST 注入消息/事件，由插件处理；不依赖任何 IM
http_inject:
  enabled: false             # 默认关闭，避免意外开端口
  host: 127.0.0.1
  port: 8901
  path: /hook                # POST http://host:port/path
  token: ""                  # 可选；留空则不校验（建议仅内网使用）

# ── 官方插件开关（core_plugins/） ─────────────────────────────
# ZCBOT 内置的官方插件，每个都可独立开关（true 加载 / false 禁用）
# 启动时会自动扫描 core_plugins/ 目录，把「已安装但下方未列出」的官方插件
# 自动补进本段（缺省值：onebot_adapter/webui/session/scheduler=true，
# http_api/http_inject=false），无需手动维护；false 表示禁用。
# 说明：
#   - http_api / http_inject 需「此处开启」且「下方对应段自身 enabled」都满足才生效
#   - 双进程模式下（dual_process.enabled: true），这些插件全部跑在进程1（核心），
#     进程2（宿主）只加载 plugins/ 下的用户插件
core_plugins:
  onebot_adapter: true       # OneBot 11 WebSocket 协议接入
  webui: true                # Web 管理后台
  session: true              # 会话管理器
  scheduler: true            # 定时任务调度器
  http_api: false            # HTTP REST API（给外部程序用）
  http_inject: false         # HTTP 事件注入接入端

# ── 插件配置 ────────────────────────────────────────────────
plugin:
  dir: plugins               # 插件代码目录（.py 文件）
  # dat_dir: data/plugins_dat # 插件数据/配置目录（默认统一存放于 data/plugins_dat）
  heartbeat_interval: 60     # 插件注册心跳间隔（秒）
  auto_install_deps_on_startup: true  # 启动时自动安装缺失依赖到全局环境（移机自愈，版本冲突自动跳过）
  max_memory_mb: 64          # 单插件内存上限（MB），超限自动卸载

# ── 日志配置 ────────────────────────────────────────────────
log:
  level: INFO                # DEBUG / INFO / WARNING / ERROR
  file: data/logs/zcbot.log  # 日志文件路径（统一存放于 data/logs/），留空则只输出控制台
  log_raw_message: true      # 是否记录收到的原始消息内容
  log_sent_message: true     # 是否记录发送到 OneBot11 的消息内容

# ── GitHub 加速 ─────────────────────────────────────────────
# 插件市场 / 插件下载 / 框架更新的 GitHub 加速代理地址
# 留空则使用内置 ghproxy 镜像回退；国内网络直连 GitHub 慢/失败时填写，如 https://ghproxy.net
github_proxy: ""

# ── 系统配置 ────────────────────────────────────────────────
system:
  show_cpu: true             # 仪表盘显示 CPU 使用率
  show_disk: true            # 仪表盘显示磁盘使用率
  status_interval: 30        # 系统状态刷新间隔（秒）

# ── 安全配置（双请求防破解认证系统） ─────────────────────────
# 保护管理后台免受暴力破解与自动化脚本攻击
# 客户端必须按顺序发送两次请求：先发 fake_token_len 位探针获取 nonce，再发 real_token_len 位 Token + nonce
security:
  fake_token_len: 8            # 探针请求的 Token 长度（任意字符串）
  real_token_len: 8192         # 真实认证的 Token 长度
  nonce_len: 16                # 下发 nonce 的长度
  fake_response_msg: "你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
  nonce_expiry: 60             # nonce 有效期（秒）
  blacklist_enabled: true      # 是否启用黑名单（持久化到数据库 ip_blacklist 表，重启不清除）
  whitelist_ips:               # 白名单 IP，跳过所有检查
    - "127.0.0.1"
  # success_fake_data:         # 认证通过后返回的迷惑性数据（不填则使用默认假数据）
  # 内网 IP（127.*、192.168.*、172.16-31.*、10.*）自动豁免蜜罐拉黑，避免误伤本机/局域网

# ── 双进程架构（可选） ────────────────────────────────────────
# 一次启动拆成两个进程，用标准库 IPC 通信（零第三方依赖）：
#   进程1（核心）：Web/WebUI、协议接入（onebot_adapter/http_inject）、真实数据库、IPC 服务端
#   进程2（宿主）：加载并执行全部用户插件，经 IPC 访问数据库/发消息/注册远程路由/推送日志
# 默认关闭（单进程，行为完全不变）；开启后可隔离插件故障、降低核心内存占用
# 其余能力（崩溃自动重启、跨进程事务、日志合并、远程 REST 路由）见架构文档
dual_process:
  enabled: false             # 是否启用双进程
  core_plugins:              # 核心进程加载的官方插件白名单（缺省由插件 process:'core' 标记自动判定）
    - onebot_adapter
    - http_inject
    - http_api
    - webui
  max_restarts: 5           # 宿主崩溃重启限流：窗口内最大重启次数（默认 5）
  restart_interval: 30      # 限流窗口（秒）（默认 30）
"""


def _env_replace(value):
    """
    递归遍历配置值，将 ${VAR_NAME} 替换为环境变量
    支持默认值语法：${VAR_NAME:-default}
    """
    if isinstance(value, str):
        def replacer(m):
            expr = m.group(1)
            if ':-' in expr:
                var, default = expr.split(':-', 1)
                return os.environ.get(var, default)
            return os.environ.get(expr, m.group(0))  # 未找到则不替换
        return re.sub(r'\$\{([^}]+)\}', replacer, value)
    elif isinstance(value, dict):
        return {k: _env_replace(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_env_replace(v) for v in value]
    return value


def _generate_default_config(config_path: str):
    """生成默认配置文件"""
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.isdir(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(_DEFAULT_CONFIG)
    _get_logger().info(f"已生成默认配置文件: {config_path}，请按需修改后重启")


def load_config(config_path: str = None) -> dict:
    """
    加载 YAML 配置文件，支持环境变量替换
    如果配置文件不存在，自动生成默认配置并加载
    """
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.yaml')

    if not os.path.isfile(config_path):
        _get_logger().warning(f"配置文件不存在，正在生成默认配置: {config_path}")
        _generate_default_config(config_path)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 环境变量替换
    config = _env_replace(config)

    # 官方插件配置中心：自动扫描 core_plugins/ 同步 core_plugins.yaml，并合并进 config
    _autoload_core_plugins(config)

    return config


def get_config() -> dict:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


# 默认禁用（需显式开启）的官方插件——安全/端口相关，避免误开
_CORE_PLUGIN_DEFAULT_DISABLED = ('http_api', 'http_inject')


# 官方插件配置中心：独立 yaml，由启动时自动扫描 core_plugins/ 目录同步
CORE_PLUGINS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'core_plugins.yaml')


# 官方插件 → 主 config 段名映射（仅列段名与插件名不同的）
_CORE_PLUGIN_SECTION = {'onebot_adapter': 'onebot', 'webui': 'web'}


# 官方插件默认配置（含 enabled 与主要配置项），自动写入 core_plugins.yaml。
# 未列出的已安装插件也会被扫描加入（enabled 按 _default_core_plugin_enabled）。
_CORE_PLUGIN_SCHEMA = {
    'onebot_adapter': {'enabled': True, 'listen_host': '0.0.0.0',
                       'listen_port': 6830, 'access_token': ''},
    'webui': {'enabled': True, 'host': '127.0.0.1', 'port': 8080},
    'session': {'enabled': True},
    'scheduler': {'enabled': True},
    'http_api': {'enabled': False, 'host': '127.0.0.1', 'port': 1145,
                 'token': '', 'allow_db': False},
    'http_inject': {'enabled': False, 'host': '127.0.0.1', 'port': 8901,
                    'path': '/hook', 'token': ''},
}


def _scan_core_plugins() -> list:
    """扫描 core_plugins/ 目录，返回已安装（含 main.py）的官方插件名列表"""
    plugins_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), 'core_plugins')
    names = []
    if os.path.isdir(plugins_dir):
        for name in os.listdir(plugins_dir):
            if name.startswith('_'):
                continue
            if os.path.isfile(os.path.join(plugins_dir, name, 'main.py')):
                names.append(name)
    return sorted(names)


def _default_core_plugin_enabled(name: str) -> bool:
    """官方插件缺省开关：http_api/http_inject 默认 false，其余默认 true"""
    return name not in _CORE_PLUGIN_DEFAULT_DISABLED


def _autoload_core_plugins(config: dict) -> dict:
    """官方插件配置中心（core_plugins.yaml）自动同步 + 合并进主 config。

    流程：
      1. 扫描 core_plugins/ 目录得到已安装官方插件；
      2. 读 core_plugins.yaml，为「已安装但缺失」的插件补默认配置块；
      3. 移除 yaml 中已不再安装的插件块（卸载自动删除）；
      4. 有变化则回写 yaml（自动更新，用户无需手动维护）；
      5. 把每个插件的 enabled 合并进 config['core_plugins']，把插件配置块
         合并进对应 section（onebot/web/http_api/...），插件现有
         `fw.config.get('onebot')` 等读取方式无需改动。
    返回合并后的 {plugin: cfg}。
    """
    installed = _scan_core_plugins()
    if not installed:
        return {}
    yaml_path = CORE_PLUGINS_YAML

    # 读现有 yaml
    data = {}
    if os.path.isfile(yaml_path):
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            _get_logger().warning(f"读取 core_plugins.yaml 失败: {e}")
    cps = data.get('core_plugins') if isinstance(data, dict) else None
    cps = cps if isinstance(cps, dict) else {}

    # 主 config 中已有的官方插件开关（用于首次生成时迁移，向后兼容不丢设置）
    main_cp = config.get('core_plugins')
    if not isinstance(main_cp, dict):
        main_cp = {}

    changed = False
    # 2. 已安装缺失插件 → 补默认配置块；优先迁移主 config 已有值
    for name in installed:
        section = _CORE_PLUGIN_SECTION.get(name, name)
        main_sec = config.get(section)
        if not isinstance(main_sec, dict):
            main_sec = {}
        blk = cps.get(name)
        if not isinstance(blk, dict):
            blk = dict(_CORE_PLUGIN_SCHEMA.get(name, {}))
            cps[name] = blk
            changed = True
        if 'enabled' not in blk:
            blk['enabled'] = bool(main_cp[name]) if name in main_cp \
                else _default_core_plugin_enabled(name)
            changed = True
        # 补默认配置键（不覆盖 yaml 已设的值；优先迁移主 config 对应段的值）
        for k, v in _CORE_PLUGIN_SCHEMA.get(name, {}).items():
            if k not in blk:
                blk[k] = main_sec[k] if k in main_sec else v
                changed = True
    # 3. 移除已卸载插件
    for name in list(cps.keys()):
        if name not in installed:
            del cps[name]
            changed = True

    # 4. 回写 yaml（自动更新）
    if changed:
        try:
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump({'core_plugins': cps}, f,
                               allow_unicode=True, sort_keys=False)
            _get_logger().info(f"已自动同步官方插件配置: {yaml_path}")
        except Exception as e:
            _get_logger().warning(f"同步 core_plugins.yaml 失败: {e}")

    # 5. 合并进主 config：enabled → core_plugins 段；配置块 → 对应 section
    core_cfg = {}
    for name, blk in cps.items():
        if not isinstance(blk, dict):
            continue
        section = _CORE_PLUGIN_SECTION.get(name, name)
        cur = config.get(section)
        if isinstance(cur, dict):
            merged = dict(cur)
            merged.update(blk)
        else:
            merged = dict(blk)
        config[section] = merged
        core_cfg[name] = bool(blk.get('enabled', False))
    config['core_plugins'] = core_cfg
    return cps


_config = None
