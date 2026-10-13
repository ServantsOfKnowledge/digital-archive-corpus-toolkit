#!/bin/bash
# Real-time harvest monitoring dashboard

while true; do
    clear
    echo "================================================================================"
    echo "  TDL HARVEST MONITOR - $(date '+%Y-%m-%d %H:%M:%S')"
    echo "================================================================================"
    echo ""

    # Show current state summary
    python3 analyze_state.py 2>/dev/null | grep -A 100 "CATEGORY ANALYSIS" | head -30

    echo ""
    echo "================================================================================"
    echo "  ACTIVE DOWNLOADS (last 5 minutes)"
    echo "================================================================================"

    # Check recent log activity
    for log in logs/*_download.log; do
        if [ -f "$log" ]; then
            cat_name=$(basename "$log" _download.log)

            # Check if log was modified in last 5 minutes
            if [ $(find "$log" -mmin -5 2>/dev/null | wc -l) -gt 0 ]; then
                # Count recent downloads
                recent=$(tail -100 "$log" 2>/dev/null | grep -c "Downloaded\|Uploaded\|SUCCESS" 2>/dev/null || true)
                errors=$(tail -100 "$log" 2>/dev/null | grep -c "ERROR\|FAILED\|Failed" 2>/dev/null || true)

                printf "%-20s: %3d recent actions, %3d errors\n" "$cat_name" "$recent" "$errors"
            fi
        fi
    done

    echo ""
    echo "================================================================================"
    echo "  SYSTEM RESOURCES"
    echo "================================================================================"

    # Disk usage
    df -h . | grep -v "Filesystem" | awk '{print "Disk: " $3 " used / " $2 " total (" $5 " full)"}'

    # Memory usage (if free command exists)
    if command -v free &> /dev/null; then
        free -h | grep "Mem:" | awk '{print "RAM:  " $3 " used / " $2 " total"}'
    fi

    # CPU load (if uptime command exists)
    if command -v uptime &> /dev/null; then
        uptime | awk -F'load average:' '{print "Load: " $2}'
    fi

    # Process count
    ps aux | grep -E "tdl_downloader|tdl_upload|tdl_multi_worker" | grep -v grep | wc -l | awk '{print "Active harvest processes: " $1}'

    echo ""
    echo "================================================================================"
    echo "  Press Ctrl+C to exit | Refreshing every 30 seconds..."
    echo "================================================================================"

    sleep 30
done
