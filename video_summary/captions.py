from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .brief import brief_people_terms, brief_prompt_terms, brief_replacement_rules, load_project_metadata
from .clip_results import load_all_clip_results, update_clip_result
from .taxonomy import load_project_taxonomy, taxonomy_signature


DEFAULT_CAPTION_MODEL = "medium"
_MODEL_CACHE: Dict[Tuple[str, str, str], object] = {}


def _provider_name() -> str:
    if importlib.util.find_spec("faster_whisper") is not None:
        return "faster-whisper"
    return "sidecar-only"


def _taxonomy_with_brief(taxonomy: Dict[str, object], brief: Dict[str, object]) -> Dict[str, object]:
    merged = json.loads(json.dumps(taxonomy, ensure_ascii=False))
    prompt_terms = list(merged.get("prompt_terms", [])) + brief_prompt_terms(brief)
    merged["prompt_terms"] = list(dict.fromkeys([str(term).strip() for term in prompt_terms if str(term).strip()]))

    terms = list(merged.get("terms", []))
    existing = {str(term.get("canonical", "")).strip() for term in terms if isinstance(term, dict)}
    for term in brief_people_terms(brief):
        if str(term["canonical"]) not in existing:
            terms.append(term)
    merged["terms"] = terms

    merged["replacement_rules"] = list(merged.get("replacement_rules", [])) + brief_replacement_rules(brief)
    return merged


def _cache_key(clip_path: Path, cache_signature: str) -> str:
    digest = hashlib.sha1(f"{clip_path.resolve()}::{cache_signature}".encode("utf-8")).hexdigest()[:10]
    return f"{clip_path.stem}-{digest}"


def _sidecar_candidates(clip_path: Path) -> List[Path]:
    return [
        Path(f"{clip_path}.captions.json"),
        clip_path.with_suffix(".captions.json"),
    ]


def _normalize_caption_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip(" -")


