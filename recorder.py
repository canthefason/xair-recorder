#!/usr/bin/env python3
"""Core recording logic for the X Air multitrack recorder.

Captures all input channels from a Behringer X-Air series mixer (XR12, XR16,
XR18, X18, ...) over USB as a single interleaved WAV file via ALSA's
arecord, and provides start/stop/status operations shared by the CLI and the
web UI.

CARD_NAME and CHANNELS default to the XR18 (the device this was built and
tested against) but work with any X-Air device via environment variables -
no code changes needed:

    XAIR_CARD_NAME  - the ALSA card name (check with `arecord -l`)
    XAIR_CHANNELS   - how many channels that device sends over USB (this is
                      configurable on the mixer itself and isn't always the
                      same as its total input count)

Sample format/rate are NOT configurable: verified against a real XR18, the
device hard-rejects any format but S24_3LE, and silently ignores any rate
request other than 48000Hz (falls back to 48000Hz rather than erroring, per
`arecord`'s own warning). Making those "configurable" would just be a knob
that's either a no-op or actively wrong, so they're fixed constants instead.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

CARD_NAME = os.environ.get("XAIR_CARD_NAME", "XR18")
CHANNELS = int(os.environ.get("XAIR_CHANNELS", "18"))
SAMPLE_FORMAT = "S24_3LE"  # 24-bit packed in 3 bytes
BYTES_PER_SAMPLE = 3
SAMPLE_RATE = 48000

MIN_FREE_SECONDS = 600  # refuse to start unless this much recording time fits

# Below this peak level (dBFS, via ffmpeg's volumedetect), a split channel is
# treated as "nothing was plugged into this input" and left out of the zip
# bundle - not a config knob, just the line between silence and a real signal.
SILENCE_THRESHOLD_DB = -50.0

RECORDINGS_DIR = Path(os.environ.get("XAIR_REC_DIR", str(Path.home() / "recordings")))
STATE_FILE = RECORDINGS_DIR / ".recorder_state.json"

BYTES_PER_SECOND = CHANNELS * BYTES_PER_SAMPLE * SAMPLE_RATE

NETWORK_DIR = Path(__file__).resolve().parent / "network"
WIFI_DEVICE = os.environ.get("XAIR_WIFI_DEVICE", "wlan0")
AP_CONN_NAME = "xair-ap"

NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")
MAX_NAME_LENGTH = 50
# Matches a recording's filename stem, capturing the fixed timestamp part so
# rename_recording() can replace the label after it without disturbing the
# timestamp recordings are expected to sort by.
RECORDING_STEM_RE = re.compile(r"^xair-(\d{8}-\d{6})(?:-.*)?$")


class RecorderError(Exception):
    pass


def find_card_index(name: str = CARD_NAME) -> int:
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RecorderError(f"could not list ALSA capture devices: {exc}") from exc

    for line in out.splitlines():
        # e.g. "card 1: X18XR18 [X18XR18], device 0: USB Audio [USB Audio]"
        if f": {name} [" in line:
            return int(line.split("card ")[1].split(":")[0])
    raise RecorderError(f"ALSA card '{name}' not found - is the mixer plugged in and powered on?")


def _read_state():
    if not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not _pid_alive(state.get("pid")):
        return None
    return state


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def _write_state(state):
    STATE_FILE.write_text(json.dumps(state))


def _clear_state():
    STATE_FILE.unlink(missing_ok=True)


def disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _sanitize_recording_name(name: str) -> str:
    """Turn user-supplied free text into a safe filename suffix: only
    letters/digits/hyphen/underscore, everything else (spaces, punctuation)
    collapsed to a single hyphen, leading/trailing hyphens trimmed."""
    cleaned = NAME_SANITIZE_RE.sub("-", name.strip()).strip("-")
    if not cleaned:
        raise RecorderError("recording name must contain at least one letter, digit, '-' or '_'")
    return cleaned[:MAX_NAME_LENGTH]


def start() -> dict:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    if _read_state() is not None:
        raise RecorderError("a recording is already in progress")

    card = find_card_index()

    free = disk_free_bytes(RECORDINGS_DIR)
    if free < BYTES_PER_SECOND * MIN_FREE_SECONDS:
        minutes_available = free // BYTES_PER_SECOND // 60
        raise RecorderError(
            f"not enough free disk space: only ~{minutes_available} min of recording time left"
        )

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = RECORDINGS_DIR / f"xair-{timestamp}.wav"

    cmd = [
        "arecord",
        "-D", f"hw:{card}",
        "-c", str(CHANNELS),
        "-f", SAMPLE_FORMAT,
        "-r", str(SAMPLE_RATE),
        "-t", "wav",
        str(filename),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # webui.py is a long-running process, so arecord's exit is never collected
    # unless something calls wait() on it - without this it stops correctly
    # but lingers as a zombie forever, which makes stop()'s liveness check
    # (os.kill(pid, 0), true for zombies) time out even though it's done.
    threading.Thread(target=proc.wait, daemon=True).start()

    # Give arecord a moment to fail fast (e.g. device busy) before reporting success.
    time.sleep(0.5)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise RecorderError(f"arecord exited immediately: {stderr.strip()}")

    state = {"pid": proc.pid, "file": str(filename), "started_at": time.time()}
    _write_state(state)
    return state


def stop(timeout: float = 10.0) -> dict:
    state = _read_state()
    if state is None:
        raise RecorderError("no recording in progress")

    pid = state["pid"]
    os.kill(pid, signal.SIGINT)

    deadline = time.time() + timeout
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.2)

    if _pid_alive(pid):
        raise RecorderError("recording process did not stop in time; check it manually")

    _clear_state()
    state["duration_seconds"] = time.time() - state["started_at"]
    return state


def status() -> dict:
    state = _read_state()
    free = disk_free_bytes(RECORDINGS_DIR) if RECORDINGS_DIR.exists() else disk_free_bytes(Path.home())
    info = {
        "recording": state is not None,
        "free_minutes": round(free / BYTES_PER_SECOND / 60, 1),
        "network_mode": network_mode(),
    }
    if state is not None:
        info["file"] = state["file"]
        info["elapsed_seconds"] = round(time.time() - state["started_at"], 1)
    return info


def network_mode() -> str:
    """Whether the WiFi device is broadcasting the venue AP, joined to some
    other ("home") network, or in an unreadable/disconnected state.

    Deliberately doesn't hardcode a "home network" name: any active
    connection on the WiFi device that isn't our own AP profile counts as
    "home", so this works on whatever network the device happens to use.
    """
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"

    active = [line.rsplit(":", 1)[0] for line in out.splitlines() if line.endswith(f":{WIFI_DEVICE}")]
    if AP_CONN_NAME in active:
        return "venue"
    if active:
        return "home"
    return "unknown"


def switch_network(mode: str) -> None:
    """Switch wlan0 between the home WiFi profile and the venue access point.

    Runs synchronously (nmcli connection down/up), so callers whose response
    has to survive the switch (e.g. a web request arriving over the network
    being torn down) must reply to the client BEFORE calling this.
    """
    if mode not in ("home", "venue"):
        raise RecorderError(f"unknown network mode: {mode}")
    script = NETWORK_DIR / f"{mode}-mode.sh"
    if not script.exists():
        raise RecorderError(f"missing script: {script}")
    result = subprocess.run(["sudo", str(script), WIFI_DEVICE], capture_output=True, text=True)
    if result.returncode != 0:
        raise RecorderError(f"switch to {mode} failed: {result.stderr.strip()}")


def list_recordings() -> list:
    """List recorded WAV files, newest first, for browsing/download in the UI."""
    if not RECORDINGS_DIR.exists():
        return []
    files = []
    for p in RECORDINGS_DIR.glob("*.wav"):
        stat = p.stat()
        files.append({"name": p.name, "size_bytes": stat.st_size, "modified": stat.st_mtime})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


def delete_recording(name: str) -> None:
    safe_name = Path(name).name
    file_path = RECORDINGS_DIR / safe_name
    if safe_name != name or file_path.suffix.lower() != ".wav" or not file_path.is_file():
        raise RecorderError(f"recording not found: {name}")

    state = _read_state()
    if state is not None and Path(state["file"]).name == safe_name:
        raise RecorderError("cannot delete a recording that is currently in progress")

    file_path.unlink()


def rename_recording(old_name: str, new_label: str = None) -> str:
    """Rename a recording's label while keeping its original timestamp
    prefix fixed, so renaming never changes the order recordings sort in.
    Pass new_label=None (or "") to clear a label back to just the timestamp.

    Any cached per-channel split or zip bundle for the old name is dropped
    rather than renamed along with it - they're cheap to regenerate under
    the new name on the next Split/Zip click, which is simpler than keeping
    every file inside them in sync with a new stem."""
    safe_old = Path(old_name).name
    old_path = RECORDINGS_DIR / safe_old
    if safe_old != old_name or old_path.suffix.lower() != ".wav" or not old_path.is_file():
        raise RecorderError(f"recording not found: {old_name}")

    state = _read_state()
    if state is not None and Path(state["file"]).name == safe_old:
        raise RecorderError("cannot rename a recording that is currently in progress")

    match = RECORDING_STEM_RE.match(old_path.stem)
    if not match:
        raise RecorderError(f"unrecognized recording filename: {old_name}")
    timestamp = match.group(1)

    suffix = f"-{_sanitize_recording_name(new_label)}" if new_label else ""
    new_path = RECORDINGS_DIR / f"xair-{timestamp}{suffix}.wav"

    if new_path != old_path and new_path.exists():
        raise RecorderError(f"a recording named {new_path.name} already exists")

    old_stem = old_path.stem
    old_path.rename(new_path)

    channels_dir = RECORDINGS_DIR / f"{old_stem}-channels"
    if channels_dir.is_dir():
        shutil.rmtree(channels_dir)
    zip_path = RECORDINGS_DIR / f"{old_stem}-channels.zip"
    if zip_path.is_file():
        zip_path.unlink()

    return new_path.name


def resolve_recording_path(rel_path: str) -> Path:
    """Resolve a name/relative-path (top-level recording, a channels/ subfile,
    or a zipped channel bundle) to a real file inside RECORDINGS_DIR, refusing
    anything that escapes it."""
    base = RECORDINGS_DIR.resolve()
    candidate = (base / rel_path).resolve()
    if (not candidate.is_relative_to(base)
            or candidate.suffix.lower() not in (".wav", ".zip")
            or not candidate.is_file()):
        raise RecorderError(f"recording not found: {rel_path}")
    return candidate


def split_recording(name: str) -> list:
    """Split a top-level recording into per-channel files, reusing a previous
    split if it's already complete and not older than the source file."""
    safe_name = Path(name).name
    file_path = RECORDINGS_DIR / safe_name
    if safe_name != name or file_path.suffix.lower() != ".wav" or not file_path.is_file():
        raise RecorderError(f"recording not found: {name}")

    channels_dir = RECORDINGS_DIR / f"{file_path.stem}-channels"
    existing = sorted(channels_dir.glob("*.wav")) if channels_dir.exists() else []
    source_mtime = file_path.stat().st_mtime
    if len(existing) == CHANNELS and all(p.stat().st_mtime >= source_mtime for p in existing):
        outputs = existing
    else:
        outputs = [Path(p) for p in split_channels(str(file_path))]

    return [str(p.relative_to(RECORDINGS_DIR)) for p in outputs]


