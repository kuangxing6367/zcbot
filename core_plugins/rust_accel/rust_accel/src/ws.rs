//! OneBot 11 反向 WebSocket 服务端（Rust 接收端）
//! 收帧 → JSON 解析 → echo 分类 → 闸门（条数+字节）→ 归一化（含 _est_size 预算）
//! → 每 bot 有序 mpsc → forward task 批量写出 IPC。
//! 语义对齐 core_plugins/onebot_adapter/main.py（含闸门阈值与丢弃计数口径）。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex};
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::accept_hdr_async_with_config;

use crate::api::{BotSlot, Broadcaster, Conns};
use crate::config::Config;
use crate::ipc::Outbound;
use crate::normalize;
use crate::stats::Stats;

/// 转发通道条目：完整行（含 \n）+ 闸门字节 + 入队时刻（微秒，延迟统计用）
type FwdItem = (Vec<u8>, usize, u64);

/// 分发层闸门：条数 + 字节双限（对齐 Python _dispatch_pending / _dispatch_pending_bytes）
pub struct Gate {
    inner: Mutex<GateInner>,
}

struct GateInner {
    pending: usize,
    bytes: usize,
    max_ev: usize,
    max_bytes: usize,
}

impl Gate {
    fn new(max_ev: usize, max_bytes: usize) -> Self {
        Self {
            inner: Mutex::new(GateInner { pending: 0, bytes: 0, max_ev, max_bytes }),
        }
    }

    async fn accept(&self, sz: usize) -> bool {
        let mut g = self.inner.lock().await;
        if g.pending >= g.max_ev || g.bytes + sz > g.max_bytes {
            return false;
        }
        g.pending += 1;
        g.bytes += sz;
        true
    }

    async fn release(&self, sz: usize) {
        let mut g = self.inner.lock().await;
        g.pending = g.pending.saturating_sub(1);
        g.bytes = g.bytes.saturating_sub(sz);
    }
}

/// 启动 WS 服务端：bind 后 spawn accept 循环，返回实际端口
pub async fn spawn(
    cfg: Arc<Config>,
    stats: Arc<Stats>,
    out: Outbound,
    conns: Conns,
    bc: Arc<Broadcaster>,
    bot_counter: Arc<AtomicU64>,
) -> Result<u16, String> {
    let bind: std::net::SocketAddr = format!("{}:{}", cfg.ws_host, cfg.ws_port)
        .parse()
        .map_err(|e| format!("WS 地址无效 {}:{} -> {e}", cfg.ws_host, cfg.ws_port))?;
    let listener = TcpListener::bind(bind)
        .await
        .map_err(|e| format!("WS 绑定失败 {bind}: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    tokio::spawn(accept_loop(
        listener,
        cfg,
        stats,
        out,
        conns,
        bc,
        bot_counter,
    ));
    Ok(port)
}

async fn accept_loop(
    listener: TcpListener,
    cfg: Arc<Config>,
    stats: Arc<Stats>,
    out: Outbound,
    conns: Conns,
    bc: Arc<Broadcaster>,
    bot_counter: Arc<AtomicU64>,
) {
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let (c, s, o, cs, b, bct) = (
                    cfg.clone(),
                    stats.clone(),
                    out.clone(),
                    conns.clone(),
                    bc.clone(),
                    bot_counter.clone(),
                );
                tokio::spawn(async move {
                    handle_conn(stream, c, s, o, cs, b, bct).await;
                });
            }
            Err(e) => {
                eprintln!("[rust_accel] WS accept 错误: {e}");
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            }
        }
    }
}

