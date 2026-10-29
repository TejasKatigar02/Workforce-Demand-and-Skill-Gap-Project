#!/usr/bin/env python3
"""
validate.py - independent checks on a generated dataset folder.

Re-derives every computed value from the raw input tables (it does not trust the
generator's derived columns) and checks:
  PK uniqueness, FK integrity, enums, date ordering, lifecycle transitions,
  allocation rules, arithmetic reconciliation (capacity, plan totals, gaps),
  recommendation-rule consistency, duplicates.

Pass rule: every violation must be labelled by ground_truth (row id match), and
every primary labelled anomaly must be detected. Anything else fails the run.

Usage: python3 validate.py --data out/full_run1
"""
import argparse, csv, json, math, os, sys
from collections import defaultdict
from datetime import date, timedelta

import generate as G

TOL = 1e-3


def conv(v, typ):
    if v == "":
        return None
    if typ == "int":
        return int(v)
    if typ == "float":
        return float(v)
    if typ == "date":
        return date.fromisoformat(v)
    if typ == "bool":
        return v == "true"
    return v


def load_table(data, name):
    types = {c[0]: c[1] for c in G.SCHEMA[name]["columns"]}
    with open(os.path.join(data, G.LAYOUT[name]), newline="", encoding="utf-8") as f:
        return [{k: conv(v, types.get(k, "string")) for k, v in raw.items()} for raw in csv.DictReader(f)]


