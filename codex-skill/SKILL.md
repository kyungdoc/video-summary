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
- Use a destination display name rather than the internal project ID on the first intro. Prefer explicit `project.destination`/`--destination`; otherwise infer from the source-folder basename after removing a leading date, generic media tokens, and a country prefix when a more specific place remains, then fall back to the project name. Derive the period only from scan-corrected DAY keys: the full range for trip output and that DAY for daily output. Record both values and provenance in `render-report.json.intro_metadata`.
- The trip mosaic uses only plan-selected, usable local candidate frames. Default to `trip_intro_grid_size: 7` for a 7×7 grid of up to 49 frames; allow `6` for larger tiles or `8` for a denser overview. Cover usable DAYs first and sample across the trip when DAY count exceeds grid capacity. Fill remaining cells in balanced DAY rounds after honoring reviewed IDs, preferring one frame from each distinct source clip before repeating a clip. Leave unused cells blank.
- Default to `trip_intro_animation: flow`: place edit-plan-chronological frames in alternating left-to-right/right-to-left serpentine rows, with each tile briefly flipping/sliding horizontally into place, then reveal the translucent center title/date panel late. `static` shows the same completed mosaic without tile motion. Fall back from flow to static, then to the classic title card if the mosaic cannot be produced.
- Treat `trip_intro_candidate_ids` as global selection preferences, not tile positions. Multiple reviewed IDs may come from one DAY, while stale or unselected IDs are ignored; display the final selection in edit-plan chronological order. Daily videos keep the classic title intro.
- Merge overlapping or touching candidate windows from one source before planning, and reject residual same-source time overlap above the 1 ms tolerance during plan validation.
- Plan event-first: collect every locally detected high-confidence interview, meal, and journey-waypoint event with the candidates needed to complete it, then place that material in capture-time order before adding ordinary scenery, dialogue, or fun candidates. `target_minutes_per_day` is the compatibility/display value for episode `target_duration`, not a fill quota or the actual selection ceiling. Apply `editing.soft_max_minutes_per_day: 10` as the default soft ceiling for ordinary selection, but allow mandatory event completion to exceed it. If every ordinary event cannot fit, distribute coverage across the DAY's early, middle, and late timeline instead of exhausting the budget in the morning. Stop below either number when no worthwhile non-repetitive material remains.
- With `editing.preserve_family_interviews: true` (default), treat every locally detected high-confidence family travel-review Q&A run as mandatory-if-present. Keep every tagged candidate chronological at `speed=1.0`, even when it exceeds the soft duration ceiling; reject external/file plans that omit or speed up one. If no interview is detected, continue normally. Detection is transcript-based, not face recognition or speaker identification, so it preserves distinct Q&A runs rather than proving each family member's identity. Do not use `--skip-transcribe` while this guarantee is enabled.
- With `editing.preserve_meal_events: true` (default), preserve every locally detected breakfast, lunch, dinner, cafe, dessert, or snack event as a compact story. Require at least one chronological `speed=1.0` actual food/table/eating body option per event. Treat restaurant arrival and ordering as setup; treat departure/thanks and taste reactions or retrospectives tied to an explicit food or meal name as closure. Select one option from every detected context group, but never let setup or closure substitute for the body. Do not make every broad `food` candidate mandatory. Silent body clips may be inferred only when anchored by explicit nearby meal context. Because `not_detected` means only that transcript/timeline evidence was absent, inspect the full candidate contact sheets by DAY before approving a final render. Repair the file plan so every visually confirmed meal has a body shot and its useful surrounding story; visual-only evidence is QA input rather than automatic detection proof. Do not use `--skip-transcribe` while this guarantee is enabled.
- Always treat each locally detected high-confidence `transition` journey waypoint as mandatory-if-present. This includes explicit family/group pickup or joining, a transfer or stopover, rental-car pickup or return, lodging check-in or checkout, and explicit departure, arrival, boarding, or alighting at an airport, station, or terminal. Keep every tagged candidate in capture-time order at `speed=1.0`, ahead of the soft duration ceiling, and reject an external/file plan that omits, speeds up, or gives an invalid role to one. Its normal plan role is `transition`; the first or last DAY source may remain `hook` or `closing`, and the stricter interview contract wins when both tags apply.
- Do not force a simple question, generic movement language without a concrete journey connection, an announcement, or arrival at a restaurant or tourist attraction. Automatic tagging uses explicit transcript evidence only; known location is review/planner context and does not make a silent clip mandatory. The pipeline cannot guarantee an untranscribed waypoint. `--skip-transcribe` does not add a separate waypoint-specific failure, but it removes this guarantee.
- Quantize every rendered source duration to an exact target-fps frame count and validate fps, frame count, and A/V durations before reusing it. Mosaic-only changes preserve source caches; output-format or exact-frame-policy changes require source rerendering.
- A waypoint-detection policy-version change rebuilds candidate packaging and planning, but it reuses completed per-clip transcripts and visual signals when source/ASR inputs are unchanged. If the selection changes, rebuild the assembly; existing source pieces remain reusable only when their real-time windows and render format are unchanged, so render only newly selected waypoint pieces. If a prior run skipped transcription or source/ASR inputs changed, transcribe the affected clips again.
- Recommend `intro_seconds: 6.0` for a multi-day flow intro, `date_card_seconds: 3.0`, `outro_seconds: 5.0`, and `transition_seconds: 0.18` unless the user requests different pacing. Coalesce exactly contiguous compatible selections first, then use a short video/audio fade-through-black at every remaining source-group and card boundary. This preserves duration and low-memory sequential rendering without a multi-input crossfade graph. Set it to `0` to disable the fades.
- Intro/date/outro cards are rendered into the MP4 frames. `.chapters.txt` is timestamp text for the YouTube description, not embedded MP4 metadata or an automatic upload.
- Trip chapters are DAY-level: DAY 1 starts at `00:00` and includes the trip intro; later DAYs start at their date cards.
- `.timeline.txt` retains the detailed card/source index for QA; do not paste it into the YouTube description.

