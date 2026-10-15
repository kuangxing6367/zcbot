// DB 层抽象：DB 全部在 Go 进程内执行（modernc.org/sqlite，纯 Go、无 cgo）。
// 具体实现见 sqlite_store.go（sqliteStore）—— mem / :memory: 与文件路径均走真 SQLite，
// 支持完整 DDL/DML（CREATE TABLE / CREATE INDEX / 多列 WHERE / ORDER BY / 聚合等）。
package main

// Store 是 DB 抽象，DB 全部在 Go 进程内执行。
type Store interface {
	Query(sql string, args ...interface{}) ([]map[string]interface{}, error)
	Exec(sql string, args ...interface{}) (int64, error)
	Insert(sql string, args ...interface{}) (int64, error) // INSERT 专用：返回 last insert id
	GetConfig(key string) (string, error)
	SetConfig(key, value string) error
}
