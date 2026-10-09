#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(feature = "packaged-daemon")]
mod daemon;

#[cfg(all(feature = "focus-test", feature = "packaged-daemon"))]
compile_error!("focus-test builds must not start the packaged transcription daemon");

use std::{
    fs,
    path::Path,
    sync::{Arc, Mutex},
};

use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    webview::WebviewWindow,
    Emitter, LogicalSize, Manager, PhysicalPosition, RunEvent, State,
};

const PANEL_WIDTH: i32 = 380;
const PANEL_HEIGHT: i32 = 530;
const EXPANDED_PANEL_HEIGHT: f64 = 760.0;
const SCREEN_GAP: i32 = 8;
const UNINSTALL_ARGUMENT: &str = "--whisper-uninstall";

#[derive(Clone, Default)]
struct PanelAnchor(Arc<Mutex<(i32, i32)>>);

#[derive(Clone, Default)]
struct FocusBaseline(Arc<Mutex<Option<isize>>>);

#[derive(Clone)]
struct ShortcutStatus(String);

#[derive(Clone)]
struct WebsocketToken(String);

fn mic_tray_icon(color: [u8; 4]) -> Image<'static> {
    let size = 32_u32;
    let mut rgba = vec![0_u8; (size * size * 4) as usize];
    for y in 0..size {
        for x in 0..size {
            let dx = x as i32 - 16;
            let dy = y as i32 - 16;
            if dx * dx + dy * dy <= 225 {
                let index = ((y * size + x) * 4) as usize;
                rgba[index..index + 4].copy_from_slice(&color);
                let mic = (13..=18).contains(&x) && (8..=19).contains(&y);
                let cradle = (10..=21).contains(&x)
                    && (19..=23).contains(&y)
                    && ((y == 19 && (11..=20).contains(&x))
                        || (y == 20 && (10..=21).contains(&x))
                        || (y == 21 && (11..=20).contains(&x))
                        || (y == 22 && (13..=18).contains(&x))
                        || (y == 23 && (15..=16).contains(&x)));
                if mic || cradle {
                    rgba[index..index + 4].copy_from_slice(&[255, 255, 255, 255]);
                }
            }
        }
    }
    Image::new_owned(rgba, size, size)
}

#[tauri::command]
fn set_tray_state(tray: State<'_, TrayIcon>, state: String) -> Result<(), String> {
    let color = match state.as_str() {
        "recording" | "error" => [232, 72, 72, 255],
        "processing" => [40, 120, 240, 255],
        _ => [132, 135, 145, 255],
    };
    tray.set_icon(Some(mic_tray_icon(color)))
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn focus_test_build() -> bool {
    cfg!(feature = "focus-test")
}

#[tauri::command]
fn recording_shortcut_status(status: State<'_, ShortcutStatus>) -> String {
    status.0.clone()
}

#[tauri::command]
fn websocket_token(token: State<'_, WebsocketToken>) -> String {
    token.0.clone()
}

fn load_or_create_websocket_token(path: &Path) -> Result<String, String> {
    match fs::read_to_string(path) {
        Ok(value) => {
            if let Ok(token) = uuid::Uuid::parse_str(value.trim()) {
                return Ok(token.to_string());
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!(
                "Could not read Whisper's local connection token: {error}"
            ));
        }
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!("Could not prepare Whisper's local settings folder: {error}")
        })?;
    }
    let token = uuid::Uuid::new_v4().to_string();
    fs::write(path, &token)
        .map_err(|error| format!("Could not save Whisper's local connection token: {error}"))?;
    Ok(token)
}

