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
# ZCBOT 事件驱动 IM 平台 配置文件
# 首次启动自动生成，可按需修改后重启
# ============================================================
# 凭据加密：设置环境变量 ZCBOT_TOKEN_KEY 后，access_token / app_secret / api_key
# 等凭据会被一次性加密写回本文件（enc:v1: 前缀），启动时自动解密成运行时明文。
# 未设置密钥时保持旧明文行为；${VAR} 占位符不会被加密，仍由环境变量实时替换。

# ── 界面语言 ────────────────────────────────────────────────
# 面向用户的提示文案语言（启动 / 停止等日志）
#   zh     = 简体中文（默认，正式简洁）
#   ciallo = 中文活泼版（Ciallo～(∠・ω< )⌒☆ 风格，带颜文字）
#   en     = English
# 也可用启动参数 --language / -l 或环境变量 ZCBOT_LANG 覆盖（优先级更高）
language: zh

# ── 启动配置 ────────────────────────────────────────────────
# wait_ready: true  → 等待核心/用户插件全部加载完成、路由表预热后才继续（默认）
# wait_ready: false → 异步启动：框架基础服务先就绪立即返回，插件后台加载
startup:
  wait_ready: true

# ── 数据库配置 ──────────────────────────────────────────────
# SQLite 模式（默认，零配置开箱即用）
#   ⚠️ 适用边界：仅适合小环境与开发环境（单写多读、单文件、无网络扩展）。
#      多群、高并发、多进程部署等大环境请切换 database.type: mysql。
#
# MySQL 模式（大环境推荐）：把 type 改成 mysql，并把下面几个键**平铺**在
# database 段里（与 type 同级），然后删掉不再需要的 path。
#   ⚠️ 常见写错：不要写成 database: { type: mysql, ... } 单行，
#      也不要再嵌一层 database.mysql.host —— 那样框架读到的库名/主机
#      会是一个对象，pymysql 只会报 "unsupported operand type(s) for +"。
#
#   type: mysql
#   host: 127.0.0.1
#   port: 3306
#   user: zcbot
#   password: "改成你的密码"     # 数字密码建议加引号
#   database: zcbot              # 库名，需提前建好
#   charset: utf8mb4
#   pool_size: 10                # 连接池上限
#   pool_wait_timeout: 30        # 池满时最多等多久（秒）
database:
  type: sqlite
  path: data/zcbot.db
  # ── 结构变更管控（详见 docs/进阶/数据库迁移.md） ──
  # 迁移按级别执行：additive（加表加列）开机自动跑；
  # rewrite（重建表/改类型）自动跑但先备份；destructive（删表删列删数据）
  # 必须人工执行 `python -m framework.database.migrate up --approve <变更单号>`。
  # 运行期核心表被删时默认只告警、不补建（补建会得到空表，且可能把默认
  # 管理员口令重新种回去）。确认要补时再开下面这个开关；补建不含默认口令。
  # repair_missing_tables: false
  migrations:
    auto_apply_destructive: false   # 生产环境保持 false：开机流程不能兼任批准人
    backup_before_rewrite: true     # rewrite/destructive 前自动备份（SQLite 在线快照）
    keep_backups: 2                 # 备份保留份数，避免占满磁盘
    # operator: ""                  # 记录到执行流水里的操作人；默认取系统登录名
  # ── 自动备份（与迁移前备份共用 data/backups/，靠文件名前缀区分） ──
  # 全部阈值可改。改完重启生效（WebUI「数据库备份」页也读这段配置）。
  backup:
    enabled: true
    interval_hours: 6          # 每 6 小时一次
    initial_delay_minutes: 5   # 启动后等 5 分钟再跑首次；0 = 启动不跑
    keep: 2                    # 最多保留 2 份
    compress: true             # 超过下面的阈值就打包 zip
    compress_threshold_mb: 100 # 快照大于 100MB 才压缩（不压就只是复制）
    compress_level: 1          # 1 档：压缩时尽量不抢 CPU，不卡事件处理
    refuse_above_mb: 100       # 压缩后仍超 100MB 才暂停备份，等人确认
    allow_large: false         # true = 无视上面的上限（确认库体积正常后再开）
    min_free_after_mb: 0.5     # 空间探测写到只剩 512KB 就停手并清理
    headroom_ratio: 1.10       # 预留空间 = 库体积 × 该系数
    probe_max_mb: 4096         # 探测硬上限，防止算出天文数字时写爆磁盘
    verify: true               # 写完读回复核，过了才转正
  # ── MySQL 连接保活/自动重连（type: mysql 时生效） ──
  # 数据库断开（wait_timeout/服务重启/网络中断）后自动重连，不再卡死
  # ping_interval: 5        # 连接空闲超过该秒数后自动 ping 检测存活
  # connect_timeout: 10     # 建立连接超时（秒）
  # read_timeout: 30        # 读超时（秒），避免断连后无限阻塞
  # write_timeout: 30       # 写超时（秒）
  # max_reconnect: 3        # 单次操作最大自动重连次数

# ── 事件队列配置 ────────────────────────────────────────────
# maxsize/workers/drain_timeout 见 framework/core/base.py；buffer 为消息流入分层缓冲
event_queue:
  maxsize: 2000
  workers: 4
  drain_timeout: 5
  buffer:
    l1_max_bytes: 524288
    sqlite_enabled: false
    l2_path: data/event_buffer.l2.log
    sqlite_path: data/event_buffer.db
    sqlite_write_timeout: 0.5
    sqlite_batch: 64
    l3_max_bytes: 4194304
    full_action: warn_drop

# ── OneBot WebSocket 服务端 ─────────────────────────────────
# OneBot 客户端反向连接此端口（如 NapCat、Lagrange 等）
onebot:
  listen_port: 6830
  access_token: ""           # 必须设置！留空则不校验 token，任何客户端都能接入
  # ── 多实例（可选）：一份适配器同时监听多个端口 ────────────────
  # 不配 instances 即单实例（默认，行为不变）。配置后每个实例监听独立端口/token，
  # 共享同一套事件分发；每项可覆盖上面的任意键，只需写差异部分。
  # bot_name_prefix 用于在多个实例连同一个机器人时隔离连接名，避免互相覆盖。
  # instances:
  #   - listen_port: 6830
  #     access_token: "token-of-instance-a"
  #     bot_name_prefix: "a"
  #   - listen_port: 6831
  #     access_token: "token-of-instance-b"
  #     bot_name_prefix: "b"

# ── Web UI 管理后台 ─────────────────────────────────────────
web:
  host: 127.0.0.1            # 仅本机访问；需要局域网/公网访问请改为 0.0.0.0（注意安全）
  port: 8080                 # WebUI 端口：提供前端页面 + 蜜罐诱饵
  # ui_enabled: true         # 是否启用 WebUI 前端。设为 false 即"纯 API 模式"：不启动 WebUI 端口，
  #                          仅起 API 端口（web.api_port；未设则用 web.port），本进程只提供真实 /api/*。
  # api_port: 8081           # API 独立端口（可选）。设置后 WebUI 端口不再提供任何真实 API，
  #                          只留静态文件与诱饵；真实 /api/* 全部搬到该端口。
  #                          两者同进程，黑名单 / 人机验证票据 / 会话状态天然同步。
  #                          外网部署时需在反代里把 /api/ 转发到该端口。
  # secret_key: ""          # 留空则每次重启随机生成，填入后重启保持登录态
  session_timeout: 3600      # 登录会话超时（秒），同时作为登录 token 的有效期
  official_sidebar: true     # 是否显示官方默认侧边栏菜单（关闭后仅显示插件注册项与设置）
  # 自定义左侧栏：order = 官方菜单显示顺序；hidden = 隐藏的官方项（可在「设置 → 侧边栏」调整）
  # 官方键：dashboard, marketplace, plugins, commands, users, groups, permissions,
  #         apikeys, tasks, runtime, connection, filebrowser, logs, database
  sidebar:
    order: []
    hidden: []

# ── 可观测性（健康探针 / 指标端点）────────────────────────────
# 供 Docker healthcheck / Kubernetes probe / systemd / 负载均衡 / Prometheus 使用。
# 三个端点只读、零新依赖；免登录、免限速、免人机验证（否则外部探针无法通过）。
# ── 遥测（默认关闭）────────────────────────────────────────────
# 开启后每 interval_days 天上报一次系统信息与错误日志，携带框架生成的随机唯一标识。
# 不上报管理员账号、令牌、IP。endpoint 为空时即使 enabled=true 也不会上报。
telemetry:
  enabled: false
  endpoint: ""
  interval_days: 15

observability:
  health: true                 # /healthz（存活，不查库）与 /readyz（就绪，含逐项检查明细）
  metrics: true                # /metrics（Prometheus 文本格式，仅在 API 端口暴露，避免公网 WebUI 端口泄露）
  metrics_token: ""            # 非空时 /metrics 需 Authorization: Bearer <token>（跨机抓取时设置）
  db_probe_ttl: 1.5            # 就绪探针读库结果缓存秒数，0 = 每次真读

# ── 入站可靠性（去重 + 限流，核心基础设施，非运维项）──────────────
# 生产环境机器人先挂的往往是「没限流」和「重连重放重复回复」，不是命令逻辑。
# 详见 docs/进阶/可靠性与限流.md。
reliability:
  dedup_window: 300            # 入站消息去重窗口（秒）：断线重连 / 客户端重推的同一 message_id
                              # 只处理首条，后续静默跳过；0=禁用。仅对 message 类事件生效，
                              # notice/meta/request 本身幂等，不去重。
  # 多源去重（可选，默认关）：一份适配器配多实例 / 多个 bot 收到同一条消息时是否合并。
  # ⚠ 风险：指纹是「内容 + 发送者(可关) + 群」。同一发送者在窗口内连发两条完全相同
  #   的内容，或关掉 include_sender 后不同人发相同内容，都会被静默丢弃真实消息。
  #   默认逐个处理最安全；确需开启时把 window 调小，并保留 include_sender: true。
  multisource_dedup:
    enabled: false             # 默认关：默认每条消息都独立处理
    window: 5                  # 去重窗口（秒）：窗口内内容指纹相同者只处理首条
    include_sender: true       # 指纹是否含发送者；关掉后「不同人发相同内容」也会被去重（更危险）
  rate_limit:
    enabled: false             # 默认关闭：阈值带业务属性（群规模/活跃度），由部署方按场景开启
    window: 60                 # 滑动窗口（秒）
    global_per_min: 0          # 全局每分钟上限，0=不限
    group_per_min: 0           # 单群每分钟上限，0=不限
    user_per_min: 0            # 单用户每分钟上限，0=不限
    action: skip               # skip=静默丢弃；notify=每窗口至多回复一条限流提示
    notify_text: "消息过于频繁，请稍后再试。"  # action=notify 时回复文案

