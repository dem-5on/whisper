/* Transcriber GNOME panel: idle / recording + timer + live partial /
 * processing spinner / error + retry. Daemon owns the work; this only
 * displays status (from `transcriber status --json`) and sends commands
 * (`toggle`, `cancel`, `retry`, `set-provider`, `set-model`, `set-mic`).
 * The transcript switch shows/hides the full completed transcript (fetched
 * via `transcriber last`); while recording, the daemon's rolling-window
 * decode streams partial/committed text via status.live_text (polled here)
 * or `transcriber subscribe` for push clients. Partials stay in the panel:
 * only the finalized transcript is typed into the focused app.
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const POLL_MS = 500;

function socketPath() {
    const runtime = GLib.getenv('XDG_RUNTIME_DIR');
    if (runtime)
        return `${runtime}/transcriber.sock`;
    return `/tmp/transcriber-${GLib.get_user_uid?.() ?? 0}.sock`;
}

function runTranscriber(argv) {
    return new Promise(resolve => {
        try {
            const proc = Gio.Subprocess.new(
                ['transcriber', ...argv],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            );
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, stdout] = p.communicate_utf8_finish(res);
                    resolve({ ok: true, stdout: (stdout ?? '').trim() });
                } catch (e) {
                    resolve({ ok: false, error: String(e) });
                }
            });
        } catch (e) {
            resolve({ ok: false, error: String(e) });
        }
    });
}

async function statusJson() {
    const r = await runTranscriber(['status', '--json']);
    if (!r.ok)
        return null;
    try {
        return JSON.parse(r.stdout);
    } catch {
        return null;
    }
}

function formatElapsed(seconds) {
    const s = Math.max(0, Math.floor(seconds ?? 0));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
}

const TranscriberIndicator = GObject.registerClass(
class TranscriberIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Transcriber', false);

        this._state = 'UNAVAILABLE';
        this._status = null;
        this._previous = null;
        this._pollId = 0;
        this._blinkOn = true;

        const box = new St.BoxLayout({ style_class: 'transcriber-box', vertical: false });
        this._icon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'system-status-icon transcriber-icon',
        });
        this._label = new St.Label({ text: '', style_class: 'transcriber-label', y_align: Clutter.ActorAlign.CENTER });
        box.add_child(this._icon);
        box.add_child(this._label);
        this.add_child(box);

        // Menu skeleton; items are rebuilt on each poll to reflect daemon state.
        this._statusItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._statusItem.label.add_style_class_name('transcriber-status-label');
        this._statusItem.label.get_clutter_text().set_ellipsize(Pango.EllipsizeMode.END);
        this._actionsItem = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        const actionsBox = new St.BoxLayout({ style_class: 'transcriber-action-row', x_expand: true });
        this._recordButton = new St.Button({
            style_class: 'button transcriber-action-button transcriber-record-button',
            x_expand: true, can_focus: true, track_hover: true,
        });
        const recordContent = new St.BoxLayout({ style_class: 'transcriber-action-content', x_expand: true });
        this._recordIcon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic', style_class: 'transcriber-action-icon',
        });
        this._recordLabel = new St.Label({ text: 'Start recording', y_align: Clutter.ActorAlign.CENTER });
        recordContent.add_child(this._recordIcon);
        recordContent.add_child(this._recordLabel);
        this._recordButton.set_child(recordContent);
        this._recordButton.connect('clicked', () => this._send('toggle'));
        this._cancelButton = new St.Button({
            style_class: 'button transcriber-action-button',
            x_expand: true, can_focus: true, track_hover: true,
        });
        const cancelContent = new St.BoxLayout({ style_class: 'transcriber-action-content', x_expand: true });
        this._cancelIcon = new St.Icon({ icon_name: 'process-stop-symbolic', style_class: 'transcriber-action-icon' });
        this._cancelLabel = new St.Label({ text: 'Cancel', y_align: Clutter.ActorAlign.CENTER });
        cancelContent.add_child(this._cancelIcon);
        cancelContent.add_child(this._cancelLabel);
        this._cancelButton.set_child(cancelContent);
        this._cancelButton.connect('clicked', () => this._send('cancel'));
        this._copyButton = new St.Button({
            style_class: 'button transcriber-action-button',
            x_expand: true, can_focus: true, track_hover: true,
        });
        const copyContent = new St.BoxLayout({ style_class: 'transcriber-action-content', x_expand: true });
        this._copyIcon = new St.Icon({ icon_name: 'edit-copy-symbolic', style_class: 'transcriber-action-icon' });
        this._copyLabel = new St.Label({ text: 'Copy transcript', y_align: Clutter.ActorAlign.CENTER });
        copyContent.add_child(this._copyIcon);
        copyContent.add_child(this._copyLabel);
        this._copyButton.set_child(copyContent);
        this._copyButton.connect('clicked', () => this._copyLast());
        actionsBox.add_child(this._recordButton);
        actionsBox.add_child(this._cancelButton);
        actionsBox.add_child(this._copyButton);
        this._actionsItem.add_child(actionsBox);
        this._retryItem = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        this._retryButton = new St.Button({
            style_class: 'button transcriber-action-button transcriber-record-button transcriber-retry-button',
            x_expand: true, can_focus: true, track_hover: true,
        });
        const retryContent = new St.BoxLayout({ style_class: 'transcriber-retry-content', x_expand: true });
        this._retryLabel = new St.Label({ text: 'Re-transcribe last', y_align: Clutter.ActorAlign.CENTER });
        retryContent.add_child(this._retryLabel);
        this._retryButton.set_child(retryContent);
        this._retryButton.connect('clicked', () => this._send('retry'));
        this._retryItem.add_child(this._retryButton);
        this._expandedChoice = null;
        this._providerMenu = this._makeChoiceControl('provider', 'Provider', 'Choose a provider', 'network-server-symbolic');
        this._modelMenu = this._makeChoiceControl('model', 'Model', 'Choose a transcription model', 'applications-science-symbolic');
        this._micMenu = this._makeChoiceControl('microphone', 'Microphone', 'Choose a microphone', 'audio-input-microphone-symbolic');
        this._providerModelRow = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        this._providerModelBox = new St.BoxLayout({
            style_class: 'transcriber-provider-model-row',
            x_expand: true,
        });
        this._providerMenu.item.set_x_expand(true);
        this._modelMenu.item.set_x_expand(true);
        this._providerModelBox.add_child(this._providerMenu.item);
        this._providerModelBox.add_child(this._modelMenu.item);
        this._providerModelRow.add_child(this._providerModelBox);
        this._micRetryRow = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        this._micRetryBox = new St.BoxLayout({
            style_class: 'transcriber-provider-model-row',
            x_expand: true,
        });
        this._micMenu.item.set_x_expand(true);
        this._retryItem.set_x_expand(true);
        this._retryItem.add_style_class_name('transcriber-retry-item');
        this._micRetryBox.add_child(this._micMenu.item);
        this._micRetryBox.add_child(this._retryItem);
        this._micRetryRow.add_child(this._micRetryBox);
        this._choiceDetailsItem = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        this._choiceDetailsItem.add_style_class_name('transcriber-choice-detail-item');
        this._choiceDetailsPanel = new St.BoxLayout({
            vertical: true,
            style_class: 'transcriber-option-panel',
            x_expand: false,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._choiceDetailsHeader = new St.BoxLayout({ style_class: 'transcriber-option-header', x_expand: true });
        this._choiceDetailsIcon = new St.Icon({ style_class: 'transcriber-option-header-icon' });
        this._choiceDetailsTitle = new St.Label({
            style_class: 'transcriber-option-title',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._choiceDetailsHeader.add_child(this._choiceDetailsIcon);
        this._choiceDetailsHeader.add_child(this._choiceDetailsTitle);
        this._choiceDetailsScroll = new St.ScrollView({
            style_class: 'transcriber-option-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: false,
            enable_mouse_scrolling: true,
            x_expand: true,
        });
        this._choiceDetailsScroll.set_style('max-height: 240px;');
        this._choiceDetailsOptions = new St.BoxLayout({ vertical: true, x_expand: true });
        this._choiceDetailsScroll.set_child(this._choiceDetailsOptions);
        this._choiceDetailsPanel.add_child(this._choiceDetailsHeader);
        this._choiceDetailsPanel.add_child(this._choiceDetailsScroll);
        this._choiceDetailsItem.add_child(this._choiceDetailsPanel);
        this._choiceDetailsItem.visible = false;
        this._modelsBackend = null;
        this._modelsList = [];
        this._modelsFetching = false;
        this._keyItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._keyItem.label.add_style_class_name('transcriber-key-label');
        this._keyItem.label.get_clutter_text().set_ellipsize(Pango.EllipsizeMode.END);
        // ON/OFF switch for the full completed transcript. While recording,
        // status.live_text carries the rolling partial (committed + newest
        // provisional words); only the finalized text is ever typed.
        this._showTranscript = true;
        this._fullTranscript = '';
        this._lastKey = null;
        this._fetching = false;
        this._destroyed = false;
        this._transcriptSwitch = new PopupMenu.PopupSwitchMenuItem('Show transcript', true);
        this._transcriptSwitch.connect('toggled', (item, state) => {
            this._showTranscript = state;
            this._renderTranscript(this._status);
        });
        // Live transcription (streaming PCM + rolling partials + VAD
        // auto-stop). That's a daemon setting, persisted to config.yaml and
        // applied to the next recording — flipping it never disturbs a run.
        this._streamingSwitch = new PopupMenu.PopupSwitchMenuItem('Live transcription', true);
        this._streamingSwitch.connect('toggled', (item, state) => {
            this._sendKeepOpen('set-streaming', ['--enabled', state ? 'true' : 'false']);
        });
        this._liveInfoItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._liveInfoItem.label.add_style_class_name('transcriber-live-info-label');
        this._liveInfoItem.label.get_clutter_text().set_ellipsize(Pango.EllipsizeMode.END);
        this._transcriptItem = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        this._transcriptScroll = new St.ScrollView({
            style_class: 'transcriber-transcript-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: false,
            enable_mouse_scrolling: true,
            x_expand: true,
        });
        this._transcriptContent = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });
        this._transcriptLabel = new St.Label({
            style_class: 'transcriber-label transcriber-transcript',
            x_expand: true,
            y_expand: false,
        });
        this._transcriptLabel.get_clutter_text().set_line_wrap(true);
        this._transcriptContent.add_child(this._transcriptLabel);
        this._transcriptScroll.set_child(this._transcriptContent);
        this._transcriptItem.add_child(this._transcriptScroll);
        this._transcriptScrollCap = 0;
        this._startItem = new PopupMenu.PopupMenuItem('Start daemon');
        this._startItem.connect('activate', () => this._startDaemon());

        this.menu.addMenuItem(this._statusItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._actionsItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._providerModelRow);
        this.menu.addMenuItem(this._micRetryRow);
        this.menu.addMenuItem(this._choiceDetailsItem);
        this.menu.addMenuItem(this._keyItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._streamingSwitch);
        this.menu.addMenuItem(this._liveInfoItem);
        this.menu.addMenuItem(this._transcriptSwitch);
        this.menu.addMenuItem(this._transcriptItem);
        this.menu.addMenuItem(this._startItem);

        this._pollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => {
            this._poll().catch(e => log(`transcriber: poll failed: ${e}`));
            return GLib.SOURCE_CONTINUE;
        });
        this._poll().catch(e => log(`transcriber: poll failed: ${e}`));
    }

    destroy() {
        this._destroyed = true;
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = 0;
        }
        super.destroy();
    }

    async _send(command, extra = []) {
        await runTranscriber([command, ...extra]);
        // Refresh immediately so the bar and timer respond without waiting a tick.
        await this._poll();
    }

    async _sendKeepOpen(command, extra = []) {
        // Setup flow (provider/model/mic): GNOME closes the menu on item
        // activate, so reopen it — it should only close on outside click or
        // the mic icon, letting the user finish setup in one pass.
        await this._send(command, extra);
        if (!this._destroyed)
            this.menu.open();
    }

    async _copyLast() {
        const r = await runTranscriber(['last']);
        if (!r.ok || !r.stdout) {
            Main.notify('Transcriber', 'No previous transcript to copy');
            return;
        }
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, r.stdout);
        Main.notify('Transcriber', 'Transcript copied to clipboard');
    }

    _startDaemon() {
        // Prefer the user service; fall back to a direct spawn when unmanaged.
        try {
            Gio.Subprocess.new(
                ['systemctl', '--user', 'start', 'transcriber'],
                Gio.SubprocessFlags.NONE,
            );
        } catch {
            try {
                Gio.Subprocess.new(['transcriber-daemon'], Gio.SubprocessFlags.NONE);
            } catch (e) {
                Main.notify('Transcriber', 'Could not start daemon');
            }
        }
    }

    async _poll() {
        const status = await statusJson();
        if (this._destroyed)
            return;
        this._previous = this._state;
        if (!status) {
            this._state = 'UNAVAILABLE';
            this._status = null;
            this._renderUnavailable();
            return;
        }
        this._state = status.state ?? 'UNAVAILABLE';
        this._status = status;
        this._render(status);
        this._notifyTransitions(this._previous, status);
    }

    _renderUnavailable() {
        this._icon.icon_name = 'audio-input-microphone-muted-symbolic';
        this.remove_style_class_name('transcriber-recording');
        this.remove_style_class_name('transcriber-error');
        this.remove_style_class_name('transcriber-warn');
        this._label.text = '';
        this._statusItem.label.text = 'Daemon not running';
        this._recordButton.sensitive = false;
        this._cancelButton.sensitive = false;
        this._retryButton.sensitive = false;
        this._copyButton.sensitive = false;
        this._providerMenu.arrow.sensitive = false;
        this._modelMenu.arrow.sensitive = false;
        this._micMenu.arrow.sensitive = false;
        this._toggleChoiceControl(null);
        this._streamingSwitch.sensitive = false;
        this._keyItem.visible = false;
        this._transcriptItem.visible = false;
        this._startItem.visible = true;
    }

    _render(s) {
        const state = s.state ?? 'UNAVAILABLE';
        this._startItem.visible = false;

        if (state === 'RECORDING') {
            this._icon.icon_name = 'audio-input-microphone-symbolic';
            this.add_style_class_name('transcriber-recording');
            this.remove_style_class_name('transcriber-error');
            // Blink the dot via the label; the timer itself is the trust cue.
            this._blinkOn = !this._blinkOn;
            const dot = this._blinkOn ? '●' : '○';
            this._label.text = `${dot} ${formatElapsed(s.recording_elapsed)}`;
            let liveNote = s.streaming !== false && !s.live_available
                ? ` — ${s.live_unavailable_reason || 'live partials unavailable'}`
                : (s.speech_active ? ' — speech detected' : ' — listening for speech');
            if (s.live_available && s.live_decode_ms)
                liveNote += ` · ${Math.round(s.live_decode_ms)} ms decode`;
            this._statusItem.label.text = `Recording… ${formatElapsed(s.recording_elapsed)}${liveNote}`;
        } else if (state === 'PROCESSING' || state === 'DELIVERING') {
            this._icon.icon_name = 'view-refresh-symbolic';
            this.remove_style_class_name('transcriber-recording');
            this.remove_style_class_name('transcriber-error');
            this.remove_style_class_name('transcriber-warn');
            const what = state === 'DELIVERING' ? 'Delivering' : 'Transcribing';
            this._label.text = '…';
            this._statusItem.label.text = `${what}… ${formatElapsed(s.processing_elapsed)} (${s.backend ?? 'local'}/${s.model ?? ''})`;
        } else if (state === 'ERROR') {
            // Soft failure (silence / no speech): orange mic, no crash styling.
            // Hard failure: red error icon as before.
            const soft = !!s.soft_error;
            this._icon.icon_name = soft
                ? 'audio-input-microphone-symbolic'
                : 'dialog-error-symbolic';
            this.remove_style_class_name('transcriber-recording');
            this.remove_style_class_name('transcriber-error');
            this.remove_style_class_name('transcriber-warn');
            if (soft)
                this.add_style_class_name('transcriber-warn');
            else
                this.add_style_class_name('transcriber-error');
            this._label.text = soft ? '○' : '!';
            const reason = s.error || 'Something failed';
            this._statusItem.label.text = soft
                ? `${reason} — press Start to retry`
                : `Error: ${reason}`;
        } else {
            this._icon.icon_name = 'audio-input-microphone-symbolic';
            this.remove_style_class_name('transcriber-recording');
            this.remove_style_class_name('transcriber-error');
            this.remove_style_class_name('transcriber-warn');
            this._label.text = '';
            this._statusItem.label.text = `Ready (${s.backend ?? 'local'}/${s.model ?? ''} · ${s.mic ?? 'default'})`;
        }

        const hasLast = !!s.last_audio;
        this._retryButton.sensitive = hasLast && !['RECORDING', 'PROCESSING', 'DELIVERING'].includes(state);
        this._retryLabel.text = 'Re-transcribe last';
        const recording = state === 'RECORDING';
        this._recordLabel.text = recording
            ? 'Stop'
            : (state === 'ERROR' ? 'Try again' : 'Start');
        this._recordIcon.icon_name = recording
            ? 'media-playback-stop-symbolic'
            : 'audio-input-microphone-symbolic';
        if (recording)
            this._recordButton.add_style_class_name('transcriber-stop-button');
        else
            this._recordButton.remove_style_class_name('transcriber-stop-button');
        this._recordButton.sensitive = !['PROCESSING', 'DELIVERING', 'UNAVAILABLE'].includes(state);
        this._cancelButton.sensitive = state === 'RECORDING' || state === 'ERROR';
        this._copyButton.sensitive = hasLast;

        this._renderProviderMenu(s);
        this._renderModelMenu(s);
        this._renderMicMenu(s);
        this._renderKeyHint(s);
        this._renderStreamingSwitch(s);

        this._renderTranscript(s);
    }

    async _fetchFullTranscript(key) {
        // Fetch once per completed run; polling every second must not spawn
        // `transcriber last` on every tick.
        if (this._lastKey === key)
            return;
        this._lastKey = key;
        const r = await runTranscriber(['last']);
        if (this._destroyed)
            return;
        this._fullTranscript = r.ok && r.stdout ? r.stdout : '';
        this._renderTranscript(this._status);
    }

    _renderTranscript(s) {
        this._updateTranscriptScrollCap();
        if (this._destroyed || !s || !this._showTranscript) {
            this._transcriptItem.visible = false;
            return;
        }
        const state = s.state ?? 'UNAVAILABLE';
        if (state === 'RECORDING') {
            const live = (s.live_text ?? '').trim();
            this._transcriptItem.visible = true;
            if (live) {
                this._transcriptLabel.text = `● ${live}`;
            } else if (s.streaming !== false && !s.live_available) {
                this._transcriptLabel.text = `● ${s.live_unavailable_reason || 'Live partials unavailable.'}`;
            } else if (s.live_decode_error) {
                this._transcriptLabel.text = `● Live decode retrying: ${s.live_decode_error}`;
            } else {
                this._transcriptLabel.text = '● Listening… press Stop recording when done.';
            }
            return;
        }
        if (state === 'PROCESSING' || state === 'DELIVERING') {
            const live = (s.live_text ?? '').trim();
            this._transcriptItem.visible = true;
            this._transcriptLabel.text = live
                ? `… Finalizing: ${live}`
                : '… Transcribing… full text appears here when done.';
            return;
        }
        if (!s.last_audio) {
            this._transcriptItem.visible = true;
            this._transcriptLabel.text = 'No transcript yet — press Start recording.';
            return;
        }
        const key = `${s.last_backend}|${s.last_duration}|${s.last_audio}`;
        if (this._lastKey !== key && !this._fetching) {
            this._fetching = true;
            this._fetchFullTranscript(key).finally(() => {
                this._fetching = false;
            });
        }
        if (this._fullTranscript) {
            const dur = s.last_duration ? ` (${s.last_duration}s via ${s.last_backend || s.backend || 'local'})` : '';
            this._transcriptItem.visible = true;
            this._transcriptLabel.text = `${this._fullTranscript}${dur}`;
        } else {
            this._transcriptItem.visible = true;
            this._transcriptLabel.text = '… Loading transcript…';
        }
    }

    _updateTranscriptScrollCap() {
        // Let short transcripts determine their own height. Bound only the
        // transcript viewport so long text scrolls without stretching the
        // rest of the menu across the monitor.
        const monitor = Main.layoutManager.findMonitorForActor(this)
            ?? Main.layoutManager.primaryMonitor;
        if (!monitor?.height)
            return;
        const cap = Math.max(160, Math.min(420, Math.floor(monitor.height * 0.35)));
        if (cap === this._transcriptScrollCap)
            return;
        this._transcriptScrollCap = cap;
        this._transcriptScroll.set_style(`max-height: ${cap}px;`);
    }

    _makeChoiceControl(id, label, title, iconName) {
        const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        item.add_style_class_name('transcriber-choice-item');
        const root = new St.BoxLayout({
            vertical: false,
            style_class: 'transcriber-choice-control',
            x_expand: true,
        });
        const tile = new St.BoxLayout({
            style_class: 'transcriber-choice-row',
            x_expand: true,
        });
        const tileContent = new St.BoxLayout({
            style_class: 'transcriber-choice-content',
            x_expand: true,
        });
        const tileIcon = new St.Icon({ icon_name: iconName, style_class: 'transcriber-choice-icon' });
        const tileLabels = new St.BoxLayout({ vertical: true, style_class: 'transcriber-choice-labels', x_expand: true });
        const titleLabel = new St.Label({ text: label, style_class: 'transcriber-choice-title' });
        const valueLabel = new St.Label({ text: '', style_class: 'transcriber-choice-value' });
        valueLabel.get_clutter_text().set_single_line_mode(true);
        valueLabel.get_clutter_text().set_ellipsize(3);
        tileLabels.add_child(titleLabel);
        tileLabels.add_child(valueLabel);
        tileContent.add_child(tileIcon);
        tileContent.add_child(tileLabels);
        const arrow = new St.Button({
            style_class: 'button transcriber-choice-arrow-button',
            can_focus: true,
            track_hover: true,
            y_expand: true,
        });
        const arrowIcon = new St.Icon({ icon_name: 'pan-end-symbolic', style_class: 'transcriber-choice-arrow-icon' });
        arrow.set_child(arrowIcon);
        tile.add_child(tileContent);
        tile.add_child(arrow);
        root.add_child(tile);
        item.add_child(root);
        const control = { id, title, iconName, item, label: valueLabel, arrow, arrowIcon, choices: [], optionsSignature: '' };
        arrow.connect('clicked', () => this._toggleChoiceControl(control));
        return control;
    }

    _toggleChoiceControl(control) {
        const show = control !== null && this._expandedChoice !== control.id;
        this._expandedChoice = show ? control.id : null;
        this._choiceDetailsItem.visible = show;
        if (!show)
            return;
        const rowWidth = this._providerModelRow.get_width();
        const cardWidth = Math.max(240, rowWidth - 24);
        this._choiceDetailsPanel.set_width(cardWidth);
        this._choiceDetailsScroll.set_width(Math.max(180, cardWidth - 40));
        this._choiceDetailsTitle.text = control.title;
        this._choiceDetailsIcon.icon_name = control.iconName;
        this._renderChoiceOptions(control.choices);
    }

    _renderChoiceOptions(choices) {
        for (const child of this._choiceDetailsOptions.get_children()) {
            this._choiceDetailsOptions.remove_child(child);
            child.destroy();
        }
        for (const choice of choices) {
            if (choice.placeholder) {
                const placeholder = new St.Label({ text: choice.label, style_class: 'transcriber-option-placeholder' });
                this._choiceDetailsOptions.add_child(placeholder);
                continue;
            }
            const button = new St.Button({
                style_class: 'button transcriber-option-button',
                x_expand: true,
                can_focus: true,
                track_hover: true,
                reactive: choice.enabled !== false,
            });
            const row = new St.BoxLayout({ style_class: 'transcriber-option-row', x_expand: true });
            const label = new St.Label({
                text: choice.label,
                style_class: 'transcriber-option-label',
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            label.get_clutter_text().set_single_line_mode(true);
            label.get_clutter_text().set_ellipsize(3);
            const check = new St.Icon({
                icon_name: 'object-select-symbolic',
                style_class: 'transcriber-option-check',
                visible: !!choice.active,
            });
            row.add_child(label);
            row.add_child(check);
            button.set_child(row);
            button.connect('clicked', () => choice.activate?.());
            this._choiceDetailsOptions.add_child(button);
        }
    }

    _setChoiceOptions(control, choices) {
        const signature = JSON.stringify(choices.map(choice => [choice.label, choice.active, choice.enabled !== false]));
        if (signature === control.optionsSignature)
            return;
        control.optionsSignature = signature;
        control.choices = choices;
        if (this._expandedChoice === control.id)
            this._renderChoiceOptions(choices);
    }

    _renderProviderMenu(s) {
        this._providerMenu.arrow.sensitive = !['RECORDING', 'PROCESSING', 'DELIVERING'].includes(s.state);
        const providers = [
            ['local', 'Local', true],
            ['groq', 'Groq', false],
            ['openrouter', 'OpenRouter', false],
            ['openai', 'OpenAI', true],
        ];
        const choices = [];
        for (const [b, name, live] of providers) {
            choices.push({
                label: `${name} · ${live ? 'Live' : 'Batch only'}`,
                active: s.backend === b,
                enabled: !['RECORDING', 'PROCESSING', 'DELIVERING'].includes(s.state),
                activate: () => this._sendKeepOpen('set-provider', ['--provider', b]),
            });
        }
        const providerName = providers.find(([id]) => id === s.backend)?.[1] ?? 'Local';
        this._providerMenu.label.text = providerName;
        this._setChoiceOptions(this._providerMenu, choices);
    }

    _renderModelMenu(s) {
        const busy = ['RECORDING', 'PROCESSING', 'DELIVERING'].includes(s.state);
        this._modelMenu.arrow.sensitive = !busy;
        const choices = [];
        if (s.backend === 'local' || s.backend === 'openai') {
            const models = s.backend === 'local'
                ? ['tiny', 'base', 'small', 'medium', 'large-v3', 'turbo']
                : ['gpt-transcribe', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe', 'whisper-1'];
            for (const m of models) {
                choices.push({
                    label: m,
                    active: s.model === m,
                    enabled: !busy,
                    activate: () => this._sendKeepOpen('set-model', ['--model', m]),
                });
            }
        } else if (this._modelsBackend === s.backend && this._modelsList.length) {
            for (const m of this._modelsList) {
                const id = String(m.id ?? '');
                if (!id)
                    continue;
                choices.push({
                    label: id,
                    active: s.model === id,
                    enabled: !busy,
                    activate: () => this._sendKeepOpen('set-model', ['--model', id]),
                });
            }
        } else if (this._modelsBackend === s.backend && !this._modelsFetching) {
            choices.push({ label: 'No models available', placeholder: true });
        } else {
            choices.push({ label: 'Loading models…', placeholder: true });
            if (!this._modelsFetching) {
                this._modelsBackend = s.backend;
                this._modelsList = [];
                this._fillModelMenu(s.backend);
            }
        }
        this._modelMenu.label.text = s.model ?? '';
        this._setChoiceOptions(this._modelMenu, choices);
    }

    async _fillModelMenu(backend) {
        this._modelsFetching = true;
        try {
            const r = await runTranscriber(['models', '--json']);
            if (this._destroyed)
                return;
            try {
                const parsed = JSON.parse(r.stdout);
                const models = Array.isArray(parsed.models) ? parsed.models : [];
                this._modelsBackend = backend;
                this._modelsList = models;
            } catch {
                this._modelsBackend = backend;
                this._modelsList = [];
            }
            if (!this._destroyed && this._status?.backend === backend)
                this._renderModelMenu(this._status);
        } finally {
            this._modelsFetching = false;
            if (!this._destroyed && this._status?.backend === backend)
                this._renderModelMenu(this._status);
        }
    }

    _renderStreamingSwitch(s) {
        // Daemon truth wins every poll; setToggleState emits no signal, so
        // this never loops back into set-streaming by itself.
        const providerBlocksLive = ['groq', 'openrouter'].includes(s.backend);
        const providerName = s.backend === 'groq' ? 'Groq' : 'OpenRouter';
        this._streamingSwitch.setToggleState(!providerBlocksLive && s.streaming !== false);
        this._streamingSwitch.label.text = providerBlocksLive || (s.streaming !== false && !s.live_available)
            ? 'Live transcription (unavailable)'
            : 'Live transcription';
        this._streamingSwitch.sensitive = !providerBlocksLive
            && !['RECORDING', 'PROCESSING', 'DELIVERING'].includes(s.state);
        this._liveInfoItem.visible = providerBlocksLive;
        this._liveInfoItem.label.text = providerBlocksLive
            ? `${providerName} is batch-only. Select Local or OpenAI for live transcription.`
            : '';
    }

    _renderKeyHint(s) {
        // Presence only; values never leave the daemon.
        const missing = (s.backend === 'groq' && !s.has_groq_key)
            || (s.backend === 'openrouter' && !s.has_openrouter_key)
            || (s.backend === 'openai' && !s.has_openai_key);
        if (missing) {
            this._keyItem.visible = true;
            const provider = s.backend;
            this._keyItem.label.text = `${provider} key missing — run: transcriber set-key --provider ${provider}`;
        } else {
            this._keyItem.visible = false;
        }
    }

    _renderMicMenu(s) {
        const busy = ['RECORDING', 'PROCESSING', 'DELIVERING'].includes(s.state);
        this._micMenu.arrow.sensitive = !busy;
        this._micMenu.label.text = s.mic && s.mic !== 'default' ? (this._micsList ?? []).find(mic => mic.id === s.mic)?.name ?? s.mic : 'Default';
        if (!this._micsFilled)
            this._fillMicMenu(s.mic ?? 'default');
        const choices = [{
            label: 'Default',
            active: !s.mic || s.mic === 'default',
            enabled: !busy,
            activate: () => this._sendKeepOpen('set-mic', ['--device', 'default']),
        }];
        for (const mic of this._micsList ?? []) {
            choices.push({
                label: mic.name,
                active: s.mic === mic.id,
                enabled: !busy,
                activate: () => this._sendKeepOpen('set-mic', ['--device', mic.id]),
            });
        }
        this._setChoiceOptions(this._micMenu, choices);
    }

    async _fillMicMenu(current) {
        this._micsFilled = true;
        const r = await runTranscriber(['mics', '--json']);
        if (this._destroyed)
            return;
        if (!r.ok)
            return;
        try {
            const parsed = JSON.parse(r.stdout);
            this._micsList = [];
            for (const m of parsed.mics ?? []) {
                const id = String(m.id ?? '');
                if (!id)
                    continue;
                this._micsList.push({ id, name: String(m.name ?? id) });
            }
            if (this._status)
                this._renderMicMenu(this._status);
        } catch {
            this._micsList = [];
        }
    }

    _notifyTransitions(previous, s) {
        if (previous === 'RECORDING' && (s.state === 'PROCESSING' || s.state === 'DELIVERING'))
            Main.notify('Transcriber', 'Recording stopped; transcribing…');
        else if ((previous === 'PROCESSING' || previous === 'DELIVERING') && s.state === 'IDLE')
            Main.notify('Transcriber', this._doneBody(s));
        else if (s.state === 'ERROR' && previous !== 'ERROR')
            Main.notify('Transcriber', s.soft_error
                ? `No speech detected — try again`
                : `Failed: ${s.error || 'unknown error'}`);
    }

    _doneBody(s) {
        // Confirmation that the right provider finished, how long it took, and
        // (opt-in) what it heard — without opening anything.
        const parts = [`Done via ${s.last_backend || s.backend || 'local'}`];
        if (s.last_duration)
            parts.push(`${s.last_duration}s`);
        const preview = s.last_preview ? ` — “${s.last_preview}”` : '';
        return `${parts.join(' · ')}${preview}`;
    }
});

export default class TranscriberExtension extends Extension {
    enable() {
        this._indicator = new TranscriberIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
