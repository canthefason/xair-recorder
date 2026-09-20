# Architecture

A small, dependency-free system for capturing all input channels of a
Behringer X-Air series mixer over USB, on any Linux device, with web-based
remote control. This doc explains how the pieces fit together and why
they're built the way they are.

## Components

```
                    ┌─────────────────────────────────────────┐
                    │              Linux device                │
                    │                                           │
  USB  ┌────────┐   │   ┌──────────┐        ┌────────────────┐ │
 ─────▶│ X-Air  │───┼──▶│  ALSA    │◀──────▶│   arecord       │ │
       │ mixer  │   │   │ (kernel) │  spawn  │ (child process) │ │
       └────────┘   │   └──────────┘         └────────┬────────┘ │
                    │                                  │ writes  │
                    │                                  ▼         │
                    │                          recordings/*.wav  │
                    │                                  ▲         │
                    │   ┌──────────────┐   reads/writes │        │
                    │   │  recorder.py │────────────────┘        │
                    │   │ (core logic) │                         │
                    │   └──────┬───────┘                         │
                    │          │ imported by                     │
                    │   ┌──────▼───────┐      HTTP        Browser│
                    │   │   webui.py   │◀─────────────────/phone │
                    │   │ (http.server)│                         │
                    │   └──────────────┘                         │
                    │          │                                 │
                    │          │ subprocess (sudo)                │
                    │   ┌──────▼───────┐                         │
                    │   │ network/*.sh │──▶ nmcli (NetworkManager)│
                    │   └──────────────┘                         │
                    └─────────────────────────────────────────────┘
```

- **`recorder.py`** — all the actual logic: finding the mixer's ALSA card,
  starting/stopping `arecord`, tracking recording state, listing/deleting/
  splitting recordings, and driving the WiFi mode switch. No web/HTTP
  awareness at all — it's a plain module + CLI (`python3 recorder.py
  start|stop|status|split|venue-mode|home-mode`).
- **`webui.py`** — a thin HTTP layer (stdlib `http.server`, no framework) that
  serves a single-page control UI and calls straight into `recorder.py`.
  Deliberately has zero business logic of its own beyond request routing and
  JSON/error-code translation.
- **`network/*.sh`** — small shell scripts wrapping `nmcli` for the
  venue/home WiFi switch. Kept as standalone scripts (not inlined in Python)
  so they can be run directly for debugging/manual use, and because they need
  `sudo` (NetworkManager profile changes) while the web service itself
  shouldn't run as root.
- **`tests/`** — stdlib `unittest`, subprocess calls mocked so tests run
  without ffmpeg/arecord/nmcli/an actual mixer attached.

## Supporting other X-Air devices

`CARD_NAME` and `CHANNELS` in `recorder.py` are the only two device-specific
constants in the whole codebase (confirmed by grepping both `recorder.py`
and `webui.py` for other hardcoded channel counts or device names — there
aren't any). They're read from `XAIR_CARD_NAME`/`XAIR_CHANNELS` environment
variables at import time, defaulting to the XR18's values (`"XR18"`, `18`).
Pointing this at an XR12, XR16, or X18 is a config change, not a code change
— see the README's Environment Variables section.

## Why a state file, not just in-memory state

`recorder.py` is used two ways: imported by the long-running `webui.py`
process, and invoked fresh per command from the CLI (`python3 recorder.py
stop` runs and exits). Since a `start` and a later `stop` might happen in
*different processes* (e.g. start from the web UI, stop from an SSH session),
recording state can't live only in memory — it's persisted to
`<recordings dir>/.recorder_state.json` (pid, output file, start time) so any
invocation can find out what's going on.

`status()`/`_read_state()` treat a state file as stale (returns "not
recording") if the pid it names isn't actually alive, so a crash doesn't
leave the system stuck thinking it's still recording.

## The zombie-process fix

`arecord` is a child process. If nothing ever calls `wait()` on it after it
exits, it becomes a zombie — and critically, `os.kill(pid, 0)` (the "is this
process alive" check used throughout) returns success for zombies too. In the
long-running `webui.py` process this caused a real bug: `stop()` would send
`SIGINT`, the process would actually terminate, but `stop()`'s "is it still
alive" poll loop kept seeing it as alive (zombie) until a 10-second timeout,
reporting a false failure even though the recording had completed correctly.

The fix (`start()` in `recorder.py`): immediately after spawning `arecord`,
a background thread calls `proc.wait()`, so the moment the process actually
exits, it gets reaped right away — regardless of whether/when `stop()` is
ever called from this or another process.

## Path safety for downloads/deletes

Recording filenames are also used directly in URLs (`/recordings/<name>`,
`/recordings/<name>/split`, etc.), so every place that turns user-supplied
input back into a filesystem path validates the result stays inside
`RECORDINGS_DIR`:

- `resolve_recording_path()` resolves the joined path and checks
  `is_relative_to()` against the resolved recordings directory — this is what
  allows a nested channel-split file (`<name>-channels/<name>-ch01.wav`)
  while still rejecting `../../etc/passwd`-style traversal.
- `delete_recording()` and `split_recording()` use the stricter
  `Path(name).name` (no subdirectories at all) since those operate on
  top-level recordings only, never on already-split channel files.

## Splitting into per-channel files

`split_channels()` uses ffmpeg's `pan` filter, one invocation per channel
(`pan=mono|c0=c<N>`) — not the older `-map_channel` flag, which newer ffmpeg
builds have dropped entirely (this broke in production against ffmpeg 7.1.5).

`split_recording()` wraps that with a cheap cache: if a `<name>-channels/`
folder already has the expected number of files and none of them are older
than the source recording, it skips re-running ffmpeg entirely. This matters
because splitting a real multi-hour recording into 18 mono files is not free,
and the web UI's "Split channels" button can reasonably be clicked more than
once.

## WiFi mode switching (venue vs. home)

Live-sound X-Air setups often end up somewhere with no existing network. The
system can turn its own WiFi radio into a standalone access point
(`network/setup-ap-profile.sh`, via `nmcli`), so the control UI is reachable
with zero external infrastructure.

Rather than hardcoding the name of "the normal network" anywhere,
`venue-mode.sh` looks up whatever connection is currently active on the WiFi
device and saves its name to a small state file before switching to the AP
profile; `home-mode.sh` reads that back to restore it. This is what makes the
feature portable across devices/networks without editing any config —
`recorder.py`'s `network_mode()` mirrors this by treating *any* active
non-AP connection as "home", rather than checking for a specific name.

Because switching networks can tear down the very connection carrying the
HTTP request that triggered it, `webui.py`'s `/venue-mode` and `/home-mode`
handlers reply to the client *first*, then run the actual switch after a
short delay on a background timer.

## What's deliberately NOT in this repo

Anything specific to one person's physical setup is kept out of version
control (see `.gitignore`) and provided as templates/examples instead:

- `systemd/xair-recorder.service.template` — filled in per-install by
  `scripts/install-service.sh` (actual user, path, recordings dir).
- `config.mk.example` — copy to `config.mk` (gitignored) for your own
  `make deploy` target host/path; the `Makefile` has generic placeholder
  defaults so it still works untouched, just pointed at nothing useful.
- No hardcoded home-network name anywhere (see above).
