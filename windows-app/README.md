# Whisper Windows UI spike

This Tauri shell is the first Windows UI spike: a resident tray icon opens a hidden, non-activating panel anchored at the tray. The initial focus check is still a release gate; recording controls remain disabled until the no-focus-stealing behavior has been verified on Windows.

## Build and run on Windows

Install the current stable Rust toolchain and Node.js 20 or newer. Then, from this directory:

```powershell
npm ci
npm run tauri dev
```

To build the daemon sidecar for local native testing, run this developer helper
from the repository root (not from a user installation):

```powershell
.\windows-app\scripts\build-sidecar.ps1
```

It creates a temporary isolated Python environment, installs the
Windows/Python build dependencies there, and places the packaged sidecar in
Tauri's ignored build directory. It does not change the developer's global
Python environment. Then, from `windows-app`, run `npm ci` and build the focus
diagnostic:

```powershell
npm run tauri build -- --no-bundle --ci --features focus-test
```

The executable is at
`src-tauri\target\release\whisper-windows-ui.exe`. Only after the manual
focus test below passes, a developer can build a private unsigned installer
preview with:

```powershell
npm run tauri build -- --ci --features packaged-daemon --no-sign
```

That unsigned preview is for testing only; Windows may warn because it has no
trusted publisher signature. End users will receive a signed NSIS installer,
not the PowerShell build helper or a command they need to run in a terminal.

The UI connects to the Windows daemon at `127.0.0.1:47651`; packaged builds authenticate that connection with a random per-user token stored in the app's local data folder and passed only between the Tauri shell and its sidecar. Reusing the token lets a restarted UI reconnect to a daemon that survived the shell. CLI and tray commands use a separate authenticated Windows named pipe; Linux continues to use its Unix-domain socket. The daemon reports a distinct duplicate-instance exit so the new shell can adopt the running process and periodically check whether it needs to start a replacement. For development, start `whisper-daemon` from a second PowerShell window with the project virtual environment active. The packaged-daemon feature starts and supervises the bundled sidecar and registers `Ctrl+Alt+R` to start or stop recording; it is disabled in the focus-test executable so that focus verification does not start a model or claim the shortcut.

The packaged daemon writes rotating diagnostics to
`%LOCALAPPDATA%\Whisper\logs\daemon.log` (2 MiB per file, three backups).
This remains available when the hidden sidecar has no console, and helps debug
startup or transcription failures without requiring users to run PowerShell.

The panel exposes the daemon's start/stop, cancel, retry, provider/model/microphone selection, live-transcription toggle and live text, and transcript-copy actions. Provider API keys, the live engine, hosted service URL, and live model/profile are configured in the separate focused settings window; the recording flyout itself remains non-activating so it does not take focus from the app where dictated text should be inserted.

## Focus test (must pass before controls or a public installer are enabled)

The Windows Tauri workflow builds an executable artifact named
`whisper-windows-focus-spike`. Download it from the workflow run's **Artifacts**
section, extract it, and run `whisper-windows-ui.exe`. It has no daemon sidecar
and is not an installer or functional transcription release; recording and
settings controls are intentionally disabled until the focus test passes.

The Tauri crate has no default Cargo features: CI opts into `focus-test` for
this diagnostic executable, while the preview installer workflow explicitly
opts into `packaged-daemon`. This ensures the focus-test executable cannot
silently launch the transcription daemon.

After the manual focus test passes, run the **Windows installer preview and
release** workflow from GitHub Actions and select `yes` for its focus-test
confirmation. It builds an NSIS setup executable containing the Tauri UI and a
PyInstaller daemon sidecar, then uploads a private workflow artifact by default.
Keep `sign_installer` off for an unsigned test preview. For a signed preview, add the repository secrets
`WINDOWS_SIGNING_CERTIFICATE_BASE64` and
`WINDOWS_SIGNING_CERTIFICATE_PASSWORD`, then enable `sign_installer`; the
workflow signs the daemon, Tauri executable, and installer and validates the
installer signature. To attach a signed installer to an existing GitHub release,
also enable `publish_release` and provide its six-digit `DDMMYY` tag. Publication
is restricted to the `main` branch, checks out the release tag itself, and
requires that the tag already has a release, the tag matches the project and
Tauri versions, and both the focus-test and signed installation-test
confirmations are `yes`. The installation test covers install, launch,
recording, settings, and uninstall. The script-based Windows installer has been
removed from the website and repository.

The PFX must be a Windows code-signing certificate with its private key. To
base64-encode it locally in PowerShell, run
`[Convert]::ToBase64String([IO.File]::ReadAllBytes('certificate.pfx'))`, then
store that output as `WINDOWS_SIGNING_CERTIFICATE_BASE64` and the export
password as `WINDOWS_SIGNING_CERTIFICATE_PASSWORD`. Never commit the PFX or
password to the repository.

The NSIS installer is per-user (no administrator prompt), starts Whisper after
installation, enables it at Windows sign-in, and removes that sign-in entry on
uninstall. User configuration and retained recordings are outside the app
installation directory and are not deleted by the installer. On a PC without
the WebView2 Runtime, setup silently downloads and installs Microsoft's
bootstrapper; that case needs an internet connection. Windows 10 (April 2018 or
newer) and Windows 11 normally include the runtime already. The first local
speech-model download also needs internet access.

1. Open a text editor and leave the caret in the document.
2. Type a short distinctive prefix, then click Whisper's tray icon.
3. Continue typing without clicking the editor again.
4. Confirm all new characters still appear in the editor behind Whisper.
5. Click the panel's **×** button to hide it, then type another marker. Confirm it still goes to the editor; this also checks that mouse controls respond without activating the flyout.
6. Reopen the panel and check its focus result: it should say **Focus preserved**. Press Escape to hide it, reopen it, then activate another app; Whisper should hide without becoming the active window. Repeat the typing check.

If the panel reports **Focus changed**, or typing stops appearing in the editor, treat this spike as failed; do not connect recording/text insertion until the native Windows activation behavior is fixed. The panel's message samples the foreground window after a short settling delay, but it is still only a diagnostic; the typing check remains the authoritative manual test.
