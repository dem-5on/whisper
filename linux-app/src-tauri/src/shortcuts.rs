//! Global keyboard shortcuts for the Linux desktop app.
//!
//! Three mode bindings (Dictation, Meeting, Lecture) are stored in
//! `$XDG_CONFIG_HOME/wicx/shortcuts.json`. Combinations are registered
//! through `tauri-plugin-global-shortcut`; registration is best-effort on
//! Wayland and per-action refusal is reported with the desktop-environment
//! custom-shortcut fallback (`wicx toggle`, `wicx start-session`, ...).
//!
//! A bare Right Alt cannot be expressed in any hotkey API, so Dictation's
//! default is monitored directly at the evdev layer instead: a RightAlt
//! press followed by release with no other key in between toggles
//! Dictation. Monitoring is read-only (never grabs the device, never logs
//! keys) and needs read access to `/dev/input/event*` (the `input` group,
//! same privilege class as the existing `/dev/uinput` requirement).

use std::{
    collections::{HashMap, HashSet},
    fs,
    path::PathBuf,
    str::FromStr,
    sync::Mutex,
};

use evdev::{Device, EventType, KeyCode};

use serde_json::{json, Map, Value};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

use crate::send_daemon;

pub const ACTIONS: &[&str] = &["dictation", "meeting", "lecture"];

pub struct ShortcutState_ {
    errors: Mutex<HashMap<String, String>>,
    monitored: Mutex<HashSet<PathBuf>>,
    monitor_error: Mutex<Option<String>>,
    test_mode: Mutex<bool>,
}

