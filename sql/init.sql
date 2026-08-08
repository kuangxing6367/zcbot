-- ============================================================
-- OneBot 11 QQ机器人框架 · 数据库初始化脚本
-- 目标数据库：MySQL 5.7
-- 编码：utf8mb4
-- ============================================================

CREATE DATABASE IF NOT EXISTS zcbot
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE zcbot;

-- ============================================================
-- 1. 用户表
-- 记录机器人已知的 QQ 用户信息
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    user_id         BIGINT          NOT NULL        COMMENT 'QQ号',
    nickname        VARCHAR(100)    DEFAULT NULL    COMMENT '昵称',
    avatar_url      VARCHAR(500)    DEFAULT NULL    COMMENT '头像URL',
    is_friend       TINYINT(1)      DEFAULT 0       COMMENT '是否为好友',
    is_blacklist    TINYINT(1)      DEFAULT 0       COMMENT '是否黑名单',
    remark          VARCHAR(200)    DEFAULT NULL    COMMENT '备注',
    first_seen_at   DATETIME        DEFAULT CURRENT_TIMESTAMP    COMMENT '首次出现时间',
    last_active_at  DATETIME        DEFAULT NULL    COMMENT '最后活跃时间',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_id (user_id),
    INDEX idx_blacklist (is_blacklist),
    INDEX idx_last_active (last_active_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='QQ用户信息表';


-- ============================================================
-- 2. 群组表
-- 记录机器人加入的 QQ 群信息
-- ============================================================
CREATE TABLE IF NOT EXISTS groups_info (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    group_id        BIGINT          NOT NULL        COMMENT '群号',
    group_name      VARCHAR(200)    DEFAULT NULL    COMMENT '群名称',
    member_count    INT             DEFAULT 0       COMMENT '成员数',
    max_member_count INT            DEFAULT 0       COMMENT '最大成员数',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '机器人是否在此群活跃',
    is_blacklist    TINYINT(1)      DEFAULT 0       COMMENT '是否黑名单群',
    join_at         DATETIME        DEFAULT NULL    COMMENT '机器人入群时间',
    leave_at        DATETIME        DEFAULT NULL    COMMENT '机器人退群时间',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_group_id (group_id),
    INDEX idx_active (is_active),
    INDEX idx_blacklist (is_blacklist)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='QQ群信息表';


-- ============================================================
-- 3. 群成员表
-- 记录机器人已知的群成员关系
-- ============================================================
CREATE TABLE IF NOT EXISTS group_members (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    group_id        BIGINT          NOT NULL        COMMENT '群号',
    user_id         BIGINT          NOT NULL        COMMENT 'QQ号',
    card            VARCHAR(100)    DEFAULT NULL    COMMENT '群名片/昵称',
    role            VARCHAR(20)     DEFAULT 'member' COMMENT '角色：owner/admin/member',
    title           VARCHAR(100)    DEFAULT NULL    COMMENT '群头衔',
    join_time       INT             DEFAULT 0       COMMENT '加群时间戳',
    is_muted        TINYINT(1)      DEFAULT 0       COMMENT '是否被禁言',
    mute_until      DATETIME        DEFAULT NULL    COMMENT '禁言到期时间',
    last_active_at  DATETIME        DEFAULT NULL    COMMENT '最后发言时间',
    message_count   INT             DEFAULT 0       COMMENT '总发言数',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_group_user (group_id, user_id),
    INDEX idx_user (user_id),
    INDEX idx_role (role),
    INDEX idx_muted (is_muted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='群成员关系表';


-- ============================================================
-- 4. 插件注册表
-- 记录所有已安装的插件元数据与状态
-- ============================================================
CREATE TABLE IF NOT EXISTS plugins (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    plugin_name     VARCHAR(50)     NOT NULL        COMMENT '插件名称',
    version         VARCHAR(20)     DEFAULT NULL    COMMENT '版本号',
    author          VARCHAR(100)    DEFAULT NULL    COMMENT '作者',
    description     TEXT            DEFAULT NULL    COMMENT '插件描述',
    priority        INT             DEFAULT 50      COMMENT '全局优先级（越小越优先）',
    status          ENUM('running','stopped','error','oom')  DEFAULT 'running'   COMMENT '运行状态',
    memory_usage    DOUBLE          DEFAULT 0       COMMENT '实时内存占用(MB)',
    install_path    VARCHAR(500)    DEFAULT NULL    COMMENT '安装路径',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '启用/禁用',
    has_register    TINYINT(1)      DEFAULT 0       COMMENT '是否已完成register()',
    loaded_at       DATETIME        DEFAULT NULL    COMMENT '最后加载时间',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_plugin_name (plugin_name),
    INDEX idx_priority (priority),
    INDEX idx_status (status),
    INDEX idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='插件注册表';


-- ============================================================
-- 5. 静态命令注册表
-- 插件通过 register(ctx) 注册的命令，1分钟心跳刷新
-- ============================================================
CREATE TABLE IF NOT EXISTS commands (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    plugin_name     VARCHAR(50)     NOT NULL        COMMENT '所属插件名称',
    pattern         VARCHAR(500)    NOT NULL        COMMENT '正则表达式模式',
    alias           VARCHAR(500)    DEFAULT NULL    COMMENT '命令别名（逗号分隔，如 /help,/h）',
    description     VARCHAR(500)    DEFAULT NULL    COMMENT '命令描述',
    priority        INT             DEFAULT 50      COMMENT '匹配优先级（越小越优先）',
    handler         VARCHAR(100)    NOT NULL        COMMENT '处理函数名',
    is_dynamic      TINYINT(1)      DEFAULT 0       COMMENT '是否为动态命令(1=心跳不清除)',
    require_level   VARCHAR(20)     DEFAULT ''       COMMENT '权限要求: admin=管理员/群主/超管, super=超管',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '启用/禁用',
    hit_count       INT             DEFAULT 0       COMMENT '命中次数统计',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP    COMMENT '注册时间',
    INDEX idx_plugin (plugin_name),
    INDEX idx_priority (priority),
    INDEX idx_active (is_active),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='静态命令注册表';


-- ============================================================
-- 6. 动态命令表
-- 运行时增删改，持久化存储，重启后自动恢复
-- ============================================================
CREATE TABLE IF NOT EXISTS dynamic_commands (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    keyword         VARCHAR(200)    NOT NULL        COMMENT '触发关键词',
    response        TEXT            NOT NULL        COMMENT '回复内容（支持CQ码）',
    match_type      ENUM('exact','prefix','contains','regex')  DEFAULT 'exact'  COMMENT '匹配方式',
    handler         VARCHAR(100)    DEFAULT ''      COMMENT 'handler回调 plugin:func',
    plugin_name     VARCHAR(50)     DEFAULT 'system' COMMENT '所属插件/system表示系统内置',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '启用/禁用',
    hit_count       INT             DEFAULT 0       COMMENT '命中次数',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP    COMMENT '创建时间',
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_keyword (keyword),
    INDEX idx_plugin (plugin_name),
    INDEX idx_active (is_active),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='动态命令表';


-- ============================================================
-- 7. 定时任务注册表
-- 插件通过 ctx.task() 注册的 cron 定时任务
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    plugin_name     VARCHAR(50)     NOT NULL        COMMENT '所属插件名称',
    cron_expression VARCHAR(50)     NOT NULL        COMMENT 'cron表达式',
    handler         VARCHAR(100)    NOT NULL        COMMENT '执行函数名',
    description     VARCHAR(500)    DEFAULT NULL    COMMENT '任务描述',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '启用/禁用',
    last_run_at     DATETIME        DEFAULT NULL    COMMENT '上次执行时间',
    next_run_at     DATETIME        DEFAULT NULL    COMMENT '下次执行时间',
    run_count       INT             DEFAULT 0       COMMENT '总执行次数',
    last_status     VARCHAR(20)     DEFAULT NULL    COMMENT '上次执行状态(success/error)',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_plugin (plugin_name),
    INDEX idx_active (is_active),
    INDEX idx_next_run (next_run_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='定时任务注册表';


-- ============================================================
-- 8. 管理员账号表
-- Web UI 登录认证
-- ============================================================
CREATE TABLE IF NOT EXISTS admin_users (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    username        VARCHAR(50)     NOT NULL        COMMENT '用户名',
    password_hash   VARCHAR(255)    NOT NULL        COMMENT '密码哈希(bcrypt)',
    token           VARCHAR(2048)   DEFAULT NULL    COMMENT '登录令牌(2048位随机)',
    token_created_at DATETIME       DEFAULT NULL    COMMENT '令牌签发时间',
    role            ENUM('super','admin')           DEFAULT 'admin'  COMMENT '角色',
    is_active       TINYINT(1)      DEFAULT 1       COMMENT '启用/禁用',
    last_login_at   DATETIME        DEFAULT NULL    COMMENT '最后登录时间',
    last_login_ip   VARCHAR(45)     DEFAULT NULL    COMMENT '最后登录IP',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='管理员账号表';


-- ============================================================
-- 9. 审计日志表
-- 记录所有管理操作
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id              BIGINT          AUTO_INCREMENT  PRIMARY KEY,
    admin_id        INT             DEFAULT NULL    COMMENT '操作管理员ID',
    admin_name      VARCHAR(50)     DEFAULT NULL    COMMENT '操作管理员名',
    action          VARCHAR(50)     NOT NULL        COMMENT '操作类型',
    target_type     VARCHAR(50)     DEFAULT NULL    COMMENT '操作对象类型(plugin/command/user/group)',
    target_name     VARCHAR(200)    DEFAULT NULL    COMMENT '操作对象名称',
    detail          TEXT            DEFAULT NULL    COMMENT '操作详情(JSON)',
    ip_address      VARCHAR(45)     DEFAULT NULL    COMMENT '来源IP',
    result          ENUM('success','failure')       DEFAULT 'success'  COMMENT '操作结果',
    error_message   TEXT            DEFAULT NULL    COMMENT '错误信息',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_admin (admin_id),
    INDEX idx_action (action),
    INDEX idx_target (target_type, target_name),
    INDEX idx_created (created_at),
    INDEX idx_result (result)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='审计日志表';


-- ============================================================
-- 10. 插件配置表
-- 存储插件通过 _conf_schema.json 定义的配置项值（参考 AstrBot）
-- 用户可在 Web UI 修改，插件通过 ctx.get_config() 读取
-- ============================================================
CREATE TABLE IF NOT EXISTS plugin_configs (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    plugin_name     VARCHAR(50)     NOT NULL        COMMENT '所属插件名称',
    config_key      VARCHAR(100)    NOT NULL        COMMENT '配置键名',
    config_value    TEXT            DEFAULT NULL    COMMENT '配置值(JSON)',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_plugin_key (plugin_name, config_key),
    INDEX idx_plugin (plugin_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='插件配置表';


-- ============================================================
-- 11. 系统配置表
-- 框架的全局配置，支持动态修改
-- ============================================================
CREATE TABLE IF NOT EXISTS system_config (
    id              INT             AUTO_INCREMENT  PRIMARY KEY,
    config_key      VARCHAR(100)    NOT NULL        COMMENT '配置键',
    config_value    TEXT            NOT NULL        COMMENT '配置值(JSON)',
    description     VARCHAR(500)    DEFAULT NULL    COMMENT '配置说明',
    updated_by      VARCHAR(50)     DEFAULT NULL    COMMENT '最后修改者',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_config_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='系统配置表';


-- ============================================================
-- IP 黑名单表（蜜罐自动拉黑 + 手动拉黑）
-- 内网 IP（127.*、192.168.*、172.16-31.*）由框架逻辑豁免，不写入本表
-- ============================================================
CREATE TABLE IF NOT EXISTS ip_blacklist (
    id          INT             AUTO_INCREMENT  PRIMARY KEY,
    ip          VARCHAR(64)     NOT NULL        COMMENT '被封禁的 IP',
    reason      VARCHAR(255)    DEFAULT NULL    COMMENT '拉黑原因',
    source      VARCHAR(32)     DEFAULT 'manual' COMMENT '来源: honeypot=蜜罐自动/manual=手动',
    expires_at  DATETIME        DEFAULT NULL    COMMENT '解封时间(NULL=永久)',
    created_at  DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ip (ip)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='IP 黑名单表';


-- ============================================================
-- 插入默认数据
-- ============================================================

-- 默认管理员账号（密码需要在首次启动时强制修改）
-- 默认密码: admin123 （生产环境务必修改）
INSERT INTO admin_users (username, password_hash, role) VALUES
    ('admin', '$2b$12$YsniVDvsFqQU0ENEUQNVhuVqpbr/e03SBWLSEcaUFkmeQzOaMujpq', 'super');

-- 默认系统配置
INSERT INTO system_config (config_key, config_value, description) VALUES
    ('framework.name', '"ZCBOT OneBot Bot"', '框架名称'),
    ('framework.version', '"1.0.0"', '框架版本'),
    ('framework.port', '8080', 'Web UI 监听端口'),
    ('framework.host', '0.0.0.0', 'Web UI 绑定地址'),
    ('onebot.ws_url', '"ws://127.0.0.1:6700"', 'OneBot WebSocket 地址'),
    ('onebot.access_token', '""', 'OneBot 访问令牌'),
    ('plugin.max_memory_mb', '64', '单插件内存上限(MB)'),
    ('plugin.heartbeat_interval', '60', '插件注册心跳间隔(秒)'),
    ('log.level', '"INFO"', '日志级别'),
    ('log.retention_days', '30', '日志保留天数'),
    ('web.session_timeout', '3600', 'Web UI 会话超时(秒)');