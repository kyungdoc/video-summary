# Workflow Reference

Resolve the caller workspace and an absolute source path, then use the wrapper:

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh doctor

bash /absolute/path/to/this-skill/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "날짜별로 여정, 음식, 대화와 재미있는 반응을 살려줘." \
  --planner local
```

The one-shot pipeline performs scan, local ASR, low-resolution visual analysis, candidate packaging, day-based planning, strict plan validation, sequential rendering, and YouTube companion-file generation. Daily output is the default; request one combined video with:

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 전체를 한 편으로, 날짜별 여정과 재미있는 반응이 드러나게 편집해줘." \
  --planner local \
  --episode-mode trip
```

Artifacts stay under the caller workspace:

- `.video-summary/<project>/`: configuration, caches, internal plan, render state
- `exports/<project>/`: daily outputs or `trip-summary.mp4` with matching VTT, chapters, detailed timeline, and description sidecars

The first intro uses a destination display title, not the internal project ID. Leave `project.destination` empty to infer it from the source folder after removing a leading date, generic media tokens, and a country prefix when a more specific place remains, or pass `--destination` for an exact spelling. The travel period comes from scan-corrected DAY keys after timezone, day-start, and date overrides; trip output shows the full range and daily output shows that DAY. Inspect `render-report.json.intro_metadata` for the resolved values and provenance.

## Ordering and output contract

Within each DAY, all selected source segments remain in capture-time order. With `cold_open` enabled, only the earliest selected source may be labeled `hook`; it remains after the DAY's visual date card and is not moved ahead of it.

The local planner preserves the earliest candidate as the DAY's journey anchor and reserves room for a visually strong low- or no-speech scenery candidate when one is available. Visual quality and stable outdoor context remain valid selection signals even without transcript text.

Planning is event-first. For each DAY, collect all locally detected high-confidence interview, meal, and journey-waypoint events together with the candidates required for their completion, sort that material by capture time, and only then add ordinary scenery, dialogue, or fun candidates. `target_minutes_per_day` remains the episode `target_duration` compatibility/display value; it is neither a fill quota nor the effective selection ceiling. Ordinary selection uses `soft_max_minutes_per_day`, which defaults to 10 minutes, while complete mandatory events may exceed it. When every ordinary event cannot fit, balance coverage across the DAY's early, middle, and late timeline rather than spending the budget on the earliest events. Never pad a DAY with weak repetition merely to reach either number.

Family travel-review interviews are mandatory-if-detected by default. Local transcript analysis identifies high-confidence question-and-answer runs, tags every candidate needed to keep each answer complete, and requires local, Codex, Claude, and file plans to include them chronologically at normal speed. A project with no detected interview proceeds normally. This is not face recognition or speaker identification; separate Q&A runs are the auditable unit. Keep `editing.preserve_family_interviews: true` and do not combine it with `--skip-transcribe` when this guarantee is required.

Meal events are also mandatory-if-detected by default. With `editing.preserve_meal_events: true`, local analysis groups breakfast, lunch, dinner, cafe, dessert, and snack stories. Every planner must keep at least one chronological normal-speed food/table/eating body option per event. Restaurant arrival and ordering form setup context; departure/thanks and taste reactions or retrospectives tied to an explicit food or meal name form closure context. Keep one option from every detected context group, but context never substitutes for the body. Broad `food` role candidates remain preferences rather than mandatory footage. A silent body clip can be inferred from nearby explicit meal context. A wholly untranscribed or visual-only meal remains outside automatic detection, so before the final render inspect every DAY's full candidate contact sheets, inventory distinct filmed meals, and repair the validated file plan until each visual event has a body shot and useful surrounding story. Contact sheets are QA evidence, not a replacement for the body contract or proof of automatic detection. `not_detected` reports missing automatic evidence rather than proving that no meal exists. Do not combine this guarantee with `--skip-transcribe`.

High-confidence journey waypoints are also always mandatory-if-detected. Local transcript analysis gives the `transition` role to explicit family/group pickup or joining, transfers or stopovers, rental-car pickup or return, lodging check-in or checkout, and explicit departure, arrival, boarding, or alighting at an airport, station, or terminal. Local, Codex, Claude, and file plans must keep every tagged candidate in capture-time order at normal speed even when that exceeds the soft DAY ceiling. Its normal plan role is `transition`, with `hook` or `closing` allowed only when it is the first or last DAY source; the stricter interview contract wins when both tags apply. Ordinary `journey` candidates are not mandatory merely because they have that role.

A simple question, generic movement language without a concrete journey connection, an announcement, or arrival at a restaurant or tourist attraction is not forced. Automatic tagging uses explicit transcript evidence only; known location is review/planner context and does not make a silent clip mandatory. It does not infer a route from GPS or recognize boarding actions visually, so it cannot guarantee an untranscribed waypoint. `--skip-transcribe` does not add a separate waypoint-specific failure, but it removes this guarantee.

The combined trip sequence is: global mosaic intro (or a classic title-card fallback) → DAY date card → all chronological source segments for that DAY, including the optional earliest hook → repeat for later DAYs → global outro.

For readable pacing, keep these recommended defaults unless the user asks otherwise:

```yaml
editing:
  target_minutes_per_day: 4.0     # compatibility/display target; not a fill quota
  soft_max_minutes_per_day: 10.0  # ordinary-selection ceiling; mandatory completion may exceed it

render:
  trip_intro_style: mosaic
  trip_intro_candidate_ids: []  # empty means automatic DAY-coverage-first selection
  trip_intro_grid_size: 7       # 6, 7, or 8
  trip_intro_animation: flow    # flow or static
  intro_seconds: 6.0            # recommended for a multi-day flow intro
  date_card_seconds: 3.0
  outro_seconds: 5.0
  transition_seconds: 0.18
```

