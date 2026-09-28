//! 配置：从环境变量 RA_CONFIG（JSON）读取，缺省与 core_plugins.yaml → onebot 段一致

use serde::Deserialize;

fn default_host() -> String {
    "0.0.0.0".to_string()
}
fn default_port() -> u16 {
    6830
}
fn default_frame() -> usize {
    16 * 1024 * 1024
}
fn default_pev() -> usize {
    4096
}
fn default_pbytes() -> usize {
    64 * 1024 * 1024
}
fn default_stats() -> u64 {
    5
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Config {
    /// OneBot 反向 WS 监听地址
    pub ws_host: String,
    /// OneBot 反向 WS 监听端口
    pub ws_port: u16,
    /// access_token（空 = 不校验）
    pub access_token: String,
    /// WS 单帧上限（字节），对齐 onebot.max_frame_size 缺省 16MB
    pub max_frame_size: usize,
    /// 分发闸门条数上限
    pub max_pending_events: usize,
    /// 分发闸门字节上限
    pub max_pending_bytes: usize,
    /// 监控 stats 上报周期（秒，0 = 关闭）
    pub stats_interval_secs: u64,
    /// IPC 事件是否剥离 raw 字段（True 时 Python 侧补空 dict 保持形态）
    pub ipc_strip_raw: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            ws_host: default_host(),
            ws_port: default_port(),
            access_token: String::new(),
            max_frame_size: default_frame(),
            max_pending_events: default_pev(),
            max_pending_bytes: default_pbytes(),
            stats_interval_secs: default_stats(),
            ipc_strip_raw: false,
        }
    }
}

impl Config {
    /// 从环境变量 RA_CONFIG 读取；缺失/解析失败回退缺省并打印告警
    pub fn from_env() -> Self {
        match std::env::var("RA_CONFIG") {
            Ok(s) => serde_json::from_str(&s).unwrap_or_else(|e| {
                eprintln!("[rust_accel] RA_CONFIG 解析失败（用缺省）: {e}");
                Config::default()
            }),
            Err(_) => Config::default(),
        }
    }
}
