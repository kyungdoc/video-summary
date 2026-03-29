# Pipeline

## End-to-End Flow

1. A user provides a free-form editing prompt, either inline or from a text file.
2. Local transcription uses `CohereLabs/cohere-transcribe-03-2026` through `transformers` to generate cue-level transcripts for every clip.
3. Cue groups become `segment_candidates`.
4. Representative frames are extracted for each candidate.
5. Cue analysis scores each candidate by features such as event type, people, fun, emotion, food, scenery, and transition value, then writes a day-based analysis list.
6. `plan` automatically derives an internal selection from the prompt and candidate bundle.
7. `plan` validates that selection and materializes:
   - `cue_analysis_by_day.json`
   - `timeline_final.json`
8. `render` creates one final video from that timeline.
9. `run` performs the full flow in one shot.

## Failure Behavior

- No transcript cues: fail
- No candidates: fail
- No valid enabled selections: fail
- Out-of-bounds selection windows: fail

There is intentionally no heuristic fallback timeline generation.

## Default Loop

1. Install dependencies with `uv sync`
2. Run `python3 -m video_summary run --source-dir ... --prompt "..."`

Path handling rules:

- Use `--project-dir` when you want artifacts under a specific root.
- Otherwise, artifacts go under the current workspace root.

The pipeline is designed to work without manual editing of internal artifacts.
The default one-shot path stays a single `run` command.

## Advanced Loop

1. Run `plan --source-dir ... --prompt "..."`
2. Run `render`