# ── 出站闸门（发送节流 + 失败重试）──────────────────────────
# 入站守卫的对称面：管"发出去"这一侧。只作用于发送类动作（send_msg / send_group_msg /
# send_private_msg / send_group_forward_msg / send_like / upload_group_file 等），
# get_* 查询动作原样直通，不受节流也不重试。
#
# 节流是**等待**而非丢弃：发送是插件的明确意图，静默丢弃会让插件以为已发出。
# 等待超过 max_wait 才返回失败结构（retcode=-4, throttled=true），失败可见但不无限拖住。
#
# 重试只覆盖"确定没发出去"的失败：retcode=-3（本地通道异常）。
# -1 未连接不重试（等一会儿也不会自己好）；-2 超时**不重试**——超时不代表没送达，
# 重试只会产生重复消息，而重复消息比发送失败更难排查。
outbound:
  rate_limit:
    enabled: true              # 默认开启：阈值明显高于正常业务、低于平台惩罚线
    window: 60                 # 滑动窗口（秒）
    global_per_min: 180        # 全局每分钟上限（所有目标合计），0=不限
    group_per_min: 30          # 单群每分钟上限，0=不限
    user_per_min: 20           # 单用户私聊每分钟上限，0=不限
    max_wait: 5.0              # 单次发送最多等多久（秒），超过即放弃并返回失败
  retry:
    enabled: true
    attempts: 1                # 额外重试次数（0=不重试），之后按 backoff 翻倍退避
    backoff: 0.4               # 首次重试前等待（秒）

# ── SSL / TLS（HTTPS / WSS，可选）───────────────────────────
# 启用后：Web 管理后台走 https、OneBot 反向 WS 走 wss（两者共用同一份证书）。
# cert / key 支持绝对路径，或相对项目根目录的路径（如 certs/fullchain.pem）。
ssl:
  enabled: false
  cert: ""                   # 证书链文件（fullchain.pem / .crt）
  key: ""                    # 私钥文件（privkey.pem / .key）

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

# ── 插件配置 ────────────────────────────────────────────────
plugin:
  # 插件代码目录。留空 = 状态目录下的 plugins（推荐，即 data/plugins）。
  # 相对路径一律以**状态目录 data/ 为基准**：写 plugins 也等于 data/plugins；
  # 旧版留在仓库根的 plugins/ 会被自动兼容回退，启动迁移搬到 data/ 后自动切换。
  # 别写死成仓库根目录：往插件目录释放文件的组件（如 llm_load）每轮重启都会把
  # 根目录 plugins/ 重新建出来，布局探测永久停在「新旧并存」，迁移收不了尾。
  dir: ""
  # dat_dir: data/plugins_dat # 插件数据/配置目录（相对路径以项目根为基准，默认 data/plugins_dat）
  heartbeat_interval: 60     # 插件注册心跳间隔（秒）
  auto_install_deps_on_startup: true  # 启动时自动安装缺失依赖到全局环境（移机自愈，版本冲突自动跳过）
  max_memory_mb: 64          # 单插件内存上限（MB），超限自动卸载
  hot_reload: true           # 用户插件（plugins/）源码变更时自动全量热重载（unload+reload），改动的 handler 逻辑即时生效；false 则沿用旧行为（仅重新注册，不重跑模块代码）。仅作用于用户插件，核心插件不参与

# ── 插件源码静态检查（启动期扫描同步阻塞 / 同步等待 / 同步退出）──
# 启动时先用 AST 把插件目录整体扫一遍，命中写进日志后照常启动；
# 只读源码、不执行插件代码，也不改动任何文件。
plugins:
  source_scan:
    enabled: true                    # 启动时扫描插件目录（默认开）
    mode: warn                       # warn=命中只告警并继续启动（默认）；block=命中即拒绝加载该插件
    scan_core_plugins: true          # 一并扫描 core_plugins/（官方插件同样跑在核心进程内）
    max_report: 50                   # 单次启动最多逐条打印多少条命中，其余汇总一行
    allowlist: []                    # 信任的插件名（跳过静态检查）
    block_on_import_side_effects: true # 仅 mode=block 生效：模块加载期的退出/阻塞拒绝加载
    block_on_blocking_handler: true  # 仅 mode=block 生效：async handler 里的同步阻塞同样拒绝

# ── 依赖自检（启动自愈）──────────────────────────────────
# 启动自检读取 requirements.txt，自动安装缺失的核心依赖（移机自愈）。
# allow_missing：列出"允许缺失、无需安装也不报错"的包名（小写，连字符/下划线等价）。
#   这些包即便缺失，也只记一条 WARNING，不强制安装、不打断启动、不退出。
#   适用：确定不用的可选依赖（如纯 Web 部署不装 Pillow / 不接 IM 平台不装对应驱动包）。
#   注意：被注释在 requirements.txt 中的可选依赖本就不会被自检强制，无需在此重复列出。
dependencies:
  allow_missing: []

# ── 日志配置 ────────────────────────────────────────────────
log:
  level: INFO                # DEBUG / INFO / WARNING / ERROR
  format: text               # 输出格式：text（给人看）/ json（JSON Lines，便于 Loki、ELK 等采集）
  file: data/logs/zcbot.log  # 日志文件路径（统一存放于 data/logs/），留空则只输出控制台
  log_raw_message: true      # 是否记录收到的原始消息内容
  log_sent_message: true     # 是否记录发送到 OneBot11 的消息内容
  coalesce: true              # 高并发下重复错误自动合并（防刷屏）
  coalesce_window: 10         # 合并滑动窗口（秒），窗口内重复只记首条
  coalesce_level: WARNING     # 参与合并的最低级别（INFO/DEBUG 不合并）

# ── GitHub 加速 ─────────────────────────────────────────────
# 插件市场 / 插件下载 / 框架更新的 GitHub 加速代理地址
# 留空则使用内置 ghproxy 镜像回退；国内网络直连 GitHub 慢/失败时填写，如 https://ghproxy.net
github_proxy: ""

# ── 系统配置 ────────────────────────────────────────────────
system:
  show_cpu: true             # 仪表盘显示 CPU 使用率
  show_disk: true            # 仪表盘显示磁盘使用率
  status_interval: 30        # 系统状态刷新间隔（秒）

# ── 内存策略 ────────────────────────────────────────────────
# allocator: auto/malloc/pymalloc；limit_mb 超触发看门狗清理；check_interval 检查间隔
memory:
  allocator: auto
  limit_mb: 120
  check_interval: 30

# ── 安全配置 ────────────────────────────────────────────────
# 由「安全等级」一档统管：蜜罐诱饵、tarpit 延迟、自动拉黑、安全响应头、指纹隐藏、
# JS 人机验证、认证静默丢弃。全部默认值由 level 决定，无需逐项调参；
# 确有需要再对单项显式覆盖（显式值优先）。
#
# 档位（security.level）：
#   off       关闭所有主动防御（仅保留登录限速），适合本地开发
#   low       宽松：核心诱饵 + 安全响应头 + 人机验证，不自动拉黑
#   medium    标准（默认，推荐）：完整诱饵集 + 拉黑 1 小时 + 认证静默 + 人机验证
#   high      严格：延迟 1.5s、拉黑 24 小时、认证失败 10 次即封
#   paranoid  偏执：延迟 3s、永久封禁、认证失败 5 次即封
#
# ⚠️ 蜜罐所有诱饵返回的密钥/凭据/Token 均为随机伪造，不具备任何真实访问能力。
# ⚠️ 内网 IP（127.*、192.168.*、172.16-31.*、10.*）与 whitelist_ips 自动豁免拉黑。
security:
  level: medium                # 安全等级总开关：off / low / medium / high / paranoid
  silent_auth: true            # 认证不对就静默丢弃（不返回 401 JSON，扫描器拿不到反馈）

  # ── 双请求防破解认证（与等级无关的独立参数） ──
  fake_token_len: 8            # 探针请求的 Token 长度（任意字符串）
  real_token_len: 8192         # 真实认证的 Token 长度
  nonce_len: 16                # 下发 nonce 的长度
  fake_response_msg: "你上钩了！但这里只是蜜罐，请去 GitHub 点个 Star。"
  nonce_expiry: 60             # nonce 有效期（秒）
  blacklist_enabled: true      # 是否启用黑名单（持久化到数据库 ip_blacklist 表，重启不清除）
  whitelist_ips:               # 白名单 IP，跳过所有检查
    - "127.0.0.1"
  trusted_proxies: []          # 受信反向代理 IP：仅这些 IP 的 X-Forwarded-For 才被采信
  # success_fake_data:         # 认证通过后返回的迷惑性数据（不填则使用默认假数据）

  # ── 单项覆盖（可选；不填则完全由 security.level 决定）──
  # headers:
  #   enabled: true            # 是否注入安全响应头（CSP / X-Frame-Options 等）
  #   server: ""               # 覆盖 Server 头；留空 = 隐藏真实服务器指纹
  #   csp: ""                  # 自定义 Content-Security-Policy；留空用内置策略
  # human_check:               # JS 人机验证（工作量证明，必须真正执行 JS 才能通过）
  #   enabled: true
  #   difficulty: 4            # sha256 前导 0 的十六进制位数；每 +1 成本 ×16（4≈0.3s，5≈8s）
  #   apply_to: all            # 校验范围：all（所有 /api）/ login（仅登录）/ none
  #   bypass_whitelist: false  # 白名单 IP 是否免检
  #   ticket_ttl: 1800         # 通行票据有效期（秒）
  #   challenge_ttl: 120       # challenge 有效期（秒）
  # honeypot:
  #   enabled: true            # 蜜罐总开关
  #   decoy_set: full          # 诱饵集：off / basic（核心 45 个）/ full（全量 125 个）
  #   tarpit_ms: 600           # 命中诱饵后的随机延迟上限（毫秒，0=不延迟）
  #   blacklist: true          # 命中是否自动拉黑来源 IP
  #   blacklist_ttl: 3600      # 强证据封禁秒数（0=永久封禁）
  #   soft_ban_ttl: 300        # 弱证据软封禁秒数，登录成功即解
  #   log_body: true           # 是否记录请求体（便于取证）
  #   max_body_log: 2048       # 请求体记录上限（字节）
  #   unknown_api_decoy: true  # 未知 /api 路径返回诱饵（false=普通 404）
  #   auth_fail_blacklist: 40  # 同一 IP 认证连续失败多少次后软封禁（0=不拉黑）
  #   auth_fail_window: 300    # 认证失败统计窗口（秒）
  #   tarpit_auth_ms: 800      # 认证失败时的随机延迟上限（毫秒）
  #   invalid_token_decoy: false # true=无效 token 返回诱饵 200（会掩盖静默丢弃，慎用）
  #   canary: true             # 在诱饵响应中植入唯一追踪串，便于事后追溯泄露路径
  #   evasion_detect: true     # 检测路径变形绕过（%2e 编码 / 重复斜杠 / 穿越），加重处置
  #   fake_404: true           # 未知页面返回伪装 nginx 的假 404（false=普通 JSON 404）
  #   entry_paths: []          # 自定义安全入口（陷阱路径），默认为空；命中按蜜罐处置

# ── 定时任务（scheduler 段）──────────────────────────────────
# 影响 ctx.task 注册的 cron 任务、内置任务，以及插件定时逻辑的执行方式。
scheduler:
  # 时区：local（系统本地时区，默认）/ utc / IANA 名（Asia/Shanghai）
  # 容器镜像的 TZ 通常是 UTC，写 `0 9 * * *` 的任务会在北京时间 17:00 才执行，
  # 而日志里看不出来。部署在中国大陆建议显式写 Asia/Shanghai。
  # 注意：Windows 上 ZoneInfo 需要 tzdata 包（pip install tzdata），缺失会自动退回 local。
  timezone: local
  # 单任务执行超时（秒），0 = 不限。
  # 超时只是「不再等待」，Python 无法强杀线程：任务线程仍会在后台跑完。
  # 要可中断必须写 async handler 并在内部支持取消。
  task_timeout: 300
  # 慢任务告警阈值（秒），0 = 不告警。执行耗时超阈值会打 warning 并记入任务记录。
  slow_task_threshold: 10
  # 同步 handler 专用线程池大小（与命令 handler 用的默认线程池隔离，互不挤占）
  workers: 4
  # 优雅停机：stop/卸载时等待在途任务完成的最长秒数（0=立即丢弃在途任务）。
  # 缺省不写=min(单任务超时, 30s)。超时后放弃等待，在途线程仍在后台跑完（不阻塞退出）。
  # stop_drain_timeout: 30

