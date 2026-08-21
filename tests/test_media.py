from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.media import VISUAL_SIGNAL_POLICY_VERSION, analyze_visual_signals
from video_summary.models import Clip
from video_summary.project import ProjectPaths
from video_summary.utils import VideoSummaryError


FRAME_SIZE = 96 * 54


class _FakeProcess:
    def __init__(self, frames: bytes, return_code: int) -> None:
        self.stdout = io.BytesIO(frames)
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _ExplodingStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        raise RuntimeError("decode stream failed")


class VisualSignalDecodeTests(unittest.TestCase):
    def _clip(self) -> Clip:
        return Clip(
            clip_id="clip_test",
            path="/tmp/test.mp4",
            relative_path="test.mp4",
            fingerprint="fingerprint",
            size_bytes=1,
            duration=10.0,
            captured_at="2026-08-20T10:00:00+09:00",
            capture_source="metadata",
            day_key="2026-08-20",
            travel_day=1,
            width=3840,
            height=2160,
            fps=29.97,
            codec="hevc",
            rotation=0,
            has_audio=True,
        )

    def test_hardware_failure_retries_software_without_reusing_partial_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            calls: list[list[str]] = []
            processes = [
                _FakeProcess(bytes([5]) * FRAME_SIZE, 1),
                _FakeProcess(bytes([10]) * FRAME_SIZE + bytes([30]) * FRAME_SIZE, 0),
            ]

            def fake_popen(args: list[str], *, stdout: object, stderr: object) -> _FakeProcess:
                calls.append(args)
                if len(calls) == 1:
                    stderr.write(b"hardware unavailable")  # type: ignore[attr-defined]
                return processes[len(calls) - 1]

            with patch("video_summary.media.subprocess.Popen", side_effect=fake_popen):
                samples = analyze_visual_signals(paths, self._clip(), 3.0)

            self.assertEqual(len(samples), 2)
            self.assertEqual([sample["time"] for sample in samples], [0.0, 3.0])
            self.assertIn("-hwaccel", calls[0])
            self.assertIn("auto", calls[0])
            self.assertNotIn("-hwaccel", calls[1])
            self.assertEqual(calls[0][calls[0].index("-i") + 1], self._clip().path)
            self.assertEqual(calls[1][calls[1].index("-i") + 1], self._clip().path)

            payload = json.loads((paths.signals / "clip_test.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], VISUAL_SIGNAL_POLICY_VERSION)
            self.assertEqual(payload["samples"], samples)

    def test_successful_hardware_decode_is_cached_without_software_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            calls: list[list[str]] = []

            def fake_popen(args: list[str], *, stdout: object, stderr: object) -> _FakeProcess:
                calls.append(args)
                return _FakeProcess(bytes([20]) * FRAME_SIZE, 0)

            with patch("video_summary.media.subprocess.Popen", side_effect=fake_popen):
                first = analyze_visual_signals(paths, self._clip(), 3.0)
                second = analyze_visual_signals(paths, self._clip(), 3.0)

            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertIn("-hwaccel", calls[0])

    def test_both_decode_paths_fail_without_writing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            calls = 0

            def fake_popen(args: list[str], *, stdout: object, stderr: object) -> _FakeProcess:
                nonlocal calls
                calls += 1
                stderr.write(f"failure {calls}".encode())  # type: ignore[attr-defined]
                return _FakeProcess(b"", 1)

            with patch("video_summary.media.subprocess.Popen", side_effect=fake_popen):
                with self.assertRaisesRegex(VideoSummaryError, "hardware: failure 1"):
                    analyze_visual_signals(paths, self._clip(), 3.0)

            self.assertEqual(calls, 2)
            self.assertFalse((paths.signals / "clip_test.json").exists())

    def test_unexpected_stream_error_terminates_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            process = _FakeProcess(b"", 0)
            process.stdout = _ExplodingStream()

            with patch("video_summary.media.subprocess.Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "decode stream failed"):
                    analyze_visual_signals(paths, self._clip(), 3.0)

            self.assertTrue(process.terminated)
            self.assertTrue(process.stdout.closed)


if __name__ == "__main__":
    unittest.main()
