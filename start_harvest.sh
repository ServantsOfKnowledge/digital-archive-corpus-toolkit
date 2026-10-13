#!/bin/bash
# Start the TVU harvest in background with proper environment

set -e

cd /home/sokadmin/digital-archive-corpus-toolkit

# Activate the venv
source /home/sokadmin/tmvu-corpus-server-handoff/venv/bin/activate

echo "Starting harvest with venv: $(which python3)"
echo "Process will run in background..."
echo ""

# Run harvest in background
nohup python3 tdl_multi_worker.py \
  --total-workers 12 \
  --categories-parallel 3 \
  > harvest.log 2>&1 &

PID=$!
echo $PID > harvest.pid

echo "✓ Harvest started!"
echo "  PID: $PID"
echo "  Log: harvest.log"
echo "  PID file: harvest.pid"
echo ""
echo "Monitor with:"
echo "  tail -f harvest.log"
echo "  python3 monitor_realtime.py --dashboard"
echo ""
echo "Stop with:"
echo "  kill \$(cat harvest.pid)"
echo "  # or: pkill -f tdl_multi_worker"
