#!/usr/bin/env python3
"""
generate.py - deterministic synthetic workforce dataset generator.

Builds master, transaction, derived-analysis and ground-truth tables for the
"forecast demand by role/project, find skill gaps, utilisation risk and
hiring/training options" workflow. Same seed + same parameters => identical bytes.

Examples
  python3 generate.py --preset full    --out out/full_run1
  python3 generate.py --preset starter --out out/starter
  python3 generate.py --preset full --employees 800 --projects 120 --out out/custom
"""
import argparse, calendar, csv, hashlib, json, math, os, random
from collections import defaultdict
from datetime import date, timedelta

GENERATOR_VERSION = "1.0.0"
YEAR = 2026
WEEKS = 52                      # 12-month horizon = ISO weeks 1..52 of 2026
SNAPSHOT = date(2026, 10, 28)   # as-of date used to derive statuses
SNAP_WEEK = SNAPSHOT.isocalendar()[1]

PRESETS = {
    "full": dict(employees=5000, skills=300, projects=500, training=2500,
                 anom_over=25, anom_overlap=25, anom_stale=40, anom_dup=30, seed=20261022),
    "starter": dict(employees=200, skills=60, projects=40, training=100,
                    anom_over=3, anom_overlap=3, anom_stale=4, anom_dup=3, seed=20261022),
}

# ----------------------------------------------------------------------------
# Reference vocabularies (single source of truth for generator AND validator)
# ----------------------------------------------------------------------------
FAMILIES = ["Data", "Backend", "Frontend", "Cloud", "Security", "QA",
            "Product", "Design", "Analytics", "DevOps", "Mobile", "ML"]
TITLES = ["ENGINEER", "SPECIALIST"]
LEVELS = ["JUNIOR", "MID", "SENIOR", "LEAD"]
LEVEL_W = [0.40, 0.35, 0.20, 0.05]
LEVEL_PROF = {"JUNIOR": (1, 3), "MID": (2, 4), "SENIOR": (3, 5), "LEAD": (3, 5)}
FTE_W = [(1.0, 0.82), (0.8, 0.10), (0.6, 0.05), (0.5, 0.03)]
EXPIRY_W = [(12, 0.20), (24, 0.30), (36, 0.30), (None, 0.20)]
LOCATIONS = ["BLR", "LON", "NYC", "BER", "REMOTE"]
LEAVE_W = [("ANNUAL_LEAVE", 0.6), ("SICK_LEAVE", 0.3), ("PARENTAL_LEAVE", 0.1)]
PRIORITY_W = [("P1", 0.2), ("P2", 0.5), ("P3", 0.3)]
LINES_W = [(1, 0.2), (2, 0.4), (3, 0.3), (4, 0.1)]
REQ_FTE_W = [(4.0, 0.20), (8.0, 0.25), (12.0, 0.25), (16.0, 0.15), (24.0, 0.10), (32.0, 0.05)]
MIN_PROF_W = [(2, 0.2), (3, 0.4), (4, 0.3), (5, 0.1)]
FIRST = ["Aanya", "Rahul", "Maya", "Liam", "Chen", "Sofia", "Omar", "Priya", "Lucas", "Noor",
         "Ravi", "Elena", "Kenji", "Amara", "Diego", "Ines", "Yusuf", "Hana", "Mateo", "Zara",
         "Arjun", "Clara", "Tomas", "Leila"]
LAST = ["Sharma", "Nguyen", "Garcia", "Okafor", "Tanaka", "Schmidt", "Kumar", "Rossi", "Haddad",
        "Lee", "Brown", "Novak", "Silva", "Khan", "Muller", "Ito", "Patel", "Moreau", "Costa",
        "Reyes", "Iyer", "Fischer", "Park", "Adeyemi"]
PROF_SCALE = [(1, "NOVICE", "Needs supervision; knows basic terms"),
              (2, "ADVANCED_BEGINNER", "Completes routine tasks with guidance"),
              (3, "COMPETENT", "Works independently on standard tasks"),
              (4, "PROFICIENT", "Handles complex tasks and mentors others"),
              (5, "EXPERT", "Sets standards; go-to person for the skill")]
ASSUMPTIONS = [
    ("ASM-001", "Demand for a demand line is constant across its window (start_week..demand_end_week). DemandPlan totals are sums of DemandPlanLine rows."),
    ("ASM-002", "A skill verification is valid until expiry_date = last_verified_date + skill.expiry_months. NO_EXPIRY skills never lapse."),
    ("ASM-003", "Skill and allocation statuses are evaluated at the snapshot date 2026-10-28. Planning validity is checked on the Monday of each ISO week."),
    ("ASM-004", "Only APPROVED leave reduces capacity. Each approved weekday lost reduces that week's capacity by 1/5 of the employee's FTE. PENDING leave is ignored."),
    ("ASM-005", "Part-time capacity equals contracted FTE (0.5 to 1.0) in every week the employee is active."),
    ("ASM-006", "Allocations are committed per demand line in 0.5 FTE steps for the full line window. The planning view does not release committed time for leave."),
    ("ASM-007", "Reallocatable FTE = planned residual capacity of same-family employees holding a valid skill at min_proficiency. Pools are not netted across lines, so this is a per-line upper bound."),
    ("ASM-008", "A trainable candidate (proficiency = min_proficiency - 1, valid skill) becomes one full FTE at min_proficiency. Training lead time is not modelled."),
    ("ASM-009", "A hire fills one FTE at min_proficiency. Time-to-hire and onboarding are not modelled."),
    ("ASM-010", "Utilisation = allocated_fte / effective_capacity_fte. Weekly risk: HIGH >= 0.95, MEDIUM >= 0.80, else LOW. Gap risk: HIGH if gap >= 50% of requirement."),
    ("ASM-011", "A LEAVER has capacity up to and including the ISO week of leaver_date and zero afterwards (one-way lifecycle)."),
    ("ASM-012", "Records flagged in ground_truth are kept in input tables with their recorded values and are not corrected in place. Expected handling is stored only in ground_truth."),
]

