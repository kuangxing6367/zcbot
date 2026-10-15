// 内核编排服务容器：把 1:1 补齐的附属件（调度器/会话/群开关/权限解析）
// 与插件数据目录集中到一处，main 装配、handlers 消费、断开清理共用。
package main

import (
	"os"
	"path/filepath"
)

// kernelServices 内核单例（进程内唯一，测试可替换）。
var (
	sched         *TaskScheduler
	sessMgr       *SessionManager
	grpSw         *GroupSwitch
	permRes       *PermResolver
	pluginsDatDir = "plugins_dat"
)

// InitServices 装配编排服务（main 启动时调用一次）。
func InitServices(bus *EventBus, store Store) {
	sched = NewTaskScheduler(bus)
	sessMgr = NewSessionManager()
	grpSw = NewGroupSwitch(store)
	permRes = NewPermResolver(store)
	// 会话配对必须先于插件看到消息（同步前置钩子）
	bus.SetPreHook(func(ev Event) {
		sessMgr.Offer(ev)
	})
	// 群级插件开关：事件扇出时按 group_id 过滤
	bus.SetFanFilter(func(pluginID string, ev Event) bool {
		if pluginID == "" {
			return true
		}
		if gid, ok := strGroupID(ev.Data["group_id"]); ok && gid != "" && gid != "0" {
			return !grpSw.IsDisabled(pluginID, gid)
		}
		return true
	})
	sched.Run()
	logInfo("services", "编排服务就绪（scheduler/session/group_switch/perm）")
}

// strGroupID 归一化事件里的 group_id（JSON 数字/字符串都可能是）。
func strGroupID(v interface{}) (string, bool) {
	switch x := v.(type) {
	case string:
		return x, true
	case float64:
		return itoa(int64(x)), true
	case int64:
		return itoa(x), true
	case int:
		return itoa(int64(x)), true
	default:
		return "", false
	}
}

func ensureDir(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	abs, err := filepath.Abs(dir)
	if err == nil {
		pluginsDatDir = filepath.Dir(abs) // 记录父目录，便于日志排查
	}
	return nil
}
