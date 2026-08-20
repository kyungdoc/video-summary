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

## Ordering and output contract

Within each DAY, all selected source segments remain in capture-time order. With `cold_open` enabled, only the earliest selected source may be labeled `hook`; it remains after the DAY's visual date card and is not moved ahead of it.

The local planner preserves the earliest candidate as the DAY's journey anchor and reserves room for a visually strong low- or no-speech scenery candidate when one is available. Visual quality and stable outdoor context remain valid selection signals even without transcript text.

The combined trip sequence is: global mosaic intro (or a classic title-card fallback) → DAY date card → all chronological source segments for that DAY, including the optional earliest hook → repeat for later DAYs → global outro.

For readable pacing, keep these recommended defaults unless the user asks otherwise:

```yaml
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

Overlapping or touching candidate windows from the same source are unioned before planning. Plan validation also rejects residual real-time overlap above the 1 ms tolerance from one source, including overlaps submitted by an external planner. Each rendered source is quantized to the nearest whole frame at the target fps; fps/PTS and A/V duration are normalized, padded/trimmed as needed, and validated before the cache is accepted.

`transition_seconds` defaults to `0.18` and accepts 0–1 seconds. The first source of each DAY fades in from black/silence after its date card, and the last source fades to black/silence before the next date card or the trip outro. Source-to-source joins within a DAY remain clean cuts; this is not a crossfade graph. Set the value to `0` to disable boundary fades.

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

The external planner is optional and requires the user's explicit opt-in. `local` is fully local; `codex` and `claude` send prompt/bounded candidate excerpts and metadata from an isolated request directory, with reduced contact sheets only when `--planner-images` is supplied.
