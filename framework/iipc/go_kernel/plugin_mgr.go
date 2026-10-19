// 插件进程管理器：发现 plugins/ 下每个子目录为一个插件，各自独立进程启动（LangBot 式隔离 GIL）。
// Go 主进程扮演"编排层"，每个 Python 插件独立进程、各自一个 GIL。
// 支持 plugin.json 清单（校验 id、注册事件类型与命令前缀）、崩溃看门狗重启、目录变更热重载。
package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

// maxPluginRestarts 插件连续崩溃自动重启的上限；超过后停止重启并告警，
// 避免"崩溃炸弹"（启动即崩的插件每 1s 拉起一个完整 Python 解释器，吞内存/CPU）。
// 干净退出（exit 0）会重置计数，故偶发崩溃（非持续崩溃循环）不会触发本上限。
const maxPluginRestarts = 5

type PluginManager struct {
	addr       string
	auth       string
	python     string
	pluginsDir string
	sdkDir     string
	procs      map[string]*exec.Cmd
	manifests  map[string]*PluginManifest
	restarts   map[string]int
	stopped    bool
	mu         sync.Mutex
}

func NewPluginManager(addr, auth, python, pluginsDir, sdkDir string) *PluginManager {
	if python == "" {
		python = "python"
	}
	return &PluginManager{
		addr:       addr,
		auth:       auth,
		python:     python,
		pluginsDir: pluginsDir,
		sdkDir:     sdkDir,
		procs:      make(map[string]*exec.Cmd),
		manifests:  make(map[string]*PluginManifest),
		restarts:   make(map[string]int),
	}
}

// isStopped 编排层是否已进入停止流程（优雅关闭时不再拉起任何子进程）。
func (pm *PluginManager) isStopped() bool {
	pm.mu.Lock()
	defer pm.mu.Unlock()
	return pm.stopped
}

// Stop 优雅关闭：置停止标志并杀掉全部子进程。看门狗随后检测到 stopped 不会重启。
func (pm *PluginManager) Stop() {
	pm.mu.Lock()
	pm.stopped = true
	for id, c := range pm.procs {
		if c != nil && c.Process != nil {
			_ = c.Process.Kill()
		}
		delete(pm.procs, id)
	}
	pm.mu.Unlock()
	logInfo("pm", "已关闭全部子进程")
}

// DiscoverAndLaunch 扫描插件目录并启动所有插件，同时加载清单。
func (pm *PluginManager) DiscoverAndLaunch() {
	entries, err := os.ReadDir(pm.pluginsDir)
	if err != nil {
		logWarn("pm", "读取插件目录失败: %v", err)
		return
	}
	for _, e := range entries {
		if e.IsDir() {
			pm.launch(e.Name())
		}
	}
}

func (pm *PluginManager) loadManifest(id string) *PluginManifest {
	absDir := filepath.Join(pm.pluginsDir, id)
	m, err := LoadManifest(absDir)
	if err != nil {
		logWarn("pm", "插件 %s 清单解析失败(忽略): %v", id, err)
		return nil
	}
	if m != nil {
		if err := m.Validate(id); err != nil {
			logWarn("pm", "插件 %s 清单校验失败: %v", id, err)
			return nil
		}
		logInfo("pm", "插件 %s 清单: id=%s commands=%v events=%v", id, m.ID, m.Commands, m.EventTypes)
	}
	pm.mu.Lock()
	pm.manifests[id] = m
	pm.mu.Unlock()
	return m
}

// cmdPermFor 返回某插件声明的命令权限表（前缀→等级），供内核下发给 SDK 做命令级鉴权。
func (pm *PluginManager) cmdPermFor(id string) map[string]string {
	pm.mu.Lock()
	m := pm.manifests[id]
	pm.mu.Unlock()
	out := map[string]string{}
	if m == nil {
		return out
	}
	for _, c := range m.Commands {
		out[c.Name] = c.Perm
	}
	return out
}

func (pm *PluginManager) launch(id string) {
	pm.mu.Lock()
	if pm.stopped {
		pm.mu.Unlock()
		return
	}
	pm.mu.Unlock()
	pm.loadManifest(id)
	absPluginDir, _ := filepath.Abs(filepath.Join(pm.pluginsDir, id))
	entry := filepath.Join(pm.sdkDir, "entry.py")
	cmd := exec.Command(pm.python, entry,
		"--plugin-dir", absPluginDir,
		"--iipc-addr", pm.addr,
		"--iipc-auth", pm.auth,
		"--plugin-id", id,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "ZCBOT_PLUGIN_ID="+id,
		"PYTHONIOENCODING=utf-8", "PYTHONUTF8=1")
	if err := cmd.Start(); err != nil {
		logError("pm", "启动插件 %s 失败: %v", id, err)
		return
	}
	pm.mu.Lock()
	pm.procs[id] = cmd
	pm.mu.Unlock()
	logInfo("pm", "已拉起插件进程 %s (pid=%d)", id, cmd.Process.Pid)
	go pm.watchdog(id, cmd)
}

// watchdog 监控插件进程：干净退出（exit 0）重置崩溃计数；异常退出按指数退避重启，
// 超过上限后停止重启并告警，杜绝"崩溃炸弹"。
func (pm *PluginManager) watchdog(id string, cmd *exec.Cmd) {
	_ = cmd.Wait()
	if pm.isStopped() {
		return
	}
	pm.mu.Lock()
	exitedClean := cmd.ProcessState != nil && cmd.ProcessState.ExitCode() == 0
	if exitedClean {
		pm.restarts[id] = 0
		pm.mu.Unlock()
		logInfo("pm", "插件 %s 干净退出", id)
		return
	}
	pm.restarts[id]++
	n := pm.restarts[id]
	pm.mu.Unlock()
	if n > maxPluginRestarts {
		logError("pm", "插件 %s 连续崩溃 %d 次，已停止自动重启（请检查插件代码）", id, n)
		return
	}
	// 指数退避 1s → 2s → 4s → 8s → 16s → 封顶 30s（避免崩溃炸弹）
	backoff := time.Duration(1<<uint(min(n, 4))) * time.Second
	if backoff > 30*time.Second {
		backoff = 30 * time.Second
	}
	time.Sleep(backoff)
	if pm.isStopped() {
		return
	}
	pm.launch(id)
}

