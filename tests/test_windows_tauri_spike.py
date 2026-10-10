import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_tauri_focus_spike_starts_hidden_and_non_focusable():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    window = config["app"]["windows"][0]

    assert window["visible"] is False
    assert window["focus"] is False
    assert window["focusable"] is False
    assert window["alwaysOnTop"] is True
    assert window["skipTaskbar"] is True
    assert window["width"] == 380


def test_flyout_keeps_its_fixed_width_and_scrolls_added_vertical_content():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    styles = (ROOT / "windows-app/src/style.css").read_text()

    assert config["app"]["windows"][0]["width"] == 380
    assert "overflow-x: hidden" in styles
    assert "overflow-y: auto" in styles
    assert "overflow-wrap: anywhere" in styles


def test_windows_ui_buttons_have_visible_hover_and_keyboard_focus_states():
    panel_styles = (ROOT / "windows-app/src/style.css").read_text()
    key_styles = (ROOT / "windows-app/src/keys.css").read_text()

    assert "button:not(:disabled):hover" in panel_styles
    assert "button:focus-visible" in panel_styles
    assert "button:not(:disabled):hover" in key_styles
    assert "button:focus-visible, input:focus-visible, select:focus-visible" in key_styles


def test_settings_uses_native_close_control_to_hide_to_tray():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    settings = (ROOT / "windows-app/keys.html").read_text()
    frontend = (ROOT / "windows-app/src/keys.js").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()

    assert config["app"]["windows"][1]["decorations"] is True
    assert 'id="close-keys"' not in settings
    assert "Close this window to keep Whisper running in the tray." in settings
    assert "WindowEvent::CloseRequested" in rust
    assert "api.prevent_close()" in rust
    assert "close_target.hide()" in rust
    assert 'event.key === "Escape"' in frontend


def test_windows_ui_displays_microphone_names_instead_of_only_device_ids():
    source = (ROOT / "windows-app/src/main.js").read_text()
    display = (ROOT / "windows-app/src/display.js").read_text()

    assert 'request("mics").then((response) => updateMicrophoneSources(response.mics))' in source
    assert "microphoneLabel(currentStatus.mic, microphoneSources)" in source
    assert 'String(item.id) === String(mic)' in display
    assert "source?.name || String(mic)" in display


def test_windows_spike_applies_native_no_activation_flags():
    source = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()

    assert "WS_EX_NOACTIVATE" in source
    assert "SWP_NOACTIVATE" in source
    assert "SWP_SHOWWINDOW" in source
    assert "SWP_FRAMECHANGED" in source
    assert "SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE" in source
    assert "GetForegroundWindow" in source
    assert "monitor_foreground" in source
    assert "foreground_window() != expected" in source
    assert "GetAsyncKeyState(VK_ESCAPE.0 as i32)" in source
    assert "Duration::from_millis(200)" in source
    assert "let actual = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) }" in source
    assert "actual & required != required" in source
    assert "let visible = window.is_visible().unwrap_or(false)" in source
    assert "&& foreground_before == foreground_window()" in source
    assert 'window.emit("focus-check", preserved)' in source


def test_tray_panel_positioning_is_extracted_and_covered_by_windows_rust_tests():
    source = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    workflow = (ROOT / ".github/workflows/windows-tauri-ui.yml").read_text()

    assert "fn panel_position(" in source
    assert "let (x, y) = panel_position(" in source
    assert "positions_panel_with_negative_monitor_origin_and_top_edge_clamping" in source
    assert "anchors_panel_above_tray_and_keeps_it_inside_right_monitor_edge" in source
    assert "expanded_panel_height_respects_scaled_monitor_bounds" in source
    assert "expanded_panel_keeps_requested_height_when_it_fits" in source
    assert "panel_height_for_monitor(panel_height, size.height, scale, SCREEN_GAP)" in source
    assert "cargo test --manifest-path windows-app/src-tauri/Cargo.toml --features focus-test" in workflow


