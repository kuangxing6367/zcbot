// 群级插件开关（对齐 framework loader 的 set_group_plugin_enabled 系列）：
// 某插件可在指定群被禁用——事件扇出时按 group_id 过滤，禁用群的插件收不到该群事件。
// 状态持久化在 group_plugin_settings 表，重启恢复。
package main

import (
	"sync"
)

// GroupSwitch 群插件开关表（内存镜像 + DB 持久化）。
type GroupSwitch struct {
	mu    sync.Mutex
	store Store
	// disabled[pluginID][groupID] = true 表示在该群禁用
	disabled map[string]map[string]bool
	// knownPlugins 已注册插件名（get 时用于回全量状态）
	known map[string]bool
}

func NewGroupSwitch(store Store) *GroupSwitch {
	gs := &GroupSwitch{
		store:    store,
		disabled: map[string]map[string]bool{},
		known:    map[string]bool{},
	}
	gs.load()
	return gs
}

func (gs *GroupSwitch) load() {
	rows, err := gs.store.Query(
		"SELECT group_id, plugin_name, enabled FROM group_plugin_settings")
	if err != nil {
		logWarn("group_switch", "加载群插件开关失败: %v", err)
		return
	}
	n := 0
	for _, r := range rows {
		if truthy(r["enabled"]) {
			continue
		}
		gid, pl := strVal(r["group_id"]), strVal(r["plugin_name"])
		if gs.disabled[pl] == nil {
			gs.disabled[pl] = map[string]bool{}
		}
		gs.disabled[pl][gid] = true
		n++
	}
	if n > 0 {
		logInfo("group_switch", "恢复群插件禁用配置 %d 条", n)
	}
}

// NotePlugin 记录一个已知插件（注册时调用）。
func (gs *GroupSwitch) NotePlugin(pluginID string) {
	gs.mu.Lock()
	gs.known[pluginID] = true
	gs.mu.Unlock()
}

// ForgetPlugin 插件断开时清理其在禁用表中的条目。
func (gs *GroupSwitch) ForgetPlugin(pluginID string) {
	gs.mu.Lock()
	delete(gs.disabled, pluginID)
	delete(gs.known, pluginID)
	gs.mu.Unlock()
}

// Set 设置某插件在某群的启停（持久化）。
func (gs *GroupSwitch) Set(groupID, pluginID string, enabled bool) error {
	v := 1
	if !enabled {
		v = 0
	}
	if _, err := gs.store.Exec(
		"INSERT INTO group_plugin_settings(group_id, plugin_name, enabled, updated_at) VALUES(?,?,?,datetime('now')) "+
			"ON CONFLICT(group_id, plugin_name) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
		groupID, pluginID, v); err != nil {
		return err
	}
	gs.mu.Lock()
	defer gs.mu.Unlock()
	if !enabled {
		if gs.disabled[pluginID] == nil {
			gs.disabled[pluginID] = map[string]bool{}
		}
		gs.disabled[pluginID][groupID] = true
	} else if m := gs.disabled[pluginID]; m != nil {
		delete(m, groupID)
	}
	return nil
}

// Get 返回某群全部已知插件的启用状态（对齐 get_plugin_status_list）。
func (gs *GroupSwitch) Get(groupID string) map[string]bool {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	out := map[string]bool{}
	for p := range gs.known {
		out[p] = true
	}
	// DB 里出现过但当前未注册的插件也带上（默认启用），保持与 Web 视图一致
	rows, err := gs.store.Query(
		"SELECT DISTINCT plugin_name FROM group_plugin_settings")
	if err == nil {
		for _, r := range rows {
			p := strVal(r["plugin_name"])
			if _, ok := out[p]; !ok {
				out[p] = true
			}
		}
	}
	for p, m := range gs.disabled {
		if m[groupID] {
			if _, ok := out[p]; !ok {
				out[p] = true
			}
			out[p] = false
		}
	}
	return out
}

// IsDisabled 该插件在该群是否被禁用。
func (gs *GroupSwitch) IsDisabled(pluginID, groupID string) bool {
	gs.mu.Lock()
	defer gs.mu.Unlock()
	return gs.disabled[pluginID][groupID]
}
