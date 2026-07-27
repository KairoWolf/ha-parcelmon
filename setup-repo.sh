#!/usr/bin/env bash
# Stamp your GitHub username into the placeholders, then push.
#   ./setup-repo.sh <github-username>
set -euo pipefail

USER="${1:-}"
if [ -z "$USER" ]; then
  echo "usage: ./setup-repo.sh <github-username>" >&2
  exit 1
fi

grep -rl 'REPLACE_ME' . --exclude-dir=.git --exclude=setup-repo.sh \
  | xargs sed -i.bak "s|REPLACE_ME|${USER}|g"
find . -name '*.bak' -delete

echo "Stamped ${USER} into manifest.json, hacs.json, README.md and LICENSE."
echo
echo "Next:"
echo "  git init -b main"
echo "  git add -A"
echo "  git commit -m 'Parcelmon: AU parcel tracking for Home Assistant'"
echo "  gh repo create ha-parcelmon --public --source=. --push"
