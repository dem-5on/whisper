# Linux compatibility and validation

Wicx is built with Linux desktop environments in mind, but “Linux” does not guarantee identical global-shortcut, overlay-placement, audio, tray, or focused-app text-input behavior. A combination is supported only after its relevant checks below have been performed.

## Validation matrix

| Distribution / session | Audio stack | Frontend build | Rust/Tauri checks | Audio and overlay runtime | Focused-app insertion | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Kali Linux x86_64, GNOME 50.4, Wayland with XWayland available | PipeWire 1.6.9; Built-in Audio Analog Stereo capture verified | Pass (`npm run build --ignore-scripts`) | Pass (`cargo check`; `cargo fmt --check`; three pill-position unit tests) | Daemon 2026.10.10 reinstalled; recording → cloud transcription → focused-app delivery verified; pill waveform live at ~8.6Hz after PipeWire latency fix; pill cancel/finish mechanics verified | Verified via `ydotool` keyboard backend on this box | **Partial validation only** |
| GNOME on X11 | Not tested | — | — | Not tested | Not tested | Untested |
| KDE Plasma on Wayland / X11 | Not tested | — | — | Not tested | Not tested | Untested |
| Xfce, Cinnamon, and other desktops | Not tested | — | — | Not tested | Not tested | Untested |

The first row records hands-on validation on 2026-10-13: real microphone recording, cloud transcription and delivery into the focused app, pill waveform/cancel/finish behavior, and insertion all exercised. Still open on this combination: pill Thinking/hide transition across a full record→process→idle cycle, desktop-global-shortcut binding, and restart/microphone-switch survival. It does **not** certify other environments.

## Current implementation notes

- Linux shortcut setup currently asks the user to register `wicx toggle` with their desktop's custom keyboard-shortcut settings. Do not assume one global-shortcut API works across all compositors.
- The app tries XWayland when a Wayland session also exposes `DISPLAY`, unless the user explicitly sets `GDK_BACKEND`. Without XWayland, the compositor may ignore absolute coordinates requested for the pill; placement must be verified per compositor.
- PipeWire device enumeration is not the same as recording permission or successful capture. Test the actual selected input and waveform with the user's consent before marking audio capture verified.
- The pill-position arithmetic has unit coverage for centering, negative monitor origins, and display scaling. Those tests do not prove that a compositor honors the requested position.

## Required manual checks before advertising a combination

1. Record a short, consented test phrase; confirm correct microphone, waveform response, and audio-derived silence/speech behavior.
2. Confirm the pill appears near bottom-center without moving focus; cancel and finish it, then verify each result.
3. Dictate into a separate target application and verify insertion goes to the previously focused app.
4. Test the desktop-configured global shortcut and record any compositor conflict or setup limitation.
5. Repeat after app restart and after changing microphone/default audio device.

Record OS/distribution version, desktop environment/version, X11 or Wayland, XWayland availability, audio server, hardware/input, Wicx version, and exact results for every tested combination.
