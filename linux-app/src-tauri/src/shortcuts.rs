//! Global keyboard shortcuts for the Linux desktop app.
//!
//! Three mode bindings (Dictation, Meeting, Lecture) are stored in
//! `$XDG_CONFIG_HOME/wicx/shortcuts.json` and registered through
//! `tauri-plugin-global-shortcut`. Registration is best-effort: Wayland
//! compositors without the Global Shortcuts portal will refuse, in which
//! case the error is reported per action and the desktop-environment custom
//! shortcut fallback (`wicx toggle`, `wicx start-session`, ...) remains the
//! supported path.

use std::{collections::HashMap, fs, path::PathBuf, str::FromStr, sync::Mutex};

use serde_json::{json, Map, Value};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

use crate::send_daemon;

pub const ACTIONS: &[&str] = &["dictation", "meeting", "lecture"];

pub struct ShortcutState_ {
    errors: Mutex<HashMap<String, String>>,
}

impl ShortcutState_ {
    pub fn new() -> Self {
        Self {
            errors: Mutex::new(HashMap::new()),
        }
    }

    fn set_error(&self, action: &str, error: String) {
        if let Ok(mut errors) = self.errors.lock() {
            errors.insert(action.to_string(), error);
        }
    }

    fn clear_error(&self, action: &str) {
        if let Ok(mut errors) = self.errors.lock() {
            errors.remove(action);
        }
    }

    fn error(&self, action: &str) -> Option<String> {
        self.errors
            .lock()
            .ok()
            .and_then(|errors| errors.get(action).cloned())
    }
}

fn config_dir() -> PathBuf {
    if let Some(home) = std::env::var_os("XDG_CONFIG_HOME") {
        return PathBuf::from(home).join("wicx");
    }
    if let Some(home) = std::env::var_os("HOME") {
        return PathBuf::from(home).join(".config").join("wicx");
    }
    PathBuf::from("/tmp/wicx-config")
}

fn store_path() -> PathBuf {
    config_dir().join("shortcuts.json")
}

fn default_store() -> Value {
    json!({
        "version": 1,
        "setup_completed": false,
        "long_consent": false,
        "bindings": {
            "dictation": {"display": "Alt + Space", "accelerator": "Alt+Space"},
            "meeting": {"display": "", "accelerator": null},
            "lecture": {"display": "", "accelerator": null},
        },
    })
}

fn load_store() -> Value {
    let path = store_path();
    let mut store = default_store();
    let text = fs::read_to_string(&path).unwrap_or_default();
    if text.trim().is_empty() {
        return store;
    }
    let parsed: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    if let (Some(map), Some(bindings)) = (
        store.as_object_mut(),
        parsed.get("bindings").and_then(Value::as_object),
    ) {
        if let Some(ours) = map.get_mut("bindings").and_then(Value::as_object_mut) {
            for action in ACTIONS {
                if let Some(entry) = bindings.get(*action) {
                    ours.insert((*action).to_string(), entry.clone());
                }
            }
        }
        if let Some(consent) = parsed.get("long_consent").and_then(Value::as_bool) {
            map.insert("long_consent".into(), json!(consent));
        }
        if let Some(done) = parsed.get("setup_completed").and_then(Value::as_bool) {
            map.insert("setup_completed".into(), json!(done));
        }
    }
    store
}

fn save_store(store: &Value) -> Result<(), String> {
    let dir = config_dir();
    fs::create_dir_all(&dir).map_err(|error| format!("Could not store shortcuts: {error}"))?;
    let text = serde_json::to_string_pretty(store)
        .map_err(|error| format!("Could not store shortcuts: {error}"))?;
    fs::write(store_path(), text).map_err(|error| format!("Could not store shortcuts: {error}"))?;
    Ok(())
}

