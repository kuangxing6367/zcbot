// 核心表结构（SQLite 方言）：与 python_side/sql/init.sql（MySQL 5.7）保持列名一致，
// 让 zcbot 插件里按 MySQL 习惯写的 SQL 经 TranslateSQL 翻译后直接可跑。
// 只建「插件运行时真正会碰」的表；WebUI 专属表（api_tokens 等）也一并建，
// 保证 1:1 数据面完整，后续 Web 等价物可直接复用。
package main

// coreSchemaDDL 按依赖顺序排列（被引用的字典表在前）。
var coreSchemaDDL = []string{
	`CREATE TABLE IF NOT EXISTS users (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id         TEXT NOT NULL,
		nickname        TEXT DEFAULT NULL,
		avatar_url      TEXT DEFAULT NULL,
		is_friend       INTEGER DEFAULT 0,
		is_blacklist    INTEGER DEFAULT 0,
		role            TEXT DEFAULT '',
		remark          TEXT DEFAULT NULL,
		first_seen_at   TEXT DEFAULT NULL,
		last_active_at  TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (user_id)
	)`,
	`CREATE TABLE IF NOT EXISTS groups_info (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		group_id        TEXT NOT NULL,
		group_name      TEXT DEFAULT NULL,
		member_count    INTEGER DEFAULT 0,
		max_member_count INTEGER DEFAULT 0,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (group_id)
	)`,
	`CREATE TABLE IF NOT EXISTS group_members (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		group_id        TEXT NOT NULL,
		user_id         TEXT NOT NULL,
		card            TEXT DEFAULT NULL,
		role            TEXT DEFAULT 'member',
		title           TEXT DEFAULT NULL,
		join_time       INTEGER DEFAULT 0,
		is_muted        INTEGER DEFAULT 0,
		mute_until      TEXT DEFAULT NULL,
		last_active_at  TEXT DEFAULT NULL,
		message_count   INTEGER DEFAULT 0,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (group_id, user_id)
	)`,
	`CREATE TABLE IF NOT EXISTS plugins (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		name            TEXT NOT NULL,
		version         TEXT DEFAULT NULL,
		author          TEXT DEFAULT NULL,
		description     TEXT DEFAULT NULL,
		enabled         INTEGER DEFAULT 1,
		priority        INTEGER DEFAULT 50,
		installed_at    TEXT DEFAULT NULL,
		UNIQUE (name)
	)`,
	`CREATE TABLE IF NOT EXISTS commands (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		plugin_name     TEXT NOT NULL,
		pattern         TEXT NOT NULL,
		alias           TEXT DEFAULT NULL,
		description     TEXT DEFAULT NULL,
		priority        INTEGER DEFAULT 50,
		handler         TEXT NOT NULL,
		is_dynamic      INTEGER DEFAULT 0,
		require_level   TEXT DEFAULT '',
		require_perm    TEXT DEFAULT '',
		is_active       INTEGER DEFAULT 1,
		hit_count       INTEGER DEFAULT 0,
		created_at      TEXT DEFAULT NULL,
		UNIQUE (plugin_name, handler)
	)`,
	`CREATE TABLE IF NOT EXISTS dynamic_commands (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		keyword         TEXT NOT NULL,
		response        TEXT NOT NULL,
		match_type      TEXT DEFAULT 'exact',
		handler         TEXT DEFAULT '',
		plugin_name     TEXT DEFAULT NULL,
		enabled         INTEGER DEFAULT 1,
		hit_count       INTEGER DEFAULT 0,
		created_by      TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS tasks (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		plugin_name     TEXT NOT NULL,
		cron_expression TEXT NOT NULL,
		handler         TEXT NOT NULL,
		description     TEXT DEFAULT NULL,
		enabled         INTEGER DEFAULT 1,
		last_run_at     TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		UNIQUE (plugin_name, handler)
	)`,
	`CREATE TABLE IF NOT EXISTS admin_users (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		username        TEXT NOT NULL,
		password_hash   TEXT NOT NULL,
		token           TEXT DEFAULT NULL,
		token_created_at TEXT DEFAULT NULL,
		role            TEXT DEFAULT 'admin',
		is_active       INTEGER DEFAULT 1,
		last_login_at   TEXT DEFAULT NULL,
		last_login_ip   TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (username)
	)`,
	`CREATE TABLE IF NOT EXISTS audit_logs (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		admin_id        INTEGER DEFAULT NULL,
		admin_name      TEXT DEFAULT NULL,
		action          TEXT NOT NULL,
		target_type     TEXT DEFAULT NULL,
		target_name     TEXT DEFAULT NULL,
		detail          TEXT DEFAULT NULL,
		ip_address      TEXT DEFAULT NULL,
		result          TEXT DEFAULT 'success',
		error_message   TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS plugin_configs (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		plugin_name     TEXT NOT NULL,
		config_key      TEXT NOT NULL,
		config_value    TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (plugin_name, config_key)
	)`,
	`CREATE TABLE IF NOT EXISTS system_config (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		config_key      TEXT NOT NULL,
		config_value    TEXT DEFAULT NULL,
		description     TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (config_key)
	)`,
	`CREATE TABLE IF NOT EXISTS ip_blacklist (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		ip              TEXT NOT NULL,
		reason          TEXT DEFAULT NULL,
		source          TEXT DEFAULT 'manual',
		expires_at      TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (ip)
	)`,
	`CREATE TABLE IF NOT EXISTS perm_groups (
		name            TEXT NOT NULL,
		display_name    TEXT DEFAULT NULL,
		weight          INTEGER DEFAULT 0,
		prefix          TEXT DEFAULT NULL,
		suffix          TEXT DEFAULT NULL,
		is_default      INTEGER DEFAULT 0,
		created_at      TEXT DEFAULT NULL,
		PRIMARY KEY (name)
	)`,
	`CREATE TABLE IF NOT EXISTS perm_group_nodes (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		group_name      TEXT NOT NULL,
		node            TEXT NOT NULL,
		value           INTEGER DEFAULT 1,
		context_key     TEXT DEFAULT NULL,
		context_val     TEXT DEFAULT NULL,
		expire_at       TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS perm_user_nodes (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id         TEXT NOT NULL,
		node            TEXT NOT NULL,
		value           INTEGER DEFAULT 1,
		context_key     TEXT DEFAULT NULL,
		context_val     TEXT DEFAULT NULL,
		expire_at       TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS perm_tracks (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id         TEXT NOT NULL,
		group_name      TEXT NOT NULL,
		context_key     TEXT DEFAULT NULL,
		context_val     TEXT DEFAULT NULL,
		expire_at       TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS perm_audit (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		operator        TEXT DEFAULT NULL,
		action          TEXT DEFAULT NULL,
		target_type     TEXT DEFAULT NULL,
		target_name     TEXT DEFAULT NULL,
		detail          TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL
	)`,
	`CREATE TABLE IF NOT EXISTS api_tokens (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		name            TEXT DEFAULT NULL,
		token_hash      TEXT NOT NULL,
		scope           TEXT DEFAULT NULL,
		expires_at      TEXT DEFAULT NULL,
		created_at      TEXT DEFAULT NULL,
		last_used_at    TEXT DEFAULT NULL,
		UNIQUE (token_hash)
	)`,
	`CREATE TABLE IF NOT EXISTS group_plugin_settings (
		id              INTEGER PRIMARY KEY AUTOINCREMENT,
		group_id        TEXT NOT NULL,
		plugin_name     TEXT NOT NULL,
		enabled         INTEGER DEFAULT 1,
		updated_at      TEXT DEFAULT NULL,
		UNIQUE (group_id, plugin_name)
	)`,
}

