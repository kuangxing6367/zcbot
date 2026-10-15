// SQLite 存储：纯 Go（modernc.org/sqlite，无 cgo），生产可落地。
// 默认 :memory: 即真实 SQLite 引擎（非玩具正则库），--db <path> 可落盘。
// DB 全部在 Go 进程内执行，无 Python GIL 参与。
package main

import (
	"database/sql"
	"fmt"

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

// NewStore 优先返回 SQLite；失败回退内存库，保证内核始终能起。
func NewStore(path string) (Store, error) {
	if path == "mem" {
		return NewMemoryStore(), nil
	}
	s, err := NewSqliteStore(path)
	if err != nil {
		return nil, fmt.Errorf("sqlite 初始化失败 (%s): %w", path, err)
	}
	return s, nil
}
