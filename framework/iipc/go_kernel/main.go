// zcbot IIPC 主进程（Go 内核）：事件总线 + DB + 插件进程编排 + Go 原生插件。
// 拓扑：python main.py -iipc 启动本内核；Rust 适配器作为协议层连入（收内核 req/回 res、推入站事件）；
// Python 插件经 pluginhost 接入。适配器与协议细节全部外置到 Rust，内核只管事件/DB/编排。
package main

import (
	"encoding/json"
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
		// 文件型 DB 损坏/不可用：先原子隔离坏文件，避免内核下次启动再次加载同一坏文件，
		// 陷入"告警+静默丢数据"死循环；再明确告知运维持久化已失效，而非含糊的"回退真内存引擎"。
		if *dbPath != "" && *dbPath != "mem" && *dbPath != ":memory:" {
			isolateCorruptDB(*dbPath)
		}
		logWarn("db", "SQLite 初始化失败(%v)：已隔离坏文件并向内存引擎 :memory: 回退。" +
			"【持久化已失效】本次运行写入的数据重启即丢失，请用隔离文件恢复备份后以原路径重启。", err)
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

	// 扩展面：服务注册表 / 插件间调用 / 共享 KV / 指标 / 能力声明
	InitExtensions()
	registerExtensionHandlers(srv, pm)

	// 全局变换管线：内核在事件扇出给所有订阅者之前，按 priority 升序调用各变换节点，
	// 把返回的 event.data 作为新事件；节点返回 null 则全局丢弃该事件。零节点时此钩子为空操作。
	bus.SetTransformHook(func(ev *Event) bool {
		if extPipeline == nil {
			return false
		}
		xs := extPipeline.List()
		if len(xs) == 0 {
			return false
		}
		for _, x := range xs {
			target := srv.pluginConn(x.PluginID)
			if target == nil {
				logWarn("pipeline", "变换节点 %s 不在线，跳过", x.PluginID)
				continue
			}
			raw, err := target.Call(MethodPluginInvoke, map[string]interface{}{
				"action": x.Action,
				"params": ev.Data,
				"from":   "kernel",
			}, 2*time.Second)
			if err != nil {
				logWarn("pipeline", "变换节点 %s/%s 调用失败(已跳过): %v", x.PluginID, x.Action, err)
				continue
			}
			var res map[string]interface{}
			if len(raw) > 0 && string(raw) != "null" {
				if uerr := json.Unmarshal(raw, &res); uerr != nil {
					logWarn("pipeline", "变换节点 %s/%s 返回非 JSON(已跳过): %v", x.PluginID, x.Action, uerr)
					continue
				}
			}
			if res == nil {
				// 变换节点显式返回 null → 丢弃该事件
				logInfo("pipeline", "变换节点 %s/%s 丢弃事件 type=%s", x.PluginID, x.Action, ev.Type)
				return true
			}
			ev.Data = res
		}
		return false
	})

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

// isolateCorruptDB 把损坏/不可用的数据库文件原子隔离为 <名>.corrupt-<时间戳>，
// 避免内核下次启动再次加载同一坏文件、反复触发"告警+静默丢数据"死循环。
// 改名失败（如文件被占用）则退化为复制隔离，绝不阻塞启动。
func isolateCorruptDB(path string) {
	ts := time.Now().Format("20060102-150405")
	dir := filepath.Dir(path)
	base := filepath.Base(path)
	target := filepath.Join(dir, base+".corrupt-"+ts)
		if err := os.Rename(path, target); err != nil {
		logWarn("db", "坏文件隔离改名失败(%v)，尝试复制隔离", err)
		if data, rerr := os.ReadFile(path); rerr == nil {
			if werr := os.WriteFile(target, data, 0o600); werr != nil {
				logWarn("db", "坏文件复制隔离也失败(%v)，仅告警不阻塞", werr)
				return
			}
			// 复制成功：尽量删掉源文件，避免内核下次启动再次加载同一坏文件陷入"告警+静默丢数据"死循环。
			if rerr2 := os.Remove(path); rerr2 != nil {
				logWarn("db", "坏文件已复制隔离为 %s，但源文件删除失败(%v)；若仍残留，请手动清理以防重复告警", target, rerr2)
			}
		}
	}
	logWarn("db", "坏文件已隔离为: %s（如需恢复，请先校验再用它替换原路径）", target)
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