def test_windows_panel_uses_mica_with_an_older_windows_fallback():
    manifest = (ROOT / "windows-app/src-tauri/Cargo.toml").read_text()
    source = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    styles = (ROOT / "windows-app/src/style.css").read_text()

    assert 'window-vibrancy = "0.8.1"' in manifest
    assert "window_vibrancy::apply_mica(window, Some(true))" in source
    assert "window_vibrancy::apply_blur(window, Some((32, 33, 39, 230)))" in source
    assert "apply_panel_backdrop(&window)" in source
    assert "background: rgba(32, 33, 39, 0.88)" in styles


def test_focus_spike_reports_result_in_the_panel():
    source = (ROOT / "windows-app/src/main.js").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()

    assert 'listen("focus-check"' in source
    assert "Panel visible · previous app remains active" in source
    assert "Focus check failed" in source
    assert 'id="focus-result"' in markup


def test_focus_test_runbook_checks_mouse_hide_without_activating_the_flyout():
    readme = (ROOT / "windows-app/README.md").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()

    assert 'id="close"' in markup
    assert "Click the panel's **×** button to hide it" in readme
    assert "mouse controls respond without activating the flyout" in readme


def test_focus_test_installer_is_available_without_approving_the_focus_gate():
    workflow = (ROOT / ".github/workflows/windows-focus-test-installer.yml").read_text()
    readme = (ROOT / "windows-app/README.md").read_text()

    assert "workflow_dispatch" in workflow
    assert "focus_test_passed" not in workflow
    assert "--features focus-test --no-sign" in workflow
    assert "--features packaged-daemon" not in workflow
    assert "whisper-windows-focus-test-installer" in workflow
    assert "WaitForExit(90000)" in workflow
    assert "Silent uninstall did not exit within 90 seconds." in workflow
    assert "if: always()" in workflow
    assert "does not start the daemon" in readme


def test_tray_visual_state_tracks_recording_status():
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()

    assert '"recording" | "error" => [232, 72, 72, 255]' in rust
    assert '"processing" => [40, 120, 240, 255]' in rust
    assert "invoke(\"set_tray_state\"" in frontend


def test_packaged_windows_app_owns_a_restarting_daemon_sidecar():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    capability = json.loads((ROOT / "windows-app/src-tauri/capabilities/default.json").read_text())
    supervisor = (ROOT / "windows-app/src-tauri/src/daemon.rs").read_text()
    app = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()
    workflow = (ROOT / ".github/workflows/windows-tauri-ui.yml").read_text()
    sidecar_build = (ROOT / "windows-app/scripts/build-sidecar.ps1").read_text()

    assert config["bundle"]["active"] is True
    assert config["bundle"]["targets"] == ["nsis"]
    assert config["bundle"]["externalBin"] == ["binaries/whisper-daemon"]
    assert config["bundle"]["windows"]["webviewInstallMode"] == {
        "type": "downloadBootstrapper",
        "silent": True,
    }
    assert any(
        permission.get("identifier") == "shell:allow-spawn"
        and permission["allow"][0]["name"] == "binaries/whisper-daemon"
        and permission["allow"][0]["sidecar"] is True
        for permission in capability["permissions"]
        if isinstance(permission, dict)
    )
    assert "CommandEvent::Terminated" in supervisor
    assert 'format!("stopped: {detail}")' in supervisor
    assert 'format!("unavailable: {error}")' in supervisor
    assert "retry_delay = Duration::from_secs(5)" in supervisor
    assert 'listen("daemon-process"' in frontend
    assert "could not start its transcription service" in frontend
    assert "stopped unexpectedly" in frontend
    assert "child.kill()" in supervisor
    assert "status.code == Some(75)" in supervisor
    assert '.args(["--ui-token", websocket_token.as_str()])' in supervisor
    assert 'app_data_dir()' in app
    assert 'load_or_create_websocket_token(&token_path)' in app
    assert 'uuid::Uuid::new_v4().to_string()' in app
    assert 'fn websocket_token(token: State' in app
    shell_permission = next(permission for permission in capability["permissions"] if isinstance(permission, dict))
    assert shell_permission["allow"][0]["args"] == ["--ui-token", {"validator": "^[0-9a-f-]{36}$"}]
    assert "retry_delay = (retry_delay * 2).min(Duration::from_secs(30))" in supervisor
    assert "./windows-app/scripts/build-sidecar.ps1" in workflow
    assert "PyInstaller" in sidecar_build
    assert "@('-m', 'venv', $VenvRoot)" in sidecar_build
    assert "Invoke-Checked $BuildPython" in sidecar_build
    assert "--hidden-import', 'transcriber.platforms.windows.ipc'" in sidecar_build
    assert 'feature = "packaged-daemon"' in app
    assert "--no-bundle --ci --features focus-test" in workflow
    assert 'compile_error!("focus-test builds must not start the packaged transcription daemon")' in app
    assert 'default = []' in (ROOT / "windows-app/src-tauri/Cargo.toml").read_text()


