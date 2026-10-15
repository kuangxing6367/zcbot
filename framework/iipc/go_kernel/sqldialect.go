// SQL 方言翻译：zcbot 插件按 MySQL 习惯写 %s 占位符，SQLite 只认 ?。
// 直接把字符串 replace 掉会误伤字符串字面量里的 %s（如 LIKE '%s%' 其实是
// LIKE '%<关键字>%' 的误写，但 LIKE '%abc%' 完全合法），因此必须逐字符扫描、
// 跳过字符串字面量后再替换。
//
// 规则（与 framework/database 的 MySQL 侧语义对齐）：
//   - 字面量外部的 %s        → ?
//   - 字面量外部的 %%        → %（MySQL 转义）
//   - 已是 ? 的保持不变（SQLite 原生写法也可用）
//   - 字面量内部（'...'，'' 为转义引号）一律不动
package main

import "strings"

// TranslateSQL 把 MySQL 风格 SQL 翻译为 SQLite 可执行 SQL。
func TranslateSQL(sql string) string {
	// 快速路径：既无 % 也无 %s 形态，直接返回。
	if !strings.ContainsRune(sql, '%') {
		return sql
	}
	var b strings.Builder
	b.Grow(len(sql))
	inStr := false // 是否处于 '...' 字面量内
	for i := 0; i < len(sql); i++ {
		ch := sql[i]
		if inStr {
			b.WriteByte(ch)
			if ch == '\'' {
				// '' 是字面量内的转义引号，继续留在字面量内
				if i+1 < len(sql) && sql[i+1] == '\'' {
					b.WriteByte('\'')
					i++
					continue
				}
				inStr = false
			}
			continue
		}
		switch ch {
		case '\'':
			inStr = true
			b.WriteByte(ch)
		case '%':
			if i+1 < len(sql) && sql[i+1] == '%' {
				b.WriteByte('%') // %% → %
				i++
				continue
			}
			if i+1 < len(sql) && sql[i+1] == 's' {
				b.WriteByte('?') // %s → ?
				i++
				continue
			}
			b.WriteByte(ch) // 孤立 %（如 LIKE '%' 结尾）原样保留
		default:
			b.WriteByte(ch)
		}
	}
	return b.String()
}