/// Validate a user-captured accelerator before it is stored or registered.
///
/// Accepts modifier combos, function keys, and the bare `Alt` default (meant
/// as Right Alt; the plugin cannot isolate left from right). Bare Left
/// Ctrl/Shift/Super and single typing characters are refused because they
/// would break normal use.
pub fn validate_accelerator(accelerator: &str) -> Result<(), String> {
    let text = accelerator.trim();
    if text.is_empty() {
        return Err("Press a key combination first.".to_string());
    }
    let upper = text.to_uppercase();
    if upper == "ALT" {
        return Err("“Alt” alone cannot be grabbed by apps. Use Alt+Space or a combination — or bind Right Alt in your desktop settings.".to_string());
    }
    for banned in ["CONTROL", "CTRL", "SHIFT", "SUPER", "META", "WIN"] {
        if upper == banned {
            return Err(format!(
                "“{text}” alone would break your system. Add another key, or use Right Alt."
            ));
        }
    }
    if text.chars().count() == 1 && text.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err(format!(
            "“{text}” alone fires while typing. Add Alt, Ctrl, or Shift."
        ));
    }
    Shortcut::from_str(text)
        .map(|_| ())
        .map_err(|_| format!("“{text}” is not a usable shortcut. Try a combination like Alt+M."))?;
    Ok(())
}

