from __future__ import annotations

import array
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from video_summary.models import Candidate, Clip, EditPlan, Episode, PlanSegment, TranscriptCue
from video_summary.project import DEFAULT_CONFIG, ProjectPaths
from video_summary.render_assets import MOSAIC_CAPACITY
from video_summary.renderer import (
    Piece,
    RENDER_POLICY_VERSION,
    SOURCE_RENDER_POLICY_VERSION,
    SourceMember,
    SourceSelection,
    assemble_output,
    cached_piece_is_usable,
    card_fade_seconds,
    coalesce_source_selections,
    episode_pieces,
    legacy_segment_directories,
    render_cache_key,
    render_card_piece,
    render_mosaic_card_piece,
    render_trip_intro_piece,
    select_trip_intro_frames,
    render_source_piece,
    source_output_timing,
    source_cache_namespace,
    write_chapters,
    write_timeline,
    write_trip_day_chapters,
    write_vtt,
)
from video_summary.utils import VideoSummaryError


FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def candidate(
    candidate_id: str,
    captured_at: str,
    start: float,
    *,
    travel_day: int = 1,
    frame_path: str = "",
    visual_quality: float = 0.5,
    score: float = 0.5,
) -> Candidate:
    day_key = (date(2026, 8, 18) + timedelta(days=travel_day)).isoformat()
    return Candidate(
        candidate_id=candidate_id,
        clip_id="clip",
        day_key=day_key,
        travel_day=travel_day,
        start=start,
        end=start + 5.0,
        captured_at=captured_at,
        transcript="",
        roles=["journey"],
        score=score,
        speech_ratio=0.0,
        motion_score=0.5,
        visual_quality=visual_quality,
        location=None,
        frame_path=frame_path,
    )


