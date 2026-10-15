// IIPC 服务端：监听客户端连接，按方法分派，向订阅者推送事件。
// 连接分两类角色：
//   - 插件（plugin）：Python 插件侧，收事件、发 req（db/msg/api），一般不收 req。
//   - 适配器（adapter）：Rust 协议层，收内核 req（api.call）并回 res，向内核推入站事件（evt）。
// 出站采用「每连接独立队列 + 专属写 goroutine」：Publish/Call 只入队，绝不因对端读慢而队头阻塞。
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

// MethodHandler 处理来自客户端的请求；c 为发起请求的连接。
type MethodHandler func(c *Conn, params json.RawMessage) (interface{}, error)

// connOutCap 单连接出站队列容量；对端读慢、积压超限则丢帧（背压保护，不拖垮他人）。
const connOutCap = 1024

// defaultCallTimeout 内核主动调用对端（适配器/插件）的默认超时。
const defaultCallTimeout = 15 * time.Second

// outResult 承载一次出站调用的结果或错误。
type outResult struct {
	result json.RawMessage
	err    string
}

// Conn 封装一个客户端连接的读写 + 出站调用关联。
type Conn struct {
	id        int64
	raw       net.Conn
	reader    *bufio.Scanner
	server    *IpcServer
	pluginID  string
	isAdapter bool

	mu     sync.Mutex
	closed bool
	out    chan []byte
	done   chan struct{}
	once   sync.Once

	// 出站 req/res 关联（内核→对端的调用，按 id 等待对端 res）
	opMu       sync.Mutex
	outSeq     int64
	outPending map[int64]chan outResult
}

func newConn(id int64, raw net.Conn, server *IpcServer) *Conn {
	c := &Conn{
		id:         id,
		raw:        raw,
		server:     server,
		out:        make(chan []byte, connOutCap),
		done:       make(chan struct{}),
		outPending: make(map[int64]chan outResult),
	}
	c.reader = bufio.NewScanner(raw)
	c.reader.Buffer(make([]byte, 0, 1024*1024), 1024*1024)
	go c.writeLoop()
	return c
}

// writeLoop 单连接专属写出 goroutine：从队列取帧写 socket；对端窗口满只阻塞本连接。
func (c *Conn) writeLoop() {
	for {
		select {
		case b, ok := <-c.out:
			if !ok {
				return
			}
			if _, err := c.raw.Write(b); err != nil {
				c.close()
				return
			}
		case <-c.done:
			return
		}
	}
}

// sendRaw 非阻塞入队一帧；队列满则丢弃并告警。
func (c *Conn) sendRaw(f *Frame) {
	b, err := json.Marshal(f)
	if err != nil {
		logError("ipc", "帧编码失败: %v", err)
		return
	}
	b = append(b, '\n')
	select {
	case c.out <- b:
	default:
		logWarn("ipc", "连接 %d 出站队列已满(>%d 帧)，丢弃一帧（背压保护）", c.id, connOutCap)
	}
}

// sendEvent 推送一条事件给该连接（核心→插件单向，不等待响应）。
func (c *Conn) sendEvent(payload json.RawMessage) {
	c.sendRaw(&Frame{T: "evt", Channel: EventChannel, Payload: payload})
}

// Call 内核主动调用对端：发 req 并阻塞等待匹配 id 的 res。
func (c *Conn) Call(method string, params interface{}, timeout time.Duration) (json.RawMessage, error) {
	if timeout <= 0 {
		timeout = defaultCallTimeout
	}
	pb, err := json.Marshal(params)
	if err != nil {
		return nil, err
	}
	c.opMu.Lock()
	c.outSeq++
	id := c.outSeq
	ch := make(chan outResult, 1)
	c.outPending[id] = ch
	c.opMu.Unlock()

	c.sendRaw(&Frame{T: "req", ID: id, Method: method, Params: pb})
	select {
	case r := <-ch:
		if r.err != "" {
			return nil, errors.New(r.err)
		}
		return r.result, nil
	case <-time.After(timeout):
		c.opMu.Lock()
		delete(c.outPending, id)
		c.opMu.Unlock()
		return nil, fmt.Errorf("调用 %s 超时", method)
	}
}

