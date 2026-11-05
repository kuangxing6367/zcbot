// 老库补列回归：CREATE TABLE IF NOT EXISTS 对已存在的表是 no-op，
// 往 DDL 里加列只对新库生效。这里模拟一个「已存在但缺新列」的 admin_users，
// 锁死 ensureColumns 能把它补齐、且重复执行幂等（SQLite 的
// ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS，重复执行会报 duplicate column）。
package main

import "testing"

func hasColumn(t *testing.T, s Store, table, column string) bool {
	t.Helper()
	rows, err := s.Query("SELECT name FROM pragma_table_info('" + table + "')")
	if err != nil {
		t.Fatalf("探测 %s 列失败: %v", table, err)
	}
	for _, r := range rows {
		if name, ok := r["name"].(string); ok && name == column {
			return true
		}
	}
	return false
}

func TestEnsureColumnsPatchesLegacyAdminUsers(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败: %v", err)
	}
	defer s.db.Close()

	// 老库结构：admin_users 没有 token_hash
	if _, err := s.Exec(`CREATE TABLE admin_users (
		id                INTEGER PRIMARY KEY AUTOINCREMENT,
		username          TEXT NOT NULL,
		password_hash     TEXT NOT NULL,
		token             TEXT DEFAULT NULL,
		token_created_at  TEXT DEFAULT NULL,
		role              TEXT DEFAULT 'admin',
		is_active         INTEGER DEFAULT 1,
		last_login_at     TEXT DEFAULT NULL,
		last_login_ip     TEXT DEFAULT NULL,
		created_at        TEXT DEFAULT NULL,
		updated_at        TEXT DEFAULT NULL,
		UNIQUE (username)
	)`); err != nil {
		t.Fatalf("建老表失败: %v", err)
	}
	if hasColumn(t, s, "admin_users", "token_hash") {
		t.Fatal("前置条件不成立：老表不应有 token_hash")
	}

	EnsureSchema(s)

	if !hasColumn(t, s, "admin_users", "token_hash") {
		t.Fatal("EnsureSchema 后 admin_users 仍缺 token_hash：老库升级后 Web 鉴权会 no such column")
	}
	if got := missingSchemaTables(s); len(got) != 0 {
		t.Fatalf("复验后不应缺表，实际: %v", got)
	}

	// 幂等：重复初始化不能因为 duplicate column 把补列算成失败
	EnsureSchema(s)
	EnsureSchema(s)
	if !hasColumn(t, s, "admin_users", "token_hash") {
		t.Fatal("重复初始化后 token_hash 消失")
	}

	// 索引必须建上（补列排在建索引之前，否则索引会 no such column）
	rows, err := s.Query(
		"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_admin_token_hash'")
	if err != nil || len(rows) != 1 {
		t.Fatalf("idx_admin_token_hash 应存在，实际 %d 行（err=%v）", len(rows), err)
	}

	// 老行还在（补列不能重建表）
	if _, err := s.Insert(
		"INSERT INTO admin_users(username, password_hash) VALUES(?, ?)",
		"legacy", "x"); err != nil {
		t.Fatalf("补列后写入失败: %v", err)
	}
}

func TestEnsureColumnsNoopOnFreshDB(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败: %v", err)
	}
	defer s.db.Close()

	EnsureSchema(s)
	added, failed := ensureColumns(s) // 新库已由 DDL 带列，应为 no-op
	if added != 0 || failed != 0 {
		t.Fatalf("新库补列应为 no-op，实际 added=%d failed=%d", added, failed)
	}
}
