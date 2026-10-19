// util 层：内核侧通用工具函数（字符串/路径/编码复用）。
package main

// strVal 把任意标量收敛为字符串（用于 user_id/self_id 等既可能是数字也可能是字符串的字段）。
func strVal(v interface{}) string {
	switch x := v.(type) {
	case string:
		return x
	case float64:
		return itoa(int64(x))
	case float32:
		return itoa(int64(x))
	case int:
		return itoa(int64(x))
	case int64:
		return itoa(x)
	default:
		return ""
	}
}

// itoa 快速 int64 → string（避免 import strconv 的反射开销；热路径友好）。
func itoa(i int64) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var buf [20]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}
