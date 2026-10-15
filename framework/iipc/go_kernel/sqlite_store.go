// SQLite 存储：纯 Go（modernc.org/sqlite，无 cgo），生产可落地。
// 默认 :memory: 即真实 SQLite 引擎（非玩具正则库），--db <path> 可落盘。
// DB 全部在 Go 进程内执行，无 Python GIL 参与。
package main

import (
	"database/sql"

	_ "modernc.org/sqlite"
)

type sqliteStore struct {
	db *sql.DB
}

func NewSqliteStore(path string) (*sqliteStore, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// 单写连接：避免 sqlite 并发写 "database is locked"；读也复用它。
	// 真实高并发场景改用 WAL + 连接池，此处优先正确性。
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		return nil, err
	}
	s := &sqliteStore{db: db}
	// 配置持久化表：key → value（落盘或 :memory: 引擎内）
	if _, err := db.Exec("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)"); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *sqliteStore) GetConfig(key string) (string, error) {
	rows, err := s.Query("SELECT v FROM config WHERE k = ?", key)
	if err != nil {
		return "", err
	}
	if len(rows) == 0 {
		return "", nil
	}
	if v, ok := rows[0]["v"].(string); ok {
		return v, nil
	}
	return "", nil
}

func (s *sqliteStore) SetConfig(key, value string) error {
	_, err := s.db.Exec(
		"INSERT INTO config(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
		key, value,
	)
	return err
}

func (s *sqliteStore) Exec(sqlStr string, args ...interface{}) (int64, error) {
	res, err := s.db.Exec(sqlStr, args...)
	if err != nil {
		return 0, err
	}
	if n, err := res.RowsAffected(); err == nil && n > 0 {
		return n, nil
	}
	// CREATE / DDL 等不返回 affected 的语句：用 last insert id 兜底
	if id, err := res.LastInsertId(); err == nil && id > 0 {
		return id, nil
	}
	return 0, nil
}

// Insert INSERT 专用：返回 last insert id（框架 db_insert 的 lastrowid 语义）。
func (s *sqliteStore) Insert(sqlStr string, args ...interface{}) (int64, error) {
	res, err := s.db.Exec(sqlStr, args...)
	if err != nil {
		return 0, err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	return id, nil
}

func (s *sqliteStore) Query(sqlStr string, args ...interface{}) ([]map[string]interface{}, error) {
	rows, err := s.db.Query(sqlStr, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	out := []map[string]interface{}{}
	for rows.Next() {
		vals := make([]interface{}, len(cols))
		ptrs := make([]interface{}, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := map[string]interface{}{}
		for i, c := range cols {
			row[c] = normalizeSqliteVal(vals[i])
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

func normalizeSqliteVal(v interface{}) interface{} {
	switch x := v.(type) {
	case []byte:
		return string(x)
	default:
		return x
	}
}

var _ Store = (*sqliteStore)(nil)

// NewStore 返回存储实现。
// db 取值语义：
//   - "" / "mem" / ":memory:" → 真 SQLite 内存引擎（纯 Go、无 cgo），支持完整 DDL/DML；
//   - 其它值 → 该路径的 SQLite 文件（可落盘、可 WAL）。
//
// 历史上曾有一个"仅支持极简 SQL 子集的玩具正则内存库"作为 mem 模式后端，
// 但它无法执行 CREATE INDEX / ORDER BY / 多列查询，导致核心表与插件表静默建表失败，
// 表现为"内存更省"的功能残缺假象。现已移除：mem 与 :memory: 统一走真 SQLite 引擎。
func NewStore(path string) (Store, error) {
	if path == "" || path == "mem" || path == ":memory:" {
		return NewSqliteStore(":memory:")
	}
	return NewSqliteStore(path)
}
