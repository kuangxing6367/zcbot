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
# ZCBOT OneBot QQ机器人框架 配置文件
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
  fake_response_msg: "🎣 你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
  nonce_expiry: 60             # nonce 有效期（秒）
  blacklist_enabled: true      # 是否启用黑名单（持久化到数据库 ip_blacklist 表，重启不清除）
  whitelist_ips:               # 白名单 IP，跳过所有检查
    - "127.0.0.1"
  # success_fake_data:         # 认证通过后返回的迷惑性数据（不填则使用默认假数据）
  # 内网 IP（127.*、192.168.*、172.16-31.*、10.*）自动豁免蜜罐拉黑，避免误伤本机/局域网
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

    return config


def get_config() -> dict:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


_config = None
