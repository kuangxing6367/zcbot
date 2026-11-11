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
  migrations:
    auto_apply_destructive: false   # 生产环境保持 false：开机流程不能兼任批准人
    backup_before_rewrite: true     # rewrite/destructive 前自动备份（SQLite 在线快照）
    keep_backups: 5                 # 备份保留份数，避免占满磁盘
    # operator: ""                  # 记录到执行流水里的操作人；默认取系统登录名
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
observability:
  health: true                 # /healthz（存活，不查库）与 /readyz（就绪，含逐项检查明细）
  metrics: true                # /metrics（Prometheus 文本格式，仅在 API 端口暴露，避免公网 WebUI 端口泄露）
  metrics_token: ""            # 非空时 /metrics 需 Authorization: Bearer <token>（跨机抓取时设置）

# ── 入站可靠性（去重 + 限流，核心基础设施，非运维项）──────────────
# 生产环境机器人先挂的往往是「没限流」和「重连重放重复回复」，不是命令逻辑。
# 详见 docs/进阶/可靠性与限流.md。
reliability:
  dedup_window: 300            # 入站消息去重窗口（秒）：断线重连 / 客户端重推的同一 message_id
                              # 只处理首条，后续静默跳过；0=禁用。仅对 message 类事件生效，
                              # notice/meta/request 本身幂等，不去重。
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
  dir: plugins               # 插件代码目录（.py 文件）
  # dat_dir: data/plugins_dat # 插件数据/配置目录（默认统一存放于 data/plugins_dat）
  heartbeat_interval: 60     # 插件注册心跳间隔（秒）
  auto_install_deps_on_startup: true  # 启动时自动安装缺失依赖到全局环境（移机自愈，版本冲突自动跳过）
  max_memory_mb: 64          # 单插件内存上限（MB），超限自动卸载
  hot_reload: true           # 用户插件（plugins/）源码变更时自动全量热重载（unload+reload），改动的 handler 逻辑即时生效；false 则沿用旧行为（仅重新注册，不重跑模块代码）。仅作用于用户插件，核心插件不参与

# ── 依赖自检（启动自愈）──────────────────────────────────
# 启动自检读取 requirements.txt，自动安装缺失的核心依赖（移机自愈）。
# allow_missing：列出"允许缺失、无需安装也不报错"的包名（小写，连字符/下划线等价）。
#   这些包即便缺失，也只记一条 WARNING，不强制安装、不打断启动、不退出。
#   适用：确定不用的可选依赖（如纯 Web 部署不装 Pillow / 不用 IIPC 不装 go 工具链对应包）。
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
  #   blacklist_ttl: 3600      # 自动解封秒数（0=永久封禁）
  #   log_body: true           # 是否记录请求体（便于取证）
  #   max_body_log: 2048       # 请求体记录上限（字节）
  #   unknown_api_decoy: true  # 未知 /api 路径返回诱饵（false=普通 404）
  #   auth_fail_blacklist: 20  # 同一 IP 认证连续失败多少次后拉黑（0=不拉黑）
  #   auth_fail_window: 300    # 认证失败统计窗口（秒）
  #   tarpit_auth_ms: 800      # 认证失败时的随机延迟上限（毫秒）
  #   invalid_token_decoy: false # true=无效 token 返回诱饵 200（会掩盖静默丢弃，慎用）
  #   canary: true             # 在诱饵响应中植入唯一追踪串，便于事后追溯泄露路径
  #   evasion_detect: true     # 检测路径变形绕过（%2e 编码 / 重复斜杠 / 穿越），加重处置
  #   fake_404: true           # 未知页面返回伪装 nginx 的假 404（false=普通 JSON 404）

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
  # ── 状态目录自动迁移（默认开启）──
  # 启动期探测当前目录是哪一代版本留下的布局；若是旧版根目录布局，自动把
  # config.yaml / core_plugins.yaml / plugins/ 收敛到 data/。
  # 约束：绝不覆盖 data/ 下已有文件，绝不改写配置内容，失败不阻断启动。
  # 想自己管目录就改成 false —— 框架仍兼容读取旧布局，只是不再自动搬。
  # 临时覆盖（CI / 排障）：环境变量 ZCBOT_AUTO_MIGRATE=0，无需改配置文件。
  # 手动体检 / 预演：python -m framework.migration --inspect / --dry-run
  auto_migrate: true

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

# ── IIPC 内核链路（可选，--iipc 启动）────────────────────────
# 与主框架并列的第二条启动路径；不带 --iipc 时本节不参与。
# 优先级：命令行 > 本节 > 环境变量 > 内置默认。
iipc:
  ipc_addr: "127.0.0.1:18730"
  db: ":memory:"
  admin: ""
  plugins_dir: ""
  onebot_url: ""
  onebot_listen: ""
  onebot_token: ""
  no_rust: false
  max_rss_mb: 1024

