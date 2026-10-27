// zcbot Rust 适配器（协议层）：IIPC 客户端 + OneBot 11 协议端。
//
// 拓扑：
//   Go 内核  <──IIPC(TCP+行分隔JSON)──>  本适配器  <──WebSocket──>  OneBot 实现
//
// 职责：
//   1. 连入内核并 adapter.register（self_id 路由）
//   2. 收内核 req(api.call) → 以 OneBot API 请求（带 echo 关联）发到 WS 对端
//      → 对端回响应（同 echo）→ 回 res 给内核（透传完整响应体，对齐原框架）
//   3. OneBot 事件（post_type）→ 翻译为 IIPC evt 推给内核：
//        type = post_type（message/notice/request/meta_event，别名对齐框架 contract）
//   4. WS 保活：定时 ping；断线自动重连（客户端模式）/ 等待重连（反向服务端模式）
//
// 两种 OneBot 接入形态（env/CLI 可配）：
//   --onebot-url ws://host:port/path   客户端模式：适配器主动连（正向 WS）
//   --onebot-listen [ip:]port          服务端模式：OneBot 反向连入（zcbot 原 reverse_ws 形态，默认）
//
// 编码说明：std-only（无 tokio/异步栈），帧编解码见 ws.rs；wss:// 明确报错
// （std 无 TLS，需前置反代终止），绝不静默失败。
mod ws;

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

// ── 配置 ──

struct Config {
    iipc_addr: String,
    self_id: String,
    /// 客户端模式 URL（Some=主动连）。
    onebot_url: Option<String>,
    /// 服务端模式监听地址（None=不监听）。
    onebot_listen: Option<String>,
    token: Option<String>,
}

fn parse_args() -> Config {
    let mut c = Config {
        iipc_addr: std::env::var("ZCBOT_IIPC_ADDR").unwrap_or_else(|_| "127.0.0.1:18730".into()),
        self_id: "rustbot".into(),
        onebot_url: std::env::var("ZCBOT_ONEBOT_URL").ok().filter(|s| !s.is_empty()),
        onebot_listen: std::env::var("ZCBOT_ONEBOT_LISTEN").ok().filter(|s| !s.is_empty()),
        token: std::env::var("ZCBOT_ONEBOT_TOKEN").ok().filter(|s| !s.is_empty()),
    };
    // 两者都未指定时，默认反向服务端模式（对齐 zcbot 原 reverse_ws 常用形态）
    if c.onebot_url.is_none() && c.onebot_listen.is_none() {
        c.onebot_listen = Some("127.0.0.1:6701".into());
    }
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--addr" | "--iipc-addr" => {
                if i + 1 < args.len() {
                    c.iipc_addr = args[i + 1].clone();
                    i += 1;
                }
            }
            "--self-id" => {
                if i + 1 < args.len() {
                    c.self_id = args[i + 1].clone();
                    i += 1;
                }
            }
            "--onebot-url" => {
                if i + 1 < args.len() {
                    c.onebot_url = Some(args[i + 1].clone());
                    c.onebot_listen = None; // 显式指定客户端模式
                    i += 1;
                }
            }
            "--onebot-listen" => {
                if i + 1 < args.len() {
                    c.onebot_listen = Some(args[i + 1].clone());
                    c.onebot_url = None; // 显式指定服务端模式
                    i += 1;
                }
            }
            "--token" | "--access-token" => {
                if i + 1 < args.len() {
                    c.token = Some(args[i + 1].clone());
                    i += 1;
                }
            }
            _ => {}
        }
        i += 1;
    }
    c
}

// ── 共享状态 ──

/// 当前活跃的 OneBot WS 连接（多连接时为首个/最近一个；写侧互斥）。
type SharedConn = Arc<Mutex<Option<Arc<Mutex<ws::WsConn>>>>>;

/// echo → 内核 req id 的关联表（api.call 请求-响应对齐）。
type PendingMap = Arc<Mutex<HashMap<String, i64>>>;

static ECHO_SEQ: AtomicI64 = AtomicI64::new(1);

fn next_echo() -> String {
    format!("iipc-{}", ECHO_SEQ.fetch_add(1, Ordering::Relaxed))
}

