#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};

use serde_json::{json, Map, Value};

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

    let mut payload = Map::new();
    payload.insert("command".into(), json!(command));
    if let Some(Value::Object(values)) = params {
        payload.extend(values.into_iter().filter(|(key, _)| key != "command"));
    }

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

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![daemon_request])
        .run(tauri::generate_context!())
        .expect("error while running Wicx Linux desktop app");
}
