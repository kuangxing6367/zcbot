//! 入口：组装 IPC 服务端 + OneBot 反向 WS 服务端（接收 + 广播），输出 HELLO 后常驻。

mod api;
mod config;
mod ipc;
mod normalize;
mod stats;
mod ws;

use std::sync::atomic::AtomicU64;
use std::sync::Arc;

use api::{Broadcaster, Conns};
use config::Config;
use stats::Stats;

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let cfg = Arc::new(Config::from_env());
    let stats = Arc::new(Stats::new());
    let conns: Conns = Arc::new(tokio::sync::RwLock::new(std::collections::HashMap::new()));
    let bc = Arc::new(Broadcaster::new(conns.clone(), stats.clone()));
    let bot_counter = Arc::new(AtomicU64::new(1));

    // 1) IPC 服务端（127.0.0.1:0，OS 分配端口）：Python 壳据此建连
    let (ipc_port, out) = match ipc::serve(cfg.clone(), stats.clone(), bc.clone()).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[rust_accel] IPC 启动失败: {e}");
            std::process::exit(1);
        }
    };

    // 2) OneBot 反向 WS 服务端（接收 + 广播共用连接表）
    let ws_ready = match ws::spawn(
        cfg.clone(),
        stats.clone(),
        out.clone(),
        conns.clone(),
        bc.clone(),
        bot_counter,
    )
    .await
    {
        Ok(port) => {
            eprintln!("[rust_accel] WS 服务端已启动, ws_port={port}");
            Some(port)
        }
        Err(e) => {
            eprintln!("[rust_accel] WS 启动失败: {e}");
            None
        }
    };

    // 3) HELLO 握手（stdout 首行）。Python 壳读到此行后连接 IPC；
    //    此步必须立即 flush，供父进程阻塞读取。
    let hello = format!(
        "{{\"type\":\"hello\",\"ipc_port\":{},\"ws_ready\":{},\"ws_port\":{},\"version\":\"0.1.0\"}}",
        ipc_port,
        ws_ready.is_some(),
        ws_ready.unwrap_or(cfg.ws_port),
    );
    println!("{hello}");
    use std::io::Write as _;
    let _ = std::io::stdout().flush();

    if ws_ready.is_none() {
        eprintln!("[rust_accel] WS 未就绪，退出");
        std::process::exit(1);
    }

    // 4) 常驻：正常退出路径为 IPC shutdown（read_loop 内 process::exit）或 Ctrl-C
    match tokio::signal::ctrl_c().await {
        Ok(()) => {
            eprintln!("[rust_accel] Ctrl-C，退出");
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("[rust_accel] 信号监听失败: {e}");
        }
    }
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
    }
}