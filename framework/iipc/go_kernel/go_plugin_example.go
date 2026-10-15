// 内置 Go 原生插件示例：展示"事件→Go handler→动作"零 IPC 路径。
// 注册一个 echo_go 插件：处理 message 事件，/goping 直接回 pong（不经 Python 子进程）。
package main

import (
	"fmt"
	"strconv"
	"sync/atomic"
)

type echoGoPlugin struct {
	counter int64
}

func (p *echoGoPlugin) ID() string { return "echo_go" }
func (p *echoGoPlugin) EventTypes() []string {
	return []string{"message", "notice", "request", "meta_event"}
}
func (p *echoGoPlugin) HandleEvent(ev Event) {
	// 仅计数，演示事件可被 Go 直接消费
	atomic.AddInt64(&p.counter, 1)
}
func (p *echoGoPlugin) Commands() map[string]GoCommand {
	return map[string]GoCommand{
		"/goping": {
			Desc: "Go 原生插件回响测试",
			Handler: func(args string, ev Event) (string, error) {
				n := atomic.AddInt64(&p.counter, 1)
				return fmt.Sprintf("pong from go (handled=%d)", n), nil
			},
		},
		"/goadd": {
			Desc: "Go 原生插件：把参数当整数加一返回",
			Handler: func(args string, ev Event) (string, error) {
				v, err := strconv.Atoi(args)
				if err != nil {
					return "", fmt.Errorf("参数需为整数")
				}
				return fmt.Sprintf("goadd -> %d", v+1), nil
			},
		},
	}
}

func init() {
	RegisterGoPlugin(&echoGoPlugin{})
}
