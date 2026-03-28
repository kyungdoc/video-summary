#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${SKILL_DIR}/video-summary.env"
CALLER_CWD="$(pwd)"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Run bootstrap-video-summary.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

if [[ -z "${VIDEO_SUMMARY_REPO:-}" ]]; then
  echo "VIDEO_SUMMARY_REPO is not set in ${ENV_FILE}." >&2
  exit 1
fi

if [[ ! -d "${VIDEO_SUMMARY_REPO}" ]]; then
  echo "VIDEO_SUMMARY_REPO does not exist: ${VIDEO_SUMMARY_REPO}" >&2
  exit 1
fi

if [[ -z "${VIDEO_SUMMARY_PROJECT_DIR:-}" ]]; then
  export VIDEO_SUMMARY_PROJECT_DIR="${CALLER_CWD}"
fi

cd "${VIDEO_SUMMARY_REPO}"
exec uv run python -m video_summary "$@"