def test_windows_installer_workflow_smoke_tests_install_startup_and_uninstall():
    workflow = (ROOT / ".github/workflows/windows-installer-preview.yml").read_text()

    assert "Smoke-test per-user install and uninstall" in workflow
    assert "Start-Process -FilePath $Installer.FullName -ArgumentList '/S'" in workflow
    assert "$AppPath.StartsWith($env:LOCALAPPDATA" in workflow
    assert "[regex]::Match($AutoStart, '^\"([^\"]+)\"')" in workflow
    assert "Microsoft\\Windows\\CurrentVersion\\Run" in workflow
    assert "Start menu shortcut" in workflow
    assert "Get-Process -Name 'whisper-daemon*'" in workflow
    assert "The installed Whisper daemon sidecar did not launch." in workflow
    assert "Whisper daemon sidecar did not stop with the app." in workflow
    assert "--whisper-uninstall" in workflow
    assert "Start-Process -FilePath $Uninstaller -ArgumentList '/S'" in workflow
    assert "Uninstall left Whisper in the Windows sign-in startup registry." in workflow


def test_windows_release_versions_match_the_python_project_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tauri = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    package = json.loads((ROOT / "windows-app/package.json").read_text())

    version = project["project"]["version"]
    assert tauri["version"] == version
    assert package["version"] == version


def test_tauri_custom_signing_command_is_nested_under_windows():
    bundle = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())["bundle"]

    assert "signCommand" not in bundle
    assert bundle["windows"]["signCommand"]["cmd"] == "powershell.exe"
    assert "%1" in bundle["windows"]["signCommand"]["args"][-1]


def test_retired_powershell_installer_stays_out_of_the_download_path():
    website = (ROOT / "website/download/index.html").read_text()
    readme = " ".join((ROOT / "README.md").read_text().split())
    windows_section = website.split('id="windows-details"', 1)[1].split("</section>", 1)[0]

    assert not (ROOT / "install.ps1").exists()
    assert not (ROOT / "uninstall.ps1").exists()
    assert not (ROOT / "website/download/whisper-installer.ps1").exists()
    assert "install.ps1" not in windows_section.lower()
    assert "whisper-installer.ps1" not in website.lower()
    assert "Windows download temporarily unavailable" not in windows_section
    assert "Install Whisper on Windows" in windows_section
    assert "Download Windows installer" in windows_section
    assert "old PowerShell" in readme
    assert "not the supported Windows installation path" in readme


