// 兜底回归测试：部分表丢失自愈 / 运行中卷变只读。
//
// 只读用 `PRAGMA query_only=ON` 模拟：与只读挂载、只读文件属性在 SQLite 内核层
// 返回同一个错误（SQLITE_READONLY, "attempt to write a readonly database"），
// 因此走的是同一条代码路径。
package main

import "testing"

// TestEnsureSchemaSelfHealsPartialLoss 锁死：只丢部分表时，再次初始化必须补齐。
// 历史缺陷是逐条建表失败只 logWarn、末尾仍打"核心表结构就绪"，缺表无法暴露。
func TestEnsureSchemaSelfHealsPartialLoss(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败: %v", err)
	}
	defer s.db.Close()

	EnsureSchema(s)
	if got := missingSchemaTables(s); len(got) != 0 {
		t.Fatalf("首次建表后不应缺表，实际缺: %v", got)
	}

	for _, tbl := range []string{"group_members", "perm_audit", "group_plugin_settings"} {
		if _, err := s.Exec("DROP TABLE " + tbl); err != nil {
			t.Fatalf("DROP %s 失败: %v", tbl, err)
		}
	}
	if got := missingSchemaTables(s); len(got) != 3 {
		t.Fatalf("应有 3 张表缺失，实际: %v", got)
	}

	// 重启语义：再跑一次初始化
	EnsureSchema(s)

	if got := missingSchemaTables(s); len(got) != 0 {
		t.Fatalf("复验失败，仍缺表: %v", got)
	}
	// 默认权限组必须仍然只有一行（INSERT OR IGNORE 幂等）
	rows, err := s.Query("SELECT name FROM perm_groups WHERE name = ?", "default")
	if err != nil || len(rows) != 1 {
		t.Fatalf("默认权限组应恰好 1 行，实际 %d（err=%v）", len(rows), err)
	}
}

// TestReadOnlyDetectedAndRecovered 锁死：只读时写必须报错并被识别，读仍可用，
// 卷恢复后写必须恢复且清掉只读标记。
func TestReadOnlyDetectedAndRecovered(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败: %v", err)
	}
	defer s.db.Close()
	EnsureSchema(s)

	if _, err := s.Exec("PRAGMA query_only = ON"); err != nil {
		t.Fatalf("开启只读模拟失败: %v", err)
	}

	if _, err := s.Insert("INSERT INTO plugins(name) VALUES(?)", "ro_probe"); err == nil {
		t.Fatal("只读状态下写入应返回错误，不能静默成功")
	} else if !isReadOnlyErr(err) {
		t.Fatalf("错误未被识别为只读: %v", err)
	}

	// 读操作必须照常可用（只读卷不等于数据库不可用）
	if _, err := s.Query("SELECT COUNT(*) AS c FROM plugins"); err != nil {
		t.Fatalf("只读状态下读应可用: %v", err)
	}

	if !s.ReadOnly() {
		t.Fatal("只读失败后应标记 ReadOnly=true，供健康检查暴露")
	}

	if _, err := s.Exec("PRAGMA query_only = OFF"); err != nil {
		t.Fatalf("关闭只读模拟失败: %v", err)
	}
	if _, err := s.Insert("INSERT INTO plugins(name) VALUES(?)", "rw_probe"); err != nil {
		t.Fatalf("恢复可写后写入应成功: %v", err)
	}
	if s.ReadOnly() {
		t.Fatal("恢复可写后应清除只读标记")
	}
}

// TestEnsureSchemaReportsMissingOnReadOnlyDB 锁死：空库 + 只读时初始化必须留下
// 明确的"缺表"事实（供上层打 error），而不是看起来"建过了"。
func TestEnsureSchemaReportsMissingOnReadOnlyDB(t *testing.T) {
	s, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败: %v", err)
	}
	defer s.db.Close()

	if _, err := s.Exec("PRAGMA query_only = ON"); err != nil {
		t.Fatalf("开启只读模拟失败: %v", err)
	}

	EnsureSchema(s)

	missing := missingSchemaTables(s)
	if len(missing) == 0 {
		t.Fatal("只读空库不可能建出表，missingSchemaTables 必须报缺失")
	}
	if len(missing) != len(coreSchemaTableNames) {
		t.Fatalf("应全部缺失（%d 张），实际缺 %d 张: %v",
			len(coreSchemaTableNames), len(missing), missing)
	}
	if !s.ReadOnly() {
		t.Fatal("建表失败应让 store 进入只读标记状态")
	}
}