ENUMS = {
    ("role", "title_level"): TITLES,
    ("role", "job_family"): FAMILIES,
    ("skill", "job_family"): FAMILIES,
    ("skill", "expiry_months"): [12, 24, 36, None],
    ("employee", "level"): LEVELS,
    ("employee", "fte"): [0.5, 0.6, 0.8, 1.0],
    ("employee", "location"): LOCATIONS,
    ("employee", "employment_status"): ["ACTIVE", "LEAVER"],
    ("employee_skill", "proficiency_level"): [1, 2, 3, 4, 5],
    ("employee_skill", "status"): ["NO_EXPIRY", "CURRENT", "EXPIRED"],
    ("employee_skill", "status_recorded"): ["NO_EXPIRY", "CURRENT", "EXPIRED"],
    ("project", "priority"): ["P1", "P2", "P3"],
    ("project", "status"): ["PLANNED", "ACTIVE", "COMPLETED", "CANCELLED"],
    ("demand_line", "min_proficiency"): [2, 3, 4, 5],
    ("allocation", "status"): ["PLANNED", "ACTIVE", "ENDED"],
    ("availability", "availability_type"): [t for t, _ in LEAVE_W],
    ("availability", "approval_status"): ["APPROVED", "PENDING"],
    ("training", "training_status"): ["COMPLETED", "IN_PROGRESS", "FAILED"],
    ("gap_analysis", "risk_level"): ["HIGH", "MEDIUM", "LOW"],
    ("gap_analysis", "recommended_action"): ["NONE", "REALLOCATE", "TRAIN", "HIRE"],
    ("weekly_capacity", "risk_level"): ["HIGH", "MEDIUM", "LOW"],
}

