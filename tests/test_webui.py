#!/usr/bin/env python3
"""Integration tests for webui.py's HTTP routing.

A real ThreadingHTTPServer is started on an ephemeral localhost port so the
actual request parsing/routing/status-code/header logic is exercised end to
end; the `recorder` module's functions (which would otherwise shell out to
arecord/ffmpeg/nmcli) are mocked at the call sites webui.py uses.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recorder  # noqa: E402
import webui  # noqa: E402


class WebUITestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _request(self, path, method="GET", data=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _json_request(self, path, method="GET", data=None):
        status, headers, body = self._request(path, method=method, data=data)
        return status, json.loads(body.decode())


class BasicRoutesTests(WebUITestCase):
    def test_index_serves_html_with_utf8_charset(self):
        status, headers, body = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn("charset=utf-8", headers.get("Content-Type", ""))
        self.assertIn(b"X Air Recorder", body)

    def test_unknown_route_is_404(self):
        status, body = self._json_request("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    @patch("webui.recorder.status")
    def test_status_endpoint_proxies_recorder_status(self, mock_status):
        mock_status.return_value = {"recording": False, "free_minutes": 10.0, "network_mode": "home"}
        status, body = self._json_request("/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["network_mode"], "home")

    def test_logo_served_when_present(self):
        status, headers, body = self._request("/logo.svg")
        # The repo ships a real assets/logo.svg, so this should succeed as-is.
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/svg+xml")

    @patch("webui.LOGO_PATH", Path("/nonexistent/logo.svg"))
    def test_logo_missing_is_404_not_a_crash(self):
        status, body = self._json_request("/logo.svg")
        self.assertEqual(status, 404)


class StartStopTests(WebUITestCase):
    @patch("webui.recorder.start")
    def test_start_success(self, mock_start):
        mock_start.return_value = {"pid": 1, "file": "x.wav", "started_at": 0}
        status, body = self._json_request("/start", method="POST", data=b"")
        self.assertEqual(status, 200)
        self.assertEqual(body["pid"], 1)

    @patch("webui.recorder.start", side_effect=recorder.RecorderError("a recording is already in progress"))
    def test_start_conflict_returns_400_with_message(self, mock_start):
        status, body = self._json_request("/start", method="POST", data=b"")
        self.assertEqual(status, 400)
        self.assertIn("already in progress", body["error"])

    @patch("webui.recorder.stop")
    def test_stop_success(self, mock_stop):
        mock_stop.return_value = {"pid": 1, "duration_seconds": 5.0}
        status, body = self._json_request("/stop", method="POST", data=b"")
        self.assertEqual(status, 200)

    @patch("webui.recorder.stop", side_effect=recorder.RecorderError("no recording in progress"))
    def test_stop_when_idle_returns_400(self, mock_stop):
        status, body = self._json_request("/stop", method="POST", data=b"")
        self.assertEqual(status, 400)


class RecordingsRoutesTests(WebUITestCase):
    @patch("webui.recorder.list_recordings")
    def test_list_recordings(self, mock_list):
        mock_list.return_value = [{"name": "a.wav", "size_bytes": 1, "modified": 0}]
        status, body = self._json_request("/recordings")
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["name"], "a.wav")

    @patch("webui.recorder.resolve_recording_path")
    def test_download_recording_streams_file_with_correct_headers(self, mock_resolve):
        tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            tmp.write_bytes(b"RIFF-fake-wav-data")
            mock_resolve.return_value = tmp
            status, headers, body = self._request(f"/recordings/{tmp.name}")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), "audio/wav")
            self.assertIn(f'filename="{tmp.name}"', headers.get("Content-Disposition", ""))
            self.assertEqual(body, b"RIFF-fake-wav-data")
        finally:
            tmp.unlink()

    @patch("webui.recorder.resolve_recording_path", side_effect=recorder.RecorderError("recording not found: x"))
    def test_download_missing_recording_is_404(self, mock_resolve):
        status, body = self._json_request("/recordings/missing.wav")
        self.assertEqual(status, 404)

    @patch("webui.recorder.delete_recording")
    def test_delete_recording_calls_through_with_decoded_name(self, mock_delete):
        status, body = self._json_request("/recordings/my%20file.wav", method="DELETE")
        self.assertEqual(status, 200)
        mock_delete.assert_called_once_with("my file.wav")

    @patch("webui.recorder.delete_recording", side_effect=recorder.RecorderError("cannot delete a recording that is currently in progress"))
    def test_delete_in_progress_recording_returns_400(self, mock_delete):
        status, body = self._json_request("/recordings/live.wav", method="DELETE")
        self.assertEqual(status, 400)

    def test_delete_non_recordings_path_is_404(self):
        status, body = self._json_request("/other", method="DELETE")
        self.assertEqual(status, 404)

    @patch("webui.recorder.split_recording")
    def test_split_recording(self, mock_split):
        mock_split.return_value = ["a-channels/a-ch01.wav", "a-channels/a-ch02.wav"]
        status, body = self._json_request("/recordings/a.wav/split", method="POST", data=b"")
        self.assertEqual(status, 200)
        self.assertEqual(body["files"], mock_split.return_value)
        mock_split.assert_called_once_with("a.wav")

    @patch("webui.recorder.split_recording", side_effect=recorder.RecorderError("recording not found: a.wav"))
    def test_split_missing_recording_returns_400(self, mock_split):
        status, body = self._json_request("/recordings/a.wav/split", method="POST", data=b"")
        self.assertEqual(status, 400)


class NetworkSwitchTests(WebUITestCase):
    @patch("webui.threading.Timer")
    def test_venue_mode_replies_immediately_and_schedules_switch(self, mock_timer_cls):
        status, body = self._json_request("/venue-mode", method="POST", data=b"")
        self.assertEqual(status, 200)
        self.assertIn("venue", body["message"])
        # The switch must be deferred (not run inline in the request handler),
        # since it can tear down the very network carrying this response.
        mock_timer_cls.assert_called_once()
        delay, fn = mock_timer_cls.call_args.args[0], mock_timer_cls.call_args.args[1]
        self.assertEqual(delay, 2.0)
        self.assertEqual(fn, webui._switch_network_safe)

    @patch("webui.threading.Timer")
    def test_home_mode_replies_immediately_and_schedules_switch(self, mock_timer_cls):
        status, body = self._json_request("/home-mode", method="POST", data=b"")
        self.assertEqual(status, 200)
        self.assertIn("home", body["message"])
        mock_timer_cls.assert_called_once()

    @patch("webui.recorder.switch_network", side_effect=recorder.RecorderError("switch to venue failed: boom"))
    def test_switch_network_safe_swallows_errors_and_logs(self, mock_switch):
        # Must never raise - it runs on a background Timer thread with no
        # caller left to catch an exception.
        try:
            webui._switch_network_safe("venue")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_switch_network_safe raised {exc!r} instead of swallowing it")


if __name__ == "__main__":
    unittest.main()
