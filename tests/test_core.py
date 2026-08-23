from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from video_summary.media import (
    _day_key,
    _filename_datetime,
    _rebase_capture_date,
    infer_capture_time,
    resolve_location,
)
from video_summary.cli import build_parser, parse_target_minutes
from video_summary.models import Clip, TranscriptCue
from video_summary.pipeline import _help_has_flag, analyze_project
from video_summary.project import DEFAULT_CONFIG, _validate_config, load_config, project_paths
from video_summary.state import StateStore
from video_summary.transcribe import (
    WhisperCppTranscriber,
    _can_auto_fallback,
    _model_signature,
    find_transcript_sidecar,
    parse_subtitle,
    transcribe_project,
)
from video_summary.utils import VideoSummaryError


class CoreTests(unittest.TestCase):
    def test_family_interview_preservation_config_requires_boolean(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        self.assertTrue(config["editing"]["preserve_family_interviews"])
        config["editing"]["preserve_family_interviews"] = "yes"
        with self.assertRaisesRegex(VideoSummaryError, "preserve_family_interviews"):
            _validate_config(config)

    def test_meal_event_preservation_config_requires_boolean(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        self.assertTrue(config["editing"]["preserve_meal_events"])
        config["editing"]["preserve_meal_events"] = "yes"
        with self.assertRaisesRegex(VideoSummaryError, "preserve_meal_events"):
            _validate_config(config)

    def test_analyze_rejects_skipped_transcription_when_interviews_are_preserved(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        paths = project_paths("/tmp", "interview-skip-test")
        with self.assertRaisesRegex(VideoSummaryError, "가족 인터뷰"):
            analyze_project(paths, config, skip_transcribe=True)

    def test_analyze_rejects_skipped_transcription_when_meals_are_preserved(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["editing"]["preserve_family_interviews"] = False
        config["editing"]["preserve_meal_events"] = True
        paths = project_paths("/tmp", "meal-skip-test")
        with self.assertRaisesRegex(VideoSummaryError, "식사 사건"):
            analyze_project(paths, config, skip_transcribe=True)

    def test_analyze_allows_skipped_transcription_after_explicit_interview_opt_out(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["editing"]["preserve_family_interviews"] = False
        config["editing"]["preserve_meal_events"] = False
        paths = project_paths("/tmp", "interview-skip-opt-out-test")
        with (
            patch("video_summary.pipeline.load_clips", return_value=[]),
            patch(
                "video_summary.pipeline.transcribe_project",
                return_value={"backend": "skip", "clip_count": 0},
            ) as transcribe,
            patch(
                "video_summary.pipeline.build_candidates",
                return_value={"count": 0},
            ),
        ):
            result = analyze_project(paths, config, skip_transcribe=True)

        self.assertEqual(result["transcription"]["backend"], "skip")
        self.assertTrue(transcribe.call_args.kwargs["skip"])

    def test_prompt_runtime_range_is_inferred(self) -> None:
        self.assertEqual(parse_target_minutes("날짜별 4~6분으로 만들어줘"), 5.0)
        self.assertEqual(parse_target_minutes("약 1시간으로"), 60.0)

    def test_render_accepts_output_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "render", "--project", "trip", "--resolution", "2160p",
                "--episode-mode", "trip", "--destination", "Okinawa",
            ]
        )
        self.assertEqual(args.resolution, "2160p")
        self.assertEqual(args.episode_mode, "trip")
        self.assertEqual(args.destination, "Okinawa")

    def test_planner_images_are_explicit_opt_in(self) -> None:
        parser = build_parser()
        default_args = parser.parse_args(["plan", "--project", "trip", "--planner", "codex"])
        image_args = parser.parse_args(
            ["plan", "--project", "trip", "--planner", "codex", "--planner-images"]
        )
        self.assertFalse(default_args.planner_images)
        self.assertTrue(image_args.planner_images)

    def test_osmo_filename_timestamp_and_invalid_date(self) -> None:
        timezone = ZoneInfo("Asia/Seoul")
        parsed = _filename_datetime("DJI_20260819_013045_001_D.MP4", timezone)
        self.assertEqual(parsed, datetime(2026, 8, 19, 1, 30, 45, tzinfo=timezone))
        self.assertIsNone(_filename_datetime("DJI_20261340_999999_001_D.MP4", timezone))

    def test_capture_time_uses_aware_metadata_and_warns_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DJI_20260819_013045_001_D.MP4"
            path.write_bytes(b"x")
            probe = {"format": {"tags": {"creation_time": "2026-08-18T01:30:45Z"}}}
            captured, source, warnings = infer_capture_time(
                path, Path(path.name), probe, ZoneInfo("Asia/Seoul"), []
            )
        self.assertEqual(source, "metadata")
        self.assertEqual(captured.isoformat(), "2026-08-18T10:30:45+09:00")
        self.assertTrue(warnings)

    def test_invalid_metadata_falls_back_to_filename_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DJI_20260819_013045_001_D.MP4"
            path.write_bytes(b"x")
            probe = {"format": {"tags": {"creation_time": "not-a-timestamp"}}}
            captured, source, warnings = infer_capture_time(
                path, Path(path.name), probe, ZoneInfo("Asia/Seoul"), []
            )
        self.assertEqual(source, "filename")
        self.assertEqual(captured.isoformat(), "2026-08-19T01:30:45+09:00")
        self.assertIn("메타데이터 촬영 시각을 해석할 수 없어 무시했습니다.", warnings)

    def test_invalid_metadata_falls_back_to_mtime_with_and_without_date_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "clip.mp4"
            path.write_bytes(b"x")
            fixed_mtime = datetime(2026, 1, 15, 12, 34, 56, tzinfo=timezone.utc).timestamp()
            os.utime(path, (fixed_mtime, fixed_mtime))
            probe = {"format": {"tags": {"creation_time": "not-a-timestamp"}}}

            captured, source, warnings = infer_capture_time(
                path, Path(path.name), probe, ZoneInfo("Asia/Seoul"), []
            )
            overridden, override_source, override_warnings = infer_capture_time(
                path,
                Path("day") / path.name,
                probe,
                ZoneInfo("Asia/Seoul"),
                [{"match": "day/*", "date": "2026-05-18", "timezone": "America/Los_Angeles"}],
            )

        self.assertEqual(captured.isoformat(), "2026-01-15T21:34:56+09:00")
        self.assertEqual(source, "mtime")
        self.assertEqual(len(warnings), 2)
        self.assertEqual(overridden.isoformat(), "2026-05-18T04:34:56-07:00")
        self.assertEqual(override_source, "date_override:mtime")
        self.assertEqual(len(override_warnings), 2)

    def test_date_override_rebases_utc_clock_to_real_local_date_and_dst(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DJI_20000102005724_0011_D.MP4"
            path.write_bytes(b"x")
            probe = {"format": {"tags": {"creation_time": "2000-01-01T15:57:25.123456Z"}}}
            captured, source, warnings = infer_capture_time(
                path,
                Path("0517-us") / path.name,
                probe,
                ZoneInfo("Asia/Seoul"),
                [{"match": "0517-us/*", "date": "2026-05-17", "timezone": "America/Los_Angeles"}],
            )
        self.assertEqual(captured.isoformat(), "2026-05-17T08:57:25.123456-07:00")
        self.assertEqual(source, "date_override:metadata")
        self.assertFalse(warnings)

        tokyo = _rebase_capture_date(
            datetime.fromisoformat("1970-01-01T23:30:45.123456+00:00"),
            date(2026, 8, 20),
            ZoneInfo("Asia/Tokyo"),
        )
        self.assertEqual(tokyo.isoformat(), "2026-08-20T08:30:45.123456+09:00")

    def test_date_override_uses_literal_filename_time_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DJI_20000103001428_0031_D.MP4"
            path.write_bytes(b"x")
            captured, source, warnings = infer_capture_time(
                path,
                Path("0518") / path.name,
                {},
                ZoneInfo("Asia/Seoul"),
                [{"match": "0518/*", "date": "2026-05-18", "timezone": "America/Los_Angeles"}],
            )
        self.assertEqual(captured.isoformat(), "2026-05-18T00:14:28-07:00")
        self.assertEqual(source, "date_override:filename")
        self.assertFalse(warnings)

    def test_date_override_invalid_metadata_falls_back_to_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DJI_20000103001428_0031_D.MP4"
            path.write_bytes(b"x")
            captured, source, warnings = infer_capture_time(
                path,
                Path("0518") / path.name,
                {"format": {"tags": {"creation_time": "broken"}}},
                ZoneInfo("Asia/Seoul"),
                [{"match": "0518/*", "date": "2026-05-18", "timezone": "America/Los_Angeles"}],
            )
        self.assertEqual(captured.isoformat(), "2026-05-18T00:14:28-07:00")
        self.assertEqual(source, "date_override:filename")
        self.assertIn("메타데이터 촬영 시각을 해석할 수 없어 무시했습니다.", warnings)

    def test_date_override_rejects_ambiguous_or_missing_dst_wall_time(self) -> None:
        cases = (
            ("2024-03-10", "2000-01-01T02:30:00"),
            ("2024-11-03", "2000-01-01T01:30:00"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "clip.mp4"
            path.write_bytes(b"x")
            for target_date, timestamp in cases:
                with self.subTest(target_date=target_date), self.assertRaises(VideoSummaryError):
                    infer_capture_time(
                        path,
                        Path("day") / path.name,
                        {"format": {"tags": {"creation_time": timestamp}}},
                        ZoneInfo("UTC"),
                        [{"match": "day/*", "date": target_date, "timezone": "America/New_York"}],
                    )

    def test_day_start_uses_capture_local_clock(self) -> None:
        timezone = ZoneInfo("America/Los_Angeles")
        self.assertEqual(_day_key(datetime(2026, 5, 18, 3, 59, 59, 999999, tzinfo=timezone), 4), "2026-05-17")
        self.assertEqual(_day_key(datetime(2026, 5, 18, 4, 0, tzinfo=timezone), 4), "2026-05-18")

    def test_location_scan_skips_keyword_only_rule_and_keeps_rule_order(self) -> None:
        rules = [
            {
                "label": "인천국제공항",
                "day_key": "2026-08-20",
                "keywords": ["공항", "탑승"],
            },
            {"label": "오키나와 · 나하", "day_key": "2026-08-20", "match": ["*"]},
            {"label": "다른 날짜", "match": ["*"]},
        ]
        relative = Path("day-02/DJI_0002.MP4")
        self.assertEqual(resolve_location(relative, "2026-08-20", rules), "오키나와 · 나하")
        self.assertEqual(
            resolve_location(relative, "2026-08-20", rules, transcript="공항에 도착했다"),
            "인천국제공항",
        )
        self.assertEqual(
            resolve_location(relative, "2026-08-21", rules, transcript="공항에 도착했다"),
            "다른 날짜",
        )

    def test_empty_keyword_selector_is_not_an_implicit_wildcard(self) -> None:
        relative = Path("day-02/DJI_0002.MP4")
        for keywords in ([], "", [" "]):
            with self.subTest(keywords=keywords):
                self.assertIsNone(
                    resolve_location(
                        relative,
                        "2026-08-20",
                        [{"label": "공항", "keywords": keywords}],
                    )
                )

    def test_config_rejects_nan_and_bad_worker_count(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["editing"]["target_minutes_per_day"] = math.nan
        with self.assertRaises(VideoSummaryError):
            _validate_config(config)

    def test_soft_maximum_defaults_to_ten_minutes_and_is_validated(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["editing"]["soft_max_minutes_per_day"], 10.0)
        for value in (math.nan, 0, 181, True, "10"):
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["editing"]["soft_max_minutes_per_day"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                VideoSummaryError,
                "soft_max_minutes_per_day",
            ):
                _validate_config(config)

    def test_legacy_config_without_soft_maximum_loads_with_the_new_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = project_paths(tmpdir, "legacy")
            paths.ensure()
            paths.config.write_text(
                "version: 1\nediting:\n  target_minutes_per_day: 6.0\n",
                encoding="utf-8",
            )

            config = load_config(paths)

        self.assertEqual(config["editing"]["target_minutes_per_day"], 6.0)
        self.assertEqual(config["editing"]["soft_max_minutes_per_day"], 10.0)

    def test_config_validates_date_override_contract(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["date_overrides"] = [
            {"match": "0518/*", "date": "2026-05-18", "timezone": "America/Los_Angeles"}
        ]
        _validate_config(config)
        for rule in (
            {"match": "0518/*", "date": "not-a-date"},
            {"match": "0518/*", "captured_at": "2026-05-18T10:00:00-07:00", "date": "2026-05-18"},
            {"match": "0518/*", "date": "2026-05-18", "timezone": "Mars/Olympus"},
        ):
            invalid = copy.deepcopy(DEFAULT_CONFIG)
            invalid["date_overrides"] = [rule]
            with self.subTest(rule=rule), self.assertRaises(VideoSummaryError):
                _validate_config(invalid)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["analysis"]["cpu_threads"] = 0
        with self.assertRaises(VideoSummaryError):
            _validate_config(config)

    def test_config_validates_trip_intro_settings(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["render"]["trip_intro_style"] = "card"
        config["render"]["trip_intro_grid_size"] = 6
        config["render"]["trip_intro_animation"] = "static"
        config["render"]["trip_intro_candidate_ids"] = ["cand_one", "cand_two"]
        _validate_config(config)
        for style, candidate_ids in (
            ("collage", []),
            ("mosaic", "cand_one"),
            ("mosaic", ["cand_one", "cand_one"]),
            ("mosaic", [""]),
        ):
            invalid = copy.deepcopy(DEFAULT_CONFIG)
            invalid["render"]["trip_intro_style"] = style
            invalid["render"]["trip_intro_candidate_ids"] = candidate_ids
            with self.subTest(style=style, candidate_ids=candidate_ids), self.assertRaises(VideoSummaryError):
                _validate_config(invalid)
        for grid_size, animation in ((5, "flow"), (9, "flow"), (7, "spin"), (True, "flow")):
            invalid = copy.deepcopy(DEFAULT_CONFIG)
            invalid["render"]["trip_intro_grid_size"] = grid_size
            invalid["render"]["trip_intro_animation"] = animation
            with self.subTest(grid_size=grid_size, animation=animation), self.assertRaises(VideoSummaryError):
                _validate_config(invalid)

    def test_config_validates_destination_override(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["destination"] = "Phu Quoc"
        _validate_config(config)
        for destination in ([], "x" * 101):
            invalid = copy.deepcopy(DEFAULT_CONFIG)
            invalid["project"]["destination"] = destination
            with self.subTest(destination=destination), self.assertRaises(VideoSummaryError):
                _validate_config(invalid)

    def test_subtitle_parser_supports_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "clip.srt"
            path.write_text(
                "1\n00:00:00,250 --> 00:00:01,500\n우와 정말 좋다!\n\n",
                encoding="utf-8",
            )
            cues = parse_subtitle(path)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].start, 0.25)
        self.assertEqual(cues[0].text, "우와 정말 좋다!")

    def test_vtt_without_hours_and_telemetry_srt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "clip.mp4"
            video.write_bytes(b"x")
            video.with_suffix(".srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nFrameCnt : 1 [iso : 100] [shutter : 1/120]\n\n",
                encoding="utf-8",
            )
            vtt = video.with_suffix(".vtt")
            vtt.write_text("WEBVTT\n\n00:01.000 --> 00:03.000\n바다에 도착했다\n\n", encoding="utf-8")
            selected = find_transcript_sidecar(video)
            cues = parse_subtitle(vtt)
        self.assertEqual(selected, vtt)
        self.assertEqual(cues[0].start, 1.0)

    def test_skip_transcription_does_not_require_asr_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "clip.mp4"
            media.write_bytes(b"x")
            paths = project_paths(root, "test")
            paths.ensure()
            clip = Clip(
                clip_id="clip_1", path=str(media), relative_path="clip.mp4", fingerprint="fp",
                size_bytes=1, duration=1.0, captured_at="2026-08-19T10:00:00+09:00",
                capture_source="filename", day_key="2026-08-19", travel_day=1,
                width=320, height=180, fps=30.0, codec="h264", rotation=0, has_audio=False,
            )
            with patch("video_summary.transcribe.resolve_backend", side_effect=AssertionError("must not run")):
                result = transcribe_project(paths, [clip], copy.deepcopy(DEFAULT_CONFIG), skip=True)
        self.assertEqual(result["backend"], "skip")
        self.assertEqual(result["empty_clip_count"], 1)

    def test_auto_asr_falls_back_after_whisper_cpp_runtime_failure(self) -> None:
        class FailingTranscriber:
            name = "whisper.cpp"

            def transcribe(self, _audio_path: Path, _language: str):
                raise VideoSummaryError("broken whisper model")

        class WorkingTranscriber:
            name = "faster-whisper"

            def transcribe(self, _audio_path: Path, _language: str):
                return [TranscriptCue(0.1, 0.8, "도착했다")], "ko"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "clip.mp4"
            media.write_bytes(b"video")
            paths = project_paths(root, "fallback")
            paths.ensure()
            clip = Clip(
                clip_id="clip_1", path=str(media), relative_path="clip.mp4", fingerprint="fp",
                size_bytes=5, duration=1.0, captured_at="2026-08-19T10:00:00+09:00",
                capture_source="filename", day_key="2026-08-19", travel_day=1,
                width=320, height=180, fps=30.0, codec="h264", rotation=0, has_audio=True,
            )

            def backend(requested: str, _analysis: dict) -> str:
                return "whisper.cpp" if requested == "auto" else requested

            def transcriber(name: str, _analysis: dict):
                return FailingTranscriber() if name == "whisper.cpp" else WorkingTranscriber()

            with (
                patch("video_summary.transcribe.resolve_backend", side_effect=backend),
                patch("video_summary.transcribe.create_transcriber", side_effect=transcriber),
                patch("video_summary.transcribe.importlib.util.find_spec", return_value=object()),
                patch("video_summary.transcribe._extract_audio", side_effect=lambda _clip, path: path.write_bytes(b"wav")),
            ):
                result = transcribe_project(paths, [clip], copy.deepcopy(DEFAULT_CONFIG))

            transcript = json.loads((paths.transcripts / "clip_1.json").read_text(encoding="utf-8"))
        self.assertEqual(result["backend"], "faster-whisper")
        self.assertEqual(transcript["provider"], "faster-whisper")

    def test_whisper_cpp_rejects_invalid_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = root / "model.bin"
            audio = root / "audio.wav"
            model.write_bytes(b"model")
            audio.write_bytes(b"wav")
            transcriber = WhisperCppTranscriber(model, None, 1)

            invoked: list[str] = []

            def invalid_output(args: list[str]):
                invoked.extend(args)
                audio.with_suffix(".json").write_text('{"result":{"language":"ko"}}', encoding="utf-8")

            with patch("video_summary.transcribe.run_command", side_effect=invalid_output):
                with self.assertRaisesRegex(VideoSummaryError, "transcription"):
                    transcriber.transcribe(audio, "ko")
            self.assertFalse(audio.with_suffix(".json").exists())
            self.assertIn("--output-json", invoked)
            self.assertNotIn("--output-json-full", invoked)

    def test_whisper_cpp_rejects_string_offsets_and_model_signature_handles_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = root / "model.bin"
            audio = root / "audio.wav"
            model.write_bytes(b"model")
            audio.write_bytes(b"wav")
            transcriber = WhisperCppTranscriber(model, None, 1)
            payload = {
                "result": {"language": "ko"},
                "transcription": [{"offsets": {"from": "0", "to": 500}, "text": "도착"}],
            }

            def invalid_output(_args: list[str]):
                audio.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")

            with patch("video_summary.transcribe.run_command", side_effect=invalid_output):
                with self.assertRaisesRegex(VideoSummaryError, "offsets"):
                    transcriber.transcribe(audio, "ko")

            signature = _model_signature(
                "whisper.cpp",
                {"whisper_cpp_model": str(model), "whisper_cpp_vad_model": str(root)},
            )
        self.assertTrue(signature["model"]["is_file"])
        self.assertFalse(signature["vad_model"]["is_file"])

    def test_whisper_cpp_requires_result_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = root / "model.bin"
            audio = root / "audio.wav"
            model.write_bytes(b"model")
            audio.write_bytes(b"wav")
            transcriber = WhisperCppTranscriber(model, None, 1)

            def invalid_output(_args: list[str]):
                audio.with_suffix(".json").write_text('{"transcription":[]}', encoding="utf-8")

            with patch("video_summary.transcribe.run_command", side_effect=invalid_output):
                with self.assertRaisesRegex(VideoSummaryError, "result"):
                    transcriber.transcribe(audio, "ko")

    def test_doctor_flag_matching_and_fallback_scope_are_exact(self) -> None:
        help_text = "--output-json-full\n--vad-model\n"
        self.assertFalse(_help_has_flag(help_text, "--output-json"))
        self.assertFalse(_help_has_flag(help_text, "--vad"))
        self.assertTrue(_help_has_flag(help_text, "--vad-model"))
        with patch("video_summary.transcribe.importlib.util.find_spec", return_value=object()):
            self.assertTrue(_can_auto_fallback(True, "auto", "whisper.cpp", VideoSummaryError("model")))
            self.assertFalse(_can_auto_fallback(True, "auto", "whisper.cpp", MemoryError()))

    def test_state_fallback_is_not_a_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.sqlite3")
            store.mark_fallback("plan", "key", {"planner": "local"}, "temporary failure")
            self.assertFalse(store.is_complete("plan", "key"))
            self.assertEqual(store.rows()[0]["status"], "fallback")


if __name__ == "__main__":
    unittest.main()
