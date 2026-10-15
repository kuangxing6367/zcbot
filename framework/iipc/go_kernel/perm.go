// 权限系统（节点式）：port 自 framework/perm 的 resolve 模型，保持 1:1 行为。
//
// 模型要点（与原实现一致）：
//   - 用户直节点（perm_user_nodes）优先级最高；node 以 "group." 开头表示组归属
//   - 默认组（perm_groups.is_default）全员自动拥有
//   - 框架四层身份映射为内置继承链：
//       __member(w0) ← __admin(w20) ← __owner(w30) ← __super(w100)
//     各内置组自带 zcbot.role.{...} 节点
//   - 沿 group.xxx 做 BFS 继承展开（带环检测、显式否决剪枝）
//   - 合并顺序：用户直节点 → 各组按 weight 降序；先到先得（setdefault 语义）
//   - 未定义一律拒绝
package main

import (
	"sort"
	"strconv"
	"strings"
	"time"
)

// builtinGroup 内置角色组定义（对齐 framework/perm BUILTIN_GROUPS）。
type builtinGroup struct {
	weight   int
	inherits []string
	node     string
}

var builtinGroups = map[string]builtinGroup{
	"__member": {weight: 0, node: "zcbot.role.member"},
	"__admin":  {weight: 20, inherits: []string{"__member"}, node: "zcbot.role.admin"},
	"__owner":  {weight: 30, inherits: []string{"__admin"}, node: "zcbot.role.owner"},
	"__super":  {weight: 100, inherits: []string{"__owner"}, node: "zcbot.role.super"},
}

// roleToGroup 框架身份 → 内置组（对齐 ROLE_TO_GROUP）。
var roleToGroup = map[string]string{
	"member": "__member",
	"admin":  "__admin",
	"owner":  "__owner",
	"super":  "__super",
}

// permNodeRow 一条节点记录（用户直节点或组节点共用）。
type permNodeRow struct {
	Node       string
	Value      bool
	ContextKey string
	ContextVal string
	ExpireAt   string
}

func (r permNodeRow) alive(now int64) bool {
	if r.ExpireAt == "" {
		return true
	}
	ts, err := strconv.ParseInt(strings.TrimSpace(r.ExpireAt), 10, 64)
	if err != nil {
		return true // 解析不了按不过期处理（与原 _alive 一致）
	}
	return ts > now
}

// ctxMatch 上下文匹配：context_key 为空 → 全局生效；否则要求 context[key] == val。
func (r permNodeRow) ctxMatch(ctx map[string]string) bool {
	if r.ContextKey == "" {
		return true
	}
	v, ok := ctx[r.ContextKey]
	if !ok {
		return false
	}
	return v == r.ContextVal
}

// PermResolver 权限解析器（无缓存版：IIPC 单机场景权限变更要即时生效，
// 原框架的 60s TTL 缓存在 Web 改权限后会有延迟，这里选择正确性优先）。
type PermResolver struct {
	store Store
}

func NewPermResolver(store Store) *PermResolver { return &PermResolver{store: store} }

func (pr *PermResolver) queryNodes(sql string, args ...interface{}) []permNodeRow {
	rows, err := pr.store.Query(TranslateSQL(sql), args...)
	if err != nil {
		logWarn("perm", "查询失败: %v", err)
		return nil
	}
	out := make([]permNodeRow, 0, len(rows))
	for _, r := range rows {
		out = append(out, permNodeRow{
			Node:       strVal(r["node"]),
			Value:      truthy(r["value"]),
			ContextKey: strVal(r["context_key"]),
			ContextVal: strVal(r["context_val"]),
			ExpireAt:   strVal(r["expire_at"]),
		})
	}
	return out
}

// HasPerm 单点权限判断（未定义按拒绝）。
func (pr *PermResolver) HasPerm(userID, node string, ctx map[string]string, role string) bool {
	merged := pr.resolve(userID, ctx, role)
	v, ok := merged[strings.ToLower(strings.TrimSpace(node))]
	return ok && v
}

// UserGroups 返回用户生效权限组（weight 降序，不含内置 __ 组的顺序保证）。
func (pr *PermResolver) UserGroups(userID string, ctx map[string]string, role string) []string {
	groups, _ := pr.resolveGroups(userID, ctx, role)
	return groups
}

// resolve 返回合并后的节点表（node → granted）。
func (pr *PermResolver) resolve(userID string, ctx map[string]string, role string) map[string]bool {
	_, merged := pr.resolveGroups(userID, ctx, role)
	return merged
}