fn main() {
    let cfg = parse_args();
    eprintln!(
        "[rust-adapter] 内核={} self_id={} 模式={}",
        cfg.iipc_addr,
        cfg.self_id,
        if cfg.onebot_url.is_some() { "客户端(正向WS)" } else { "服务端(反向WS)" }
    );

    // 1) 连内核
    let kstream = match TcpStream::connect(&cfg.iipc_addr) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[rust-adapter] 连接内核失败: {}（内核是否已启动？）", e);
            std::process::exit(1);
        }
    };
    kstream.set_nodelay(true).ok();
    let kwriter = Arc::new(Mutex::new(kstream.try_clone().expect("clone kstream")));
    let reg = json!({"t":"req","id":1,"method":"adapter.register","params":{"self_id": cfg.self_id}});
    ksend(&kwriter, &reg);

    let shared: SharedConn = Arc::new(Mutex::new(None));
    let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));

    // 2) 内核读线程：收 api.call → 转发 OneBot
    {
        let kw = Arc::clone(&kwriter);
        let shared = Arc::clone(&shared);
        let pending = Arc::clone(&pending);
        let self_id = cfg.self_id.clone();
        thread::spawn(move || {
            let reader = BufReader::new(kstream);
            for line in reader.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => break,
                };
                if line.trim().is_empty() {
                    continue;
                }
                let v: Value = match serde_json::from_str(&line) {
                    Ok(v) => v,
                    Err(e) => {
                        eprintln!("[rust-adapter] 内核帧解析失败: {} <- {}", e, line);
                        continue;
                    }
                };
                if v.get("t").and_then(|x| x.as_str()) != Some("req") {
                    continue;
                }
                let method = v.get("method").and_then(|x| x.as_str()).unwrap_or("");
                let id = v.get("id").and_then(|x| x.as_i64()).unwrap_or(0);
                if method != "api.call" {
                    let res = json!({"t":"res","id":id,"error": format!("unknown method: {}", method)});
                    ksend(&kw, &res);
                    continue;
                }
                let params = v.get("params").cloned().unwrap_or(json!({}));
                let action = params.get("action").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let aparams = params.get("params").cloned().unwrap_or(json!({}));
                let req_self_id = params
                    .get("self_id")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();

                // 多 bot 路由：self_id 不匹配当前连接时明确报错（当前版本单连接）
                if !req_self_id.is_empty() && req_self_id != self_id {
                    let res = json!({"t":"res","id":id,
                        "error": format!("self_id 不匹配: 适配器实例={} 请求={}", self_id, req_self_id)});
                    ksend(&kw, &res);
                    continue;
                }

                let conn = { shared.lock().unwrap().clone() };
                let conn = match conn {
                    Some(c) => c,
                    None => {
                        let res = json!({"t":"res","id":id,
                            "error": "OneBot 未连接（WS 未建立）"});
                        ksend(&kw, &res);
                        continue;
                    }
                };
                let echo = next_echo();
                let payload = json!({"action": action, "params": aparams, "echo": echo});
                // 必须先登记再发送：对端可能在本线程写完整帧之前就回响应，
                // 读线程若查不到 echo 就会丢弃该响应 → 内核干等 15s 超时
                // （突发高并发下间歇性 15s 卡顿的根因）。
                pending.lock().unwrap().insert(echo.clone(), id);
                let ok = {
                    let g = conn.lock().unwrap();
                    g.send_text(&payload.to_string()).is_ok()
                };
                if !ok {
                    // 发送失败：撤掉登记，避免 pending 泄漏
                    pending.lock().unwrap().remove(&echo);
                    let res = json!({"t":"res","id":id,"error": "OneBot WS 发送失败"});
                    ksend(&kw, &res);
                    continue;
                }
                eprintln!("[rust-adapter] => api.call {} {}", action, aparams);
            }
            eprintln!("[rust-adapter] 与内核的连接已断开");
            std::process::exit(0);
        });
    }

    // 3) OneBot 连接管理（两种模式）
    if let Some(url) = cfg.onebot_url.clone() {
        run_client_mode(url, cfg.token.clone(), Arc::clone(&shared),
                        Arc::clone(&pending), Arc::clone(&kwriter), cfg.self_id.clone());
    }
    if let Some(listen) = cfg.onebot_listen.clone() {
        run_server_mode(listen, cfg.token.clone(), Arc::clone(&shared), Arc::clone(&pending),
                        Arc::clone(&kwriter), cfg.self_id.clone());
    }

    // 主线程保活
    loop {
        thread::sleep(Duration::from_secs(3600));
    }
}

/// 客户端模式：主动连 OneBot，断线重连。
fn run_client_mode(url: String, token: Option<String>, shared: SharedConn,
                   pending: PendingMap, kw: Arc<Mutex<TcpStream>>, self_id: String) {
    thread::spawn(move || {
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            match ws::ws_connect(&url, token.as_deref()) {
                Ok(conn) => {
                    eprintln!("[rust-adapter] OneBot WS 已连接 ({})", url);
                    attempt = 0;
                    let conn = Arc::new(Mutex::new(conn));
                    *shared.lock().unwrap() = Some(Arc::clone(&conn));
                    serve_onebot_conn(conn, Arc::clone(&pending), Arc::clone(&kw), self_id.clone(), true);
                    *shared.lock().unwrap() = None;
                    eprintln!("[rust-adapter] OneBot WS 断开，2s 后重连");
                }
                Err(e) => {
                    eprintln!(
                        "[rust-adapter] OneBot WS 连接失败(第{}次): {}（2s 后重试）",
                        attempt, e
                    );
                }
            }
            thread::sleep(Duration::from_secs(2));
        }
    });
}