class RendererTests(unittest.TestCase):
    @staticmethod
    def make_frame(path: Path, color: tuple[int, int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGB", (160, 90), color) as image:
            image.save(path)

    @staticmethod
    def media_probe(
        *,
        frame_count: str = "180",
        frame_rate: str = "30/1",
        video_duration: str = "6.000000",
        audio_duration: str = "6.021333",
        format_duration: str = "6.021333",
    ) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1280,
                    "height": 720,
                    "nb_frames": frame_count,
                    "r_frame_rate": frame_rate,
                    "duration": video_duration,
                },
                {
                    "codec_type": "audio",
                    "sample_rate": "48000",
                    "duration": audio_duration,
                },
            ],
            "format": {"duration": format_duration},
        }

    def assemble_with_probe(self, probe: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = ProjectPaths(root, "trip")
            paths.ensure()
            sources = [root / "source-1.mp4", root / "source-2.mp4"]
            for source in sources:
                source.write_bytes(b"x" * 2048)
            pieces = [Piece(sources[0], 4.0, "one"), Piece(sources[1], 2.0, "two")]
            episode = Episode("2026-08-19", 1, "DAY 1", "", "summary", 6.0, [])
            config = copy.deepcopy(DEFAULT_CONFIG)
            output = paths.exports / "summary.mp4"

            def fake_run(args: list[str], *, cwd: Path | None = None):
                destination = Path(args[-1])
                if not destination.is_absolute() and cwd is not None:
                    destination = Path(cwd) / destination
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"o" * 2048)

            with (
                patch("video_summary.renderer.run_command", side_effect=fake_run),
                patch("video_summary.renderer.probe_media", return_value=probe),
            ):
                return assemble_output(
                    pieces,
                    output,
                    episode,
                    paths,
                    config,
                    paths.render / "assembly",
                    1280,
                    720,
                    False,
                )

    def test_episode_cards_precede_all_sources_and_sources_are_chronological(self) -> None:
        early = candidate("early", "2026-08-19T08:00:00+09:00", 0.0)
        late_hook = candidate("late", "2026-08-19T18:00:00+09:00", 6.0)
        episode = Episode(
            day_key="2026-08-19",
            travel_day=1,
            title="DAY 1 · 서울",
            subtitle="2026-08-19",
            summary="첫날",
            target_duration=10.0,
            segments=[
                PlanSegment(late_hook.candidate_id, "hook", "재미있는 장면"),
                PlanSegment(early.candidate_id, "journey", "출발"),
            ],
        )
        plan = EditPlan("Trip", "", "local", "hash", [episode])
        clip = Clip(
            clip_id="clip", path="/tmp/clip.mp4", relative_path="clip.mp4", fingerprint="fp",
            size_bytes=1, duration=20.0, captured_at=early.captured_at, capture_source="filename",
            day_key=episode.day_key, travel_day=1, width=1920, height=1080, fps=30.0,
            codec="h264", rotation=0, has_audio=True,
        )
        config = copy.deepcopy(DEFAULT_CONFIG)

        def fake_card(_directory: Path, card_id: str, *_args, **_kwargs) -> Piece:
            return Piece(Path(f"/{card_id}.mp4"), 2.0, card_id)

        def fake_source(segment: PlanSegment, item: Candidate, *_args, **_kwargs) -> Piece:
            return Piece(Path(f"/{item.candidate_id}.mp4"), item.duration, item.candidate_id, item, segment)

        with (
            patch("video_summary.renderer.render_card_piece", side_effect=fake_card),
            patch("video_summary.renderer.render_source_piece", side_effect=fake_source),
        ):
            pieces = episode_pieces(
                episode, plan, {"early": early, "late": late_hook}, {"clip": clip}, config,
                Path("/segments"), Path("/cards"), Path("/overlays"),
                1280, 720, 30, "libx264", "4M", True,
                include_intro=True, include_outro=True, force=False,
            )

        self.assertEqual([piece.label for piece in pieces], [
            "intro-day-1", "date-day-1", "early", "late", "outro-day-1",
        ])
        self.assertEqual(pieces[1].day_chapter, "DAY 1 · 2026-08-19 · 서울")

    def test_daily_intro_uses_destination_and_day_while_date_card_stays_unchanged(self) -> None:
        episode = Episode(
            "2026-08-19", 1, "DAY 1 · 서울", "2026-08-19", "첫날", 10.0, []
        )
        plan = EditPlan("internal-project-id", "", "local", "hash", [episode])
        config = copy.deepcopy(DEFAULT_CONFIG)
        calls: list[tuple[str, str, str]] = []

        def fake_card(
            _directory: Path,
            card_id: str,
            title: str,
            subtitle: str,
            *_args,
            **_kwargs,
        ) -> Piece:
            calls.append((card_id, title, subtitle))
            return Piece(Path(f"/{card_id}.mp4"), 2.0, card_id)

        with patch("video_summary.renderer.render_card_piece", side_effect=fake_card):
            episode_pieces(
                episode,
                plan,
                {},
                {},
                config,
                Path("/segments"),
                Path("/cards"),
                Path("/overlays"),
                1280,
                720,
                30,
                "libx264",
                "4M",
                True,
                include_intro=True,
                include_outro=False,
                force=False,
                intro_title="San Francisco",
                intro_subtitle="2026-08-19",
            )

        self.assertEqual(calls[0], ("intro-day-1", "San Francisco", "2026-08-19"))
        self.assertEqual(calls[1], ("date-day-1", "DAY 1 · 서울", "2026-08-19"))

    def test_episode_coalesces_only_compatible_contiguous_sources_and_keeps_day_fades(self) -> None:
        first = candidate("first", "2026-08-19T08:00:00+09:00", 0.0)
        second = candidate("second", "2026-08-19T08:00:00+09:00", 5.0)
        third = candidate("third", "2026-08-19T08:00:00+09:00", 11.0)
        segments = [
            PlanSegment(first.candidate_id, "journey", "", location="Pier 39", caption="Bay"),
            PlanSegment(second.candidate_id, "scenery", "", location=" Pier 39 ", caption=" Bay "),
            PlanSegment(third.candidate_id, "journey", "", location="Pier 39", caption="Bay"),
        ]
        episode = Episode("2026-08-19", 1, "DAY 1", "", "", 15.0, segments)
        plan = EditPlan("Trip", "", "local", "hash", [episode])
        clip = Clip(
            clip_id="clip", path="/tmp/clip.mp4", relative_path="clip.mp4", fingerprint="fp",
            size_bytes=1, duration=20.0, captured_at=first.captured_at, capture_source="filename",
            day_key=episode.day_key, travel_day=1, width=1920, height=1080, fps=30.0,
            codec="h264", rotation=0, has_audio=True,
        )
        render_calls: list[dict[str, object]] = []

        def fake_card(_directory: Path, card_id: str, *_args, **_kwargs) -> Piece:
            return Piece(Path(f"/{card_id}.mp4"), 2.0, card_id)

        def fake_source(segment: PlanSegment, item: Candidate, *_args, **kwargs) -> Piece:
            render_calls.append(kwargs)
            selections = kwargs["coalesced_selections"]
            labels = kwargs["member_labels"]
            start = selections[0].candidate.start
            members = tuple(
                SourceMember(selection.candidate, selection.segment, selection.candidate.start - start, labels[index])
                for index, selection in enumerate(selections)
            )
            return Piece(
                Path(f"/{item.candidate_id}.mp4"),
                selections[-1].candidate.end - start,
                labels[0],
                item,
                segment,
                source_members=members,
            )

        with (
            patch("video_summary.renderer.render_card_piece", side_effect=fake_card),
            patch("video_summary.renderer.render_source_piece", side_effect=fake_source),
        ):
            pieces = episode_pieces(
                episode,
                plan,
                {item.candidate_id: item for item in (first, second, third)},
                {clip.clip_id: clip},
                copy.deepcopy(DEFAULT_CONFIG),
                Path("/segments"),
                Path("/cards"),
                Path("/overlays"),
                1280,
                720,
                30,
                "libx264",
                "4M",
                True,
                include_intro=False,
                include_outro=False,
                force=False,
            )

        self.assertEqual(len(pieces), 3)  # date card + two rendered source pieces
        self.assertEqual(
            [[selection.candidate.candidate_id for selection in call["coalesced_selections"]] for call in render_calls],
            [["first", "second"], ["third"]],
        )
        self.assertEqual(
            [(call["fade_in"], call["fade_out"]) for call in render_calls],
            [(True, True), (True, True)],
        )
        self.assertEqual([member.label for member in pieces[1].source_members], ["Pier 39", "scenery"])

    def test_coalescing_rejects_any_incompatible_source_property(self) -> None:
        first = candidate("first", "2026-08-19T08:00:00+09:00", 0.0)
        second = candidate("second", "2026-08-19T08:00:00+09:00", 5.001)
        base = PlanSegment(first.candidate_id, "journey", "", location="Bay", caption="View", speed=1.0)
        compatible = PlanSegment(second.candidate_id, "scenery", "", location=" Bay ", caption=" View ", speed=1.0)
        groups = coalesce_source_selections([base, compatible], {"first": first, "second": second})
        self.assertEqual([[item.candidate.candidate_id for item in group] for group in groups], [["first", "second"]])

        incompatible_cases: list[tuple[str, Candidate, PlanSegment]] = []
        gap = copy.deepcopy(second)
        gap.start = 5.002
        gap.end = 10.002
        incompatible_cases.append(("gap", gap, compatible))
        other_clip = copy.deepcopy(second)
        other_clip.clip_id = "other"
        incompatible_cases.append(("clip", other_clip, compatible))
        incompatible_cases.append((
            "speed", second,
            PlanSegment(second.candidate_id, "scenery", "", location="Bay", caption="View", speed=1.25),
        ))
        incompatible_cases.append((
            "location", second,
            PlanSegment(second.candidate_id, "scenery", "", location="City", caption="View", speed=1.0),
        ))
        incompatible_cases.append((
            "caption", second,
            PlanSegment(second.candidate_id, "scenery", "", location="Bay", caption="Other", speed=1.0),
        ))
        for label, item, segment in incompatible_cases:
            with self.subTest(label=label):
                result = coalesce_source_selections([base, segment], {"first": first, "second": item})
                self.assertEqual(len(result), 2)

    def test_trip_chapters_are_day_only_and_youtube_valid(self) -> None:
        pieces = [
            Piece(Path("intro"), 4.0, "Trip"),
            Piece(Path("day1"), 3.0, "DAY 1", day_chapter="DAY 1 · 서울"),
            Piece(Path("source1"), 13.0, "journey"),
            Piece(Path("day2"), 3.0, "DAY 2", day_chapter="DAY 2 · 부산"),
            Piece(Path("source2"), 12.0, "food"),
            Piece(Path("day3"), 3.0, "DAY 3", day_chapter="DAY 3 · 제주"),
            Piece(Path("source3"), 12.0, "scenery"),
            Piece(Path("outro"), 4.0, "Outro"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters_path = Path(tmpdir) / "trip.chapters.txt"
            timeline_path = Path(tmpdir) / "trip.timeline.txt"
            chapters = write_trip_day_chapters(chapters_path, pieces)
            timeline = write_timeline(timeline_path, pieces)

        self.assertEqual(chapters, ["00:00 DAY 1 · 서울", "00:20 DAY 2 · 부산", "00:35 DAY 3 · 제주"])
        self.assertEqual(len(timeline), len(pieces))
        self.assertTrue(all("journey" not in line for line in chapters))

    def test_short_trip_omits_invalid_youtube_chapters_but_keeps_timeline(self) -> None:
        pieces = [
            Piece(Path("intro"), 2.0, "Trip"),
            Piece(Path("day1"), 1.0, "DAY 1", day_chapter="DAY 1"),
            Piece(Path("source1"), 2.0, "source"),
            Piece(Path("day2"), 1.0, "DAY 2", day_chapter="DAY 2"),
            Piece(Path("source2"), 2.0, "source"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters_path = Path(tmpdir) / "trip.chapters.txt"
            timeline_path = Path(tmpdir) / "trip.timeline.txt"
            self.assertEqual(write_trip_day_chapters(chapters_path, pieces), [])
            write_timeline(timeline_path, pieces)
            self.assertEqual(chapters_path.read_text(encoding="utf-8"), "")
            self.assertIn("DAY 2", timeline_path.read_text(encoding="utf-8"))

    def test_daily_chapters_select_valid_subset_and_keep_full_timeline(self) -> None:
        pieces = [
            Piece(Path("intro"), 4.0, "Intro"),
            Piece(Path("date"), 3.0, "Date"),
            Piece(Path("early"), 10.0, "Early source"),
            Piece(Path("middle"), 11.0, "Middle source"),
            Piece(Path("late"), 12.0, "Late source"),
            Piece(Path("too-late"), 4.0, "Too late for a 10 second tail"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters_path = Path(tmpdir) / "daily.chapters.txt"
            timeline_path = Path(tmpdir) / "daily.timeline.txt"
            chapters = write_chapters(chapters_path, pieces)
            timeline = write_timeline(timeline_path, pieces)

        self.assertEqual(chapters, [
            "00:00 Intro",
            "00:17 Middle source",
            "00:28 Late source",
        ])
        self.assertEqual(len(timeline), len(pieces))
        self.assertTrue(all("Date" not in line and "Early source" not in line for line in chapters))
        self.assertNotIn("Too late", "\n".join(chapters))

    def test_daily_chapters_are_empty_when_no_valid_three_chapter_subset_exists(self) -> None:
        pieces = [
            Piece(Path("intro"), 4.0, "Intro"),
            Piece(Path("date"), 3.0, "Date"),
            Piece(Path("source"), 12.0, "Source"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters_path = Path(tmpdir) / "daily.chapters.txt"
            self.assertEqual(write_chapters(chapters_path, pieces), [])
            self.assertEqual(chapters_path.read_text(encoding="utf-8"), "")

    def test_card_fade_and_source_cache_scope(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        original = source_cache_namespace(config, 1280, 720, 30, "libx264", "4M")
        config["render"]["intro_seconds"] += 3.0
        config["render"]["date_card_seconds"] += 2.0
        self.assertEqual(original, source_cache_namespace(config, 1280, 720, 30, "libx264", "4M"))
        config["render"]["audio_bitrate"] = "256k"
        self.assertNotEqual(original, source_cache_namespace(config, 1280, 720, 30, "libx264", "4M"))
        config = copy.deepcopy(DEFAULT_CONFIG)
        transitioned = source_cache_namespace(config, 1280, 720, 30, "libx264", "4M")
        config["render"]["transition_seconds"] = 0.0
        self.assertEqual(transitioned, source_cache_namespace(config, 1280, 720, 30, "libx264", "4M"))
        self.assertEqual(card_fade_seconds("intro-day-1", 4.0), 0.65)
        self.assertEqual(card_fade_seconds("date-day-1", 3.0), 0.5)
        self.assertEqual(card_fade_seconds("trip-outro", 0.4), 0.1)

    def test_audio_sanitizing_policy_invalidates_render_and_source_caches(self) -> None:
        self.assertEqual(RENDER_POLICY_VERSION, 16)
        self.assertEqual(SOURCE_RENDER_POLICY_VERSION, 8)

    def test_regular_cards_use_integer_frame_durations_without_accumulated_drift(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("video_summary.renderer.cached_piece_is_usable", return_value=False),
            patch("video_summary.renderer.create_card"),
            patch("video_summary.renderer.render_still_image_piece") as render_still,
        ):
            cards = Path(tmpdir)
            pieces = [
                render_card_piece(
                    cards,
                    f"card-{index}",
                    "Title",
                    "Subtitle",
                    2.91,
                    1280,
                    720,
                    30,
                    "libx264",
                    "4M",
                    config,
                    False,
                )
                for index in range(20)
            ]

        self.assertTrue(all(piece.duration == 2.9 for piece in pieces))
        self.assertAlmostEqual(sum(piece.duration for piece in pieces), 58.0)
        self.assertTrue(all(call.args[3] == 2.9 for call in render_still.call_args_list))

    def test_render_cache_key_tracks_music_file_contents_not_just_path(self) -> None:
        plan = EditPlan("Trip", "", "local", "hash", [])
        config = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmpdir:
            music = Path(tmpdir) / "music.wav"
            music.write_bytes(b"first soundtrack")
            config["render"]["music_file"] = str(music)
            first = render_cache_key(
                plan, [], config["render"], "daily", False,
                1280, 720, 30, "libx264", "4M", version=RENDER_POLICY_VERSION,
            )
            music.write_bytes(b"other soundtrack")
            second = render_cache_key(
                plan, [], config["render"], "daily", False,
                1280, 720, 30, "libx264", "4M", version=RENDER_POLICY_VERSION,
            )

        self.assertNotEqual(first, second)

    def test_intro_metadata_changes_render_cache_but_not_source_namespace(self) -> None:
        plan = EditPlan("Trip", "", "local", "hash", [])
        config = copy.deepcopy(DEFAULT_CONFIG)
        first_metadata = {
            "destination": "Okinawa",
            "period": "2026-05-07 — 2026-05-10",
            "destination_source": "source_dir",
        }
        second_metadata = {**first_metadata, "destination": "오키나와", "destination_source": "config"}
        first = render_cache_key(
            plan,
            [],
            config["render"],
            "trip",
            False,
            1280,
            720,
            30,
            "libx264",
            "4M",
            version=RENDER_POLICY_VERSION,
            intro_metadata=first_metadata,
        )
        second = render_cache_key(
            plan,
            [],
            config["render"],
            "trip",
            False,
            1280,
            720,
            30,
            "libx264",
            "4M",
            version=RENDER_POLICY_VERSION,
            intro_metadata=second_metadata,
        )
        source_before = source_cache_namespace(config, 1280, 720, 30, "libx264", "4M")
        config["project"]["destination"] = "오키나와"
        source_after = source_cache_namespace(config, 1280, 720, 30, "libx264", "4M")

        self.assertNotEqual(first, second)
        self.assertEqual(source_before, source_after)

    def test_cached_piece_requires_exact_frames_fps_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cached = Path(tmpdir) / "piece.mp4"
            cached.write_bytes(b"x" * 2048)
            cases = [
                ("exact", self.media_probe(), True),
                ("one frame short", self.media_probe(frame_count="179"), False),
                ("wrong fps", self.media_probe(frame_rate="30000/1001"), False),
                ("short video", self.media_probe(video_duration="5.900000"), False),
                ("long audio", self.media_probe(audio_duration="6.100000", format_duration="6.100000"), False),
                (
                    "stream durations unavailable",
                    self.media_probe(video_duration="N/A", audio_duration="N/A"),
                    True,
                ),
                (
                    "container duration unavailable",
                    self.media_probe(format_duration="N/A"),
                    True,
                ),
            ]
            for label, probe, expected in cases:
                with self.subTest(label=label), patch("video_summary.renderer.probe_media", return_value=probe):
                    self.assertEqual(
                        cached_piece_is_usable(
                            cached,
                            1280,
                            720,
                            expected_frame_count=180,
                            expected_duration=6.0,
                            expected_fps=30,
                        ),
                        expected,
                    )

    def test_assembled_output_accepts_one_aac_frame_tail(self) -> None:
        report = self.assemble_with_probe(self.media_probe())
        self.assertEqual(report["duration"], 6.021)

    def test_assembled_output_uses_timestamp_fallbacks_for_na_durations(self) -> None:
        probe = self.media_probe(video_duration="N/A", audio_duration="N/A", format_duration="N/A")
        video, audio = probe["streams"]
        audio["duration_ts"] = "289024"
        audio["time_base"] = "1/48000"
        report = self.assemble_with_probe(probe)
        self.assertEqual(report["duration"], 6.021)

    def test_assembled_output_rejects_timeline_drift_over_one_frame(self) -> None:
        probe = self.media_probe(video_duration="6.040000", audio_duration="6.040000", format_duration="6.040000")
        with self.assertRaisesRegex(VideoSummaryError, "1프레임 이상"):
            self.assemble_with_probe(probe)

    def test_assembled_output_rejects_large_audio_video_drift(self) -> None:
        probe = self.media_probe(audio_duration="6.080000", format_duration="6.080000")
        with self.assertRaisesRegex(VideoSummaryError, "오디오/비디오"):
            self.assemble_with_probe(probe)

    def test_failed_validation_preserves_existing_export_with_and_without_music(self) -> None:
        for with_music in (False, True):
            with self.subTest(with_music=with_music), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = ProjectPaths(root, "trip")
                paths.ensure()
                source = root / "piece.mp4"
                source.write_bytes(b"p" * 2048)
                output = paths.exports / "summary.mp4"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"known-good-export")
                config = copy.deepcopy(DEFAULT_CONFIG)
                music = root / "music.wav"
                if with_music:
                    music.write_bytes(b"music")
                    config["render"]["music_file"] = str(music)
                episode = Episode("2026-08-19", 1, "DAY 1", "", "", 2.0, [])

                def fake_run(args: list[str], *, cwd: Path | None = None):
                    destination = Path(args[-1])
                    if not destination.is_absolute() and cwd is not None:
                        destination = cwd / destination
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"unvalidated-output" * 128)

                def fake_mix(_assembled: Path, destination: Path, *_args) -> None:
                    destination.write_bytes(b"unvalidated-mix" * 128)

                invalid_probe = self.media_probe()
                invalid_probe["streams"][0]["width"] = 640
                with (
                    patch("video_summary.renderer.run_command", side_effect=fake_run),
                    patch("video_summary.renderer.mix_music", side_effect=fake_mix) as mixed,
                    patch("video_summary.renderer.probe_media", return_value=invalid_probe),
                ):
                    with self.assertRaisesRegex(VideoSummaryError, "해상도"):
                        assemble_output(
                            [Piece(source, 2.0, "source")],
                            output,
                            episode,
                            paths,
                            config,
                            paths.render / "assembly",
                            1280,
                            720,
                            False,
                        )

                self.assertEqual(output.read_bytes(), b"known-good-export")
                self.assertFalse(output.with_name(".summary.partial.mp4").exists())
                self.assertEqual(mixed.called, with_music)

    @unittest.skipUnless(FFMPEG_AVAILABLE, "FFmpeg/FFprobe are required")
    def test_final_audio_is_continuous_across_individually_encoded_aac_pieces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = ProjectPaths(root, "trip")
            paths.ensure()
            pieces: list[Piece] = []
            for index in range(2):
                piece_path = root / f"piece-{index}.mp4"
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=30:d=1",
                        "-f", "lavfi", "-i", "aevalsrc=0.25:s=48000:d=1",
                        "-map", "0:v:0", "-map", "1:a:0", "-frames:v", "30", "-t", "1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2",
                        "-video_track_timescale", "90000", "-y", str(piece_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                pieces.append(Piece(piece_path, 1.0, f"piece {index}"))

            config = copy.deepcopy(DEFAULT_CONFIG)
            config["render"]["fps"] = 30
            output = paths.exports / "summary.mp4"
            assemble_output(
                pieces,
                output,
                Episode("2026-08-19", 1, "DAY 1", "", "", 2.0, []),
                paths,
                config,
                paths.render / "assembly",
                160,
                90,
                False,
            )
            decoded = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
                    "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ar", "48000", "-ac", "1", "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            samples = array.array("h")
            samples.frombytes(decoded)
            boundary = 48000
            window = samples[boundary - 4800:boundary + 4800]
            longest_quiet_run = 0
            current_quiet_run = 0
            for sample in window:
                if abs(sample) < 500:
                    current_quiet_run += 1
                    longest_quiet_run = max(longest_quiet_run, current_quiet_run)
                else:
                    current_quiet_run = 0

            self.assertGreater(max(abs(sample) for sample in window), 3000)
            self.assertLess(longest_quiet_run, 480)  # No AAC-frame-scale silence at the boundary.

    def test_source_piece_has_exact_frame_timing_and_soft_transition(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        item = candidate("source", "2026-08-19T08:00:00+09:00", 0.0)
        item.end = 5.98
        segment = PlanSegment(item.candidate_id, "journey", "")
        clip = Clip(
            clip_id="clip", path="/source.mp4", relative_path="source.mp4", fingerprint="fp",
            size_bytes=1, duration=20.0, captured_at=item.captured_at, capture_source="filename",
            day_key=item.day_key, travel_day=1, width=1920, height=1080, fps=29.97,
            codec="h264", rotation=0, has_audio=True,
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("video_summary.renderer.cached_piece_is_usable", side_effect=[False, True]),
            patch("video_summary.renderer.run_command") as run,
            patch("video_summary.renderer.os.replace"),
        ):
            piece = render_source_piece(
                segment, item, clip, Path(tmpdir), Path(tmpdir), 1280, 720, 30,
                "libx264", "4M", config, location_overlay=None, fade_in=True, fade_out=True, force=False,
            )
        args = run.call_args.args[0]
        filters = args[args.index("-filter_complex") + 1]
        self.assertEqual(source_output_timing(5.98, 30), (179, 179 / 30))
        self.assertAlmostEqual(piece.duration, 179 / 30)
        self.assertNotIn("-frames:v", args)
        self.assertIn("trim=end_frame=179", filters)
        self.assertIn("fade=t=in", filters)
        self.assertIn("fade=t=out", filters)
        self.assertIn("afade=t=in", filters)
        self.assertIn("afade=t=out", filters)
        self.assertIn(
            "loudnorm=I=-16:LRA=11:TP=-1.5,aresample=48000:osf=s16,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
            filters,
        )
        self.assertIn("iw*sar", filters)
        self.assertIn("setsar=1", filters)

    @unittest.skipUnless(FFMPEG_AVAILABLE, "FFmpeg/FFprobe are required")
    def test_source_piece_renders_exact_silence_without_non_finite_aac_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "silent-source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=2.2",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.2",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-y", str(source),
                ],
                check=True,
                capture_output=True,
            )
            item = candidate("silent", "2026-08-19T08:00:00+09:00", 0.0)
            item.end = 2.135
            segment = PlanSegment(item.candidate_id, "journey", "")
            clip = Clip(
                clip_id="clip", path=str(source), relative_path=source.name,
                fingerprint="silent-source-fingerprint", size_bytes=source.stat().st_size,
                duration=2.2, captured_at=item.captured_at, capture_source="filename",
                day_key=item.day_key, travel_day=1, width=320, height=180, fps=30.0,
                codec="h264", rotation=0, has_audio=True, audio_sample_rate=48000,
            )
            config = copy.deepcopy(DEFAULT_CONFIG)
            segments = root / "segments"
            overlays = root / "overlays"
            segments.mkdir()
            overlays.mkdir()

            piece = render_source_piece(
                segment,
                item,
                clip,
                segments,
                overlays,
                320,
                180,
                30,
                "libx264",
                "1M",
                config,
                location_overlay=None,
                fade_in=True,
                fade_out=True,
                force=False,
            )

            self.assertTrue(piece.path.is_file())
            self.assertEqual(source_output_timing(2.135, 30), (64, 64 / 30))
            self.assertAlmostEqual(piece.duration, 64 / 30)

    @unittest.skipUnless(FFMPEG_AVAILABLE, "FFmpeg/FFprobe are required")
    def test_source_piece_preserves_late_audio_start_and_normalizes_sar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "offset-source.mkv"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=720x480:r=30:d=2",
                    "-itsoffset", "0.478", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=1.5",
                    "-map", "0:v:0", "-map", "1:a:0", "-vf", "setsar=8/9",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le",
                    "-y", str(source),
                ],
                check=True,
                capture_output=True,
            )
            item = candidate("offset", "2026-08-19T08:00:00+09:00", 0.0)
            item.end = 2.0
            segment = PlanSegment(item.candidate_id, "journey", "")
            clip = Clip(
                clip_id="clip", path=str(source), relative_path=source.name,
                fingerprint="offset-sar-fingerprint", size_bytes=source.stat().st_size,
                duration=2.0, captured_at=item.captured_at, capture_source="filename",
                day_key=item.day_key, travel_day=1, width=720, height=480, fps=30.0,
                codec="h264", rotation=0, has_audio=True,
            )
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["render"]["transition_seconds"] = 0.0
            segments = root / "segments"
            overlays = root / "overlays"
            segments.mkdir()
            overlays.mkdir()
            piece = render_source_piece(
                segment,
                item,
                clip,
                segments,
                overlays,
                1280,
                720,
                30,
                "libx264",
                "2M",
                config,
                location_overlay=None,
                fade_in=False,
                fade_out=False,
                force=False,
            )

            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(piece.path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout)
            video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
            self.assertEqual(video["sample_aspect_ratio"], "1:1")

            decoded_frame = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1.0",
                    "-i", str(piece.path), "-frames:v", "1", "-pix_fmt", "rgb24",
                    "-f", "rawvideo", "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            middle_row = decoded_frame[360 * 1280 * 3:(360 + 1) * 1280 * 3]
            blue_pixels = [
                x
                for x in range(1280)
                if middle_row[x * 3 + 2] - middle_row[x * 3] > 100
            ]
            self.assertAlmostEqual(min(blue_pixels), 160, delta=4)
            self.assertAlmostEqual(max(blue_pixels), 1119, delta=4)

            decoded = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(piece.path),
                    "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ar", "48000", "-ac", "1", "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            samples = array.array("h")
            samples.frombytes(decoded)
            first_audible = next(index for index, sample in enumerate(samples) if abs(sample) > 500)
            self.assertAlmostEqual(first_audible / 48000, 0.478, delta=0.06)

    def test_contiguous_source_piece_seeks_once_and_preserves_member_offsets(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = candidate("first", "2026-08-19T08:00:00+09:00", 10.0)
        first.end = 15.0
        second = candidate("second", "2026-08-19T08:00:00+09:00", 15.0)
        second.end = 20.0
        first_segment = PlanSegment(first.candidate_id, "journey", "", speed=1.0)
        second_segment = PlanSegment(second.candidate_id, "scenery", "", speed=1.0)
        clip = Clip(
            clip_id="clip", path="/source.mp4", relative_path="source.mp4", fingerprint="fp",
            size_bytes=1, duration=30.0, captured_at=first.captured_at, capture_source="filename",
            day_key=first.day_key, travel_day=1, width=1920, height=1080, fps=29.97,
            codec="h264", rotation=0, has_audio=True,
        )
        selections = (SourceSelection(first, first_segment), SourceSelection(second, second_segment))
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("video_summary.renderer.cached_piece_is_usable", side_effect=[False, True]),
            patch("video_summary.renderer.run_command") as run,
            patch("video_summary.renderer.os.replace"),
        ):
            piece = render_source_piece(
                first_segment,
                first,
                clip,
                Path(tmpdir),
                Path(tmpdir),
                1280,
                720,
                30,
                "libx264",
                "4M",
                config,
                location_overlay=None,
                fade_in=True,
                fade_out=True,
                force=False,
                coalesced_selections=selections,
                member_labels=("journey", "scenery"),
            )

        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-ss") + 1], "10.000")
        self.assertIn("trim=end_frame=300", args[args.index("-filter_complex") + 1])
        self.assertAlmostEqual(piece.duration, 10.0)
        self.assertEqual(
            [(member.candidate.candidate_id, member.offset, member.label) for member in piece.source_members],
            [("first", 0.0, "journey"), ("second", 5.0, "scenery")],
        )

    def test_contiguous_piece_preserves_each_source_range_in_vtt_and_timeline(self) -> None:
        first = candidate("first", "2026-08-19T08:00:00+09:00", 0.0)
        second = candidate("second", "2026-08-19T08:00:00+09:00", 5.0)
        first_segment = PlanSegment(first.candidate_id, "journey", "")
        second_segment = PlanSegment(second.candidate_id, "scenery", "")
        members = (
            SourceMember(first, first_segment, 0.0, "journey"),
            SourceMember(second, second_segment, 5.0, "scenery"),
        )
        pieces = [
            Piece(Path("card.mp4"), 2.0, "DAY 1"),
            Piece(
                Path("source.mp4"),
                10.0,
                "journey",
                first,
                first_segment,
                source_members=members,
            ),
        ]
        cues = [TranscriptCue(1.0, 2.0, "first cue"), TranscriptCue(6.0, 7.0, "second cue")]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = ProjectPaths(root, "trip")
            paths.ensure()
            vtt_path = root / "summary.vtt"
            timeline_path = root / "summary.timeline.txt"
            with patch("video_summary.renderer.load_transcript", return_value=cues):
                write_vtt(vtt_path, pieces, paths)
            write_timeline(timeline_path, pieces)
            vtt = vtt_path.read_text(encoding="utf-8")
            timeline = timeline_path.read_text(encoding="utf-8")

        self.assertIn("00:00:03.000 --> 00:00:04.000", vtt)
        self.assertIn("00:00:08.000 --> 00:00:09.000", vtt)
        self.assertEqual(vtt.count("first cue"), 1)
        self.assertEqual(vtt.count("second cue"), 1)
        self.assertIn("00:00:02.000 journey [first]", timeline)
        self.assertIn("00:00:07.000 scenery [second]", timeline)

    def test_custom_caption_overrides_transcript_and_captions_silent_candidate(self) -> None:
        first = candidate("first", "2026-08-19T08:00:00+09:00", 0.0)
        second = candidate("second", "2026-08-19T08:00:00+09:00", 5.0)
        first_segment = PlanSegment(first.candidate_id, "journey", "", caption="  Planner caption  ")
        second_segment = PlanSegment(second.candidate_id, "scenery", "", caption="Silent view")
        piece = Piece(
            Path("source.mp4"),
            10.0,
            "journey",
            first,
            first_segment,
            source_members=(
                SourceMember(first, first_segment, 0.0, "journey"),
                SourceMember(second, second_segment, 5.0, "scenery"),
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = ProjectPaths(root, "trip")
            paths.ensure()
            output = root / "captions.vtt"
            with patch(
                "video_summary.renderer.load_transcript",
                return_value=[TranscriptCue(0.5, 1.5, "original transcript")],
            ) as transcript:
                write_vtt(output, [piece], paths)
            rendered = output.read_text(encoding="utf-8")

        transcript.assert_not_called()
        self.assertIn("00:00:00.000 --> 00:00:05.000\nPlanner caption", rendered)
        self.assertIn("00:00:05.000 --> 00:00:10.000\nSilent view", rendered)
        self.assertNotIn("original transcript", rendered)

    def test_legacy_import_is_scoped_to_exact_render_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            render_root = Path(tmpdir)
            (render_root / "preferred" / "segments").mkdir(parents=True)
            (render_root / "different-config" / "segments").mkdir(parents=True)
            directories = legacy_segment_directories(render_root, "preferred")
        self.assertEqual(directories, (render_root / "preferred" / "segments",))

    def test_trip_intro_selects_one_plan_frame_per_day_and_honors_curated_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates = {
                "day1-high": candidate(
                    "day1-high", "2026-08-19T08:00:00+09:00", 0.0,
                    frame_path="frames/day1-high.jpg", visual_quality=0.95,
                ),
                "day1-curated": candidate(
                    "day1-curated", "2026-08-19T09:00:00+09:00", 1.0,
                    frame_path="frames/day1-curated.jpg", visual_quality=0.6,
                ),
                "day2-corrupt": candidate(
                    "day2-corrupt", "2026-08-20T08:00:00+09:00", 0.0, travel_day=2,
                    frame_path="frames/day2-corrupt.jpg", visual_quality=0.99,
                ),
                "day2-good": candidate(
                    "day2-good", "2026-08-20T09:00:00+09:00", 1.0, travel_day=2,
                    frame_path="frames/day2-good.jpg", visual_quality=0.8,
                ),
                "not-selected": candidate(
                    "not-selected", "2026-08-20T10:00:00+09:00", 2.0, travel_day=2,
                    frame_path="frames/not-selected.jpg", visual_quality=1.0,
                ),
            }
            for name in ("day1-high", "day1-curated", "day2-good", "not-selected"):
                self.make_frame(root / candidates[name].frame_path, (40, 80, 120))
            corrupt_path = root / candidates["day2-corrupt"].frame_path
            self.make_frame(corrupt_path, (200, 40, 40))
            corrupt_path.write_bytes(corrupt_path.read_bytes()[:-1])
            episodes = [
                Episode(
                    "2026-08-19", 1, "DAY 1", "", "", 10.0,
                    [PlanSegment("day1-high", "journey", ""), PlanSegment("day1-curated", "journey", "")],
                ),
                Episode(
                    "2026-08-20", 2, "DAY 2", "", "", 10.0,
                    [PlanSegment("day2-corrupt", "journey", ""), PlanSegment("day2-good", "journey", "")],
                ),
            ]

            selected = select_trip_intro_frames(
                episodes, candidates, root, ["day1-curated", "day2-corrupt"], limit=2
            )

        self.assertEqual([item.candidate_id for item, _path in selected], ["day1-curated", "day2-good"])

    def test_trip_intro_samples_the_whole_trip_when_there_are_more_than_mosaic_capacity_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates: dict[str, Candidate] = {}
            episodes: list[Episode] = []
            for day in range(1, MOSAIC_CAPACITY + 6):
                candidate_id = f"day-{day}"
                day_key = (date(2026, 8, 18) + timedelta(days=day)).isoformat()
                item = candidate(
                    candidate_id,
                    f"{day_key}T08:00:00+09:00",
                    0.0,
                    travel_day=day,
                    frame_path=f"frames/{candidate_id}.jpg",
                )
                candidates[candidate_id] = item
                self.make_frame(root / item.frame_path, ((day * 10) % 255, 80, 120))
                episodes.append(Episode(item.day_key, day, f"DAY {day}", "", "", 5.0, [PlanSegment(candidate_id, "journey", "")]))

            selected = select_trip_intro_frames(episodes, candidates, root)

        selected_days = [item.travel_day for item, _path in selected]
        self.assertEqual(len(selected_days), MOSAIC_CAPACITY)
        self.assertEqual(selected_days[0], 1)
        self.assertEqual(selected_days[-1], MOSAIC_CAPACITY + 5)
        self.assertEqual(selected_days, sorted(selected_days))

    def test_trip_intro_adds_extra_frames_after_covering_every_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates: dict[str, Candidate] = {}
            episodes: list[Episode] = []
            for day in range(1, 4):
                segments: list[PlanSegment] = []
                for position in range(3):
                    candidate_id = f"day-{day}-{position}"
                    item = candidate(
                        candidate_id,
                        f"2026-08-{18 + day:02d}T{8 + position:02d}:00:00+09:00",
                        float(position),
                        travel_day=day,
                        frame_path=f"frames/{candidate_id}.jpg",
                        visual_quality=0.9 - position * 0.1,
                    )
                    candidates[candidate_id] = item
                    self.make_frame(root / item.frame_path, (day * 50, position * 50, 120))
                    segments.append(PlanSegment(candidate_id, "journey", ""))
                episodes.append(Episode(f"2026-08-{18 + day:02d}", day, f"DAY {day}", "", "", 5.0, segments))

            selected = select_trip_intro_frames(
                episodes,
                candidates,
                root,
                ["day-1-1", "day-2-1", "day-3-1", "day-1-2"],
                limit=6,
            )

        selected_ids = [item.candidate_id for item, _path in selected]
        self.assertEqual(len(selected_ids), 6)
        self.assertTrue({"day-1-1", "day-2-1", "day-3-1", "day-1-2"}.issubset(selected_ids))
        self.assertEqual([item.travel_day for item, _path in selected], sorted(item.travel_day for item, _path in selected))

    def test_curated_extras_from_one_day_do_not_displace_balanced_day_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates: dict[str, Candidate] = {}
            episodes: list[Episode] = []
            for day in range(1, 5):
                segments: list[PlanSegment] = []
                for position in range(4):
                    candidate_id = f"day-{day}-{position}"
                    item = candidate(
                        candidate_id,
                        f"2026-08-{18 + day:02d}T{8 + position:02d}:00:00+09:00",
                        float(position),
                        travel_day=day,
                        frame_path=f"frames/{candidate_id}.jpg",
                        visual_quality=0.9 - position * 0.1,
                    )
                    candidates[candidate_id] = item
                    self.make_frame(root / item.frame_path, (day * 40, position * 40, 120))
                    segments.append(PlanSegment(candidate_id, "journey", ""))
                day_key = f"2026-08-{18 + day:02d}"
                episodes.append(Episode(day_key, day, f"DAY {day}", "", "", 5.0, segments))

            selected = select_trip_intro_frames(
                episodes,
                candidates,
                root,
                ["day-1-0", "day-2-0", "day-3-0", "day-4-0", "day-1-1", "day-1-2", "day-1-3"],
                limit=8,
            )

        selected_days = [item.travel_day for item, _path in selected]
        self.assertEqual({day: selected_days.count(day) for day in range(1, 5)}, {1: 2, 2: 2, 3: 2, 4: 2})

    def test_trip_intro_prefers_distinct_source_clips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            items = [
                candidate("a1", "2026-08-19T08:00:00+09:00", 0.0, frame_path="frames/a1.jpg", visual_quality=0.99),
                candidate("a2", "2026-08-19T09:00:00+09:00", 5.0, frame_path="frames/a2.jpg", visual_quality=0.98),
                candidate("b1", "2026-08-19T10:00:00+09:00", 10.0, frame_path="frames/b1.jpg", visual_quality=0.70),
                candidate("c1", "2026-08-19T11:00:00+09:00", 15.0, frame_path="frames/c1.jpg", visual_quality=0.60),
            ]
            items[0].clip_id = items[1].clip_id = "clip-a"
            items[2].clip_id = "clip-b"
            items[3].clip_id = "clip-c"
            for index, item in enumerate(items):
                self.make_frame(root / item.frame_path, (40 + index * 30, 80, 120))
            by_id = {item.candidate_id: item for item in items}
            episode = Episode(
                items[0].day_key, 1, "DAY 1", "", "", 20.0,
                [PlanSegment(item.candidate_id, "journey", "") for item in items],
            )
            selected = select_trip_intro_frames([episode], by_id, root, limit=3)

        self.assertEqual([item.candidate_id for item, _path in selected], ["a1", "b1", "c1"])

    def test_trip_intro_reuses_a_clip_when_distinct_clips_are_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            items = [
                candidate(f"same-{index}", f"2026-08-19T{8 + index:02d}:00:00+09:00", index * 5.0,
                          frame_path=f"frames/same-{index}.jpg", visual_quality=0.9 - index * 0.1)
                for index in range(3)
            ]
            for index, item in enumerate(items):
                item.clip_id = "one-clip"
                self.make_frame(root / item.frame_path, (40 + index * 30, 80, 120))
            by_id = {item.candidate_id: item for item in items}
            episode = Episode(
                items[0].day_key, 1, "DAY 1", "", "", 15.0,
                [PlanSegment(item.candidate_id, "journey", "") for item in items],
            )
            selected = select_trip_intro_frames([episode], by_id, root, limit=3)

        self.assertEqual(len(selected), 3)

    def test_mosaic_cache_key_changes_with_frame_contents_and_order(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["render"]["trip_intro_animation"] = "static"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frame = root / "frame.jpg"
            other_frame = root / "other-frame.jpg"
            cards = root / "cards"
            item = candidate("candidate", "2026-08-19T08:00:00+09:00", 0.0)
            other = candidate("other", "2026-08-19T09:00:00+09:00", 1.0)
            self.make_frame(frame, (255, 0, 0))
            self.make_frame(other_frame, (0, 0, 255))
            with (
                patch("video_summary.renderer.create_mosaic_card"),
                patch("video_summary.renderer.render_still_image_piece"),
                patch("video_summary.renderer.cached_piece_is_usable", return_value=False),
            ):
                first = render_mosaic_card_piece(
                    cards, "trip-intro-mosaic", "Trip", "Dates", 4.0,
                    [(item, frame), (other, other_frame)],
                    1280, 720, 30, "libx264", "4M", config, False,
                )
                self.make_frame(frame, (0, 255, 0))
                second = render_mosaic_card_piece(
                    cards, "trip-intro-mosaic", "Trip", "Dates", 4.0,
                    [(item, frame), (other, other_frame)],
                    1280, 720, 30, "libx264", "4M", config, False,
                )
                reversed_order = render_mosaic_card_piece(
                    cards, "trip-intro-mosaic", "Trip", "Dates", 4.0,
                    [(other, other_frame), (item, frame)],
                    1280, 720, 30, "libx264", "4M", config, False,
                )
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(second.path, reversed_order.path)

    def test_flow_mosaic_uses_configured_grid_and_exact_rendered_duration(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["render"]["trip_intro_grid_size"] = 6
        config["render"]["trip_intro_animation"] = "flow"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frame = root / "frame.jpg"
            self.make_frame(frame, (40, 80, 120))
            item = candidate("candidate", "2026-08-19T08:00:00+09:00", 0.0)
            with patch(
                "video_summary.renderer.render_animated_mosaic",
                return_value=SimpleNamespace(duration=5.0),
            ) as animated:
                piece = render_mosaic_card_piece(
                    root / "cards", "trip-intro-mosaic", "Trip", "Dates", 5.016,
                    [(item, frame)], 1280, 720, 30, "libx264", "4M", config, False,
                )
        self.assertEqual(piece.duration, 5.0)
        self.assertEqual(animated.call_args.kwargs["grid_size"], 6)
        self.assertEqual(animated.call_args.kwargs["animation_style"], "flow")

    def test_flow_mosaic_failure_uses_static_mosaic_before_title_card(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["render"]["trip_intro_animation"] = "flow"
        item = candidate("candidate", "2026-08-19T08:00:00+09:00", 0.0)
        static_piece = Piece(Path("static.mp4"), 4.0, "Trip")
        with (
            patch(
                "video_summary.renderer.select_trip_intro_frames",
                return_value=[(item, Path("frame.jpg"))],
            ),
            patch(
                "video_summary.renderer.render_mosaic_card_piece",
                side_effect=[VideoSummaryError("flow failed"), static_piece],
            ) as mosaic,
            patch("video_summary.renderer.render_card_piece") as title_card,
            patch("video_summary.renderer.print_status") as status,
        ):
            piece, report = render_trip_intro_piece(
                [Episode(item.day_key, 1, "DAY 1", "", "", 5.0, [])],
                {item.candidate_id: item},
                Path("/project"),
                Path("/cards"),
                "Trip",
                "Dates",
                4.0,
                1280,
                720,
                30,
                "libx264",
                "4M",
                config,
                False,
            )

        self.assertIs(piece, static_piece)
        self.assertEqual(mosaic.call_count, 2)
        self.assertEqual(mosaic.call_args_list[0].args[11]["render"]["trip_intro_animation"], "flow")
        self.assertEqual(mosaic.call_args_list[1].args[11]["render"]["trip_intro_animation"], "static")
        title_card.assert_not_called()
        self.assertEqual(report["effective_style"], "mosaic")
        self.assertEqual(report["requested_motion"], "flow")
        self.assertEqual(report["motion"], "static")
        self.assertIn("flow failed", report["fallback_reason"])
        self.assertTrue(any("정적 모자이크 fallback" in call.args[0] for call in status.call_args_list))

    def test_trip_intro_falls_back_to_card_when_no_frame_is_usable(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        fallback = Piece(Path("fallback.mp4"), 4.0, "Trip")
        with (
            patch("video_summary.renderer.select_trip_intro_frames", return_value=[]),
            patch("video_summary.renderer.render_card_piece", return_value=fallback),
        ):
            piece, report = render_trip_intro_piece(
                [], {}, Path("/project"), Path("/cards"), "Trip", "Dates", 4.0,
                1280, 720, 30, "libx264", "4M", config, False,
            )
        self.assertIs(piece, fallback)
        self.assertEqual(report["effective_style"], "card")
        self.assertIn("fallback_reason", report)


if __name__ == "__main__":
    unittest.main()
