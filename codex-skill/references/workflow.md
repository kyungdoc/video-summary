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

Planning is full-source-accounting first. Before choosing highlights or consulting a runtime value, inspect each DAY's raw timeline and full contact sheets, group every filmed activity chronologically under a stable `story_event_id`, and assign candidates an auditable story stage, importance, speed policy, and exclusion state. Compare total raw seconds with candidate-accounted seconds. `unassigned_source_seconds` is a candidate hole to review, not evidence that nothing happened there; restore missing candidates for a visible event or body/action before planning.

Read an event as the stages actually filmed: `setup → body/action → reaction/outcome → closure`. Not every event needs all stages, but setup and closure cannot substitute for its body/action. When source/contact-sheet review confirms that the body/action was never recorded, classify that fact as `not_filmed` in the review notes; this is an editorial judgment, not a promised machine-generated field. Do not make adjacent silent footage impersonate the missing activity. Use only an honest card, caption, or narration bridge such as “after the meal” or “after a short break.”

Build the complete chronological story spine first, then apply the compression ladder `full → compact → speed_up → omit`. Start with enough normal-speed footage to understand each event. If it drags, compact repetition within that event while keeping its useful setup/body/action/reaction/closure run at normal speed. If further compression is useful, accelerate only low-change, non-dialogue travel, waiting, or repetitive scenery marked `allow_fast`. Meals/eating, play and exploration (pool, outdoor sightseeing, rides), family interviews, people meeting or joining, dialogue, key actions, and meaningful reactions are `protected_1x`. Omission is the last resort for true semantic repetition, explicitly excluded private material, or unusable footage; never delete a later unique event merely because earlier events used more runtime.

Use `editing.pacing_profile` to tune the amount of repetition retained inside each event, not to remove events. The default `gentle` profile uses source-time soft envelopes of 120/60/70/30/45/20 seconds for interview/meal/play/waypoint/dialogue/scenery and caps acceleration at 2×. `balanced` uses 90/45/50/20/30/15 seconds and may compress eligible `allow_fast` bridges more assertively within the configured maximum. Mandatory stages and useful contiguous core runs may exceed these envelopes, and `protected_1x` footage remains at normal speed in either profile.

The default `editing.tone_profile: playful` changes ranking only inside already represented events. Prefer chronological choice/action/reveal/reaction chains, family interaction, and visible change over repetition without adding events or increasing the envelope. Before adding cutaways, choose each event stage's main view: use a comparable action-camera view as the soft default for the same filmed beat, but promote a phone's unique or materially stronger key activity/reaction to a normal unrestricted main segment. A nearby 2–8 second candidate from another reliable source stream may then be used as at most one optional cutaway per event inside the remaining envelope; never add one automatically to an interview or use it to replace required meal footage. A manually estimated `low`-confidence timestamp never authorizes automatic preference, replacement, suppression, cutaway, or simultaneous-angle grouping. `calm` keeps the same event/contract coverage while favoring natural spatial and dialogue context. New plans record both profiles; profile-less legacy plans retain their historical `balanced + calm` meaning.

`target_minutes_per_day` remains the episode `target_duration` compatibility/display value; it is neither a fill quota nor a selection ceiling. `soft_max_minutes_per_day`, default 10 minutes, is a DAY review guard. Exceeding it starts a pacing/compression audit and may remain the correct result when complete flow needs the time. Stop below it when the remaining footage adds only weak repetition. The story-flow report records the resulting event treatment as `full`, `full_speed_up`, `compact`, `compact_speed_up`, or `omit`; these report values describe which rungs were actually combined, while the editorial decision order remains full, compact, speed-up, then omit.

Family travel-review interviews are mandatory-if-detected by default. Local transcript analysis identifies high-confidence question-and-answer runs, tags every candidate needed to keep each answer complete, and requires local, Codex, Claude, and file plans to include them chronologically at normal speed. A project with no detected interview proceeds normally. This is not face recognition or speaker identification; separate Q&A runs are the auditable unit. Keep `editing.preserve_family_interviews: true` and do not combine it with `--skip-transcribe` when this guarantee is required.