def validate(data):
    T = {n: load_table(data, n) for n in G.LAYOUT}
    with open(os.path.join(data, "ground_truth", "ground_truth.json"), encoding="utf-8") as f:
        gt = json.load(f)["anomalies"]
    V, checks_run = [], defaultdict(int)

    def flag(check, table, rid, detail):
        V.append({"check": check, "table": table, "row_id": rid, "detail": detail})

    def ok(check, n=1):
        checks_run[check] += n

    emp = {e["employee_id"]: e for e in T["employee"]}
    emp_idx = {e["employee_id"]: i for i, e in enumerate(T["employee"])}
    N = len(T["employee"])
    role = {r["role_id"]: r for r in T["role"]}
    skill = {s["skill_id"]: s for s in T["skill"]}
    proj = {p["project_id"]: p for p in T["project"]}
    line = {l["demand_line_id"]: l for l in T["demand_line"]}
    plan = {p["demand_id"]: p for p in T["demand_plan"]}
    es_key = {(r["employee_id"], r["skill_id"]): r for r in T["employee_skill"]}
    alloc_all = T["allocation"]
    W = G.WEEKS

    # 1. Primary keys -----------------------------------------------------
    for name, spec in G.SCHEMA.items():
        seen = set()
        for r in T[name]:
            k = tuple(r[c] for c in spec["pk"])
            ok("PK_UNIQUE")
            if k in seen:
                flag("PK_UNIQUE", name, str(k[0]), "duplicate primary key")
            seen.add(k)

    # 2. Foreign keys -----------------------------------------------------
    for child, parent in G.FKS.items():
        ct, cc = child.split(".")
        pt, pc = parent.split(".")
        parents = {r[pc] for r in T[pt]}
        pk = G.SCHEMA[ct]["pk"][0]
        for r in T[ct]:
            ok("FK_INTEGRITY")
            if r[cc] is not None and r[cc] not in parents:
                flag("FK_INTEGRITY", ct, str(r[pk]), f"{cc}={r[cc]} not found in {pt}.{pc}")

    # 3. Enumerations ------------------------------------------------------
    for (t, c), allowed in G.ENUMS.items():
        pk = G.SCHEMA[t]["pk"][0]
        for r in T[t]:
            ok("ENUM")
            if r[c] not in allowed:
                flag("ENUM", t, str(r[pk]), f"{c}={r[c]} not allowed")

    # 4. Employee lifecycle and dates ---------------------------------------
    for e in T["employee"]:
        ok("EMP_LIFECYCLE", 3)
        if (e["employment_status"] == "LEAVER") != (e["leaver_date"] is not None):
            flag("EMP_LIFECYCLE", "employee", e["employee_id"], "leaver_date iff LEAVER violated")
        if e["leaver_date"] and not (e["hire_date"] < e["leaver_date"]):
            flag("EMP_LIFECYCLE", "employee", e["employee_id"], "leaver before hire")
        if e["manager_id"] is not None:
            m = emp.get(e["manager_id"]) or {"level": None, "employee_id": None}
            if m["level"] != "LEAD" or m["employee_id"] == e["employee_id"]:
                flag("EMP_MANAGER", "employee", e["employee_id"], "manager must be a different LEAD")
        ok("EMP_FAMILY")
        if e["job_family"] != role[e["role_id"]]["job_family"]:
            flag("EMP_FAMILY", "employee", e["employee_id"], "job_family differs from role")

    # 5. Skills: expiry arithmetic, status, verification dates --------------
    for r in T["employee_skill"]:
        ok("SKILL_DATES", 3)
        if r["skill_id"] not in skill or r["employee_id"] not in emp:
            continue                       # dangling FK is reported by FK_INTEGRITY
        months = skill[r["skill_id"]]["expiry_months"]
        exp = G.add_months(r["last_verified_date"], months) if months else None
        if exp != r["expiry_date"]:
            flag("SKILL_DATES", "employee_skill", r["employee_skill_id"], "expiry_date != last_verified + months")
        if r["last_verified_date"] < r["base_verified_date"]:
            flag("SKILL_DATES", "employee_skill", r["employee_skill_id"], "last_verified before base_verified")
        if r["last_verified_date"] < emp[r["employee_id"]]["hire_date"]:
            flag("SKILL_DATES", "employee_skill", r["employee_skill_id"], "verified before hire")
        ok("SKILL_STATUS")
        expected_status = G.derive_status(r["expiry_date"])
        if expected_status != r["status"]:
            flag("SKILL_STATUS_DERIVED", "employee_skill", r["employee_skill_id"], "status != derive(expiry)")
        if r["status_recorded"] != expected_status:
            flag("SKILL_STATUS_MISMATCH", "employee_skill", r["employee_skill_id"],
                 f"recorded={r['status_recorded']} derived={expected_status}")
        ok("PROFICIENCY_RANGE")
        if not 1 <= r["proficiency_level"] <= 5:
            flag("PROFICIENCY_RANGE", "employee_skill", r["employee_skill_id"], "out of 1..5")

    # 6. Training ----------------------------------------------------------
    for t in T["training"]:
        ok("TRAINING", 3)
        e = emp.get(t["employee_id"])
        if e is None:
            continue
        if not (e["hire_date"] <= t["training_date"] <= G.SNAPSHOT):
            flag("TRAINING", "training", t["training_id"], "training_date outside hire..snapshot")
        if (t["proficiency_after"] is not None) != (t["training_status"] == "COMPLETED"):
            flag("TRAINING", "training", t["training_id"], "proficiency_after iff COMPLETED violated")
        if e["leaver_date"] and t["training_date"] > e["leaver_date"]:
            flag("TRAINING", "training", t["training_id"], "training after leaver_date")

    # 7. Projects and demand ----------------------------------------------
    for p in T["project"]:
        ok("PROJECT", 4)
        if p["start_week"] > p["end_week"] or p["duration_weeks"] != p["end_week"] - p["start_week"] + 1:
            flag("PROJECT", "project", p["project_id"], "window or duration invalid")
        if (p["status"] == "CANCELLED") != (p["cancelled_week"] is not None):
            flag("PROJECT", "project", p["project_id"], "cancelled_week iff CANCELLED violated")
        if p["cancelled_week"] and not (p["start_week"] <= p["cancelled_week"] <= p["end_week"]):
            flag("PROJECT", "project", p["project_id"], "cancelled_week outside window")
        if p["status"] != "CANCELLED":
            want = ("COMPLETED" if p["end_week"] < G.SNAP_WEEK
                    else "PLANNED" if p["start_week"] > G.SNAP_WEEK else "ACTIVE")
            if want != p["status"]:
                flag("PROJECT", "project", p["project_id"], f"status {p['status']} != derived {want}")

    for l in T["demand_line"]:
        ok("DEMAND_LINE", 3)
        if l["skill_id"] not in skill or l["role_id"] not in role or l["project_id"] not in proj:
            continue
        if l["job_family"] != skill[l["skill_id"]]["job_family"] or l["job_family"] != role[l["role_id"]]["job_family"]:
            flag("DEMAND_LINE", "demand_line", l["demand_line_id"], "family mismatch across role/skill")
        if l["required_fte"] <= 0 or abs(l["required_fte"] * 2 - round(l["required_fte"] * 2)) > TOL:
            flag("DEMAND_LINE", "demand_line", l["demand_line_id"], "required_fte not positive multiple of 0.5")
        pj = proj[l["project_id"]]
        want_end = pj["cancelled_week"] - 1 if pj["cancelled_week"] else pj["end_week"]
        if l["start_week"] != pj["start_week"] or l["demand_end_week"] != want_end:
            flag("DEMAND_LINE", "demand_line", l["demand_line_id"], "window differs from project lifecycle")

    # Bridge rows: window check, duplicates, parity with line
    seen_pairs = {}
    raw_total, raw_lines, distinct_total = defaultdict(float), defaultdict(int), defaultdict(float)
    for b in sorted(T["demand_plan_line"], key=lambda x: x["demand_plan_line_id"]):
        ok("DEMAND_BRIDGE", 3)
        if b["demand_line_id"] not in line or b["demand_id"] not in plan:
            continue
        ln, pl = line[b["demand_line_id"]], plan[b["demand_id"]]
        raw_total[b["demand_id"]] += b["required_fte_applied"]
        raw_lines[b["demand_id"]] += 1
        if not (ln["start_week"] <= pl["iso_week"] <= ln["demand_end_week"]):
            flag("DEMAND_OUTSIDE_LINE_WINDOW", "demand_plan_line", b["demand_plan_line_id"], "week outside line window")
        if pl["project_id"] != ln["project_id"]:
            flag("DEMAND_BRIDGE", "demand_plan_line", b["demand_plan_line_id"], "plan and line projects differ")
        if abs(b["required_fte_applied"] - ln["required_fte"]) > TOL:
            flag("DEMAND_BRIDGE", "demand_plan_line", b["demand_plan_line_id"], "applied fte != line fte")
        key = (b["demand_id"], b["demand_line_id"])
        ok("DEMAND_DUPLICATE_LINE")
        if key in seen_pairs:
            flag("DEMAND_DUPLICATE_LINE", "demand_plan_line", b["demand_plan_line_id"],
                 f"duplicate of {seen_pairs[key]} in same plan week")
        else:
            seen_pairs[key] = b["demand_plan_line_id"]
            distinct_total[b["demand_id"]] += b["required_fte_applied"]

    for pl in T["demand_plan"]:
        ok("PLAN_ARITHMETIC", 3)
        if pl["week_start_date"] != G.monday(pl["iso_week"]) or pl["iso_year"] != G.YEAR:
            flag("PLAN_ARITHMETIC", "demand_plan", pl["demand_id"], "week_start or year wrong")
        if pl["line_count"] != raw_lines[pl["demand_id"]]:
            flag("PLAN_ARITHMETIC", "demand_plan", pl["demand_id"], "line_count != raw bridge rows")
        if abs(pl["required_fte_total"] - raw_total[pl["demand_id"]]) > TOL:
            flag("PLAN_ARITHMETIC", "demand_plan", pl["demand_id"], "total != sum of raw bridge rows")
        ok("PLAN_TOTAL_MISMATCH")
        if abs(pl["required_fte_total"] - distinct_total[pl["demand_id"]]) > TOL:
            flag("PLAN_TOTAL_MISMATCH", "demand_plan", pl["demand_id"],
                 f"stored {pl['required_fte_total']} vs distinct {distinct_total[pl['demand_id']]}")

    # 8. Allocations: lifecycle, rules, and concurrency ----------------------
    for a in alloc_all:
        if a["demand_line_id"] not in line or a["employee_id"] not in emp:
            continue
        ln = line[a["demand_line_id"]]
        ok("ALLOC_RULES", 8)
        if (a["start_week"], a["end_week"]) != (ln["start_week"], ln["demand_end_week"]):
            flag("ALLOC_SPAN_MISMATCH", "allocation", a["allocation_id"], "span differs from demand line")
        if a["project_id"] != ln["project_id"]:
            flag("ALLOC_RULES", "allocation", a["allocation_id"], "project differs from demand line")
        if a["start_date"] != G.monday(a["start_week"]) or a["end_date"] != G.friday(a["end_week"]):
            flag("ALLOC_RULES", "allocation", a["allocation_id"], "dates not Monday/Friday of span")
        want = ("ENDED" if a["end_date"] < G.SNAPSHOT
                else "PLANNED" if a["start_date"] > G.SNAPSHOT else "ACTIVE")
        if want != a["status"]:
            flag("ALLOC_STATUS", "allocation", a["allocation_id"], f"status {a['status']} != derived {want}")
        if abs(a["allocated_fte"] * 2 - round(a["allocated_fte"] * 2)) > TOL or not 0.5 <= a["allocated_fte"] <= 1.0:
            flag("ALLOC_RULES", "allocation", a["allocation_id"], "allocated_fte not 0.5 step in [0.5, 1.0]")
        e = emp[a["employee_id"]]
        if e["job_family"] != ln["job_family"]:
            flag("ALLOC_FAMILY_MISMATCH", "allocation", a["allocation_id"], "employee family differs from line")
        r = es_key.get((a["employee_id"], ln["skill_id"]))
        span = range(a["start_week"], a["end_week"] + 1)
        if r is None or r["proficiency_level"] < ln["min_proficiency"]:
            flag("ALLOC_BELOW_MIN_PROFICIENCY", "allocation", a["allocation_id"], "no qualifying skill row")
        elif any(not (r["expiry_date"] is None or r["expiry_date"] >= G.monday(w)) for w in span):
            flag("ALLOC_SKILL_EXPIRED", "allocation", a["allocation_id"], "skill expired during window")
        if any(not G.alive(e_dict_for_lw(e), w) for w in span):
            flag("ALLOC_AFTER_LEAVER", "allocation", a["allocation_id"], "allocated after leaver week")

    dup_pairs = defaultdict(list)
    for a in alloc_all:
        dup_pairs[(a["employee_id"], a["demand_line_id"])].append(a["allocation_id"])
    for k, ids in dup_pairs.items():
        ok("ALLOC_DUPLICATE_PAIR")
        if len(ids) > 1:
            for rid in ids[1:]:
                flag("ALLOC_DUPLICATE_PAIR", "allocation", rid, f"same employee and line as {ids[0]}")

    # 9. Availability (leave) and recomputed capacity ------------------------
    loss = [[0] * (W + 1) for _ in range(N)]
    booked = defaultdict(list)
    for av in T["availability"]:
        ok("AVAILABILITY", 3)
        if av["employee_id"] not in emp_idx:
            continue
        if av["start_date"] > av["end_date"] or av["start_date"].year != G.YEAR:
            flag("AVAILABILITY", "availability", av["availability_id"], "date order or year invalid")
        days = [av["start_date"] + timedelta(d) for d in range((av["end_date"] - av["start_date"]).days + 1)]
        wd = [d for d in days if d.weekday() < 5]
        if av["working_days_lost"] != len(wd) or av["counts_toward_capacity"] != (av["approval_status"] == "APPROVED"):
            flag("AVAILABILITY", "availability", av["availability_id"], "derived fields wrong")
        for s, e_ in booked[av["employee_id"]]:
            if not (av["end_date"] < s or av["start_date"] > e_):
                flag("LEAVE_OVERLAP", "availability", av["availability_id"], "overlaps another leave record")
        booked[av["employee_id"]].append((av["start_date"], av["end_date"]))
        if av["approval_status"] == "APPROVED":
            i = emp_idx[av["employee_id"]]
            for d in wd:
                w = G.iso_week(d)
                if w:
                    loss[i][w] += 1

    leaver_week = [G.iso_week(e["leaver_date"]) if e["leaver_date"] else None for e in T["employee"]]
    EFF = [[0.0] * (W + 1) for _ in range(N)]
    for i, e in enumerate(T["employee"]):
        for w in range(1, W + 1):
            if leaver_week[i] is None or w <= leaver_week[i]:
                EFF[i][w] = e["fte"] * (1 - min(5, loss[i][w]) / 5)

    # Concurrency: cumulative committed FTE per employee-week, in allocation id order.
    cum = defaultdict(float)      # (emp_idx, week) -> committed fte so far
    flagged_av, flagged_ov = set(), set()
    for a in sorted(alloc_all, key=lambda x: x["allocation_id"]):
        if a["employee_id"] not in emp_idx:
            continue
        i = emp_idx[a["employee_id"]]
        fte = T["employee"][i]["fte"]
        for w in range(a["start_week"], a["end_week"] + 1):
            cum[(i, w)] += a["allocated_fte"]
            ok("ALLOC_CAPACITY")
            if cum[(i, w)] > EFF[i][w] + TOL and a["allocation_id"] not in flagged_av:
                flagged_av.add(a["allocation_id"])
                flag("ALLOC_EXCEEDS_AVAILABILITY", "allocation", a["allocation_id"],
                     f"week {w}: committed {cum[(i, w)]:.4f} > effective {EFF[i][w]:.4f}")
            if cum[(i, w)] > fte + TOL and a["allocation_id"] not in flagged_ov:
                flagged_ov.add(a["allocation_id"])
                flag("ALLOC_OVERLAP_EXCEEDS_FTE", "allocation", a["allocation_id"],
                     f"week {w}: concurrent {cum[(i, w)]:.4f} > fte {fte}")

    # 10. Weekly capacity reconciliation -----------------------------------
    alloc_w = [0.0] * (W + 1)
    for a in alloc_all:
        for w in range(a["start_week"], a["end_week"] + 1):
            alloc_w[w] += a["allocated_fte"]
    wc_file = {r["iso_week"]: r for r in T["weekly_capacity"]}
    for w in range(1, W + 1):
        ok("CAPACITY_RECON", 4)
        act = [i for i in range(N) if leaver_week[i] is None or w <= leaver_week[i]]
        nominal = float(len(act))
        pt = sum(1 - T["employee"][i]["fte"] for i in act)
        leave = sum(T["employee"][i]["fte"] * min(5, loss[i][w]) / 5 for i in act)
        eff = sum(EFF[i][w] for i in act)
        row = wc_file[w]
        if abs(nominal - pt - leave - eff) > TOL:
            flag("CAPACITY_IDENTITY", "weekly_capacity", w, "nominal - part_time - leave != effective")
        if abs(row["effective_capacity_fte"] - eff) > TOL or abs(row["allocated_fte"] - alloc_w[w]) > TOL:
            flag("CAPACITY_RECON", "weekly_capacity", w, "file totals differ from recomputed totals")
        util = alloc_w[w] / eff if eff > 0 else 0.0
        if abs(row["utilization"] - util) > TOL or row["risk_level"] != G.risk_util(util):
            flag("CAPACITY_RECON", "weekly_capacity", w, "utilization or risk differs")

    # 11. Gap analysis: traceability, arithmetic, decision rule -------------
    alloc_by_line = defaultdict(list)
    for a in alloc_all:
        alloc_by_line[a["demand_line_id"]].append(a)
    expected_rows = sum(l["demand_end_week"] - l["start_week"] + 1 for l in T["demand_line"])
    ok("GAP_COVERAGE")
    if len(T["gap_analysis"]) != expected_rows:
        flag("GAP_COVERAGE", "gap_analysis", "*", f"rows {len(T['gap_analysis'])} != line-weeks {expected_rows}")
    for g in T["gap_analysis"]:
        ok("GAP_TRACE", 5)
        if g["demand_line_id"] not in line:
            flag("GAP_TRACE", "gap_analysis", g["gap_id"], "demand_line_id not found")
            continue
        ln = line[g["demand_line_id"]]
        w = g["iso_week"]
        if g["project_id"] != ln["project_id"] or not (ln["start_week"] <= w <= ln["demand_end_week"]):
            flag("GAP_TRACE", "gap_analysis", g["gap_id"], "project or week not on demand line")
        if g["demand_id"] != f"DEM-{ln['project_id']}-{G.YEAR}W{w:02d}" or g["demand_id"] not in plan:
            flag("GAP_TRACE", "gap_analysis", g["gap_id"], "demand_id does not match line week")
        sup = sum(a["allocated_fte"] for a in alloc_by_line[g["demand_line_id"]]
                  if a["start_week"] <= w <= a["end_week"])
        gap = max(0.0, ln["required_fte"] - sup)
        if abs(g["required_fte"] - ln["required_fte"]) > TOL or abs(g["supplied_fte"] - sup) > TOL \
                or abs(g["gap_fte"] - gap) > TOL:
            flag("GAP_ARITHMETIC", "gap_analysis", g["gap_id"], "required/supplied/gap do not reconcile")
        if abs(g["coverage_ratio"] - sup / ln["required_fte"]) > TOL:
            flag("GAP_ARITHMETIC", "gap_analysis", g["gap_id"], "coverage ratio wrong")
        if g["risk_level"] != G.risk_gap(gap, ln["required_fte"]):
            flag("GAP_RISK", "gap_analysis", g["gap_id"], "risk level differs from rule")
        ok("GAP_DECISION", 2)
        if g["recommended_action"] != G.decide(gap, g["reallocatable_fte"], g["trainable_candidates"]):
            flag("GAP_DECISION", "gap_analysis", g["gap_id"], "action differs from decision rule")
        if g["recommended_action"] == "NONE" and gap > TOL:
            flag("GAP_DECISION", "gap_analysis", g["gap_id"], "NONE with open gap")
        refs = g["assumption_refs"].split(";")
        ok("GAP_ASSUMPTIONS")
        if not refs or any(r not in {a for a, _ in G.ASSUMPTIONS} for r in refs):
            flag("GAP_ASSUMPTIONS", "gap_analysis", g["gap_id"], "cites unknown assumption")

    # 12. Ground-truth matching ------------------------------------------------
    labelled_ids = set()
    for g in gt:
        labelled_ids.add(g["row_key"])
        labelled_ids.update(g["related_keys"])
    detected = {(v["check"], v["row_id"]) for v in V}
    unlabelled = [v for v in V if v["row_id"] not in labelled_ids]
    missed = [g for g in gt if g["primary"] and (g["check_name"], g["row_key"]) not in detected]
    labelled = [v for v in V if v["row_id"] in labelled_ids]
    by_issue = defaultdict(int)
    for v in labelled:
        by_issue[v["check"]] += 1
    return {"T": T, "violations": V, "checks_run": dict(checks_run), "unlabelled": unlabelled,
            "missed": missed, "labelled_count": len(labelled), "labelled_by_check": dict(by_issue),
            "gt": gt, "loss": loss, "EFF": EFF, "alloc_w": alloc_w}


