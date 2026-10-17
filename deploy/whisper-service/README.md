# Deploying the Wicx hosted service

One env file per application. This service owns only variables prefixed
`WHISPER_` / `WICX_`, so on a host running several apps nothing here can be read
by — or leak into — another app. There is no `.env` checked into the repository.

## 1. Install

```bash
python -m venv /opt/wicx/venv
/opt/wicx/venv/bin/pip install '.[server]'          # add '.[diarization]' for speaker analysis
/opt/wicx/venv/bin/pip install '.[export]'          # optional: Word (.docx) exports
```

## 2. Create the env file

```bash
install -d -m 700 /etc/wicx
cp deploy/whisper-service/.env.example /etc/wicx/whisper-service.env
chmod 600 /etc/wicx/whisper-service.env
$EDITOR /etc/wicx/whisper-service.env
```

Set at minimum:

| Variable | Why |
| --- | --- |
| `WHISPER_ENABLE_DIARIZATION=1` | expose speaker analysis |
| `WHISPER_ENABLE_ANALYSIS=1` | expose summary/decisions/actions/translation |
| `WHISPER_OPENROUTER_API_KEY` | analysis and rewrite calls |
| `HF_TOKEN` | pyannote worker (after accepting the model conditions) |
| `WHISPER_TOKENS_FILE` | invite-only user tokens, mode 0600 |
| `WHISPER_ENTITLEMENTS_FILE` | paid feature grants, mode 0600 |

Add `WHISPER_ENABLE_BILLING=1` plus the Paystack block to meter the bundled
allowance, and `WHISPER_ENABLE_OAUTH=1` plus provider credentials to let a
subscription follow a signed-in account instead of a manual grant.

## 3. systemd unit

```ini
[Unit]
Description=Wicx hosted inference service
After=network-online.target

[Service]
Type=simple
User=wicx
EnvironmentFile=/etc/wicx/whisper-service.env
ExecStart=/opt/wicx/venv/bin/whisper-server serve \
    --host 127.0.0.1 --port 8765 --device cuda --model medium
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now whisper-service
journalctl -u whisper-service -n 50
```

Terminate TLS at a reverse proxy; the service binds loopback only. Diarization
and billing share the second HTTP app on port 8766.

## 4. First user

```bash
whisper-server token add --user <user id>            # prints the token once
whisper-server entitlement grant --user <user id> --feature diarization
whisper-server entitlement grant --user <user id> --feature meeting_analysis
whisper-server entitlement grant --user <user id> --feature lecture_analysis
```

The desktop client stores that token itself:

```bash
wicx set-key --provider hosted-whisper --key <token>
```

`wicx status --json` then reports `"has_whisper_service_token": true`.

## Development without systemd

The same file works from a shell; real variables still win:

```bash
set -a; . ./deploy/whisper-service/.env; set +a
whisper-server serve --host 127.0.0.1 --port 8765
```

`WHISPER_ENV_FILE=/path/to/file.env` loads a file other than `./.env`. Parsing
rules are the desktop client's `keys.env` rules: `KEY=VALUE` lines, `#`
comments, one optional layer of quotes, uppercase keys only, and an existing
environment variable is never overwritten. Values never appear in logs — only
the variable names do.