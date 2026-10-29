#!/usr/bin/env python3
"""repro_check.py - compares two generated folders file by file (SHA-256 and row counts).

Usage: python3 repro_check.py out/full_run1 out/full_run2
Exit code 1 if any file differs or the file sets differ.
"""
import hashlib, os, sys

def digest(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn == "validation_summary.json" or fn == "validation_summary.md":
                continue   # written by validate.py, not by the generator
            full = os.path.join(dirpath, fn)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = hashlib.sha256(f.read()).hexdigest()
    return out

def rows(root, rel):
    if not rel.endswith(".csv"):
        return None
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return sum(1 for _ in f) - 1

a, b = sys.argv[1], sys.argv[2]
da, db = digest(a), digest(b)
lines = [f"Reproducibility check: {a} vs {b}", ""]
only = sorted(set(da) ^ set(db))
diff = sorted(k for k in set(da) & set(db) if da[k] != db[k])
lines.append(f"Files compared: {len(set(da) & set(db))}")
lines.append(f"Files only in one run: {only if only else 'none'}")
lines.append(f"Files with different content: {diff if diff else 'none'}")
lines.append("")
lines.append("Row counts (CSV data rows, identical in both runs):")
for k in sorted(k for k in da if k.endswith(".csv")):
    if k in db:
        lines.append(f"  {k:<42} {rows(a, k):>8}  {'match' if rows(a,k)==rows(b,k) else 'MISMATCH'}")
ok = not only and not diff
lines.append("")
lines.append("RESULT: IDENTICAL (byte-for-byte)" if ok else "RESULT: DIFFERENT")
text = "\n".join(lines)
print(text)
os.makedirs(os.path.join(a, "docs"), exist_ok=True)
with open(os.path.join(a, "docs", "repro_report.txt"), "w", encoding="utf-8") as f:
    f.write(text + "\n")
sys.exit(0 if ok else 1)
