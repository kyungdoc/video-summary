---
name: video-summary
description: Use when a user wants to turn raw travel clips plus a natural-language editing prompt into a final edited video with minimal manual steps.
---

# Video Summary

Use this skill when the goal is simple: give the system a natural-language editing prompt and get a finished video back.

## Core Workflow

1. Read the workflow docs in `workflow/`.
2. Accept a free-form editing prompt as the primary user input.
3. Internally run the pipeline as: scan -> ASR -> candidate packaging -> automatic selection -> final timeline -> render.
4. Treat generated planning artifacts as internal implementation details unless debugging is necessary.
5. Preserve day-based episode structure and chronological order inside each day by default.

## Read These First

- [workflow/pipeline.md](../workflow/pipeline.md)

## Installation

- Manual install: copy this folder to `~/.codex/skills/video-summary`
- GitHub install: use the skill installer with `--path codex-skill --name video-summary`

## Default UX

- Do not ask the user to prepare YAML before getting started.
- Do not split the experience into separate “plan skill” and “render skill” steps unless the user explicitly asks for that level of control.
- Default to one-shot execution from prompt to final output.
- Treat generated planning artifacts as internal implementation details unless debugging is necessary.
- Prefer `run` over `plan` or `render` when you are driving the tool on the user's behalf.

## Common Command

```bash
./codex-skill/scripts/run-video-summary.sh \
  "sample-trip" \
  "여행 브이로그를 따뜻하고 여유롭게 편집해줘. 날짜 순서를 지키고 전체 러닝타임은 40분 정도로." \
  "/absolute/path/to/raw-clips"
```

## Validation

- If the auto-generated selection is weak, first improve the prompt.
- If chronology looks wrong, inspect `source_time` and `date_key` before changing sequence.
- If runtime feels wrong, adjust the prompt and re-run the pipeline.