def split_channels(wav_path: str, out_dir: str = None) -> list:
    """Split an interleaved multichannel WAV into one mono WAV per channel."""
    src = Path(wav_path)
    if not src.exists():
        raise RecorderError(f"file not found: {src}")
    out = Path(out_dir) if out_dir else src.parent / f"{src.stem}-channels"
    out.mkdir(parents=True, exist_ok=True)

    # -map_channel was removed in recent ffmpeg builds; use the pan filter
    # instead, one channel per invocation (fine for occasional post-show use).
    outputs = []
    for ch in range(CHANNELS):
        out_file = out / f"{src.stem}-ch{ch + 1:02d}.wav"
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-af", f"pan=mono|c0=c{ch}",
            "-c:a", "pcm_s24le",
            str(out_file),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RecorderError(f"ffmpeg failed splitting channel {ch + 1}: {result.stderr.strip()[-500:]}")
        outputs.append(str(out_file))
    return outputs


def _channel_has_signal(path: Path) -> bool:
    """Peak level of a mono channel file, via ffmpeg's volumedetect filter.
    Defaults to "has signal" if detection fails for any reason, so a
    detection hiccup never silently drops a channel that was actually used."""
    cmd = ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stderr.splitlines():
        if "max_volume:" in line:
            try:
                db = float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                return True
            return db > SILENCE_THRESHOLD_DB
    return True