def test_recording_controls_stay_gated_until_focus_spike_passes():
    source = (ROOT / "windows-app/src/main.js").read_text()
    controls = (ROOT / "windows-app/src/control-state.js").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()

    assert "let focusTestBuild = true" in source
    assert 'invoke("focus_test_build")' in source
    assert 'import { deriveControlState } from "./control-state.js"' in source
    assert "deriveControlState(currentStatus, connected, focusTestBuild)" in source
    assert "deriveControlState(currentStatus, active, focusTestBuild)" in source
    assert 'toggleDisabled: locked || busy' in controls
    assert 'settingsDisabled: locked' in controls
    assert 'document.querySelectorAll(".tile[data-setting]")' in source
    assert 'if (focusTestBuild) return;' in source
    assert 'if (!focusTestBuild) void cancelRecording();' in source
    assert 'let recording_enabled = !cfg!(feature = "focus-test");' in rust
    assert '"toggle",\n                "Start / stop recording · Ctrl+Alt+R",\n                recording_enabled' in rust
    assert 'cfg!(feature = "focus-test")' in rust


def test_focus_diagnostic_copy_is_hidden_in_a_normal_app_build():
    source = (ROOT / "windows-app/src/main.js").read_text()

    assert "ui.hint.hidden = !focusTestBuild" in source
    assert "ui.focusResult.hidden = !focusTestBuild" in source


def test_recording_commands_hide_the_flyout_after_dispatch():
    source = (ROOT / "windows-app/src/main.js").read_text()

    assert source.count('await getCurrentWindow().hide();') == 1
    assert source.index('const pendingRequest = request(command);') < source.index('await getCurrentWindow().hide();')
    assert source.index('await getCurrentWindow().hide();') < source.index('updateStatus(await pendingRequest)')


def test_daemon_disconnect_disables_every_control_that_needs_the_connection():
    source = (ROOT / "windows-app/src/main.js").read_text()
    control_state = (ROOT / "windows-app/src/control-state.js").read_text()

    assert 'import { deriveControlState } from "./control-state.js"' in source
    assert "deriveControlState(currentStatus, connected, focusTestBuild)" in source
    assert "locked || !status?.last_audio || busy" in control_state
    assert "locked || providerBlocksLive || recording || busy" in control_state


def test_copy_transcript_uses_the_native_clipboard_with_feedback():
    manifest = (ROOT / "windows-app/src-tauri/Cargo.toml").read_text()
    capability = json.loads((ROOT / "windows-app/src-tauri/capabilities/default.json").read_text())
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()

    assert 'tauri-plugin-clipboard-manager = "2"' in manifest
    assert "clipboard-manager:allow-write-text" in capability["permissions"]
    assert "tauri_plugin_clipboard_manager::init()" in rust
    assert 'import { writeText } from "@tauri-apps/plugin-clipboard-manager"' in frontend
    assert 'request("last")' in frontend
    assert "await writeText(response.transcript)" in frontend
    assert "Transcript copied to clipboard." in frontend
    assert 'id="copy"' in markup


def test_packaged_daemon_logs_are_bounded_and_openable_from_settings():
    daemon = (ROOT / "transcriber/daemon.py").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    settings = (ROOT / "windows-app/keys.html").read_text()
    frontend = (ROOT / "windows-app/src/keys.js").read_text()
    docs = (ROOT / "windows-app/README.md").read_text()

    assert 'log_to_file=os.name == "nt" and bool(args.ui_token)' in daemon
    assert 'RotatingFileHandler(' in daemon
    assert 'maxBytes=2 * 1024 * 1024' in daemon
    assert 'backupCount=3' in daemon
    assert 'join("Whisper").join("logs")' in rust
    assert 'Command::new("explorer.exe")' in rust
    assert 'open_diagnostics_folder' in rust
    assert 'id="open-diagnostics"' in settings
    assert 'invoke("open_diagnostics_folder")' in frontend
    assert '%LOCALAPPDATA%\\Whisper\\logs\\daemon.log' in docs


def test_windows_panel_exposes_daemon_live_transcription_state_and_toggle():
    frontend = (ROOT / "windows-app/src/main.js").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()
    styles = (ROOT / "windows-app/src/style.css").read_text()

    assert 'id="streaming-toggle"' in markup
    assert 'id="live-transcript"' in markup
    assert 'request("set_streaming", { enabled: ui.streaming.checked })' in frontend
    assert 'currentStatus.live_text' in frontend
    assert 'currentStatus.live_unavailable_reason' in frontend
    assert '"groq", "openrouter"' in frontend
    assert '.live-toggle input:checked + .switch-track' in styles