# ── 调试控制台（无 TTY 也能接进运行中的框架） ──────────────────
# 启动时在 127.0.0.1 上监听一个本地端口，端口与超长随机 token 自动生成并
# 存入数据库（console_access 表），无需重启、无需 TTY（systemd 托管场景）。
# 本地执行 `python startup.py attach` 即可接入，直接使用框架的终端命令
# （help / status / plugins / reload / ...）。
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
    except Exception as e:  # noqa: BLE001 - 默认模板自身损坏不应阻断启动
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
            'type': {'allowed': ('sqlite', 'mysql')},
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
            'session_timeout': {'int': True, 'min': 30},
        },
    },
    'observability': {
        'type': dict,
        'keys': {
            'health': {'bool': True},
            'metrics': {'bool': True},
        },
    },
    'reliability': {
        'type': dict,
        'keys': {
            'dedup_window': {'int': True, 'min': 0},
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
    'memory': {
        'type': dict,
        'keys': {
            'limit_mb': {'int': True, 'min': 1},
        },
    },
    'framework': {
        'type': dict,
        'keys': {
            'conventions': {
                'type': dict,
                'keys': {
                    'command_prefix': {'str': True},
                },
            },
        },
    },
}


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
        if 'allowed' in kspec:
            if v not in kspec['allowed']:
                issues.append(f"配置项 [{full}]={v!r} 不在允许集合 {kspec['allowed']} 内")
            continue


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

    return issues


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

    with open(config_path, 'r', encoding='utf-8') as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            _get_logger().error(
                f"配置文件语法错误: {config_path} - {e}\n"
                f"已回退到默认配置启动，请修复配置后重启"
            )
            backup = config_path + '.bak'
            try:
                import shutil
                shutil.copy(config_path, backup)
                _get_logger().warning(f"损坏的配置文件已备份到: {backup}")
            except Exception:
                pass
            _generate_default_config(config_path)
            with open(config_path, 'r', encoding='utf-8') as f2:
                config = yaml.safe_load(f2)
    if config is None:
        config = {}

    # 环境变量替换
    config = _env_replace(config)

    # 官方插件配置中心：自动扫描 core_plugins/ 同步 core_plugins.yaml，并合并进 config
    _autoload_core_plugins(config)

    # 新版引入的配置段：旧 config.yaml 里没有 → 用默认值补进内存（不改用户文件）。
    # 不补的话，新能力（去重/限流、可观测、迁移管控…）对存量部署不可见不可调。
    try:
        _added = fill_missing_sections(config)
    except Exception as e:  # noqa: BLE001
        _get_logger().warning(f"配置缺段补齐失败（按原配置继续）: {e}")
    else:
        if _added:
            _get_logger().info(
                "配置文件缺少新版本引入的配置段，已用默认值生效（未改写文件）：%s。"
                "如需调整，请参考默认配置补写进 config.yaml。", ", ".join(_added))

    # Gap 1：值与类型校验（只告警、不阻断启动、不改写文件）
    try:
        _issues = validate_config(config)
    except Exception as e:  # noqa: BLE001
        _get_logger().warning(f"配置校验异常（跳过）: {e}")
    else:
        if _issues:
            _get_logger().warning(
                "配置文件存在 %d 处可疑配置（不影响启动，但可能静默误配），请检查：\n  - %s",
                len(_issues), "\n  - ".join(_issues))

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
from framework.paths import core_plugins_yaml_path as _core_plugins_yaml_path  # noqa: E402
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
    core_cfg = {}
    for name, blk in cps.items():
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
        except Exception:
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


def set_core_plugin_enabled(name: str, enabled: bool):
    """切换官方插件开关并回写 core_plugins.yaml（需重启框架生效）。

    返回 (ok, needs_restart)。plugin 不存在返回 (False, False)。
    """
    installed = _scan_core_plugins()
    if name not in installed:
        return False, False
    yaml_path = CORE_PLUGINS_YAML
    data = {}
    if os.path.isfile(yaml_path):
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
    cps = data.get('core_plugins') if isinstance(data, dict) else {}
    if not isinstance(cps, dict):
        cps = {}
    blk = cps.get(name)
    if not isinstance(blk, dict):
        blk = dict(_CORE_PLUGIN_SCHEMA.get(name, {}))
        blk['enabled'] = bool(enabled)
        cps[name] = blk
    else:
        blk['enabled'] = bool(enabled)
    data['core_plugins'] = cps
    try:
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return True, True
    except Exception as e:
        _get_logger().warning(f"写回 core_plugins.yaml 失败: {e}")
        return False, False


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
    except Exception:
        return ''
    return ''


_config = None
