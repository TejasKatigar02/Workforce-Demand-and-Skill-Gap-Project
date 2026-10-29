chmod +x run_all.sh
./run_all.sh out#!/usr/bin/env bash
# One-command pipeline: generate twice (reproducibility), validate, mutation-test, starter build.
# Usage: ./run_all.sh [output_dir]   (default: out)
set -euo pipefail
OUT="${1:-out}"
rm -rf "$OUT"
python3 generate.py --preset full    --out "$OUT/full_run1"
python3 generate.py --preset full    --out "$OUT/full_run2"
python3 generate.py --preset starter --out "$OUT/starter"
python3 validate.py --data "$OUT/full_run1"
python3 validate.py --data "$OUT/starter"
python3 sql_check.py "$OUT/full_run1"
python3 repro_check.py "$OUT/full_run1" "$OUT/full_run2"
python3 mutation_test.py "$OUT/starter"
echo "ALL STAGES PASSED"
