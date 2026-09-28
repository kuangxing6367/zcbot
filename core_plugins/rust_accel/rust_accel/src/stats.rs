//! 运行监控计数：收/转/连状态与延迟，经 IPC stats 上报给 Python 壳

use serde_json::{json, Value};
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::time::Instant;

pub struct Stats {
    pub started: Instant,
    /// WS 收帧总数
    pub frames_rx: AtomicU64,
    /// JSON / UTF-8 解析失败
    pub parse_err: AtomicU64,
    /// 归一化丢弃（无 post_type）
    pub norm_drop: AtomicU64,
    /// 闸门丢弃（条/字节超限）
    pub gate_dropped: AtomicU64,
    /// 经 IPC 成功转发事件数
    pub forwarded: AtomicU64,
    /// 转发字节数
    pub bytes_fwd: AtomicU64,
    /// IPC 写出失败/未连接
    pub ipc_fail: AtomicU64,
    /// echo 响应帧（广播端匹配用）
    pub echo_rx: AtomicU64,
    /// WS 握手拒绝
    pub ws_reject: AtomicU64,
    /// 当前连接数
    pub conn_now: AtomicI64,
    /// 连接峰值
    pub conn_peak: AtomicU64,
    /// 转发总耗时（微秒，求均值）
    pub fwd_total_us: AtomicU64,
    /// 转发最大耗时（微秒）
    pub fwd_max_us: AtomicU64,
    /// 广播端：call 总数
    pub api_calls: AtomicU64,
    /// 广播端：成功应答
    pub api_ok: AtomicU64,
    /// 广播端：发送失败/内部错误
    pub api_fail: AtomicU64,
    /// 广播端：超时
    pub api_timeout: AtomicU64,
    /// 广播端：无可用连接
    pub api_noconn: AtomicU64,
    /// 广播端：发送队列满
    pub api_qfull: AtomicU64,
    /// 广播端：往返总耗时（毫秒，求均值）与最大
    pub api_rt_total_ms: AtomicU64,
    pub api_rt_max_ms: AtomicU64,
}

impl Stats {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
            frames_rx: AtomicU64::new(0),
            parse_err: AtomicU64::new(0),
            norm_drop: AtomicU64::new(0),
            gate_dropped: AtomicU64::new(0),
            forwarded: AtomicU64::new(0),
            bytes_fwd: AtomicU64::new(0),
            ipc_fail: AtomicU64::new(0),
            echo_rx: AtomicU64::new(0),
            ws_reject: AtomicU64::new(0),
            conn_now: AtomicI64::new(0),
            conn_peak: AtomicU64::new(0),
            fwd_total_us: AtomicU64::new(0),
            fwd_max_us: AtomicU64::new(0),
            api_calls: AtomicU64::new(0),
            api_ok: AtomicU64::new(0),
            api_fail: AtomicU64::new(0),
            api_timeout: AtomicU64::new(0),
            api_noconn: AtomicU64::new(0),
            api_qfull: AtomicU64::new(0),
            api_rt_total_ms: AtomicU64::new(0),
            api_rt_max_ms: AtomicU64::new(0),
        }
    }

    pub fn add_fwd_latency(&self, us: u64) {
        self.fwd_total_us.fetch_add(us, Ordering::Relaxed);
        self.fwd_max_us.fetch_max(us, Ordering::Relaxed);
    }

    pub fn add_api_rt(&self, ms: u64) {
        self.api_rt_total_ms.fetch_add(ms, Ordering::Relaxed);
        self.api_rt_max_ms.fetch_max(ms, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> Value {
        let fwd = self.forwarded.load(Ordering::Relaxed);
        let avg_us = if fwd > 0 {
            self.fwd_total_us.load(Ordering::Relaxed) / fwd
        } else {
            0
        };
        json!({
            "uptime_secs": self.started.elapsed().as_secs_f64(),
            "rx": {
                "frames": self.frames_rx.load(Ordering::Relaxed),
                "parse_err": self.parse_err.load(Ordering::Relaxed),
                "echo": self.echo_rx.load(Ordering::Relaxed),
            },
            "drop": {
                "gate": self.gate_dropped.load(Ordering::Relaxed),
                "norm": self.norm_drop.load(Ordering::Relaxed),
            },
            "fwd": {
                "events": fwd,
                "bytes": self.bytes_fwd.load(Ordering::Relaxed),
                "avg_us": avg_us,
                "max_us": self.fwd_max_us.load(Ordering::Relaxed),
                "ipc_fail": self.ipc_fail.load(Ordering::Relaxed),
            },
            "conn": {
                "now": self.conn_now.load(Ordering::Relaxed),
                "peak": self.conn_peak.load(Ordering::Relaxed),
                "reject": self.ws_reject.load(Ordering::Relaxed),
            },
            "api": {
                "calls": self.api_calls.load(Ordering::Relaxed),
                "ok": self.api_ok.load(Ordering::Relaxed),
                "fail": self.api_fail.load(Ordering::Relaxed),
                "timeout": self.api_timeout.load(Ordering::Relaxed),
                "noconn": self.api_noconn.load(Ordering::Relaxed),
                "qfull": self.api_qfull.load(Ordering::Relaxed),
                "rt_avg_ms": if self.api_calls.load(Ordering::Relaxed) > 0 {
                    self.api_rt_total_ms.load(Ordering::Relaxed) / self.api_calls.load(Ordering::Relaxed)
                } else {
                    0
                },
                "rt_max_ms": self.api_rt_max_ms.load(Ordering::Relaxed),
            },
        })
    }
}