// ProcStats 返回内核自身 PID 与各插件进程 PID（含 pluginhost），供 launcher 侧 RSS
// 监控聚合"每插件一个解释器"的真实内存（此前 launcher 监控完全看不到插件进程）。
func (pm *PluginManager) ProcStats() map[string]interface{} {
	pm.mu.Lock()
	defer pm.mu.Unlock()
	plugins := make([]map[string]interface{}, 0, len(pm.procs))
	for id, c := range pm.procs {
		pid := 0
		running := false
		if c != nil && c.Process != nil {
			pid = c.Process.Pid
			running = true
		}
		plugins = append(plugins, map[string]interface{}{
			"plugin_id": id, "pid": pid, "running": running,
		})
	}
	return map[string]interface{}{"kernel_pid": os.Getpid(), "plugins": plugins}
}

// LaunchHost 启动单个 pluginhost 进程；由它负责 fork/spawn 各插件（Linux fork 省内存，Windows spawn）。
func (pm *PluginManager) LaunchHost(hostPath string) {
	absHost, err := filepath.Abs(hostPath)
	if err != nil {
		logError("pm", "pluginhost 路径解析失败: %v", err)
		return
	}
	if _, err := os.Stat(absHost); err != nil {
		logError("pm", "pluginhost 不存在: %v", err)
		return
	}
	cmd := exec.Command(pm.python, absHost,
		"--plugins-dir", pm.pluginsDir,
		"--sdk-dir", pm.sdkDir,
		"--iipc-addr", pm.addr,
		"--iipc-auth", pm.auth,
		"--python", pm.python,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "ZCBOT_PYTHON="+pm.python,
		"PYTHONIOENCODING=utf-8", "PYTHONUTF8=1")
	if err := cmd.Start(); err != nil {
		logError("pm", "启动 pluginhost 失败: %v", err)
		return
	}
	pm.mu.Lock()
	pm.procs["__pluginhost__"] = cmd
	pm.mu.Unlock()
	logInfo("pm", "已拉起 pluginhost (pid=%d)", cmd.Process.Pid)
	go pm.watchdogHost(cmd, absHost)
}

func (pm *PluginManager) watchdogHost(cmd *exec.Cmd, absHost string) {
	_ = cmd.Wait()
	logWarn("pm", "pluginhost 已退出，1s 后重启")
	time.Sleep(1 * time.Second)
	pm.LaunchHost(absHost)
}

// Shutdown 优雅关闭：置停止标志并杀掉所有子进程（含 pluginhost / 各插件）。
// 看门狗随后检测到 stopped 不会重启，避免内核退出后插件被重新拉起成孤儿。
func (pm *PluginManager) Shutdown() {
	pm.mu.Lock()
	pm.stopped = true
	for id, c := range pm.procs {
		if c != nil && c.Process != nil {
			_ = c.Process.Kill()
		}
		delete(pm.procs, id)
	}
	pm.mu.Unlock()
	logInfo("pm", "已关闭全部子进程")
}

// Watch 轮询插件目录变更，实现热重载（新增/修改/删除）。
func (pm *PluginManager) Watch(interval time.Duration) {
	go func() {
		// 播种 prev：用启动时的目录状态初始化，避免首次轮询把已存在插件误判为“新增”而双启动。
		prev := map[string]time.Time{}
		if entries, err := os.ReadDir(pm.pluginsDir); err == nil {
			for _, e := range entries {
				if e.IsDir() {
					prev[e.Name()] = pm.dirModTime(filepath.Join(pm.pluginsDir, e.Name()))
				}
			}
		}
		for {
			time.Sleep(interval)
			entries, err := os.ReadDir(pm.pluginsDir)
			if err != nil {
				continue
			}
			now := map[string]time.Time{}
			for _, e := range entries {
				if !e.IsDir() {
					continue
				}
				id := e.Name()
				mt := pm.dirModTime(filepath.Join(pm.pluginsDir, id))
				now[id] = mt
				if old, ok := prev[id]; !ok {
					// 新增插件
					logInfo("pm", "检测到新插件 %s，启动", id)
					pm.launch(id)
				} else if !old.Equal(mt) {
					// 修改：热重载（杀掉旧进程，watchdog 会自动重启新进程）
					logInfo("pm", "插件 %s 变更，热重载", id)
					pm.mu.Lock()
					if c, ok := pm.procs[id]; ok && c.Process != nil {
						_ = c.Process.Kill()
					}
					pm.mu.Unlock()
				}
			}
			// 删除的插件：停掉进程
			for id := range prev {
				if _, ok := now[id]; !ok {
					logInfo("pm", "插件 %s 已移除，停止", id)
					pm.mu.Lock()
					if c, ok := pm.procs[id]; ok && c.Process != nil {
						_ = c.Process.Kill()
						delete(pm.procs, id)
					}
					delete(pm.manifests, id)
					pm.mu.Unlock()
				}
			}
			prev = now
		}
	}()
}

// dirModTime 取插件目录整体最新修改时间（含 plugin.json 与 .py）。
func (pm *PluginManager) dirModTime(dir string) time.Time {
	latest := time.Time{}
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.ModTime().After(latest) {
			latest = info.ModTime()
		}
		return nil
	})
	return latest
}
