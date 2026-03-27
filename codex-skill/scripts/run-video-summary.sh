#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${1:-}"
PROMPT_VALUE="${2:-}"
SOURCE_DIR="${3:-}"
ORIGINAL_CWD="$(pwd)"

if [[ -z "${PROJECT_NAME}" || -z "${PROMPT_VALUE}" ]]; then
  echo "usage: run-video-summary.sh <project-name> <prompt-or-prompt-file> [source-dir]" >&2
  exit 1
fi

ARGS=(run --project "${PROJECT_NAME}")

if [[ -f "${PROMPT_VALUE}" ]]; then
  if [[ "${PROMPT_VALUE}" = /* ]]; then
    RESOLVED_PROMPT="${PROMPT_VALUE}"
  else
    RESOLVED_PROMPT="${ORIGINAL_CWD}/${PROMPT_VALUE}"
  fi
  ARGS+=(--prompt-file "${RESOLVED_PROMPT}")
else
  ARGS+=(--prompt "${PROMPT_VALUE}")
fi

if [[ -n "${SOURCE_DIR}" ]]; then
  if [[ "${SOURCE_DIR}" = /* ]]; then
    RESOLVED_SOURCE_DIR="${SOURCE_DIR}"
  else
    RESOLVED_SOURCE_DIR="${ORIGINAL_CWD}/${SOURCE_DIR}"
  fi
  ARGS+=(--source-dir "${RESOLVED_SOURCE_DIR}")
fi

cd "$(dirname "$0")/../.."

.venv/bin/python -m video_summary "${ARGS[@]}"