#[tauri::command]
fn show_key_settings(app: tauri::AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("keys")
        .ok_or_else(|| "Whisper key settings are unavailable".to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

#[tauri::command]
fn open_diagnostics_folder() -> Result<(), String> {
    #[cfg(windows)]
    {
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .ok_or_else(|| "Windows did not provide the local app-data folder".to_string())?;
        let log_directory = Path::new(&local_app_data).join("Whisper").join("logs");
        fs::create_dir_all(&log_directory)
            .map_err(|error| format!("Could not create Whisper's diagnostics folder: {error}"))?;
        std::process::Command::new("explorer.exe")
            .arg(log_directory)
            .spawn()
            .map_err(|error| format!("Could not open Whisper's diagnostics folder: {error}"))?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Err("Whisper's diagnostics folder is only available on Windows".to_string())
    }
}

#[cfg(windows)]
fn prepare_non_activating_window(window: &WebviewWindow) -> Result<(), Box<dyn std::error::Error>> {
    use windows::Win32::UI::WindowsAndMessaging::{
        GetWindowLongPtrW, SetWindowLongPtrW, SetWindowPos, GWL_EXSTYLE, HWND_TOPMOST,
        SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, WS_EX_NOACTIVATE,
        WS_EX_TOOLWINDOW,
    };

    let hwnd = window.hwnd()?;
    let required = WS_EX_NOACTIVATE.0 as isize | WS_EX_TOOLWINDOW.0 as isize;
    unsafe {
        let current = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, current | required);
    }
    window.set_focusable(false)?;
    unsafe {
        SetWindowPos(
            hwnd,
            Some(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE,
        )
        .ok()
        .ok_or_else(std::io::Error::last_os_error)?;
    }
    let actual = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    if actual & required != required {
        return Err(
            std::io::Error::other("Windows did not apply the non-activating panel styles").into(),
        );
    }
    Ok(())
}

#[cfg(not(windows))]
fn prepare_non_activating_window(
    _window: &WebviewWindow,
) -> Result<(), Box<dyn std::error::Error>> {
    Ok(())
}

#[cfg(windows)]
fn apply_panel_backdrop(window: &WebviewWindow) {
    if window_vibrancy::apply_mica(window, Some(true)).is_err() {
        // Mica is Windows 11-only. Use the supported blur backdrop on older
        // Windows versions instead of failing to open the panel.
        let _ = window_vibrancy::apply_blur(window, Some((32, 33, 39, 230)));
    }
}

#[cfg(not(windows))]
fn apply_panel_backdrop(_window: &WebviewWindow) {}

fn panel_position(
    monitor: (i32, i32, i32, i32),
    tray: (i32, i32),
    panel: (i32, i32),
    gap: i32,
) -> (i32, i32) {
    let (origin_x, origin_y, monitor_width, monitor_height) = monitor;
    let (tray_x, tray_y) = tray;
    let (panel_width, panel_height) = panel;
    let min_x = origin_x + gap;
    let min_y = origin_y + gap;
    let max_x = (origin_x + monitor_width - panel_width - gap).max(min_x);
    let max_y = (origin_y + monitor_height - panel_height - gap).max(min_y);
    (
        (tray_x - panel_width / 2).clamp(min_x, max_x),
        (tray_y - panel_height - gap).clamp(min_y, max_y),
    )
}

fn panel_height_for_monitor(requested: f64, monitor_height: u32, scale: f64, gap: i32) -> f64 {
    let scale = if scale.is_finite() && scale > 0.0 {
        scale
    } else {
        1.0
    };
    let usable_height = (monitor_height as f64 - 2.0 * gap as f64) / scale;
    requested.min(usable_height.max(1.0))
}

fn show_panel(
    window: &WebviewWindow,
    baseline: &FocusBaseline,
    tray_x: i32,
    tray_y: i32,
    panel_height: f64,
) {
    let foreground_before = foreground_window();
    if let Ok(mut saved) = baseline.0.lock() {
        *saved = foreground_before;
    }
    let Ok(monitors) = window.available_monitors() else {
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, panel_height));
        present_panel(
            window,
            tray_x - PANEL_WIDTH / 2,
            tray_y - panel_height.round() as i32 - SCREEN_GAP,
        );
        emit_focus_check(window, foreground_before);
        let _ = window.emit("panel-shown", ());
        return;
    };
    let monitor = monitors.iter().find(|monitor| {
        let position = monitor.position();
        let size = monitor.size();
        tray_x >= position.x
            && tray_x < position.x + size.width as i32
            && tray_y >= position.y
            && tray_y < position.y + size.height as i32
    });

    if let Some(monitor) = monitor {
        let origin = monitor.position();
        let size = monitor.size();
        let scale = monitor.scale_factor();
        let width = (PANEL_WIDTH as f64 * scale).round() as i32;
        let logical_height = panel_height_for_monitor(panel_height, size.height, scale, SCREEN_GAP);
        let height = (logical_height * scale).round() as i32;
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, logical_height));
        let (x, y) = panel_position(
            (origin.x, origin.y, size.width as i32, size.height as i32),
            (tray_x, tray_y),
            (width, height),
            SCREEN_GAP,
        );
        present_panel(window, x, y);
    } else {
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, panel_height));
        present_panel(
            window,
            tray_x - PANEL_WIDTH / 2,
            tray_y - panel_height.round() as i32 - SCREEN_GAP,
        );
    }
    emit_focus_check(window, foreground_before);
}