# ----------------------------------------------------------------------------
# Schema: column = (name, type, required, rule).  pk = primary key columns.
# ----------------------------------------------------------------------------
SCHEMA = {
    "role": {"entity": "Role", "pk": ["role_id"],
             "desc": "Job role. Each role belongs to exactly one job family (1:N family to role).",
             "columns": [
                 ("role_id", "string", True, "PK. Format ROL-000001. Immutable."),
                 ("role_name", "string", True, "Unique title, e.g. 'Data Engineer'."),
                 ("job_family", "enum", True, "Job family of the role."),
                 ("title_level", "enum", True, "ENGINEER or SPECIALIST.")]},
    "skill": {"entity": "Skill", "pk": ["skill_id"],
              "desc": "Capability that is verified, trained and required by demand lines. Belongs to one job family.",
              "columns": [
                  ("skill_id", "string", True, "PK. Format SKL-000001. Immutable."),
                  ("skill_name", "string", True, "Unique within job family."),
                  ("job_family", "enum", True, "Job family of the skill."),
                  ("expiry_months", "int", False, "Months a verification stays valid. Null = never expires (ASM-002). Units: months.")]},
    "proficiency_scale": {"entity": "ProficiencyScale", "pk": ["level"],
                          "desc": "Reference scale 1 to 5 used by EmployeeSkill and DemandLine.",
                          "columns": [
                              ("level", "int", True, "PK. 1 to 5."),
                              ("label", "string", True, "NOVICE, ADVANCED_BEGINNER, COMPETENT, PROFICIENT or EXPERT."),
                              ("description", "string", True, "Behavioural anchor for the level.")]},
    "employee": {"entity": "Employee", "pk": ["employee_id"],
                 "desc": "Person in the workforce. A manager is another employee at level LEAD. Lifecycle: ACTIVE -> LEAVER (one-way).",
                 "columns": [
                     ("employee_id", "string", True, "PK. Format EMP-000001. Immutable."),
                     ("full_name", "string", True, "Synthetic name. Not unique."),
                     ("role_id", "string", True, "FK role.role_id."),
                     ("job_family", "enum", True, "Must equal the job family of role_id (denormalised, validated)."),
                     ("level", "enum", True, "JUNIOR, MID, SENIOR or LEAD."),
                     ("manager_id", "string", False, "FK employee.employee_id, must reference a LEAD. Null for LEAD rows, or when no lead exists."),
                     ("hire_date", "date", True, "ISO date from 2012-01-01 to 2025-12-31."),
                     ("fte", "float", True, "Contracted FTE fraction. Units: FTE."),
                     ("location", "enum", True, "Office or REMOTE code."),
                     ("employment_status", "enum", True, "ACTIVE or LEAVER."),
                     ("leaver_date", "date", False, "Required iff LEAVER. Friday of an ISO week 8 to 44 of 2026. After hire_date.")]},
    "employee_skill": {"entity": "EmployeeSkill", "pk": ["employee_skill_id"],
                       "desc": "Bridge (N:M) between Employee and Skill with proficiency and verification history. Alternate key (employee_id, skill_id) is unique.",
                       "columns": [
                           ("employee_skill_id", "string", True, "PK. Format ESK-000001."),
                           ("employee_id", "string", True, "FK employee.employee_id."),
                           ("skill_id", "string", True, "FK skill.skill_id."),
                           ("proficiency_level", "int", True, "Derived: max(base level, proficiency_after of completed training). Scale 1 to 5."),
                           ("base_verified_date", "date", True, "Input verification date, between max(hire_date, 2023-01-01) and 2026-06-30."),
                           ("last_verified_date", "date", True, "Derived: max(base_verified_date, completed training dates)."),
                           ("expiry_date", "date", False, "Derived: last_verified_date + skill.expiry_months. Null if NO_EXPIRY."),
                           ("status", "enum", True, "Derived at snapshot 2026-10-28: NO_EXPIRY, CURRENT or EXPIRED."),
                           ("status_recorded", "enum", True, "Status as stored by the source system. Equals status except labelled anomalies.")]},
    "project": {"entity": "Project", "pk": ["project_id"],
                "desc": "Client or internal programme that creates demand over a window of ISO weeks.",
                "columns": [
                    ("project_id", "string", True, "PK. Format PRJ-2026-000001."),
                    ("project_name", "string", True, "Synthetic programme name."),
                    ("job_family", "enum", True, "Primary job family of the project."),
                    ("client_code", "string", True, "Format CLI-001 to CLI-080."),
                    ("priority", "enum", True, "P1, P2 or P3."),
                    ("start_week", "int", True, "ISO week 1 to 52 of 2026."),
                    ("end_week", "int", True, "ISO week 1 to 52. Must be >= start_week."),
                    ("duration_weeks", "int", True, "Derived: end_week - start_week + 1. Units: weeks."),
                    ("status", "enum", True, "Derived: PLANNED, ACTIVE, COMPLETED or CANCELLED (snapshot week 44)."),
                    ("cancelled_week", "int", False, "Required iff CANCELLED. Within [start_week, end_week]. No demand from this week on.")]},
    "demand_line": {"entity": "DemandLine", "pk": ["demand_line_id"],
                    "desc": "Requirement of one role and skill for one project. Each project has 1 to 4 lines. Child of Project.",
                    "columns": [
                        ("demand_line_id", "string", True, "PK. Format DLN-000001."),
                        ("project_id", "string", True, "FK project.project_id."),
                        ("line_no", "int", True, "1 to 4. Unique with project_id."),
                        ("role_id", "string", True, "FK role.role_id."),
                        ("skill_id", "string", True, "FK skill.skill_id."),
                        ("job_family", "enum", True, "Must equal the family of skill_id."),
                        ("min_proficiency", "int", True, "Minimum proficiency required. Scale 2 to 5."),
                        ("required_fte", "float", True, "Units: FTE. Positive, multiple of 0.5. Constant across the window (ASM-001)."),
                        ("start_week", "int", True, "ISO week. Equals project start_week."),
                        ("demand_end_week", "int", True, "Derived: project end_week, or cancelled_week - 1 when cancelled.")]},
    "demand_plan": {"entity": "DemandPlan", "pk": ["demand_id"],
                    "desc": "Weekly demand snapshot per project. Format DEM-<project-id>-2026W01. Aggregates DemandPlanLine rows.",
                    "columns": [
                        ("demand_id", "string", True, "PK. Format DEM-PRJ-2026-000001-2026W01."),
                        ("project_id", "string", True, "FK project.project_id."),
                        ("iso_year", "int", True, "Always 2026."),
                        ("iso_week", "int", True, "ISO week 1 to 52."),
                        ("week_start_date", "date", True, "Derived: Monday of iso_week."),
                        ("line_count", "int", True, "Derived: number of DemandPlanLine rows (raw)."),
                        ("required_fte_total", "float", True, "Derived: sum of required_fte_applied over DemandPlanLine rows (raw). Units: FTE.")]},
    "demand_plan_line": {"entity": "DemandPlanLine", "pk": ["demand_plan_line_id"],
                         "desc": "Bridge (N:M) between DemandPlan and DemandLine. One row per plan-line-week. Duplicates are a labelled anomaly.",
                         "columns": [
                             ("demand_plan_line_id", "string", True, "PK. Format DPL-000001."),
                             ("demand_id", "string", True, "FK demand_plan.demand_id."),
                             ("demand_line_id", "string", True, "FK demand_line.demand_line_id."),
                             ("required_fte_applied", "float", True, "Copied from demand_line.required_fte. Units: FTE.")]},
    "allocation": {"entity": "Allocation", "pk": ["allocation_id"],
                   "desc": "Commitment of an employee to a demand line for a window of weeks. Lifecycle: PLANNED -> ACTIVE -> ENDED.",
                   "columns": [
                       ("allocation_id", "string", True, "PK. Format ALLOC-2026-000001."),
                       ("employee_id", "string", True, "FK employee.employee_id."),
                       ("project_id", "string", True, "FK project.project_id. Must match the demand line's project."),
                       ("demand_line_id", "string", True, "FK demand_line.demand_line_id."),
                       ("start_week", "int", True, "ISO week. Equals the line's start_week."),
                       ("end_week", "int", True, "ISO week. Equals the line's demand_end_week."),
                       ("start_date", "date", True, "Derived: Monday of start_week."),
                       ("end_date", "date", True, "Derived: Friday of end_week."),
                       ("allocated_fte", "float", True, "Units: FTE. Multiple of 0.5, between 0.5 and 1.0."),
                       ("status", "enum", True, "Derived at snapshot: PLANNED, ACTIVE or ENDED.")]},
    "availability": {"entity": "Availability", "pk": ["availability_id"],
                     "desc": "Leave event per employee (event history). Only APPROVED leave reduces capacity (ASM-004).",
                     "columns": [
                         ("availability_id", "string", True, "PK. Format AVL-000001."),
                         ("employee_id", "string", True, "FK employee.employee_id."),
                         ("availability_type", "enum", True, "ANNUAL_LEAVE, SICK_LEAVE or PARENTAL_LEAVE."),
                         ("start_date", "date", True, "Calendar date in 2026."),
                         ("end_date", "date", True, "Calendar date, >= start_date. Non-overlapping per employee."),
                         ("approval_status", "enum", True, "APPROVED or PENDING."),
                         ("working_days_lost", "int", True, "Derived: weekdays between start_date and end_date inclusive. Units: days."),
                         ("counts_toward_capacity", "bool", True, "Derived: true iff APPROVED.")]},
    "training": {"entity": "Training", "pk": ["training_id"],
                 "desc": "Training event on an existing employee skill. Completed training refreshes verification (ASM-002).",
                 "columns": [
                     ("training_id", "string", True, "PK. Format TRN-000001."),
                     ("employee_id", "string", True, "FK employee.employee_id."),
                     ("skill_id", "string", True, "FK skill.skill_id."),
                     ("training_date", "date", True, "Between 2026-01-05 and the snapshot date."),
                     ("training_status", "enum", True, "COMPLETED, IN_PROGRESS or FAILED."),
                     ("proficiency_after", "int", False, "Required iff COMPLETED. Scale 1 to 5.")]},
    "gap_analysis": {"entity": "GapAnalysis", "pk": ["gap_id"],
                     "desc": "Gap per demand line per ISO week, with a rule-based recommended action. Traceable to DemandLine and DemandPlan.",
                     "columns": [
                         ("gap_id", "string", True, "PK. Format GAP-000001."),
                         ("demand_id", "string", True, "FK demand_plan.demand_id for the same project and week."),
                         ("demand_line_id", "string", True, "FK demand_line.demand_line_id."),
                         ("project_id", "string", True, "FK project.project_id."),
                         ("iso_week", "int", True, "ISO week 1 to 52."),
                         ("required_fte", "float", True, "Derived from demand_line.required_fte. Units: FTE."),
                         ("supplied_fte", "float", True, "Derived: sum of allocated_fte on the line active in the week. Units: FTE."),
                         ("gap_fte", "float", True, "Derived: max(0, required_fte - supplied_fte). Units: FTE."),
                         ("coverage_ratio", "float", True, "Derived: supplied_fte / required_fte. May exceed 1 when over-allocated."),
                         ("risk_level", "enum", True, "HIGH, MEDIUM or LOW (ASM-010)."),
                         ("reallocatable_fte", "float", True, "Derived planning pool of matching residual capacity (ASM-007). Units: FTE."),
                         ("trainable_candidates", "int", True, "Count of employees one level below min_proficiency with valid skill (ASM-008)."),
                         ("recommended_action", "enum", True, "NONE, REALLOCATE, TRAIN or HIRE. Decision rule in generate.decide()."),
                         ("assumption_refs", "string", True, "Semicolon-separated ASM ids that justify the row.")]},
    "weekly_capacity": {"entity": "WeeklyCapacity", "pk": ["iso_week"],
                        "desc": "Derived capacity and utilisation per ISO week (one row per week). Reconciles nominal, part-time and leave losses to effective capacity.",
                        "columns": [
                            ("iso_week", "int", True, "PK. ISO week 1 to 52 of 2026."),
                            ("week_start_date", "date", True, "Derived: Monday of the week."),
                            ("headcount_active", "int", True, "Derived: employees with capacity in the week (not after leaver week)."),
                            ("nominal_fte", "float", True, "Derived: headcount_active. Units: FTE."),
                            ("part_time_loss_fte", "float", True, "Derived: sum of (1 - fte) over active employees. Units: FTE."),
                            ("leave_loss_fte", "float", True, "Derived: sum of fte * approved_leave_days / 5. Units: FTE."),
                            ("effective_capacity_fte", "float", True, "Derived: nominal - part_time_loss - leave_loss. Units: FTE."),
                            ("allocated_fte", "float", True, "Derived: sum of allocated_fte of allocations active in the week. Units: FTE."),
                            ("unallocated_fte", "float", True, "Derived: max(0, effective - allocated). Units: FTE."),
                            ("utilization", "float", True, "Derived: allocated / effective (ASM-010). Units: ratio."),
                            ("risk_level", "enum", True, "HIGH, MEDIUM or LOW (ASM-010).")]},
}

