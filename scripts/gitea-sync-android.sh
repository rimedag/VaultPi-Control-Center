#!/usr/bin/env bash
# Manual Gitea -> Android sync job.
# Safe for web-triggered non-interactive use.

set -u
set -o pipefail

JOB_NAME="gitea-sync-android"
LOCK_FILE="/tmp/gitea-sync-android.lock"
LOG_FILE="/var/log/vaultpi/gitea-sync-android-run.log"
STATUS_FILE="/var/lib/vaultpi/status/gitea-sync-android.json"

LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-/backup/gitea}"
LOCAL_REPO_ROOT="${LOCAL_REPO_ROOT:-/var/lib/gitea/data/gitea-repositories}"

PHONE_HOST="${PHONE_HOST:-phone.lan}"
PHONE_PORT="${PHONE_PORT:-8022}"
PHONE_USER="${PHONE_USER:-git}"
ANDROID_GITEA_URL="${ANDROID_GITEA_URL:-http://phone.lan:3000}"

ANDROID_HOME="${ANDROID_HOME:-/data/data/com.termux/files/home}"
ANDROID_BACKUP_DIR="${ANDROID_BACKUP_DIR:-${ANDROID_HOME}/gitea-backups}"
ANDROID_MIRROR_DIR="${ANDROID_MIRROR_DIR:-${ANDROID_HOME}/gitea-mirrors}"

EMAIL_TO="alerts@example.com"
EMAIL_FROM="vaultpi@example.com"
MSMTP_BIN="/usr/bin/msmtp"

PING_BIN="/bin/ping"
SSH_BIN="/usr/bin/ssh"
RSYNC_BIN="/usr/bin/rsync"
GIT_BIN="/usr/bin/git"
DATE_BIN="/bin/date"
FIND_BIN="/usr/bin/find"
STAT_BIN="/usr/bin/stat"
HEAD_BIN="/usr/bin/head"
SED_BIN="/bin/sed"
AWK_BIN="/usr/bin/awk"
SORT_BIN="/usr/bin/sort"

START_TS="$(${DATE_BIN} -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(${DATE_BIN} +%s)"
SCRIPT_EXIT_CODE=1

OVERALL_STATUS="FAILED"
PHONE_REACHABLE="false"
BACKUP_COPY_STATUS="failed"
BACKUP_RETENTION_STATUS="skipped"
REPO_MIRROR_STATUS="failed"
LAST_MESSAGE="Not started."
FAIL_REASONS=""

LATEST_BACKUP_PATH=""
LATEST_BACKUP_FILE=""
LATEST_BACKUP_SIZE="0B"
LATEST_BACKUP_SIZE_BYTES=0
BACKUP_COPY_DURATION=0

TOTAL_REPOS=0
MIRROR_SUCCESS=0
MIRROR_FAILED=0
ORG_COUNTS_LINES=""
MIRROR_FAILURE_LINES=""

mkdir -p "$(dirname "${LOG_FILE}")" >/dev/null 2>&1 || true
mkdir -p "$(dirname "${STATUS_FILE}")" >/dev/null 2>&1 || true

exec >>"${LOG_FILE}" 2>&1
cd / >/dev/null 2>&1 || true

