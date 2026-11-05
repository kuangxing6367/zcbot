// core 层：IIPC 服务端单元测试。
package main

import (
	"bufio"
	"encoding/json"
	"net"
	"strings"
	"testing"
	"time"
)

func dialServer(t *testing.T, addr string) (net.Conn, *bufio.Scanner) {
	t.Helper()
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial %s: %v", addr, err)
	}
	return c, bufio.NewScanner(c)
}

func sendLine(t *testing.T, c net.Conn, v interface{}) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	b = append(b, '\n')
	if _, err := c.Write(b); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func recvFrame(t *testing.T, sc *bufio.Scanner) map[string]interface{} {
	t.Helper()
	if !sc.Scan() {
		t.Fatalf("连接已关闭，未收到帧")
	}
	var m map[string]interface{}
	if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
		t.Fatalf("unmarshal %q: %v", sc.Text(), err)
	}
	return m
}

// handshake 完成连接鉴权：首帧发 auth，读回 auth_res 并断言成功。
func handshake(t *testing.T, c net.Conn, sc *bufio.Scanner, auth string) {
	t.Helper()
	sendLine(t, c, map[string]interface{}{"t": FrameTypeAuth, "auth": auth})
	r := recvFrame(t, sc)
	if r["t"] != FrameTypeAuthRes || r["result"] != true {
		t.Fatalf("auth 握手失败: %v", r)
	}
}

// TestAdapterRoleAndRouting 走真实回环 TCP，验证：
//  1. adapter.register 标记角色
//  2. 适配器推 evt → 订阅该事件的插件收到事件
//  3. 插件 api.call → 适配器收到 req、回 res → 插件拿到返回值（出站 req/res 关联）
//  4. 非适配器连接推 evt 被拒绝（不污染总线）
func TestAdapterRoleAndRouting(t *testing.T) {
	addr := "127.0.0.1:18799"
	bus := NewEventBus()
	store, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("NewSqliteStore(:memory:) 失败: %v", err)
	}
	InitServices(bus, store) // 编排附属件（调度/会话/群开关/权限），handlers 依赖
	srv := NewIpcServer(addr, "unit-test-secret", bus, store)
	srv.SetOrchestration(sessMgr, sched, grpSw)
	pm := NewPluginManager(addr, "unit-test-secret", "python", "plugins", "py_sdk")
	registerCoreHandlers(srv, bus, store, pm, map[string]bool{})
	go func() { _ = srv.Run() }()
	for i := 0; i < 200; i++ {
		if c, err := net.Dial("tcp", addr); err == nil {
			c.Close()
			break
		}
		time.Sleep(5 * time.Millisecond)
	}

	// 适配器连接
	ac, asc := dialServer(t, addr)
	defer ac.Close()
	handshake(t, ac, asc, "unit-test-secret")
	sendLine(t, ac, map[string]interface{}{"t": "req", "id": 1, "method": MethodAdapterRegister,
		"params": map[string]interface{}{"self_id": "123"}})
	if r := recvFrame(t, asc); r["t"] != "res" {
		t.Fatalf("adapter.register 期望 res，got %v", r)
	}

	// 插件连接（订阅 message）
	pc, psc := dialServer(t, addr)
	defer pc.Close()
	handshake(t, pc, psc, "unit-test-secret")
	sendLine(t, pc, map[string]interface{}{"t": "req", "id": 1, "method": MethodPluginRegister,
		"params": map[string]interface{}{"plugin_id": "t", "event_types": []string{"message"}}})
	if r := recvFrame(t, psc); r["t"] != "res" {
		t.Fatalf("plugin.register 期望 res，got %v", r)
	}

	// 1) 适配器推事件 → 插件收到 evt
	sendLine(t, ac, map[string]interface{}{"t": "evt", "channel": EventChannel,
		"payload": map[string]interface{}{"type": "message", "data": map[string]interface{}{"message": "hi"}}})
	ev := recvFrame(t, psc)
	if ev["t"] != "evt" {
		t.Fatalf("插件期望收到 evt，got %v", ev)
	}
	payload, _ := ev["payload"].(map[string]interface{})
	if payload == nil || payload["type"] != "message" {
		t.Fatalf("事件载荷不正确: %v", ev)
	}

	// 2) 插件 api.call → 适配器收到 req；适配器回 res → 插件拿到结果
	sendLine(t, pc, map[string]interface{}{"t": "req", "id": 2, "method": MethodApiCall,
		"params": map[string]interface{}{"action": "get_login_info"}})
	req := recvFrame(t, asc)
	if req["t"] != "req" || req["method"] != MethodApiCall {
		t.Fatalf("适配器期望收到 api.call req，got %v", req)
	}
	sendLine(t, ac, map[string]interface{}{"t": "res", "id": req["id"],
		"result": map[string]interface{}{"nickname": "x"}})
	back := recvFrame(t, psc)
	if back["t"] != "res" {
		t.Fatalf("插件期望 api.call 的 res，got %v", back)
	}
	res2, _ := back["result"].(map[string]interface{})
	if res2 == nil || res2["nickname"] != "x" {
		t.Fatalf("api.call 返回值不正确: %v", back)
	}

	// 3) 非适配器连接推 evt 应被忽略（插件不会收到第二次 evt）
	sendLine(t, pc, map[string]interface{}{"t": "evt", "channel": EventChannel,
		"payload": map[string]interface{}{"type": "message", "data": map[string]interface{}{"message": "spoof"}}})
	// 该连接之后若再无合法帧到达即通过；设短读超时验证"没有多余 evt"
	pc.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	if psc.Scan() {
		var extra map[string]interface{}
		_ = json.Unmarshal(psc.Bytes(), &extra)
		if extra["t"] == "evt" {
			t.Fatalf("非适配器推的 evt 不应被发布，却收到: %v", extra)
		}
	}
	pc.SetReadDeadline(time.Time{})
}