FKS = {
    "employee.role_id": "role.role_id",
    "employee.manager_id": "employee.employee_id",
    "employee_skill.employee_id": "employee.employee_id",
    "employee_skill.skill_id": "skill.skill_id",
    "project.job_family": None,
    "demand_line.project_id": "project.project_id",
    "demand_line.role_id": "role.role_id",
    "demand_line.skill_id": "skill.skill_id",
    "demand_plan.project_id": "project.project_id",
    "demand_plan_line.demand_id": "demand_plan.demand_id",
    "demand_plan_line.demand_line_id": "demand_line.demand_line_id",
    "allocation.employee_id": "employee.employee_id",
    "allocation.project_id": "project.project_id",
    "allocation.demand_line_id": "demand_line.demand_line_id",
    "availability.employee_id": "employee.employee_id",
    "training.employee_id": "employee.employee_id",
    "training.skill_id": "skill.skill_id",
    "gap_analysis.demand_id": "demand_plan.demand_id",
    "gap_analysis.demand_line_id": "demand_line.demand_line_id",
    "gap_analysis.project_id": "project.project_id",
}
FKS = {k: v for k, v in FKS.items() if v}

LAYOUT = {
    "role": "master/role.csv",
    "skill": "master/skill.csv",
    "proficiency_scale": "master/proficiency_scale.csv",
    "employee": "master/employee.csv",
    "employee_skill": "master/employee_skill.csv",
    "project": "master/project.csv",
    "demand_line": "transactions/demand_line.csv",
    "demand_plan": "transactions/demand_plan.csv",
    "demand_plan_line": "transactions/demand_plan_line.csv",
    "allocation": "transactions/allocation.csv",
    "availability": "transactions/availability.csv",
    "training": "transactions/training.csv",
    "gap_analysis": "analysis/gap_analysis.csv",
    "weekly_capacity": "analysis/weekly_capacity.csv",
}
SQL_ORDER = list(LAYOUT)

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def wchoice(rng, pairs):
    """Weighted choice from [(value, weight), ...] using a single rng draw."""
    r = rng.random() * sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if r < acc:
            return v
    return pairs[-1][0]


def add_months(d, m):
    idx = d.month - 1 + m
    y, mo = d.year + idx // 12, idx % 12 + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def monday(w):
    return date.fromisocalendar(YEAR, w, 1)


def friday(w):
    return date.fromisocalendar(YEAR, w, 5)


def iso_week(d):
    """ISO week of a 2026 date, or None if outside weeks 1..52 or outside 2026."""
    if d.year != YEAR:
        return None
    w = d.isocalendar()[1]
    return w if w <= WEEKS else None


def rnd_date(rng, a, b):
    return a + timedelta(days=rng.randint(0, (b - a).days))


def derive_status(exp):
    if exp is None:
        return "NO_EXPIRY"
    return "EXPIRED" if exp < SNAPSHOT else "CURRENT"


def valid_at(r, w):
    return r["expiry_date"] is None or r["expiry_date"] >= monday(w)


def alive(e, w):
    return e["_lw"] is None or w <= e["_lw"]


def decide(gap, realloc, trainable):
    """Recommendation rule (documented in README and ASM-007..009)."""
    if gap <= 1e-9:
        return "NONE"
    if realloc + 1e-9 >= gap:
        return "REALLOCATE"
    if trainable >= math.ceil(gap - 1e-9):
        return "TRAIN"
    return "HIRE"


def risk_gap(gap, req):
    if gap <= 1e-9:
        return "LOW"
    return "HIGH" if gap / req >= 0.5 else "MEDIUM"


def risk_util(u):
    return "HIGH" if u >= 0.95 else ("MEDIUM" if u >= 0.80 else "LOW")


