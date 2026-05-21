# WhisperLive systemd unit (S3-1)

Provides the upstream Whisper transcription service consumed by
the dashboard's `/ws/voice` WebSocket route. The dashboard
forwards browser-side AudioWorklet PCM frames to WhisperLive,
receives partial + final transcripts back, and accumulates finals
into the per-tmux-session buffer for commit via `tmux_send`.

**Spec:** `graph://86fd1897-d4d`

## Prerequisites

- CUDA-capable GPU (spec: RTX 5080; large-v3 + ollama coexistence
  is the canonical target). int8_float16 quantisation keeps the
  model under ~1.5GB VRAM.
- Python with `whisper-live` installed (`pip install whisper-live`
  or the operator's preferred install path; the binary must end
  up at `/usr/local/bin/whisperlive` to match the unit file's
  `ExecStart`, OR the operator edits the unit to match wherever
  the binary actually landed).
- Faster-whisper backend dependencies (CT2, etc.) installed by
  whisper-live's setup.

## Install

```bash
# 1. Copy the unit file into systemd's system-scope directory.
sudo cp tools/dashboard/systemd/whisperlive.service /etc/systemd/system/

# 2. Edit User= to match the host operator account that owns
#    CUDA/GPU access. The placeholder __OPERATOR_USER__ MUST be
#    replaced; daemon-reload will accept the unit either way but
#    the service will fail to start until the user exists.
sudo sed -i 's/__OPERATOR_USER__/jeremy/' /etc/systemd/system/whisperlive.service
# (substitute your own host account; this example uses 'jeremy')

# 3. Reload + enable + start.
sudo systemctl daemon-reload
sudo systemctl enable whisperlive
sudo systemctl start whisperlive
```

## Verify

```bash
# Follow logs (journald tag: whisperlive)
journalctl -t whisperlive -f

# Expected on startup (large-v3 cold-load takes ~5-10s):
#   "Loading model large-v3..."
#   "Server listening on 127.0.0.1:9090"

# Probe the port locally
ss -tlnp | grep 9090

# Round-trip via the dashboard once /ws/voice is rebuilt with the
# websockets dependency from S3-4a:
#   - open /ws/voice?bind=<live tmux> through the dashboard
#   - send {"type": "start"} → the SERVER_READY handshake should
#     complete in <5s; the dashboard sends no error frame
```

## Resource notes

| Concern | Spec value | Where it lives |
|---|---|---|
| Port | 9090 | `--port 9090` in unit |
| Audio format on the wire | float32 LE [-1,1] (pip 0.8.0) / int16 LE (upstream + --raw_pcm_input) | converted inside `tools/dashboard/voice_whisperlive.py` based on `WHISPERLIVE_WIRE_FORMAT` |
| Backend | faster_whisper (CT2) | `--backend faster_whisper` in unit |
| Model | large-v3 | `--model large-v3` in unit |
| VAD | server-side, on (per-client) | client handshake `use_vad=True` (not a server CLI flag) |
| Quantisation | int8_float16 (~1.5GB VRAM) | NOT yet a flag in this unit — see below |

### Quantisation knob

The spec pins `int8_float16` to keep WhisperLive under ~1.5GB VRAM
so it cohabits with ollama on the same GPU (S7's local-inference
canary). The CT2 backend's `compute_type` knob is set differently
across WhisperLive versions — some expose it as a server CLI
flag, some as an env var, some only in a Python config file.

**Operators MUST verify the quant setting via `nvidia-smi` after
starting the service.** Expected: a single `whisperlive` process
using ~1.5GB. If it's using 3GB+ the default float16 was applied;
look up your installed WhisperLive version's compute_type knob
and add it to `ExecStart`.

### Audio format alignment

The dashboard's browser-side capture (AudioWorklet) emits int16 LE
PCM for bandwidth efficiency. WhisperLive expects either int16 or
float32 depending on how it was installed; the dashboard converts
at the boundary based on
`tools.dashboard.voice_whisperlive.WHISPERLIVE_WIRE_FORMAT`:

| Install source | WhisperLive expects | Set `WHISPERLIVE_WIRE_FORMAT` to |
|---|---|---|
| `pip install whisper-live` (0.8.0+) | float32 LE [-1,1] | `"float32_le"` (default) |
| Upstream GitHub + `--raw_pcm_input` | int16 LE | `"int16_le"` |

Mismatch produces silent garbage transcripts that look like "server
is working but transcribing nonsense". If you see that symptom,
verify the `WHISPERLIVE_WIRE_FORMAT` constant matches your install
path. Operator-changing this constant is a code edit + dashboard
restart, not a runtime setting; if it becomes operator-frequent
worth promoting to Settings.

## Lifecycle (S3 scope)

This slice ships **manual start/stop** plus `Restart=on-failure`
with a 5s backoff. That's enough to keep the service alive across
transient crashes and the operator-known periodic OOM events
during heavy use.

**Out of scope for S3-1** (planned for S10 ops hardening):

- Crash-event logging into the knowledge graph
- Pre-warming on dashboard restart (cold-start is currently
  exposed to the first operator who connects)
- Auto-restart escalation (e.g. systemctl + watchdog)
- Multi-tenant routing or pooling

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `whisperlive.service` failed to start | `User=` still has the placeholder | Edit `/etc/systemd/system/whisperlive.service`, replace `__OPERATOR_USER__` with your account, `systemctl daemon-reload && systemctl restart whisperlive` |
| `whisperlive` binary not found | install path differs from `/usr/local/bin/whisperlive` | `which whisperlive` on the host, edit `ExecStart` to match |
| Service stays in `activating (auto-restart)` | likely CUDA / GPU access issue | `journalctl -t whisperlive -n 100`; verify the User= account owns `/dev/nvidia*` |
| Connect from dashboard returns `whisperlive_connect_failed` | service not running OR port mismatch | `systemctl status whisperlive`; verify `--port 9090` matches `WHISPERLIVE_URL` in `tools/dashboard/voice_whisperlive.py` |
| Connect succeeds but transcripts are nonsense | `WHISPERLIVE_WIRE_FORMAT` doesn't match installed server's expected format | Set to `"float32_le"` for pip 0.8.0, `"int16_le"` for upstream + `--raw_pcm_input`; restart dashboard |
| Cold-start > 30s | model not pre-fetched on disk | first run downloads large-v3 (~3GB); subsequent restarts hit the cache |

## Uninstall

```bash
sudo systemctl stop whisperlive
sudo systemctl disable whisperlive
sudo rm /etc/systemd/system/whisperlive.service
sudo systemctl daemon-reload
```
