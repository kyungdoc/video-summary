# Workflow Core

This directory is the stable, model-agnostic specification for the video-editing workflow.

Current repository behavior:

- transcription is local-only
- transcription uses `CohereLabs/cohere-transcribe-03-2026` via `transformers`
- the default user path is still a single `run` command
- planning also writes day-based cue analysis artifacts before final rendering

Read in this order:

1. [pipeline.md](pipeline.md)
2. [examples/project_prompt.example.md](examples/project_prompt.example.md)

The goal is to keep the workflow spec independent from any one agent runtime.
