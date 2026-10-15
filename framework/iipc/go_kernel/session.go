// 会话管理器（对齐 core_plugins/session 的 wait_for / create_session 语义）：
// 插件在 handler 里阻塞等待"同一用户、同一聊天上下文"的下一条消息。
// 内核负责挂起与配对，插件侧只发一次 session.wait（req-res 阻塞，带超时）。
//
// 配对规则（与原实现一致的口径）：
//   - user_id 必须相同
//   - 聊天上下文相同：group_id 相等（群聊），或双方均为空（私聊）
//   - 只认 post_type=message 的消息事件
// 过滤器（handler 参数）在插件侧执行：不命中则插件用剩余超时再次 wait，
// 内核不感知 Python 过滤器，这是协议边界内的合理近似。
package main

import (
	"sync"
	"time"
)

// sessionWait 一个挂起中的会话等待。
type sessionWait struct {
	conn     *Conn
	pluginID string
	userID   string
	groupID  string
	ch       chan map[string]interface{}
	timer    *time.Timer
}

// SessionManager 会话挂起/配对。
type SessionManager struct {
	mu      sync.Mutex
	pending []*sessionWait
}

func NewSessionManager() *SessionManager { return &SessionManager{} }

// Wait 挂起等待，返回 (事件数据, 是否等到)。timeout<=0 时用 60s 默认。
func (sm *SessionManager) Wait(conn *Conn, pluginID, userID, groupID string, timeout time.Duration) (map[string]interface{}, bool) {
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	w := &sessionWait{
		conn: conn, pluginID: pluginID, userID: userID, groupID: groupID,
		ch: make(chan map[string]interface{}, 1),
	}
	w.timer = time.AfterFunc(timeout, func() {
		sm.mu.Lock()
		sm.remove(w)
		sm.mu.Unlock()
		select {
		case w.ch <- nil: // 超时信号
		default:
		}
	})
	sm.mu.Lock()
	sm.pending = append(sm.pending, w)
	sm.mu.Unlock()

	// 连接断开也要能解除挂起，避免 handler 永久阻塞占着插件进程
	select {
	case ev := <-w.ch:
		if ev == nil {
			return nil, false // 超时
		}
		return ev, true
	case <-conn.Done():
		sm.mu.Lock()
		sm.remove(w)
		sm.mu.Unlock()
		return nil, false
	}
}

// Offer 把一条入站消息事件喂给匹配的挂起会话；返回是否被消费。
func (sm *SessionManager) Offer(ev Event) bool {
	if ev.Type != "message" {
		return false
	}
	data := ev.Data
	uid := strVal(data["user_id"])
	gid := strVal(data["group_id"])
	if uid == "" {
		return false
	}
	sm.mu.Lock()
	var hit *sessionWait
	for _, w := range sm.pending {
		if w.userID != uid {
			continue
		}
		// 聊天上下文：群号相等，或双方均空（私聊）
		if (w.groupID == "" && gid == "") || (w.groupID != "" && w.groupID == gid) {
			hit = w
			break
		}
	}
	if hit != nil {
		sm.remove(hit)
	}
	sm.mu.Unlock()
	if hit == nil {
		return false
	}
	select {
	case hit.ch <- data:
	default:
	}
	return true
}

// DropPlugin 插件断开：清理其全部挂起会话（Wait 的 conn 轮询会兜底，这里双保险）。
func (sm *SessionManager) DropPlugin(pluginID string) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	kept := sm.pending[:0]
	for _, w := range sm.pending {
		if w.pluginID == pluginID {
			select {
			case w.ch <- nil:
			default:
			}
			continue
		}
		kept = append(kept, w)
	}
	sm.pending = kept
}

func (sm *SessionManager) remove(w *sessionWait) {
	for i, x := range sm.pending {
		if x == w {
			sm.pending = append(sm.pending[:i], sm.pending[i+1:]...)
			return
		}
	}
}