def fmtv(v):
    """Deterministic text formatting for CSV cells."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    return str(v)


def gt_entry(issue, check, table, row_key, related, primary, decision, value, why):
    return {"issue_type": issue, "check_name": check, "table": table, "row_key": row_key,
            "related_keys": related, "primary": primary, "expected_decision": decision,
            "expected_value": value, "rationale": why}


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------
def build(p):
    rng = random.Random(p["seed"])
    if p["skills"] < len(FAMILIES):
        raise SystemExit("need at least one skill per job family (12)")

    # 1. Roles and skills (taxonomy) -----------------------------------------
    roles = []
    for fam in FAMILIES:
        for t in TITLES:
            roles.append({"role_id": f"ROL-{len(roles)+1:06d}", "role_name": f"{fam} {t.title()}",
                          "job_family": fam, "title_level": t})
    fam_roles = {f: [r for r in roles if r["job_family"] == f] for f in FAMILIES}

    skills = []
    for i in range(1, p["skills"] + 1):
        fam = FAMILIES[(i - 1) % len(FAMILIES)]
        skills.append({"skill_id": f"SKL-{i:06d}",
                       "skill_name": f"{fam} Practice {(i - 1) // len(FAMILIES) + 1:02d}",
                       "job_family": fam, "expiry_months": wchoice(rng, EXPIRY_W)})
    skill_by_id = {s["skill_id"]: s for s in skills}
    fam_skills = {f: [s for s in skills if s["job_family"] == f] for f in FAMILIES}

    # 2. Employees and managers ----------------------------------------------
    emps = []
    for i in range(1, p["employees"] + 1):
        role = roles[rng.randrange(len(roles))]
        leaver = rng.random() < 0.02
        emps.append({
            "employee_id": f"EMP-{i:06d}",
            "full_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "role_id": role["role_id"], "job_family": role["job_family"],
            "level": wchoice(rng, list(zip(LEVELS, LEVEL_W))),
            "manager_id": None,
            "hire_date": rnd_date(rng, date(2012, 1, 1), date(2025, 12, 31)),
            "fte": wchoice(rng, FTE_W),
            "location": rng.choice(LOCATIONS),
            "employment_status": "LEAVER" if leaver else "ACTIVE",
            "leaver_date": friday(rng.randint(8, 44)) if leaver else None,
        })
    leads = defaultdict(list)
    all_leads = []
    for e in emps:
        if e["level"] == "LEAD":
            leads[e["job_family"]].append(e["employee_id"])
            all_leads.append(e["employee_id"])
    for e in emps:
        if e["level"] != "LEAD":
            pool = leads[e["job_family"]] or all_leads
            e["manager_id"] = rng.choice(pool) if pool else None
        e["_lw"] = iso_week(e["leaver_date"]) if e["leaver_date"] else None
    emp_i = {e["employee_id"]: i for i, e in enumerate(emps)}
    N = len(emps)

    # 3. Employee skill profiles (base) --------------------------------------
    es = []
    for e in emps:
        fam = e["job_family"]
        k = rng.randint(9, 15)
        own = rng.sample(fam_skills[fam], min(len(fam_skills[fam]), max(1, round(k * 0.8))))
        others = [s for s in skills if s["job_family"] != fam]
        chosen = own + rng.sample(others, k - len(own))
        lo, hi = LEVEL_PROF[e["level"]]
        for s in chosen:
            base = rng.randint(lo, hi)
            verified = rnd_date(rng, max(e["hire_date"], date(2023, 1, 1)), date(2026, 6, 30))
            es.append({"employee_skill_id": f"ESK-{len(es)+1:06d}", "employee_id": e["employee_id"],
                       "skill_id": s["skill_id"], "_base": base, "proficiency_level": base,
                       "base_verified_date": verified})
    es_key = {(r["employee_id"], r["skill_id"]): r for r in es}

    # 4. Training events (history) --------------------------------------------
    pool = [r for r in es if emps[emp_i[r["employee_id"]]]["employment_status"] == "ACTIVE"
            and r["_base"] < 5]
    trainings = []
    for i in range(1, p["training"] + 1):
        r = rng.choice(pool)
        st = wchoice(rng, [("COMPLETED", 0.75), ("IN_PROGRESS", 0.10), ("FAILED", 0.15)])
        trainings.append({
            "training_id": f"TRN-{i:06d}", "employee_id": r["employee_id"], "skill_id": r["skill_id"],
            "training_date": rnd_date(rng, date(YEAR, 1, 5), SNAPSHOT), "training_status": st,
            "proficiency_after": min(5, r["_base"] + (1 if rng.random() < 0.5 else 0))
            if st == "COMPLETED" else None})

    # 5. Derive skill state from source records (not generated independently) -
    completed = defaultdict(list)
    for t in trainings:
        if t["training_status"] == "COMPLETED":
            completed[(t["employee_id"], t["skill_id"])].append(t)
    for r in es:
        ts = completed.get((r["employee_id"], r["skill_id"]), [])
        last = max([r["base_verified_date"]] + [t["training_date"] for t in ts])
        r["proficiency_level"] = max([r["_base"]] + [t["proficiency_after"] for t in ts])
        months = skill_by_id[r["skill_id"]]["expiry_months"]
        r["last_verified_date"] = last
        r["expiry_date"] = add_months(last, months) if months else None
        r["status"] = derive_status(r["expiry_date"])
        r["status_recorded"] = r["status"]

    # 6. Leave (availability events) and effective capacity -------------------
    loss = [[0] * (WEEKS + 1) for _ in range(N)]     # approved weekdays lost per week
    avail = []
    for i, e in enumerate(emps):
        booked = []
        for _ in range(wchoice(rng, [(0, 0.15), (1, 0.40), (2, 0.30), (3, 0.15)])):
            typ = wchoice(rng, LEAVE_W)
            length = {"ANNUAL_LEAVE": rng.randint(5, 14), "SICK_LEAVE": rng.randint(1, 3),
                      "PARENTAL_LEAVE": rng.randint(30, 60)}[typ]
            start = rnd_date(rng, date(YEAR, 1, 5), date(YEAR, 12, 31) - timedelta(days=length))
            end = start + timedelta(days=length - 1)
            if any(not (end < a or start > b) for a, b in booked):
                continue
            booked.append((start, end))
            status = wchoice(rng, [("APPROVED", 0.9), ("PENDING", 0.1)])
            weekdays = [start + timedelta(days=k) for k in range(length)]
            weekdays = [d for d in weekdays if d.weekday() < 5]
            if status == "APPROVED":
                for d in weekdays:
                    w = iso_week(d)
                    if w:
                        loss[i][w] += 1
            avail.append({"availability_id": f"AVL-{len(avail)+1:06d}", "employee_id": e["employee_id"],
                          "availability_type": typ, "start_date": start, "end_date": end,
                          "approval_status": status, "working_days_lost": len(weekdays),
                          "counts_toward_capacity": status == "APPROVED"})
    EFF = [[0.0] * (WEEKS + 1) for _ in range(N)]
    for i, e in enumerate(emps):
        for w in range(1, WEEKS + 1):
            if alive(e, w):
                EFF[i][w] = e["fte"] * (1 - min(5, loss[i][w]) / 5)

    # 7. Projects and demand lines ---------------------------------------------
    projects, lines = [], []
    for i in range(1, p["projects"] + 1):
        pid = f"PRJ-{YEAR}-{i:06d}"
        fam = rng.choice(FAMILIES)
        start = rng.randint(1, WEEKS)
        end = min(WEEKS, start + rng.randint(4, 52) - 1)
        cancelled = rng.random() < 0.04
        cancel_w = rng.randint(start, end) if cancelled else None
        dem_end = cancel_w - 1 if cancelled else end
        if cancelled:
            status = "CANCELLED"
        elif end < SNAP_WEEK:
            status = "COMPLETED"
        elif start > SNAP_WEEK:
            status = "PLANNED"
        else:
            status = "ACTIVE"
        projects.append({"project_id": pid, "project_name": f"{fam} Programme {i:03d}",
                         "job_family": fam, "client_code": f"CLI-{rng.randint(1, 80):03d}",
                         "priority": wchoice(rng, PRIORITY_W), "start_week": start, "end_week": end,
                         "duration_weeks": end - start + 1, "status": status,
                         "cancelled_week": cancel_w})
        if dem_end < start:
            continue
        for ln_no in range(1, wchoice(rng, LINES_W) + 1):
            role = rng.choice(fam_roles[fam])
            skill = rng.choice(fam_skills[fam])
            lines.append({"demand_line_id": f"DLN-{len(lines)+1:06d}", "project_id": pid,
                          "line_no": ln_no, "role_id": role["role_id"], "skill_id": skill["skill_id"],
                          "job_family": fam, "min_proficiency": wchoice(rng, MIN_PROF_W),
                          "required_fte": wchoice(rng, REQ_FTE_W), "start_week": start,
                          "demand_end_week": dem_end})

    # 8. Demand plans (weekly) and bridge rows ---------------------------------
    bridge, plan_keys = [], {}
    for ln in lines:
        for w in range(ln["start_week"], ln["demand_end_week"] + 1):
            did = f"DEM-{ln['project_id']}-{YEAR}W{w:02d}"
            plan_keys[did] = (ln["project_id"], w)
            bridge.append({"demand_plan_line_id": f"DPL-{len(bridge)+1:06d}", "demand_id": did,
                           "demand_line_id": ln["demand_line_id"],
                           "required_fte_applied": ln["required_fte"]})

    # 9. Greedy, capacity-aware allocation -----------------------------------
    holders = defaultdict(list)
    for r in es:
        holders[r["skill_id"]].append(r)

    def qualified(ln, r, span):
        i = emp_i[r["employee_id"]]
        return (emps[i]["job_family"] == ln["job_family"]
                and r["proficiency_level"] >= ln["min_proficiency"]
                and all(valid_at(r, w) and alive(emps[i], w) for w in span))

    allocs = []

    def add_alloc(emp_id, ln, span, fte, inj=None):
        allocs.append({"allocation_id": f"ALLOC-{YEAR}-{len(allocs)+1:06d}", "employee_id": emp_id,
                       "project_id": ln["project_id"], "demand_line_id": ln["demand_line_id"],
                       "start_week": span[0], "end_week": span[-1], "allocated_fte": fte, "_inj": inj})
        return allocs[-1]

    load = [[0.0] * (WEEKS + 1) for _ in range(N)]     # planned load (pre-anomaly)
    for ln in lines:
        span = list(range(ln["start_week"], ln["demand_end_week"] + 1))
        need = ln["required_fte"]
        cands = [r for r in holders[ln["skill_id"]] if qualified(ln, r, span)]
        rng.shuffle(cands)
        for r in cands:
            if need < 0.5:
                break
            i = emp_i[r["employee_id"]]
            head = min(EFF[i][w] - load[i][w] for w in span)
            chunk = min(need, math.floor(head * 2) / 2)
            if chunk < 0.5:
                continue
            for w in span:
                load[i][w] += chunk
            need -= chunk
            add_alloc(r["employee_id"], ln, span, chunk)
    n_normal = len(allocs)

    # 10. Labelled anomaly injection (appended after valid baseline) ----------
    gt = []
    line_by_id = {ln["demand_line_id"]: ln for ln in lines}
    pairs = {(a["employee_id"], a["demand_line_id"]) for a in allocs}

    # A. Allocation exceeds availability (approved leave cuts capacity below commitment)
    done = tries = 0
    while done < p["anom_over"] and tries < 200000 and lines:
        tries += 1
        ln = rng.choice(lines)
        span = list(range(ln["start_week"], ln["demand_end_week"] + 1))
        pool_ = holders[ln["skill_id"]]
        if not pool_:
            continue
        r = rng.choice(pool_)
        i = emp_i[r["employee_id"]]
        if (r["employee_id"], ln["demand_line_id"]) in pairs or not qualified(ln, r, span):
            continue
        hit = [w for w in span if loss[i][w] > 0]
        if not hit:
            continue
        a = add_alloc(r["employee_id"], ln, span, math.ceil(emps[i]["fte"] * 2) / 2, inj="ALLOC_EXCEEDS_AVAILABILITY")
        pairs.add((r["employee_id"], ln["demand_line_id"]))
        w = hit[0]
        gt.append(gt_entry("ALLOC_EXCEEDS_AVAILABILITY", "ALLOC_EXCEEDS_AVAILABILITY", "allocation",
                           a["allocation_id"], [], True, "REDUCE_OR_REASSIGN",
                           f"week {w}: effective_capacity_fte={EFF[i][w]:.4f} < allocated_fte={a['allocated_fte']}",
                           "Approved leave in this week cuts capacity below the committed allocation."))
        done += 1

    # B. Employee assigned to overlapping projects beyond nominal FTE
    normal_allocs = allocs[:n_normal]
    done = tries = 0
    while done < p["anom_overlap"] and tries < 200000 and normal_allocs:
        tries += 1
        a = rng.choice(normal_allocs)
        i = emp_i[a["employee_id"]]
        L = rng.choice(lines)
        if (L["job_family"] != emps[i]["job_family"] or L["demand_line_id"] == a["demand_line_id"]
                or L["start_week"] > a["end_week"] or L["demand_end_week"] < a["start_week"]):
            continue
        r = es_key.get((a["employee_id"], L["skill_id"]))
        span = list(range(L["start_week"], L["demand_end_week"] + 1))
        if r is None or not qualified(L, r, span) or (a["employee_id"], L["demand_line_id"]) in pairs:
            continue
        b = add_alloc(a["employee_id"], L, span, math.ceil(emps[i]["fte"] * 2) / 2, inj="ALLOC_OVERLAP_EXCEEDS_FTE")
        pairs.add((a["employee_id"], L["demand_line_id"]))
        gt.append(gt_entry("ALLOC_OVERLAP_EXCEEDS_FTE", "ALLOC_OVERLAP_EXCEEDS_FTE", "allocation",
                           b["allocation_id"], [a["allocation_id"]], True, "RESOLVE_OVERLAP",
                           f"overlap {max(a['start_week'], L['start_week'])}..{min(a['end_week'], L['demand_end_week'])}: "
                           f"concurrent allocated_fte > fte={emps[i]['fte']}",
                           "Second commitment overlaps an existing allocation and pushes the employee over contracted FTE."))
        done += 1

    # C. Skill proficiency stale but recorded as CURRENT
    expired = [r for r in es if r["expiry_date"] is not None and r["expiry_date"] < SNAPSHOT]
    for r in rng.sample(expired, min(p["anom_stale"], len(expired))):
        r["status_recorded"] = "CURRENT"
        gt.append(gt_entry("SKILL_STALE_TREATED_CURRENT", "SKILL_STATUS_MISMATCH", "employee_skill",
                           r["employee_skill_id"], [], True, "TREAT_AS_EXPIRED_EXCLUDE_FROM_SUPPLY",
                           f"status=EXPIRED expiry_date={r['expiry_date'].isoformat()}",
                           "Recorded status CURRENT contradicts expiry_date before the snapshot."))

    # D. Demand double-counted (duplicate plan-line bridge row)
    base_total = defaultdict(float)
    for b in bridge:
        base_total[b["demand_id"]] += b["required_fte_applied"]
    for src in rng.sample(bridge, min(p["anom_dup"], len(bridge))):
        d = {"demand_plan_line_id": f"DPL-{len(bridge)+1:06d}", "demand_id": src["demand_id"],
             "demand_line_id": src["demand_line_id"], "required_fte_applied": src["required_fte_applied"]}
        bridge.append(d)
        gt.append(gt_entry("DEMAND_DOUBLE_COUNTED", "DEMAND_DUPLICATE_LINE", "demand_plan_line",
                           d["demand_plan_line_id"], [src["demand_plan_line_id"], src["demand_id"]], True,
                           "DEDUPLICATE_ON_DEMAND_AND_LINE", "duplicate of " + src["demand_plan_line_id"],
                           "Same demand line counted twice in one plan week."))
        gt.append(gt_entry("DEMAND_DOUBLE_COUNTED", "PLAN_TOTAL_MISMATCH", "demand_plan",
                           src["demand_id"], [], False, "USE_DISTINCT_TOTAL",
                           f"distinct required_fte_total={base_total[src['demand_id']]:.4f}",
                           "Stored total is inflated by the duplicate row."))

    # 11. Finalise allocations ------------------------------------------------
    for a in allocs:
        a["start_date"] = monday(a["start_week"])
        a["end_date"] = friday(a["end_week"])
        a["status"] = ("ENDED" if a["end_date"] < SNAPSHOT
                       else ("PLANNED" if a["start_date"] > SNAPSHOT else "ACTIVE"))

    # 12. Demand plan totals (raw sums, as stored) ----------------------------
    plan_total, plan_lines = defaultdict(float), defaultdict(int)
    for b in bridge:
        plan_total[b["demand_id"]] += b["required_fte_applied"]
        plan_lines[b["demand_id"]] += 1
    plans = []
    for did in sorted(plan_keys):
        pid, w = plan_keys[did]
        plans.append({"demand_id": did, "project_id": pid, "iso_year": YEAR, "iso_week": w,
                      "week_start_date": monday(w), "line_count": plan_lines[did],
                      "required_fte_total": round(plan_total[did], 4)})

    # 13. Gap analysis per demand line per week -------------------------------
    alloc_by_line = defaultdict(list)
    for a in allocs:
        alloc_by_line[a["demand_line_id"]].append(a)
    gaps = []
    for ln in lines:
        mp, fam, req = ln["min_proficiency"], ln["job_family"], ln["required_fte"]
        pool_ = [r for r in holders[ln["skill_id"]] if emps[emp_i[r["employee_id"]]]["job_family"] == fam]
        for w in range(ln["start_week"], ln["demand_end_week"] + 1):
            sup = sum(a["allocated_fte"] for a in alloc_by_line[ln["demand_line_id"]]
                      if a["start_week"] <= w <= a["end_week"])
            gap = max(0.0, req - sup)
            realloc, trainable = 0.0, 0
            for r in pool_:
                i = emp_i[r["employee_id"]]
                if not alive(emps[i], w) or not valid_at(r, w):
                    continue
                if r["proficiency_level"] >= mp:
                    realloc += max(0.0, math.floor((EFF[i][w] - load[i][w]) * 2) / 2)
                elif r["proficiency_level"] == mp - 1:
                    trainable += 1
            action = decide(gap, realloc, trainable)
            refs = ["ASM-001", "ASM-002", "ASM-003", "ASM-004", "ASM-005", "ASM-006", "ASM-010"]
            refs += {"REALLOCATE": ["ASM-007"], "TRAIN": ["ASM-008"], "HIRE": ["ASM-009"]}.get(action, [])
            gaps.append({"gap_id": f"GAP-{len(gaps)+1:06d}",
                         "demand_id": f"DEM-{ln['project_id']}-{YEAR}W{w:02d}",
                         "demand_line_id": ln["demand_line_id"], "project_id": ln["project_id"],
                         "iso_week": w, "required_fte": req, "supplied_fte": round(sup, 4),
                         "gap_fte": round(gap, 4), "coverage_ratio": round(sup / req, 4),
                         "risk_level": risk_gap(gap, req), "reallocatable_fte": round(realloc, 4),
                         "trainable_candidates": trainable, "recommended_action": action,
                         "assumption_refs": ";".join(refs)})

    # 14. Weekly capacity and utilisation -------------------------------------
    alloc_w = [0.0] * (WEEKS + 1)
    for a in allocs:
        for w in range(a["start_week"], a["end_week"] + 1):
            alloc_w[w] += a["allocated_fte"]
    weekly = []
    for w in range(1, WEEKS + 1):
        act = [i for i in range(N) if alive(emps[i], w)]
        nominal = len(act)
        pt = sum(1 - emps[i]["fte"] for i in act)
        leave = sum(emps[i]["fte"] * min(5, loss[i][w]) / 5 for i in act)
        eff_total = sum(EFF[i][w] for i in act)
        util = alloc_w[w] / eff_total if eff_total > 0 else 0.0
        weekly.append({"iso_week": w, "week_start_date": monday(w), "headcount_active": nominal,
                       "nominal_fte": round(float(nominal), 4), "part_time_loss_fte": round(pt, 4),
                       "leave_loss_fte": round(leave, 4), "effective_capacity_fte": round(eff_total, 4),
                       "allocated_fte": round(alloc_w[w], 4),
                       "unallocated_fte": round(max(0.0, eff_total - alloc_w[w]), 4),
                       "utilization": round(util, 4), "risk_level": risk_util(util)})

    T = {
        "role": roles, "skill": skills,
        "proficiency_scale": [{"level": l, "label": n, "description": d} for l, n, d in PROF_SCALE],
        "employee": emps, "employee_skill": es, "project": projects,
        "demand_line": lines, "demand_plan": plans, "demand_plan_line": bridge,
        "allocation": allocs, "availability": avail, "training": trainings,
        "gap_analysis": gaps, "weekly_capacity": weekly,
    }
    for i, g in enumerate(gt, 1):
        g["anomaly_id"] = f"ANOM-{i:04d}"
    return T, gt


# ----------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------
def write_csv(path, rows, columns):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow(columns)
        for r in rows:
            wr.writerow([fmtv(r.get(c)) for c in columns])


def sql_lit(v, typ):
    if v is None or v == "":
        return "NULL"
    if typ == "bool":
        return "1" if v else "0"
    if typ in ("int", "float"):
        return fmtv(v)
    return "'" + fmtv(v).replace("'", "''") + "'"


SQL_TYPE = {"string": "TEXT", "enum": "TEXT", "date": "TEXT", "int": "INTEGER",
            "float": "REAL", "bool": "INTEGER"}


def write_sql(path, T):
    # Deferred FK checks: employee.manager_id may point to a row inserted later.
    out = ["PRAGMA foreign_keys = ON;", "BEGIN TRANSACTION;", "PRAGMA defer_foreign_keys = ON;"]
    for t in SQL_ORDER:
        spec = SCHEMA[t]
        defs = []
        for name, typ, req, _ in spec["columns"]:
            d = f"  {name} {SQL_TYPE[typ]}" + (" NOT NULL" if req else "")
            fk = FKS.get(f"{t}.{name}")
            if fk:
                d += f" REFERENCES {fk.split('.')[0]}({fk.split('.')[1]})"
            defs.append(d)
        defs.append(f"  PRIMARY KEY ({', '.join(spec['pk'])})")
        out.append(f"CREATE TABLE {t} (\n" + ",\n".join(defs) + "\n);")
        names = [c[0] for c in spec["columns"]]
        for r in T[t]:
            vals = ", ".join(sql_lit(r.get(c[0]), c[1]) for c in spec["columns"])
            out.append(f"INSERT INTO {t} ({', '.join(names)}) VALUES ({vals});")
    out.append("COMMIT;")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def write_dictionary(path):
    L = ["# Data dictionary", "",
         "Generated from `SCHEMA` in `generate.py`, which is the single source of truth.", ""]
    for t, spec in SCHEMA.items():
        L += [f"## {spec['entity']} (`{LAYOUT[t]}`)", "", spec["desc"], "",
              f"- Primary key: {', '.join(spec['pk'])}"]
        fks = [f"{k} -> {v}" for k, v in FKS.items() if k.startswith(t + ".")]
        L.append("- Foreign keys: " + ("; ".join(fks) if fks else "none"))
        L += ["", "| Field | Type | Required | Rule, allowed values, units |", "|---|---|---|---|"]
        for name, typ, req, rule in spec["columns"]:
            if (t, name) in ENUMS:
                rule += " Allowed: " + ", ".join("null" if v is None else str(v) for v in ENUMS[(t, name)])
            L.append(f"| {name} | {typ} | {'Y' if req else 'N'} | {rule} |")
        L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_all(out, p, T, gt):
    counts = {}
    for t, rel in LAYOUT.items():
        write_csv(os.path.join(out, rel), T[t], [c[0] for c in SCHEMA[t]["columns"]])
        counts[t] = len(T[t])
    gt_cols = ["anomaly_id", "issue_type", "check_name", "table", "row_key", "related_keys",
               "primary", "expected_decision", "expected_value", "rationale"]
    gt_rows = [dict(g, related_keys=";".join(g["related_keys"])) for g in gt]
    os.makedirs(os.path.join(out, "ground_truth"), exist_ok=True)
    with open(os.path.join(out, "ground_truth", "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump({"note": "Expected handling for labelled anomalies. Never use as model input.",
                   "anomalies": gt}, f, indent=2, sort_keys=True)
    write_csv(os.path.join(out, "ground_truth", "ground_truth.csv"), gt_rows, gt_cols)
    counts["ground_truth"] = len(gt)

    os.makedirs(os.path.join(out, "docs"), exist_ok=True)
    write_dictionary(os.path.join(out, "docs", "data_dictionary.md"))
    write_csv(os.path.join(out, "docs", "assumptions.csv"),
              [{"assumption_id": a, "statement": s} for a, s in ASSUMPTIONS],
              ["assumption_id", "statement"])
    with open(os.path.join(out, "docs", "source_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"synthetic": True,
                   "external_sources": [],
                   "personal_data": "None. Names are drawn from a small invented list and are not real people.",
                   "license": "Output of this generator only. No third-party data is included or redistributed.",
                   "transformations": "None. All values are generated by generate.py from seed and parameters."},
                  f, indent=2, sort_keys=True)
    write_sql(os.path.join(out, "seed.sql"), T)

    files = {}
    for root, _, fs in os.walk(out):
        for fn in fs:
            full = os.path.join(root, fn)
            files[os.path.relpath(full, out)] = sha256(full)
    manifest = {"generator": "generate.py", "generator_version": GENERATOR_VERSION,
                "generator_sha256": sha256(os.path.abspath(__file__)),
                "parameters": p, "snapshot_date": SNAPSHOT.isoformat(),
                "horizon": f"ISO weeks 1..{WEEKS} of {YEAR}", "row_counts": counts,
                "files_sha256": dict(sorted(files.items()))}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=list(PRESETS), default="full")
    ap.add_argument("--employees", type=int)
    ap.add_argument("--skills", type=int)
    ap.add_argument("--projects", type=int)
    ap.add_argument("--training", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    p = dict(PRESETS[a.preset])
    for k in ["employees", "skills", "projects", "training", "seed"]:
        if getattr(a, k) is not None:
            p[k] = getattr(a, k)
    T, gt = build(p)
    counts = write_all(a.out, p, T, gt)
    print(f"generated -> {a.out}")
    for k, v in counts.items():
        print(f"  {k:<18} {v:>8}")


if __name__ == "__main__":
    main()
