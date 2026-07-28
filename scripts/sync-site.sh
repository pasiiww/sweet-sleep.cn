#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-root@101.43.110.147}"
REMOTE_ROOT="${REMOTE_ROOT:-/www/wwwroot/myweb}"
SITE_URL="${SITE_URL:-https://sweet-sleep.cn/}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf '%s\n' \
    'Usage: ./scripts/sync-site.sh [commit message]' \
    '' \
    'Commits local changes, uploads the site files, and pushes the current Git branch.' \
    'Override REMOTE_HOST, REMOTE_ROOT, or SITE_URL when needed.'
  exit 0
fi

cd "$REPO_DIR"

commit_message="${1:-Sync website}"
current_branch="$(git branch --show-current)"
if [[ -z "$current_branch" ]]; then
  printf '%s\n' 'Cannot sync from a detached HEAD.' >&2
  exit 1
fi

site_files=(
  'index.html'
  'video.html'
  'images.json'
  'beian-gongan.jpg'
  'agents.md'
)
site_dirs=(
  'img'
  'ar'
  'menu'
  'gacha'
)

for path in "${site_files[@]}" "${site_dirs[@]}"; do
  if [[ ! -e "$path" ]]; then
    printf 'Missing site path: %s\n' "$path" >&2
    exit 1
  fi
done

git add -A
if git diff --cached --quiet; then
  printf '%s\n' 'No local Git changes to commit.'
else
  git commit -m "$commit_message"
fi

printf 'Uploading site files to %s:%s\n' "$REMOTE_HOST" "$REMOTE_ROOT"
scp -r "${site_files[@]}" "${site_dirs[@]}" "$REMOTE_HOST:$REMOTE_ROOT/"

printf 'Pushing branch %s to origin\n' "$current_branch"
git push origin "$current_branch"

if command -v curl >/dev/null 2>&1; then
  curl -fsSI "$SITE_URL" >/dev/null
  printf 'Live site check passed: %s\n' "$SITE_URL"
fi

printf '%s\n' 'Sync complete.'
