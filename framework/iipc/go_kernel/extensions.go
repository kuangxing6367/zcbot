// 内核扩展面：把核心从"消息中转"抬到"可编排宿主"。
//
// 四件事，全部按插件隔离、插件断开自动回收：
//  1. 服务注册表：插件声明自己提供什么能力，别的插件按名字解析——跨插件协作不必互相 import。
//  2. 插件间调用：内核中转 RPC，调用方只见服务名/插件名，不持有对方进程句柄。
//  3. 共享 KV：内核托管的键值空间，带 TTL；给"多插件共享一份状态"一个不用各自建表的去处。
//  4. 指标与能力声明：统一的观测出口 + 能力自述，供终端面板与依赖解析消费。
package main

import (
	"encoding/json"
	"errors"
	"sort"
	"strconv"
	"sync"
	"time"
)

// ---------- 服务注册表 ----------

// ServiceEntry 一条服务声明。Provider 是提供它的插件 ID。
type ServiceEntry struct {
	Name     string                 `json:"name"`
	Provider string                 `json:"provider"`
	Version  string                 `json:"version,omitempty"`
	Caps     []string               `json:"caps,omitempty"`
	Meta     map[string]interface{} `json:"meta,omitempty"`
	Since    int64                  `json:"since"`
}

// ServiceRegistry 名字 → 服务声明。同名后注册者覆盖（热重载场景）。
type ServiceRegistry struct {
	mu   sync.RWMutex
	svcs map[string]*ServiceEntry
}

func NewServiceRegistry() *ServiceRegistry {
	return &ServiceRegistry{svcs: make(map[string]*ServiceEntry)}
}

func (r *ServiceRegistry) Register(e *ServiceEntry) {
	r.mu.Lock()
	r.svcs[e.Name] = e
	r.mu.Unlock()
}

// Resolve 按名字解析；命中返回条目与 true。
func (r *ServiceRegistry) Resolve(name string) (*ServiceEntry, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.svcs[name]
	return e, ok
}

