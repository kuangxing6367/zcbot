//! 广播端：每 bot 写队列（有界 mpsc，满即失败）+ echo 响应匹配 + 超时回执。
//! 语义对齐 core_plugins/onebot_adapter/main.py BotConnection.acall：
//!   - 未指定 bot 时取第一个已连接（与 ApiCaller.get_connection 一致）
//!   - 应答等待 10s 超时（对齐 asyncio.wait_for(10)），结果原样透传
//! 在框架整体提速中的角色：把"json.dumps + WS 写 + 响应挂起"从 Python
//! 事件循环移出（Rust 侧 Sink 串行写出 + oneshot 匹配），call 方只等 IPC 回执。

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{json, Value};
use tokio::sync::{mpsc, oneshot, Mutex, RwLock};
use tokio_tungstenite::tungstenite::Message;

use crate::stats::Stats;

/// 每 bot 写侧槽位：有界队列 + 独立写 task（Sink 串行写出，不阻塞 WS 读循环）
pub struct BotSlot {
    pub tx: mpsc::Sender<Message>,
}

/// 在线 bot 连接表：name -> 写侧槽位（ipc query / 广播选路共用）
pub type Conns = Arc<RwLock<HashMap<String, BotSlot>>>;

/// 广播端核心：选连接 → 序列化 → 入队 → echo 匹配 → IPC 回执
pub struct Broadcaster {
    conns: Conns,
    stats: Arc<Stats>,
    /// echo -> (call id, 应答 oneshot 发送端)
    pending: Arc<Mutex<HashMap<String, (Value, oneshot::Sender<Value>)>>>,
    next_echo: AtomicU64,
}

/// 动作响应等待超时（对齐 Python asyncio.wait_for 10s）
const CALL_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// 每 bot 发送队列上限（满即时失败 + 计数；ws.rs 建队时同步使用）
pub const SEND_QUEUE_CAP: usize = 1024;

fn fail_resp(id: Value, retcode: i64, msg: &str) -> Value {
    json!({
        "type": "call_resp", "id": id, "ok": false,
        "result": {"status": "failed", "retcode": retcode, "msg": msg},
    })
}

impl Broadcaster {
    pub fn new(conns: Conns, stats: Arc<Stats>) -> Self {
        Self {
            conns,
            stats,
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_echo: AtomicU64::new(1),
        }
    }

    /// 当前在线 bot 名单（ipc query connections 用），语义同旧 HashSet 集合
    pub async fn names(&self) -> Vec<String> {
        self.conns.read().await.keys().cloned().collect()
    }

    /// 处理 py→rust `call`，返回完整 call_resp（id/ok/result/elapsed_ms）
    pub async fn call(&self, raw: &Value) -> Value {
        self.stats.api_calls.fetch_add(1, Ordering::Relaxed);
        let id = raw.get("id").cloned().unwrap_or(Value::Null);
        let action = match raw.get("action").and_then(Value::as_str) {
            Some(a) if !a.is_empty() => a.to_string(),
            _ => return fail_resp(id, -3, "action 缺失"),
        };
        let params = raw.get("params").cloned().unwrap_or_else(|| json!({}));
        let bot = raw.get("bot").and_then(Value::as_str);

        // 选连接：指定 bot 优先；未指定取第一个已连接（与 ApiCaller.get_connection 一致）
        let tx = {
            let g = self.conns.read().await;
            bot.and_then(|b| g.get(b))
                .or_else(|| g.values().next())
                .map(|s| s.tx.clone())
        };
        let Some(tx) = tx else {
            self.stats.api_noconn.fetch_add(1, Ordering::Relaxed);
            return fail_resp(id, -1, "无可用 OneBot 连接");
        };

        let echo = format!("r{}", self.next_echo.fetch_add(1, Ordering::Relaxed));
        let payload = json!({ "action": action, "params": params, "echo": echo });
        let (otx, orx) = oneshot::channel();
        self.pending.lock().await.insert(echo.clone(), (id.clone(), otx));

        // 有界队列即时投递：满/断开（channel closed）即时失败，不发就撤 pending
        let send = tx.try_send(Message::Text(payload.to_string().into()));
        if let Err(e) = send {
            self.pending.lock().await.remove(&echo);
            return match e {
                mpsc::error::TrySendError::Full(_) => {
                    self.stats.api_qfull.fetch_add(1, Ordering::Relaxed);
                    fail_resp(id, -1, "动作发送队列已满")
                }
                mpsc::error::TrySendError::Closed(_) => {
                    self.stats.api_fail.fetch_add(1, Ordering::Relaxed);
                    fail_resp(id, -1, "OneBot 连接已断开")
                }
            };
        }

        let t0 = Instant::now();
        match tokio::time::timeout(CALL_TIMEOUT, orx).await {
            Ok(Ok(data)) => {
                let elapsed_ms = t0.elapsed().as_millis() as u64;
                self.stats.api_ok.fetch_add(1, Ordering::Relaxed);
                self.stats.add_api_rt(elapsed_ms);
                let ok = data
                    .get("status")
                    .and_then(Value::as_str)
                    .map(|s| s == "ok")
                    .unwrap_or(false);
                json!({
                    "type": "call_resp", "id": id, "ok": ok,
                    "result": data, "elapsed_ms": elapsed_ms,
                })
            }
            Ok(Err(_)) => {
                self.stats.api_fail.fetch_add(1, Ordering::Relaxed);
                fail_resp(id, -3, "内部应答通道关闭")
            }
            Err(_) => {
                self.pending.lock().await.remove(&echo);
                self.stats.api_timeout.fetch_add(1, Ordering::Relaxed);
                fail_resp(id, -2, "请求超时")
            }
        }
    }

    /// WS 收帧含 echo 时的匹配入口；命中向对应 call 投递应答（原样透传）
    pub async fn on_echo(&self, echo: &str, data: Value) -> bool {
        let tx = {
            let mut g = self.pending.lock().await;
            g.remove(echo).map(|(_, t)| t)
        };
        match tx {
            Some(t) => {
                let _ = t.send(data);
                true
            }
            None => false,
        }
    }
}