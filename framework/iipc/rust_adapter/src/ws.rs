// WebSocket（RFC 6455）最小实现：std-only，无外部依赖。
// 同时提供：
//   - 客户端（正向 WS）：适配器主动连 OneBot 实现（ws://host:port/path）
//   - 服务端（反向 WS）：OneBot 实现连入适配器（zcbot 原架构的 reverse_ws 模式）
// 帧编解码对两种角色通用：客户端发出的帧必须掩码，服务端发出的帧不掩码；
// 读侧两种都认（按 MASK 位判断）。
//
// 并发模型（关键）：读句柄（reader）由读线程**独占持有**，写句柄（writer）
// 经 Mutex 短锁共享。绝不允许"持连接锁阻塞读"——否则内核读线程要发
// api.call 时抢不到锁，整条回传链死锁（2026-10-14 实测教训）。
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub const OP_TEXT: u8 = 0x1;
pub const OP_BINARY: u8 = 0x2;
pub const OP_CLOSE: u8 = 0x8;
pub const OP_PING: u8 = 0x9;
pub const OP_PONG: u8 = 0xA;

/// 一条完整的 WS 消息（已处理分片与掩码）。
pub struct WsMessage {
    pub opcode: u8,
    pub payload: Vec<u8>,
}

/// 已握手的 WS 连接（任一角色）。
///
/// - `writer`：写句柄（TcpStream clone），所有写操作经此 Mutex **短锁**；
/// - `reader`：读句柄，读线程启动时 `take_reader()` 取走独占，
///   此后本结构只承载写职责。
pub struct WsConn {
    pub writer: Arc<Mutex<TcpStream>>,
    reader: Option<TcpStream>,
    /// 本端是否为客户端（决定发出帧是否掩码）。
    pub is_client: bool,
}

impl WsConn {
    pub fn new(stream: TcpStream, is_client: bool) -> std::io::Result<WsConn> {
        let writer = stream.try_clone()?;
        Ok(WsConn {
            writer: Arc::new(Mutex::new(writer)),
            reader: Some(stream),
            is_client,
        })
    }

    /// 读线程取走读句柄（独占）。重复取返回 None。
    pub fn take_reader(&mut self) -> Option<TcpStream> {
        self.reader.take()
    }

    /// 读一条完整消息（自动拼接分片；控制帧穿插时直接处理回 Pong）。
    /// 在**读句柄**上调用，不持任何共享锁。
    pub fn read_message(reader: &mut TcpStream) -> std::io::Result<WsMessage> {
        let mut msg: Option<(u8, Vec<u8>)> = None;
        loop {
            let (fin, opcode, payload) = Self::read_frame(reader)?;
            match opcode {
                OP_PING => {
                    // 协议要求立即回 Pong（带回同样 payload）
                    let _ = Self::write_frame_static(reader, OP_PONG, &payload, false);
                }
                OP_PONG => { /* 心跳应答，忽略 */ }
                OP_CLOSE => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::ConnectionAborted,
                        "对端关闭 WebSocket",
                    ));
                }
                OP_TEXT | OP_BINARY => {
                    if let Some((_, buf)) = msg.as_mut() {
                        buf.extend_from_slice(&payload);
                    } else {
                        msg = Some((opcode, payload));
                    }
                }
                0x0 => {
                    // 续传分片
                    if let Some((_, buf)) = msg.as_mut() {
                        buf.extend_from_slice(&payload);
                    }
                }
                _ => {}
            }
            if fin {
                let (op, buf) = msg.take().unwrap_or((OP_TEXT, Vec::new()));
                return Ok(WsMessage { opcode: op, payload: buf });
            }
        }
    }

    /// 读一个帧头 + 载荷（按 MASK 位解掩码）。
    fn read_frame(reader: &mut TcpStream) -> std::io::Result<(bool, u8, Vec<u8>)> {
        let mut hdr = [0u8; 2];
        reader.read_exact(&mut hdr)?;
        let fin = hdr[0] & 0x80 != 0;
        let opcode = hdr[0] & 0x0F;
        let masked = hdr[1] & 0x80 != 0;
        let len7 = (hdr[1] & 0x7F) as u64;
        let len: u64 = match len7 {
            126 => {
                let mut b = [0u8; 2];
                reader.read_exact(&mut b)?;
                u16::from_be_bytes(b) as u64
            }
            127 => {
                let mut b = [0u8; 8];
                reader.read_exact(&mut b)?;
                u64::from_be_bytes(b)
            }
            n => n,
        };
        let mut mask_key = [0u8; 4];
        if masked {
            reader.read_exact(&mut mask_key)?;
        }
        let mut payload = vec![0u8; len as usize];
        if len > 0 {
            reader.read_exact(&mut payload)?;
        }
        if masked {
            for (i, b) in payload.iter_mut().enumerate() {
                *b ^= mask_key[i % 4];
            }
        }
        Ok((fin, opcode, payload))
    }

    /// 帧编码（不含锁）；is_client 决定是否掩码。
    fn encode_frame(opcode: u8, payload: &[u8], is_client: bool) -> Vec<u8> {
        let mut hdr: Vec<u8> = Vec::with_capacity(14);
        hdr.push(0x80 | opcode); // FIN + opcode
        let mask_bit = if is_client { 0x80u8 } else { 0u8 };
        let n = payload.len() as u64;
        if n < 126 {
            hdr.push(mask_bit | n as u8);
        } else if n <= 0xFFFF {
            hdr.push(mask_bit | 126);
            hdr.extend_from_slice(&(n as u16).to_be_bytes());
        } else {
            hdr.push(mask_bit | 127);
            hdr.extend_from_slice(&n.to_be_bytes());
        }
        let mut out = hdr;
        if is_client {
            // 客户端帧必须掩码：key 由时间+计数器混合生成（无需 rand 依赖）
            let key = gen_mask_key();
            out.extend_from_slice(&key);
            let mut masked = payload.to_vec();
            for (i, b) in masked.iter_mut().enumerate() {
                *b ^= key[i % 4];
            }
            out.extend_from_slice(&masked);
        } else {
            out.extend_from_slice(payload);
        }
        out
    }

    /// 在裸句柄上写一帧（读线程回 Pong 用）。
    fn write_frame_static(stream: &mut TcpStream, opcode: u8, payload: &[u8], is_client: bool) -> std::io::Result<()> {
        let out = Self::encode_frame(opcode, payload, is_client);
        stream.write_all(&out)?;
        stream.flush()
    }

    /// 写一个帧（文本/二进制；单帧不分片）。经 writer 短锁，多线程安全。
    pub fn write_frame(&self, opcode: u8, payload: &[u8]) -> std::io::Result<()> {
        let out = Self::encode_frame(opcode, payload, self.is_client);
        let mut g = self.writer.lock().map_err(|_| {
            std::io::Error::new(std::io::ErrorKind::Other, "writer 锁中毒")
        })?;
        g.write_all(&out)?;
        g.flush()
    }

    pub fn send_text(&self, text: &str) -> std::io::Result<()> {
        self.write_frame(OP_TEXT, text.as_bytes())
    }

    pub fn ping(&self) -> std::io::Result<()> {
        self.write_frame(OP_PING, b"")
    }

    #[allow(dead_code)]
    pub fn close(&self) {
        let _ = self.write_frame(OP_CLOSE, b"");
    }
}