// coreSchemaIndexes：SQLite 不支持表内联 INDEX，单独补（与 init.sql 的热点索引对齐）。
var coreSchemaIndexes = []string{
	`CREATE INDEX IF NOT EXISTS idx_gm_user ON group_members(user_id)`,
	`CREATE INDEX IF NOT EXISTS idx_gm_role ON group_members(role)`,
	`CREATE INDEX IF NOT EXISTS idx_cmd_plugin ON commands(plugin_name)`,
	`CREATE INDEX IF NOT EXISTS idx_cmd_priority ON commands(priority)`,
	`CREATE INDEX IF NOT EXISTS idx_cfg_plugin ON plugin_configs(plugin_name)`,
	`CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)`,
	`CREATE INDEX IF NOT EXISTS idx_pgn_group ON perm_group_nodes(group_name)`,
	`CREATE INDEX IF NOT EXISTS idx_pun_user ON perm_user_nodes(user_id)`,
	`CREATE INDEX IF NOT EXISTS idx_gps_group ON group_plugin_settings(group_id)`,
}

// coreSchemaTableNames 与 coreSchemaDDL 一一对应（同序），用于建表后的存在性复验。
var coreSchemaTableNames = []string{
	"users", "groups_info", "group_members", "plugins", "commands",
	"dynamic_commands", "tasks", "admin_users", "audit_logs",
	"plugin_configs", "system_config", "ip_blacklist", "perm_groups",
	"perm_group_nodes", "perm_user_nodes", "perm_tracks", "perm_audit",
	"api_tokens", "group_plugin_settings",
}