// ByCap 按能力反查提供该能力的服务（依赖解析用：我要一个能发图的）。
func (r *ServiceRegistry) ByCap(cap string) []*ServiceEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*ServiceEntry, 0)
	for _, e := range r.svcs {
		for _, c := range e.Caps {
			if c == cap {
				out = append(out, e)
				break
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (r *ServiceRegistry) List() []*ServiceEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*ServiceEntry, 0, len(r.svcs))
	for _, e := range r.svcs {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// ForgetPlugin 插件断开：回收它声明的全部服务，避免留下指向死进程的服务名。
func (r *ServiceRegistry) ForgetPlugin(pluginID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for name, e := range r.svcs {
		if e.Provider == pluginID {
			delete(r.svcs, name)
			n++
		}
	}
	return n
}

// ---------- 共享 KV ----------

type kvEntry struct {
	value    string
	expireAt time.Time // 零值表示不过期
}

// KVStore 内核托管键值空间。value 一律字符串（调用方自行 JSON 编解码），
// 语义是"跨插件共享缓存"而非持久存储：内核重启即清空，需要落盘请走 db.*。
type KVStore struct {
	mu   sync.RWMutex
	data map[string]kvEntry
}

func NewKVStore() *KVStore { return &KVStore{data: make(map[string]kvEntry)} }

func (k *KVStore) Get(key string) (string, bool) {
	k.mu.RLock()
	e, ok := k.data[key]
	k.mu.RUnlock()
	if !ok {
		return "", false
	}
	if !e.expireAt.IsZero() && time.Now().After(e.expireAt) {
		k.mu.Lock()
		delete(k.data, key)
		k.mu.Unlock()
		return "", false
	}
	return e.value, true
}

func (k *KVStore) Set(key, value string, ttlSec int) {
	k.mu.Lock()
	e := kvEntry{value: value}
	if ttlSec > 0 {
		e.expireAt = time.Now().Add(time.Duration(ttlSec) * time.Second)
	}
	k.data[key] = e
	k.mu.Unlock()
}

func (k *KVStore) Del(key string) bool {
	k.mu.Lock()
	_, ok := k.data[key]
	delete(k.data, key)
	k.mu.Unlock()
	return ok
}

// Incr 数值自增；非数值按 0 起算。返回新值。
func (k *KVStore) Incr(key string, delta float64) float64 {
	k.mu.Lock()
	defer k.mu.Unlock()
	e := k.data[key]
	var cur float64
	if e.value != "" {
		if v, err := strconv.ParseFloat(e.value, 64); err == nil {
			cur = v
		}
	}
	nv := cur + delta
	e.value = strconv.FormatFloat(nv, 'f', -1, 64)
	k.data[key] = e
	return nv
}

// Keys 按前缀列举未过期的键。前缀为空则全列。
func (k *KVStore) Keys(prefix string) []string {
	now := time.Now()
	k.mu.Lock()
	defer k.mu.Unlock()
	out := make([]string, 0, len(k.data))
	for key, e := range k.data {
		if !e.expireAt.IsZero() && now.After(e.expireAt) {
			delete(k.data, key)
			continue
		}
		if prefix == "" || len(key) >= len(prefix) && key[:len(prefix)] == prefix {
			out = append(out, key)
		}
	}
	sort.Strings(out)
	return out
}

// ---------- 指标 ----------

type metricVal struct {
	N    int     `json:"n"`
	Sum  float64 `json:"sum"`
	Min  float64 `json:"min"`
	Max  float64 `json:"max"`
	Last float64 `json:"last"`
}

// Metrics 极简观测：每个名字一组 count/sum/min/max/last，够画趋势与找异常，
// 不引入直方图分桶的复杂度。冷路径调用，热路径上量请自行采样。
type Metrics struct {
	mu   sync.RWMutex
	data map[string]*metricVal
}

func NewMetrics() *Metrics { return &Metrics{data: make(map[string]*metricVal)} }

func (m *Metrics) Observe(name string, v float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	d := m.data[name]
	if d == nil {
		m.data[name] = &metricVal{N: 1, Sum: v, Min: v, Max: v, Last: v}
		return
	}
	d.N++
	d.Sum += v
	d.Last = v
	if v < d.Min {
		d.Min = v
	}
	if v > d.Max {
		d.Max = v
	}
}

func (m *Metrics) Snapshot() map[string]metricVal {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make(map[string]metricVal, len(m.data))
	for k, v := range m.data {
		out[k] = *v
	}
	return out
}

// ---------- 能力声明 ----------

// CapabilityRegistry 插件自述"我能做什么"→ 依赖解析与终端面板的查询入口。
// 与 ServiceRegistry 的分工：服务是"可被别人调用的接口"，能力是"我具备的语义标签"。
type CapabilityRegistry struct {
	mu   sync.RWMutex
	byPl map[string][]string // plugin -> caps
}

func NewCapabilityRegistry() *CapabilityRegistry {
	return &CapabilityRegistry{byPl: make(map[string][]string)}
}

func (r *CapabilityRegistry) Declare(pluginID string, caps []string) {
	r.mu.Lock()
	r.byPl[pluginID] = caps
	r.mu.Unlock()
}

func (r *CapabilityRegistry) Query(cap string) []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0)
	for pl, caps := range r.byPl {
		for _, c := range caps {
			if c == cap {
				out = append(out, pl)
				break
			}
		}
	}
	sort.Strings(out)
	return out
}

func (r *CapabilityRegistry) List() map[string][]string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string][]string, len(r.byPl))
	for k, v := range r.byPl {
		out[k] = append([]string(nil), v...)
	}
	return out
}

func (r *CapabilityRegistry) ForgetPlugin(pluginID string) {
	r.mu.Lock()
	delete(r.byPl, pluginID)
	r.mu.Unlock()
}

// ---------- 全局变换管线 ----------

// TransformEntry 一个全局变换节点：内核在事件扇出给所有订阅者之前，按 priority
// 升序逐个调用，把返回的 event.data 作为新的事件数据往下走；返回 null 则全局丢弃
// 该事件。这正是"节点间数据变换"的等价物——一个变换节点可以脱敏、翻译、补全、
// 改写任意事件，所有订阅者都看到变换后的事件，而不必各自实现一遍。
type TransformEntry struct {
	PluginID string `json:"plugin_id"`
	Action   string `json:"action"`
	Priority int    `json:"priority"`
}