#[cfg(windows)]
fn monitor_foreground(window: WebviewWindow, baseline: FocusBaseline) {
    std::thread::spawn(move || {
        let mut escape_was_down = false;
        loop {
            std::thread::sleep(std::time::Duration::from_millis(100));
            if !window.is_visible().unwrap_or(false) {
                escape_was_down = false;
                continue;
            }

            let escape_down = escape_is_down();
            let escape_pressed = escape_down && !escape_was_down;
            escape_was_down = escape_down;

            let expected = baseline.0.lock().ok().and_then(|saved| *saved);
            if escape_pressed || (expected.is_some() && foreground_window() != expected) {
                let _ = window.hide();
                if let Ok(mut saved) = baseline.0.lock() {
                    *saved = None;
                }
            }
        }
    });
}

#[cfg(not(windows))]
fn monitor_foreground(_window: WebviewWindow, _baseline: FocusBaseline) {}

#[cfg(windows)]
fn escape_is_down() -> bool {
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_ESCAPE};

    unsafe { GetAsyncKeyState(VK_ESCAPE.0 as i32) < 0 }
}

#[cfg(windows)]
fn foreground_window() -> Option<isize> {
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

    let handle = unsafe { GetForegroundWindow() };
    let raw = handle.0 as isize;
    (raw != 0).then_some(raw)
}

#[cfg(not(windows))]
fn foreground_window() -> Option<isize> {
    None
}

fn emit_focus_check(window: &WebviewWindow, foreground_before: Option<isize>) {
    #[cfg(windows)]
    {
        let window = window.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(200));
            let visible = window.is_visible().unwrap_or(false);
            let preserved =
                visible && foreground_before.is_some() && foreground_before == foreground_window();
            let _ = window.emit("focus-check", preserved);
        });
    }
    #[cfg(not(windows))]
    {
        let _ = window.emit("focus-check", false);
    }
}

#[tauri::command]
fn resize_panel(
    app: tauri::AppHandle,
    anchor: State<'_, PanelAnchor>,
    expanded: bool,
) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Whisper panel is unavailable".to_string())?;
    let (tray_x, tray_y) = *anchor
        .0
        .lock()
        .map_err(|_| "Whisper panel position is unavailable".to_string())?;
    let height = if expanded {
        EXPANDED_PANEL_HEIGHT
    } else {
        PANEL_HEIGHT as f64
    };
    let baseline = app.state::<FocusBaseline>();
    show_panel(&window, &baseline, tray_x, tray_y, height);
    Ok(())
}

#[cfg(windows)]
fn present_panel(window: &WebviewWindow, x: i32, y: i32) {
    use windows::Win32::UI::WindowsAndMessaging::{
        SetWindowPos, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOSIZE, SWP_SHOWWINDOW,
    };

    let _ = window.set_position(PhysicalPosition::new(x, y));
    if let Ok(hwnd) = window.hwnd() {
        unsafe {
            let _ = SetWindowPos(
                hwnd,
                Some(HWND_TOPMOST),
                x,
                y,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW,
            );
        }
    }
}

