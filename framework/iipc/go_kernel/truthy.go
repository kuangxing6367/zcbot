// util 层：真值判定小工具——把配置/DB 里可能出现的 "true"/"1"/"yes"/"是" 等归一到 bool。
package main

// truthy 把 DB 取值（0/1、true/false、"0"/"1"）归一为 bool。
// SQLite 动态类型 + JSON 数字都可能出现，统一收敛避免权限/开关判断漏判。
func truthy(v interface{}) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		switch x {
		case "", "0", "false", "False", "no":
			return false
		}
		return true
	case []byte:
		return truthy(string(x))
	default:
		return false
	}
}
