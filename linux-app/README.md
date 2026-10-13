# Wicx Linux desktop app

This is the Linux-specific desktop workspace, separate from the Windows Tauri
app and the existing GNOME panel extension. Its visual direction follows the
Typeless Linux reference: a quiet workspace, compact sidebar, clear voice-mode
entry points, and a small setup rail rather than a dense control panel.

## Preview the interface

Install Node.js and npm, then from this directory run:

```sh
npm install
npm run dev
```

Open the local URL printed by Vite. Browser preview mode uses in-memory sample
recordings and dictionary terms; recording controls are explicitly preview-only.

## Run the desktop app

Install the Tauri Linux prerequisites for your distribution, Rust, Node.js/npm,
and this repository's Python app dependencies. Ensure the existing Wicx user
daemon is running, then run:

```sh
npm run tauri dev
```

The app talks to the existing per-user daemon over its Unix socket. It does not
change the transcription service or claim that global shortcuts, tray menus,
focus handling, or synthetic text insertion work identically on all Linux
desktop environments. Those integrations remain environment-specific and must
be validated per the [Linux compatibility matrix](COMPATIBILITY.md) before
public release. Current matrix entries distinguish build checks from actual
audio, overlay, shortcut, and focused-app validation.

The first UI slice includes Home, Dictation/Lecture/Meeting entry points,
recording consent, local History, custom Dictionary, and preferences for the
recording pill and rewrite behavior. Account, subscription, speaker diarization,
and full lecture/meeting analysis screens are not implemented by this UI slice.
