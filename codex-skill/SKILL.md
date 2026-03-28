---
name: video-summary
description: Use when a user wants to turn raw travel clips plus a natural-language editing prompt into a final edited video with minimal manual steps.
---

# Video Summary

Use this skill when the goal is simple: give the system a natural-language editing prompt and let Codex produce a finished video by driving the local pipeline end to end.

## Input Resolution

Codex should resolve paths from the user's prompt and workspace context before running the pipeline.

- First, look for an explicit filesystem path in the user's message and use that as the strongest hint.
- If the user does not mention a path, inspect the current working directory.
- If the current working directory contains media files directly, treat the current working directory as `--source-dir`.
- If the current working directory contains a single obvious media subfolder like `raw`, `clips`, or `videos`, use that subfolder as `--source-dir`.
- Only ask the user for clarification when multiple candidate folders are plausible and choosing the wrong one would likely waste significant processing time.
- When a path is inferred rather than explicitly provided, briefly state the assumption before running.
- Always keep generated artifacts under the user's current workspace root unless the user explicitly asks for a different output root.

## Core Workflow

1. If `~/.codex/skills/video-summary/video-summary.env` is missing, or the repo at `$VIDEO_SUMMARY_REPO` does not exist, run:

```bash
bash ~/.codex/skills/video-summary/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

2. Read `~/.codex/skills/video-summary/video-summary.env` and resolve `$VIDEO_SUMMARY_REPO`.
3. Treat `$VIDEO_SUMMARY_REPO` as the only execution repository for all pipeline commands.
4. Treat the user's current project directory only as a source of inputs such as raw clips and prompt files.
5. Read the workflow docs in `workflow/` from inside `$VIDEO_SUMMARY_REPO`.
6. Accept a free-form editing prompt as the primary user input.
7. Codex runs the local commands for scan, ASR, candidate packaging, timeline generation, and render.
8. Codex inspects the generated candidate artifacts and uses the editing prompt to decide which cues belong in the final timeline.
9. Treat generated planning artifacts as internal implementation details unless debugging is necessary.
10. Preserve day-based episode structure and chronological order inside each day by default.

## Execution Boundary

- Never run `python3 -m video_summary ...` from the user's raw-media project folder.
- Never assume the current working directory is the workflow repository.
- Always source `~/.codex/skills/video-summary/video-summary.env` and run commands from `$VIDEO_SUMMARY_REPO`.
- Always convert the resolved user path into a concrete `--source-dir`.
- By default, store `.video-summary/` and `exports/` under the user's current workspace root.
- Only pass `--project-dir` when the user explicitly wants a different output root.
- Prefer the wrapper script below so the repository path and `uv` environment stay consistent:

```bash
bash ~/.codex/skills/video-summary/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘."
```

This wrapper keeps outputs under the caller's workspace root even when the source clips live elsewhere.

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
- Codex should normally use the one-shot `run` entrypoint first.
- If inspection is needed, Codex may switch to `plan` and `render`, but those commands must still run from `$VIDEO_SUMMARY_REPO`.
- The user should not need to know or supply CLI flags unless they want explicit control.
- Codex should translate the user's natural-language request into the concrete paths and command invocation.

## Codex Execution Loop

```bash
bash ~/.codex/skills/video-summary/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘. 날짜 순서를 지키고 전체 러닝타임은 40분 정도로."
```

When the user wants more control or debugging, use:

```bash
bash ~/.codex/skills/video-summary/scripts/run-video-summary.sh plan \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘."

bash ~/.codex/skills/video-summary/scripts/run-video-summary.sh render \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips"
```

## Validation

- If the selected cues feel weak, inspect the generated candidate artifacts and revise the timeline before rendering.
- If chronology looks wrong, inspect `source_time` and `date_key` before changing sequence.
- If runtime feels wrong, adjust the prompt and re-run the pipeline.
