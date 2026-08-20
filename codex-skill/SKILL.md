---
name: video-summary
description: Use when a user wants to turn raw travel clips plus a natural-language editing prompt into daily or trip-summary YouTube-ready videos with a local CLI.
---

# Video Summary

Use this skill to resolve a raw travel-media folder and run the local pipeline from a natural-language prompt to final daily videos or one combined trip video.

## Input resolution

1. Prefer an explicit media path in the user's request.
2. Otherwise inspect the current workspace for media directly or one obvious folder named `raw`, `clips`, or `videos`.
3. Ask only when multiple large candidate folders make a wrong choice expensive.
4. Resolve the source to an absolute path before running.
5. Keep `.video-summary/` and `exports/` under the caller's workspace unless the user explicitly chooses another workspace.

## Bootstrap and execution repository

If this skill's `video-summary.env` is missing or `$VIDEO_SUMMARY_REPO` does not exist, run:

```bash
bash /absolute/path/to/this-skill/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

Read the environment file and use the bundled wrapper for every command. The wrapper selects the repository's locked uv environment while preserving the caller's working directory.

## Default workflow

1. Read [references/workflow.md](./references/workflow.md) and `$VIDEO_SUMMARY_REPO/workflow/pipeline.md`.
2. Run `doctor` if the environment has not been checked in the current task and verify that its JSON has `ready: true`.
3. Choose `local` by default. Use Codex/Claude planning only when the user explicitly opts into sending excerpts and metadata externally.
4. Explain that `--planner-images` sends reduced contact sheets to the external planner and use it only when the user wants visual review.
5. Prefer a single `run` command. Daily output is the default; add `--episode-mode trip` when the user wants one combined trip video.
6. On failure, inspect `status` and the relevant internal artifact, then rerun the same command to resume.
7. Report final MP4, VTT, chapters, detailed timeline, and description paths. Do not upload to YouTube unless separately requested and authorized.

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "날짜별로 여정과 재미있는 대화를 살린 여행 브이로그로 편집해줘." \
  --planner local
```

For semantic planning:

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt-file "/absolute/path/to/editing-prompt.md" \
  --planner codex \
  --strict-planner
```

## Local processing

- Prefer configured whisper.cpp (`large-v3-turbo-q5_0` + Silero VAD) on a 16GB Apple Silicon Mac.
- Otherwise the locked environment provides faster-whisper small/int8 CPU.
- ASR, visual analysis, and segment rendering are sequential and cached per clip/segment.
- The first faster-whisper run may download model files, but audio/video remains local.
- Default output is daily 1080p/30fps; `--episode-mode trip` produces `trip-summary.mp4`. Use `--draft` for a 720p review.

## Output contract

- Keep every selected source segment in capture-time order within its DAY. `cold_open` may label only the earliest selected source as `hook`; it stays after that DAY's visual date card and is never moved ahead of it.
- Preserve the earliest usable candidate as each DAY's narrative anchor. Consider visually strong scenery and stable outdoor shots even with little or no speech; do not rank candidates by transcript density alone.
- A trip MP4 follows: global mosaic intro (classic title card fallback) → DAY date card → all chronological source segments for that DAY, including the optional earliest hook → repeat for later DAYs → global outro.
- The trip mosaic uses only plan-selected, usable local candidate frames. Default to `trip_intro_grid_size: 7` for a 7×7 grid of up to 49 frames; allow `6` for larger tiles or `8` for a denser overview. Cover usable DAYs first and sample across the trip when DAY count exceeds grid capacity. Fill remaining cells in balanced DAY rounds after honoring reviewed IDs, preferring one frame from each distinct source clip before repeating a clip. Leave unused cells blank.
- Default to `trip_intro_animation: flow`: place edit-plan-chronological frames in alternating left-to-right/right-to-left serpentine rows, with each tile briefly flipping/sliding horizontally into place, then reveal the translucent center title/date panel late. `static` shows the same completed mosaic without tile motion. Fall back from flow to static, then to the classic title card if the mosaic cannot be produced.
- Treat `trip_intro_candidate_ids` as global selection preferences, not tile positions. Multiple reviewed IDs may come from one DAY, while stale or unselected IDs are ignored; display the final selection in edit-plan chronological order. Daily videos keep the classic title intro.
- Merge overlapping or touching candidate windows from one source before planning, and reject residual same-source time overlap above the 1 ms tolerance during plan validation.
- Quantize every rendered source duration to an exact target-fps frame count and validate fps, frame count, and A/V durations before reusing it. Mosaic-only changes preserve source caches; output-format or exact-frame-policy changes require source rerendering.
- Recommend `intro_seconds: 6.0` for a multi-day flow intro, `date_card_seconds: 3.0`, `outro_seconds: 5.0`, and `transition_seconds: 0.18` unless the user requests different pacing. The transition is a black/video and audio fade only on each DAY's first and last source at card boundaries; keep source-to-source joins as clean cuts, not crossfades. Set it to `0` to disable it.
- Intro/date/outro cards are rendered into the MP4 frames. `.chapters.txt` is timestamp text for the YouTube description, not embedded MP4 metadata or an automatic upload.
- Trip chapters are DAY-level: DAY 1 starts at `00:00` and includes the trip intro; later DAYs start at their date cards.
- `.timeline.txt` retains the detailed card/source index for QA; do not paste it into the YouTube description.

## Privacy

- `local` planner sends nothing to an external model.
- Codex/Claude receive the editing prompt and bounded candidate transcript excerpts/metadata in an isolated request directory.
- `--planner-images` explicitly adds reduced contact sheets derived from candidate frames to the external planner request.
- Trip-intro mosaic frames are processed one at a time and combined locally during render to keep memory bounded. Mosaic mode alone never adds them to an external planner request, and layout/animation-only changes reuse transcript, analysis, and source-segment caches.
- The pipeline does not explicitly attach raw MP4 files, but the external CLI process can read local files according to its sandbox and account policy.

## Validation

- Verify every selected source remains chronological after its DAY's date card and only the earliest selected source can be the hook.
- Verify `edit-plan.json` shows the intended planner rather than an unnoticed fallback when strict external planning is required.
- If dates are wrong, inspect capture provenance and add `date_overrides` instead of manually reordering the plan.
- If locations are missing, add `locations` rules by filename, date, or transcript keyword.
- If pacing is wrong, adjust the prompt or `target_minutes_per_day`; cached analysis should remain reusable.