func (pr *PermResolver) resolveGroups(userID string, ctx map[string]string, role string) ([]string, map[string]bool) {
	now := time.Now().Unix()
	if ctx == nil {
		ctx = map[string]string{}
	}

	// 1. 用户直节点 + 组归属
	direct := map[string]bool{}
	joined := []string{}
	excluded := map[string]bool{}
	for _, r := range pr.queryNodes(
		"SELECT node, value, context_key, context_val, expire_at FROM perm_user_nodes WHERE user_id = %s",
		userID) {
		if !r.alive(now) || !r.ctxMatch(ctx) {
			continue
		}
		n := strings.ToLower(strings.TrimSpace(r.Node))
		if n == "" {
			continue
		}
		if strings.HasPrefix(n, "group.") {
			if r.Value {
				joined = append(joined, n[len("group."):])
			} else {
				excluded[n[len("group."):]] = true
			}
			continue
		}
		direct[n] = r.Value
	}

	// 2. 默认组 + 内置角色组
	defRows, _ := pr.store.Query("SELECT name FROM perm_groups WHERE is_default = 1")
	for _, r := range defRows {
		name := strVal(r["name"])
		if name != "" && !excluded[name] {
			joined = append(joined, name)
		}
	}
	if g, ok := roleToGroup[strings.ToLower(role)]; ok {
		joined = append(joined, g)
	}

	// 3. BFS 展开 group.xxx 继承（环检测 + 否决剪枝）
	groupNodes := pr.loadGroupNodes()
	allGroups := expandInheritance(joined, excluded, groupNodes, now, ctx)

	// 4. weight 降序（同名字典序，保证确定性）
	sort.SliceStable(allGroups, func(i, j int) bool {
		wi, wj := pr.weightOf(allGroups[i]), pr.weightOf(allGroups[j])
		if wi != wj {
			return wi > wj
		}
		return allGroups[i] < allGroups[j]
	})

	// 5. 合并：用户直节点优先，其后各组按 weight 降序，先到先得
	merged := map[string]bool{}
	for k, v := range direct {
		merged[k] = v
	}
	for _, g := range allGroups {
		src := map[string]bool{}
		for _, r := range groupNodes[g] {
			if !r.alive(now) || !r.ctxMatch(ctx) {
				continue
			}
			n := strings.ToLower(strings.TrimSpace(r.Node))
			if n == "" || strings.HasPrefix(n, "group.") {
				continue
			}
			src[n] = r.Value
		}
		if b, ok := builtinGroups[g]; ok && b.node != "" {
			src[b.node] = true
		}
		for k, v := range src {
			if _, exists := merged[k]; !exists {
				merged[k] = v
			}
		}
	}
	return allGroups, merged
}

// loadGroupNodes 一次性载入全部组节点（IIPC 单机数据量小，避免逐组查询）。
func (pr *PermResolver) loadGroupNodes() map[string][]permNodeRow {
	out := map[string][]permNodeRow{}
	raw, err := pr.store.Query("SELECT group_name, node, value, context_key, context_val, expire_at FROM perm_group_nodes")
	if err != nil {
		logWarn("perm", "查询组节点失败: %v", err)
		return out
	}
	for _, r := range raw {
		g := strVal(r["group_name"])
		if g == "" {
			continue
		}
		out[g] = append(out[g], permNodeRow{
			Node:       strVal(r["node"]),
			Value:      truthy(r["value"]),
			ContextKey: strVal(r["context_key"]),
			ContextVal: strVal(r["context_val"]),
			ExpireAt:   strVal(r["expire_at"]),
		})
	}
	return out
}

func expandInheritance(joined []string, excluded map[string]bool,
	groupNodes map[string][]permNodeRow, now int64, ctx map[string]string) []string {
	result := []string{}
	seen := map[string]bool{}
	queue := append([]string{}, joined...)
	for len(queue) > 0 {
		g := queue[0]
		queue = queue[1:]
		if seen[g] || excluded[g] {
			continue
		}
		seen[g] = true
		result = append(result, g)
		// 组节点里的 group.xxx 继承 + 内置组继承链
		for _, r := range groupNodes[g] {
			if !r.alive(now) || !r.ctxMatch(ctx) || !r.Value {
				continue
			}
			n := strings.ToLower(strings.TrimSpace(r.Node))
			if strings.HasPrefix(n, "group.") {
				queue = append(queue, n[len("group."):])
			}
		}
		if b, ok := builtinGroups[g]; ok {
			queue = append(queue, b.inherits...)
		}
	}
	return result
}

// weightOf 取组权重：内置组走内置表，自定义组查 perm_groups。
// perm_groups 表极小（通常 <20 行），每次解析查一次可接受；要优化只改这里。
func (pr *PermResolver) weightOf(g string) int {
	if b, ok := builtinGroups[g]; ok {
		return b.weight
	}
	rows, err := pr.store.Query("SELECT weight FROM perm_groups WHERE name = ?", g)
	if err != nil || len(rows) == 0 {
		return 0
	}
	if f, ok := toFloat(rows[0]["weight"]); ok {
		return int(f)
	}
	return 0
}