def test_windows_settings_chooser_has_radio_group_and_arrow_key_navigation():
    markup = (ROOT / "windows-app/index.html").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()

    assert 'role="radiogroup" aria-labelledby="chooser-title"' in markup
    assert 'button.setAttribute("role", "radio")' in frontend
    assert '"ArrowDown", "ArrowUp", "Home", "End"' in frontend
    assert 'options[next].focus()' in frontend


def test_windows_panel_uses_consistent_vector_icons_instead_of_unicode_glyphs():
    frontend = (ROOT / "windows-app/src/main.js").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()
    styles = (ROOT / "windows-app/src/style.css").read_text()

    assert 'document.createElementNS(SVG_NS, "svg")' in frontend
    assert 'setActionContent(ui.cancel, "cancel", "Cancel")' in frontend
    assert 'data-icon="provider"' in markup
    assert 'data-icon="model"' in markup
    assert 'data-icon="mic"' in markup
    assert ".tile-symbol { width: 19px; height: 19px; }" in styles


def test_windows_installer_preview_is_manual_and_bundles_the_daemon():
    workflow = (ROOT / ".github/workflows/windows-installer-preview.yml").read_text()
    sidecar_build = (ROOT / "windows-app/scripts/build-sidecar.ps1").read_text()

    assert "workflow_dispatch:" in workflow
    assert "focus_test_passed == 'yes'" in workflow
    assert "--features packaged-daemon --no-sign" in workflow
    assert "./windows-app/scripts/build-sidecar.ps1" in workflow
    assert "--onefile" in sidecar_build and "--windowed" in sidecar_build
    assert "temporary isolated Python environment" in (ROOT / "windows-app/README.md").read_text()
    assert "whisper-windows-installer-preview" in workflow
    assert "Expected exactly one NSIS installer" in workflow
    assert "$Installers[0].Length -lt 1MB" in workflow
    assert "$Sidecar.Length -lt 10MB" in sidecar_build
    assert "foreach ($File in $PackagedFiles)" in workflow
    assert "Packaged file is not a Windows executable" in workflow
    assert "Get-FileHash -LiteralPath $Installers[0].FullName -Algorithm SHA256" in workflow
    assert "*.exe.sha256" in workflow


def test_public_windows_release_upload_requires_focus_confirmation_and_signing():
    workflow = (ROOT / ".github/workflows/windows-installer-preview.yml").read_text()
    docs = (ROOT / "windows-app/README.md").read_text()

    assert 'publish_release:\n        description:' in workflow
    assert 'release_tag:\n        description: Existing six-digit DDMMYY' in workflow
    assert 'installation_test_passed:\n        description:' in workflow
    assert "if: inputs.focus_test_passed == 'yes'" in workflow
    assert "if ('${{ inputs.sign_installer }}' -ne 'true')" in workflow
    assert "if ('${{ inputs.installation_test_passed }}' -ne 'yes')" in workflow
    assert "refs/heads/main" in workflow
    assert "ref: ${{ inputs.publish_release && inputs.release_tag || github.ref }}" in workflow
    assert "scripts/validate_release.py $env:RELEASE_TAG" in workflow
    assert "gh release view $env:RELEASE_TAG" in workflow
    assert "gh release upload $env:RELEASE_TAG" in workflow
    assert "gh release upload $env:RELEASE_TAG" in workflow and "--clobber" not in workflow
    assert "Windows installer preview and\nrelease** workflow" in docs


def test_windows_workflows_install_rustfmt_and_check_rust_formatting():
    ui_workflow = (ROOT / ".github/workflows/windows-tauri-ui.yml").read_text()
    installer_workflow = (ROOT / ".github/workflows/windows-installer-preview.yml").read_text()

    assert "components: rustfmt" in ui_workflow
    assert "components: rustfmt" in installer_workflow
    assert "cargo fmt --manifest-path windows-app/src-tauri/Cargo.toml -- --check" in ui_workflow
    assert installer_workflow.count("cargo fmt --manifest-path src-tauri/Cargo.toml -- --check") == 2


