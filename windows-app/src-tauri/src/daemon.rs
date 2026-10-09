//! Owns the packaged Whisper daemon for the lifetime of the Tauri shell.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};

#[derive(Clone, Default)]
pub struct DaemonSupervisor {
    shutdown: Arc<AtomicBool>,
    child: Arc<Mutex<Option<CommandChild>>>,
}

impl DaemonSupervisor {
    pub fn request_shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Ok(mut child) = self.child.lock() {
            if let Some(child) = child.take() {
                let _ = child.kill();
            }
        }
    }

    fn is_shutdown_requested(&self) -> bool {
        self.shutdown.load(Ordering::SeqCst)
    }

    fn set_child(&self, child: CommandChild) {
        if let Ok(mut current) = self.child.lock() {
            // Recheck after acquiring the same lock used by shutdown. If
            // shutdown started while this thread was waiting for the lock,
            // never install a child that would outlive the Tauri shell.
            if self.is_shutdown_requested() {
                let _ = child.kill();
            } else {
                *current = Some(child);
            }
        } else {
            let _ = child.kill();
        }
    }

    fn clear_child(&self) {
        if let Ok(mut child) = self.child.lock() {
            child.take();
        }
    }
}

pub fn supervise(app: AppHandle, supervisor: DaemonSupervisor, websocket_token: String) {
    // Development uses the developer's project environment and separately
    // started daemon; the sidecar exists only in packaged builds.
    if cfg!(debug_assertions) {
        return;
    }

    tauri::async_runtime::spawn(async move {
        let mut retry_delay = Duration::from_secs(1);
        while !supervisor.is_shutdown_requested() {
            let started_at = Instant::now();
            let mut adopted_existing_daemon = false;
            let child = app
                .shell()
                .sidecar("binaries/whisper-daemon")
                .map(|command| command.args(["--ui-token", websocket_token.as_str()]));
            match child.and_then(|command| command.spawn()) {
                Ok((mut events, child)) => {
                    supervisor.set_child(child);
                    let _ = app.emit("daemon-process", "started");
                    while let Some(event) = events.recv().await {
                        match event {
                            CommandEvent::Terminated(status) => {
                                if status.code == Some(75) {
                                    // The daemon survived a UI-shell restart.
                                    // Its stable per-user token lets this UI
                                    // reconnect without replacing active state.
                                    adopted_existing_daemon = true;
                                    let _ = app.emit("daemon-process", "adopted-existing");
                                    break;
                                }
                                if !supervisor.is_shutdown_requested() {
                                    let detail = status
                                        .code
                                        .map(|code| format!("exit code {code}"))
                                        .or_else(|| {
                                            status.signal.map(|signal| format!("signal {signal}"))
                                        })
                                        .unwrap_or_else(|| "unknown reason".to_string());
                                    let _ = app.emit(
                                        "daemon-process",
                                        format!("stopped: {detail}"),
                                    );
                                }
                                break;
                            }
                            CommandEvent::Error(_) => {
                                // Keep process diagnostics out of the UI; the
                                // socket connection reports whether service is ready.
                            }
                            CommandEvent::Stdout(_) | CommandEvent::Stderr(_) => {}
                            _ => {}
                        }
                    }
                    supervisor.clear_child();
                    if !adopted_existing_daemon {
                        let _ = app.emit("daemon-process", "stopped");
                    }
                }
                Err(error) => {
                    // Surface a concise launch failure to the tray UI instead
                    // of leaving users with an unexplained reconnect spinner.
                    let _ = app.emit("daemon-process", format!("unavailable: {error}"));
                }
            }

            if supervisor.is_shutdown_requested() {
                break;
            }
            if adopted_existing_daemon {
                // Probe periodically by attempting the sidecar startup again.
                // An active daemon exits quickly with the adoption sentinel;
                // once it is gone, this shell starts supervising a new one.
                retry_delay = Duration::from_secs(5);
                tokio::time::sleep(retry_delay).await;
                continue;
            }
            if started_at.elapsed() >= Duration::from_secs(30) {
                retry_delay = Duration::from_secs(1);
            }
            tokio::time::sleep(retry_delay).await;
            retry_delay = (retry_delay * 2).min(Duration::from_secs(30));
        }
    });
}