fn binding(store: &Value, action: &str) -> (String, Option<String>) {
    let display = store
        .get("bindings")
        .and_then(|b| b.get(action))
        .and_then(|b| b.get("display"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let accelerator = store
        .get("bindings")
        .and_then(|b| b.get(action))
        .and_then(|b| b.get("accelerator"))
        .and_then(Value::as_str)
        .map(str::to_string);
    (display, accelerator)
}

fn register_all(app: &AppHandle) {
    let store = load_store();
    let handle = app.clone();
    let shortcuts = match handle.try_state::<ShortcutState_>() {
        Some(state) => state,
        None => return,
    };
    let global = handle.global_shortcut();
    let _ = global.unregister_all();
    for action in ACTIONS {
        let (_, accelerator) = binding(&store, action);
        let Some(accelerator) = accelerator.filter(|a| !a.trim().is_empty()) else {
            shortcuts.clear_error(action);
            continue;
        };
        let parsed = match Shortcut::from_str(&accelerator) {
            Ok(parsed) => parsed,
            Err(_) => {
                shortcuts.set_error(
                    action,
                    format!("“{accelerator}” could not be used. Pick another combination."),
                );
                continue;
            }
        };
        match global.register(parsed) {
            Ok(()) => shortcuts.clear_error(action),
            Err(error) => shortcuts.set_error(
                action,
                format!("System refused “{accelerator}” ({error}). Use the desktop-shortcut fallback below."),
            ),
        }
    }
}

fn fire_action(app: &AppHandle, action: &str) {
    let result = match action {
        "dictation" => send_daemon("toggle", Map::new()),
        "meeting" | "lecture" => toggle_long_session(app, action),
        _ => Err("Unknown shortcut action.".to_string()),
    };
    if let Err(error) = result {
        eprintln!("wicx shortcut '{action}' failed: {error}");
    }
}

fn toggle_long_session(app: &AppHandle, mode: &str) -> Result<Value, String> {
    let status = send_daemon("status", Map::new())?;
    let recording_same_mode = status.get("state").and_then(Value::as_str) == Some("RECORDING")
        && status.get("session_mode").and_then(Value::as_str) == Some(mode);
    if recording_same_mode {
        return send_daemon("stop_session", Map::new());
    }
    if status.get("state").and_then(Value::as_str) == Some("RECORDING") {
        return Err("Finish the current recording first.".to_string());
    }
    let store = load_store();
    if store.get("long_consent").and_then(Value::as_bool) != Some(true) {
        // No stored recording consent: open the app so the user sees the
        // consent dialog instead of silently recording other people.
        let _ = app.emit("shortcut-needs-consent", mode);
        if let Some(main) = app.get_webview_window("main") {
            let _ = main.show();
            let _ = main.set_focus();
        }
        return Err("Recording consent is needed first. The Wicx window is open.".to_string());
    }
    let mut params = Map::new();
    params.insert("mode".into(), json!(mode));
    params.insert("consent_acknowledged".into(), json!(true));
    send_daemon("start_session", params)
}

pub fn init(app: &AppHandle) -> tauri::Result<()> {
    register_all(app);
    Ok(())
}

pub fn handle_press(app: &AppHandle, id: &Shortcut) {
    let store = load_store();
    for action in ACTIONS {
        let (_, accelerator) = binding(&store, action);
        let Some(accelerator) = accelerator else {
            continue;
        };
        let matches = Shortcut::from_str(&accelerator)
            .map(|parsed| parsed.id() == id.id())
            .unwrap_or(false);
        if matches {
            fire_action(app, action);
            return;
        }
    }
}

#[tauri::command]
pub fn get_shortcuts(app: AppHandle) -> Result<Value, String> {
    let store = load_store();
    let errors = app.try_state::<ShortcutState_>();
    let mut bindings = Map::new();
    for action in ACTIONS {
        let (display, accelerator) = binding(&store, action);
        bindings.insert(
            (*action).to_string(),
            json!({
                "display": display,
                "accelerator": accelerator,
                "register_error": errors.as_ref().and_then(|s| s.error(action)),
            }),
        );
    }
    Ok(json!({
        "bindings": bindings,
        "long_consent": store.get("long_consent").and_then(Value::as_bool).unwrap_or(false),
        "setup_completed": store.get("setup_completed").and_then(Value::as_bool).unwrap_or(false),
    }))
}

#[tauri::command]
pub fn set_shortcut(
    app: AppHandle,
    action: String,
    display: String,
    accelerator: Option<String>,
) -> Result<Value, String> {
    if !ACTIONS.contains(&action.as_str()) {
        return Err("Unknown shortcut action.".to_string());
    }
    let mut store = load_store();
    match accelerator {
        Some(text) if !text.trim().is_empty() => {
            validate_accelerator(&text)?;
            if let Some(bindings) = store.get_mut("bindings").and_then(Value::as_object_mut) {
                bindings.insert(
                    action.clone(),
                    json!({"display": display, "accelerator": text.trim()}),
                );
            }
        }
        _ => {
            if let Some(bindings) = store.get_mut("bindings").and_then(Value::as_object_mut) {
                bindings.insert(action.clone(), json!({"display": "", "accelerator": null}));
            }
        }
    }
    save_store(&store)?;
    register_all(&app);
    get_shortcuts(app)
}

#[tauri::command]
pub fn set_long_consent(consented: bool) -> Result<Value, String> {
    let mut store = load_store();
    if let Some(map) = store.as_object_mut() {
        map.insert("long_consent".into(), json!(consented));
    }
    save_store(&store)?;
    Ok(json!({"long_consent": consented}))
}

#[tauri::command]
pub fn complete_setup() -> Result<Value, String> {
    let mut store = load_store();
    if let Some(map) = store.as_object_mut() {
        map.insert("setup_completed".into(), json!(true));
    }
    save_store(&store)?;
    Ok(json!({"setup_completed": true}))
}

#[cfg(test)]
mod tests {
    use super::validate_accelerator;

    #[test]
    fn bare_system_modifiers_are_rejected() {
        for banned in ["Shift", "Control", "Super", "CTRL", "meta"] {
            assert!(
                validate_accelerator(banned).is_err(),
                "{banned} must be rejected"
            );
        }
    }

    #[test]
    fn single_typing_characters_are_rejected() {
        for key in ["a", "M", "5"] {
            assert!(validate_accelerator(key).is_err(), "{key} must be rejected");
        }
    }

    #[test]
    fn alt_default_combos_and_function_keys_pass() {
        for good in ["Alt+Space", "Alt+KeyM", "Control+Shift+Space", "F9"] {
            assert!(
                validate_accelerator(good).is_ok(),
                "{good} must be accepted"
            );
        }
    }

    #[test]
    fn bare_alt_alone_is_rejected_with_guidance() {
        let error = validate_accelerator("Alt").unwrap_err();
        assert!(error.contains("Alt+Space"), "unexpected message: {error}");
    }

    #[test]
    fn empty_and_garbage_are_rejected() {
        assert!(validate_accelerator("").is_err());
        assert!(validate_accelerator("   ").is_err());
        assert!(validate_accelerator("Not+A+Key+At+All+X1").is_err());
    }
}
