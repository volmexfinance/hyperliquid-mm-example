#!/bin/sh
# Restart a bot after unexpected exits; forward stop signals so mm.py can cancel.

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <bot.py> <log-file> [bot arguments...]" >&2
  exit 2
fi

bot=$1
log_file=$2
shift 2
base_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$base_dir" || exit 1
mkdir -p "$(dirname -- "$log_file")" || exit 1

stopping=0
child_pid=
backoff=2

request_stop() {
  stopping=1
  printf '%s RUNNER_STOP signal received\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$log_file"
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
}

trap request_stop HUP INT TERM

while [ "$stopping" -eq 0 ]; do
  start_time=$(date +%s)
  printf '%s BOT_START bot=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$bot" >> "$log_file"
  .venv/bin/python -u "$bot" "$@" >> "$log_file" 2>&1 &
  child_pid=$!
  wait "$child_pid"
  status=$?
  child_pid=
  end_time=$(date +%s)
  runtime=$((end_time - start_time))

  if [ "$stopping" -eq 1 ]; then
    break
  fi

  if [ "$runtime" -ge 300 ]; then
    backoff=2
  fi
  printf '%s RESTART bot=%s exit=%s runtime_seconds=%s backoff_seconds=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$bot" "$status" "$runtime" "$backoff" >> "$log_file"
  sleep "$backoff" &
  child_pid=$!
  wait "$child_pid"
  child_pid=
  if [ "$stopping" -eq 1 ]; then
    break
  fi
  if [ "$backoff" -lt 60 ]; then
    backoff=$((backoff * 2))
    if [ "$backoff" -gt 60 ]; then
      backoff=60
    fi
  fi
done

printf '%s RUNNER_EXIT bot=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$bot" >> "$log_file"
exit 0