fn gen_mask_key() -> [u8; 4] {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let mut x = t ^ (n.wrapping_mul(0x9E3779B97F4A7C15)) ^ (std::process::id() as u64) << 32;
    // xorshift 混合，让 4 个字节尽量不相关
    x ^= x >> 30;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 27;
    x = x.wrapping_mul(0x94D049BB133111EB);
    x ^= x >> 31;
    let b = x.to_le_bytes();
    [b[0], b[1], b[2], b[3]]
}

/// URL 解析结果。
pub struct WsUrl {
    pub host: String,
    pub port: u16,
    pub path: String,
    pub host_header: String,
}

/// 解析 ws://host:port/path 或 http://host:port/path（http 视为 ws）。
/// wss:// 返回错误：std 无 TLS，需在前置反代终止 TLS（明确报错，不静默失败）。
pub fn parse_ws_url(url: &str) -> Result<WsUrl, String> {
    let u = url.trim();
    let (scheme, rest) = if let Some(r) = u.strip_prefix("ws://") {
        ("ws", r)
    } else if let Some(r) = u.strip_prefix("http://") {
        ("ws", r)
    } else if u.strip_prefix("wss://").is_some() {
        return Err(format!(
            "wss:// 需要 TLS，std 无 TLS 实现：请用 ws:// 或在前置反代（nginx/stunnel）终止 TLS 后连 ws://（url={}）",
            u
        ));
    } else if u.strip_prefix("https://").is_some() {
        return Err(format!(
            "https:// 需要 TLS，std 无 TLS 实现：请用 ws:// 或在前置反代终止 TLS 后连 ws://（url={}）",
            u
        ));
    } else {
        // 缺省按 ws 处理（host:port/path）
        ("ws", u)
    };
    let _ = scheme;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => {
            let port: u16 = p.parse().map_err(|e| format!("端口非法: {} ({})", p, e))?;
            (h.to_string(), port)
        }
        None => (authority.to_string(), 80u16),
    };
    if host.is_empty() {
        return Err("URL 缺 host".to_string());
    }
    Ok(WsUrl {
        host_header: if authority.contains(':') {
            authority.to_string()
        } else {
            format!("{}:{}", host, port)
        },
        host,
        port,
        path: if path.is_empty() { "/".to_string() } else { path.to_string() },
    })
}

