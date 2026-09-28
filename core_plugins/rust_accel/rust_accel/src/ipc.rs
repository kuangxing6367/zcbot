//! IPC 服务端：127.0.0.1:0 随机端口 + JSON Lines（双向）
//! rust→python: hello / event / call_resp / conn / stats / alert / conn_list
//! python→rust: call / query / shutdown

use std::sync::Arc;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::TcpListener;
use tokio::sync::Mutex;

use crate::api::Broadcaster;
use crate::config::Config;
use crate::stats::Stats;

/// IPC 出站通道：事件 / stats / 应答统一经此写（未连接时返回 Err）。
/// 连接替换语义：accept 到新连接即 swap 写侧；旧连接断开不影响出站对象。
#[derive(Clone)]
pub struct Outbound {
    inner: Arc<OutboundInner>,
}

struct OutboundInner {
    write: Mutex<Option<BufWriter<OwnedWriteHalf>>>,
}

impl Outbound {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(OutboundInner { write: Mutex::new(None) }),
        }
    }

    pub async fn attach(&self, w: OwnedWriteHalf) {
        let mut g = self.inner.write.lock().await;
        *g = Some(BufWriter::new(w));
    }

    pub async fn is_connected(&self) -> bool {
        self.inner.write.lock().await.is_some()
    }

    /// 写一行（自动补 \n 并 flush——Python 侧 readline 依赖换行即时可见）
    pub async fn send_line(&self, line: &str) -> Result<(), String> {
        let mut g = self.inner.write.lock().await;
        let w = g.as_mut().ok_or_else(|| "IPC 未连接".to_string())?;
        w.write_all(line.as_bytes()).await.map_err(|e| e.to_string())?;
        w.write_all(b"\n").await.map_err(|e| e.to_string())?;
        w.flush().await.map_err(|e| e.to_string())
    }

    /// 批量写多行（行内已含 \n），一次 write_all + flush，减少 syscall
    pub async fn send_batch(&self, batch: &[(Vec<u8>, usize, u64)]) -> Result<(), String> {
        let mut g = self.inner.write.lock().await;
        let w = g.as_mut().ok_or_else(|| "IPC 未连接".to_string())?;
        for (line, _, _) in batch {
            w.write_all(line).await.map_err(|e| e.to_string())?;
        }
        w.flush().await.map_err(|e| e.to_string())
    }
}

/// 启动 IPC 服务端。返回 (实际端口, 出站通道)。
pub async fn serve(
    cfg: Arc<Config>,
    stats: Arc<Stats>,
    bc: Arc<Broadcaster>,
) -> Result<(u16, Outbound), String> {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("IPC 绑定失败: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let out = Outbound::new();

    // accept 循环：Python 壳主连接；断开后等待重连（重连时替换写侧）
    {
        let (listener, out, stats, bc) =
            (listener, out.clone(), stats.clone(), bc.clone());
        tokio::spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, _)) => {
                        let (r, w) = stream.into_split();
                        out.attach(w).await;
                        eprintln!("[rust_accel] IPC 已连接 (127.0.0.1:{port})");
                        let (s2, o2, b2) = (stats.clone(), out.clone(), bc.clone());
                        tokio::spawn(async move {
                            read_loop(r, s2, o2, b2).await;
                        });
                    }
                    Err(e) => {
                        eprintln!("[rust_accel] IPC accept 错误: {e}");
                        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
                    }
                }
            }
        });
    }

    // 周期监控上报
    if cfg.stats_interval_secs > 0 {
        let (s2, o2) = (stats.clone(), out.clone());
        let iv_secs = cfg.stats_interval_secs;
        tokio::spawn(async move {
            let mut timer = tokio::time::interval(std::time::Duration::from_secs(iv_secs));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                timer.tick().await;
                let connected = o2.is_connected().await;
                let line = json!({
                    "type": "stats",
                    "stats": s2.snapshot(),
                    "ipc_connected": connected,
                })
                .to_string();
                let _ = o2.send_line(&line).await;
            }
        });
    }

    Ok((port, out))
}

/// python→rust 消息读循环（每连接一个）
async fn read_loop(r: OwnedReadHalf, stats: Arc<Stats>, out: Outbound, bc: Arc<Broadcaster>) {
    let mut reader = BufReader::new(r);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        let n = match reader.read_until(b'\n', &mut buf).await {
            Ok(n) => n,
            Err(e) => {
                eprintln!("[rust_accel] IPC 读错误: {e}");
                break;
            }
        };
        if n == 0 {
            break; // EOF：Python 侧断开
        }
        let line = match std::str::from_utf8(&buf) {
            Ok(s) => s.trim(),
            Err(_) => continue,
        };
        let v: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        match v.get("type").and_then(Value::as_str) {
            Some("shutdown") => {
                eprintln!("[rust_accel] 收到 shutdown，退出");
                std::process::exit(0);
            }
            Some("query") => {
                let resp = match v.get("kind").and_then(Value::as_str) {
                    Some("connections") => json!({
                        "type": "conn_list",
                        "connections": bc.names().await
                    }),
                    _ => json!({"type": "stats", "stats": stats.snapshot()}),
                };
                let _ = out.send_line(&resp.to_string()).await;
            }
            Some("call") => {
                // 广播端：Rust 选连接 → 序列化 → 入队发送 → echo 匹配 → 回执
                let resp = bc.call(&v).await;
                let _ = out.send_line(&resp.to_string()).await;
            }
            _ => {}
        }
    }
    eprintln!("[rust_accel] IPC 连接断开，等待重连");
}