Meal events are also mandatory-if-detected by default. With `editing.preserve_meal_events: true`, local analysis groups breakfast, lunch, dinner, cafe, dessert, and snack stories. Every planner must keep at least one chronological normal-speed food/table/eating body option per filmed event. Restaurant arrival and ordering form setup context; departure/thanks and taste reactions or retrospectives tied to an explicit food or meal name form closure context. Keep one option from every detected context group, but context never substitutes for the body. Broad `food` role candidates remain preferences rather than mandatory footage. A wholly untranscribed or visual-only meal remains outside automatic detection, so inspect every DAY's full contact sheets, inventory distinct filmed meals, and repair missing candidates or the validated file plan until each filmed event has a body shot and useful surrounding story. If review shows no recorded body, note `not_filmed` and bridge honestly instead of inferring one from unrelated silent footage. Contact sheets are QA evidence, not proof of automatic detection. `not_detected` reports missing automatic evidence rather than proving that no meal exists. Do not combine this guarantee with `--skip-transcribe`.

High-confidence journey waypoints are also always mandatory-if-detected. Local transcript analysis gives the `transition` role to explicit family/group pickup or joining, transfers or stopovers, rental-car pickup or return, lodging check-in or checkout, and explicit departure, arrival, boarding, or alighting at an airport, station, or terminal. Local, Codex, Claude, and file plans must keep every tagged candidate in capture-time order at normal speed even past the DAY review guard. Its normal plan role is `transition`, with `hook` or `closing` allowed only when it is the first or last DAY source; the stricter interview contract wins when both tags apply. Ordinary low-change travel may be accelerated only when it is `allow_fast`; the waypoint action itself remains `protected_1x`.

A simple question, generic movement language without a concrete journey connection, an announcement, or arrival at a restaurant or tourist attraction is not forced from transcript alone. Automatic tagging uses explicit transcript evidence; it does not infer GPS routes or visually recognize boarding. The full-timeline/contact-sheet audit must therefore check silent waypoint and activity candidates rather than assuming automatic `not_detected` is complete. `--skip-transcribe` removes the transcript-based guarantee.

Use `editing.exclude_ranges` to keep private, unsafe, or explicitly forbidden footage out of planning and rendering. Each entry requires a relative-path or basename glob in `match` and a 1–160 character `reason`; `start` defaults to clip-relative 0 seconds and omitted `end` means the clip end. A candidate overlapping that interval stays auditable with an exclusion reason and omit policy but must never enter a plan or render. Typical uses include changing clothes, medical/hygiene footage, and exposed sensitive information.

The combined trip sequence is: global mosaic intro (or a classic title-card fallback) → DAY date card → all chronological source segments for that DAY, including the optional earliest hook → repeat for later DAYs → global outro.

For readable pacing, keep these recommended defaults unless the user asks otherwise:

