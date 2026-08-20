#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${SKILL_DIR}/video-summary.env"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
elif [[ -f "${SKILL_DIR}/../pyproject.toml" ]] && [[ -d "${SKILL_DIR}/../video_summary" ]]; then
  VIDEO_SUMMARY_REPO="$(cd "${SKILL_DIR}/.." && pwd)"
else
  echo "Missing ${ENV_FILE}. Run bootstrap-video-summary.sh first." >&2
  exit 1
fi

if [[ -z "${VIDEO_SUMMARY_REPO:-}" ]]; then
  echo "VIDEO_SUMMARY_REPO is not set in ${ENV_FILE}." >&2
  exit 1
fi

if [[ ! -d "${VIDEO_SUMMARY_REPO}" ]]; then
  echo "VIDEO_SUMMARY_REPO does not exist: ${VIDEO_SUMMARY_REPO}" >&2
  exit 1
fi

exec uv run \
  --project "${VIDEO_SUMMARY_REPO}" \
  --frozen \
  video-summary "$@"
