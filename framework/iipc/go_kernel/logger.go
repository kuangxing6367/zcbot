// 结构化日志：级别 + 时间戳 + 组件，输出到 stdout（+ 可选文件）。
// 替代裸 log 包，便于生产排查。
package main

import (
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

type LogLevel int

const (
	LevelDebug LogLevel = iota
	LevelInfo
	LevelWarn
	LevelError
)

type Logger struct {
	mu  sync.Mutex
	out io.Writer
	lvl LogLevel
}

var defaultLogger = &Logger{out: os.Stdout, lvl: LevelInfo}

// InitLogger 配置日志输出。path 非空则同时写文件（stdout + 文件）。
func InitLogger(path string, level LogLevel) {
	if path != "" {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
		if err == nil {
			defaultLogger = &Logger{out: io.MultiWriter(os.Stdout, f), lvl: level}
			return
		}
	}
	defaultLogger = &Logger{out: os.Stdout, lvl: level}
}

func (l *Logger) logf(level LogLevel, comp, format string, args ...interface{}) {
	if level < l.lvl {
		return
	}
	var tag string
	switch level {
	case LevelDebug:
		tag = "DEBUG"
	case LevelInfo:
		tag = "INFO"
	case LevelWarn:
		tag = "WARN"
	default:
		tag = "ERROR"
	}
	ts := time.Now().Format("2006-01-02 15:04:05")
	msg := fmt.Sprintf(format, args...)
	l.mu.Lock()
	fmt.Fprintf(l.out, "%s %s [%s] %s\n", ts, tag, comp, msg)
	l.mu.Unlock()
}

func logInfo(comp, format string, args ...interface{})  { defaultLogger.logf(LevelInfo, comp, format, args...) }
func logWarn(comp, format string, args ...interface{})  { defaultLogger.logf(LevelWarn, comp, format, args...) }
func logError(comp, format string, args ...interface{}) { defaultLogger.logf(LevelError, comp, format, args...) }
func logDebug(comp, format string, args ...interface{}) { defaultLogger.logf(LevelDebug, comp, format, args...) }
