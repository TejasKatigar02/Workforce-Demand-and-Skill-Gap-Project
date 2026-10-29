#!/usr/bin/env python3
"""mutation_test.py - proves validate.py catches deliberate, unlabelled corruption.

Copies a generated folder, applies one corruption at a time, and expects the
validator to fail. Usage: python3 mutation_test.py out/starter
"""
import csv, os, shutil, subprocess, sys, tempfile

src = sys.argv[1]

def edit_csv(path, fn):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    fn(header, body)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\n").writerows([header] + body)

def col(header, name): return header.index(name)

def m_overcommit(d):          # allocation above capacity with NO label
    edit_csv(os.path.join(d, "transactions/allocation.csv"), lambda h, b: [
        r.__setitem__(col(h, "allocated_fte"), "1.0") for r in b[:40]])

def m_gap_arithmetic(d):
    edit_csv(os.path.join(d, "analysis/gap_analysis.csv"), lambda h, b:
             b[5].__setitem__(col(h, "gap_fte"), "99.0"))

def m_pk_duplicate(d):
    edit_csv(os.path.join(d, "master/employee.csv"), lambda h, b:
             b[1].__setitem__(col(h, "employee_id"), b[0][col(h, "employee_id")]))

def m_fk_orphan(d):
    edit_csv(os.path.join(d, "transactions/training.csv"), lambda h, b:
             b[0].__setitem__(col(h, "skill_id"), "SKL-999999"))

def m_capacity_drift(d):
    edit_csv(os.path.join(d, "analysis/weekly_capacity.csv"), lambda h, b:
             b[10].__setitem__(col(h, "effective_capacity_fte"), "1.0"))

def m_decision_rule(d):
    edit_csv(os.path.join(d, "analysis/gap_analysis.csv"), lambda h, b:
             [r.__setitem__(col(h, "recommended_action"), "NONE") for r in b if float(r[col(h, "gap_fte")]) > 0][:1])

def m_leaver_lifecycle(d):
    edit_csv(os.path.join(d, "master/employee.csv"), lambda h, b:
             b[0].__setitem__(col(h, "employment_status"), "LEAVER"))

def m_date_order(d):
    edit_csv(os.path.join(d, "transactions/availability.csv"), lambda h, b:
             b[0].__setitem__(col(h, "end_date"), "2000-01-01"))

CASES = [m_overcommit, m_gap_arithmetic, m_pk_duplicate, m_fk_orphan,
         m_capacity_drift, m_decision_rule, m_leaver_lifecycle, m_date_order]
failures = 0
for fn in CASES:
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "data")
    shutil.copytree(src, dst)
    fn(dst)
    r = subprocess.run([sys.executable, "validate.py", "--data", dst], capture_output=True, text=True)
    caught = r.returncode != 0
    print(f"{fn.__name__:<20} {'CAUGHT' if caught else 'MISSED'}  {r.stdout.strip().splitlines()[0] if r.stdout.strip() else r.stderr[-200:]}")
    failures += (not caught)
    shutil.rmtree(tmp)
print("ALL MUTATIONS CAUGHT" if failures == 0 else f"{failures} mutation(s) missed")
sys.exit(1 if failures else 0)
