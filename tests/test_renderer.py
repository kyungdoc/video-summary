from __future__ import annotations

import copy
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
    SourceMember,
    SourceSelection,
    assemble_output,
    cached_piece_is_usable,
    card_fade_seconds,
    coalesce_source_selections,
    episode_pieces,
    legacy_segment_directories,
    render_mosaic_card_piece,
    render_trip_intro_piece,
    select_trip_intro_frames,
    render_source_piece,
    source_output_timing,
    source_cache_namespace,
    write_timeline,
    write_trip_day_chapters,
    write_vtt,
)
from video_summary.utils import VideoSummaryError


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
            [(True, False), (False, True)],
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
        self.assertEqual(args[args.index("-frames:v") + 1], "179")
        self.assertIn("trim=end_frame=179", filters)
        self.assertIn("fade=t=in", filters)
        self.assertIn("fade=t=out", filters)
        self.assertIn("afade=t=in", filters)
        self.assertIn("afade=t=out", filters)

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
        self.assertEqual(args[args.index("-frames:v") + 1], "300")
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