def _build_zip(channel_files: list, zip_path: Path) -> list:
    """Zip the channel files that have detected signal, dropping silent ones.
    Never ships an empty zip: if detection finds signal on none of them,
    all are kept instead."""
    kept = [p for p in channel_files if _channel_has_signal(p)]
    if not kept:
        kept = channel_files
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in kept:
            zf.write(p, arcname=p.name)
    return kept


def zip_channels(wav_path: str, out_dir: str = None) -> str:
    """Split wav_path into per-channel files (reusing split_channels) and
    bundle the ones with signal into a single zip alongside them."""
    src = Path(wav_path)
    channel_files = [Path(p) for p in split_channels(wav_path, out_dir)]
    zip_path = channel_files[0].parent / f"{src.stem}-channels.zip"
    _build_zip(channel_files, zip_path)
    return str(zip_path)


def zip_recording(name: str) -> str:
    """Zip a top-level recording's per-channel split (channels with no
    detected signal left out) into one file, reusing a previous zip if it's
    already newer than the source recording."""
    safe_name = Path(name).name
    file_path = RECORDINGS_DIR / safe_name
    if safe_name != name or file_path.suffix.lower() != ".wav" or not file_path.is_file():
        raise RecorderError(f"recording not found: {name}")

    split_recording(name)  # ensures the per-channel split exists and is fresh (cached)
    source_mtime = file_path.stat().st_mtime
    zip_path = RECORDINGS_DIR / f"{file_path.stem}-channels.zip"
    if zip_path.is_file() and zip_path.stat().st_mtime >= source_mtime:
        return str(zip_path.relative_to(RECORDINGS_DIR))

    channels_dir = RECORDINGS_DIR / f"{file_path.stem}-channels"
    channel_files = sorted(channels_dir.glob("*.wav"))
    _build_zip(channel_files, zip_path)
    return str(zip_path.relative_to(RECORDINGS_DIR))


