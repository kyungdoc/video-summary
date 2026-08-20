from __future__ import annotations

import gc
import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Protocol

from .models import Clip, TranscriptCue
from .project import ProjectPaths
from .state import StateStore
from .utils import VideoSummaryError, command_exists, file_fingerprint, print_status, read_json, run_command, stable_hash, write_json


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: Path, language: str) -> tuple[list[TranscriptCue], str | None]: ...


class _AutoFallbackRequested(Exception):
    """Signals a whisper.cpp model/CLI failure that may be retried with faster-whisper."""


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, model_name: str, cpu_threads: int, offline: bool):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VideoSummaryError("faster-whisper가 없습니다. `uv sync`를 실행하세요.") from exc
        print_status(f"ASR 모델 로드: faster-whisper/{model_name} (CPU int8)")
        kwargs: dict[str, Any] = {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": max(1, cpu_threads),
            "num_workers": 1,
        }
        if offline:
            kwargs["local_files_only"] = True
        self.model = WhisperModel(model_name, **kwargs)

    def transcribe(self, audio_path: Path, language: str) -> tuple[list[TranscriptCue], str | None]:
        selected_language = None if language in {"", "auto"} else language.split("-", 1)[0]
        segments, info = self.model.transcribe(
            str(audio_path),
            language=selected_language,
            beam_size=1,
            best_of=1,
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        cues = [
            TranscriptCue(float(segment.start), float(segment.end), str(segment.text).strip())
            for segment in segments
            if str(segment.text).strip() and float(segment.end) > float(segment.start)
        ]
        return cues, str(getattr(info, "language", selected_language or "")) or None


class WhisperCppTranscriber:
    name = "whisper.cpp"

    def __init__(self, model_path: Path, vad_model_path: Path | None, cpu_threads: int):
        if not model_path.is_file():
            raise VideoSummaryError(f"whisper.cpp 모델이 없습니다: {model_path}")
        if vad_model_path is not None and not vad_model_path.is_file():
            raise VideoSummaryError(f"whisper.cpp VAD 모델이 없습니다: {vad_model_path}")
        self.model_path = model_path
        self.vad_model_path = vad_model_path
        self.cpu_threads = max(1, cpu_threads)

    def transcribe(self, audio_path: Path, language: str) -> tuple[list[TranscriptCue], str | None]:
        output_base = audio_path.with_suffix("")
        output_json = output_base.with_suffix(".json")
        args = [
            "whisper-cli",
            "--model",
            str(self.model_path),
            "--file",
            str(audio_path),
            "--language",
            language or "auto",
            "--output-json",
            "--output-file",
            str(output_base),
            "--processors",
            "1",
            "--threads",
            str(self.cpu_threads),
            "--no-prints",
        ]
        if self.vad_model_path:
            args.extend(["--vad", "--vad-model", str(self.vad_model_path)])
        run_command(args)
        try:
            payload = read_json(output_json)
        finally:
            output_json.unlink(missing_ok=True)
        if not isinstance(payload, dict):
            raise VideoSummaryError("whisper.cpp JSON 최상위 값이 object가 아닙니다.")
        segments = payload.get("transcription")
        if not isinstance(segments, list):
            raise VideoSummaryError("whisper.cpp JSON에 transcription 배열이 없습니다.")
        cues: list[TranscriptCue] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise VideoSummaryError(f"whisper.cpp JSON segment {index}가 object가 아닙니다.")
            offsets = segment.get("offsets", {})
            text_value = segment.get("text")
            if not isinstance(offsets, dict) or not isinstance(text_value, str):
                raise VideoSummaryError(f"whisper.cpp JSON segment {index}의 필드가 잘못되었습니다.")
            raw_start = offsets.get("from")
            raw_end = offsets.get("to")
            if type(raw_start) is not int or type(raw_end) is not int:
                raise VideoSummaryError(f"whisper.cpp JSON segment {index}의 offsets가 잘못되었습니다.")
            start = float(raw_start) / 1000.0
            end = float(raw_end) / 1000.0
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise VideoSummaryError(f"whisper.cpp JSON segment {index}의 시간이 잘못되었습니다.")
            text = text_value.strip()
            if text and end > start:
                cues.append(TranscriptCue(start, end, text))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise VideoSummaryError("whisper.cpp JSON에 result object가 없습니다.")
        language_result = result.get("language")
        if not isinstance(language_result, str) or not language_result.strip():
            raise VideoSummaryError("whisper.cpp JSON result.language가 비어 있거나 문자열이 아닙니다.")
        return cues, language_result.strip()


def transcribe_project(
    paths: ProjectPaths,
    clips: list[Clip],
    config: dict[str, Any],
    *,
    force: bool = False,
    skip: bool = False,
    _allow_auto_fallback: bool = True,
) -> dict[str, Any]:
    analysis = config["analysis"]
    language = str(config["project"].get("language", "auto"))
    transcript_sidecars = {clip.clip_id: find_transcript_sidecar(Path(clip.path)) for clip in clips}
    needs_asr = not skip and any(clip.has_audio and transcript_sidecars[clip.clip_id] is None for clip in clips)
    requested_backend = str(analysis.get("asr_backend", "auto"))
    backend_name = (
        "skip" if skip else resolve_backend(requested_backend, analysis) if needs_asr else "sidecar-only"
    )
    model_name = str(analysis.get("asr_model", "small"))
    model_signature = _model_signature(backend_name, analysis)
    stage_key = stable_hash(
        {
            "version": 3,
            "clips": [(clip.clip_id, clip.fingerprint, clip.has_audio) for clip in clips],
            "backend": backend_name,
            "model": model_signature,
            "language": language,
            "sidecars": [_sidecar_signature(Path(clip.path)) for clip in clips],
        }
    )
    state = StateStore(paths.state)
    all_present = all((paths.transcripts / f"{clip.clip_id}.json").exists() for clip in clips)
    if not force and all_present and state.is_complete("transcribe", stage_key):
        print_status("transcribe: 캐시 사용")
        return {"backend": backend_name, "clip_count": len(clips), "cached": True}

    state.mark_running("transcribe", stage_key, {"backend": backend_name, "model": model_name})
    transcriber: Transcriber | None = None
    try:
        missing = []
        for clip in clips:
            cache_path = paths.transcripts / f"{clip.clip_id}.json"
            cache_key = _clip_cache_key(clip, backend_name, model_signature, language, skip)
            if not force and cache_path.exists() and read_json(cache_path).get("cache_key") == cache_key:
                continue
            if not skip and clip.has_audio and transcript_sidecars[clip.clip_id] is None:
                missing.append(clip)

        if missing:
            try:
                transcriber = create_transcriber(backend_name, analysis)
            except Exception as exc:
                if _can_auto_fallback(_allow_auto_fallback, requested_backend, backend_name, exc):
                    raise _AutoFallbackRequested(str(exc)) from None
                raise

        transcribed = 0
        empty = 0
        for index, clip in enumerate(clips, start=1):
            cache_path = paths.transcripts / f"{clip.clip_id}.json"
            cache_key = _clip_cache_key(clip, backend_name, model_signature, language, skip)
            if not force and cache_path.exists():
                cached = read_json(cache_path)
                if cached.get("cache_key") == cache_key:
                    print_status(f"transcribe {index}/{len(clips)}: {Path(clip.path).name} (캐시)")
                    if cached.get("cues"):
                        transcribed += 1
                    else:
                        empty += 1
                    continue

            print_status(f"transcribe {index}/{len(clips)}: {Path(clip.path).name}")
            cues: list[TranscriptCue] = []
            provider = "none"
            detected_language: str | None = None
            sidecar = transcript_sidecars[clip.clip_id]
            if sidecar:
                cues = parse_subtitle(sidecar)
                provider = f"sidecar:{sidecar.suffix.lower().lstrip('.')}"
            elif not skip and clip.has_audio:
                assert transcriber is not None
                audio_path = paths.transcripts / f".{clip.clip_id}.wav"
                _extract_audio(clip, audio_path)
                try:
                    try:
                        cues, detected_language = transcriber.transcribe(audio_path, language)
                    except Exception as exc:
                        if _can_auto_fallback(_allow_auto_fallback, requested_backend, backend_name, exc):
                            raise _AutoFallbackRequested(str(exc)) from None
                        raise
                    provider = transcriber.name
                finally:
                    audio_path.unlink(missing_ok=True)

            payload = {
                "version": 1,
                "clip_id": clip.clip_id,
                "fingerprint": clip.fingerprint,
                "cache_key": cache_key,
                "provider": provider,
                "model": model_name if provider not in {"none"} and not provider.startswith("sidecar") else None,
                "language": detected_language or language,
                "status": "ok" if cues else "empty",
                "cues": [cue.to_dict() for cue in cues],
            }
            write_json(cache_path, payload)
            if cues:
                transcribed += 1
            else:
                empty += 1

        del transcriber
        gc.collect()
        result = {
            "backend": backend_name,
            "model": model_name,
            "clip_count": len(clips),
            "transcribed_clip_count": transcribed,
            "empty_clip_count": empty,
        }
        state.mark_complete("transcribe", stage_key, result)
        return result
    except _AutoFallbackRequested as exc:
        state.mark_failed("transcribe", stage_key, str(exc))
        print_status(f"whisper.cpp 전사 실패, faster-whisper로 다시 시도합니다: {exc}")
        transcriber = None
        gc.collect()
        fallback_config = dict(config)
        fallback_config["analysis"] = dict(analysis)
        fallback_config["analysis"]["asr_backend"] = "faster-whisper"
        return transcribe_project(
            paths,
            clips,
            fallback_config,
            force=force,
            skip=skip,
            _allow_auto_fallback=False,
        )
    except BaseException as exc:
        state.mark_failed("transcribe", stage_key, str(exc))
        raise


def resolve_backend(requested: str, analysis: dict[str, Any]) -> str:
    requested = requested.strip().lower()
    if requested not in {"auto", "whisper.cpp", "whisper-cpp", "faster-whisper"}:
        raise VideoSummaryError("asr_backend는 auto, whisper.cpp, faster-whisper 중 하나여야 합니다.")
    if requested in {"whisper.cpp", "whisper-cpp"}:
        if not command_exists("whisper-cli"):
            raise VideoSummaryError("whisper-cli를 찾지 못했습니다. `video-summary doctor`를 확인하세요.")
        return "whisper.cpp"
    if requested == "faster-whisper":
        return "faster-whisper"

    model = str(analysis.get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL", "")).strip()
    if command_exists("whisper-cli") and model and Path(model).expanduser().is_file():
        return "whisper.cpp"
    if importlib.util.find_spec("faster_whisper") is not None:
        return "faster-whisper"
    raise VideoSummaryError("사용 가능한 로컬 ASR이 없습니다. whisper.cpp 모델을 설정하거나 `uv sync`를 실행하세요.")


def _can_auto_fallback(
    allowed: bool,
    requested_backend: str,
    backend_name: str,
    error: Exception,
) -> bool:
    return bool(
        allowed
        and not isinstance(error, MemoryError)
        and requested_backend.strip().lower() == "auto"
        and backend_name == "whisper.cpp"
        and importlib.util.find_spec("faster_whisper") is not None
    )


def create_transcriber(backend: str, analysis: dict[str, Any]) -> Transcriber:
    threads = int(analysis.get("cpu_threads", 4))
    if backend == "whisper.cpp":
        model_value = str(analysis.get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL", "")).strip()
        if not model_value:
            raise VideoSummaryError("WHISPER_CPP_MODEL 또는 analysis.whisper_cpp_model을 설정하세요.")
        vad_value = str(analysis.get("whisper_cpp_vad_model") or os.environ.get("WHISPER_CPP_VAD_MODEL", "")).strip()
        return WhisperCppTranscriber(
            Path(model_value).expanduser().resolve(),
            Path(vad_value).expanduser().resolve() if vad_value else None,
            threads,
        )
    return FasterWhisperTranscriber(
        str(analysis.get("asr_model", "small")),
        threads,
        bool(analysis.get("offline", False)),
    )


def load_transcript(paths: ProjectPaths, clip_id: str) -> list[TranscriptCue]:
    path = paths.transcripts / f"{clip_id}.json"
    if not path.exists():
        return []
    return [TranscriptCue.from_dict(item) for item in read_json(path).get("cues", [])]


def find_sidecar(video_path: Path) -> Path | None:
    candidates = sidecar_candidates(video_path)
    return candidates[0] if candidates else None


def sidecar_candidates(video_path: Path) -> list[Path]:
    found: list[Path] = []
    for suffix in (".srt", ".vtt"):
        candidate = video_path.with_suffix(suffix)
        if candidate.exists():
            found.append(candidate)
        upper = video_path.with_suffix(suffix.upper())
        if upper.exists():
            found.append(upper)
    return found


def find_transcript_sidecar(video_path: Path) -> Path | None:
    for sidecar in sidecar_candidates(video_path):
        cues = parse_subtitle(sidecar)
        if cues and not _looks_like_telemetry(cues):
            return sidecar
    return None


def parse_subtitle(path: Path) -> list[TranscriptCue]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    pattern = re.compile(
        r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
        r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})[^\n]*\n"
        r"(?P<text>.*?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    cues: list[TranscriptCue] = []
    for match in pattern.finditer(text):
        content = re.sub(r"<[^>]+>", "", match.group("text"))
        content = " ".join(line.strip() for line in content.splitlines() if line.strip())
        start = _subtitle_time(match.group("start"))
        end = _subtitle_time(match.group("end"))
        if content and end > start:
            cues.append(TranscriptCue(start, end, content))
    return cues


def _subtitle_time(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _looks_like_telemetry(cues: list[TranscriptCue]) -> bool:
    markers = (
        "framecnt", "difftime", "[iso", "[shutter", "[fnum", "[ev", "[ct",
        "[color_md", "[latitude", "[longitude", "gps ", "gb_yaw", "gb_pitch", "gb_roll",
    )
    hits = sum(any(marker in cue.text.casefold() for marker in markers) for cue in cues)
    return bool(cues) and hits / len(cues) >= 0.5


def _extract_audio(clip: Clip, output: Path) -> None:
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            clip.path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output),
        ]
    )


