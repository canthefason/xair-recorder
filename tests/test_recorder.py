#!/usr/bin/env python3
"""Unit tests for recorder.py.

Subprocess calls (arecord, ffmpeg, nmcli, the network scripts) are mocked
throughout - these tests exercise our own logic (state handling, path safety,
caching decisions), not the external tools themselves. The one exception is
stop(), which uses a real short-lived `sleep` child process so the actual
SIGINT/zombie-reaping behavior (the source of a real bug we hit in
production) is genuinely exercised rather than assumed.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recorder  # noqa: E402


class RecorderTestCase(unittest.TestCase):
    """Redirects RECORDINGS_DIR/STATE_FILE to a throwaway temp dir per test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self._orig_recordings_dir = recorder.RECORDINGS_DIR
        self._orig_state_file = recorder.STATE_FILE
        recorder.RECORDINGS_DIR = self.tmp_path
        recorder.STATE_FILE = self.tmp_path / ".recorder_state.json"

    def tearDown(self):
        recorder.RECORDINGS_DIR = self._orig_recordings_dir
        recorder.STATE_FILE = self._orig_state_file
        self._tmpdir.cleanup()

    def _touch(self, name, mtime_offset=0):
        recorder.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        p = recorder.RECORDINGS_DIR / name
        p.write_bytes(b"dummy")
        if mtime_offset:
            t = time.time() + mtime_offset
            os.utime(p, (t, t))
        return p


class FindCardIndexTests(RecorderTestCase):
    @patch("recorder.subprocess.run")
    def test_finds_matching_card(self, mock_run):
        mock_run.return_value = MagicMock(stdout=(
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 0: Headphones [bcm2835 Headphones], device 0: ...\n"
            "card 1: XR18 [XR18], device 0: USB Audio [USB Audio]\n"
        ))
        self.assertEqual(recorder.find_card_index("XR18"), 1)

    @patch("recorder.subprocess.run")
    def test_raises_when_not_found(self, mock_run):
        mock_run.return_value = MagicMock(stdout="**** List of CAPTURE Hardware Devices ****\n")
        with self.assertRaises(recorder.RecorderError):
            recorder.find_card_index("XR18")

    @patch("recorder.subprocess.run", side_effect=FileNotFoundError())
    def test_raises_when_arecord_missing(self, mock_run):
        with self.assertRaises(recorder.RecorderError):
            recorder.find_card_index("XR18")


class StartTests(RecorderTestCase):
    def _fake_proc(self, pid=None, exit_code=None, stderr=b""):
        # Use our own test-process pid by default: _read_state() filters out
        # any pid that isn't verifiably alive (via os.kill(pid, 0)), so a
        # made-up pid would make start()'s own state round-trip look broken.
        proc = MagicMock()
        proc.pid = pid if pid is not None else os.getpid()
        proc.poll.return_value = exit_code
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = stderr
        return proc

    @patch("recorder.threading.Thread")
    @patch("recorder.subprocess.Popen")
    @patch("recorder.find_card_index", return_value=0)
    def test_start_writes_state_and_returns_it(self, mock_find, mock_popen, mock_thread):
        mock_popen.return_value = self._fake_proc()
        state = recorder.start()
        self.assertEqual(state["pid"], os.getpid())
        self.assertTrue(recorder.STATE_FILE.exists())
        self.assertEqual(recorder._read_state()["pid"], os.getpid())
        # a reaper thread must be started so arecord's exit gets collected
        # (this is the fix for the zombie-process bug found in production)
        mock_thread.assert_called_once()

    @patch("recorder.find_card_index", return_value=0)
    def test_start_raises_when_already_recording(self, mock_find):
        recorder._write_state({"pid": os.getpid(), "file": "x.wav", "started_at": time.time()})
        with self.assertRaises(recorder.RecorderError):
            recorder.start()

    @patch("recorder.disk_free_bytes", return_value=0)
    @patch("recorder.find_card_index", return_value=0)
    def test_start_raises_when_disk_full(self, mock_find, mock_disk):
        with self.assertRaises(recorder.RecorderError):
            recorder.start()

    @patch("recorder.find_card_index", side_effect=recorder.RecorderError("no card"))
    def test_start_raises_when_no_card(self, mock_find):
        with self.assertRaises(recorder.RecorderError):
            recorder.start()

    @patch("recorder.threading.Thread")
    @patch("recorder.subprocess.Popen")
    @patch("recorder.find_card_index", return_value=0)
    def test_start_raises_when_arecord_exits_immediately(self, mock_find, mock_popen, mock_thread):
        mock_popen.return_value = self._fake_proc(exit_code=1, stderr=b"device busy")
        with self.assertRaises(recorder.RecorderError):
            recorder.start()
        self.assertFalse(recorder.STATE_FILE.exists())


