# Workflow Reference

Before running the workflow, make sure the skill has been bootstrapped. If the
skill-local `video-summary.env` file is missing, run the bundled bootstrap
script from this skill's `scripts/` directory:

```bash
bash /absolute/path/to/this-skill/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

Then read the generated `video-summary.env` file from this skill directory and
use `$VIDEO_SUMMARY_REPO` as the working repository for all pipeline commands.
Do not run the pipeline from the user's raw-media project folder; pass that
folder through `--source-dir` instead.

Resolve user input in this order:

1. explicit path mentioned in the user's prompt
2. obvious source folder in the current working directory
3. a short clarification question only if multiple candidates remain

Prefer the wrapper script so the repository and `uv` environment are fixed:

```bash
bash /absolute/path/to/this-skill/scripts/run-video-summary.sh run \
  --project "sample-trip" \
  --source-dir "/absolute/path/to/raw-clips" \
  --prompt "여행 브이로그를 따뜻하고 여유롭게 편집해줘."
```

Keep outputs under the user's current workspace root by default.
Pass `--project-dir` only when the user explicitly wants a different output location.

This skill wraps the repository workflow:

- one-shot prompt-first execution
- local-only transcription with `CohereLabs/cohere-transcribe-03-2026`
- candidate packaging
- day-based cue analysis
- automatic day-based selection
- final timeline generation
- final render

The default path remains a single wrapper command using `run`.

After bootstrap, the canonical workflow docs live in:

- `workflow/pipeline.md`
