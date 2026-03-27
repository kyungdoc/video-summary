# Video Summary OSS Kit

An open-source toolkit for prompt-first travel video editing built around:

- local ASR transcription
- candidate segment packaging
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
- local environment capable of running `faster-whisper`

## Quick Start

```bash
uv sync
python3 -m video_summary run \
  --project "Sample-Trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘. 날짜 순서를 지키고, 식사 장면과 리액션을 살리고, 전체 러닝타임은 40분 정도로 맞춰줘."
```

`--prompt-file /absolute/path/to/project_prompt.md` 도 사용할 수 있습니다.

`run` is the default one-shot CLI entrypoint. When Codex is using the bundled skill, Codex should normally drive `plan` and `render` itself after inspecting the generated artifacts.

If you want more control, you can still run `plan` and `render` separately.

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
  --repo <owner>/<repo> \
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
  https://github.com/<owner>/<repo>.git
```

After bootstrap, Codex can run the workflow directly against that repository.

## Notes

- The Python implementation is still evolving; the `workflow/` directory is the intended stable public surface.
- The Codex skill is meant to consume the workflow docs rather than duplicate them.
- `project_metadata.yaml`, `segment_candidates.json`, and `timeline_final.json` are internal build artifacts.