class StopTests(RecorderTestCase):
    def test_stop_raises_when_nothing_recording(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.stop()

    def test_stop_sends_sigint_and_reaps(self):
        # Real child process (not a mock) so the SIGINT + liveness-check path
        # that caused the zombie-process bug in production is truly exercised.
        # The reaper thread mirrors what start() does in production - without
        # it, the process would sit as a zombie (which os.kill(pid, 0) still
        # reports as "alive") and stop() would time out waiting for it, since
        # nothing but the parent calling wait() can actually reap a child.
        proc = subprocess.Popen(["sleep", "5"])
        threading.Thread(target=proc.wait, daemon=True).start()
        recorder._write_state({"pid": proc.pid, "file": "test.wav", "started_at": time.time()})
        result = recorder.stop(timeout=5.0)
        self.assertEqual(result["pid"], proc.pid)
        self.assertIn("duration_seconds", result)
        self.assertFalse(recorder.STATE_FILE.exists())

    def test_stop_raises_if_process_wont_die(self):
        # A pid that stays alive (never actually receives/honors the signal
        # in a way that ends it) must surface as an error, not hang forever.
        with patch("recorder._pid_alive", return_value=True):
            recorder._write_state({"pid": os.getpid(), "file": "test.wav", "started_at": time.time()})
            with patch("recorder.os.kill"):  # don't actually signal our own test process
                with self.assertRaises(recorder.RecorderError):
                    recorder.stop(timeout=0.3)


class StatusTests(RecorderTestCase):
    def test_status_idle(self):
        info = recorder.status()
        self.assertFalse(info["recording"])
        self.assertIn("free_minutes", info)
        self.assertIn("network_mode", info)

    def test_status_recording(self):
        f = recorder.RECORDINGS_DIR / "x.wav"
        recorder.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        recorder._write_state({"pid": os.getpid(), "file": str(f), "started_at": time.time() - 5})
        info = recorder.status()
        self.assertTrue(info["recording"])
        self.assertGreaterEqual(info["elapsed_seconds"], 5)
        self.assertEqual(info["file"], str(f))


class ListDeleteResolveTests(RecorderTestCase):
    def test_list_recordings_sorted_newest_first(self):
        self._touch("a.wav", mtime_offset=-10)
        self._touch("b.wav", mtime_offset=0)
        names = [f["name"] for f in recorder.list_recordings()]
        self.assertEqual(names, ["b.wav", "a.wav"])

    def test_list_recordings_empty_dir(self):
        self.assertEqual(recorder.list_recordings(), [])

    def test_list_recordings_missing_dir(self):
        recorder.RECORDINGS_DIR = self.tmp_path / "does-not-exist"
        self.assertEqual(recorder.list_recordings(), [])

    def test_delete_recording_removes_file(self):
        p = self._touch("del-me.wav")
        recorder.delete_recording("del-me.wav")
        self.assertFalse(p.exists())

    def test_delete_recording_raises_for_missing(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.delete_recording("nope.wav")

    def test_delete_recording_blocks_traversal(self):
        target = self._touch("passwd")  # simulate a real file outside RECORDINGS_DIR by name only
        with self.assertRaises(recorder.RecorderError):
            recorder.delete_recording("../../etc/passwd")
        self.assertTrue(target.exists())  # our unrelated file must be untouched

    def test_delete_recording_blocks_in_progress(self):
        p = self._touch("live.wav")
        recorder._write_state({"pid": os.getpid(), "file": str(p), "started_at": time.time()})
        with self.assertRaises(recorder.RecorderError):
            recorder.delete_recording("live.wav")
        self.assertTrue(p.exists())

    def test_resolve_recording_path_top_level(self):
        p = self._touch("top.wav")
        self.assertEqual(recorder.resolve_recording_path("top.wav"), p.resolve())

    def test_resolve_recording_path_nested_channel_file(self):
        sub = recorder.RECORDINGS_DIR / "top-channels"
        sub.mkdir(parents=True)
        p = sub / "top-ch01.wav"
        p.write_bytes(b"x")
        self.assertEqual(
            recorder.resolve_recording_path("top-channels/top-ch01.wav"), p.resolve()
        )

    def test_resolve_recording_path_blocks_traversal(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.resolve_recording_path("../../../etc/passwd")

    def test_resolve_recording_path_rejects_non_wav(self):
        p = recorder.RECORDINGS_DIR / "notes.txt"
        recorder.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text("hi")
        with self.assertRaises(recorder.RecorderError):
            recorder.resolve_recording_path("notes.txt")

    def test_resolve_recording_path_rejects_missing(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.resolve_recording_path("missing.wav")


class SplitChannelsTests(RecorderTestCase):
    @patch("recorder.subprocess.run")
    def test_builds_one_ffmpeg_call_per_channel(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        src = self._touch("show.wav")
        outputs = recorder.split_channels(str(src))
        self.assertEqual(len(outputs), recorder.CHANNELS)
        self.assertEqual(mock_run.call_count, recorder.CHANNELS)
        first_cmd = mock_run.call_args_list[0].args[0]
        # regression check: -map_channel was removed from recent ffmpeg
        # builds and broke this feature in production - must not come back.
        self.assertNotIn("-map_channel", first_cmd)
        self.assertIn("pan=mono|c0=c0", first_cmd)
        self.assertIn(recorder._FFMPEG_PCM_CODEC[recorder.SAMPLE_FORMAT], first_cmd)

    @patch("recorder.subprocess.run")
    def test_raises_on_ffmpeg_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="boom")
        src = self._touch("show.wav")
        with self.assertRaises(recorder.RecorderError):
            recorder.split_channels(str(src))

    def test_raises_for_missing_source(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.split_channels(str(recorder.RECORDINGS_DIR / "nope.wav"))


class SplitRecordingCachingTests(RecorderTestCase):
    def _write_channel_files(self, stem, content=b"x", mtime=None):
        channels_dir = recorder.RECORDINGS_DIR / f"{stem}-channels"
        channels_dir.mkdir(parents=True, exist_ok=True)
        for i in range(recorder.CHANNELS):
            p = channels_dir / f"{stem}-ch{i + 1:02d}.wav"
            p.write_bytes(content)
            if mtime is not None:
                os.utime(p, (mtime, mtime))
        return channels_dir

    @patch("recorder.split_channels")
    def test_reuses_fresh_cache_without_calling_ffmpeg(self, mock_split):
        self._touch("show.wav")
        self._write_channel_files("show")  # freshly created, newer than source
        result = recorder.split_recording("show.wav")
        mock_split.assert_not_called()
        self.assertEqual(len(result), recorder.CHANNELS)
        self.assertTrue(all(r.startswith("show-channels/") for r in result))

    @patch("recorder.split_channels")
    def test_resplits_when_cache_is_stale(self, mock_split):
        stale = time.time() - 1000
        self._write_channel_files("show", content=b"old", mtime=stale)
        self._touch("show.wav")  # freshly created -> newer than the stale cache
        mock_split.return_value = [
            str(recorder.RECORDINGS_DIR / "show-channels" / f"show-ch{i + 1:02d}.wav")
            for i in range(recorder.CHANNELS)
        ]
        result = recorder.split_recording("show.wav")
        mock_split.assert_called_once()
        self.assertEqual(len(result), recorder.CHANNELS)

    @patch("recorder.split_channels")
    def test_resplits_when_cache_incomplete(self, mock_split):
        self._touch("show.wav")
        self._write_channel_files("show")
        # delete one channel file so the cache is incomplete
        incomplete = sorted((recorder.RECORDINGS_DIR / "show-channels").glob("*.wav"))[0]
        incomplete.unlink()
        mock_split.return_value = [
            str(recorder.RECORDINGS_DIR / "show-channels" / f"show-ch{i + 1:02d}.wav")
            for i in range(recorder.CHANNELS)
        ]
        recorder.split_recording("show.wav")
        mock_split.assert_called_once()

    def test_raises_for_missing_source(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.split_recording("nope.wav")

    def test_blocks_traversal(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.split_recording("../../etc/passwd")


class NetworkTests(RecorderTestCase):
    @patch("recorder.subprocess.run")
    def test_network_mode_home_for_any_non_ap_connection(self, mock_run):
        # No connection name is hardcoded - literally any active connection
        # on the WiFi device that isn't our own AP profile counts as "home".
        mock_run.return_value = MagicMock(stdout="some-random-network-name:wlan0\nlo:lo\n")
        self.assertEqual(recorder.network_mode(), "home")

    @patch("recorder.subprocess.run")
    def test_network_mode_venue(self, mock_run):
        mock_run.return_value = MagicMock(stdout=f"{recorder.AP_CONN_NAME}:wlan0\n")
        self.assertEqual(recorder.network_mode(), "venue")

    @patch("recorder.subprocess.run")
    def test_network_mode_unknown_when_nothing_active_on_wifi_device(self, mock_run):
        mock_run.return_value = MagicMock(stdout="othernet:eth0\n")
        self.assertEqual(recorder.network_mode(), "unknown")

    @patch("recorder.subprocess.run", side_effect=FileNotFoundError())
    def test_network_mode_unknown_when_nmcli_missing(self, mock_run):
        self.assertEqual(recorder.network_mode(), "unknown")

    def test_switch_network_rejects_invalid_mode(self):
        with self.assertRaises(recorder.RecorderError):
            recorder.switch_network("nowhere")

    @patch("recorder.subprocess.run")
    def test_switch_network_raises_on_script_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="nmcli error")
        with self.assertRaises(recorder.RecorderError):
            recorder.switch_network("home")

    @patch("recorder.subprocess.run")
    def test_switch_network_success_runs_correct_script(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        recorder.switch_network("venue")
        cmd = mock_run.call_args.args[0]
        self.assertIn("sudo", cmd)
        self.assertTrue(cmd[1].endswith("venue-mode.sh"))
        self.assertEqual(cmd[2], recorder.WIFI_DEVICE)


class DeviceConfigEnvVarTests(unittest.TestCase):
    """CARD_NAME/CHANNELS are read at import time, so overriding them for a
    different X-Air device (XR12/XR16/X18/...) requires a fresh interpreter -
    exercise that real import path via subprocess rather than monkeypatching
    the already-imported module."""

    def test_defaults_when_unset(self):
        out = subprocess.run(
            [sys.executable, "-c", "import recorder; print(recorder.CARD_NAME, recorder.CHANNELS)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(out, "XR18 18")

    def test_env_vars_override_for_a_different_device(self):
        env = dict(os.environ, XAIR_CARD_NAME="XR16", XAIR_CHANNELS="16")
        out = subprocess.run(
            [sys.executable, "-c", "import recorder; print(recorder.CARD_NAME, recorder.CHANNELS)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        self.assertEqual(out, "XR16 16")

    def test_sample_format_and_rate_default(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import recorder; print(recorder.SAMPLE_FORMAT, recorder.SAMPLE_RATE, recorder.BYTES_PER_SAMPLE)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(out, "S24_3LE 48000 3")

    def test_sample_format_and_rate_overridable(self):
        env = dict(os.environ, XAIR_SAMPLE_FORMAT="S16_LE", XAIR_SAMPLE_RATE="44100")
        out = subprocess.run(
            [sys.executable, "-c",
             "import recorder; print(recorder.SAMPLE_FORMAT, recorder.SAMPLE_RATE, recorder.BYTES_PER_SAMPLE)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        self.assertEqual(out, "S16_LE 44100 2")

    def test_bytes_per_second_reflects_overridden_format(self):
        # This is what disk-space/free-minutes math in start()/status() relies
        # on, so a format override has to actually change the byte math, not
        # just be cosmetic.
        env = dict(os.environ, XAIR_SAMPLE_FORMAT="S16_LE", XAIR_CHANNELS="2", XAIR_SAMPLE_RATE="44100")
        out = subprocess.run(
            [sys.executable, "-c", "import recorder; print(recorder.BYTES_PER_SECOND)"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        self.assertEqual(int(out), 2 * 2 * 44100)

    def test_invalid_sample_format_fails_fast_with_clear_error(self):
        env = dict(os.environ, XAIR_SAMPLE_FORMAT="NOT_A_REAL_FORMAT")
        result = subprocess.run(
            [sys.executable, "-c", "import recorder"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOT_A_REAL_FORMAT", result.stderr)


if __name__ == "__main__":
    unittest.main()
