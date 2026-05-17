#!/usr/bin/env python3
import json, subprocess, sys, time

r = subprocess.run(
    ["ia", "search", "collection:ServantsOfKnowledge AND identifier:tdl.*"],
    capture_output=True, text=True, timeout=120
)
idents = []
for line in r.stdout.strip().split('\n'):
    if line.strip():
        try:
            d = json.loads(line)
            idents.append(d['identifier'])
        except:
            pass

print(f"Found {len(idents)} items to update")
done = 0
errors = 0
skipped = 0

for i, ident in enumerate(idents):
    if i > 0 and i % 50 == 0:
        print(f"  progress: {i}/{len(idents)} ({done} ok, {errors} err, {skipped} skip)")

    # Check if already has correct metadata
    try:
        ck = subprocess.run(["ia", "metadata", ident], capture_output=True, text=True, timeout=15)
        if ck.returncode == 0:
            d = json.loads(ck.stdout)
            m = d.get('metadata', {})
            coll = m.get('collection', [])
            if isinstance(coll, list) and 'TamilVirtualAcademy' in coll and m.get('language') == 'tam':
                skipped += 1
                continue
    except:
        pass

    for attempt in range(3):
        try:
            r = subprocess.run(
                ["ia", "metadata", ident,
                 "-m", "language:tam",
                 "-m", "collection:TamilVirtualAcademy",
                 "-m", "collection:JaiGyan"],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode == 0:
                done += 1
                break
            err = (r.stderr or "").lower()
            if "timeout" in err or "connection" in err:
                if attempt < 2:
                    wait = 10 * (2 ** attempt)
                    time.sleep(wait)
                    continue
            errors += 1
            print(f"  ✗ {ident}: {r.stderr.strip()[:120]}")
            break
        except subprocess.TimeoutExpired:
            if attempt < 2:
                wait = 10 * (2 ** attempt)
                time.sleep(wait)
                continue
            errors += 1
            print(f"  ✗ {ident}: Timeout after 120s")

print(f"\nDone: {done} updated, {skipped} already correct, {errors} errors")