// TestAuthRejectsUnauthenticated 验证服务端真实校验令牌：错误令牌 / 未授权请求
// 被明确拒绝并回错误帧，正确令牌握手后请求放行（覆盖「只存不校验」回归）。
func TestAuthRejectsUnauthenticated(t *testing.T) {
	addr := "127.0.0.1:18798"
	bus := NewEventBus()
	store, err := NewSqliteStore(":memory:")
	if err != nil {
		t.Fatalf("NewSqliteStore(:memory:) 失败: %v", err)
	}
	InitServices(bus, store)
	srv := NewIpcServer(addr, "test-secret-token", bus, store)
	srv.SetOrchestration(sessMgr, sched, grpSw)
	pm := NewPluginManager(addr, "test-secret-token", "python", "plugins", "py_sdk")
	registerCoreHandlers(srv, bus, store, pm, map[string]bool{})
	go func() { _ = srv.Run() }()
	for i := 0; i < 200; i++ {
		if c, err := net.Dial("tcp", addr); err == nil {
			c.Close()
			break
		}
		time.Sleep(5 * time.Millisecond)
	}

	// 1) 错误令牌 → auth_res 带明确 error（不静默放行）
	c1, sc1 := dialServer(t, addr)
	sendLine(t, c1, map[string]interface{}{"t": FrameTypeAuth, "auth": "wrong-token"})
	r1 := recvFrame(t, sc1)
	if r1["t"] != FrameTypeAuthRes {
		t.Fatalf("错误令牌期望 auth_res，got %v", r1)
	}
	if e, _ := r1["error"].(string); e == "" {
		t.Fatalf("错误令牌应回明确错误，got %v", r1)
	}
	c1.Close()

	// 2) 不握手直接发高危 RPC → 明确 unauthorized，绝不静默放行
	c2, sc2 := dialServer(t, addr)
	sendLine(t, c2, map[string]interface{}{"t": "req", "id": 9, "method": MethodDBQuery,
		"params": map[string]interface{}{"sql": "SELECT 1"}})
	r2 := recvFrame(t, sc2)
	if e, _ := r2["error"].(string); !strings.Contains(e, "unauthorized") {
		t.Fatalf("未授权 req 应被明确拒绝，got %v", r2)
	}
	c2.Close()

	// 3) 正确令牌 → 握手成功，随后请求不再被拒绝
	c3, sc3 := dialServer(t, addr)
	defer c3.Close()
	handshake(t, c3, sc3, "test-secret-token")
	sendLine(t, c3, map[string]interface{}{"t": "req", "id": 1,
		"method": MethodAdapterRegister, "params": map[string]interface{}{"self_id": "1"}})
	r3 := recvFrame(t, sc3)
	if r3["t"] != "res" {
		t.Fatalf("授权连接 register 期望 res，got %v", r3)
	}
	if e, _ := r3["error"].(string); strings.Contains(e, "unauthorized") {
		t.Fatalf("授权连接不应再被拒绝: %v", r3)
	}
}