/// 客户端握手：连上并升级为 WebSocket。
pub fn ws_connect(url: &str, token: Option<&str>) -> Result<WsConn, String> {
    let u = parse_ws_url(url)?;
    let mut stream = TcpStream::connect((u.host.as_str(), u.port))
        .map_err(|e| format!("连接 {}:{} 失败: {}", u.host, u.port, e))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(300)))
        .map_err(|e| format!("设置读超时失败: {}", e))?;
    stream
        .set_nodelay(true)
        .map_err(|e| format!("设置 nodelay 失败: {}", e))?;

    let key = base64_key();
    let mut req = format!(
        "GET {path} HTTP/1.1\r\n\
         Host: {host}\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Key: {key}\r\n\
         Sec-WebSocket-Version: 13\r\n",
        path = u.path,
        host = u.host_header,
        key = key,
    );
    if let Some(t) = token {
        if !t.is_empty() {
            req.push_str(&format!("Authorization: Bearer {}\r\n", t));
        }
    }
    req.push_str("\r\n");

    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("握手写失败: {}", e))?;
    stream
        .flush()
        .map_err(|e| format!("握手 flush 失败: {}", e))?;

    // 读响应头（直到 \r\n\r\n）
    let mut buf = Vec::with_capacity(1024);
    let mut byte = [0u8; 1];
    loop {
        stream
            .read_exact(&mut byte)
            .map_err(|e| format!("握手读失败: {}", e))?;
        buf.push(byte[0]);
        if buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n" {
            break;
        }
        if buf.len() > 16 * 1024 {
            return Err("握手响应头过长".to_string());
        }
    }
    let head = String::from_utf8_lossy(&buf);
    let status_line = head.lines().next().unwrap_or("");
    if !status_line.contains("101") {
        return Err(format!("握手失败: {}", status_line.trim()));
    }
    if !head.to_lowercase().contains("upgrade: websocket") {
        return Err("握手响应缺 Upgrade: websocket".to_string());
    }
    WsConn::new(stream, true).map_err(|e| format!("克隆写句柄失败: {}", e))
}

/// 服务端握手：accept 到的连接先按 HTTP 升级请求处理，返回 101。
pub fn ws_accept(mut stream: TcpStream) -> Result<WsConn, String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(300)))
        .map_err(|e| format!("设置读超时失败: {}", e))?;
    stream
        .set_nodelay(true)
        .map_err(|e| format!("设置 nodelay 失败: {}", e))?;
    let mut buf = Vec::with_capacity(2048);
    let mut byte = [0u8; 1];
    loop {
        stream
            .read_exact(&mut byte)
            .map_err(|e| format!("握手读失败: {}", e))?;
        buf.push(byte[0]);
        if buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n" {
            break;
        }
        if buf.len() > 16 * 1024 {
            return Err("握手请求头过长".to_string());
        }
    }
    let req = String::from_utf8_lossy(&buf).to_lowercase();
    if !req.contains("upgrade: websocket") {
        return Err("不是 WebSocket 升级请求".to_string());
    }
    // 服务端 accept = base64(sha1(client_key + RFC 固定 magic))（RFC 6455 §1.3）
    let accept_key = {
        let raw = String::from_utf8_lossy(&buf);
        // 注意：必须 filter_map 收集所有 "k: v" 行再 find 目标头——
        // 若用 find_map 会在第一行（Host）就返回，filter 永远轮不到
        // Sec-WebSocket-Key（2026-10-14 实测 bug）。
        let key = raw
            .lines()
            .filter_map(|l| l.split_once(':'))
            .find(|(k, _)| k.trim().eq_ignore_ascii_case("sec-websocket-key"))
            .map(|(_, v)| v.trim().to_string())
            .unwrap_or_default();
        if key.is_empty() {
            return Err("升级请求缺 Sec-WebSocket-Key".to_string());
        }
        let mut input = key.into_bytes();
        input.extend_from_slice(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
        base64_encode(&sha1(&input))
    };
    let resp = format!(
        "HTTP/1.1 101 Switching Protocols\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Accept: {}\r\n\r\n",
        accept_key
    );
    stream
        .write_all(resp.as_bytes())
        .map_err(|e| format!("握手写失败: {}", e))?;
    stream
        .flush()
        .map_err(|e| format!("握手 flush 失败: {}", e))?;
    WsConn::new(stream, false).map_err(|e| format!("克隆写句柄失败: {}", e))
}

/// 客户端 Sec-WebSocket-Key：16 随机字节 base64。
fn base64_key() -> String {
    let mut b = [0u8; 16];
    for chunk in b.chunks_mut(4) {
        let kb = gen_mask_key();
        for (i, c) in chunk.iter_mut().enumerate() {
            *c = kb[i];
        }
    }
    base64_encode(&b)
}

const B64: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { B64[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { B64[n as usize & 63] as char } else { '=' });
    }
    out
}

/// SHA-1（FIPS 180-1）最小实现——服务端握手 accept 值计算用。
pub fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let ml = (data.len() as u64) * 8;
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&ml.to_be_bytes());
    for block in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[i * 4],
                block[i * 4 + 1],
                block[i * 4 + 2],
                block[i * 4 + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let tmp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, v) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}
