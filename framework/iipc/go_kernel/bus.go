// 事件总线：按事件类型维护 "订阅该类型的插件连接" 集合。
// 发布时把事件一次性推送（批量帧见 Server.sendEvent）给所有订阅者。
// fanFilter：扇出过滤器（群级插件开关等），返回 false 则跳过该连接。
package main

import (
	"encoding/json"
	"sync"
)

type EventBus struct {
	mu        sync.RWMutex
	subs      map[string]map[int64]*Conn
	hook      func(ev Event) // 发布钩子：用于扇出到 Go 原生插件等
	preHook   func(ev Event) // 前置钩子：会话配对等在扇出前同步执行
	fanFilter func(pluginID string, ev Event) bool
}

func NewEventBus() *EventBus {
	return &EventBus{subs: make(map[string]map[int64]*Conn)}
}

// SetPublishHook 注册发布钩子（在推送完 IPC 连接后异步调用）。
func (b *EventBus) SetPublishHook(h func(ev Event)) { b.hook = h }

// SetPreHook 注册前置钩子（扇出前同步调用：会话配对等需要"先于插件看到事件"的场景）。
func (b *EventBus) SetPreHook(h func(ev Event)) { b.preHook = h }

// SetFanFilter 注册扇出过滤器（返回 false 的连接不收该事件）。
func (b *EventBus) SetFanFilter(f func(pluginID string, ev Event) bool) { b.fanFilter = f }

// Subscribe 让某连接订阅某事件类型
func (b *EventBus) Subscribe(eventType string, c *Conn) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.subs[eventType] == nil {
		b.subs[eventType] = make(map[int64]*Conn)
	}
	b.subs[eventType][c.id] = c
}

// Unsubscribe 取消订阅
func (b *EventBus) Unsubscribe(eventType string, c *Conn) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if m := b.subs[eventType]; m != nil {
		delete(m, c.id)
		if len(m) == 0 {
			delete(b.subs, eventType)
		}
	}
}

// Publish 把事件推给所有订阅该类型的连接（调用方已持有对应连接）
func (b *EventBus) Publish(ev Event) {
	if b.preHook != nil {
		b.preHook(ev) // 同步：会话配对必须先于插件侧 handler 执行
	}
	b.mu.RLock()
	conns := make([]*Conn, 0, 4)
	if m := b.subs[ev.Type]; m != nil {
		for _, c := range m {
			conns = append(conns, c)
		}
	}
	b.mu.RUnlock()
	if len(conns) == 0 {
		return
	}
	payload, _ := json.Marshal(ev)
	for _, c := range conns {
		if b.fanFilter != nil && !b.fanFilter(c.pluginID, ev) {
			continue
		}
		c.sendEvent(payload)
	}
	// 扇出到 Go 原生插件（异步，避免阻塞发布）
	if b.hook != nil {
		go b.hook(ev)
	}
}

// PublishTo 只把事件推给指定插件的订阅连接（定时任务 tick 等定向场景）。
func (b *EventBus) PublishTo(ev Event, pluginID string) {
	if b.preHook != nil {
		b.preHook(ev)
	}
	b.mu.RLock()
	var targets []*Conn
	if m := b.subs[ev.Type]; m != nil {
		for _, c := range m {
			if c.pluginID == pluginID {
				targets = append(targets, c)
			}
		}
	}
	b.mu.RUnlock()
	if len(targets) == 0 {
		return
	}
	payload, _ := json.Marshal(ev)
	for _, c := range targets {
		c.sendEvent(payload)
	}
}