// TransformPipeline 按 priority 有序维护变换节点列表（priority 相同按插件 ID 稳定排序）。
type TransformPipeline struct {
	mu      sync.RWMutex
	entries []TransformEntry
}

func NewTransformPipeline() *TransformPipeline { return &TransformPipeline{} }

func (t *TransformPipeline) Add(e TransformEntry) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.entries = append(t.entries, e)
	sort.SliceStable(t.entries, func(i, j int) bool {
		if t.entries[i].Priority != t.entries[j].Priority {
			return t.entries[i].Priority < t.entries[j].Priority
		}
		return t.entries[i].PluginID < t.entries[j].PluginID
	})
}

func (t *TransformPipeline) RemovePlugin(pluginID string) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	n := 0
	out := t.entries[:0]
	for _, e := range t.entries {
		if e.PluginID == pluginID {
			n++
			continue
		}
		out = append(out, e)
	}
	t.entries = out
	return n
}

func (t *TransformPipeline) List() []TransformEntry {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return append([]TransformEntry(nil), t.entries...)
}

// ---------- 内核级单例 ----------

var (
	extServices *ServiceRegistry
	extKV       *KVStore
	extMetrics  *Metrics
	extCaps     *CapabilityRegistry
	extPipeline *TransformPipeline
	kernelStart = time.Now()
)

// InitExtensions 装配扩展面（main 启动时调用一次，与 InitServices 并列）。
func InitExtensions() {
	extServices = NewServiceRegistry()
	extKV = NewKVStore()
	extMetrics = NewMetrics()
	extCaps = NewCapabilityRegistry()
	extPipeline = NewTransformPipeline()
}

// forgetPluginExtensions 插件断开：回收它声明的服务与能力及变换节点。
func forgetPluginExtensions(pluginID string) {
	if pluginID == "" {
		return
	}
	if extServices != nil {
		if n := extServices.ForgetPlugin(pluginID); n > 0 {
			logInfo("ext", "插件 %s 断开，回收服务 %d 条", pluginID, n)
		}
	}
	if extCaps != nil {
		extCaps.ForgetPlugin(pluginID)
	}
	if extPipeline != nil {
		if n := extPipeline.RemovePlugin(pluginID); n > 0 {
			logInfo("ext", "插件 %s 断开，回收变换节点 %d 个", pluginID, n)
		}
	}
}

