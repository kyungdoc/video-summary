# Pipeline

## End-to-End Flow

1. A user provides a free-form editing prompt, either inline or from a text file.
2. Local ASR generates cue-level transcripts for every clip.
3. Cue groups become `segment_candidates`.
4. Representative frames are extracted for each candidate.
5. `plan` automatically derives an internal selection from the prompt and candidate bundle.
6. `plan` validates that selection and materializes:
   - `timeline_final.json`
7. `render` creates one final video from that timeline.
8. `run` performs the full flow in one shot.

## Failure Behavior

- No transcript cues: fail
- No candidates: fail
- No valid enabled selections: fail
- Out-of-bounds selection windows: fail

There is intentionally no heuristic fallback timeline generation.

## Default Loop

1. Install dependencies with `uv sync`
2. Run `python3 -m video_summary run --source-dir ... --prompt "..."`

The pipeline is designed to work without manual editing of internal artifacts.

## Advanced Loop

1. Run `plan --source-dir ... --prompt "..."`
2. Run `render`
