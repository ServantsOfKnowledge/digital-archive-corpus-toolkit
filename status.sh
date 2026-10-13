#!/bin/bash
# Check harvest status

echo "═══════════════════════════════════════════════════════════════════════════════"
echo "HARVEST STATUS CHECK"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Check if running
if pgrep -f "tdl_multi_worker" > /dev/null; then
    echo "✓ Harvest is RUNNING"
    if [ -f harvest.pid ]; then
        echo "  PID: $(cat harvest.pid)"
    fi
else
    echo "✗ Harvest is NOT running"
    echo "  Start with: ./start_harvest.sh"
fi

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "ACTIVE PROCESSES"
echo "───────────────────────────────────────────────────────────────────────────────"
ps aux | grep -E "tdl_multi_worker|tdl_downloader" | grep -v grep | grep -v tmvu-corpus-server-handoff | awk '{printf "  %-8s %-10s %-6s %s\n", $1, $2, $3"%", substr($0, index($0,$11))}'

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "LOG FILES"
echo "───────────────────────────────────────────────────────────────────────────────"
if [ -d logs ]; then
    ls -lh logs/*.log 2>/dev/null | awk '{printf "  %-20s %8s  %s\n", $9, $5, $6" "$7" "$8}' || echo "  (no logs yet)"
else
    echo "  (logs directory not created yet)"
fi

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "PROGRESS"
echo "───────────────────────────────────────────────────────────────────────────────"
if [ -f upload_progress.json ]; then
    source /home/sokadmin/tmvu-corpus-server-handoff/venv/bin/activate 2>/dev/null
    python3 -c "
import json
p = json.load(open('upload_progress.json'))
total = len(p.get('uploaded', []))
by_cat = p.get('uploaded_by_cat', {})
failed = len(p.get('failed', []))

# Calculate new items
BASELINE = 97147  # Items already on IA when we started
new_items = total - BASELINE if total > BASELINE else 0

print(f'  Already on IA:  {BASELINE:,} (skipped)')
print(f'  NEW items:      {new_items:,} ⭐')
print(f'  Total tracked:  {total:,}')
print(f'  Failed:         {failed:,}')
if by_cat:
    print(f'')
    print(f'  New by category:')
    for cat, count in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            print(f'    {cat:20s}: {count:6,}')
" 2>/dev/null || echo "  Error reading progress file"
else
    echo "  (no progress file yet)"
fi

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "RECENT ACTIVITY (last 10 lines from harvest.log)"
echo "───────────────────────────────────────────────────────────────────────────────"
if [ -f harvest.log ]; then
    tail -10 harvest.log | sed 's/^/  /'
else
    echo "  (no harvest log yet)"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "COMMANDS"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  Monitor:    python3 monitor_realtime.py --dashboard"
echo "  Logs:       tail -f harvest.log"
echo "  Stop:       ./stop_harvest.sh"
echo "═══════════════════════════════════════════════════════════════════════════════"
