//! Owns the packaged Wicx transcription service for the lifetime of the Tauri shell.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

#[derive(Clone, Default)]
pub struct DaemonSupervisor {
    shutdown: Arc<AtomicBool>,
    child: Arc<Mutex<Option<CommandChild>>>,
}

impl DaemonSupervisor {
    pub fn request_shutdown(&self) {
        append_sidecar_log("shutdown requested");
        self.shutdown.store(true, Ordering::SeqCst);
        if let Ok(mut child) = self.child.lock() {
            if let Some(child) = child.take() {
                let pid = child.pid();
                // PyInstaller's --onefile bootloader launches the extracted
                // application as a child process. Killing CommandChild alone
                // terminates only the bootloader and leaves the actual daemon
                // serving behind. Terminate the whole Windows process tree
                // before falling back to the shell plugin's direct-child kill.
                #[cfg(windows)]
                let tree_terminated = terminate_process_tree(pid);
                #[cfg(not(windows))]
                let tree_terminated = false;

                if tree_terminated {
                    append_sidecar_log(&format!("sidecar process tree terminated (pid {pid})"));
                } else {
                    match child.kill() {
                        Ok(()) => {
                            append_sidecar_log(&format!("sidecar kill signaled (pid {pid})"))
                        }
                        Err(error) => {
                            append_sidecar_log(&format!(
                                "sidecar kill failed (pid {pid}): {error}; trying TerminateProcess"
                            ));
                            force_terminate_process(pid);
                        }
                    }
                }
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

/// Stop a Windows sidecar and descendants (notably PyInstaller's onefile
/// child) as one operation. `CommandChild::kill` only targets the launcher.
#[cfg(windows)]
fn terminate_process_tree(pid: u32) -> bool {
    use std::{os::windows::process::CommandExt, process::Command};

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    match Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        Ok(output) if output.status.success() => true,
        Ok(output) => {
            append_sidecar_log(&format!(
                "taskkill process tree failed (pid {pid}, status {}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
            false
        }
        Err(error) => {
            append_sidecar_log(&format!("could not run taskkill for pid {pid}: {error}"));
            false
        }
    }
}

fn sidecar_log_path() -> std::path::PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    base.join("Whisper").join("logs").join("ui-sidecar.log")
}

/// Best-effort file log of sidecar supervision. Spawn failures are otherwise
/// only visible as webview events, which headless smoke tests cannot read.
fn append_sidecar_log(message: &str) {
    use std::io::Write;
    let path = sidecar_log_path();
    if let Some(parent) = path.parent() {
        if std::fs::create_dir_all(parent).is_err() {
            return;
        }
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "{secs} {message}");
    }
}

/// Last-resort termination when the plugin kill reports failure. Runs
/// immediately on the still-valid pid while the PID-reuse window is tiny.
#[cfg(windows)]
fn force_terminate_process(pid: u32) {
    use windows::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
    unsafe {
        match OpenProcess(PROCESS_TERMINATE, false, pid) {
            Ok(handle) => {
                let result = TerminateProcess(handle, 1);
                append_sidecar_log(&format!("TerminateProcess(pid {pid}) returned {result:?}"));
            }
            Err(error) => append_sidecar_log(&format!("OpenProcess(pid {pid}) failed: {error}")),
        }
    }
}

#[cfg(not(windows))]
fn force_terminate_process(pid: u32) {
    append_sidecar_log(&format!("no TerminateProcess fallback off Windows (pid {pid})"));
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
            // NOTE: sidecar() takes the bare binary name, not the externalBin
            // path ("whisper-daemon", never "binaries/whisper-daemon").
            // The full path form fails resolution with os error 3 on every
            // attempt, leaving the UI running with no daemon.
            let child = app
                .shell()
                .sidecar("whisper-daemon")
                .map(|command| command.args(["--ui-token", websocket_token.as_str()]));
            match child.and_then(|command| command.spawn()) {
                Ok((mut events, child)) => {
                    supervisor.set_child(child);
                    append_sidecar_log("sidecar spawned");
                    let _ = app.emit("daemon-process", "started");
                    while let Some(event) = events.recv().await {
                        match event {
                            CommandEvent::Terminated(status) => {
                                if status.code == Some(75) {
                                    // The daemon survived a UI-shell restart.
                                    // Its stable per-user token lets this UI
                                    // reconnect without replacing active state.
                                    adopted_existing_daemon = true;
                                    append_sidecar_log("adopted existing daemon (exit 75)");
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
                                    let _ =
                                        app.emit("daemon-process", format!("stopped: {detail}"));
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
                    append_sidecar_log(&format!("sidecar spawn failed: {error}"));
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