#[cfg(not(windows))]
fn present_panel(window: &WebviewWindow, x: i32, y: i32) {
    let _ = window.set_position(PhysicalPosition::new(x, y));
    let _ = window.show();
}

#[cfg(windows)]
fn cursor_position() -> Option<(i32, i32)> {
    use windows::{Win32::Foundation::POINT, Win32::UI::WindowsAndMessaging::GetCursorPos};

    let mut point = POINT::default();
    unsafe { GetCursorPos(&mut point).ok()? };
    Some((point.x, point.y))
}

#[cfg(not(windows))]
fn cursor_position() -> Option<(i32, i32)> {
    None
}

fn toggle_panel(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
        return;
    }

    let (x, y) = cursor_position().unwrap_or((PANEL_WIDTH, PANEL_HEIGHT));
    if let Ok(mut anchor) = app.state::<PanelAnchor>().0.lock() {
        *anchor = (x, y);
    }
    let baseline = app.state::<FocusBaseline>();
    show_panel(&window, &baseline, x, y, PANEL_HEIGHT as f64);
    let _ = window.emit("panel-shown", ());
}

fn main() {
    #[cfg(feature = "packaged-daemon")]
    let builder = {
        use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};

        let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyR);
        let handler_shortcut = shortcut.clone();
        tauri::Builder::default()
            .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
                if args.iter().any(|argument| argument == UNINSTALL_ARGUMENT) {
                    app.exit(0);
                } else {
                    toggle_panel(app);
                }
            }))
            .plugin(tauri_plugin_shell::init())
            .plugin(tauri_plugin_clipboard_manager::init())
            .plugin(
                tauri_plugin_global_shortcut::Builder::new()
                    .with_handler(move |app, pressed, event| {
                        if pressed == &handler_shortcut && event.state() == ShortcutState::Pressed {
                            let _ = app.emit("recording-hotkey", ());
                        }
                    })
                    .build(),
            )
    };
    #[cfg(not(feature = "packaged-daemon"))]
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_clipboard_manager::init());
    builder
        .setup(move |app| {
            if std::env::args().any(|argument| argument == UNINSTALL_ARGUMENT) {
                // Uninstall helper (NSIS PREUNINSTALL ExecWait). Exit
                // immediately without initializing windows, tray, or daemon so
                // the uninstaller never waits on a locked executable. The
                // resident instance (if any) is signaled separately via the
                // single-instance plugin in packaged builds, or force-closed
                // by the smoke test in focus-test builds.
                std::process::exit(0);
            }
            let window = app
                .get_webview_window("main")
                .expect("main panel window is configured");
            let token_path = app
                .path()
                .app_data_dir()
                .map_err(std::io::Error::other)?
                .join("ui-token");
            let websocket_token =
                load_or_create_websocket_token(&token_path).map_err(std::io::Error::other)?;
            app.manage(WebsocketToken(websocket_token.clone()));
            prepare_non_activating_window(&window)?;
            apply_panel_backdrop(&window);
            if let Some(key_window) = app.get_webview_window("keys") {
                let close_target = key_window.clone();
                key_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = close_target.hide();
                    }
                });
            }
            app.manage(PanelAnchor::default());
            app.manage(FocusBaseline::default());
            #[cfg(feature = "packaged-daemon")]
            let shortcut_status = {
                use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

                let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyR);
                if app.global_shortcut().register(shortcut).is_ok() {
                    "available"
                } else {
                    "unavailable"
                }
            };
            #[cfg(not(feature = "packaged-daemon"))]
            let shortcut_status = if cfg!(feature = "focus-test") {
                "focus-test"
            } else {
                "development"
            };
            app.manage(ShortcutStatus(shortcut_status.to_string()));
            let focus_baseline = app.state::<FocusBaseline>().inner().clone();
            monitor_foreground(window.clone(), focus_baseline.clone());
            #[cfg(feature = "packaged-daemon")]
            {
                let supervisor = daemon::DaemonSupervisor::default();
                app.manage(supervisor.clone());
                daemon::supervise(app.handle().clone(), supervisor, websocket_token);
            }
            let tray_anchor = app.state::<PanelAnchor>().inner().clone();
            let recording_enabled = !cfg!(feature = "focus-test");

            let toggle = MenuItem::with_id(
                app,
                "toggle",
                "Start / stop recording · Ctrl+Alt+R",
                recording_enabled,
                None::<&str>,
            )?;
            let cancel = MenuItem::with_id(
                app,
                "cancel",
                "Cancel recording",
                recording_enabled,
                None::<&str>,
            )?;
            let open = MenuItem::with_id(app, "open", "Show / hide Whisper", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Whisper", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&toggle, &cancel, &open, &quit])?;
            let tray = TrayIconBuilder::with_id("whisper")
                .icon(mic_tray_icon([132, 135, 145, 255]))
                .tooltip("Whisper")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(move |_tray, event| {
                    if let TrayIconEvent::Click {
                        position,
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if window.is_visible().unwrap_or(false) {
                            let _ = window.hide();
                        } else {
                            let tray_x = position.x as i32;
                            let tray_y = position.y as i32;
                            if let Ok(mut anchor) = tray_anchor.0.lock() {
                                *anchor = (tray_x, tray_y);
                            }
                            show_panel(
                                &window,
                                &focus_baseline,
                                tray_x,
                                tray_y,
                                PANEL_HEIGHT as f64,
                            );
                            let _ = window.emit("panel-shown", ());
                        }
                    }
                })
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "toggle" => {
                        let _ = app.emit("recording-hotkey", ());
                    }
                    "cancel" => {
                        let _ = app.emit("cancel-recording", ());
                    }
                    "open" => toggle_panel(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            app.manage(tray);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            resize_panel,
            set_tray_state,
            focus_test_build,
            recording_shortcut_status,
            websocket_token,
            show_key_settings,
            open_diagnostics_folder
        ])
        .build(tauri::generate_context!())
        .expect("Whisper tray UI failed to start")
        .run(|app, event| {
            #[cfg(feature = "packaged-daemon")]
            if matches!(event, RunEvent::Exit) {
                app.state::<daemon::DaemonSupervisor>().request_shutdown();
            }
        })
}

