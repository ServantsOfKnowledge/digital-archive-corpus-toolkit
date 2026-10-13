#!/bin/bash
# Control the Shodhganga harvest: ./shg_harvest.sh start|stop|status
# Env: SHG_VENV (python venv), SHG_DIR (corpus dir), SHG_WORKERS, SHG_EXTRA (extra run flags)

cd "$(dirname "$0")" || exit 1
VENV="${SHG_VENV:-/home/sokadmin/tmvu-corpus-server-handoff/venv}"
DIR="${SHG_DIR:-/home/sokadmin/shodaganga/shg_corpus}"
WORKERS="${SHG_WORKERS:-4}"
PIDFILE=shg_harvest.pid
LOG=logs/shg_harvest.log

case "$1" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
        echo "Already running (PID $(cat $PIDFILE))"; exit 1
    fi
    mkdir -p logs
    rm -f "$DIR/harvest.stop"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    nohup python3 shg_harvest.py run --dir "$DIR" --workers "$WORKERS" --execute $SHG_EXTRA >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started PID $(cat $PIDFILE); log: $LOG"
    ;;
  stop)
    # Graceful: workers finish in-flight items, then exit.
    touch "$DIR/harvest.stop"
    echo "Stop requested (in-flight items finish first). Force: kill \$(cat $PIDFILE)"
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
        echo "RUNNING (PID $(cat $PIDFILE))"
    else
        echo "NOT running"
    fi
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    python3 shg_harvest.py status --dir "$DIR"
    echo "--- last log lines ---"; tail -5 "$LOG" 2>/dev/null
    ;;
  *) echo "usage: $0 start|stop|status"; exit 2 ;;
esac