def main():
    parser = argparse.ArgumentParser(description="X Air multitrack recorder control")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    split_p = sub.add_parser("split")
    split_p.add_argument("file")
    split_p.add_argument("--out-dir")
    zip_p = sub.add_parser("zip")
    zip_p.add_argument("file")
    zip_p.add_argument("--out-dir")
    rename_p = sub.add_parser("rename")
    rename_p.add_argument("file")
    rename_p.add_argument("--name", help="new label; omit to clear back to just the timestamp")
    sub.add_parser("venue-mode")
    sub.add_parser("home-mode")

    args = parser.parse_args()
    try:
        if args.command == "start":
            print(json.dumps(start(), indent=2))
        elif args.command == "stop":
            print(json.dumps(stop(), indent=2))
        elif args.command == "status":
            print(json.dumps(status(), indent=2))
        elif args.command == "split":
            print(json.dumps(split_channels(args.file, args.out_dir), indent=2))
        elif args.command == "zip":
            print(zip_channels(args.file, args.out_dir))
        elif args.command == "rename":
            print(rename_recording(args.file, args.name))
        elif args.command == "venue-mode":
            switch_network("venue")
            print("switched to venue AP")
        elif args.command == "home-mode":
            switch_network("home")
            print("switched to home WiFi")
    except RecorderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
