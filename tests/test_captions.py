from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.captions import (
    _cache_key,
    _cohere_processor_inputs,
    _load_or_generate_clip_transcript,
    _read_pcm16_mono,
    transcribe_project_clips,
)
from video_summary.clip_results import load_clip_result


class CaptionsTests(unittest.TestCase):
    def test_read_pcm16_mono_requires_numpy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.wav"
            import wave

            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00")

            with patch("video_summary.captions.importlib.util.find_spec", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "numpy is required"):
                    _read_pcm16_mono(path)

    def test_cohere_processor_inputs_prefers_transcription_request(self) -> None:
        class FakeProcessor:
            def __init__(self) -> None:
                self.called = None

            def apply_transcription_request(self, **kwargs):
                self.called = ("apply", kwargs)
                return {"ok": True}

            def __call__(self, **kwargs):
                self.called = ("call", kwargs)
                return {"fallback": True}

        processor = FakeProcessor()
        payload = _cohere_processor_inputs(processor, [0.0] * 8, "hello")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(processor.called[0], "apply")
        self.assertEqual(processor.called[1]["sampling_rate"], 16000)

    def test_load_or_generate_clip_transcript_reuses_empty_cached_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clip_path = root / "clip.mp4"
            clip_path.write_bytes(b"")
            cache_dir = root / "captions"
            cache_dir.mkdir(parents=True)
            taxonomy = {"context": "", "terms": [], "prompt_terms": [], "replacement_rules": []}

            with patch("video_summary.captions.taxonomy_signature", return_value="sig"):
                cache_path = cache_dir / f"{_cache_key(clip_path, 'cohere-local:medium:ko-KR:sig')}.json"
                cache_path.write_text(
                    json.dumps({"provider": "cache", "transcript": []}, ensure_ascii=False),
                    encoding="utf-8",
                )

                with patch("video_summary.captions._transcribe_with_cohere_transformers") as mocked_transcribe:
                    transcript, provider = _load_or_generate_clip_transcript(
                        clip_path,
                        cache_dir,
                        speech_locale="ko-KR",
                        model_size="medium",
                        taxonomy=taxonomy,
                    )

            self.assertEqual(transcript, [])
            self.assertEqual(provider, "cache")
            mocked_transcribe.assert_not_called()

    def test_transcribe_project_clips_allows_empty_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            build_dir.mkdir(parents=True)
            clip_one = root / "clip-1.mp4"
            clip_two = root / "clip-2.mp4"
            clip_one.write_bytes(b"")
            clip_two.write_bytes(b"")
            taxonomy = {"context": "", "terms": [], "prompt_terms": [], "replacement_rules": []}
            brief = {}
            transcripts = {
                str(clip_one.resolve()): ([{"start": 0.0, "end": 1.0, "text": "hello"}], "cache"),
                str(clip_two.resolve()): ([], "cache"),
            }

            def fake_load_or_generate(clip_path: Path, *_args, **_kwargs):
                return transcripts[str(clip_path.resolve())]

            with (
                patch("video_summary.captions.load_project_taxonomy", return_value=taxonomy),
                patch("video_summary.captions.load_project_metadata", return_value=brief),
                patch("video_summary.captions._load_or_generate_clip_transcript", side_effect=fake_load_or_generate),
                patch("video_summary.captions.taxonomy_signature", return_value="sig"),
            ):
                summary = transcribe_project_clips(
                    build_dir,
                    project_name="sample",
                    clip_paths=[str(clip_one), str(clip_two)],
                    model_size="medium",
                )

            self.assertEqual(summary["provider"], "cache")
            self.assertEqual(summary["model"], "medium")
            self.assertEqual(summary["clip_count"], 2)
            self.assertEqual(summary["transcribed_clip_count"], 1)
            self.assertEqual(summary["empty_transcript_count"], 1)

            clip_one_result = load_clip_result(build_dir, str(clip_one.resolve()))
            clip_two_result = load_clip_result(build_dir, str(clip_two.resolve()))
            self.assertEqual(clip_one_result["transcript_status"], "ok")
            self.assertEqual(clip_two_result["transcript_status"], "empty")

            transcripts_payload = json.loads((build_dir / "transcripts.json").read_text(encoding="utf-8"))
            clips = {item["clip_path"]: item for item in transcripts_payload["clips"]}
            self.assertEqual(clips[str(clip_two.resolve())]["transcript"], [])


if __name__ == "__main__":
    unittest.main()