def _sidecar_signature(video_path: Path) -> list[dict[str, str]]:
    return [
        {"path": str(sidecar), "fingerprint": file_fingerprint(sidecar)}
        for sidecar in sidecar_candidates(video_path)
    ]


def _clip_cache_key(clip: Clip, backend: str, model_signature: Any, language: str, skip: bool) -> str:
    return stable_hash(
        {
            "version": 3,
            "clip": clip.fingerprint,
            "backend": "skip" if skip else backend,
            "model": model_signature,
            "language": language,
            "sidecar": _sidecar_signature(Path(clip.path)),
        }
    )


def _model_signature(backend: str, analysis: dict[str, Any]) -> dict[str, Any]:
    if backend != "whisper.cpp":
        return {"backend": backend, "model": str(analysis.get("asr_model", "small"))}
    model_value = str(analysis.get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL", "")).strip()
    vad_value = str(analysis.get("whisper_cpp_vad_model") or os.environ.get("WHISPER_CPP_VAD_MODEL", "")).strip()

    def signature(value: str) -> dict[str, Any] | None:
        if not value:
            return None
        path = Path(value).expanduser()
        try:
            path = path.resolve()
            is_file = path.is_file()
            fingerprint = file_fingerprint(path) if is_file else None
            return {"path": str(path), "is_file": is_file, "fingerprint": fingerprint}
        except OSError as exc:
            return {"path": str(path), "is_file": False, "fingerprint": None, "error": type(exc).__name__}

    return {"backend": backend, "model": signature(model_value), "vad_model": signature(vad_value)}