log() {
  printf '[%s] %s\n' "$(${DATE_BIN} -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

json_escape() {
  # shellcheck disable=SC2001
  printf '%s' "$1" | ${SED_BIN} -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a;N;$!ba;s/\n/\\n/g'
}

human_bytes() {
  local bytes="$1"
  if [ "${bytes}" -lt 1024 ]; then
    printf '%sB' "${bytes}"
  elif [ "${bytes}" -lt 1048576 ]; then
    printf '%.1fK' "$(${AWK_BIN} "BEGIN {print ${bytes}/1024}")"
  elif [ "${bytes}" -lt 1073741824 ]; then
    printf '%.1fM' "$(${AWK_BIN} "BEGIN {print ${bytes}/1048576}")"
  else
    printf '%.1fG' "$(${AWK_BIN} "BEGIN {print ${bytes}/1073741824}")"
  fi
}

write_status_json() {
  local running="$1"
  local finished_at="$2"
  local duration="$3"
  local msg_escaped
  msg_escaped="$(json_escape "${LAST_MESSAGE}")"

  cat >"${STATUS_FILE}" <<EOF
{
  "job": "${JOB_NAME}",
  "running": ${running},
  "last_started_at": "${START_TS}",
  "last_finished_at": "${finished_at}",
  "last_status": "$(printf '%s' "${OVERALL_STATUS}" | tr '[:upper:]' '[:lower:]')",
  "last_exit_code": ${SCRIPT_EXIT_CODE},
  "phone_reachable": ${PHONE_REACHABLE},
  "backup_copy_status": "${BACKUP_COPY_STATUS}",
  "backup_retention_status": "${BACKUP_RETENTION_STATUS}",
  "repo_mirror_status": "${REPO_MIRROR_STATUS}",
  "total_repos": ${TOTAL_REPOS},
  "mirrored_success": ${MIRROR_SUCCESS},
  "mirrored_failed": ${MIRROR_FAILED},
  "latest_backup_file": "${LATEST_BACKUP_FILE}",
  "latest_backup_size": "${LATEST_BACKUP_SIZE}",
  "last_duration_seconds": ${duration},
  "last_message": "${msg_escaped}"
}
EOF
}

send_report_email() {
  local finished_ts="$1"
  local duration="$2"
  local subject="Gitea Android Sync Report - ${OVERALL_STATUS}"

  if [ ! -x "${MSMTP_BIN}" ]; then
    log "WARN: msmtp not found at ${MSMTP_BIN}; report email skipped."
    return
  fi

  {
    printf 'From: %s\n' "${EMAIL_FROM}"
    printf 'To: %s\n' "${EMAIL_TO}"
    printf 'Subject: %s\n' "${subject}"
    printf '\n'
    printf 'Gitea Android Sync Report\n\n'
    printf 'Status: %s\n' "${OVERALL_STATUS}"
    printf 'Date (UTC): %s\n' "${finished_ts}"
    printf 'Phone reachable: %s\n' "$( [ "${PHONE_REACHABLE}" = "true" ] && printf 'YES' || printf 'NO' )"
    printf '\nBackup Copy\n-----------\n'
    printf 'Local backup file: %s\n' "${LATEST_BACKUP_FILE:-N/A}"
    printf 'Backup size: %s\n' "${LATEST_BACKUP_SIZE:-N/A}"
    printf 'Copy to phone: %s\n' "${BACKUP_COPY_STATUS^^}"
    printf 'Copy duration: %ss\n' "${BACKUP_COPY_DURATION}"
    printf 'Phone backup retention: %s\n' "${BACKUP_RETENTION_STATUS^^}"
    printf '\nRepository Mirroring\n--------------------\n'
    printf 'Repositories found: %s\n' "${TOTAL_REPOS}"
    printf 'Mirrored successfully: %s\n' "${MIRROR_SUCCESS}"
    printf 'Failed: %s\n' "${MIRROR_FAILED}"
    if [ -n "${ORG_COUNTS_LINES}" ]; then
      printf '\nRepos per organization:\n%s\n' "${ORG_COUNTS_LINES}"
    fi
    if [ -n "${MIRROR_FAILURE_LINES}" ] || [ -n "${FAIL_REASONS}" ]; then
      printf '\nFailures\n--------\n'
      [ -n "${FAIL_REASONS}" ] && printf '%s\n' "${FAIL_REASONS}"
      [ -n "${MIRROR_FAILURE_LINES}" ] && printf '%s\n' "${MIRROR_FAILURE_LINES}"
    fi
    printf '\nDuration\n--------\n%ss\n' "${duration}"
    printf '\nLog file: %s\n' "${LOG_FILE}"
  } | "${MSMTP_BIN}" -t
}

cleanup_lock() {
  if [ -f "${LOCK_FILE}" ]; then
    rm -f -- "${LOCK_FILE}" >/dev/null 2>&1 || true
  fi
}

finalize_and_exit() {
  local end_ts end_epoch duration
  end_ts="$(${DATE_BIN} -u +%Y-%m-%dT%H:%M:%SZ)"
  end_epoch="$(${DATE_BIN} +%s)"
  duration=$((end_epoch - START_EPOCH))

  write_status_json "false" "${end_ts}" "${duration}"
  send_report_email "${end_ts}" "${duration}" || true

  log "Finished ${JOB_NAME}: status=${OVERALL_STATUS} exit_code=${SCRIPT_EXIT_CODE} duration=${duration}s"
  exit "${SCRIPT_EXIT_CODE}"
}

trap 'cleanup_lock' EXIT

log "Starting ${JOB_NAME}"
write_status_json "true" "" "0"

# Lock acquisition with stale lock cleanup.
if [ -f "${LOCK_FILE}" ]; then
  OLD_PID="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
  if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
    OVERALL_STATUS="FAILED"
    LAST_MESSAGE="Another sync is already running (pid=${OLD_PID})."
    FAIL_REASONS="- Another sync is already running."
    SCRIPT_EXIT_CODE=10
    log "${LAST_MESSAGE}"
    finalize_and_exit
  fi
  log "Stale lock detected; removing ${LOCK_FILE}"
  rm -f -- "${LOCK_FILE}" >/dev/null 2>&1 || true
fi
echo "$$" >"${LOCK_FILE}"

# Dependency checks
for cmd in "${PING_BIN}" "${SSH_BIN}" "${RSYNC_BIN}" "${GIT_BIN}" "${FIND_BIN}" "${STAT_BIN}" "${HEAD_BIN}" "${SED_BIN}" "${AWK_BIN}" "${SORT_BIN}"; do
  if [ ! -x "${cmd}" ]; then
    OVERALL_STATUS="FAILED"
    LAST_MESSAGE="Missing required dependency: ${cmd}"
    FAIL_REASONS="- Missing required dependency: ${cmd}"
    SCRIPT_EXIT_CODE=11
    log "${LAST_MESSAGE}"
    finalize_and_exit
  fi
done

SSH_OPTS=(-p "${PHONE_PORT}" -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
REMOTE="${PHONE_USER}@${PHONE_HOST}"

# A) Pre-flight checks
if ! "${PING_BIN}" -c 1 -W 2 "${PHONE_HOST}" >/dev/null 2>&1; then
  OVERALL_STATUS="FAILED"
  PHONE_REACHABLE="false"
  LAST_MESSAGE="FAILED - PHONE UNREACHABLE (ping failed)"
  FAIL_REASONS="- Phone unreachable: ping to ${PHONE_HOST} failed."
  SCRIPT_EXIT_CODE=20
  log "${LAST_MESSAGE}"
  finalize_and_exit
fi

if ! "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" "echo ok" >/dev/null 2>&1; then
  OVERALL_STATUS="FAILED"
  PHONE_REACHABLE="false"
  LAST_MESSAGE="FAILED - PHONE UNREACHABLE (SSH failed)"
  FAIL_REASONS="- Phone unreachable: SSH to ${REMOTE}:${PHONE_PORT} failed."
  SCRIPT_EXIT_CODE=21
  log "${LAST_MESSAGE}"
  finalize_and_exit
fi

PHONE_REACHABLE="true"
log "Phone reachability check passed."

# C) Latest backup selection
LATEST_BACKUP_PATH="$(${FIND_BIN} "${LOCAL_BACKUP_DIR}" -maxdepth 1 -type f -name 'gitea-*.zip' -printf '%T@ %p\n' 2>/dev/null | ${SORT_BIN} -nr | ${HEAD_BIN} -n1 | ${AWK_BIN} '{print $2}')"
if [ -z "${LATEST_BACKUP_PATH}" ] || [ ! -f "${LATEST_BACKUP_PATH}" ]; then
  OVERALL_STATUS="FAILED"
  LAST_MESSAGE="FAILED - NO LOCAL BACKUP FOUND"
  FAIL_REASONS="- No local backup file matching ${LOCAL_BACKUP_DIR}/gitea-*.zip"
  SCRIPT_EXIT_CODE=22
  log "${LAST_MESSAGE}"
  finalize_and_exit
fi

LATEST_BACKUP_FILE="$(basename "${LATEST_BACKUP_PATH}")"
LATEST_BACKUP_SIZE_BYTES="$(${STAT_BIN} -c '%s' "${LATEST_BACKUP_PATH}" 2>/dev/null || echo 0)"
LATEST_BACKUP_SIZE="$(human_bytes "${LATEST_BACKUP_SIZE_BYTES}")"
log "Selected latest backup: ${LATEST_BACKUP_FILE} (${LATEST_BACKUP_SIZE})"

# Ensure destination dirs exist on phone.
if ! "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" "mkdir -p '${ANDROID_BACKUP_DIR}' '${ANDROID_MIRROR_DIR}'"; then
  OVERALL_STATUS="FAILED"
  LAST_MESSAGE="Unable to create backup/mirror directories on phone."
  FAIL_REASONS="- Unable to create ${ANDROID_BACKUP_DIR} and/or ${ANDROID_MIRROR_DIR} on phone."
  SCRIPT_EXIT_CODE=23
  log "${LAST_MESSAGE}"
  finalize_and_exit
fi

# D) Copy latest backup to phone (resumable rsync, up to 3 tries).
copy_try=1
copy_ok=0
copy_start="$(${DATE_BIN} +%s)"
while [ "${copy_try}" -le 3 ]; do
  log "Backup copy attempt ${copy_try}/3"
  if "${RSYNC_BIN}" -a --partial --append-verify --inplace --timeout=180 \
      -e "${SSH_BIN} -p ${PHONE_PORT} -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=3" \
      "${LATEST_BACKUP_PATH}" "${REMOTE}:${ANDROID_BACKUP_DIR}/"; then
    copy_ok=1
    break
  fi
  copy_try=$((copy_try + 1))
  sleep 4
done
copy_end="$(${DATE_BIN} +%s)"
BACKUP_COPY_DURATION=$((copy_end - copy_start))

if [ "${copy_ok}" -eq 1 ]; then
  BACKUP_COPY_STATUS="success"
  log "Backup copy succeeded in ${BACKUP_COPY_DURATION}s"
else
  BACKUP_COPY_STATUS="failed"
  OVERALL_STATUS="FAILED"
  LAST_MESSAGE="Backup copy to phone failed after retries."
  FAIL_REASONS="${FAIL_REASONS}
- Backup copy failed after 3 rsync attempts."
  SCRIPT_EXIT_CODE=24
  log "${LAST_MESSAGE}"
  finalize_and_exit
fi

# E) Keep only the newest phone backup (only after successful copy).
if "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" \
  "ls -1t '${ANDROID_BACKUP_DIR}'/gitea-*.zip 2>/dev/null | tail -n +2 | while read -r old; do [ -n \"\$old\" ] && rm -f -- \"\$old\"; done"; then
  BACKUP_RETENTION_STATUS="success"
  log "Phone backup retention cleanup succeeded (keep newest 1)."
else
  BACKUP_RETENTION_STATUS="failed"
  FAIL_REASONS="${FAIL_REASONS}
- Phone backup retention cleanup failed."
  log "WARN: phone backup retention cleanup failed."
fi

# F) Mirror all repos to phone.
log "Starting repository mirror sync from ${LOCAL_REPO_ROOT}"
if [ ! -d "${LOCAL_REPO_ROOT}" ]; then
  OVERALL_STATUS="FAILED"
  LAST_MESSAGE="Local repo root not found: ${LOCAL_REPO_ROOT}"
  FAIL_REASONS="${FAIL_REASONS}
- Local repo root not found: ${LOCAL_REPO_ROOT}"
  SCRIPT_EXIT_CODE=25
  finalize_and_exit
fi

REPO_LIST="$(${FIND_BIN} "${LOCAL_REPO_ROOT}" -type d -name '*.git' | ${SORT_BIN})"
if [ -z "${REPO_LIST}" ]; then
  TOTAL_REPOS=0
  MIRROR_SUCCESS=0
  MIRROR_FAILED=0
  REPO_MIRROR_STATUS="success"
  log "No repositories found to mirror."
else
  # repo/org summary via first path segment under LOCAL_REPO_ROOT
  ORG_COUNTS_LINES="$(printf '%s\n' "${REPO_LIST}" | ${SED_BIN} "s#^${LOCAL_REPO_ROOT}/##" | ${AWK_BIN} -F/ '{org[$1]++} END {for (o in org) printf("- %s: %d repos\n", o, org[o])}' | ${SORT_BIN})"

  while IFS= read -r local_repo; do
    [ -z "${local_repo}" ] && continue
    TOTAL_REPOS=$((TOTAL_REPOS + 1))
    rel_path="${local_repo#${LOCAL_REPO_ROOT}/}"
    remote_repo="${ANDROID_MIRROR_DIR}/${rel_path}"
    remote_repo_dir="$(dirname "${remote_repo}")"

    if ! "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" "mkdir -p '${remote_repo_dir}'"; then
      MIRROR_FAILED=$((MIRROR_FAILED + 1))
      MIRROR_FAILURE_LINES="${MIRROR_FAILURE_LINES}
- ${rel_path}: failed to create remote directory"
      log "WARN: ${rel_path} -> failed to create remote dir"
      continue
    fi

    if ! "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" "[ -d '${remote_repo}' ]"; then
      if ! "${SSH_BIN}" "${SSH_OPTS[@]}" "${REMOTE}" "git init --bare '${remote_repo}' >/dev/null 2>&1"; then
        MIRROR_FAILED=$((MIRROR_FAILED + 1))
        MIRROR_FAILURE_LINES="${MIRROR_FAILURE_LINES}
- ${rel_path}: failed to init bare repo on phone"
        log "WARN: ${rel_path} -> failed remote init"
        continue
      fi
    fi

    mirror_try=1
    mirror_ok=0
    while [ "${mirror_try}" -le 2 ]; do
      if "${GIT_BIN}" -C "${local_repo}" push --mirror "ssh://${PHONE_USER}@${PHONE_HOST}:${PHONE_PORT}${remote_repo}" >/dev/null 2>&1; then
        mirror_ok=1
        break
      fi
      mirror_try=$((mirror_try + 1))
      sleep 2
    done

    if [ "${mirror_ok}" -eq 1 ]; then
      MIRROR_SUCCESS=$((MIRROR_SUCCESS + 1))
      log "Mirror OK: ${rel_path}"
    else
      MIRROR_FAILED=$((MIRROR_FAILED + 1))
      MIRROR_FAILURE_LINES="${MIRROR_FAILURE_LINES}
- ${rel_path}: git push --mirror failed"
      log "WARN: Mirror failed: ${rel_path}"
    fi
  done <<EOF
${REPO_LIST}
EOF

  if [ "${MIRROR_FAILED}" -eq 0 ]; then
    REPO_MIRROR_STATUS="success"
  elif [ "${MIRROR_SUCCESS}" -gt 0 ]; then
    REPO_MIRROR_STATUS="partial"
  else
    REPO_MIRROR_STATUS="failed"
  fi
fi

# Final status resolution
if [ "${BACKUP_COPY_STATUS}" = "success" ] && [ "${REPO_MIRROR_STATUS}" = "success" ] && [ "${BACKUP_RETENTION_STATUS}" != "failed" ]; then
  OVERALL_STATUS="SUCCESS"
  SCRIPT_EXIT_CODE=0
  LAST_MESSAGE="Backup copied, phone backups pruned, repo mirroring succeeded."
elif [ "${BACKUP_COPY_STATUS}" = "success" ] && { [ "${REPO_MIRROR_STATUS}" = "partial" ] || [ "${BACKUP_RETENTION_STATUS}" = "failed" ]; }; then
  OVERALL_STATUS="PARTIAL"
  SCRIPT_EXIT_CODE=30
  LAST_MESSAGE="Backup copied, with partial issues during retention or repo mirroring."
else
  OVERALL_STATUS="FAILED"
  [ "${SCRIPT_EXIT_CODE}" -eq 0 ] && SCRIPT_EXIT_CODE=31
  LAST_MESSAGE="Android sync failed."
fi

log "Summary: copy=${BACKUP_COPY_STATUS}, retention=${BACKUP_RETENTION_STATUS}, mirror=${REPO_MIRROR_STATUS}, total=${TOTAL_REPOS}, ok=${MIRROR_SUCCESS}, failed=${MIRROR_FAILED}"
finalize_and_exit