async fn handle_conn(
    stream: TcpStream,
    cfg: Arc<Config>,
    stats: Arc<Stats>,
    out: Outbound,
    conns: Conns,
    bc: Arc<Broadcaster>,
    bot_counter: Arc<AtomicU64>,
) {
    // 握手：access_token 校验（Authorization: Bearer / query access_token）+ X-Self-ID
    let mut self_id: Option<String> = None;
    let ws_cfg = WebSocketConfig {
        max_message_size: Some(cfg.max_frame_size),
        max_frame_size: Some(cfg.max_frame_size),
        ..Default::default()
    };
    let ws = match accept_hdr_async_with_config(
        stream,
        |req: &Request, resp: Response| {
            if !cfg.access_token.is_empty() && extract_token(req) != cfg.access_token {
                // callback 拒绝语义：返回 HTTP 级 Error Response（客户端直接收到 403）
                let denied = Response::builder()
                    .status(403)
                    .body(Some("access_token error".to_string()))
                    .unwrap();
                return Err(denied);
            }
            self_id = req
                .headers()
                .get("x-self-id")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string());
            Ok(resp)
        },
        Some(ws_cfg),
    )
    .await
    {
        Ok(w) => w,
        Err(_) => {
            stats.ws_reject.fetch_add(1, Ordering::Relaxed);
            return;
        }
    };

    let bot_name = match self_id {
        Some(n) if !n.is_empty() => n,
        _ => format!("bot_{}", bot_counter.fetch_add(1, Ordering::Relaxed)),
    };

    // 注册 + 连接事件；每 bot 建写侧发送队列（broadcast 用）
    let (bot_tx, mut bot_rx) = mpsc::channel::<Message>(crate::api::SEND_QUEUE_CAP);
    {
        let mut g = conns.write().await;
        g.insert(bot_name.clone(), BotSlot { tx: bot_tx });
    }
    let now = stats.conn_now.fetch_add(1, Ordering::Relaxed) + 1;
    stats.conn_peak.fetch_max(now as u64, Ordering::Relaxed);
    let _ = out
        .send_line(&json!({"type": "conn", "state": "up", "name": bot_name}).to_string())
        .await;

    // 每 bot 有序转发通道
    let gate = Arc::new(Gate::new(cfg.max_pending_events, cfg.max_pending_bytes));
    let (tx, rx) = mpsc::channel::<FwdItem>(1024);
    let fw = tokio::spawn(forward_loop(bot_name.clone(), rx, out.clone(), stats.clone(), gate.clone()));

    let (mut sink, mut stream) = ws.split();
    // 写侧 task：串行 drain 广播队列 → WS Sink；读循环不受影响
    let write_task = tokio::spawn(async move {
        while let Some(msg) = bot_rx.recv().await {
            if sink.send(msg).await.is_err() {
                break;
            }
        }
    });

    loop {
        match stream.next().await {
            Some(Ok(msg)) => {
                stats.frames_rx.fetch_add(1, Ordering::Relaxed);
                let bytes: Vec<u8> = match msg {
                    Message::Text(t) => t.as_bytes().to_vec(),
                    Message::Binary(b) => b.to_vec(),
                    Message::Ping(_) | Message::Pong(_) => continue,
                    Message::Close(_) => break,
                    Message::Frame(_) => continue,
                };
                let text = match String::from_utf8(bytes) {
                    Ok(s) => s,
                    Err(_) => {
                        stats.parse_err.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                };
                let raw: Value = match serde_json::from_str(&text) {
                    Ok(v) => v,
                    Err(_) => {
                        stats.parse_err.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                };
                if let Some(echo) = raw.get("echo").and_then(Value::as_str).map(String::from) {
                    // 广播端应答匹配：命中则投递给对应 call 等待者；否则计数忽略
                    stats.echo_rx.fetch_add(1, Ordering::Relaxed);
                    bc.on_echo(&echo, raw).await;
                    continue;
                }
                if raw.get("post_type").is_none() {
                    continue;
                }
                // 闸门（口径同 Python：原始 payload 估算）
                let sz = normalize::calc_size(&raw);
                if !gate.accept(sz).await {
                    stats.gate_dropped.fetch_add(1, Ordering::Relaxed);
                    continue;
                }
                let Some(ev) = normalize::normalize_event(&raw, &bot_name) else {
                    stats.norm_drop.fetch_add(1, Ordering::Relaxed);
                    gate.release(sz).await;
                    continue;
                };
                let mut o = Map::new();
                o.insert("type".to_string(), Value::String("event".to_string()));
                o.insert("event".to_string(), Value::Object(ev));
                let line = match serde_json::to_string(&Value::Object(o)) {
                    Ok(s) => s + "\n",
                    Err(_) => {
                        gate.release(sz).await;
                        continue;
                    }
                };
                let ts = Instant::now().elapsed().as_micros() as u64;
                if tx.send((line.into_bytes(), sz, ts)).await.is_err() {
                    stats.ipc_fail.fetch_add(1, Ordering::Relaxed);
                    gate.release(sz).await;
                }
            }
            Some(Err(e)) => {
                if !matches!(e, WsError::ConnectionClosed | WsError::Protocol(_)) {
                    eprintln!("[{bot_name}] WS 连接错误: {e}");
                }
                break;
            }
            None => break,
        }
    }

    // 断开清理 + 连接事件
    conns.write().await.remove(&bot_name);
    stats.conn_now.fetch_sub(1, Ordering::Relaxed);
    let _ = out
        .send_line(&json!({"type": "conn", "state": "down", "name": bot_name}).to_string())
        .await;
    fw.abort();
    write_task.abort();
}

/// 每 bot 有序转发：批量 drain 队列后一次写出（事件顺序即接收顺序）
async fn forward_loop(
    _bot_name: String,
    mut rx: mpsc::Receiver<FwdItem>,
    out: Outbound,
    stats: Arc<Stats>,
    gate: Arc<Gate>,
) {
    while let Some((line, sz, ts0)) = rx.recv().await {
        let mut batch = vec![(line, sz, ts0)];
        while let Ok(more) = rx.try_recv() {
            batch.push(more);
        }
        let total_bytes: usize = batch.iter().map(|(l, _, _)| l.len()).sum();
        let n = batch.len() as u64;
        if out.send_batch(&batch).await.is_ok() {
            stats.forwarded.fetch_add(n, Ordering::Relaxed);
            stats.bytes_fwd.fetch_add(total_bytes as u64, Ordering::Relaxed);
            let now = Instant::now().elapsed().as_micros() as u64;
            for (_, _, ts) in &batch {
                stats.add_fwd_latency(now.saturating_sub(*ts));
            }
        } else {
            stats.ipc_fail.fetch_add(n, Ordering::Relaxed);
        }
        for (_, s, _) in batch {
            gate.release(s).await;
        }
    }
}

/// 从 WS 握手请求提取 access_token：Authorization: Bearer xx / query access_token=xx
fn extract_token(req: &Request) -> String {
    if let Some(auth) = req
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
    {
        if let Some(bearer) = auth
            .strip_prefix("Bearer ")
            .or_else(|| auth.strip_prefix("bearer "))
        {
            return bearer.to_string();
        }
    }
    if let Some(q) = req.uri().query() {
        for pair in q.split('&') {
            if let Some((k, v)) = pair.split_once('=') {
                if k == "access_token" {
                    // 与 Python parse_qs 等价；token 一般无 URL 编码字符
                    return v.to_string();
                }
            }
        }
    }
    String::new()
}