def test_windows_installer_can_be_signed_and_rejects_invalid_signatures():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    workflow = (ROOT / ".github/workflows/windows-installer-preview.yml").read_text()
    signer = (ROOT / "windows-app/scripts/sign-windows.ps1").read_text()
    sign_command = config["bundle"]["windows"]["signCommand"]

    assert sign_command["cmd"] == "powershell.exe"
    assert "%1" in sign_command["args"][-1]
    assert "WHISPER_SIGNING_THUMBPRINT" in signer
    assert "signtool.exe sign /fd SHA256" in signer or "sign /fd SHA256" in signer
    assert "type: boolean" in workflow
    assert "WINDOWS_SIGNING_CERTIFICATE_BASE64" in workflow
    assert "WINDOWS_SIGNING_CERTIFICATE_PASSWORD" in workflow
    assert "--no-sign" in workflow
    assert "Get-AuthenticodeSignature $File" in workflow
    assert "The signature for $File is not valid" in workflow


def test_daemon_supervisor_cannot_store_a_child_after_shutdown_wins_the_lock():
    supervisor = (ROOT / "windows-app/src-tauri/src/daemon.rs").read_text()
    set_child = supervisor.split("fn set_child(", 1)[1].split("\n    fn clear_child", 1)[0]

    assert "if let Ok(mut current) = self.child.lock()" in set_child
    assert set_child.index("self.child.lock()") < set_child.index("self.is_shutdown_requested()")
    assert "*current = Some(child)" in set_child
    assert set_child.count("child.kill()") == 2


def test_nsis_installs_per_user_starts_whisper_and_cleans_up_autostart():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    hooks = (ROOT / "windows-app/src-tauri/windows/installer-hooks.nsh").read_text()
    nsis = config["bundle"]["windows"]["nsis"]

    assert nsis["installMode"] == "currentUser"
    assert nsis["startMenuFolder"] == "Whisper"
    assert nsis["installerHooks"] == "windows/installer-hooks.nsh"
    assert '!macro NSIS_HOOK_POSTINSTALL' in hooks
    assert 'WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "Whisper"' in hooks
    assert 'Exec \'"$INSTDIR\\whisper-windows-ui.exe"\'' in hooks
    assert '!macro NSIS_HOOK_PREUNINSTALL' in hooks
    assert 'ExecWait \'"$INSTDIR\\whisper-windows-ui.exe" --whisper-uninstall\'' in hooks
    assert 'DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "Whisper"' in hooks


def test_uninstaller_closes_the_resident_app_and_its_daemon_cleanly():
    source = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    daemon = (ROOT / "windows-app/src-tauri/src/daemon.rs").read_text()

    assert 'const UNINSTALL_ARGUMENT: &str = "--whisper-uninstall"' in source
    assert "app.exit(0);" in source
    assert "std::process::exit(0)" in source
    assert "request_shutdown" in daemon


def test_packaged_tauri_app_registers_global_recording_shortcut():
    manifest = (ROOT / "windows-app/src-tauri/Cargo.toml").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()

    assert 'tauri-plugin-global-shortcut = "2"' in manifest
    assert "Modifiers::CONTROL | Modifiers::ALT" in rust
    assert "Code::KeyR" in rust
    assert 'app.emit("recording-hotkey", ())' in rust
    assert 'listen("recording-hotkey"' in frontend
    assert 'const toggleRecording = () => sendCommandAndHide("toggle")' in frontend
    assert "const HOTKEY_PENDING_WINDOW_MS = 2000" in frontend
    assert "pendingHotkeyToggles = (pendingHotkeyToggles + 1) % 2" in frontend
    assert "if (shouldToggle) void toggleRecording()" in frontend
    assert 'invoke("recording_shortcut_status")' in frontend


