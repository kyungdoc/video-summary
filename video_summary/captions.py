from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import wave
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .brief import brief_people_terms, brief_prompt_terms, brief_replacement_rules, load_project_metadata
from .clip_results import load_all_clip_results, update_clip_result
from .taxonomy import load_project_taxonomy, taxonomy_signature


DEFAULT_CAPTION_MODEL = "CohereLabs/cohere-transcribe-03-2026"
_MODEL_CACHE: Dict[Tuple[str, str, str], object] = {}


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


def _normalize_caption_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip(" -")


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


def _speech_language_code(speech_locale: str) -> str:
    return speech_locale.split("-", 1)[0].lower().strip() or "en"


def _read_pcm16_mono(audio_path: Path) -> List[float]:
    with wave.open(str(audio_path), "rb") as wav_file:
        sample_width = wav_file.getsampwidth()
        if sample_width != 2:
            raise RuntimeError(f"Expected 16-bit PCM WAV for transcription input, got sample width {sample_width}.")
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)
    samples = memoryview(raw).cast("h")
    return [max(-1.0, min(1.0, float(value) / 32768.0)) for value in samples]


def _cohere_transformers_components(model_id: str):
    if importlib.util.find_spec("transformers") is None:
        raise RuntimeError(
            "transformers is required for Cohere transcription. Install dependencies with `uv sync` first."
        )
    if importlib.util.find_spec("torch") is None:
        raise RuntimeError(
            "torch is required for Cohere transcription. Install dependencies with `uv sync` first."
        )
    if importlib.util.find_spec("librosa") is None:
        raise RuntimeError(
            "librosa is required for Cohere transcription audio loading. Install dependencies with `uv sync` first."
        )
    import torch
    from transformers import AutoProcessor, CohereAsrForConditionalGeneration

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = torch.float16 if device == "mps" else torch.float32
    key = ("cohere-local", model_id, device)
    if key not in _MODEL_CACHE:
        processor = AutoProcessor.from_pretrained(model_id)
        model = CohereAsrForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype)
        model.to(device)
        model.eval()
        _MODEL_CACHE[key] = (processor, model, device, dtype)
    return _MODEL_CACHE[key]

def _transcribe_with_cohere_transformers(
    audio_path: Path,
    speech_locale: str,
    model_id: str,
    taxonomy: Dict[str, object],
) -> List[Dict[str, object]]:
    processor, model, device, dtype = _cohere_transformers_components(model_id)
    audio = _read_pcm16_mono(audio_path)
    language = _speech_language_code(speech_locale)
    prompt = _taxonomy_prompt(taxonomy)

    inputs = processor(
        audio=audio,
        sampling_rate=16000,
        return_tensors="pt",
        language=language,
        prompt=prompt or None,
    )
    audio_chunk_index = inputs.get("audio_chunk_index")
    inputs = {key: value.to(device=device, dtype=dtype) if hasattr(value, "to") else value for key, value in inputs.items()}

    outputs = model.generate(**inputs, max_new_tokens=256)
    decoded = processor.decode(
        outputs,
        skip_special_tokens=True,
        audio_chunk_index=audio_chunk_index,
        language=language,
    )
    text = decoded[0] if isinstance(decoded, list) else str(decoded)
    text = _apply_taxonomy(_normalize_caption_text(text), taxonomy)
    if not text:
        return []
    duration = 0.0
    with wave.open(str(audio_path), "rb") as wav_file:
        duration = wav_file.getnframes() / float(max(wav_file.getframerate(), 1))
    return [{"start": 0.0, "end": round(duration, 3), "text": text}]


def _load_or_generate_clip_transcript(
    clip_path: Path,
    cache_dir: Path,
    speech_locale: str,
    model_size: str,
    taxonomy: Dict[str, object],
) -> Tuple[List[Dict[str, object]], str]:
    cache_signature = f"cohere-local:{model_size}:{speech_locale}:{taxonomy_signature(taxonomy)}"
    cache_path = cache_dir / f"{_cache_key(clip_path, cache_signature)}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if "transcript" in cached:
            transcript = list(cached.get("transcript", []))
            return transcript, str(cached.get("provider", "cache"))

    audio_path = cache_dir / f"{_cache_key(clip_path, cache_signature)}.wav"
    extracted_audio = _extract_audio(clip_path, audio_path)
    provider = "cohere-local"
    transcript = _transcribe_with_cohere_transformers(
        extracted_audio,
        speech_locale,
        model_size,
        taxonomy=taxonomy,
    )
    cache_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "model": model_size,
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
    empty_transcript_count = 0
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
        if not transcript:
            empty_transcript_count += 1
        update_clip_result(
            build_dir,
            clip_path,
            {
                "provider": provider,
                "transcription_model": model_size,
                "speech_locale": speech_locale,
                "taxonomy_signature": taxonomy_signature(taxonomy),
                "transcript_status": "empty" if not transcript else "ok",
                "transcript": transcript,
            },
        )

    partial_results = load_all_clip_results(build_dir)
    _write_json(
        transcripts_path,
        {
            "project": project_name,
            "speech_locale": speech_locale,
            "provider": ", ".join(sorted(providers)) if providers else "cohere-local",
            "model": model_size,
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
        "provider": ", ".join(sorted(providers)) if providers else "cohere-local",
        "model": model_size,
        "speech_locale": speech_locale,
        "taxonomy_path": str((build_dir / "taxonomy.json").resolve()),
        "taxonomy_signature": taxonomy_signature(taxonomy),
        "transcripts_path": str(transcripts_path.resolve()),
        "clip_count": len(normalized_paths),
        "transcribed_clip_count": len(normalized_paths) - empty_transcript_count,
        "empty_transcript_count": empty_transcript_count,
    }
    (build_dir / "transcripts_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