/// 服务端模式：监听反向 WS 连接（OneBot 实现连入）。
/// `token` 透传至 `ws_accept` 做 OneBot 11 access_token 鉴权（空=关闭）。
fn run_server_mode(listen: String, token: Option<String>, shared: SharedConn, pending: PendingMap,
                   kw: Arc<Mutex<TcpStream>>, self_id: String) {
    thread::spawn(move || {
        let listener = match TcpListener::bind(&listen) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[rust-adapter] 监听 {} 失败: {}（反向 WS 模式不可用）", listen, e);
                return;
            }
        };
        eprintln!("[rust-adapter] 反向 WS 监听 {} （OneBot 实现请配 reverse_ws 连入）", listen);
        for stream in listener.incoming() {
            match stream {
                Ok(s) => {
                    let shared = Arc::clone(&shared);
                    let pending = Arc::clone(&pending);
                    let kw = Arc::clone(&kw);
                    let self_id = self_id.clone();
                    let token = token.clone();
                    thread::spawn(move || match ws::ws_accept(s, token.as_deref()) {
                        Ok(conn) => {
                            eprintln!("[rust-adapter] OneBot 反向连接已建立");
                            let conn = Arc::new(Mutex::new(conn));
                            *shared.lock().unwrap() = Some(Arc::clone(&conn));
                            serve_onebot_conn(conn, pending, kw, self_id, false);
                            *shared.lock().unwrap() = None;
                        }
                        Err(e) => eprintln!("[rust-adapter] 反向 WS 握手失败: {}", e),
                    });
                }
                Err(e) => eprintln!("[rust-adapter] accept 失败: {}", e),
            }
        }
    });
}

/// 服务一条已建立的 OneBot WS 连接：事件→内核 / API 响应(echo)→内核。
fn serve_onebot_conn(conn: Arc<Mutex<ws::WsConn>>, pending: PendingMap,
                     kw: Arc<Mutex<TcpStream>>, self_id: String, is_client_mode: bool) {
    // 保活：定时 ping（客户端与反向连接都保）
    {
        let conn = Arc::clone(&conn);
        thread::spawn(move || loop {
            thread::sleep(Duration::from_secs(30));
            let g = conn.lock().unwrap();
            if g.ping().is_err() {
                break;
            }
        });
    }
    let _ = is_client_mode;
    // 读句柄独占：绝不在持连接锁的情况下阻塞读，否则内核读线程要发
    // api.call 时抢不到 writer 锁，整条回传链死锁（2026-10-14 实测教训）。
    let mut reader = match conn.lock().unwrap().take_reader() {
        Some(r) => r,
        None => {
            eprintln!("[rust-adapter] reader 已被取走（重复 serve？）");
            return;
        }
    };
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| loop {
        let msg = ws::WsConn::read_message(&mut reader);
        let msg = match msg {
            Ok(m) => m,
            Err(e) => {
                eprintln!("[rust-adapter] OneBot WS 读取结束: {}", e);
                break;
            }
        };
        if msg.opcode != ws::OP_TEXT && msg.opcode != ws::OP_BINARY {
            continue;
        }
        let text = String::from_utf8_lossy(&msg.payload);
        // 容错：一个文本帧里多条换行分隔 JSON（部分实现如此）
        for line in text.split('\n') {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let v: Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("[rust-adapter] OneBot 帧 JSON 解析失败: {} <- {}", e, line);
                    continue;
                }
            };
            // API 响应（带 echo）→ 回给内核对应 req
            if let Some(echo) = v.get("echo").and_then(|x| x.as_str()) {
                let hit = pending.lock().unwrap().remove(echo);
                if let Some(kid) = hit {
                    eprintln!("[rust-adapter] <= api 响应 echo={} {}", echo, v);
                    let res = json!({"t":"res","id":kid,"result": v});
                    ksend(&kw, &res);
                }
                continue;
            }
            // 事件 → 推给内核（type 用 post_type，别名对齐框架 contract）
            let post_type = v.get("post_type").and_then(|x| x.as_str()).unwrap_or("");
            if post_type.is_empty() {
                continue;
            }
            let etype = match post_type {
                "message" | "message_sent" => "message",
                "notice" => "notice",
                "request" => "request",
                "meta_event" | "meta" => "meta_event",
                other => other,
            };
            let mut data = v.clone();
            if let Some(obj) = data.as_object_mut() {
                obj.entry("self_id").or_insert(json!(self_id));
                obj.entry("bot_name").or_insert(json!(self_id));
                obj.entry("adapter").or_insert(json!("onebot"));
            }
            let ev = json!({
                "t":"evt","channel":"event",
                "payload":{"type": etype, "data": data}
            });
            ksend(&kw, &ev);
        }
    }));
    if result.is_err() {
        eprintln!("[rust-adapter] OneBot 读循环 PANIC: {:?}",
                  result.err().map(|e| e.downcast_ref::<String>().cloned()
                      .or_else(|| e.downcast_ref::<&str>().map(|s| s.to_string()))
                      .unwrap_or_else(|| "unknown".into())));
    }
}

fn ksend(w: &Arc<Mutex<TcpStream>>, v: &Value) {
    let mut s = v.to_string();
    s.push('\n');
    match w.lock() {
        Ok(mut g) => {
            if let Err(e) = g.write_all(s.as_bytes()) {
                eprintln!("[rust-adapter] 发给内核失败: {}", e);
            }
            let _ = g.flush();
        }
        Err(e) => eprintln!("[rust-adapter] 内核写锁中毒: {}", e),
    }
}