// resolveRes 用对端返回的 res 回填对应出站调用。
func (c *Conn) resolveRes(id int64, r outResult) {
	c.opMu.Lock()
	ch, ok := c.outPending[id]
	if ok {
		delete(c.outPending, id)
	}
	c.opMu.Unlock()
	if ok {
		ch <- r // ch 有缓冲 1，不会阻塞读线程
	}
}

func (c *Conn) close() {
	c.once.Do(func() {
		c.mu.Lock()
		c.closed = true
		c.mu.Unlock()
		close(c.done)
		c.raw.Close()
	})
}

// Done 连接关闭信号（close() 时 close(c.done)）。
func (c *Conn) Done() <-chan struct{} { return c.done }

// isClosed 连接是否已关闭。
func (c *Conn) isClosed() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.closed
}

func (c *Conn) readLoop() {
	defer c.close()
	for c.reader.Scan() {
		var f Frame
		if err := json.Unmarshal(c.reader.Bytes(), &f); err != nil {
			continue
		}
		switch f.T {
		case "req":
			// register 类（适配器/插件）内联同步处理：确保角色/订阅在紧随其后的帧之前生效，
			// 避免"适配器 register 后立刻推 evt，却因 isAdapter 尚未置位被当非适配器丢弃"的竞态。
			// 阻塞类方法（api.call/msg.send/db）仍走 goroutine，避免占用读循环。
			if f.Method == MethodAdapterRegister || f.Method == MethodPluginRegister {
				c.server.handleReq(c, &f)
			} else {
				go c.server.handleReq(c, &f)
			}
		case "res":
			c.resolveRes(f.ID, outResult{result: f.Result, err: f.Error})
		case "evt":
			if f.Channel == EventChannel {
				c.server.publishFromConn(c, f.Payload)
			}
		case "ping":
			c.sendRaw(&Frame{T: "pong"})
		default:
			// 忽略
		}
	}
	c.server.dropConn(c)
}

func (s *IpcServer) handleReq(c *Conn, f *Frame) {
	defer func() {
		if r := recover(); r != nil {
			logError("ipc", "handler panic [%s]: %v", f.Method, r)
			c.sendRaw(&Frame{T: "res", ID: f.ID, Error: fmt.Sprintf("panic: %v", r)})
		}
	}()
	h := s.methods[f.Method]
	if h == nil {
		c.sendRaw(&Frame{T: "res", ID: f.ID, Error: fmt.Sprintf("unknown method: %s", f.Method)})
		return
	}
	logDebug("ipc", "req %q conn=%d plugin=%s adapter=%v", f.Method, c.id, c.pluginID, c.isAdapter)
	res, err := h(c, f.Params)
	if err != nil {
		c.sendRaw(&Frame{T: "res", ID: f.ID, Error: err.Error()})
		return
	}
	rp, _ := json.Marshal(res)
	c.sendRaw(&Frame{T: "res", ID: f.ID, Result: rp})
}

// IpcServer 是核心侧 IPC 服务端（多连接：插件 + 适配器）。
type IpcServer struct {
	addr    string
	auth    string
	bus     *EventBus
	store   Store
	methods map[string]MethodHandler
	mu      sync.Mutex
	conns   map[int64]*Conn
	nextID  int64
	ln      net.Listener
	Address string

	// 适配器注册表
	adapters       map[int64]*Conn
	adaptersBySelf map[string]*Conn
	primaryAdapter *Conn

	// 编排附属件（断开时按插件清理；均为可选，nil 时跳过）
	sessMgr *SessionManager
	sched   *TaskScheduler
	grpSw   *GroupSwitch
}

// SetOrchestration 注入编排附属件（main 装配用）。
func (s *IpcServer) SetOrchestration(sess *SessionManager, tsk *TaskScheduler, gs *GroupSwitch) {
	s.sessMgr = sess
	s.sched = tsk
	s.grpSw = gs
}

func NewIpcServer(addr, auth string, bus *EventBus, store Store) *IpcServer {
	return &IpcServer{
		addr:           addr,
		auth:           auth,
		bus:            bus,
		store:          store,
		methods:        make(map[string]MethodHandler),
		conns:          make(map[int64]*Conn),
		adapters:       make(map[int64]*Conn),
		adaptersBySelf: make(map[string]*Conn),
	}
}

