package main

import (
	"testing"
)

// 测试用 Go 插件：捕获事件与命令结果。
type testGoPlugin struct {
	events int
}

func (p *testGoPlugin) ID() string { return "test_go" }
func (p *testGoPlugin) EventTypes() []string {
	return []string{"message", "notice"}
}
func (p *testGoPlugin) HandleEvent(ev Event) {
	p.events++
}
func (p *testGoPlugin) Commands() map[string]GoCommand {
	return map[string]GoCommand{
		"/testping": {
			Handler: func(args string, ev Event) (string, error) {
				return "pong from go", nil
			},
		},
		"/testsecret": {
			Perm: "admin",
			Handler: func(args string, ev Event) (string, error) {
				return "secret ok", nil
			},
		},
	}
}

func TestGoPluginDispatch(t *testing.T) {
	RegisterGoPlugin(&testGoPlugin{})

	sent := []string{}
	send := func(text string, ev Event) { sent = append(sent, text) }

	// 1) 普通命令：非管理员也能命中
	ev := Event{Type: "message", Data: map[string]interface{}{"message": "/testping", "user_id": "111"}}
	dispatchGoPlugins(ev, map[string]bool{}, send)
	if len(sent) != 1 || sent[0] != "pong from go" {
		t.Fatalf("期望收到 'pong from go'，实际 %v", sent)
	}

	// 2) 管理命令：非管理员应被拒（不回发 secret ok）
	sent = nil
	ev2 := Event{Type: "message", Data: map[string]interface{}{"message": "/testsecret", "user_id": "111"}}
	dispatchGoPlugins(ev2, map[string]bool{}, send)
	for _, s := range sent {
		if s == "secret ok" {
			t.Fatalf("非管理员不应命中 /testsecret")
		}
	}

	// 3) 管理命令：管理员放行
	sent = nil
	ev3 := Event{Type: "message", Data: map[string]interface{}{"message": "/testsecret", "user_id": "999"}}
	dispatchGoPlugins(ev3, map[string]bool{"999": true}, send)
	if len(sent) != 1 || sent[0] != "secret ok" {
		t.Fatalf("管理员应命中 /testsecret，实际 %v", sent)
	}

	// 4) 非订阅事件类型不触发
	sent = nil
	ev4 := Event{Type: "meta_event", Data: map[string]interface{}{"message": "/testping"}}
	dispatchGoPlugins(ev4, map[string]bool{}, send)
	if len(sent) != 0 {
		t.Fatalf("meta_event 不应触发 go 插件命令，实际 %v", sent)
	}
}
