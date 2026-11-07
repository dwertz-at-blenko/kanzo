#!/usr/bin/env bash
# Version: v1 · created 2026-11-07 11:43 EST · updated 2026-11-07 11:43 EST
# Changes: v1 initial.
# This file: /home/claude/dev/kanzo/deploy/seed-projects.sh
# Session: 08cf0c04-265c-4b64-8e18-d5679b1a650d · vps as claude · cwd /home/claude/dev · slug vast-imagining-scott
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 08cf0c04-265c-4b64-8e18-d5679b1a650d'   (from the Mac)
# seed-projects.sh: one Kanzo project per active GitHub repo (blenko-glass-company = blue, dwertz-at-blenko = purple).
# Idempotent: existing projects are kept, the repo link and color are (re)applied. Archived repos are skipped.
# Run: bws run --project-id 9b09cc48-0a19-426c-b1fb-b4a2007f028a -- ~/dev/kanzo/deploy/seed-projects.sh [--dry-run]
set -u
TSV=$(mktemp); trap 'rm -f "$TSV"' EXIT
API=${KANZO_API:-http://127.0.0.1:8111/api}
DRY=${1:-}
declare -A COLOR=([blenko-glass-company]=blue [dwertz-at-blenko]=purple)
created=0 kept=0 skipped=0 failed=0
for owner in blenko-glass-company dwertz-at-blenko; do
  gh repo list "$owner" --limit 300 --json nameWithOwner,isArchived --jq '.[] | [.nameWithOwner, (.isArchived|tostring)] | @tsv' >> "$TSV"
done
for owner in blenko-glass-company dwertz-at-blenko; do
  while IFS=$'\t' read -r full archived; do
    [ "$archived" = "true" ] && { skipped=$((skipped+1)); continue; }
    name=${full#*/}
    slug=$(printf '%s' "$name" | tr 'A-Z_.' 'a-z--' | tr -cd 'a-z0-9-' | cut -c1-32)
    # the same repo name under both owners: the personal one gets a -personal suffix (still <= 32 chars)
    if [ "$owner" = dwertz-at-blenko ] && grep -q "^blenko-glass-company/$name	" "$TSV"; then slug="$(printf '%s' "$slug" | cut -c1-23)-personal"; fi
    if [ "$DRY" = --dry-run ]; then echo "$slug <- $full (${COLOR[$owner]})"; continue; fi
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d "{\"slug\":\"$slug\",\"name\":\"$name\",\"repo\":\"$full\"}" "$API/projects")
    case $code in
      200|201) created=$((created+1));;
      409|400) kept=$((kept+1)); curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "{\"repo\":\"$full\"}" "$API/projects/$slug/repos";;
      *) failed=$((failed+1)); echo "FAIL $code $slug ($full)";;
    esac
    curl -s -o /dev/null -X PATCH -H 'Content-Type: application/json' -d "{\"color\":\"${COLOR[$owner]}\"}" "$API/projects/$slug"
  done < <(grep "^$owner/" "$TSV")
done
echo "created $created, kept $kept, archived skipped $skipped, failed $failed"