# ── 框架基础约定与限制（framework 段） ────────────────────────
# 集中管理此前散落或写死的基础约定，统一在此可调；WebUI 也通过本段编辑。
framework:
  startup_info: true          # 启动时打印运行环境（Python/虚拟环境/容器/特权身份）
  security:
    warn_if_root: true        # 以 root / 管理员身份运行时告警
  config:
    on_error: abort           # abort（默认，拒绝启动）/ default（显式授权降级到内置默认配置）
    strict_unknown_keys: true # 安全段的未知键是否中止启动

  # ── 状态目录自动迁移（默认开启）──
  # 启动期探测当前目录是哪一代版本留下的布局；若是旧版根目录布局，自动把
  # config.yaml / core_plugins.yaml / plugins/ 收敛到 data/。
  # 约束：绝不覆盖 data/ 下已有文件，绝不改写配置内容，失败不阻断启动。
  # 想自己管目录就改成 false —— 框架仍兼容读取旧布局，只是不再自动搬。
  # 临时覆盖（CI / 排障）：环境变量 ZCBOT_AUTO_MIGRATE=0，无需改配置文件。
  # 手动体检 / 预演：python -m framework.migration --inspect / --dry-run
  auto_migrate: true

  # ── 操作审计（audit_logs 表）──
  # 回答「谁、在什么时候、对什么、做了什么、结果如何」，供追责与合规查；
  # 与运行日志分开：审计必须落库、可查询、不随日志轮转丢失。
  # Web 管理面操作（登录/改配置/加管理员/封 IP）一直有记录，这里补的是消息侧。
  # 写库是旁路：record() 只进内存队列，后台批量 INSERT，队列满直接丢弃并计数，
  # 写库失败只告警，绝不拖慢消息路由。
  audit:
    enabled: true             # 总开关
    command_events: true      # 命令执行 / 权限拒绝 / 黑名单拦截
    unmatched_events: false   # 未命中任何命令或关键词的消息（噪音大，查「为什么不回」时再开）
    record_message_text: true # 消息正文是否写进 detail（统一截断 200 字；审计表不是聊天记录表）
    flush_interval: 5.0       # 批量落库间隔（秒）
    max_queue: 5000           # 内存队列上限，满了丢弃并计数

  # ── 事件循环卡顿看门狗（诊断）──
  # 阻塞调用会让整个机器人停摆（所有插件 + 所有接入端 + 定时任务），且日志里
  # 通常什么都没有。看门狗周期性测量循环延迟，卡顿超过 threshold 时打 warning
  # 并抓取循环线程的栈顶帧，直接指出卡在哪一行。
  loop_watchdog:
    enabled: true               # 默认开；常驻一个守护线程，开销可忽略
    interval: 0.25              # 采样间隔（秒）
    threshold: 0.5              # 延迟超过该值（秒）判定为阻塞
    stall_wait: 3               # 回调多久没执行判定为循环完全卡死（秒）

  # ── 内存黑匣子（调试排障）──
  # 纯内存环形缓冲、无磁盘落库，追踪事件流转/路由命中/插件加载生命周期。
  # 极低开销，默认开启，随时待命；发生诡异 Bug/死锁时可经 WebUI/API 导出最近现场。
  blackbox:
    enabled: true               # 是否开启（默认开，开销极小）
    # 内存上限（字节），满后自动丢弃最旧帧。
    # 富追踪（事件流逐阶段 + 订阅者名单 + 发送目标 + 加载期 ctx 调用）需要更大预算，
    # 默认 256KB；若只想要粗粒度现场可调小，想要更长回溯可调大。
    max_bytes: 262144           # 默认 256KB

  # ── 本地控制通道（外部进程查看黑匣子）──
  # 只监听 127.0.0.1；端口与每次启动重新生成的令牌写入 <state_dir>/control.json。
  # 令牌不写入本文件。修改本段后需重启框架。
  control:
    enabled: true
    port: 0            # 0 = 随机端口，实际端口写入 <state_dir>/control.json
    allow_clear: false # true 时才允许 blackbox.clear，且请求仍需 confirm: true

  # ── 崩溃重启守卫（Crash-loop Guard）──
  # 解决“DB 损坏/启动致命异常 → systemd/pm2 无限重启 → 永远起不来”的死循环：
  # 崩溃时写 error.log（自动轮转 .1..N）；累计 ≥ max_restarts 次直接拒绝启动并给出处置指引。
  # 启动成功后自动清零计数（健康运行不误杀）。
  crash:
    enabled: true               # 是否启用守卫
    max_restarts: 5             # 累计崩溃多少次后拒绝启动（打破无限重启）
    path: error.log             # 崩溃日志路径（相对项目根目录，或绝对路径）
    # alert_command: ""         # 崩溃时执行的本地命令（如调用邮件脚本）；留空不触发
    # alert_webhook: ""         # 崩溃时 POST 文本到该 URL（如企业微信/钉钉机器人）；留空不触发

  # ── 基础约定（conventions）──
  conventions:
    command_prefix: "/"         # 命令前缀约定（命令匹配以该字符开头，如 /echo）
    event_workers: 0            # 事件并行 worker 数（0=自动=CPU×2；>0 显式指定；覆盖 event_queue.workers）

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
  event_batch_max: 64       # 核心→宿主事件攒批条数上限（<=1 关闭批量，逐条发送）
  event_batch_delay_ms: 5   # 攒批时间窗口（毫秒）；满批或到窗口即发送

# ── 调试控制台（无 TTY 也能接进运行中的框架） ──────────────────
# 启动时在 127.0.0.1 上监听一个本地端口，端口与超长随机 token 自动生成并
# 存入数据库（console_access 表），无需重启、无需 TTY（systemd 托管场景）。
# 本地执行 `python startup.py attach` 即可接入，直接使用框架的终端命令
# (help / status / plugins / reload / ...).
console:
  enabled: true              # 是否启用本地调试控制台
  host: 127.0.0.1            # 仅回环地址；不要改成 0.0.0.0（等于把控制台暴露到网络）
  port: 0                    # 0 = 首次启动自动分配空闲端口并落库；之后复用该端口
  token_bits: 16384          # token 位数（默认 16384 bit = 2048 字节；越长越安全）
  backlog: 8                 # 监听队列长度
  read_timeout: 3600         # 单条会话空闲超时（秒），超时自动断开

# ── 终端交互（前台运行时） ────────────────────────────────────
# 有 TTY 时的交互终端；无 TTY（systemd / 重定向）会自动跳过，改用上面 console 段接入。
terminal:
  enabled: true              # 是否启用交互终端（stdin 非 TTY 时自动跳过）
  panel_autostart: false     # 启动后自动进入运维面板（退出后回到普通终端）
  panel_refresh: 1.0         # 面板刷新间隔（秒，最小 0.2）
  panel_default_view: monitor # 面板默认页：monitor / overview / plugins / messages / system

# ── 多平台接入 ────────────────────────────────────────────────
# 框架原生只认「单一平台视图」：所有适配器（OneBot / QQ官方 / Telegram / Discord …）
# 共享同一套 user/group 身份，且所有插件处理全部适配器的消息。
# 多平台接入让框架区分不同适配器，并提供三类能力（详见 docs/进阶/多平台接入.md）：
#   1. drive 注册表：每个适配器首次加载自动登记一个稳定 uuid（重启不变）；
#   2. 身份命名空间（下方 namespace 开关）：打开后 user/group 身份按
#      `<适配器uuid>@<id>` 隔离，不同适配器下同一 QQ 号视为不同用户/群；
#   3. 插件↔适配器双向绑定：配置适配器段的 plugins 白名单，或在插件
#      __plugin_meta__ 写 adapters 列表，让「指定插件只处理指定适配器」而非全收。
multi_platform:
  # 身份命名空间开关（默认 false）：
  #   false = user/group 用裸号，跨适配器共享同一身份（旧行为，零风险）
  #   true  = 身份键 = <适配器uuid>@<id>，不同适配器下同一 QQ 号成为不同行，
  #           各适配器的用户统计/黑名单/身份彼此隔离（适合多群多平台分别运营）
  namespace: false
  # 适配器白名单示例（按需手写到对应适配器配置段，如 core_plugins.yaml 的
  # qq_official / onebot 块，或主 config 的 qq_official / onebot 段）：
  #   qq_official:
  #     plugins:                 # 该适配器只允许下列插件处理消息；留空/不写=全部插件
  #       - hello
  #       - group_guardian
  # 反向（插件侧声明自己只服务哪些适配器）写在插件 main.py：
  #   __plugin_meta__ = {..., 'adapters': ['qq_official', 'onebot']}
  # 两者「且」关系：适配器白名单放行 且 插件 adapters 含当前适配器，才允许处理。
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


def get_code_root() -> str:
    """代码根目录：统一委托 framework.paths，避免与 __file__ 上溯口径分叉。"""
    from framework.paths import code_root
    return code_root()


def _in_site_packages(path: str) -> bool:
    """向后兼容保留；判定逻辑已迁到 framework.paths。"""
    from framework.paths import _in_site_packages as _impl
    return _impl(path)


def get_home() -> str:
    """可写状态根目录：统一委托 framework.paths.home_dir()。

    解析顺序仍是 ZCBOT_HOME > ~/.zcbot（pip 安装态）> 代码根（源码运行），
    但只保留一处实现，且由 ``resolve_config_path()`` 决定配置/插件是否落在
    ``<home>/data/``（新布局）还是 ``<home>/`` 根（旧布局兼容）。
    """
    from framework.paths import home_dir
    return home_dir()


