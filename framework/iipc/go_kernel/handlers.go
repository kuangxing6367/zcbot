// 内核 IIPC 方法注册：把核心能力（适配器注册/调试注入/插件注册/DB/发送/api/配置）
// 集中注册，供 main 与测试共用（避免处理器内联在 main 里无法复用）。
package main

import (
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"time"
)

// normalizeArgs 把 JSON 数字参数收敛为整型（float64 且为整数 → int64）。
// 背景：插件按框架习惯传 int user_id，JSON 到 Go 是 float64；SQLite 会把
// float64 绑成 REAL，与 TEXT 亲和性列（user_id TEXT）比较时
// CAST(10001.0 AS TEXT) = '10001.0' 匹配不上，导致"查不到人"。
// 收敛为 int64 后绑 INTEGER，TEXT 列比较时转 '10001'，匹配正常。
func normalizeArgs(args []interface{}) []interface{} {
	if args == nil {
		return nil
	}
	out := make([]interface{}, len(args))
	for i, a := range args {
		if f, ok := a.(float64); ok && f == math.Trunc(f) && !math.IsInf(f, 0) {
			out[i] = int64(f)
			continue
		}
		out[i] = a
	}
	return out
}

// registerCoreHandlers 在 srv 上注册全部内核方法。
// sendGo 供 Go 原生插件回发动作；admins 用于命令级权限下发。
func registerCoreHandlers(srv *IpcServer, bus *EventBus, store Store, pm *PluginManager, admins map[string]bool) {
	// 适配器注册：Rust 协议层连入后调用，标记角色 + self_id 路由。
	srv.RegisterMethod(MethodAdapterRegister, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			SelfID string `json:"self_id"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		srv.registerAdapter(c, req.SelfID)
		return map[string]interface{}{"ok": true}, nil
	})

	// 调试注入：终端/测试无需真实适配器即可把事件灌进总线。
	srv.RegisterMethod(MethodDebugInject, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Type string                 `json:"type"`
			Data map[string]interface{} `json:"data"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Type == "" {
			return nil, errors.New("debug.inject 缺 type 字段")
		}
		bus.Publish(Event{Type: req.Type, Data: req.Data})
		return map[string]interface{}{"ok": true}, nil
	})

	// 插件注册：订阅事件 + 下发命令权限表/管理员。
	srv.RegisterMethod(MethodPluginRegister, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			PluginID   string   `json:"plugin_id"`
			EventTypes []string `json:"event_types"`
			Commands   []struct {
				Pattern      string `json:"pattern"`
				Alias        string `json:"alias"`
				Description  string `json:"description"`
				Priority     int    `json:"priority"`
				Handler      string `json:"handler"`
				RequireLevel string `json:"require_level"`
				RequirePerm  string `json:"require_perm"`
				IsDynamic    bool   `json:"is_dynamic"`
			} `json:"commands"`
			Tasks []struct {
				Handler string `json:"handler"`
				Cron    string `json:"cron"`
			} `json:"tasks"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.PluginID == "" {
			return nil, errors.New("plugin.register 缺 plugin_id")
		}
		c.pluginID = req.PluginID
		grpSw.NotePlugin(req.PluginID) // 群插件开关需要知道插件存在
		for _, et := range req.EventTypes {
			bus.Subscribe(et, c)
		}
		// 命令落库（commands 表，Web/CLI 可查；匹配仍在插件侧，零 DB 热路径）
		for _, cmd := range req.Commands {
			if _, err := store.Exec(
				"INSERT INTO commands(plugin_name, pattern, alias, description, priority, handler, "+
					"is_dynamic, require_level, require_perm, is_active, hit_count, created_at) "+
					"VALUES(?,?,?,?,?,?,?,?,?,1,0,datetime('now')) "+
					"ON CONFLICT(plugin_name, handler) DO UPDATE SET pattern=excluded.pattern, "+
					"alias=excluded.alias, description=excluded.description, priority=excluded.priority, "+
					"require_level=excluded.require_level, require_perm=excluded.require_perm",
				req.PluginID, cmd.Pattern, cmd.Alias, cmd.Description, cmd.Priority, cmd.Handler,
				boolToInt(cmd.IsDynamic), cmd.RequireLevel, cmd.RequirePerm); err != nil {
				logWarn("db", "命令落库失败 %s/%s: %v", req.PluginID, cmd.Handler, err)
			}
		}
		logInfo("ipc", "插件 %s 注册，订阅事件=%v，命令=%d，任务=%d",
			req.PluginID, req.EventTypes, len(req.Commands), len(req.Tasks))
		return map[string]interface{}{
			"ok":       true,
			"cmd_perm": pm.cmdPermFor(req.PluginID),
			"admins":   adminList(admins),
		}, nil
	})

	srv.RegisterMethod(MethodDBQuery, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			SQL  string        `json:"sql"`
			Args []interface{} `json:"args"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		// 方言翻译：zcbot 插件按 MySQL 习惯写 %s，SQLite 只认 ?
		rows, err := store.Query(TranslateSQL(req.SQL), normalizeArgs(req.Args)...)
		if err != nil {
			return nil, err
		}
		return map[string]interface{}{"rows": rows}, nil
	})

	// db.insert：INSERT 专用通道，返回 last insert id（对齐框架 db_insert 的 lastrowid 语义）。
	srv.RegisterMethod(MethodDBInsert, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			SQL  string        `json:"sql"`
			Args []interface{} `json:"args"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		id, err := store.Insert(TranslateSQL(req.SQL), normalizeArgs(req.Args)...)
		if err != nil {
			return nil, err
		}
		return map[string]interface{}{"id": id}, nil
	})

	srv.RegisterMethod(MethodDBExec, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			SQL  string        `json:"sql"`
			Args []interface{} `json:"args"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		affected, err := store.Exec(TranslateSQL(req.SQL), normalizeArgs(req.Args)...)
		if err != nil {
			return nil, err
		}
		return map[string]interface{}{"affected": affected}, nil
	})

	// Go 原生插件动作回发（经适配器出口）。
	sendGo := func(text string, ev Event) {
		params := map[string]interface{}{"message": text}
		if gid, ok := toFloat(ev.Data["group_id"]); ok && gid != 0 {
			params["message_type"] = "group"
			params["group_id"] = int64(gid)
		} else {
			params["message_type"] = "private"
			if uid, ok := toFloat(ev.Data["user_id"]); ok {
				params["user_id"] = int64(uid)
			}
		}
		if _, err := srv.CallAdapter("send_msg", params, strVal(ev.Data["self_id"])); err != nil {
			logWarn("goplugin", "动作回发失败: %v", err)
		}
	}
	bus.SetPublishHook(func(ev Event) {
		dispatchGoPlugins(ev, admins, sendGo)
	})

	// msg.send：插件动作 → 适配器（尽力而为，失败仅告警，不打断插件 flush）。
	srv.RegisterMethod(MethodMsgSend, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req map[string]interface{}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if _, err := srv.CallAdapter("send_msg", buildSendParams(req), ""); err != nil {
			logWarn("send", "plugin=%s send_msg 失败: %v", c.pluginID, err)
			return map[string]interface{}{"ok": false, "error": err.Error()}, nil
		}
		return map[string]interface{}{"ok": true}, nil
	})

	srv.RegisterMethod(MethodMsgSendBatch, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Items []map[string]interface{} `json:"items"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		n := 0
		for _, it := range req.Items {
			if _, err := srv.CallAdapter("send_msg", buildSendParams(it), ""); err != nil {
				logWarn("send", "plugin=%s batch 第%d条失败: %v", c.pluginID, n, err)
				continue
			}
			n++
		}
		return map[string]interface{}{"count": n}, nil
	})

	// api.call：通用 API 通道，必须拿返回值 → 透传适配器结果/错误。
	srv.RegisterMethod(MethodApiCall, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Action string                 `json:"action"`
			Params map[string]interface{} `json:"params"`
			SelfID string                 `json:"self_id"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Action == "" {
			return nil, errors.New("api.call 缺 action")
		}
		res, err := srv.CallAdapter(req.Action, req.Params, req.SelfID)
		if err != nil {
			return nil, err
		}
		return json.RawMessage(res), nil
	})

	// log.batch：插件日志汇入口（对齐原 zcbot 日志体系）。
	// 插件侧 IIPCLogger 攒批后 notify 过来，由内核统一格式输出到 stdout，
	// 最终经 launcher 日志出口落盘（config.yaml log.file）。
	srv.RegisterMethod(MethodLogBatch, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Items []map[string]interface{} `json:"items"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		for _, it := range req.Items {
			level := strVal(it["level"])
			msg := strVal(it["msg"])
			switch level {
			case "DEBUG":
				logDebug("plugin "+c.pluginID, "%s", msg)
			case "WARNING", "WARN":
				logWarn("plugin "+c.pluginID, "%s", msg)
			case "ERROR", "CRITICAL":
				logError("plugin "+c.pluginID, "%s", msg)
			default:
				logInfo("plugin "+c.pluginID, "%s", msg)
			}
		}
		return map[string]interface{}{"count": len(req.Items)}, nil
	})

	// config.get/set：插件级配置，落 plugin_configs 表（对齐 ctx.get_config 语义：
	// 按 plugin_name + config_key 存取，值 JSON 编解码由插件侧 SDK 负责）。
	// pluginID 为空（调试直连）时退回全局 config 表。
	srv.RegisterMethod(MethodConfigGet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key     string `json:"key"`
			Default string `json:"default"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID != "" {
			rows, err := store.Query(
				"SELECT config_value FROM plugin_configs WHERE plugin_name = ? AND config_key = ?",
				c.pluginID, req.Key)
			if err != nil {
				return nil, err
			}
			if len(rows) == 0 {
				return map[string]interface{}{"value": req.Default}, nil
			}
			return map[string]interface{}{"value": strVal(rows[0]["config_value"])}, nil
		}
		v, err := store.GetConfig(req.Key)
		if err != nil {
			return nil, err
		}
		if v == "" {
			v = req.Default
		}
		return map[string]interface{}{"value": v}, nil
	})

	srv.RegisterMethod(MethodConfigSet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Key   string `json:"key"`
			Value string `json:"value"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID != "" {
			if _, err := store.Exec(
				"INSERT INTO plugin_configs(plugin_name, config_key, config_value, created_at, updated_at) "+
					"VALUES(?,?,?,datetime('now'),datetime('now')) "+
					"ON CONFLICT(plugin_name, config_key) DO UPDATE SET config_value=excluded.config_value, "+
					"updated_at=excluded.updated_at",
				c.pluginID, req.Key, req.Value); err != nil {
				return nil, err
			}
			return map[string]interface{}{"ok": true}, nil
		}
		if err := store.SetConfig(req.Key, req.Value); err != nil {
			return nil, err
		}
		return map[string]interface{}{"ok": true}, nil
	})

	// ── 1:1 补齐：定时任务 / 会话 / 群插件开关 / 权限 / 数据目录 ──

	// task.register：插件登记 cron 定时任务，内核到期推 task.tick。
	srv.RegisterMethod(MethodTaskRegister, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Handler string `json:"handler"`
			Cron    string `json:"cron"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID == "" || req.Handler == "" || req.Cron == "" {
			return nil, errors.New("task.register 缺 plugin_id/handler/cron")
		}
		if sched == nil {
			return nil, errors.New("调度器未装配")
		}
		if err := sched.Register(c.pluginID, req.Handler, req.Cron); err != nil {
			return nil, err
		}
		if _, err := store.Exec(
			"INSERT INTO tasks(plugin_name, cron_expression, handler, enabled, created_at) "+
				"VALUES(?,?,?,1,datetime('now')) "+
				"ON CONFLICT(plugin_name, handler) DO UPDATE SET cron_expression=excluded.cron_expression, "+
				"enabled=1",
			c.pluginID, req.Cron, req.Handler); err != nil {
			logWarn("db", "任务落库失败 %s/%s: %v", c.pluginID, req.Handler, err)
		}
		return map[string]interface{}{"ok": true}, nil
	})

	// session.wait：阻塞等待同用户同上下文的下一条消息（对齐 ctx.wait_for）。
	srv.RegisterMethod(MethodSessionWait, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			UserID  string  `json:"user_id"`
			GroupID string  `json:"group_id"`
			Timeout float64 `json:"timeout"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if c.pluginID == "" {
			return nil, errors.New("session.wait 缺 plugin_id（未注册连接）")
		}
		if sessMgr == nil {
			return nil, errors.New("会话管理器未装配")
		}
		timeout := 60 * time.Second
		if req.Timeout > 0 {
			timeout = time.Duration(req.Timeout * float64(time.Second))
		}
		ev, ok := sessMgr.Wait(c, c.pluginID, req.UserID, req.GroupID, timeout)
		if !ok {
			return map[string]interface{}{"ok": false, "timeout": true}, nil
		}
		return map[string]interface{}{"ok": true, "event": ev}, nil
	})

	// group_plugin.set/get：群级插件开关（对齐 loader 系列）。
	srv.RegisterMethod(MethodGroupPluginSet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			GroupID string `json:"group_id"`
			Plugin  string `json:"plugin"`
			Enabled bool   `json:"enabled"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.GroupID == "" || req.Plugin == "" {
			return nil, errors.New("group_plugin.set 缺 group_id/plugin")
		}
		if grpSw == nil {
			return nil, errors.New("群插件开关未装配")
		}
		if err := grpSw.Set(req.GroupID, req.Plugin, req.Enabled); err != nil {
			return nil, err
		}
		return map[string]interface{}{"ok": true}, nil
	})

	srv.RegisterMethod(MethodGroupPluginGet, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			GroupID string `json:"group_id"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if grpSw == nil {
			return nil, errors.New("群插件开关未装配")
		}
		return map[string]interface{}{"status": grpSw.Get(req.GroupID)}, nil
	})

	// perm.has_perm / perm.user_groups：节点式权限（对齐 ctx.has_perm / user_groups）。
	srv.RegisterMethod(MethodPermHasPerm, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			UserID  string            `json:"user_id"`
			Node    string            `json:"node"`
			Context map[string]string `json:"context"`
			Role    string            `json:"role"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.UserID == "" || req.Node == "" {
			return nil, errors.New("perm.has_perm 缺 user_id/node")
		}
		if permRes == nil {
			return nil, errors.New("权限解析器未装配")
		}
		ok := permRes.HasPerm(req.UserID, req.Node, req.Context, req.Role)
		return map[string]interface{}{"ok": ok}, nil
	})

	srv.RegisterMethod(MethodPermUserGroups, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			UserID  string            `json:"user_id"`
			Context map[string]string `json:"context"`
			Role    string            `json:"role"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		return map[string]interface{}{"groups": permRes.UserGroups(req.UserID, req.Context, req.Role)}, nil
	})

	// event.emit：插件发布事件到总线（对齐 ctx.emit；广播给全部订阅者）。
	srv.RegisterMethod(MethodEventEmit, func(c *Conn, p json.RawMessage) (interface{}, error) {
		var req struct {
			Type string                 `json:"type"`
			Data map[string]interface{} `json:"data"`
		}
		if err := json.Unmarshal(p, &req); err != nil {
			return nil, err
		}
		if req.Type == "" {
			return nil, errors.New("event.emit 缺 type 字段")
		}
		if req.Data == nil {
			req.Data = map[string]interface{}{}
		}
		bus.Publish(Event{Type: req.Type, Data: req.Data})
		return map[string]interface{}{"ok": true}, nil
	})

	// data.dir：插件数据目录（对齐 ctx.get_data_dir：plugins_dat/<plugin>/）。
	srv.RegisterMethod(MethodDataDir, func(c *Conn, p json.RawMessage) (interface{}, error) {
		_ = p
		if c.pluginID == "" {
			return nil, errors.New("data.dir 缺 plugin_id（未注册连接）")
		}
		dir := filepath.Join(pluginsDatDir, c.pluginID)
		if err := ensureDir(dir); err != nil {
			return nil, err
		}
		return map[string]interface{}{"path": dir}, nil
	})
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