// missingSchemaTables 复验核心表是否真实存在。
// 只读卷 / 权限不足 / 磁盘满时 Exec 会报错，但上层很容易只记一条 warn 继续跑；
// 用 sqlite_master 实查一遍才能把"表真的没建出来"暴露成 error。
func missingSchemaTables(store Store) []string {
	var missing []string
	for _, t := range coreSchemaTableNames {
		rows, err := store.Query(
			"SELECT name FROM sqlite_master WHERE type='table' AND name = ?", t)
		if err != nil || len(rows) == 0 {
			missing = append(missing, t)
		}
	}
	return missing
}

// EnsureSchema 建齐核心表（幂等）。失败仅告警不致命：插件可自行建自己的表。
func EnsureSchema(store Store) {
	var ddlFailed, idxFailed int
	firstErr := ""
	for _, ddl := range coreSchemaDDL {
		if _, err := store.Exec(ddl); err != nil {
			ddlFailed++
			if firstErr == "" {
				firstErr = err.Error()
			}
		}
	}
	for _, idx := range coreSchemaIndexes {
		if _, err := store.Exec(idx); err != nil {
			idxFailed++
			if firstErr == "" {
				firstErr = err.Error()
			}
		}
	}
	// 播种默认权限组（对齐框架 _seed_default_group：is_default=1 全员自动拥有）
	seedFailed := 0
	if _, err := store.Exec(
		"INSERT OR IGNORE INTO perm_groups(name, display_name, weight, prefix, suffix, is_default, created_at) " +
			"VALUES('default','默认组',0,NULL,NULL,1,strftime('%s','now'))"); err != nil {
		seedFailed++
		if firstErr == "" {
			firstErr = err.Error()
		}
	}

	// 复验：建表语句"执行过"不等于"建成了"。历史实现逐条 logWarn 后仍无条件打
	// "核心表结构就绪"，只读卷/权限不足时等于对外宣称成功——必须查实际存在性。
	missing := missingSchemaTables(store)
	if len(missing) > 0 {
		logError("db", "核心表结构不完整：仍缺失 %d 张表 %v（建表失败 %d 条 / 索引失败 %d 条 / 默认组播种失败 %d，首条错误：%s）。"+
			"相关功能写库会失败，请检查磁盘是否只读、建表权限与剩余空间。",
			len(missing), missing, ddlFailed, idxFailed, seedFailed, firstErr)
		return
	}
	if ddlFailed > 0 || idxFailed > 0 || seedFailed > 0 {
		// 表齐全但索引/播种失败：功能可用，但索引缺失会拖慢查询，仍需可见
		logWarn("db", "核心表均已存在，但初始化中有 %d 条建表 / %d 条索引 / %d 条播种失败（首条错误：%s）",
			ddlFailed, idxFailed, seedFailed, firstErr)
		return
	}
	logInfo("db", "核心表结构就绪（%d 表 / %d 索引）", len(coreSchemaDDL), len(coreSchemaIndexes))
}

// SeedSuperAdmins 把 --admin 指定的 user_id 同步为 users.role='super'
// （对齐原框架"超管在 config.yaml 中配置，自动同步到 users.role"的语义）。
func SeedSuperAdmins(store Store, admins map[string]bool) {
	for uid := range admins {
		if _, err := store.Exec(
			"INSERT INTO users(user_id, role, created_at) VALUES(?, 'super', datetime('now')) "+
				"ON CONFLICT(user_id) DO UPDATE SET role='super'", uid); err != nil {
			logWarn("db", "同步超管 %s 失败: %v", uid, err)
		}
	}
	if len(admins) > 0 {
		logInfo("db", "超管已同步 users.role=super: %v", adminList(admins))
	}
}