impl ShortcutState_ {
    pub fn new() -> Self {
        Self {
            errors: Mutex::new(HashMap::new()),
            monitored: Mutex::new(HashSet::new()),
            monitor_error: Mutex::new(None),
            test_mode: Mutex::new(false),
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
        "role": "",
        "bindings": {
            "dictation": {"display": "Right Alt", "accelerator": "RightAlt"},
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
        if let Some(role) = parsed.get("role").and_then(Value::as_str) {
            map.insert("role".into(), json!(role));
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
/// `RightAlt` is the bare Right Alt tap (evdev-monitored) and is only valid
/// for Dictation. Combinations go through the hotkey API. Bare Left
/// Ctrl/Shift/Super/Alt and single typing characters are refused because
/// they would break normal use.
pub fn validate_accelerator(accelerator: &str, action: &str) -> Result<(), String> {
    let text = accelerator.trim();
    if text.is_empty() {
        return Err("Press a key combination first.".to_string());
    }
    if text == "RightAlt" {
        if action == "dictation" {
            return Ok(());
        }
        return Err(
            "Right Alt alone is reserved for Dictation. Pick a combination for this mode."
                .to_string(),
        );
    }
    let upper = text.to_uppercase();
    if upper == "ALT" {
        return Err("“Alt” alone cannot be grabbed by apps. Use Alt+Space or a combination — or bind Right Alt in your desktop settings.".to_string());
    }
    let mut seen = HashSet::new();
    for part in text.split('+') {
        let key = part.trim().to_uppercase();
        if !seen.insert(key.clone()) {
            return Err(format!("“{text}” repeats {key}. Press each key once."));
        }
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
        if accelerator == "RightAlt" {
            // Bare Right Alt is monitored at the evdev layer, not the
            // hotkey API. Its status is reported through ensure_monitor.
            shortcuts.clear_error(action);
            continue;
        }
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

fn test_mode_on(app: &AppHandle) -> bool {
    app.try_state::<ShortcutState_>()
        .and_then(|s| s.test_mode.lock().ok().map(|flag| *flag))
        .unwrap_or(false)
}

fn fire_action(app: &AppHandle, action: &str) {
    if test_mode_on(app) {
        // Setup key test: light the UI key without recording anything.
        let _ = app.emit("shortcut-test-press", action);
        return;
    }
    let result = match action {
        "dictation" => send_daemon("toggle", Map::new()),
        "meeting" | "lecture" => toggle_long_session(app, action),
        _ => Err("Unknown shortcut action.".to_string()),
    };
    if let Err(error) = result {
        eprintln!("wicx shortcut '{action}' failed: {error}");
    }
}

/// Tracks a lone Right Alt tap: press arms, any other key press disarms,
/// release fires only when nothing else was pressed in between. Pure logic
/// so it can be unit-tested without hardware.
#[derive(Default)]
struct TapTracker {
    right_alt_down: bool,
    other_pressed: bool,
}

impl TapTracker {
    /// Feed one key event. `value` follows evdev (1 press, 2 repeat, 0 release).
    /// Returns true exactly when a lone tap completed and should fire.
    fn key_event(&mut self, is_right_alt: bool, value: i32) -> bool {
        match (is_right_alt, value) {
            (true, 1) => {
                // Arm without clearing other_pressed: a key already held
                // down before this press still disarms the tap.
                self.right_alt_down = true;
                false
            }
            (true, 2) => false,
            (true, _) => {
                let fire = self.right_alt_down && !self.other_pressed;
                self.right_alt_down = false;
                self.other_pressed = false;
                fire
            }
            (false, 1) | (false, 2) => {
                self.other_pressed = true;
                false
            }
            (false, _) => false,
        }
    }
}

const RIGHT_ALT_CODE: u16 = 100;

fn has_right_alt(device: &Device) -> bool {
    device
        .supported_keys()
        .map(|keys| keys.contains(KeyCode::KEY_RIGHTALT))
        .unwrap_or(false)
}

fn monitor_device(app: AppHandle, path: PathBuf) {
    std::thread::spawn(move || {
        let mut device = match Device::open(&path) {
            Ok(device) => device,
            Err(error) => {
                eprintln!("wicx: cannot monitor {}: {error}", path.display());
                return;
            }
        };
        let mut tracker = TapTracker::default();
        loop {
            let events = match device.fetch_events() {
                Ok(events) => events,
                Err(error) => {
                    eprintln!("wicx: key monitor on {} ended: {error}", path.display());
                    return;
                }
            };
            for event in events {
                if event.event_type() != EventType::KEY {
                    continue;
                }
                if tracker.key_event(event.code() == RIGHT_ALT_CODE, event.value()) {
                    eprintln!("wicx: right-alt tap detected");
                    // Re-read the binding at fire time: threads are spawned
                    // once per device and must follow later rebinds.
                    let (_, accelerator) = binding(&load_store(), "dictation");
                    if accelerator.as_deref() == Some("RightAlt") {
                        fire_action(&app, "dictation");
                    }
                }
            }
        }
    });
}

/// Ensure keyboard devices are monitored for the Right Alt tap. Safe to call
/// repeatedly: already-monitored devices are skipped, and firing always
/// follows the current stored binding.
fn ensure_monitor(app: &AppHandle) {
    let mut probed = 0;
    let mut fresh_paths: Vec<PathBuf> = vec![];
    for (path, device) in evdev::enumerate() {
        probed += 1;
        if !has_right_alt(&device) {
            continue;
        }
        let fresh = match app.try_state::<ShortcutState_>() {
            Some(state) => match state.monitored.lock() {
                Ok(mut set) => set.insert(path.clone()),
                Err(_) => false,
            },
            None => return,
        };
        if fresh {
            fresh_paths.push(path);
        }
    }
    // enumerate() silently skips devices it cannot open, so probe access
    // directly to tell "no keyboards" apart from "no permission".
    let access_error = if probed == 0 {
        match std::fs::read_dir("/dev/input") {
            Ok(_) => None,
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => Some(
                "Wicx cannot read keyboard devices (need the “input” group — sign out and back in after install, then reopen setup). The desktop-settings fallback below still works."
                    .to_string(),
            ),
            Err(_) => None,
        }
    } else {
        None
    };
    match app.try_state::<ShortcutState_>() {
        Some(state) => match state.monitor_error.lock() {
            Ok(mut slot) => {
                *slot = access_error;
            }
            Err(_) => {}
        },
        None => {}
    }
    for path in fresh_paths {
        monitor_device(app.clone(), path);
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
    ensure_monitor(app);
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
    let monitor_error = errors
        .as_ref()
        .and_then(|s| s.monitor_error.lock().ok().and_then(|slot| slot.clone()));
    let mut bindings = Map::new();
    for action in ACTIONS {
        let (display, accelerator) = binding(&store, action);
        let mut register_error = errors.as_ref().and_then(|s| s.error(action));
        if *action == "dictation" && register_error.is_none() {
            register_error = monitor_error.clone();
        }
        bindings.insert(
            (*action).to_string(),
            json!({
                "display": display,
                "accelerator": accelerator,
                "register_error": register_error,
            }),
        );
    }
    Ok(json!({
        "bindings": bindings,
        "long_consent": store.get("long_consent").and_then(Value::as_bool).unwrap_or(false),
        "setup_completed": store.get("setup_completed").and_then(Value::as_bool).unwrap_or(false),
        "role": store.get("role").and_then(Value::as_str).unwrap_or(""),
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
            validate_accelerator(&text, &action)?;
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
    ensure_monitor(&app);
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

fn starter_pack(role: &str) -> &'static [&'static str] {
    match role {
        "student" => &["syllabus", "midterm", "thesis", "campus", "GPA"],
        "engineering" => &["API", "GitHub", "TypeScript", "deploy", "dataset"],
        "writer" => &["manuscript", "chapter", "editor", "deadline", "draft"],
        "healthcare" => &["diagnosis", "prescription", "dosage", "referral", "symptoms"],
        "legal" => &["contract", "clause", "plaintiff", "settlement", "affidavit"],
        "finance" => &["invoice", "ledger", "dividend", "forecast", "amortization"],
        "educator" => &["curriculum", "assignment", "rubric", "enrollment", "classroom"],
        "marketing" => &["campaign", "SEO", "conversion", "brand", "analytics"],
        "sales" => &["pipeline", "quota", "prospect", "CRM", "discount"],
        "creator" => &["thumbnail", "upload", "subscribers", "episode", "sponsor"],
        _ => &[],
    }
}

#[tauri::command]
pub fn set_role(role: String) -> Result<Value, String> {
    let role = role.trim().to_lowercase();
    if role.is_empty() || role.len() > 40 {
        return Err("Pick a role to continue.".to_string());
    }
    let mut store = load_store();
    if let Some(map) = store.as_object_mut() {
        map.insert("role".into(), json!(role));
    }
    save_store(&store)?;
    let mut seeded = 0;
    for term in starter_pack(&role) {
        let mut params = Map::new();
        params.insert("action".into(), json!("add"));
        params.insert("term".into(), json!(term));
        if send_daemon("dictionary", params).is_ok() {
            seeded += 1;
        }
    }
    Ok(json!({"role": role, "seeded": seeded}))
}

#[tauri::command]
pub fn set_shortcut_test(app: AppHandle, active: bool) -> Result<Value, String> {
    if let Some(state) = app.try_state::<ShortcutState_>() {
        if let Ok(mut flag) = state.test_mode.lock() {
            *flag = active;
        }
    }
    Ok(json!({"shortcut_test": active}))
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
    use super::{validate_accelerator, TapTracker};

    #[test]
    fn bare_system_modifiers_are_rejected() {
        for banned in ["Shift", "Control", "Super", "CTRL", "meta"] {
            assert!(
                validate_accelerator(banned, "dictation").is_err(),
                "{banned} must be rejected"
            );
        }
    }

    #[test]
    fn single_typing_characters_are_rejected() {
        for key in ["a", "M", "5"] {
            assert!(
                validate_accelerator(key, "dictation").is_err(),
                "{key} must be rejected"
            );
        }
    }

    #[test]
    fn alt_default_combos_and_function_keys_pass() {
        for good in ["Alt+Space", "Alt+KeyM", "Control+Shift+Space", "F9"] {
            assert!(
                validate_accelerator(good, "dictation").is_ok(),
                "{good} must be accepted"
            );
        }
    }

    #[test]
    fn bare_alt_alone_is_rejected_with_guidance() {
        let error = validate_accelerator("Alt", "dictation").unwrap_err();
        assert!(error.contains("Alt+Space"), "unexpected message: {error}");
    }

    #[test]
    fn repeated_modifier_is_rejected() {
        let error = validate_accelerator("Alt+Alt+KeyM", "meeting").unwrap_err();
        assert!(error.contains("repeats"), "unexpected message: {error}");
    }

    #[test]
    fn empty_and_garbage_are_rejected() {
        assert!(validate_accelerator("", "dictation").is_err());
        assert!(validate_accelerator("   ", "dictation").is_err());
        assert!(validate_accelerator("Not+A+Key+At+All+X1", "dictation").is_err());
    }

    #[test]
    fn right_alt_tap_is_dictation_only() {
        assert!(validate_accelerator("RightAlt", "dictation").is_ok());
        assert!(validate_accelerator("RightAlt", "meeting").is_err());
        assert!(validate_accelerator("RightAlt", "lecture").is_err());
    }

    #[test]
    fn lone_right_alt_tap_fires() {
        let mut tracker = TapTracker::default();
        assert!(!tracker.key_event(true, 1));
        assert!(tracker.key_event(true, 0));
    }

    #[test]
    fn altgr_plus_key_does_not_fire() {
        let mut tracker = TapTracker::default();
        assert!(!tracker.key_event(true, 1));
        assert!(!tracker.key_event(false, 1));
        assert!(!tracker.key_event(false, 0));
        assert!(!tracker.key_event(true, 0));
    }

    #[test]
    fn held_key_repeat_does_not_disarm_before_release() {
        let mut tracker = TapTracker::default();
        assert!(!tracker.key_event(true, 1));
        assert!(!tracker.key_event(true, 2));
        assert!(tracker.key_event(true, 0));
    }

    #[test]
    fn other_key_first_disarms_the_tap() {
        let mut tracker = TapTracker::default();
        assert!(!tracker.key_event(false, 1));
        assert!(!tracker.key_event(false, 0));
        assert!(!tracker.key_event(true, 1));
        assert!(!tracker.key_event(true, 0));
    }

    #[test]
    fn tracker_recovers_for_the_next_tap() {
        let mut tracker = TapTracker::default();
        assert!(!tracker.key_event(true, 1));
        assert!(!tracker.key_event(false, 1));
        assert!(!tracker.key_event(true, 0));
        assert!(!tracker.key_event(true, 1));
        assert!(tracker.key_event(true, 0));
    }
}
