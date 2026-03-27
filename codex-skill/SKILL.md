---
name: video-summary
description: Use when a user wants to turn raw travel clips plus a natural-language editing prompt into a final edited video with minimal manual steps.
---

# Video Summary

Use this skill when the goal is simple: give the system a natural-language editing prompt and let Codex produce a finished video by driving the local pipeline end to end.

## Core Workflow

1. If `~/.codex/skills/video-summary/video-summary.env` is missing, or the repo at `$VIDEO_SUMMARY_REPO` does not exist, run:

```bash
bash ~/.codex/skills/video-summary/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

2. Read `~/.codex/skills/video-summary/video-summary.env` and use `$VIDEO_SUMMARY_REPO` as the working repository.
3. Read the workflow docs in `workflow/`.
4. Accept a free-form editing prompt as the primary user input.
5. Codex runs the local commands for scan, ASR, candidate packaging, timeline generation, and render.
6. Codex inspects the generated candidate artifacts and uses the editing prompt to decide which cues belong in the final timeline.
7. Treat generated planning artifacts as internal implementation details unless debugging is necessary.
8. Preserve day-based episode structure and chronological order inside each day by default.

## Read These First

- [workflow/pipeline.md](../workflow/pipeline.md)

## Installation

- Manual install: copy this folder to `~/.codex/skills/video-summary`
- GitHub install: use the skill installer with `--path codex-skill --name video-summary`

## Local Prerequisites

- `git`
- Python 3.12+
- `uv`
- `ffmpeg`

When the skill is installed by itself, Codex should bootstrap the backing repository automatically on first use. The manual fallback is:

```bash
bash ~/.codex/skills/video-summary/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

## Default UX

- Do not ask the user to prepare YAML before getting started.
- Do not split the experience into separate “plan skill” and “render skill” steps unless the user explicitly asks for that level of control.
- Default to one-shot execution from prompt to final output.
- Treat generated planning artifacts as internal implementation details unless debugging is necessary.
- Codex should drive the pipeline itself: bootstrap automatically if needed, run `plan`, inspect candidate artifacts, build the final timeline, then run `render`.

## Codex Execution Loop

```bash
python3 -m video_summary plan \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘. 날짜 순서를 지키고 전체 러닝타임은 40분 정도로."

python3 -m video_summary render --project "sample-trip"
```

## Validation

- If the selected cues feel weak, inspect the generated candidate artifacts and revise the timeline before rendering.
- If chronology looks wrong, inspect `source_time` and `date_key` before changing sequence.
- If runtime feels wrong, adjust the prompt and re-run the pipeline.