def test_tray_menu_keeps_recording_and_cancel_actions_available():
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    frontend = (ROOT / "windows-app/src/main.js").read_text()

    assert '"Start / stop recording · Ctrl+Alt+R"' in rust
    assert '"Cancel recording"' in rust
    assert 'app.emit("recording-hotkey", ())' in rust
    assert 'app.emit("cancel-recording", ())' in rust
    assert 'listen("cancel-recording"' in frontend


def test_packaged_app_uses_single_instance_and_reopens_without_focus_stealing():
    manifest = (ROOT / "windows-app/src-tauri/Cargo.toml").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()

    assert 'tauri-plugin-single-instance = "2"' in manifest
    assert rust.index("tauri_plugin_single_instance::init") < rust.index("tauri_plugin_shell::init()")
    assert "toggle_panel(app);" in rust
    assert "fn toggle_panel(app: &tauri::AppHandle)" in rust
    assert "show_panel(&window, &baseline, x, y, PANEL_HEIGHT as f64)" in rust


def test_provider_key_settings_open_only_from_the_provider_chooser():
    config = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())
    windows = {window["label"]: window for window in config["app"]["windows"]}
    main = (ROOT / "windows-app/src/main.js").read_text()
    markup = (ROOT / "windows-app/index.html").read_text()
    keys = (ROOT / "windows-app/src/keys.js").read_text()
    keys_markup = (ROOT / "windows-app/keys.html").read_text()
    rust = (ROOT / "windows-app/src-tauri/src/main.rs").read_text()
    daemon = (ROOT / "transcriber/daemon.py").read_text()
    websocket = (ROOT / "transcriber/platforms/windows/websocket.py").read_text()

    assert windows["main"]["focusable"] is False
    assert windows["keys"]["focusable"] is True
    assert 'id="manage-keys"' in markup
    assert 'import { invoke } from "@tauri-apps/api/core"' in keys
    assert 'ui.manageKeys.hidden = kind !== "provider" || focusTestBuild' in main
    assert 'invoke("show_key_settings")' in main
    assert 'request("set_key", { provider: ui.provider.value, key })' in keys
    assert 'request("set_key", { provider: ui.provider.value, clear: true })' in keys
    assert 'request("set_live_engine", settings)' in keys
    assert 'request("set_live_model", { model })' in keys
    assert "let liveSettingsDirty = false" in keys
    assert "if (!liveSettingsDirty)" in keys
    assert 'id="live-server-url"' in keys_markup
    assert 'invoke("websocket_token")' in main
    assert 'invoke("websocket_token")' in keys
    assert 'id="live-engine"' in keys_markup
    assert '"live_server_url": self.config.streaming.server_url' in daemon
    assert 'command == "set_key"' in daemon
    assert "daemon.command, command, params" in websocket


def test_website_no_longer_offers_the_legacy_powershell_installer():
    page = (ROOT / "website/download/index.html").read_text()
    readme = (ROOT / "README.md").read_text()

    assert not (ROOT / "install.ps1").exists()
    assert not (ROOT / "uninstall.ps1").exists()
    assert not (ROOT / "website/download/whisper-installer.ps1").exists()
    assert "Windows installer is being rebuilt" not in page
    assert "Install Whisper on Windows" in page
    assert "Download Windows installer" in page
    assert "Download PowerShell installer" not in page
    assert "install.ps1" not in page
    assert "Do not use `irm .../install.ps1 | iex`" in readme
    assert "The old PowerShell\ninstaller is retired." in readme


def test_windows_app_version_matches_the_date_version_of_the_python_app():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    cargo = tomllib.loads((ROOT / "windows-app/src-tauri/Cargo.toml").read_text())
    package = json.loads((ROOT / "windows-app/package.json").read_text())
    tauri = json.loads((ROOT / "windows-app/src-tauri/tauri.conf.json").read_text())

    version = project["project"]["version"]
    assert cargo["package"]["version"] == version
    assert package["version"] == version
    assert tauri["version"] == version