def _generate_default_config(config_path: str):
    """生成默认配置文件"""
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.isdir(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(_DEFAULT_CONFIG)
    _get_logger().info(f"已生成默认配置文件: {config_path}，请按需修改后重启")


def fill_missing_sections(config: dict) -> list:
    """把默认配置里缺失的顶层 section 补进内存配置，返回补了哪些段。

    为什么需要：升级到新版本后，用户手里的 config.yaml 是旧的，新版本引入的
    配置段（observability / reliability / database.migrations …）在里面根本不存在。
    不补的话，新能力对存量部署等于不可见、不可调，只能靠翻文档猜。

    两条红线：
    - 只补**顶层 section**，已存在的段一律不碰（用户改过的值优先级最高）；
    - **不改写用户文件**——YAML 注释会被 dump 掉，替用户重写配置文件不可接受。
      想让改动落盘，由用户在面板/文件里自己补，日志会告知补了哪些段。
    """
    try:
        defaults = yaml.safe_load(_DEFAULT_CONFIG) or {}
    except Exception as e:
        _get_logger().warning(f"解析默认配置模板失败，跳过缺段补齐: {e}")
        return []
    if not isinstance(defaults, dict) or not isinstance(config, dict):
        return []
    added = []
    for key, val in defaults.items():
        if key not in config:
            config[key] = val
            added.append(key)
    return added


# ── 配置 schema 校验（Gap 1：缺段已补，但值与类型从不校验） ──
# 手写轻量 schema，不引入 pydantic 重依赖；只告警、绝不阻断启动
# （与 fill_missing_sections 的"补齐+不报错"哲学一致）。load_config 在
# 缺段补齐之后调用，返回问题清单供日志/测试断言。
_CONFIG_SCHEMA = {
    'database': {
        'type': dict,
        'keys': {
            # debug = 进程内 SQL 模拟引擎（framework/database/migrate.py、
            # storage.py、init_db._init_debug 都有对应分支），是合法取值。
            'type': {'allowed': ('sqlite', 'mysql', 'debug')},
            # SQLite 路径与 MySQL 连接参数都是运行时真实读取的键；
            # 之前只声明了 type/backup，导致合法配置被误报成未知键。
            'path': {'str': True},
            'host': {'str': True},
            'port': {'int': True, 'min': 1, 'max': 65535},
            'user': {'str': True},
            'password': {'str': True},
            'database': {'str': True},
            'name': {'str': True},
            'charset': {'str': True},
            'pool_size': {'int': True, 'min': 1},
            'pool_wait_timeout': {'float': True, 'min': 0},
            'ping_interval': {'float': True, 'min': 0},
            'connect_timeout': {'float': True, 'min': 0},
            'read_timeout': {'float': True, 'min': 0},
            'write_timeout': {'float': True, 'min': 0},
            'max_reconnect': {'int': True, 'min': 0},
            'repair_missing_tables': {'bool': True},
            'migrations': {
                'type': dict,
                'keys': {
                    'auto_apply_destructive': {'bool': True},
                    'backup_before_rewrite': {'bool': True},
                    'keep_backups': {'int': True, 'min': 0},
                    'operator': {'str': True},
                },
            },
            # 自动备份：全部阈值可改。类型写错时这里就拦下，
            # 而不是等到半夜备份线程跑到一半才发现 interval 是个字符串。
            'backup': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'interval_hours': {'float': True, 'min': 0.05, 'max': 720},
                    'initial_delay_minutes': {'float': True, 'min': 0},
                    'keep': {'int': True, 'min': 1, 'max': 100},
                    'compress': {'bool': True},
                    'compress_threshold_mb': {'float': True, 'min': 0},
                    'compress_level': {'int': True, 'min': 0, 'max': 9},
                    'refuse_above_mb': {'float': True, 'min': 0},
                    'allow_large': {'bool': True},
                    'min_free_after_mb': {'float': True, 'min': 0.05},
                    'headroom_ratio': {'float': True, 'min': 1.0, 'max': 10.0},
                    'probe_chunk_kb': {'float': True, 'min': 64},
                    'probe_max_mb': {'float': True, 'min': 1},
                    'verify': {'bool': True},
                    'verify_sqlite': {'bool': True},
                },
            },
        },
    },
    'event_queue': {
        'type': dict,
        'keys': {
            'maxsize': {'int': True, 'min': 1},
            'workers': {'int': True, 'min': 1},
            'drain_timeout': {'int': True, 'min': 0},
        },
    },
    'onebot': {
        'type': dict,
        'keys': {
            'listen_port': {'int': True, 'min': 1, 'max': 65535},
        },
    },
    'web': {
        'type': dict,
        'keys': {
            'host': {'str': True},
            'port': {'int': True, 'min': 1, 'max': 65535},
            'api_port': {'int': True, 'min': 1, 'max': 65535},
            'ui_enabled': {'bool': True},
            'secret_key': {'str': True},
            'session_timeout': {'int': True, 'min': 30},
            'token_timeout': {'int': True, 'min': 30},
            'dashboard': {
                'type': dict,
                'keys': {
                    'layout': {'str': True},
                    'order': {'any': True},
                },
            },
        },
    },
    'telemetry': {
        'type': dict,
        'keys': {
            'enabled': {'bool': True},
            'endpoint': {'str': True},
            'interval_days': {'int': True, 'min': 1, 'max': 365},
        },
    },
    'observability': {
        'type': dict,
        'keys': {
            'health': {'bool': True},
            'metrics': {'bool': True},
            'db_probe_ttl': {'float': True, 'min': 0, 'max': 60},
        },
    },
    # WebUI 插件段（core_plugins.yaml 管启停，这里只声明已知的配置键）。
    # 声明 security 是为了让 webui.securty 这类拼写错误能被点名提示。
    'webui': {
        'type': dict,
        'keys': {
            'security': {'bool': True},
            'theme': {'str': True},
            'max_messages': {'int': True, 'min': 1},
        },
    },
    # 独立管理员来源（文件 + DB 双来源）保留段；为空也不影响启动。
    'auth': {'type': dict, 'keys': {}},
    'reliability': {
        'type': dict,
        'keys': {
            'dedup_window': {'int': True, 'min': 0},
            'multisource_dedup': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'window': {'int': True, 'min': 0},
                    'include_sender': {'bool': True},
                },
            },
            'rate_limit': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'global_per_min': {'int': True, 'min': 0},
                    'group_per_min': {'int': True, 'min': 0},
                    'user_per_min': {'int': True, 'min': 0},
                },
            },
        },
    },
    'outbound': {
        'type': dict,
        'keys': {
            'rate_limit': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'global_per_min': {'int': True, 'min': 0},
                    'group_per_min': {'int': True, 'min': 0},
                    'user_per_min': {'int': True, 'min': 0},
                },
            },
        },
    },
    'security': {
        'type': dict,
        'keys': {
            'level': {'allowed': ('off', 'low', 'medium', 'high', 'paranoid')},
            'silent_auth': {'bool': True},
            # 与等级无关的独立参数
            'fake_token_len': {'int': True, 'min': 1},
            'real_token_len': {'int': True, 'min': 1},
            'nonce_len': {'int': True, 'min': 1},
            'fake_response_msg': {'str': True},
            'nonce_expiry': {'int': True, 'min': 0},
            'blacklist_enabled': {'bool': True},
            'whitelist_ips': {'any': True},
            'trusted_proxies': {'any': True},
            'success_fake_data': {'any': True},
            # 单项覆盖（可选；不填则完全由 security.level 决定）
            'headers': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'server': {'str': True},
                    'csp': {'str': True},
                },
            },
            'human_check': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'difficulty': {'int': True, 'min': 0, 'max': 16},
                    'apply_to': {'allowed': ('all', 'login', 'none')},
                    'bypass_whitelist': {'bool': True},
                    'ticket_ttl': {'int': True, 'min': 0},
                    'challenge_ttl': {'int': True, 'min': 0},
                },
            },
            'honeypot': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'decoy_set': {'allowed': ('off', 'basic', 'full')},
                    'tarpit_ms': {'int': True, 'min': 0},
                    'blacklist': {'bool': True},
                    'blacklist_ttl': {'int': True, 'min': 0},
                    # 软封禁时长：弱证据（旧缓存/未知路径/认证失败）用短 TTL，登录成功即解
                    'soft_ban_ttl': {'int': True, 'min': 0},
                    'log_body': {'bool': True},
                    'max_body_log': {'int': True, 'min': 0},
                    'unknown_api_decoy': {'bool': True},
                    # 弱证据拉黑阈值：honeypot.py 与 security_levels.py 真实读取。
                    'ban_internal_ips': {'bool': True},
                    'ban_threshold': {'int': True, 'min': 1},
                    'ban_window': {'int': True, 'min': 1},
                    'auth_fail_blacklist': {'int': True, 'min': 0},
                    'auth_fail_window': {'int': True, 'min': 0},
                    'tarpit_auth_ms': {'int': True, 'min': 0},
                    'invalid_token_decoy': {'bool': True},
                    'canary': {'bool': True},
                    'evasion_detect': {'bool': True},
                    'fake_404': {'bool': True},
                },
            },
        },
    },
    'log': {
        'type': dict,
        'keys': {
            'level': {'allowed': ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')},
            'format': {'allowed': ('text', 'json')},
            'coalesce': {'bool': True},
        },
    },
    'scheduler': {
        'type': dict,
        'keys': {
            'timezone': {'str': True},
            'task_timeout': {'int': True, 'min': 0},
            'slow_task_threshold': {'int': True, 'min': 0},
            'workers': {'int': True, 'min': 1},
            'stop_drain_timeout': {'int': True, 'min': 0},
        },
    },
    'plugin': {
        'type': dict,
        'keys': {
            'max_memory_mb': {'int': True, 'min': 1},
            'hot_reload': {'bool': True},
            'auto_install_deps_on_startup': {'bool': True},
        },
    },
    'plugins': {
        'type': dict,
        'keys': {
            'source_scan': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'mode': {'str': True, 'allowed': ['warn', 'block']},
                    'scan_core_plugins': {'bool': True},
                    'max_report': {'int': True, 'min': 0},
                    'block_on_import_side_effects': {'bool': True},
                    'block_on_blocking_handler': {'bool': True},
                    'allowlist': {'any': True},
                },
            },
        },
    },
    'memory': {
        'type': dict,
        'keys': {
            'limit_mb': {'int': True, 'min': 1},
        },
    },
    'framework': {
        'type': dict,
        'keys': {
            'startup_info': {'bool': True},
            'security': {
                'type': dict,
                'keys': {
                    'warn_if_root': {'bool': True},
                },
            },
            'config': {
                'type': dict,
                'keys': {
                    'on_error': {'allowed': ('abort', 'default')},
                    'strict_unknown_keys': {'bool': True},
                },
            },
            'conventions': {
                'type': dict,
                'keys': {
                    'command_prefix': {'str': True},
                },
            },
            'control': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'port': {'int': True, 'min': 0, 'max': 65535},
                    'allow_clear': {'bool': True},
                },
            },
            'crash': {
                'type': dict,
                'keys': {
                    'enabled': {'bool': True},
                    'safe_mode': {'bool': True},
                    'max_restarts': {'int': True, 'min': 1},
                    'path': {'str': True},
                },
            },
        },
    },
}

# 默认模板里还有大量「合法但没有强类型约束」的段/键（ssl、http_api、terminal…）。
# 双向未知键检测必须认识它们，否则一份原封不动的默认配置会被自己报成未知配置。
# 这里从模板自动补齐「已知键」名单，已有显式 schema 的项保持原样（不降级校验强度）。
_KNOWN_TOP_LEVEL = set(_CONFIG_SCHEMA)


def _merge_declared_keys(defaults: dict, keys: dict) -> None:
    for key, val in defaults.items():
        if not isinstance(key, str):
            continue
        if key not in keys:
            if isinstance(val, dict):
                keys[key] = {'type': dict, 'keys': {}}
                _merge_declared_keys(val, keys[key]['keys'])
            else:
                keys[key] = {'any': True}
            continue
        entry = keys[key]
        if isinstance(val, dict) and isinstance(entry, dict) and 'type' in entry:
            _merge_declared_keys(val, entry.setdefault('keys', {}))


def _merge_declared_defaults():
    try:
        defaults = yaml.safe_load(_DEFAULT_CONFIG) or {}
    except yaml.YAMLError:
        return
    if not isinstance(defaults, dict):
        return
    for sec, val in defaults.items():
        _KNOWN_TOP_LEVEL.add(sec)
        if not isinstance(val, dict):
            continue
        entry = _CONFIG_SCHEMA.setdefault(sec, {'type': dict, 'keys': {}})
        _merge_declared_keys(val, entry.setdefault('keys', {}))


_merge_declared_defaults()


