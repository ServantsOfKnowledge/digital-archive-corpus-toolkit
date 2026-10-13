#!/bin/bash
# Stop the TVU harvest

cd /home/sokadmin/digital-archive-corpus-toolkit

if [ -f harvest.pid ]; then
    PID=$(cat harvest.pid)
    echo "Stopping harvest (PID: $PID)..."
    kill $PID 2>/dev/null && echo "✓ Main process stopped" || echo "⚠ Process not found"
    rm harvest.pid
else
    echo "No harvest.pid file found"
fi

echo ""
echo "Stopping any remaining harvest processes..."
pkill -f "tdl_multi_worker" && echo "✓ Multi-worker stopped" || echo "  (no multi-worker running)"
pkill -f "tdl_downloader.*digital-archive-corpus-toolkit" && echo "✓ Downloaders stopped" || echo "  (no downloaders running)"

echo ""
echo "Current harvest processes:"
ps aux | grep -E "tdl_multi_worker|tdl_downloader" | grep -v grep | grep -v tmvu-corpus-server-handoff || echo "  (none)"

echo ""
echo "✓ Harvest stopped"
