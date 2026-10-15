// DB 层：用 Go 实现，规避 Python GIL。
// 演示用内存 KV（支持极简 SQL 子集：CREATE/INSERT/SELECT 于单表 kv），
// 生产可替换为 modernc.org/sqlite（纯 Go、无 cgo、Windows 兼容），接口不变。
package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

// Store 是 DB 抽象，DB 全部在 Go 进程内执行。
type Store interface {
	Query(sql string, args ...interface{}) ([]map[string]interface{}, error)
	Exec(sql string, args ...interface{}) (int64, error)
	Insert(sql string, args ...interface{}) (int64, error) // INSERT 专用：返回 last insert id
	GetConfig(key string) (string, error)
	SetConfig(key, value string) error
}

type memoryStore struct {
	mu     sync.Mutex
	tables map[string][]map[string]interface{}
	cols   map[string][]string
	kv     map[string]string
	seq    int64
}

func NewMemoryStore() *memoryStore {
	return &memoryStore{tables: make(map[string][]map[string]interface{}), cols: make(map[string][]string), kv: make(map[string]string)}
}

func (s *memoryStore) GetConfig(key string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if v, ok := s.kv[key]; ok {
		return v, nil
	}
	return "", nil
}

func (s *memoryStore) SetConfig(key, value string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.kv[key] = value
	return nil
}

var (
	reCreate = regexp.MustCompile(`(?i)CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.+)\)`)
	reInsert = regexp.MustCompile(`(?i)INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\((.+)\)`)
	reSelect = regexp.MustCompile(`(?i)SELECT\s+(.+?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(\w+)\s*=\s*\?)?`)
)

func (s *memoryStore) Exec(sql string, args ...interface{}) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if m := reCreate.FindStringSubmatch(sql); m != nil {
		name := m[1]
		if _, ok := s.tables[name]; !ok {
			cols := []string{}
			for _, c := range strings.Split(m[2], ",") {
				fields := strings.Fields(c)
				if len(fields) > 0 {
					cols = append(cols, strings.TrimSpace(fields[0]))
				}
			}
			s.tables[name] = []map[string]interface{}{}
			s.cols[name] = cols
		}
		return 0, nil
	}
	if m := reInsert.FindStringSubmatch(sql); m != nil {
		name := m[1]
		colNames := splitCols(m[2])
		valStrs := splitVals(m[3])
		row := map[string]interface{}{}
		ai := 0
		for i, cn := range colNames {
			if i < len(valStrs) {
				row[cn] = resolveVal(valStrs[i], args, &ai)
			}
		}
		s.tables[name] = append(s.tables[name], row)
		s.seq++
		return s.seq, nil
	}
	return 0, fmt.Errorf("unsupported exec sql: %s", sql)
}

// Insert INSERT 专用：返回自增 id（memoryStore 的 seq 即 lastrowid 语义）。
func (s *memoryStore) Insert(sql string, args ...interface{}) (int64, error) {
	return s.Exec(sql, args...)
}

func (s *memoryStore) Query(sql string, args ...interface{}) ([]map[string]interface{}, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := reSelect.FindStringSubmatch(sql)
	if m == nil {
		return nil, fmt.Errorf("unsupported query sql: %s", sql)
	}
	name := m[2]
	rows, ok := s.tables[name]
	if !ok {
		return []map[string]interface{}{}, nil
	}
	var whereCol string
	if m[3] != "" {
		whereCol = m[3]
	}
	var arg0 interface{}
	if len(args) > 0 {
		arg0 = args[0]
	}
	out := []map[string]interface{}{}
	for _, r := range rows {
		if whereCol != "" && fmt.Sprintf("%v", r[whereCol]) != fmt.Sprintf("%v", arg0) {
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

func splitCols(s string) []string {
	out := []string{}
	for _, p := range strings.Split(s, ",") {
		out = append(out, strings.TrimSpace(p))
	}
	return out
}

func splitVals(s string) []string {
	out := []string{}
	depth := 0
	cur := strings.Builder{}
	for _, r := range s {
		switch r {
		case '\'':
			depth = (depth + 1) % 2
			cur.WriteRune(r)
		case ',':
			if depth == 0 {
				out = append(out, strings.TrimSpace(cur.String()))
				cur.Reset()
			} else {
				cur.WriteRune(r)
			}
		default:
			cur.WriteRune(r)
		}
	}
	if cur.Len() > 0 {
		out = append(out, strings.TrimSpace(cur.String()))
	}
	return out
}

func resolveVal(v string, args []interface{}, ai *int) interface{} {
	v = strings.TrimSpace(v)
	if v == "?" {
		if *ai < len(args) {
			val := args[*ai]
			*ai++
			return val
		}
		return nil
	}
	if strings.HasPrefix(v, "'") && strings.HasSuffix(v, "'") && len(v) >= 2 {
		return strings.Trim(v, "'")
	}
	if n, err := strconv.Atoi(v); err == nil {
		return n
	}
	return v
}