def _coerce_int(v):
    """宽松整数判定：接受 int / 可解析为 int 的字符串（YAML 常写成字符串）。
    bool 不视为 int 语义；浮点须为整数值。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _coerce_float(v):
    """宽松浮点判定：接受 int / float / 可解析为数字的字符串。

    与 ``_coerce_int`` 分开是为了让 ``interval_hours: 6.5`` 这种写法能过——
    「每 6 小时」被写死成整数的话，想调成 1.5 小时就得改代码。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _validate_keys(path: str, node: dict, spec: dict, issues: list) -> None:
    for key, kspec in spec.items():
        full = f"{path}.{key}"
        if 'type' in kspec:  # 嵌套映射：递归
            child = node.get(key)
            if child is None:
                continue
            if not isinstance(child, dict):
                issues.append(f"配置项 [{full}] 应是映射(dict)，实际为 {type(child).__name__}")
                continue
            _validate_keys(full, child, kspec.get('keys', {}), issues)
            continue
        v = node.get(key)
        if v is None:
            continue
        if kspec.get('bool'):
            ok = isinstance(v, bool) or (
                isinstance(v, str)
                and v.strip().lower() in ('1', '0', 'true', 'false', 'yes',
                                          'no', 'on', 'off', 'y', 'n'))
            if not ok:
                issues.append(f"配置项 [{full}] 应是布尔值/布尔字符串，实际为 {type(v).__name__}：{v!r}")
            continue
        if kspec.get('str'):
            if not isinstance(v, str):
                issues.append(f"配置项 [{full}] 应是字符串，实际为 {type(v).__name__}：{v!r}")
            continue
        if kspec.get('int'):
            iv = _coerce_int(v)
            if iv is None:
                issues.append(f"配置项 [{full}] 应是整数（或整数字符串），实际为 {type(v).__name__}：{v!r}")
            elif 'min' in kspec and iv < kspec['min']:
                issues.append(f"配置项 [{full}]={iv} 小于下限 {kspec['min']}")
            elif 'max' in kspec and iv > kspec['max']:
                issues.append(f"配置项 [{full}]={iv} 大于上限 {kspec['max']}")
            continue
        if kspec.get('float'):
            fv = _coerce_float(v)
            if fv is None:
                issues.append(f"配置项 [{full}] 应是数字（或数字字符串），实际为 {type(v).__name__}：{v!r}")
            elif 'min' in kspec and fv < kspec['min']:
                issues.append(f"配置项 [{full}]={fv} 小于下限 {kspec['min']}")
            elif 'max' in kspec and fv > kspec['max']:
                issues.append(f"配置项 [{full}]={fv} 大于上限 {kspec['max']}")
            continue
        if 'allowed' in kspec:
            if v not in kspec['allowed']:
                issues.append(f"配置项 [{full}]={v!r} 不在允许集合 {kspec['allowed']} 内")
            continue


class ConfigLoadError(Exception):
    """配置文件语法错误：不可静默降级，调用方必须拒绝启动。"""

    def __init__(self, path, line, column, snippet, cause):
        self.path = os.path.abspath(str(path))
        self.line = line
        self.column = column
        self.snippet = snippet or ''
        self.cause = cause
        self.copy_path = ''
        super().__init__(
            f'配置文件语法错误: {self.path} 第 {self.line} 行第 {self.column} 列: {self.cause}')


class ConfigValidationError(Exception):
    """安全段未知键等致命校验问题：与语法错误一样拒绝启动（退出码 2）。"""

    def __init__(self, path, issues):
        self.path = os.path.abspath(str(path))
        self.issues = list(issues)
        detail = '；'.join(str(i) for i in self.issues[:5])
        super().__init__(
            f'配置校验未通过: {self.path}（{len(self.issues)} 处问题）'
            + (f'：{detail}' if detail else ''))


class ConfigIssue(str):
    """问题清单条目：保持 str 语义，额外携带日志级别供启动横幅分流。"""

    def __new__(cls, text, level='warning'):
        obj = super().__new__(cls, text)
        obj.level = level
        return obj


def _make_broken_copy(path: str) -> str:
    """把坏配置原文复制到 <path>.broken.<时间戳>，原文件只读。"""
    import shutil
    import time

    stamp = time.strftime('%Y%m%d-%H%M%S')
    copy_path = f'{path}.broken.{stamp}'
    suffix = 1
    while os.path.exists(copy_path):
        copy_path = f'{path}.broken.{stamp}.{suffix}'
        suffix += 1
    try:
        shutil.copyfile(path, copy_path)
    except OSError as e:
        return f'副本创建失败: {e}'
    return copy_path


def _error_context(snippet: str, line) -> list:
    """出错行前后各 2 行原文，问题行用 > 标记。"""
    lines = (snippet or '').splitlines()
    if isinstance(line, int) and 1 <= line <= len(lines):
        start, end = max(1, line - 2), min(len(lines), line + 2)
    else:
        start, end = 1, min(len(lines), 5)
    out = []
    for no in range(start, end + 1):
        out.append(f"    {'>' if no == line else ' '} {no:>4} | {lines[no - 1]}")
    return out or ['    （原文为空）']


def format_config_error(exc: ConfigLoadError) -> str:
    """结构化错误块：print 到 stderr 与 logger.critical 必须用同一份内容。"""
    return '\n'.join([
        '=' * 72,
        '[配置文件语法错误] 框架未启动，请修复后重启',
        f'  配置文件: {exc.path}',
        f'  错误位置: 第 {exc.line} 行，第 {exc.column} 列',
        f'  YAML 报错: {exc.cause}',
        f'  原文副本: {os.path.abspath(exc.copy_path) if exc.copy_path else "未创建（只读检查）"}',
        '  出错位置前后 2 行原文（> 标出问题行）:',
        *_error_context(exc.snippet, exc.line),
        '  原配置文件未被修改；修复后可用 python -m framework.config --check <path> 复核。',
        '=' * 72,
    ])


def _peek_on_error(raw: str) -> str:
    """坏配置无法整体解析，只做最小限度的显式降级开关探测。

    只认 framework.config.on_error: default；匹配不到一律 abort。
    """
    import re

    match = re.search(r'(?m)^\s*on_error:\s*([A-Za-z_][\w-]*)\s*(?:#.*)?$', raw or '')
    return (match.group(1).strip().lower() if match else '') or 'abort'