#[cfg(test)]
mod tests {
    use super::{load_or_create_websocket_token, panel_height_for_monitor, panel_position};
    use std::{fs, path::PathBuf};

    #[test]
    fn positions_panel_with_negative_monitor_origin_and_top_edge_clamping() {
        assert_eq!(
            panel_position((-1920, 0, 1920, 1080), (-10, 60), (570, 800), 8),
            (-578, 8)
        );
    }

    #[test]
    fn anchors_panel_above_tray_and_keeps_it_inside_right_monitor_edge() {
        assert_eq!(
            panel_position((0, 0, 1920, 1080), (1800, 900), (380, 530), 8),
            (1532, 362)
        );
    }

    #[test]
    fn expanded_panel_height_respects_scaled_monitor_bounds() {
        let height = panel_height_for_monitor(760.0, 1080, 1.5, 8);
        assert!((height - (1064.0 / 1.5)).abs() < 0.01);
    }

    #[test]
    fn expanded_panel_keeps_requested_height_when_it_fits() {
        assert_eq!(panel_height_for_monitor(760.0, 1440, 1.0, 8), 760.0);
    }

    #[test]
    fn websocket_token_is_reused_when_the_ui_shell_restarts() {
        let path = PathBuf::from(std::env::temp_dir())
            .join(format!("whisper-token-test-{}.txt", uuid::Uuid::new_v4()));
        let first = load_or_create_websocket_token(&path).expect("first token should be created");
        let second = load_or_create_websocket_token(&path).expect("token should be loaded again");
        assert_eq!(first, second);
        assert_eq!(uuid::Uuid::parse_str(&first).unwrap().to_string(), first);
        fs::remove_file(path).unwrap();
    }
}
