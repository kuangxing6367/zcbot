// Go 原生插件路径（T5）：Windows 下不支持 plugin 包（c-shared），
// 故采用"编译期注册 + 内置扩展"机制——在 main 包内直接注册 Go 插件，
// 其 handler 跑在 Go 进程内，零 GIL、零 IPC 往返，适合高频/低延迟动作。
package main

import (
	"strings"
	"sync"
)

// GoCommand 一个 Go 侧命令：收到事件时若文本以 Name 开头，调用 Handler。
type GoCommand struct {
	Perm    string // admin=仅管理员
	Desc    string
	Handler func(args string, ev Event) (string, error)
}

// GoPlugin 一个 Go 原生插件。
type GoPlugin interface {
	ID() string
	EventTypes() []string
	HandleEvent(ev Event) // 收到订阅事件时调用
	Commands() map[string]GoCommand
}

var (
	goPluginMu sync.RWMutex
	goPlugins  = map[string]GoPlugin{}
)

// RegisterGoPlugin 注册一个 Go 原生插件（编译期调用）。
func RegisterGoPlugin(p GoPlugin) {
	goPluginMu.Lock()
	goPlugins[p.ID()] = p
	goPluginMu.Unlock()
}

func registeredGoPlugins() []GoPlugin {
	goPluginMu.RLock()
	out := make([]GoPlugin, 0, len(goPlugins))
	for _, p := range goPlugins {
		out = append(out, p)
	}
	goPluginMu.RUnlock()
	return out
}

// dispatchGoPlugins 事件到达后，扇出给所有订阅该类型的 Go 插件。
// 命令在 Bus 层已结算好（goCommandMatched），此处只跑事件 handler + 命令结果回发。
func dispatchGoPlugins(ev Event, admins map[string]bool, send func(text string, ev Event)) {
	goPluginMu.RLock()
	plugins := make([]GoPlugin, 0, len(goPlugins))
	for _, p := range goPlugins {
		plugins = append(plugins, p)
	}
	goPluginMu.RUnlock()

	for _, p := range plugins {
		// 仅处理订阅了该事件类型的插件
		subscribed := false
		for _, et := range p.EventTypes() {
			if et == ev.Type {
				subscribed = true
				break
			}
		}
		if !subscribed {
			continue
		}
		// 事件 handler
		func() {
			defer func() { _ = recover() }()
			p.HandleEvent(ev)
		}()
		// 命令匹配
		for name, cmd := range p.Commands() {
			text, _ := ev.Data["message"].(string)
			if !strings.HasPrefix(text, name) {
				continue
			}
			if cmd.Perm == "admin" {
				uid := strVal(ev.Data["user_id"])
				if !admins[uid] {
					send("⛔ 无权限执行 "+name, ev)
					continue
				}
			}
			args := strings.TrimSpace(strings.TrimPrefix(text, name))
			out, err := cmd.Handler(args, ev)
			if err != nil {
				send("❌ "+err.Error(), ev)
				continue
			}
			if out != "" {
				send(out, ev)
			}
		}
	}
}