## Privacy

- `local` planner sends nothing to an external model.
- Codex/Claude receive the editing prompt and bounded candidate transcript excerpts/metadata in an isolated request directory.
- Those bounded excerpts can contain family interview answers and language revealing pickup, transfer, lodging, terminal movements, or meal context. `--planner-images` can also place family faces and dining tables in reduced contact sheets; use an external planner only with that privacy boundary understood.
- `--planner-images` explicitly adds reduced contact sheets derived from candidate frames to the external planner request.
- Trip-intro mosaic frames are processed one at a time and combined locally during render to keep memory bounded. Mosaic mode alone never adds them to an external planner request, and layout/animation-only changes reuse transcript, analysis, and source-segment caches.
- The pipeline does not explicitly attach raw MP4 files, but the external CLI process can read local files according to its sandbox and account policy.

## Validation

- Verify every selected source remains chronological after its DAY's date card and only the earliest selected source can be the hook.
- Verify `edit-plan.json` shows the intended planner rather than an unnoticed fallback when strict external planning is required.
- If dates are wrong, inspect capture provenance and add `date_overrides` instead of manually reordering the plan.
- If locations are missing, add `locations` rules by filename, date, or transcript keyword.
- If pacing is wrong, adjust the prompt or `soft_max_minutes_per_day`; change `target_minutes_per_day` only when the plan's compatibility/display target should change. Both accept finite values from 0.1 to 180, and cached analysis should remain reusable.
- Inspect `render-report.json.moment_coverage.family_interviews`: it must be `satisfied` when interview groups were detected, `not_detected` when none were found, or `disabled` only when `preserve_family_interviews: false` was explicitly configured.
- Inspect `render-report.json.moment_coverage.meals`: it must be `satisfied` when meal events were detected, `not_detected` when none were found, or `disabled` only after an explicit `preserve_meal_events: false` opt-out. Each detected event must list a selected body option plus a selected candidate from every detected setup/closure context group.
- Treat `.meals.status=not_detected` as “no automatic transcript/timeline evidence,” not proof that the trip contained no filmed meal. Before the 2160p render, inspect all DAY contact sheets—not only plan-selected frames—cluster distinct meals, cafes, snacks, and desserts, and confirm the plan contains a body shot for every visually identified event. If one is missing, add the appropriate candidate in capture-time order through a validated file plan and rerender the draft first.