// registerExtensionHandlers 注册扩展面方法。
func registerExtensionHandlers(srv *IpcServer, pm *PluginManager) {
	// service.register：插件声明自己提供的服务。
	srv.RegisterMethod(MethodServiceRegister, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Name    string                 `json:"name"`
			Version string                 `json:"version"`
			Caps    []string               `json:"caps"`
			Meta    map[string]interface{} `json:"meta"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID == "" {
			return nil, errors.New("service.register 前需先 plugin.register")
		}
		if req.Name == "" {
			return nil, errors.New("service.register 缺 name")
		}
		if extServices == nil {
			return nil, errors.New("扩展面未装配")
		}
		extServices.Register(&ServiceEntry{
			Name: req.Name, Provider: c.pluginID, Version: req.Version,
			Caps: req.Caps, Meta: req.Meta, Since: time.Now().Unix(),
		})
		logInfo("ext", "服务注册: %s ← %s (caps=%v)", req.Name, c.pluginID, req.Caps)
		return map[string]interface{}{"ok": true}, nil
	})

	// service.resolve：按名字解析，或按能力反查。二选一，name 优先。
	srv.RegisterMethod(MethodServiceResolve, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Name string `json:"name"`
			Cap  string `json:"cap"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if extServices == nil {
			return nil, errors.New("扩展面未装配")
		}
		if req.Name != "" {
			e, ok := extServices.Resolve(req.Name)
			if !ok {
				return map[string]interface{}{"found": false}, nil
			}
			return map[string]interface{}{"found": true, "service": e}, nil
		}
		if req.Cap != "" {
			list := extServices.ByCap(req.Cap)
			return map[string]interface{}{"found": len(list) > 0, "services": list}, nil
		}
		return nil, errors.New("service.resolve 需 name 或 cap")
	})

	srv.RegisterMethod(MethodServiceList, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		if extServices == nil {
			return nil, errors.New("扩展面未装配")
		}
		return map[string]interface{}{"services": extServices.List()}, nil
	})

	srv.RegisterMethod(MethodServiceUnregister, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if extServices == nil {
			return nil, errors.New("扩展面未装配")
		}
		e, ok := extServices.Resolve(req.Name)
		if !ok {
			return map[string]interface{}{"ok": true, "removed": false}, nil
		}
		// 只允许提供者本人撤销，防止插件互相摘除对方服务
		if e.Provider != c.pluginID {
			return nil, errors.New("服务 " + req.Name + " 由 " + e.Provider + " 提供，无权撤销")
		}
		return map[string]interface{}{"ok": true, "removed": extServices.ForgetPlugin(e.Provider) > 0}, nil
	})

	// plugin.call：插件间调用。内核按 ID 找到目标连接，转发并等待返回。
	// 调用方不持有对方进程句柄，对方重启后重新解析即可，天然容错。
	srv.RegisterMethod(MethodPluginCall, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Plugin  string                 `json:"plugin"`
			Action  string                 `json:"action"`
			Params  map[string]interface{} `json:"params"`
			Timeout int                    `json:"timeout_ms"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Plugin == "" {
			return nil, errors.New("plugin.call 缺 plugin")
		}
		target := srv.pluginConn(req.Plugin)
		if target == nil {
			return nil, errors.New("插件不在线: " + req.Plugin)
		}
		timeout := defaultCallTimeout
		if req.Timeout > 0 {
			timeout = time.Duration(req.Timeout) * time.Millisecond
		}
		if req.Params == nil {
			req.Params = map[string]interface{}{}
		}
		raw, err := target.Call(MethodPluginInvoke, map[string]interface{}{
			"action": req.Action,
			"params": req.Params,
			"from":   c.pluginID,
		}, timeout)
		if err != nil {
			return nil, err
		}
		var out interface{}
		if len(raw) > 0 {
			if uerr := json.Unmarshal(raw, &out); uerr != nil {
				out = string(raw)
			}
		}
		return map[string]interface{}{"ok": true, "result": out}, nil
	})

	// plugin.list：在线插件清单（连接侧），与 pm.ProcStats 的进程侧互补。
	srv.RegisterMethod(MethodPluginList, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		ids := srv.PluginIDs()
		items := make([]map[string]interface{}, 0, len(ids))
		for _, id := range ids {
			conn := srv.pluginConn(id)
			items = append(items, map[string]interface{}{
				"plugin_id": id,
				"online":    conn != nil,
			})
		}
		return map[string]interface{}{"plugins": items, "count": len(items)}, nil
	})

	// kv.*：内核托管共享 KV。
	srv.RegisterMethod(MethodKVGet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key string `json:"key"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Key == "" {
			return nil, errors.New("kv.get 缺 key")
		}
		if extKV == nil {
			return nil, errors.New("扩展面未装配")
		}
		v, ok := extKV.Get(req.Key)
		return map[string]interface{}{"found": ok, "value": v}, nil
	})

	srv.RegisterMethod(MethodKVSet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key   string `json:"key"`
			Value string `json:"value"`
			TTL   int    `json:"ttl"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Key == "" {
			return nil, errors.New("kv.set 缺 key")
		}
		if extKV == nil {
			return nil, errors.New("扩展面未装配")
		}
		extKV.Set(req.Key, req.Value, req.TTL)
		return map[string]interface{}{"ok": true}, nil
	})

	srv.RegisterMethod(MethodKVDel, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key string `json:"key"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if extKV == nil {
			return nil, errors.New("扩展面未装配")
		}
		return map[string]interface{}{"ok": true, "removed": extKV.Del(req.Key)}, nil
	})

	srv.RegisterMethod(MethodKVIncr, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key   string  `json:"key"`
			Delta float64 `json:"delta"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Key == "" {
			return nil, errors.New("kv.incr 缺 key")
		}
		if extKV == nil {
			return nil, errors.New("扩展面未装配")
		}
		if req.Delta == 0 {
			req.Delta = 1
		}
		return map[string]interface{}{"ok": true, "value": extKV.Incr(req.Key, req.Delta)}, nil
	})

	srv.RegisterMethod(MethodKVKeys, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Prefix string `json:"prefix"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if extKV == nil {
			return nil, errors.New("扩展面未装配")
		}
		return map[string]interface{}{"keys": extKV.Keys(req.Prefix)}, nil
	})

	// metric.*：统一观测出口。
	srv.RegisterMethod(MethodMetricObserve, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Name  string  `json:"name"`
			Value float64 `json:"value"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Name == "" {
			return nil, errors.New("metric.observe 缺 name")
		}
		if extMetrics == nil {
			return nil, errors.New("扩展面未装配")
		}
		extMetrics.Observe(req.Name, req.Value)
		return map[string]interface{}{"ok": true}, nil
	})

	srv.RegisterMethod(MethodMetricSnapshot, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		if extMetrics == nil {
			return nil, errors.New("扩展面未装配")
		}
		return map[string]interface{}{"metrics": extMetrics.Snapshot()}, nil
	})

	// capability.*：能力自述与查询。
	srv.RegisterMethod(MethodCapabilityDeclare, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Caps []string `json:"caps"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID == "" {
			return nil, errors.New("capability.declare 前需先 plugin.register")
		}
		if extCaps == nil {
			return nil, errors.New("扩展面未装配")
		}
		extCaps.Declare(c.pluginID, req.Caps)
		return map[string]interface{}{"ok": true}, nil
	})

	srv.RegisterMethod(MethodCapabilityQuery, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Cap string `json:"cap"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if extCaps == nil {
			return nil, errors.New("扩展面未装配")
		}
		if req.Cap == "" {
			return map[string]interface{}{"plugins": extCaps.List()}, nil
		}
		return map[string]interface{}{"plugins": extCaps.Query(req.Cap)}, nil
	})

	// lifecycle.state：内核自述（终端面板/握手探测用）。
	srv.RegisterMethod(MethodLifecycleState, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		state := map[string]interface{}{
			"uptime_sec": int64(time.Since(kernelStart).Seconds()),
			"plugins":    len(srv.PluginIDs()),
			"phase":      "running",
		}
		if extServices != nil {
			state["services"] = len(extServices.List())
		}
		if extKV != nil {
			state["kv_keys"] = len(extKV.Keys(""))
		}
		if pm != nil {
			state["proc"] = pm.ProcStats()
		}
		return state, nil
	})

	// pipeline.transform：注册一个全局变换节点。内核在事件扇出给所有订阅者之前，
	// 按 priority 升序调用各节点，把返回的 event.data 作为新事件；返回 null 全局丢弃。
	srv.RegisterMethod(MethodPipelineTransform, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Plugin  string `json:"plugin"`
			Action  string `json:"action"`
			Priority int   `json:"priority"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID == "" {
			return nil, errors.New("pipeline.transform 前需先 plugin.register")
		}
		if req.Action == "" {
			return nil, errors.New("pipeline.transform 缺 action")
		}
		if extPipeline == nil {
			return nil, errors.New("扩展面未装配")
		}
		extPipeline.Add(TransformEntry{PluginID: c.pluginID, Action: req.Action, Priority: req.Priority})
		logInfo("ext", "全局变换节点注册: %s/%s (priority=%d)", c.pluginID, req.Action, req.Priority)
		return map[string]interface{}{"ok": true}, nil
	})

	// pipeline.list：枚举当前生效的变换节点（终端面板/排障用）。
	srv.RegisterMethod(MethodPipelineList, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		if extPipeline == nil {
			return nil, errors.New("扩展面未装配")
		}
		return map[string]interface{}{"transforms": extPipeline.List()}, nil
	})
}