def e_dict_for_lw(e):
    """Adapter so generate.alive() can be reused with a leaver_week derived from the row."""
    return {"_lw": G.iso_week(e["leaver_date"]) if e["leaver_date"] else None}


def profile(res):
    T = res["T"]
    wc = T["weekly_capacity"]
    util = [r["utilization"] for r in wc]
    gap = T["gap_analysis"]
    lines_with_gap = {g["demand_line_id"] for g in gap if g["gap_fte"] > 0}
    fam = defaultdict(int)
    for e in T["employee"]:
        fam[e["job_family"]] += 1
    leave_days = defaultdict(int)
    for a in T["availability"]:
        if a["approval_status"] == "APPROVED":
            leave_days[a["employee_id"]] += a["working_days_lost"]
    conc = defaultdict(int)
    emp_weeks = defaultdict(lambda: defaultdict(float))
    for a in T["allocation"]:
        for w in range(a["start_week"], a["end_week"] + 1):
            emp_weeks[a["employee_id"]][w] += 1
    for eid, wk in emp_weeks.items():
        if any(v >= 2 for v in wk.values()):
            conc[eid] = 1
    return {
        "headcount": len(T["employee"]),
        "headcount_by_family": dict(sorted(fam.items())),
        "fte_distribution": dict(sorted(defaultdict(int, {k: sum(1 for e in T["employee"] if e["fte"] == k)
                                                          for k in {e["fte"] for e in T["employee"]}}).items())),
        "employees_with_approved_leave_pct": round(100 * len(leave_days) / len(T["employee"]), 1),
        "mean_approved_leave_working_days": round(sum(leave_days.values()) / len(T["employee"]), 2),
        "skill_status_at_snapshot": dict(sorted(defaultdict(int, {s: sum(1 for r in T["employee_skill"] if r["status"] == s)
                                                                   for s in {r["status"] for r in T["employee_skill"]}}).items())),
        "demand_lines": len(T["demand_line"]),
        "lines_with_any_gap_pct": round(100 * len(lines_with_gap) / max(1, len(T["demand_line"])), 1),
        "gap_action_counts": dict(sorted(defaultdict(int, {a: sum(1 for g in gap if g["recommended_action"] == a)
                                                             for a in {g["recommended_action"] for g in gap}}).items())),
        "total_gap_fte_weeks": round(sum(g["gap_fte"] for g in gap), 2),
        "utilization_mean": round(sum(util) / len(util), 4),
        "utilization_peak": max(util),
        "utilization_peak_week": max(wc, key=lambda r: r["utilization"])["iso_week"],
        "weeks_with_high_risk": sum(1 for r in wc if r["risk_level"] == "HIGH"),
        "employees_with_concurrent_allocations": sum(conc.values()),
    }


