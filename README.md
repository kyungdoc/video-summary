# Video Summary OSS Kit

An open-source toolkit for prompt-first travel video editing built around:

- local ASR transcription
- candidate segment packaging
- cue-level analysis by travel day
- LLM-driven day-based segment selection
- single-final rendering
- prompt-first project setup

## Repo Layout

```text
workflow/      core workflow spec, schemas, prompt examples
codex-skill/   Codex-ready skill package for operating the workflow
video_summary/ Python implementation of the pipeline
```

## Where To Start

- Workflow core: [workflow/README.md](workflow/README.md)
- Pipeline overview: [workflow/pipeline.md](workflow/pipeline.md)
- Codex packaging: [codex-skill/SKILL.md](codex-skill/SKILL.md)

## Requirements

- `git`
- Python 3.12+
- `uv`
- `ffmpeg`
- local environment capable of running either `transformers`+`torch` or `faster-whisper`

## Quick Start

```bash
uv sync
python3 -m video_summary run \
  --project "Sample-Trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --transcription-provider "cohere-transformers" \
  --transcription-model "CohereLabs/cohere-transcribe-03-2026" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘. 날짜 순서를 지키고, 식사 장면과 리액션을 살리고, 전체 러닝타임은 40분 정도로 맞춰줘."
```

`--prompt-file /absolute/path/to/project_prompt.md` 도 사용할 수 있습니다.

ASR provider options:

- `cohere-transformers`:
  local Hugging Face inference for `CohereLabs/cohere-transcribe-03-2026`. This is the default path.
- `openai-compatible`:
  sends audio to an OpenAI-compatible `/audio/transcriptions` endpoint. Use this when you want to point the pipeline at a remote transcription server.
- `faster-whisper`:
  keeps the previous local Whisper path available as a fallback.

Environment overrides are also supported:

```bash
export VIDEO_SUMMARY_TRANSCRIPTION_PROVIDER="openai-compatible"
export VIDEO_SUMMARY_TRANSCRIPTION_MODEL="CohereLabs/cohere-transcribe-03-2026"
export VIDEO_SUMMARY_TRANSCRIPTION_BASE_URL="http://127.0.0.1:8000/v1"
export VIDEO_SUMMARY_TRANSCRIPTION_API_KEY="dummy"
```

When `--source-dir` is provided, Codex-friendly outputs are written under the current workspace root instead of next to the source media:

- internal build/cache files: `WORKSPACE_ROOT/.video-summary/<project>/...`
- final exports: `WORKSPACE_ROOT/exports/<project>/...`

Most users do not need `--project-dir`:

```bash
python3 -m video_summary run \
  --project "Sample-Trip" \
  --source-dir "/absolute/path/to/trip1/raw" \
  --prompt "..."
```

If you want a specific output root, pass `--project-dir` explicitly:

```bash
python3 -m video_summary run \
  --project "Sample-Trip" \
  --source-dir "/absolute/path/to/trip1/raw" \
  --project-dir "/absolute/path/to/trip1" \
  --prompt "..."
```

By default, the bundled skill sets `WORKSPACE_ROOT` to the user's current working directory when Codex starts the run, so remote or external source folders do not affect where artifacts are stored.

`run` is the default one-shot CLI entrypoint. When Codex is using the bundled skill, Codex should normally drive `plan` and `render` itself after inspecting the generated artifacts.

If you want more control, you can still run `plan` and `render` separately.

CLI usage and Codex skill usage are not identical:

- The CLI still generates transcripts, segment candidates, and planning artifacts.
- But candidate inspection and prompt-aware final selection are designed for the Codex skill loop, where Codex reads those artifacts and decides what should make it into the final timeline.
- If you use the CLI directly, do not assume a human-like automatic review pass over the candidate bundle unless Codex is actually driving that run.

## Skill Installation

If you want to use the bundled Codex skill, there are two supported install paths.

### 1. Manual Local Install

```bash
mkdir -p ~/.codex/skills/video-summary
rsync -a /path/to/repo/codex-skill/ ~/.codex/skills/video-summary/
```

### 2. GitHub Install Via Skill Installer

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo kyungdoc/video-summary \
  --path codex-skill \
  --name video-summary
```

After either install method, restart Codex to pick up the skill.

## Skill Prerequisites

Before using the installed skill on a local machine, make sure these are available:

- `git`
- Python 3.12+
- `uv`
- `ffmpeg`

If the skill is installed by itself, bootstrap the repository and environment once:

```bash
~/.codex/skills/video-summary/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

After bootstrap, Codex can run the workflow directly against that repository.

## Notes

- The Python implementation is still evolving; the `workflow/` directory is the intended stable public surface.
- The Codex skill is meant to consume the workflow docs rather than duplicate them.
- `project_metadata.yaml`, `segment_candidates.json`, `cue_analysis_by_day.json`, and `timeline_final.json` are internal build artifacts.
