// store 层：SQLite 存储层单元测试。
package main

import "testing"

// TestNewStoreMemAlias 锁死回归：mem / :memory: / 空 必须落到真 SQLite 引擎，
// 而不是那个"只支持极简 SQL 子集的玩具正则内存库"（它会让建表/建索引静默失败）。
func TestNewStoreMemAlias(t *testing.T) {
	for _, p := range []string{"mem", ":memory:", ""} {
		s, err := NewStore(p)
		if err != nil {
			t.Fatalf("NewStore(%q) 失败: %v", p, err)
		}
		if _, ok := s.(*sqliteStore); !ok {
			t.Fatalf("NewStore(%q) 未返回 sqliteStore（退回了玩具库？）: %T", p, s)
		}
		// 以下语句玩具正则库全部不支持 → 若回退会直接报错或静默失败
		if _, err := s.Exec("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)"); err != nil {
			t.Fatalf("NewStore(%q) 建表失败（玩具库症状）: %v", p, err)
		}
		if _, err := s.Exec("CREATE INDEX IF NOT EXISTS idx_v ON t(v)"); err != nil {
			t.Fatalf("NewStore(%q) 建索引失败（玩具库症状）: %v", p, err)
		}
		if _, err := s.Insert("INSERT INTO t(v) VALUES(?)", "hello"); err != nil {
			t.Fatalf("NewStore(%q) 插入失败: %v", p, err)
		}
		rows, err := s.Query("SELECT v FROM t ORDER BY id DESC")
		if err != nil {
			t.Fatalf("NewStore(%q) ORDER BY 查询失败（玩具库症状）: %v", p, err)
		}
		if len(rows) != 1 || rows[0]["v"] != "hello" {
			t.Fatalf("NewStore(%q) 查询结果异常: %v", p, rows)
		}
	}
}

// TestNewStoreFile 锁死：NewSqliteStore 返回具体 *sqliteStore（非玩具库），可直接使用。
func TestNewStoreFile(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("NewSqliteStore(:memory:) 失败: %v", err)
	}
	if s == nil {
		t.Fatalf("NewSqliteStore 返回 nil")
	}
}
