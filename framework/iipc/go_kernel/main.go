// zcbot IIPC 主进程（Go 内核）：事件总线 + DB + 插件进程编排 + Go 原生插件。
// 拓扑：python main.py -iipc 启动本内核；Rust 适配器作为协议层连入（收内核 req/回 res、推入站事件）；
// Python 插件经 pluginhost 接入。适配器与协议细节全部外置到 Rust，内核只管事件/DB/编排。
package main

import (
	"flag"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func main() {
	pluginsDir := flag.String("plugins-dir", "../plugins", "插件目录")
	ipcAddr := flag.String("ipc-addr", "127.0.0.1:18730", "IIPC 连接地址")
	dbPath := flag.String("db", ":memory:", "SQLite DSN；:memory: 或 mem=纯 Go 内存引擎，其它值=文件路径（可落盘）")
	pluginHost := flag.String("plugin-host", "", "pluginhost.py 路径；设置则由单个 pluginhost 进程 fork/spawn 各插件（留空=内核逐插件直拉）")
	adminFlag := flag.String("admin", "", "管理员 user_id 列表（逗号分隔），用于命令级权限")
	logFile := flag.String("log-file", "", "日志文件路径（空=仅 stdout）")
	logLevel := flag.String("log-level", "info", "日志级别: debug|info|warn|error")
	dataDir := flag.String("data-dir", "plugins_dat", "插件数据目录（ctx.get_data_dir 的根）")
	flag.Parse()

	InitLogger(*logFile, parseLevel(*logLevel))

	auth := "dev-auth"
	store, err := NewStore(*dbPath)
	if err != nil {
		logWarn("db", "SQLite 初始化失败(%v)，回退真内存引擎 :memory:", err)
		store, _ = NewSqliteStore(":memory:")
	}
	EnsureSchema(store) // 核心表（1:1 数据面）：users/group_members/commands/plugin_configs/perm_*/audit_logs...
	bus := NewEventBus()
	srv := NewIpcServer(*ipcAddr, auth, bus, store)

	// 管理员集合（命令级权限 + users.role=super 同步）
	admins := map[string]bool{}
	for _, a := range strings.Split(*adminFlag, ",") {
		a = strings.TrimSpace(a)
		if a != "" {
			admins[a] = true
		}
	}
	SeedSuperAdmins(store, admins)

	absPlugins, _ := filepath.Abs(*pluginsDir)
	absSdk := filepath.Join(filepath.Dir(absPlugins), "py_sdk")
	pm := NewPluginManager(*ipcAddr, auth, os.Getenv("ZCBOT_PYTHON"), absPlugins, absSdk)

	// 编排服务：cron 调度 / 会话 / 群插件开关 / 权限解析
	InitServices(bus, store)
	pluginsDatDir = *dataDir
	srv.SetOrchestration(sessMgr, sched, grpSw)

	registerCoreHandlers(srv, bus, store, pm, admins)

	go func() {
		if err := srv.Run(); err != nil {
			logError("ipc", "服务端失败: %v", err)
			os.Exit(1)
		}
	}()

	time.Sleep(150 * time.Millisecond)

	if *pluginHost != "" {
		pm.LaunchHost(*pluginHost)
	} else {
		pm.DiscoverAndLaunch()
		pm.Watch(2 * time.Second)
	}

	// 优雅关闭：退出时杀掉所有子进程，避免孤儿
	sigc := make(chan os.Signal, 1)
	signal.Notify(sigc, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigc
		logInfo("main", "收到退出信号，关闭子进程")
		pm.Shutdown()
		os.Exit(0)
	}()

	select {}
}

// buildSendParams 把插件的 send 请求规整为 OneBot send_msg 参数（群/私聊判定）。
func buildSendParams(req map[string]interface{}) map[string]interface{} {
	params := map[string]interface{}{}
	if v, ok := req["message"]; ok {
		params["message"] = v
	}
	if gid, ok := toFloat(req["group_id"]); ok && gid != 0 {
		params["message_type"] = "group"
		params["group_id"] = int64(gid)
	} else {
		params["message_type"] = "private"
		if uid, ok := toFloat(req["user_id"]); ok {
			params["user_id"] = int64(uid)
		}
	}
	return params
}

func parseLevel(s string) LogLevel {
	switch strings.ToLower(s) {
	case "debug":
		return LevelDebug
	case "warn", "warning":
		return LevelWarn
	case "error":
		return LevelError
	default:
		return LevelInfo
	}
}

func adminList(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func toFloat(v interface{}) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case string:
		if f, err := strconv.ParseFloat(x, 64); err == nil {
			return f, true
		}
		return 0, false
	default:
		return 0, false
	}
}