def _best_effort_crash_config(path: str = None, root: str = None) -> dict:
    """配置本身读不出来时，单独容错取出 crash 段；取不到就用默认值。

    崩溃日志写入路径绝不能因为配置语法错误而再次抛异常，破坏退出路径。
    """
    from framework.paths import home_dir, resolve_config_path

    base = {
        'framework': {'crash': {
            'enabled': True,
            'safe_mode': True,
            'max_restarts': 5,
            'path': os.path.join(root or home_dir(), 'error.log'),
        }},
    }
    cp = resolve_config_path(path)
    if not os.path.isfile(cp):
        return base
    try:
        with open(cp, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return base
    try:
        parsed = yaml.safe_load(raw)
        crash = ((parsed or {}).get('framework') or {}).get('crash') \
            if isinstance(parsed, dict) else None
        if isinstance(crash, dict):
            base['framework']['crash'].update(crash)
            return base
    except Exception:
        pass
    # 语法坏掉时按缩进找 framework.crash 段里的几个关键键。
    crash_indent = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        if crash_indent is None:
            if stripped == 'crash:' and indent > 0:
                crash_indent = indent
            continue
        if indent <= crash_indent and not stripped.startswith('#'):
            break
        for key in ('enabled', 'safe_mode', 'max_restarts', 'path'):
            if stripped.startswith(key + ':'):
                val = stripped.split(':', 1)[1].split('#', 1)[0].strip().strip('"\'')
                if key in ('enabled', 'safe_mode'):
                    base['framework']['crash'][key] = val.lower() in (
                        '1', 'true', 'yes', 'on')
                elif key == 'max_restarts':
                    try:
                        base['framework']['crash'][key] = int(val)
                    except ValueError:
                        pass
                elif val:
                    base['framework']['crash'][key] = val
    return base


def _default_values_banner(config: dict) -> str:
    """显式选择默认配置时，逐项列出当前真正生效的关键值。"""
    fill_missing_sections(config)
    db = config.get('database') or {}
    web = config.get('web') or {}
    webui = config.get('webui') or {}
    plugins = config.get('plugin') or {}
    if str(db.get('type', '')).lower() == 'mysql':
        db_text = ("mysql host=%s, database=%s" %
                   (db.get('host') or '未检测到', db.get('database') or '未检测到'))
    else:
        db_text = f"sqlite {db.get('path') or '未检测到'}"
    return '\n'.join([
        '[当前运行的不是你的配置文件] 配置解析失败，已按显式授权使用内置默认配置。',
        f"  database.type / path: {db_text}",
        f"  web.host / port: {web.get('host') or '未检测到'} / {web.get('port') or '未检测到'}",
        f"  webui.host / port: {webui.get('host') or web.get('host') or '未检测到'} / "
        f"{webui.get('port') or web.get('port') or '未检测到'}",
        f"  access_token 是否为空: {'是' if not (config.get('access_token') or config.get('onebot', {}).get('access_token')) else '否'}",
        f"  framework.log_level: {(config.get('log') or {}).get('level') or '未检测到'}",
        f"  插件目录: {plugins.get('dir') or '状态目录下的 plugins（默认）'}",
        '  原配置文件未被修改；修复后请重启并移除显式降级开关。',
    ])


def report_config_load_error(exc: ConfigLoadError, root: str = None,
                             config_path: str = None) -> None:
    """进程入口统一处置：写副本、打印并落日志、计入崩溃计数。调用方自行 sys.exit(2)。"""
    import sys

    if not exc.copy_path or not os.path.isfile(exc.copy_path):
        exc.copy_path = _make_broken_copy(exc.path)
    block = format_config_error(exc)
    print(block, file=sys.stderr)
    _get_logger().critical(block)
    try:
        from framework.crash_guard import write_crash_log

        write_crash_log(
            _best_effort_crash_config(config_path or exc.path, root),
            root or os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            extra={
                '处置指引': ('修复配置文件语法后重启；可用 python -m framework.config --check '
                             '<path> 复核。原文件未被修改，副本见上面的「原文副本」。'),
                '配置错误': block,
            })
    except Exception as e:
        _get_logger().warning(f'配置错误崩溃记录写入失败（不影响退出）: {e}')


def report_config_validation_error(exc: ConfigValidationError, root: str = None,
                                   config_path: str = None) -> None:
    """安全段未知键等致命校验问题：结构化打印、落日志并计入崩溃计数。"""
    import sys

    block = '\n'.join([
        '=' * 72,
        '[配置校验失败] 框架未启动，请修复后重启',
        f'  配置文件: {exc.path}',
        *[f'  - {issue}' for issue in exc.issues],
        '=' * 72,
    ])
    print(block, file=sys.stderr)
    _get_logger().critical(block)
    try:
        from framework.crash_guard import write_crash_log

        write_crash_log(
            _best_effort_crash_config(config_path or exc.path, root),
            root or os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            extra={
                '处置指引': '修正上面点名的配置键后重启；可用 python -m framework.config --check <path> 复核。',
                '配置错误': block,
            })
    except Exception as e:
        _get_logger().warning(f'配置错误崩溃记录写入失败（不影响退出）: {e}')


# 这些段写错的代价最高（安全开关 / 数据库连接 / 探针），未知键按 error 报。
_SECURITY_SECTIONS = frozenset(
    ('webui', 'auth', 'database', 'observability', 'security'))

# 只对这些段做「段内未知键」检测：框架核心段 + 安全相关段。
# 其余段（onebot/web/scheduler/http_api…）的键来自 core_plugins.yaml 的插件
# 配置块动态合并，schema 无法穷举，检测只会制造噪声。
_UNKNOWN_KEY_SECTIONS = frozenset(
    ('framework', 'webui', 'security', 'auth', 'database', 'observability'))


def _issue_level(path: str, strict: bool = True) -> str:
    root = path.split('.', 1)[0]
    if (path.startswith('framework.security') or path.startswith('framework.config')
            or root in _SECURITY_SECTIONS):
        return 'error' if strict else 'warning'
    return 'warning'


def _edit_distance(a: str, b: str) -> int:
    """标准 Levenshtein 距离（只用于配置键纠错建议，输入都很短）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _suggest_key(key: str, candidates) -> str | None:
    """仅当编辑距离 ≤2 或互为前缀时才给建议，避免猜出一个无关键。"""
    low = str(key).lower()
    best = None
    best_score = None
    for cand in candidates:
        name = str(cand)
        cl = name.lower()
        if cl == low:
            continue
        distance = _edit_distance(low, cl)
        if not (distance <= 2 or cl.startswith(low) or low.startswith(cl)):
            continue
        score = (distance, name)
        if best_score is None or score < best_score:
            best_score, best = score, name
    return best


def _walk_unknown_keys(node, spec, path, issues, strict=True) -> None:
    """已声明配置段内做双向比对：多出来的键逐一点名。"""
    if not isinstance(node, dict):
        return
    known = (spec or {}).get('keys', {}) or {}
    for key, val in node.items():
        if not isinstance(key, str):
            continue
        full = f"{path}.{key}" if path else key
        kspec = known.get(key)
        if kspec is not None:
            if isinstance(kspec, dict) and 'type' in kspec and isinstance(val, dict):
                _walk_unknown_keys(val, kspec, full, issues, strict)
            continue
        suggestion = _suggest_key(key, known)
        text = f"配置项 [{full}] 不是已知配置键，已忽略。"
        if suggestion:
            text += f" 是否想写 [{full.rsplit('.', 1)[0]}.{suggestion}]？"
        issues.append(ConfigIssue(text, _issue_level(full, strict)))


def _nearest_section(name: str) -> str | None:
    """只在编辑距离 ≤1 时给出「是否想写 X」。用于识别 webuii 这类手滑。

    刻意不做 ≤2 的宽松匹配：``webuiplus`` 这种真实插件段名与 ``webui`` 的
    距离是 5，但 ``user`` 与 ``users`` 之类只差 1 的两个名字依然可能是两个
    独立的合法段，因此这里只接受 1，宁可漏报建议也不误导用户改名。
    """
    low = str(name).lower()
    best = None
    for cand in sorted(_KNOWN_TOP_LEVEL):
        if cand.lower() == low:
            continue
        if _edit_distance(low, cand.lower()) <= 1:
            if best is None or (cand, len(cand)) < (best, len(best)):
                best = cand
    return best


def _plugin_section_names(config: dict) -> set:
    """能被当成「插件配置段」的段名集合。

    来源两处：
      1. ``core_plugins`` 段合并后的键（插件开关表）；
      2. 项目根 ``core_plugins/`` 与 ``plugins/`` 下含 main.py 的目录名。

    插件会在 config 里占一个与插件同名的配置段（``aiwriter`` / ``webuiplus``…），
    不做这个识别就会把几十个合法段报成「未知配置段」，还会给出
    ``webuiplus`` → ``webui`` 这种会让插件配置失效的错误建议。
    """
    names = set()
    cp = config.get('core_plugins')
    if isinstance(cp, dict):
        names.update(str(k) for k in cp)
    try:
        from framework.paths import home_dir

        root = home_dir()
        for sub in ('core_plugins', 'plugins'):
            base = os.path.join(root, sub)
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for entry in entries:
                item = os.path.join(base, entry)
                if not os.path.isdir(item):
                    continue
                if (os.path.isfile(os.path.join(item, 'main.py'))
                        or os.path.isfile(os.path.join(item, 'main.py.disabled'))):
                    names.add(entry)
    except Exception as e:
        logger.debug('[config] 插件段名枚举失败（按无插件段处理）: %s', e)
    return names


def _collect_unknown_keys(config: dict, issues: list, strict=True) -> None:
    """双向比对：只对「框架核心段 + 安全段」做段内未知键检测。

    为什么不做全段检测：``onebot`` / ``web`` / ``scheduler`` / ``http_api`` 这些段
    的键由 core_plugins.yaml 的插件配置块动态合并进来（``enabled`` /
    ``max_pending_events`` 等），schema 天然不可能穷举；插件还会各自占一个同名
    配置段（``aiwriter`` / ``webuiplus`` / ``llm_load``…）。对这些做未知键检测
    只会刷出几十条噪声，把真正的拼写错误淹没掉——那和"静默忽略"一样有害。

    未知顶层段同理：无法区分「用户自己插件写的配置段」与「框架段名手滑」，
    因此识别出的插件段直接跳过，其余一律按 warning 报出（``foo`` 这种手滑
    会被点名）；只有编辑距离 ≤1 时才附带「是否想写 X」的建议。
    """
    plugins = _plugin_section_names(config)
    for sec, val in config.items():
        # 下划线开头是框架内部注入的字段（如 _config_path），不是用户写的配置段。
        if not isinstance(sec, str) or sec.startswith('_'):
            continue
        spec = _CONFIG_SCHEMA.get(sec)
        if sec not in _KNOWN_TOP_LEVEL and spec is None:
            if sec in plugins or sec == 'core_plugins':
                # 插件配置段：插件自己读，框架无从判定键名。
                continue
            text = f"配置段 [{sec}] 不是已知配置段，已忽略。"
            suggestion = _nearest_section(sec)
            if suggestion:
                text += f" 是否想写 [{suggestion}]？"
            issues.append(ConfigIssue(text, 'warning'))
            continue
        if sec not in _UNKNOWN_KEY_SECTIONS:
            continue
        if isinstance(val, dict) and isinstance(spec, dict):
            _walk_unknown_keys(val, spec, sec, issues, strict)


def _normalize_issues(issues: list, strict: bool) -> list:
    """把「无级别的纯字符串问题」补成带 level 的 ConfigIssue。

    ``_validate_keys`` 里追加的是纯字符串（值/类型/枚举/范围），没有 level
    属性；而 ``load_config`` 用 ``getattr(i, 'level', 'warning')`` 决定要不要
    中止启动，缺属性就一律按 warning 放行——安全段的枚举写错（经典例子：
    ``security.level: off`` 不加引号会被 YAML 解析成布尔 False，框架静默
    回退到 medium，管理员以为关了防护其实没关）就这样漏过去了。
    """
    out = []
    for item in issues:
        if hasattr(item, 'level'):
            out.append(item)
            continue
        text = str(item)
        match = re.search(r'\[([^\]]+)\]', text)
        path = match.group(1) if match else ''
        out.append(ConfigIssue(text, _issue_level(path, strict)))
    return out


def validate_config(config: dict) -> list:
    """校验配置值与类型，返回问题清单（每条为可读中文）。

    只告警、绝不抛异常或改写文件；与 fill_missing_sections 互补：
    前者补缺失段，本函数查"段存在但值/类型/枚举/范围可疑"。

    设计要点：
    - 数字字段对"可解析字符串"宽松（YAML 常把端口写成 "8080"），无法解析才报错；
    - 布尔字段接受 _as_bool 可识别字符串（"true"/"false"…）；
    - 只校验"类型/枚举/范围"这类会静默误配的硬伤，不评判值是否合理
      （如 token 是否为空那是部署选择）。
    """
    issues = []
    if not isinstance(config, dict):
        return ['配置根节点不是映射（dict），请检查 config.yaml 顶层结构']

    for sec, spec in _CONFIG_SCHEMA.items():
        sec_val = config.get(sec)
        if sec_val is None:
            continue  # 缺段已由 fill_missing_sections 补默认值；这里只查存在的段
        if not isinstance(sec_val, dict):
            issues.append(f"配置段 [{sec}] 应是映射(dict)，实际为 {type(sec_val).__name__}，该段配置将被忽略")
            continue
        _validate_keys(sec, sec_val, spec.get('keys', {}), issues)

    # 跨字段联动：mysql 模式必须补齐连接信息
    db = config.get('database')
    if isinstance(db, dict) and db.get('type') == 'mysql':
        for k in ('host', 'port', 'user', 'database'):
            if not db.get(k):
                issues.append(f"database.type=mysql 但缺少 database.{k}，将无法连接数据库")

    fw = config.get('framework') or {}
    cfg_section = fw.get('config') or {} if isinstance(fw, dict) else {}
    strict = _as_bool(cfg_section.get('strict_unknown_keys', True)) \
        if isinstance(cfg_section, dict) else True
    _collect_unknown_keys(config, issues, strict=strict)
    return _normalize_issues(issues, strict)


def load_config(config_path: str = None) -> dict:
    """
    加载 YAML 配置文件，支持环境变量替换
    如果配置文件不存在，自动生成默认配置并加载
    """
    if config_path is None:
        from framework.paths import resolve_config_path
        config_path = resolve_config_path()

    if not os.path.isfile(config_path):
        _get_logger().warning(f"配置文件不存在，正在生成默认配置: {config_path}")
        _generate_default_config(config_path)

    # 语法错误红线：原文件只读不写，默认拒绝启动（ConfigLoadError）。
    # 只有显式 framework.config.on_error: default 或命令行
    # --allow-default-config（ZCBOT_ALLOW_DEFAULT_CONFIG=1）才允许降级到内置默认值。
    broken = False
    with open(config_path, 'r', encoding='utf-8') as f:
        raw = f.read()
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        broken = True
        mark = getattr(e, 'problem_mark', None)
        line_no = (mark.line + 1) if mark is not None else '未知'
        col_no = (mark.column + 1) if mark is not None else '未知'
        allow_default = (os.environ.get('ZCBOT_ALLOW_DEFAULT_CONFIG') == '1'
                         or _peek_on_error(raw) == 'default')
        if not allow_default:
            exc = ConfigLoadError(config_path, line_no, col_no, raw, e)
            exc.copy_path = _make_broken_copy(config_path)
            raise exc
        config = yaml.safe_load(_DEFAULT_CONFIG) or {}
        _get_logger().critical(_default_values_banner(config))
    if config is None:
        config = {}

    # 凭据加密：有密钥就把明文 token 一次性加密写回（保留注释），内存字典同步加密；
    # 没有密钥时旧明文配置照常加载，只提示一次，绝不阻断启动。
    try:
        from framework import secretbox as _secretbox
        _secretbox.warn_if_no_key()
        if _secretbox.available() and not broken:
            _migrated = _secretbox.migrate_file_and_config(config_path, config)
            if _migrated:
                _get_logger().info("已将 %d 处明文凭据加密写回 %s", _migrated, config_path)
    except Exception as e:
        _get_logger().warning(f"凭据加密迁移跳过（按原配置继续）: {e}")

    # 环境变量替换
    config = _env_replace(config)

    # 环境变量替换后再解密：密文本身不含 ${}，顺序互换会让占位符失效。
    try:
        from framework import secretbox as _secretbox
        _secretbox.decrypt_config_secrets(config)
    except Exception as e:
        _get_logger().warning(f"凭据解密跳过（按原配置继续）: {e}")

    # 官方插件配置中心：自动扫描 core_plugins/ 同步 core_plugins.yaml，并合并进 config
    _autoload_core_plugins(config)

    # 新版引入的配置段：旧 config.yaml 里没有 → 用默认值补进内存（不改用户文件）。
    # 不补的话，新能力（去重/限流、可观测、迁移管控…）对存量部署不可见不可调。
    try:
        _added = fill_missing_sections(config)
    except Exception as e:
        _get_logger().warning(f"配置缺段补齐失败（按原配置继续）: {e}")
    else:
        if _added:
            _get_logger().info(
                "配置文件缺少新版本引入的配置段，已用默认值生效（未改写文件）：%s。"
                "如需调整，请参考默认配置补写进 config.yaml。", ", ".join(_added))

    # Gap 1：值与类型校验（只告警、不阻断启动、不改写文件）
    try:
        _issues = validate_config(config)
    except Exception as e:
        _get_logger().warning(f"配置校验异常（跳过）: {e}")
    else:
        errors = [i for i in _issues if getattr(i, 'level', 'warning') == 'error']
        warnings = [i for i in _issues if getattr(i, 'level', 'warning') != 'error']
        if errors:
            _get_logger().error(
                "配置文件存在 %d 处高危问题（安全/连接开关可能已失效），拒绝启动：\n  - %s",
                len(errors), "\n  - ".join(errors))
            raise ConfigValidationError(config_path, errors)
        if warnings:
            _get_logger().warning(
                "配置文件存在 %d 处可疑配置（不影响启动，但可能静默误配），请检查：\n  - %s",
                len(warnings), "\n  - ".join(warnings))

    # 启动横幅显示真实配置路径；不作为 YAML 键写回，也不参与校验/未知键扫描。
    config['_config_path'] = os.path.abspath(config_path)
    return config


def get_config() -> dict:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


# 官方插件配置中心：独立 yaml，由启动时自动扫描 core_plugins/ 目录同步
# 注：支持环境变量 ZCBOT_CORE_PLUGINS_YAML 覆盖路径（黑盒测试启动器
# tests/webui_blackbox_launcher.py 借此把插件配置指向一份隔离副本，
# 从而在不改动真实仓库的前提下自定义 webui 监听端口等）。
import logging

from framework.paths import core_plugins_yaml_path as _core_plugins_yaml_path

logger = logging.getLogger('zcbot')

CORE_PLUGINS_YAML = _core_plugins_yaml_path()


# 官方插件 → 主 config 段名映射（仅列段名与插件名不同的）
_CORE_PLUGIN_SECTION = {'onebot_adapter': 'onebot', 'webui': 'web'}


# 官方插件默认配置（含主要配置项），自动写入 core_plugins.yaml。
# 启停与配置的唯一权威是 core_plugins.yaml（tools/scan_core_plugins.py 维护）。
#
# 注意：各插件块里的 enabled 仅供参考/示例——「自动补块」时的真实默认开关由
# DEFAULT_ENABLED_CORE_PLUGINS 决定（见下方），避免代码权威与文档口径不一致。
_CORE_PLUGIN_SCHEMA = {
    'echo': {'enabled': True},
    'help': {'enabled': True},
    'onebot_adapter': {'enabled': False, 'listen_host': '0.0.0.0',
                       'listen_port': 6830, 'access_token': '',
                       'max_pending_events': 4096,
                       'max_pending_bytes': 67108864,
                       'max_frame_size': 16777216},
    'webui': {'enabled': True, 'host': '127.0.0.1', 'port': 8080},
    'webuiplus': {'enabled': False, 'theme': 'auto', 'glass': True,
                  'intro': True, 'display': 'card', 'aliases': {},
                  'layout': ['time', 'sender', 'text', 'images'],
                  'max_messages': 200, 'image_max_bytes': 8388608},
    'session': {'enabled': False},
    'scheduler': {'enabled': True},
    'html_assembler': {'enabled': False, 'max_template_bytes': 4194304,
                       'max_image_bytes': 4194304, 'max_output_bytes': 16777216,
                       'max_images': 64, 'escape_html': True,
                       'on_missing': 'keep'},
    'http_api': {'enabled': False, 'host': '127.0.0.1', 'port': 1145,
                 'token': '', 'allow_db': False},
    'http_inject': {'enabled': False, 'host': '127.0.0.1', 'port': 8901,
                    'path': '/hook', 'token': ''},
    'ws_client': {'enabled': False, 'url': 'ws://127.0.0.1:9000/ws',
                  'bot_name': 'ws_client', 'token': '',
                  'reconnect_interval': 5, 'reconnect_max_interval': 60,
                  'reconnect_jitter': 0.25, 'max_queue': 256},
    'qq_official': {'enabled': False, 'app_id': '', 'app_secret': '',
                    'bot_name': 'qq_official', 'intents': 100663296,
                    'reconnect_interval': 5,
                    # 官方 2026-08-10 起统一域名；sgroup / sandbox 均已不是调用入口
                    'api_base': 'https://api.bot.qq.com',
                    'auto_ack_interaction': True,
                    'enable_notice_events': True},
    'telegram': {'enabled': False, 'token': '', 'bot_name': 'telegram',
                 'polling_timeout': 30, 'api_base': 'https://api.telegram.org'},
    'kook': {'enabled': False, 'token': '', 'bot_name': 'kook',
             'api_base': 'https://www.kookapp.cn', 'reconnect_interval': 5},
    'matrix': {'enabled': False, 'homeserver': '', 'access_token': '',
               'user_id': '', 'bot_name': 'matrix', 'sync_timeout': 30,
               'rooms': [], 'reconnect_interval': 3},
    'discord': {'enabled': False, 'token': '', 'bot_name': 'discord',
                'intents': 37377, 'reconnect_interval': 5,
                'api_base': 'https://discord.com/api/v10',
                'gateway_url': 'wss://gateway.discord.gg/'},
    'llm_load': {  # LLM 核心装载器：释放/自愈 plugins/llm_core
        'enabled': False, 'plugin_name': 'llm_core',
        'auto_restart': True, 'payload': '',
    },
    'aiwriter': {  # 终端 AI 智能体：自带 LLM 客户端 + 工作区工具集 + Agent 循环
        'enabled': False, 'base_url': 'https://api.deepseek.com/v1',
        'api_key': '', 'model': 'deepseek-chat', 'temperature': 0.7,
        'max_rounds': 12, 'timeout': 120, 'mode': 'workspace',
        'allow_exec': False, 'workspace': '', 'keep_turns': 20,
        'max_history_chars': 24000, 'max_output': 8000, 'shell_timeout': 60,
        'python_timeout': 60, 'net_timeout': 20, 'session_key': 'terminal',
        'agent': 'build', 'agents': [],
    },
    'rust_accel': {'enabled': False, 'ws_host': '0.0.0.0', 'ws_port': 6831,
                   'access_token': '', 'max_frame_size': 16777216,
                   'max_pending_events': 4096, 'max_pending_bytes': 67108864,
                   'stats_interval_secs': 5, 'ipc_strip_raw': False,
                   'inherit_onebot': False, 'binary_path': '',
                   'auto_build': False},
    # 图片渲染：字体子集与原生扩展不进源码仓库（约 15MB，下载包约 9MB），
    # 首次启用时按 asset_url 获取到 data 目录。
    #
    #   backend        pil（默认，零依赖）/ rust（用原生扩展）
    #   asset_consent  ask（默认：**不下载**，只提示；确认后再改）/
    #                  auto（自动下载）/ manual（只用手动放置的文件）
    #
    # 为什么默认 ask 而不是 auto：切到 rust 需要拉约 9MB。按流量计费或内网环境
    # 下静默下载会被当成事故，所以必须先让用户知道并自己决定。
    #
    # asset_url 指向独立的资源仓库 zcbot-render-assets，不是主仓：二进制与字体
    # 的更新节奏和代码发版无关（补个字形、重编一个平台的 .so 不该逼着框架 bump
    # 版本），混在一个仓库里只会让 clone / CI / wheel 一起变胖。该仓库的 latest
    # 是固定链接，资源重发时覆盖同名 Release，这里永远不用改。
    'image_renderer': {'enabled': False,
                       'backend': 'pil',
                       'asset_consent': 'ask',
                       'asset_url': 'https://github.com/kuangxing6367/'
                                    'zcbot-render-assets/releases/latest/'
                                    'download/assets.zip',
                       'asset_timeout': 30},
}

# 官方插件的「默认开启」名单：新克隆 / 首次启动生成 core_plugins.yaml 时，
# 这些插件自动 enabled=True；其余一律默认关闭（发现即禁用）。
# 与文档《扩展清单》保持一致：echo / help / scheduler / webui 默认开启，接入端等默认关闭。
DEFAULT_ENABLED_CORE_PLUGINS = {'echo', 'help', 'scheduler', 'webui'}


def _scan_core_plugins() -> list:
    """扫描内核自带插件目录，返回已安装（含 main.py）的官方插件名列表。"""
    from framework.paths import core_plugins_dir
    plugins_dir = core_plugins_dir()
    names = []
    if os.path.isdir(plugins_dir):
        for name in os.listdir(plugins_dir):
            if name.startswith('_'):
                continue
            if os.path.isfile(os.path.join(plugins_dir, name, 'main.py')):
                names.append(name)
    return sorted(names)


def _as_bool(value) -> bool:
    """严格布尔解析：布尔、数字 0/1、常见字符串 true/false 均按语义解析。
    避免 yaml 中 enabled: "false" / "0" 等字符串被 bool() 误判为 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('1', 'true', 'yes', 'on', 'y'):
            return True
        if s in ('0', 'false', 'no', 'off', 'n'):
            return False
    return bool(value)


def _autoload_core_plugins(config: dict) -> dict:
    """官方插件配置中心（core_plugins.yaml）自动同步 + 合并进主 config。

    权威划分：
      - **enabled（启停）**：core_plugins.yaml 唯一权威；
      - **运行参数（host/port/token 等）**：config.yaml 对应段唯一权威，
        core_plugins.yaml 里若残留同名键会在下面第 2.5 步被移除，
        避免「两边都能改、只有一边生效」。

    流程：
      1. 扫描 core_plugins/ 目录得到已安装官方插件；
      2. 读 core_plugins.yaml，为「已安装但缺失」的插件补默认配置块，
         **enabled 一律 False（发现即禁用，绝不静默启用）**；
      2.5 收敛重叠键：config.yaml 已有的同名键从 core_plugins.yaml 移除；
      3. 移除 yaml 中已不再安装的插件块（卸载自动删除）；
      4. 有变化则回写 yaml；
      5. 把每个插件的 enabled 合并进 config['core_plugins']，把插件配置块
         合并进对应 section（onebot/web/http_api/...），config.yaml 优先。
    返回合并后的 {plugin: cfg}。
    """
    # 【踩坑保佑】最佳实践 配置单一事实源
    # 坑：两处都能改只有一处生效是最大的坑  →  _autoload_core_plugins
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG

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
            # 损坏现场保留为 .bak，供人工排查/恢复（随后自动重建新配置）
            try:
                bak_path = f"{yaml_path}.bak"
                if os.path.isfile(yaml_path) and not os.path.exists(bak_path):
                    os.replace(yaml_path, bak_path)
                    _get_logger().warning(f"已备份损坏的 core_plugins.yaml → {bak_path}")
            except Exception as be:
                _get_logger().warning(f"备份损坏的 core_plugins.yaml 失败: {be}")
    cps = data.get('core_plugins') if isinstance(data, dict) else None
    cps = cps if isinstance(cps, dict) else {}

    changed = False
    # core_plugins.yaml 也可能残留 access_token / token 等凭据：同样一次性加密落盘。
    # 内存里先保持密文，等最后合并进主 config 前再解密成运行时明文。
    try:
        from framework import secretbox as _secretbox
        if _secretbox.available() and _secretbox.encrypt_config_secrets(data):
            _secretbox.rewrite_secret_file(yaml_path)
            changed = True
    except Exception as e:
        _get_logger().warning(f"core_plugins.yaml 凭据加密跳过: {e}")

    # 2. 已安装缺失插件 → 补默认配置块（enabled 强制 False：发现即禁用，
    #    需用户/扫描工具显式启用后才加载）；并迁移主 config 对应段已有值
    for name in installed:
        section = _CORE_PLUGIN_SECTION.get(name, name)
        main_sec = config.get(section)
        if not isinstance(main_sec, dict):
            main_sec = {}
        blk = cps.get(name)
        if not isinstance(blk, dict):
            blk = dict(_CORE_PLUGIN_SCHEMA.get(name, {}))
            # 仅「默认开启名单」内的插件自动启用；其余发现即禁用，绝不静默启用
            blk['enabled'] = name in DEFAULT_ENABLED_CORE_PLUGINS
            cps[name] = blk
            changed = True
        # config.yaml 段已不再管理启停；缺失 enabled 时按默认开启名单处理，
        # 避免"未列出自动启用"（历史 bug：缺省 True 曾致误加载）。
        if 'enabled' not in blk:
            blk['enabled'] = name in DEFAULT_ENABLED_CORE_PLUGINS
            changed = True
        # 补默认配置键（不覆盖 yaml 已设的值；优先迁移主 config 对应段的值）
        for k, v in _CORE_PLUGIN_SCHEMA.get(name, {}).items():
            if k not in blk:
                blk[k] = main_sec[k] if k in main_sec else v
                changed = True
    # 2.5 重叠键收敛：config.yaml 已经有同名键的，从 core_plugins.yaml 中移除。
    #     两处都能改、实际只有一处生效，是配置系统最容易踩的坑（改 A 没用、
    #     改 B 也没用，因为真正生效的是另一处）。这里一次性收敛：
    #     全局/部署类参数（host/port/token 等）归 config.yaml，
    #     core_plugins.yaml 只保留 enabled 与插件私有键。
    for name, blk in list(cps.items()):
        if not isinstance(blk, dict):
            continue
        section = _CORE_PLUGIN_SECTION.get(name, name)
        main_sec = config.get(section)
        if not isinstance(main_sec, dict) or not main_sec:
            continue
        taken = [k for k in blk if k != 'enabled' and k in main_sec]
        for k in taken:
            del blk[k]
            changed = True
        if taken:
            _get_logger().info(
                f"[{name}] 配置键 {', '.join(sorted(taken))} 已收敛到 config.yaml 的 "
                f"{section} 段（core_plugins.yaml 中已移除），此后请改 config.yaml")

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
    #    优先级：config.yaml 的同名段为准（它是部署参数的权威，WebUI 面板也写这里），
    #    core_plugins.yaml 只补 config.yaml 里没有的键。
    #    注意：cps 在内存里仍是密文；这里复制一份解密，避免解密后的值被上面的
    #    safe_dump 回写又变成明文。
    core_cfg = {}
    try:
        import copy

        from framework import secretbox as _secretbox
        cps_plain = copy.deepcopy(cps)
        _secretbox.decrypt_config_secrets(cps_plain)
    except Exception as e:
        _get_logger().warning(f"core_plugins.yaml 凭据解密跳过: {e}")
        cps_plain = cps
    for name, blk in cps_plain.items():
        if not isinstance(blk, dict):
            continue
        section = _CORE_PLUGIN_SECTION.get(name, name)
        cur = config.get(section)
        if isinstance(cur, dict):
            merged = dict(blk)
            merged.update(cur)          # config.yaml 覆盖 core_plugins.yaml
        else:
            merged = dict(blk)
        config[section] = merged
        core_cfg[name] = _as_bool(blk.get('enabled', False))
    config['core_plugins'] = core_cfg
    return cps


def list_core_plugins_state():
    """返回全部已安装官方插件的开关状态（供 WebUI 可视化开关用）。

    返回 list[{name, enabled, section, desc}]。enabled 以 core_plugins.yaml
    （不存在则用默认开启名单）为准；desc 取 _CORE_PLUGIN_SCHEMA 注释。
    """
    installed = _scan_core_plugins()
    yaml_path = CORE_PLUGINS_YAML
    cps = {}
    if os.path.isfile(yaml_path):
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            cps = data.get('core_plugins') if isinstance(data, dict) else {}
        except Exception as _exc:
            logger.debug('[list_core_plugins_state] 捕获异常后按降级路径继续: %s', _exc)
            cps = {}
    out = []
    for name in sorted(installed):
        blk = cps.get(name) if isinstance(cps, dict) else None
        if isinstance(blk, dict):
            enabled = _as_bool(blk.get('enabled', name in DEFAULT_ENABLED_CORE_PLUGINS))
        else:
            enabled = name in DEFAULT_ENABLED_CORE_PLUGINS
        section = _CORE_PLUGIN_SECTION.get(name, name)
        out.append({
            'name': name,
            'enabled': enabled,
            'section': section,
            'desc': _core_plugin_desc(name),
        })
    return out


def set_core_plugin_enabled(name: str, enabled: bool, config: dict = None):
    """切换官方插件开关并回写 core_plugins.yaml。

    返回 (ok, needs_restart)。plugin 不存在返回 (False, False)。

    写回走 cli.utils 的按行原地替换：保留 yaml 里的注释、键顺序与未知字段。
    旧实现用 ``yaml.safe_dump`` 全量重写，会把用户手写的注释和排版一次性冲掉，
    是「WebUI 点一下开关，配置文件就被格式化」的根因。

    :param config: 可选。传入时同步内存 ``config['core_plugins'][name]['enabled']``，
        使同进程内紧接着的 ``_load_core_plugins(only={name})`` 能读到新值，
        从而实现「不重启即时生效」。不传则只落盘（保持 CLI 调用方行为不变）。
    """
    installed = _scan_core_plugins()
    if name not in installed:
        return False, False

    from framework.cli.utils import set_core_plugin_enabled as _patch_yaml

    yaml_path = CORE_PLUGINS_YAML
    ok, detail = _patch_yaml(yaml_path, name, bool(enabled))
    if not ok:
        _get_logger().warning(f"写回 core_plugins.yaml 失败: {detail}")
        return False, False

    # 块此前不存在时，按行工具只补了 enabled 一行；这里补齐 schema 默认值，
    # 与 _autoload_core_plugins 的「发现即补块」口径一致，避免私有键缺失。
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        cps = data.get('core_plugins') if isinstance(data, dict) else {}
        blk = cps.get(name) if isinstance(cps, dict) else None
        if isinstance(blk, dict):
            defaults = _CORE_PLUGIN_SCHEMA.get(name, {})
            missing = {k: v for k, v in defaults.items() if k not in blk}
            if missing:
                for k, v in missing.items():
                    _patch_yaml_key(yaml_path, name, k, v)
    except Exception as _exc:
        _get_logger().debug('[set_core_plugin_enabled] 补默认键失败（不影响开关）: %s', _exc)

    # 同步内存：让即时生效分支读到的 enabled 与落盘值一致
    if isinstance(config, dict):
        cc = config.setdefault('core_plugins', {})
        if isinstance(cc.get(name), dict):
            cc[name]['enabled'] = bool(enabled)

    return True, True


def _patch_yaml_key(yaml_path: str, plugin_id: str, key: str, value) -> bool:
    """在 core_plugins.<plugin_id> 块内按行补一个缺失的标量键（保注释）。

    只处理标量（str/int/float/bool/None/list-of-scalars）；复杂结构不在这里补，
    交给启动时的 _autoload_core_plugins。返回是否真的写入。
    """
    from framework.cli.utils import read_file

    if not os.path.isfile(yaml_path):
        return False
    lines = read_file(yaml_path).splitlines()
    out = []
    i, n = 0, len(lines)
    in_top = in_block = False
    placed = False

    def _fmt(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            if not v:
                return "[]"
            if all(isinstance(x, (str, int, float, bool)) for x in v):
                return "[" + ", ".join(_fmt(x) for x in v) + "]"
            return None
        s = str(v)
        return "'" + s.replace("'", "''") + "'"

    rendered = _fmt(value)
    if rendered is None:
        return False

    while i < n:
        line = lines[i]
        if not in_top:
            out.append(line)
            if line.strip() == 'core_plugins:':
                in_top = True
            i += 1
            continue
        m = re.match(r"^  ([A-Za-z0-9_]+):", line)
        if m:
            if in_block:
                # 上一个目标块结束，仍未找到 key：在块尾插入
                if not placed:
                    out.append("    %s: %s" % (key, rendered))
                    placed = True
            in_block = (m.group(1) == plugin_id)
            out.append(line)
            i += 1
            continue
        if in_block and re.match(r"^    %s\s*:" % re.escape(key), line):
            out.append(line)  # 已存在，不动
            placed = True
            i += 1
            continue
        out.append(line)
        i += 1
    if in_block and not placed:
        out.append("    %s: %s" % (key, rendered))
        placed = True
    if not placed:
        return False
    try:
        with open(yaml_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(out).rstrip() + "\n")
        return True
    except Exception as e:
        _get_logger().debug('[_patch_yaml_key] 写入失败: %s', e)
        return False


def _core_plugin_desc(name: str):
    """从 core_plugins/<name>/main.py 的 __plugin_meta__ 读取 desc（AST 静态解析，不执行代码）。"""
    import ast as _ast

    from framework.paths import core_plugins_dir
    path = os.path.join(core_plugins_dir(), name, 'main.py')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            txt = f.read()
        i = txt.find('__plugin_meta__')
        if i < 0:
            return ''
        j = txt.find('{', i)
        depth, end = 0, -1
        for k in range(j, len(txt)):
            if txt[k] == '{':
                depth += 1
            elif txt[k] == '}':
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end < 0:
            return ''
        d = _ast.literal_eval(txt[j:end])
        if isinstance(d, dict):
            return (d.get('desc') or d.get('description') or '')
    except Exception as _exc:
        logger.debug('[_core_plugin_desc] 捕获异常后按降级路径继续: %s', _exc)
        return ''
    return ''


_config = None


def check_config(path: str = None) -> int:
    """只读加载 + 校验配置：不生成文件、不启动框架、不迁移凭据。

    返回 0 = 通过，2 = 有问题（语法错误 / 结构错误 / 校验问题清单非空）。
    """
    if path is None:
        from framework.paths import resolve_config_path

        path = resolve_config_path()
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print(f'配置检查失败：文件不存在: {path}')
        return 2
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        line_no = (mark.line + 1) if mark is not None else '未知'
        col_no = (mark.column + 1) if mark is not None else '未知'
        print(f'配置检查失败：{path} 第 {line_no} 行第 {col_no} 列语法错误: {e}')
        return 2
    issues = validate_config(config)
    if issues:
        print(f'配置检查失败：{path} 共 {len(issues)} 处问题：')
        for issue in issues:
            print(f'  - [{getattr(issue, "level", "warning")}] {issue}')
        return 2
    print(f'配置检查通过：{path}')
    return 0


def _main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='ZCBOT 配置文件只读检查')
    parser.add_argument('--check', action='store_true',
                        help='只加载并校验配置，不启动框架、不写任何文件')
    parser.add_argument('path', nargs='?', default=None,
                        help='配置文件路径（缺省用默认解析路径）')
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return 0
    return check_config(args.path)


if __name__ == '__main__':
    import sys

    sys.exit(_main())