```yaml
editing:
  target_minutes_per_day: 4.0     # compatibility/display target; not a fill quota
  soft_max_minutes_per_day: 10.0  # pacing review guard; not a selection ceiling
  selection_strategy: event_flow
  pacing_profile: gentle          # gentle or balanced
  tone_profile: playful           # playful or calm
  adaptive_fast_forward: true
  max_fast_forward_speed: 3.0
  exclude_ranges:
    - match: "**/IMG_1234.MOV"
      start: 12.4
      end: 28.0
      reason: "private changing-clothes footage"

render:
  portrait_layout: blur           # blur, pillarbox, or crop
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

Both editing duration values must be finite numbers from 0.1 through 180. Changing `target_minutes_per_day` changes only the compatibility/display target stored in the plan; changing `soft_max_minutes_per_day` changes the pacing review guard. Neither is a minimum or selection ceiling. `max_fast_forward_speed` accepts 1 through 4, but it never authorizes acceleration of a `protected_1x` candidate. Preserve all unique events and their useful contiguous narrative runs, then stop when the remaining footage would only add repetition or fragmentary dialogue.

Source discovery is recursive and folder-name independent. Read QuickTime original-date before generic creation time, accept compact UTC offsets such as `-1000`, and use camera make/model metadata to classify phone, action-camera, and shared footage into opaque source streams. Multiple phones in the source root, or a phone-only event/trip, join one capture-time timeline as equal primary cameras. In a mixed-camera event, first preserve every unique stage and required contract. When reliable action-camera and phone candidates cover the same event and stage bucket at the same filmed beat, use the action-camera as the soft representative default. Promote the phone to a normal main segment without the 2–8 second cutaway limit when it is the only coverage of a key activity/reaction stage, contains unique choice/reveal/reaction or contract evidence, or is materially stronger editorially or visually. Do not increase runtime with a redundant action-camera version of that promoted beat; compact only the repeated view. Reliable candidates from different streams that overlap within one event and strongly match by representative frame form a duplicate-view group; matching transcripts may lower the threshold but never suppress a visually distinct action or reaction angle by themselves. Required DAY anchors, interview/transition all-of contracts, and distinct meal event/context one-of contracts override camera preference. After main-stage selection, visually different nearby views remain eligible for the bounded playful cutaway rule. Exclude `low`-confidence estimated times from automatic preference, replacement, suppression, grouping, and cutaways, and place them only with independent event-flow evidence. External planners receive generic source kind plus opaque stream/confidence metadata, never the local source path or camera identifier. `render.portrait_layout` controls how portrait footage fills a landscape canvas: `blur` (default), `pillarbox`, or `crop`; portrait orientation itself carries no selection penalty. On macOS, HLG/PQ iPhone sources are converted through VideoToolbox `scale_vt` to BT.709 SDR, and the assembled H.264 carries BT.709 color tags. Fail explicitly if the required HDR conversion filter is unavailable instead of silently producing incorrect color.

`transition_seconds` defaults to `0.18` and accepts 0–1 seconds. Exactly contiguous selections with compatible speed, location, and caption are coalesced first. Fragments of the same semantic event use a hard cut only when their real capture-time gap is within 30 seconds; distant fragments with an accidentally reused event ID, other source-group boundaries, and card boundaries use a short video/audio fade-through-black. This is not a multi-input crossfade graph, so durations and low-memory sequential rendering remain intact. Set the value to `0` to disable boundary fades.

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

To compare pacing profiles on one DAY without rerunning analysis, preserve each generated plan and render tagged variants. The renderer validates the full plan before applying `--day-key`; tagged outputs and companion files are written under `exports/<project>/comparisons/`.

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh plan \
  --project "sample-trip" --planner local --pacing-profile gentle
cp .video-summary/sample-trip/edit-plan.json .video-summary/sample-trip/edit-plan-gentle.json

bash /absolute/path/to/this-skill/scripts/run-video-summary.sh plan \
  --project "sample-trip" --planner local --pacing-profile balanced
cp .video-summary/sample-trip/edit-plan.json .video-summary/sample-trip/edit-plan-balanced.json

bash /absolute/path/to/this-skill/scripts/run-video-summary.sh render \
  --project "sample-trip" --day-key 2025-02-05 \
  --plan-file .video-summary/sample-trip/edit-plan-gentle.json \
  --output-tag gentle --draft
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh render \
  --project "sample-trip" --day-key 2025-02-05 \
  --plan-file .video-summary/sample-trip/edit-plan-balanced.json \
  --output-tag balanced --draft
```

The external planner is optional and requires the user's explicit opt-in. `local` is fully local; `codex` and `claude` send prompt/bounded candidate excerpts and metadata from an isolated request directory, with reduced contact sheets only when `--planner-images` is supplied. The bounded excerpts may include language that reveals a family interview, pickup, transfer, lodging stay, or terminal movement.

Family interview answers and meal context may appear in those bounded excerpts, and family faces or dining tables may appear in contact sheets when `--planner-images` is enabled. After rendering, verify `render-report.json.moment_coverage.family_interviews` and `.meals` are `satisfied` when their events were detected, `not_detected` when automatic evidence was absent, or `disabled` only after the matching explicit opt-out. For meals, confirm every filmed event has a selected body option and every reported setup/closure context group has a selected candidate; document an unrecorded body as an editorial `not_filmed` finding and use an honest bridge. Independently finish the local full-contact-sheet event inventory before treating these statuses as final approval. Verify travel waypoints through the candidates tagged `transition` and their presence, order, role, and `speed=1.0` in `edit-plan.json`.

Also inspect `render-report.json.moment_coverage.story_flow`. Compare raw source seconds with accounted candidate seconds and review `unassigned_source_seconds`; confirm every core event is represented, every explicit exclusion remains omitted, and no `protected_1x` candidate is accelerated. Review per-event treatment (`full`, `full_speed_up`, `compact`, `compact_speed_up`, or `omit`), compression savings, and each DAY's guard status. `review` means the output exceeds the pacing guard and needs human pacing inspection; it is not by itself a failure when the complete chronological story is justified.
