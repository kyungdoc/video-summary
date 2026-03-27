# Workflow Reference

Before running the workflow, make sure the skill has been bootstrapped. If
`~/.codex/skills/video-summary/video-summary.env` is missing, run:

```bash
bash ~/.codex/skills/video-summary/scripts/bootstrap-video-summary.sh \
  https://github.com/kyungdoc/video-summary.git
```

Then read `~/.codex/skills/video-summary/video-summary.env` and use
`$VIDEO_SUMMARY_REPO` as the working repository for all pipeline commands.

This skill wraps the repository workflow:

- one-shot prompt-first execution
- local ASR
- candidate packaging
- automatic day-based selection
- final timeline generation
- final render

The canonical docs live in:

- `workflow/pipeline.md`
