#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${1:-}"
INSTALL_ROOT="${2:-$HOME/.codex/video-summary/repo}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${SKILL_DIR}/video-summary.env"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/." >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required and must be installed before rendering." >&2
  exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe is required and must be installed before scanning." >&2
  exit 1
fi

if [[ -n "${REPO_URL}" ]]; then
  mkdir -p "$(dirname "${INSTALL_ROOT}")"
  if [[ -d "${INSTALL_ROOT}/.git" ]]; then
    git -C "${INSTALL_ROOT}" pull --ff-only
  else
    if [[ -e "${INSTALL_ROOT}" ]] && [[ -n "$(ls -A "${INSTALL_ROOT}" 2>/dev/null)" ]]; then
      echo "Install directory exists and is not an empty git repository: ${INSTALL_ROOT}" >&2
      exit 1
    fi
    git clone "${REPO_URL}" "${INSTALL_ROOT}"
  fi
  REPO_ROOT="${INSTALL_ROOT}"
elif [[ -f "${SKILL_DIR}/../pyproject.toml" && -d "${SKILL_DIR}/../video_summary" ]]; then
  REPO_ROOT="$(cd "${SKILL_DIR}/.." && pwd)"
elif [[ -f "${SKILL_DIR}/../../pyproject.toml" && -d "${SKILL_DIR}/../../video_summary" ]]; then
  REPO_ROOT="$(cd "${SKILL_DIR}/../.." && pwd)"
else
  echo "usage: bootstrap-video-summary.sh <repo-url> [install-dir]" >&2
  echo "Pass a GitHub repository URL when the skill is installed by itself." >&2
  exit 1
fi

cd "${REPO_ROOT}"
uv sync --frozen

cat > "${ENV_FILE}" <<EOF
export VIDEO_SUMMARY_REPO="${REPO_ROOT}"
EOF

echo "Video Summary bootstrap complete."
echo "Repository: ${REPO_ROOT}"
echo "Config file: ${ENV_FILE}"
