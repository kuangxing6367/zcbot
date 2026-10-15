// 定时任务调度器（对齐 core_plugins/scheduler + framework/scheduler 的 cron 语义）：
// 插件经 task.register 上报 (cron_expr, handler)，内核按 cron 到期向该插件
// 推送 task.tick 事件，由插件进程本地执行 handler——执行体永远在插件侧，
// 内核只负责"什么时候该跑"，与 zcbot 原框架职责划分一致。
//
// cron 支持标准 5 段：分 时 日 月 周，字段语法 * / , - 数字（周 0-7，7=周日）。
package main

import (
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// cronField 单个字段的匹配集合。
type cronField struct {
	wild bool // '*' 且无 step
	set  map[int]bool
}

func (f cronField) match(v int) bool {
	if f.wild {
		return true
	}
	return f.set[v]
}

// cronSpec 解析后的 cron 表达式。
type cronSpec struct {
	minute cronField
	hour   cronField
	dom    cronField
	month  cronField
	dow    cronField
	domStar bool // 原实现语义：日与周均为 * 时按 OR 处理，否则按 AND
	dowStar bool
}

// parseCron 解析 5 段 cron；失败返回 error（调用方应拒登记并告警）。
func parseCron(expr string) (*cronSpec, error) {
	parts := strings.Fields(strings.TrimSpace(expr))
	if len(parts) != 5 {
		return nil, errCronFields
	}
	mn, err := parseCronField(parts[0], 0, 59)
	if err != nil {
		return nil, err
	}
	hr, err := parseCronField(parts[1], 0, 23)
	if err != nil {
		return nil, err
	}
	dom, err := parseCronField(parts[2], 1, 31)
	if err != nil {
		return nil, err
	}
	mon, err := parseCronField(parts[3], 1, 12)
	if err != nil {
		return nil, err
	}
	dow, err := parseCronField(parts[4], 0, 6)
	if err != nil {
		return nil, err
	}
	return &cronSpec{
		minute: mn, hour: hr, dom: dom, month: mon, dow: dow,
		domStar: parts[2] == "*", dowStar: parts[4] == "*",
	}, nil
}

func parseCronField(s string, lo, hi int) (cronField, error) {
	f := cronField{set: map[int]bool{}}
	for _, tok := range strings.Split(s, ",") {
		tok = strings.TrimSpace(tok)
		if tok == "" {
			return f, errCronField
		}
		step := 1
		if i := strings.Index(tok, "/"); i >= 0 {
			stepStr := tok[i+1:]
			tok = tok[:i]
			n, err := strconv.Atoi(stepStr)
			if err != nil || n <= 0 {
				return f, errCronField
			}
			step = n
		}
		var start, end int
		switch {
		case tok == "*":
			start, end = lo, hi
			if step == 1 {
				f.wild = true
			}
		default:
			if i := strings.Index(tok, "-"); i >= 0 {
				a, err1 := strconv.Atoi(strings.TrimSpace(tok[:i]))
				b, err2 := strconv.Atoi(strings.TrimSpace(tok[i+1:]))
				if err1 != nil || err2 != nil {
					return f, errCronField
				}
				start, end = a, b
			} else {
				n, err := strconv.Atoi(tok)
				if err != nil {
					return f, errCronField
				}
				start = n
				end = hi
				if step == 1 {
					end = n // 单值只匹配自身
				}
			}
		}
		if start < lo || end > hi || start > end {
			return f, errCronRange
		}
		for v := start; v <= end; v += step {
			f.set[v] = true
		}
	}
	if len(f.set) == 0 {
		return f, errCronField
	}
	return f, nil
}

// matches 判断某时刻是否命中（日/周按标准 cron 的 OR 语义）。
func (c *cronSpec) matches(t time.Time) bool {
	if !c.minute.match(t.Minute()) || !c.hour.match(t.Hour()) || !c.month.match(int(t.Month())) {
		return false
	}
	dow := int(t.Weekday()) // Go: Sunday=0
	domOK := c.dom.match(t.Day())
	dowOK := c.dow.match(dow)
	if c.domStar && c.dowStar {
		return true
	}
	if c.domStar {
		return dowOK
	}
	if c.dowStar {
		return domOK
	}
	return domOK && dowOK
}

var (
	errCronFields = errStr("cron 需要 5 段（分 时 日 月 周）")
	errCronField  = errStr("cron 字段非法")
	errCronRange  = errStr("cron 字段越界")
)

type errStr string

func (e errStr) Error() string { return string(e) }

// taskEntry 一个已登记的定时任务。
type taskEntry struct {
	pluginID string
	handler  string
	cron     *cronSpec
	lastFire string // 已触发的分钟键（YYYY-MM-DD HH:MM），防止同一分钟重复触发
}

// TaskScheduler 定时任务调度器。
type TaskScheduler struct {
	mu    sync.Mutex
	tasks map[string]*taskEntry // key = pluginID + "/" + handler
	bus   *EventBus
}

func NewTaskScheduler(bus *EventBus) *TaskScheduler {
	return &TaskScheduler{tasks: map[string]*taskEntry{}, bus: bus}
}

// Register 登记（或覆盖）一个定时任务。
func (ts *TaskScheduler) Register(pluginID, handler, cronExpr string) error {
	spec, err := parseCron(cronExpr)
	if err != nil {
		return err
	}
	ts.mu.Lock()
	defer ts.mu.Unlock()
	ts.tasks[pluginID+"/"+handler] = &taskEntry{
		pluginID: pluginID, handler: handler, cron: spec,
	}
	logInfo("scheduler", "插件 %s 登记定时任务 %s cron=%q", pluginID, handler, cronExpr)
	return nil
}

// UnregisterPlugin 插件断开时清理其全部任务。
func (ts *TaskScheduler) UnregisterPlugin(pluginID string) {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	for k, e := range ts.tasks {
		if e.pluginID == pluginID {
			delete(ts.tasks, k)
		}
	}
}

// Run 调度主循环：每 30s 对齐检查一次分钟边界，命中则向所属插件推 task.tick。
// 30s 粒度足够（cron 最小单位为分钟），且空转开销可忽略。
func (ts *TaskScheduler) Run() {
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for now := range ticker.C {
			ts.tick(now)
		}
	}()
}

func (ts *TaskScheduler) tick(now time.Time) {
	ts.mu.Lock()
	entries := make([]*taskEntry, 0, len(ts.tasks))
	for _, e := range ts.tasks {
		entries = append(entries, e)
	}
	ts.mu.Unlock()
	if len(entries) == 0 {
		return
	}
	minuteKey := now.Format("2006-01-02 15:04")
	// 按 plugin 聚合，一次 Publish 推一个插件（Bus 按订阅连接扇出）
	byPlugin := map[string][]string{}
	for _, e := range entries {
		if e.lastFire == minuteKey {
			continue
		}
		if !e.cron.matches(now) {
			continue
		}
		e.lastFire = minuteKey
		byPlugin[e.pluginID] = append(byPlugin[e.pluginID], e.handler)
	}
	for pluginID, handlers := range byPlugin {
		sort.Strings(handlers)
		ev := Event{Type: "task.tick", Data: map[string]interface{}{
			"plugin_id": pluginID,
			"handlers":  handlers,
			"ts":        now.Unix(),
		}}
		// task.tick 只给所属插件：借助 Bus 的定向推送能力不可用（按类型订阅），
		// 这里直接查订阅连接并按 pluginID 过滤。
		ts.bus.PublishTo(ev, pluginID)
		logDebug("scheduler", "触发 %s 的定时任务 %v", pluginID, handlers)
	}
}