func (s *IpcServer) RegisterMethod(m string, h MethodHandler) { s.methods[m] = h }

// registerAdapter 标记连接为适配器，并按 self_id 建路由。
func (s *IpcServer) registerAdapter(c *Conn, selfID string) {
	s.mu.Lock()
	if s.adapters == nil {
		s.adapters = make(map[int64]*Conn)
	}
	if s.adaptersBySelf == nil {
		s.adaptersBySelf = make(map[string]*Conn)
	}
	c.isAdapter = true
	s.adapters[c.id] = c
	s.primaryAdapter = c
	if selfID != "" {
		s.adaptersBySelf[selfID] = c
	}
	s.mu.Unlock()
	logInfo("adapter", "适配器已注册 id=%d self_id=%q（当前 %d）", c.id, selfID, len(s.adapters))
}

func (s *IpcServer) unregisterAdapter(c *Conn) {
	s.mu.Lock()
	delete(s.adapters, c.id)
	if s.primaryAdapter == c {
		s.primaryAdapter = nil
		for _, cc := range s.adapters {
			s.primaryAdapter = cc
			break
		}
	}
	for sid, cc := range s.adaptersBySelf {
		if cc == c {
			delete(s.adaptersBySelf, sid)
		}
	}
	s.mu.Unlock()
}

func (s *IpcServer) pickAdapter(selfID string) *Conn {
	s.mu.Lock()
	defer s.mu.Unlock()
	if selfID != "" {
		if c, ok := s.adaptersBySelf[selfID]; ok {
			return c
		}
	}
	return s.primaryAdapter
}

// CallAdapter 路由一次 API 调用到适配器并等待结果。
func (s *IpcServer) CallAdapter(action string, params map[string]interface{}, selfID string) (json.RawMessage, error) {
	c := s.pickAdapter(selfID)
	if c == nil {
		return nil, errors.New("无适配器连接")
	}
	body := map[string]interface{}{"action": action, "params": params}
	if selfID != "" {
		body["self_id"] = selfID
	}
	return c.Call(MethodApiCall, body, defaultCallTimeout)
}

// publishFromConn 处理适配器上报的入站事件 → 发布到事件总线。
func (s *IpcServer) publishFromConn(c *Conn, payload json.RawMessage) {
	if !c.isAdapter {
		logWarn("bus", "非适配器连接尝试发布事件，已忽略 conn=%d", c.id)
		return
	}
	var ev Event
	if err := json.Unmarshal(payload, &ev); err != nil {
		logWarn("bus", "事件载荷解析失败: %v", err)
		return
	}
	if ev.Type == "" {
		logWarn("bus", "事件缺 type 字段，忽略")
		return
	}
	s.bus.Publish(ev)
}

func (s *IpcServer) dropConn(c *Conn) {
	s.mu.Lock()
	delete(s.conns, c.id)
	s.mu.Unlock()
	if c.isAdapter {
		s.unregisterAdapter(c)
	}
	if c.pluginID != "" {
		logInfo("ipc", "插件连接断开: %s", c.pluginID)
		// 清理该插件的编排状态：定时任务 / 挂起会话 / 群开关条目
		if s.sched != nil {
			s.sched.UnregisterPlugin(c.pluginID)
		}
		if s.sessMgr != nil {
			s.sessMgr.DropPlugin(c.pluginID)
		}
		if s.grpSw != nil {
			s.grpSw.ForgetPlugin(c.pluginID)
		}
	}
}

// Run 启动监听并接受连接（阻塞）。
func (s *IpcServer) Run() error {
	ln, err := net.Listen("tcp", s.addr)
	if err != nil {
		return err
	}
	s.ln = ln
	s.Address = ln.Addr().String()
	logInfo("ipc", "IIPC 服务端监听 %s", s.Address)
	for {
		raw, err := ln.Accept()
		if err != nil {
			logWarn("ipc", "accept 错误: %v", err)
			continue
		}
		s.mu.Lock()
		s.nextID++
		id := s.nextID
		s.mu.Unlock()
		c := newConn(id, raw, s)
		s.mu.Lock()
		s.conns[id] = c
		s.mu.Unlock()
		logInfo("ipc", "客户端已连接 id=%d", id)
		go c.readLoop()
	}
}