def _load_caption_json(path: Path) -> List[Dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cues = data.get("captions") or data.get("cues") if isinstance(data, dict) else data
    if not isinstance(cues, list):
        return []
    normalized = []
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        text = _normalize_caption_text(str(cue.get("text", "")))
        if not text:
            continue
        try:
            start = float(cue.get("start", 0.0))
            end = float(cue.get("end", start))
        except (TypeError, ValueError):
            continue
        normalized.append(
            {
                "start": round(max(0.0, start), 3),
                "end": round(max(start, end), 3),
                "text": text,
            }
        )
    return normalized


def _extract_audio(clip_path: Path, audio_path: Path) -> Path:
    if audio_path.exists():
        return audio_path
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(clip_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(audio_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return audio_path


def _taxonomy_prompt(taxonomy: Dict[str, object]) -> str:
    context = str(taxonomy.get("context", "")).strip()
    prompt_terms = [str(item) for item in taxonomy.get("prompt_terms", []) if str(item).strip()]
    terms_from_entries = [
        str(term.get("canonical", "")).strip()
        for term in taxonomy.get("terms", [])
        if isinstance(term, dict) and str(term.get("canonical", "")).strip()
    ]
    terms = list(dict.fromkeys(prompt_terms + terms_from_entries))
    if not terms:
        return context
    prompt = f"주요 고유명사와 용어: {', '.join(terms)}."
    return f"{context} {prompt}".strip()


def _apply_taxonomy(text: str, taxonomy: Dict[str, object]) -> str:
    corrected = text
    alias_rules: List[Tuple[str, str]] = []
    for term in taxonomy.get("terms", []):
        if not isinstance(term, dict):
            continue
        canonical = str(term.get("canonical", "")).strip()
        if not canonical:
            continue
        for alias in term.get("aliases", []):
            alias_text = str(alias).strip()
            if alias_text and alias_text != canonical:
                alias_rules.append((alias_text, canonical))

    alias_rules.sort(key=lambda item: len(item[0]), reverse=True)
    for alias, canonical in alias_rules:
        corrected = corrected.replace(alias, canonical)

    for rule in taxonomy.get("replacement_rules", []):
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("pattern", "")).strip()
        replacement = str(rule.get("replacement", "")).strip()
        if not pattern:
            continue
        corrected = re.sub(pattern, replacement, corrected)

    return corrected.replace("  ", " ").strip()


def _faster_whisper_model(model_size: str):
    from faster_whisper import WhisperModel

    key = ("faster-whisper", model_size, "cpu-int8")
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _MODEL_CACHE[key]


def _transcribe_with_faster_whisper(
    audio_path: Path,
    speech_locale: str,
    model_size: str,
    taxonomy: Dict[str, object],
) -> List[Dict[str, object]]:
    model = _faster_whisper_model(model_size)
    language = speech_locale.split("-", 1)[0].lower()
    segments, _info = model.transcribe(
        str(audio_path),
        beam_size=3,
        word_timestamps=True,
        vad_filter=True,
        language=language,
        condition_on_previous_text=False,
        initial_prompt=_taxonomy_prompt(taxonomy) or None,
    )
    results: List[Dict[str, object]] = []
    for segment in list(segments):
        text = _normalize_caption_text(str(getattr(segment, "text", "")))
        text = _apply_taxonomy(text, taxonomy)
        if not text:
            continue
        results.append(
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
            }
        )
    return results


def _load_or_generate_clip_transcript(
    clip_path: Path,
    cache_dir: Path,
    speech_locale: str,
    model_size: str,
    taxonomy: Dict[str, object],
) -> Tuple[List[Dict[str, object]], str]:
    for sidecar_path in _sidecar_candidates(clip_path):
        if sidecar_path.exists():
            transcript = _load_caption_json(sidecar_path)
            corrected = [{**cue, "text": _apply_taxonomy(str(cue["text"]), taxonomy)} for cue in transcript]
            return corrected, "sidecar"

    cache_signature = f"{model_size}:{speech_locale}:{taxonomy_signature(taxonomy)}"
    cache_path = cache_dir / f"{_cache_key(clip_path, cache_signature)}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        transcript = list(cached.get("transcript", []))
        if transcript:
            return transcript, str(cached.get("provider", "cache"))

    provider = _provider_name()
    if provider != "faster-whisper":
        return [], provider

    audio_path = cache_dir / f"{_cache_key(clip_path, cache_signature)}.wav"
    extracted_audio = _extract_audio(clip_path, audio_path)
    transcript = _transcribe_with_faster_whisper(
        extracted_audio,
        speech_locale,
        model_size,
        taxonomy=taxonomy,
    )
    cache_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "clip_path": str(clip_path),
                "taxonomy_signature": taxonomy_signature(taxonomy),
                "transcript": transcript,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return transcript, provider


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def transcribe_project_clips(
    build_dir: Path,
    project_name: str,
    clip_paths: Iterable[str],
    metadata_path: str | Path | None = None,
    speech_locale: str = "ko-KR",
    model_size: str = DEFAULT_CAPTION_MODEL,
) -> Dict[str, object]:
    cache_dir = build_dir / "captions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    taxonomy = load_project_taxonomy(build_dir, project_name)
    brief = load_project_metadata(build_dir, project_name, metadata_path=metadata_path)
    taxonomy = _taxonomy_with_brief(taxonomy, brief)
    transcripts_path = build_dir / "transcripts.json"

    providers: set[str] = set()
    normalized_paths = sorted({str(Path(path).resolve()) for path in clip_paths if str(path).strip()})
    for clip_path in normalized_paths:
        transcript, provider = _load_or_generate_clip_transcript(
            Path(clip_path),
            cache_dir,
            speech_locale=speech_locale,
            model_size=model_size,
            taxonomy=taxonomy,
        )
        providers.add(provider)
        update_clip_result(
            build_dir,
            clip_path,
            {
                "provider": provider,
                "speech_locale": speech_locale,
                "taxonomy_signature": taxonomy_signature(taxonomy),
                "transcript": transcript,
            },
        )

    partial_results = load_all_clip_results(build_dir)
    _write_json(
        transcripts_path,
        {
            "project": project_name,
            "speech_locale": speech_locale,
            "provider": ", ".join(sorted(providers)) if providers else _provider_name(),
            "clips": [
                {
                    "clip_path": item.get("clip_path"),
                    "filename": item.get("filename"),
                    "transcript": item.get("transcript", []),
                }
                for item in partial_results
            ],
        },
    )
    summary = {
        "provider": ", ".join(sorted(providers)) if providers else _provider_name(),
        "speech_locale": speech_locale,
        "taxonomy_path": str((build_dir / "taxonomy.json").resolve()),
        "taxonomy_signature": taxonomy_signature(taxonomy),
        "transcripts_path": str(transcripts_path.resolve()),
        "clip_count": len(normalized_paths),
    }
    (build_dir / "transcripts_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
