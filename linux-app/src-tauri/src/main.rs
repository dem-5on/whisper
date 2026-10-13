#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};

use serde_json::{json, Map, Value};
use tauri::{Emitter, Manager, PhysicalPosition};
use tauri_plugin_global_shortcut::ShortcutState;

mod shortcuts;

fn daemon_socket() -> PathBuf {
    if let Some(runtime) = std::env::var_os("XDG_RUNTIME_DIR") {
        return PathBuf::from(runtime).join("transcriber.sock");
    }
    PathBuf::from(format!("/tmp/transcriber-{}.sock", libc_uid()))
}

#[cfg(target_os = "linux")]
fn libc_uid() -> u32 {
    unsafe { libc::getuid() }
}

#[cfg(not(target_os = "linux"))]
fn libc_uid() -> u32 {
    0
}

fn bottom_center_pill_position(
    monitor_position: (i32, i32),
    monitor_size: (u32, u32),
    pill_size: (u32, u32),
    scale_factor: f64,
) -> (i32, i32) {
    let x = monitor_position.0 + (monitor_size.0 as i32 - pill_size.0 as i32) / 2;
    let bottom_gap = (36.0 * scale_factor) as i32;
    let y = monitor_position.1 + monitor_size.1 as i32 - pill_size.1 as i32 - bottom_gap;
    (x, y)
}

#[tauri::command]
fn daemon_request(command: String, params: Option<Value>) -> Result<Value, String> {
    const ALLOWED: &[&str] = &[
        "status",
        "toggle",
        "cancel",
        "retry",
        "sessions",
        "get_session",
        "delete_session",
        "dictionary",
        "start_session",
        "stop_session",
        "set_rewrite",
        "set_recording_pill_hidden",
        "mics",
        "set_mic",
    ];
    if !ALLOWED.contains(&command.as_str()) {
        return Err("That Wicx action is not supported.".to_string());
    }

    let params = match params {
        Some(Value::Object(values)) => values,
        _ => Map::new(),
    };
    send_daemon(&command, params)
}

pub fn send_daemon(command: &str, params: Map<String, Value>) -> Result<Value, String> {
    let mut payload = Map::new();
    payload.insert("command".into(), json!(command));
    payload.extend(params.into_iter().filter(|(key, _)| key != "command"));

    let socket = daemon_socket();
    let mut stream = UnixStream::connect(&socket).map_err(|_| {
        "Wicx service is unavailable. Start it from your desktop panel or user service.".to_string()
    })?;
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .map_err(|_| "Could not configure the local Wicx connection.".to_string())?;
    stream
        .write_all(format!("{}\n", Value::Object(payload)).as_bytes())
        .map_err(|_| "Could not send the request to Wicx.".to_string())?;

    let mut response = String::new();
    BufReader::new(stream)
        .read_line(&mut response)
        .map_err(|_| "Wicx returned no response. Try again.".to_string())?;
    if response.is_empty() {
        return Err("Wicx closed the local connection without responding.".to_string());
    }
    let response: Value = serde_json::from_str(&response)
        .map_err(|_| "Wicx returned an invalid response.".to_string())?;
    if response.get("ok").and_then(Value::as_str) == Some("false") {
        return Err(response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("Wicx could not complete that action.")
            .to_string());
    }
    Ok(response)
}

#[tauri::command]
fn update_recording_pill(
    app: tauri::AppHandle,
    visible: bool,
    status: Value,
) -> Result<(), String> {
    let pill = app
        .get_webview_window("recording-pill")
        .ok_or_else(|| "The Wicx recording pill window was not created.".to_string())?;

    let currently_visible = pill
        .is_visible()
        .map_err(|error| format!("Could not check recording pill visibility: {error}"))?;
    if visible != currently_visible {
        if visible {
            pill.show()
                .map_err(|error| format!("Could not show recording pill: {error}"))?;
            // Position only after the surface is visible/realized. On X11 this
            // anchors the compact pill above the bottom edge; Wayland compositors
            // retain final placement authority and may ignore client requests.
            if let Ok(Some(monitor)) = pill.current_monitor() {
                if let (Ok(window_size), Ok(scale)) = (pill.outer_size(), pill.scale_factor()) {
                    let monitor_position = monitor.position();
                    let monitor_size = monitor.size();
                    let (x, y) = bottom_center_pill_position(
                        (monitor_position.x, monitor_position.y),
                        (monitor_size.width, monitor_size.height),
                        (window_size.width, window_size.height),
                        scale,
                    );
                    let _ = pill.set_position(PhysicalPosition::new(x, y));
                }
            }
        } else {
            pill.hide()
                .map_err(|error| format!("Could not hide recording pill: {error}"))?;
        }
    }

    app.emit_to("recording-pill", "recording-status", status)
        .map_err(|error| format!("Could not update recording pill: {error}"))
}

fn main() {
    // Wayland does not let regular application windows choose absolute screen
    // coordinates. When a Wayland session also exposes XWayland, prefer it so
    // the recording pill can be anchored at screen bottom-center. Respect an
    // explicit user GDK_BACKEND choice and keep native Wayland as fallback.
    if std::env::var_os("WAYLAND_DISPLAY").is_some()
        && std::env::var_os("DISPLAY").is_some()
        && std::env::var_os("GDK_BACKEND").is_none()
    {
        // Safe before Tauri/GTK initializes and before this process starts threads.
        unsafe { std::env::set_var("GDK_BACKEND", "x11") };
    }

    tauri::Builder::default()
        .manage(shortcuts::ShortcutState_::new())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        shortcuts::handle_press(app, shortcut);
                    }
                })
                .build(),
        )
        .setup(|app| {
            eprintln!("wicx: setup begin");
            shortcuts::init(&app.handle())?;
            eprintln!("wicx: setup shortcuts ready");
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            daemon_request,
            update_recording_pill,
            shortcuts::get_shortcuts,
            shortcuts::set_shortcut,
            shortcuts::set_long_consent,
            shortcuts::set_role,
            shortcuts::set_shortcut_test,
            shortcuts::complete_setup
        ])
        .run(tauri::generate_context!())
        .expect("error while running Wicx Linux desktop app");
}

#[cfg(test)]
mod tests {
    use super::bottom_center_pill_position;

    #[test]
    fn pill_is_centered_and_inset_from_bottom() {
        assert_eq!(
            bottom_center_pill_position((0, 0), (1920, 1080), (400, 74), 1.0),
            (760, 970),
        );
    }

    #[test]
    fn pill_position_respects_negative_monitor_origin() {
        assert_eq!(
            bottom_center_pill_position((-1920, -200), (1920, 1080), (400, 74), 1.0),
            (-1160, 770),
        );
    }

    #[test]
    fn bottom_inset_scales_with_display_density() {
        assert_eq!(
            bottom_center_pill_position((50, 100), (1440, 900), (400, 72), 1.5),
            (570, 874),
        );
    }
}