def write_reports(data, res, prof):
    checks = res["checks_run"]
    rows = [{"check": c, "rows_checked": checks[c],
             "violations": sum(1 for v in res["violations"] if v["check"] == c),
             "labelled": sum(1 for v in res["violations"] if v["check"] == c and v["row_id"] in
                             {g["row_key"] for g in res["gt"]} | {r for g in res["gt"] for r in g["related_keys"]})}
            for c in sorted(checks)]
    passed = not res["unlabelled"] and not res["missed"]
    summary = {"status": "PASS" if passed else "FAIL", "unlabelled_violations": len(res["unlabelled"]),
               "labelled_violations": res["labelled_count"], "missed_anomalies": len(res["missed"]),
               "injected_anomalies": sum(1 for g in res["gt"] if g["primary"]),
               "checks": rows, "profile": prof}
    out = os.path.join(data, "docs")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "validation_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True, default=str)
    md = ["# Validation summary", "", f"**Status: {summary['status']}**", "",
          f"- Unlabelled violations: {summary['unlabelled_violations']} (must be 0)",
          f"- Labelled exceptions detected: {summary['labelled_violations']}",
          f"- Injected primary anomalies: {summary['injected_anomalies']}, missed: {summary['missed_anomalies']} (must be 0)",
          "", "## Checks", "", "| Check | Rows checked | Violations | Labelled |", "|---|---:|---:|---:|"]
    md += [f"| {r['check']} | {r['rows_checked']} | {r['violations']} | {r['labelled']} |" for r in rows]
    md += ["", "## Injected anomaly coverage", "", "| Issue type | Check | Injected | Detected |", "|---|---|---:|---:|"]
    by_issue = defaultdict(lambda: [0, 0])
    for g in res["gt"]:
        if g["primary"]:
            by_issue[(g["issue_type"], g["check_name"])][0] += 1
            if (g["check_name"], g["row_key"]) not in {(v["check"], v["row_id"]) for v in res["violations"]}:
                continue
            by_issue[(g["issue_type"], g["check_name"])][1] += 1
    md += [f"| {k[0]} | {k[1]} | {v[0]} | {v[1]} |" for k, v in sorted(by_issue.items())]
    md += ["", "## Data profile", "", "```json", json.dumps(prof, indent=2, sort_keys=True, default=str), "```", ""]
    with open(os.path.join(out, "validation_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    a = ap.parse_args()
    res = validate(a.data)
    prof = profile(res)
    s = write_reports(a.data, res, prof)
    print(f"status={s['status']} unlabelled={s['unlabelled_violations']} "
          f"labelled={s['labelled_violations']} missed={s['missed_anomalies']} "
          f"injected={s['injected_anomalies']}")
    for c in res["unlabelled"][:20]:
        print("  UNLABELLED:", c)
    for m in res["missed"][:20]:
        print("  MISSED:", m["anomaly_id"], m["check_name"], m["row_key"])
    sys.exit(0 if s["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
