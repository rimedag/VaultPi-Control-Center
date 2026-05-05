#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backup/gitea}"
STAGING_DIR="${STAGING_DIR:-${BACKUP_DIR}/staging}"
REPO_DIR="${REPO_DIR:-/var/lib/gitea/data/gitea-repositories}"
DATE="$(date +"%Y-%m-%d-%H-%M")"
RECIPIENT="${BACKUP_EMAIL_TO:-}"
EMAIL_FROM="${BACKUP_EMAIL_FROM:-}"
GITEA_BIN="${GITEA_BIN:-/usr/local/bin/gitea}"
GITEA_CONFIG_PATH="${GITEA_CONFIG_PATH:-/etc/gitea/app.ini}"
MSMTP_BIN="${MSMTP_BIN:-/usr/bin/msmtp}"

mkdir -p "$STAGING_DIR"
cd "$STAGING_DIR"

rm -f gitea-dump*.zip

START_TS=$(date +%s)
set +e
"$GITEA_BIN" --config "$GITEA_CONFIG_PATH" dump
STATUS=$?
set -e
END_TS=$(date +%s)
DURATION=$((END_TS - START_TS))

TOTAL_REPOS=$(find "$REPO_DIR" -mindepth 2 -maxdepth 2 -type d -name "*.git" | wc -l)
ORG_COUNT=$(find "$REPO_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)
ORG_BREAKDOWN=$(find "$REPO_DIR" -mindepth 2 -maxdepth 2 -type d -name "*.git" | awk -F/ '{print $(NF-1)}' | sort | uniq -c | awk '{printf "- %s: %s repos\n", $2, $1}')

if [ "$STATUS" -eq 0 ]; then
  FILE=$(ls -t gitea-dump*.zip | head -n 1)
  mv "$FILE" "$BACKUP_DIR/gitea-$DATE.zip"
  ls -t "$BACKUP_DIR"/gitea-*.zip | tail -n +2 | xargs -r rm -f

  SIZE=$(du -h "$BACKUP_DIR/gitea-$DATE.zip" | cut -f1)
  {
    echo "Gitea Backup Report"
    echo
    echo "Status: SUCCESS"
    echo "Date: $(date)"
    echo "File: gitea-$DATE.zip"
    echo "Size: $SIZE"
    echo "Duration: ${DURATION}s"
    echo "Location: $BACKUP_DIR"
    echo
    echo "Repository Summary"
    echo "Organizations: $ORG_COUNT"
    echo "Repositories: $TOTAL_REPOS"
    echo
    echo "Repos per organization:"
    echo "$ORG_BREAKDOWN"
  } | if [ -n "$RECIPIENT" ] && [ -n "$EMAIL_FROM" ] && [ -x "$MSMTP_BIN" ]; then
    {
      echo "From: $EMAIL_FROM"
      echo "To: $RECIPIENT"
      echo "Subject: Gitea Backup Report - SUCCESS"
      echo
      cat
    } | "$MSMTP_BIN" "$RECIPIENT"
  else
    cat >/dev/null
  fi
else
  {
    echo "Gitea Backup Report"
    echo
    echo "Status: FAILED"
    echo "Date: $(date)"
    echo "Stage: dump"
    echo "Exit code: $STATUS"
    echo
    echo "Repository Summary"
    echo "Organizations: $ORG_COUNT"
    echo "Repositories: $TOTAL_REPOS"
    echo
    echo "Repos per organization:"
    echo "$ORG_BREAKDOWN"
  } | if [ -n "$RECIPIENT" ] && [ -n "$EMAIL_FROM" ] && [ -x "$MSMTP_BIN" ]; then
    {
      echo "From: $EMAIL_FROM"
      echo "To: $RECIPIENT"
      echo "Subject: Gitea Backup Report - FAILED"
      echo
      cat
    } | "$MSMTP_BIN" "$RECIPIENT"
  else
    cat >/dev/null
  fi
fi

exit "$STATUS"