The trip mosaic uses only plan-selected local candidate JPEGs that can be decoded successfully. `trip_intro_grid_size` accepts `6`, `7`, or `8`; the default 7×7 layout holds up to 49 frames, 6×6 gives up to 36 larger tiles, and 8×8 preserves the denser 64-tile option. Selection first covers every usable DAY with one frame. If usable DAY count exceeds grid capacity, it samples across the trip while retaining the first and last. Reviewed IDs and balanced DAY rounds still apply, but the filler first uses one frame from each distinct source clip and repeats a clip only when unique clips are exhausted. The automatic rank is visual quality, candidate score, capture time, and candidate ID. Fewer frames than grid capacity leave dark blank cells, and missing or corrupt frames fall through to the next selected frame for that DAY.

`trip_intro_animation` accepts `flow` or `static`. In the default `flow` mode, selected frames are sorted by edit-plan chronology and fill the board in serpentine rows, alternating left-to-right and right-to-left. Each tile briefly flips/slides horizontally into place; after the board has become legible, the title and trip period appear late on a translucent dark center panel. `static` shows a completed mosaic from the same selected frames without tile motion. If flow generation fails, render the static mosaic; if no frame is usable or static generation also fails, render the classic title card.

`trip_intro_candidate_ids` is a global selection-preference list of already plan-selected IDs, not a tile-position list. It may contain multiple IDs from one DAY, but DAY coverage is secured first. Stale or unselected IDs are ignored, and the chosen frames are finally displayed in edit-plan chronological order. Daily intro cards are unchanged.

Mosaic generation processes candidate JPEGs one at a time and combines them locally, keeping memory bounded without loading full-resolution source videos together. Enabling mosaic does not itself send images anywhere. Only a separately and explicitly requested `--planner-images` adds reduced contact sheets derived from candidate frames to an external planner request. Mosaic layout, grid size, animation, selection, or title changes invalidate only the relevant render assets/assembly while preserving transcript, analysis, and source-segment caches, so do not use `--force` merely for these settings. Output-format or exact-frame-policy changes invalidate source caches and rerender those segments.

Changing the waypoint- or meal-detection policy version rebuilds candidate packaging and planning. It reuses completed per-clip transcripts and visual signals when source and ASR inputs are unchanged, so that policy change alone does not require `--force`. If the selection changes, rebuild the assembly; an existing source piece is reusable only when its real-time window and render format are unchanged, so only newly selected pieces need rendering. A prior run that skipped transcription, or a source/ASR change, requires transcription of the affected clips again.

Overlapping or touching candidate windows from the same source are unioned before planning. Plan validation also rejects residual real-time overlap above the 1 ms tolerance from one source, including overlaps submitted by an external planner. Each rendered source is quantized to the nearest whole frame at the target fps; fps/PTS and A/V duration are normalized, padded/trimmed as needed, and validated before the cache is accepted.

Both editing duration values must be finite numbers from 0.1 through 180. Changing `target_minutes_per_day` changes the compatibility/display target stored in the plan, while `soft_max_minutes_per_day` controls the ordinary-selection ceiling. Neither is a minimum: preserve the earliest context, meaningful closing, all high-confidence events, and complete contiguous runs, then stop when the remaining footage would only add repetition or fragmentary dialogue. Mandatory completion may take a DAY past the soft maximum.

`transition_seconds` defaults to `0.18` and accepts 0–1 seconds. Exactly contiguous selections with compatible speed, location, and caption are coalesced first. Every remaining source-group boundary and card boundary uses a short video/audio fade-through-black. This is not a multi-input crossfade graph, so durations and low-memory sequential rendering remain intact. Set the value to `0` to disable boundary fades.

Intro, date, and outro cards are rendered into the MP4 frames. The matching `.chapters.txt` is timestamp text to paste into a YouTube description; it is not embedded MP4 chapter metadata, is not an upload instruction, and is not uploaded automatically. A trip summary has one chapter per DAY: DAY 1 starts at `00:00` and includes the trip intro, while later DAYs start at their date cards.

The matching `.timeline.txt` keeps the detailed card/source index for QA and should not be pasted into the YouTube description.

For debugging or control:

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh status \
  --project "sample-trip"

bash /absolute/path/to/this-skill/scripts/run-video-summary.sh plan \
  --project "sample-trip" \
  --planner codex \
  --strict-planner

bash /absolute/path/to/this-skill/scripts/run-video-summary.sh render \
  --project "sample-trip" \
  --draft
```

The external planner is optional and requires the user's explicit opt-in. `local` is fully local; `codex` and `claude` send prompt/bounded candidate excerpts and metadata from an isolated request directory, with reduced contact sheets only when `--planner-images` is supplied. The bounded excerpts may include language that reveals a family interview, pickup, transfer, lodging stay, or terminal movement.

Family interview answers and meal context may appear in those bounded excerpts, and family faces or dining tables may appear in contact sheets when `--planner-images` is enabled. After rendering, verify `render-report.json.moment_coverage.family_interviews` and `.meals` are `satisfied` when their events were detected, `not_detected` when automatic evidence was absent, or `disabled` only after the matching explicit opt-out. For meals, confirm every event has a selected body option and every reported setup/closure context group has a selected candidate. Independently finish the local full-contact-sheet meal inventory before treating `.meals` as final approval. Verify travel waypoints through the candidates tagged `transition` and their presence, order, role, and `speed=1.0` in `edit-plan.json`.
