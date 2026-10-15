// IIPC 信封协议：复用 zcbot IPC 思路（行分隔 JSON），但传输由 Go 自管（非 multiprocessing.connection）。
// 核心创新：核心→插件"事件推送"，插件→核心"动作批量回传"，规避旧 IPC 逐调用同步往返（8 倍削弱根因）。
package main

import "encoding/json"

// Frame 是连接上传输的最小单元，一行一条，JSON 编码。
type Frame struct {
	T       string          `json:"t"`       // req | res | evt | ping | pong
	ID      int64           `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   string          `json:"error,omitempty"`
	Channel string          `json:"channel,omitempty"`
	Payload json.RawMessage `json:"payload,omitempty"`
}

// 由 Go 服务端提供、Python 插件侧调用的方法
const (
	MethodPluginRegister  = "plugin.register"
	MethodDBQuery         = "db.query"
	MethodDBExec          = "db.exec"
	MethodDBInsert        = "db.insert"
	MethodMsgSend         = "msg.send"
	MethodMsgSendBatch    = "msg.send_batch"
	MethodConfigGet       = "config.get"
	MethodConfigSet       = "config.set"
	MethodAdapterRegister = "adapter.register"
	MethodApiCall         = "api.call"
	MethodDebugInject     = "debug.inject"
	MethodLogBatch        = "log.batch"

	// 1:1 补齐：定时任务 / 会话 / 群插件开关 / 权限 / 数据目录 / 命令注册
	MethodTaskRegister    = "task.register"
	MethodSessionWait     = "session.wait"
	MethodGroupPluginSet  = "group_plugin.set"
	MethodGroupPluginGet  = "group_plugin.get"
	MethodPermHasPerm     = "perm.has_perm"
	MethodPermUserGroups  = "perm.user_groups"
	MethodDataDir         = "data.dir"
	MethodPluginCommands  = "plugin.commands"
	MethodEventEmit       = "event.emit"
)

// EventChannel 事件广播通道名
const EventChannel = "event"

// Event 是核心→插件推送的事件载荷
type Event struct {
	Type string                 `json:"type"`
	Data map[string]interface{} `json:"data"`
}
