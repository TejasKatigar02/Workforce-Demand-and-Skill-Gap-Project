# Workforce Demand, Skill Gap and Utilisation: Synthetic Dataset Generator

A deterministic generator and validator for a medium-complexity planning workflow:
forecast demand by role and project, allocate people, measure capacity and
utilisation (with leave and part-time factors), find skill gaps, and recommend
REALLOCATE, TRAIN or HIRE. Every derived value is computed from source records,
every injected defect is labelled in a separate ground-truth file, and the same
seed reproduces byte-identical output.

Standard library only (Python 3.8+). No external data is used, so there are no
licence or personal-data obligations (see `docs/source_manifest.json`).

## Quick start

```bash
./run_all.sh out          # generate twice, validate, check reproducibility, SQL load, mutation tests
python3 generate.py --preset starter --out out/starter   # small fast dataset, same generator
python3 generate.py --preset full --employees 800 --projects 120 --out out/custom   # any scale
```

`run_all.sh` takes about 15 seconds at full scale and exits non-zero on any failure.

## Folder layout (generated output)

| Path | Contents |
|---|---|
| `master/` | role, skill, proficiency_scale, employee, employee_skill, project |
| `transactions/` | demand_line, demand_plan, demand_plan_line, allocation, availability, training |
| `analysis/` | gap_analysis (demand line x ISO week), weekly_capacity (derived) |
| `ground_truth/` | labelled anomalies and expected handling. **Never use as model input.** |
| `docs/` | data_dictionary.md (generated from code), assumptions.csv, source_manifest.json, validation_summary.md/json, repro_report.txt |
| `seed.sql` | full seed with CREATE TABLE, primary keys, foreign keys and INSERTs |
| `manifest.json` | parameters, seed, generator SHA-256, row counts, SHA-256 of every file |

## Files in this project

| File | Role |
|---|---|
| `generate.py` | Schema (single source of truth), rules, generator, writers. |
| `validate.py` | Independent checks. Re-derives computed columns from raw input. Writes validation reports. |
| `repro_check.py` | Compares two runs file by file (SHA-256) and reports row counts. |
| `sql_check.py` | Loads `seed.sql` into SQLite with foreign keys enforced and runs integrity queries. |
| `mutation_test.py` | Corrupts a copy of the data eight different ways and confirms the validator fails each time. |
| `run_all.sh` | Runs everything in order. |

## Entity model

Core entities follow the brief: Employee, Role, Skill, EmployeeSkill, Project,
DemandPlan, Allocation, Availability, Training and GapAnalysis. Three supporting
tables are needed for the rules: DemandLine (a requirement per project: role,
skill, FTE, minimum proficiency), DemandPlanLine (the bridge that makes
DemandPlan and DemandLine many-to-many), and WeeklyCapacity (derived per week).
ProficiencyScale is a reference table.

| Relationship | Cardinality | Mechanism |
|---|---|---|
| Role to Employee | 1:N | `employee.role_id` (required) |
| JobFamily to Role, Skill, Project | 1:N | `job_family` on each (denormalised, validated equal to role or skill) |
| Employee to Skill | N:M | bridge `employee_skill` (PK `employee_skill_id`, unique `(employee_id, skill_id)`) |
| Employee to Manager | N:1 optional | `employee.manager_id` must reference a LEAD |
| Project to DemandLine | 1:N (1 to 4 lines) | `demand_line.project_id` |
| Project to DemandPlan | 1:N (one per ISO week in window) | `demand_plan.project_id` |
| DemandPlan to DemandLine | N:M | bridge `demand_plan_line` |
| Employee to DemandLine via Allocation | N:M | `allocation.employee_id`, `allocation.demand_line_id` |
| Employee to Availability | 1:N | `availability.employee_id` (non-overlapping per employee) |
| Employee to Training | 1:N | `training.employee_id`, `training.skill_id` |
| DemandLine to GapAnalysis | 1:N (one row per week) | `gap_analysis.demand_line_id`, `gap_analysis.demand_id` |

Full field-level definitions (types, required flags, units, allowed values and
rules) are in `docs/data_dictionary.md`, generated from `SCHEMA` in `generate.py`
so the documentation cannot drift from the code.

## Identifiers

`EMP-000001`, `SKL-000001`, `PRJ-2026-000001`, `DEM-PRJ-2026-000001-2026W01`,
`ALLOC-2026-000001`. Other entities use the same pattern: `ROL-`, `ESK-`, `DLN-`,
`DPL-`, `AVL-`, `TRN-`, `GAP-`. IDs are zero-padded, immutable, and assigned in
generation order. Every foreign key resolves (see validation).

## Business rules

1. **Skill validity.** A verification is valid until `last_verified_date + skill.expiry_months`. Skills with no expiry never lapse. Completed training refreshes `last_verified_date`, and proficiency becomes the maximum of the base level and any completed training result (`employee_skill` is derived).
2. **Leave.** Only APPROVED leave reduces capacity. Each approved weekday lost removes 1/5 of the employee's FTE in that week. PENDING leave is recorded but has no effect.
3. **Part-time.** Contracted FTE of 0.5, 0.6, 0.8 or 1.0 caps capacity in every active week.
4. **Lifecycle.** Employees move ACTIVE to LEAVER once and never back. A leaver has capacity through the ISO week of their leaver date and none afterwards. Projects are PLANNED, ACTIVE, COMPLETED or CANCELLED (status derived against snapshot week 44). A cancelled project has demand only before `cancelled_week`. Allocations are PLANNED, ACTIVE or ENDED (derived from dates against the 2026-10-28 snapshot).
5. **Allocation.** A line is filled greedily from qualified holders: same family, proficiency at or above the line minimum, valid skill and alive for the whole window. Each commitment is in 0.5 FTE steps and limited to the employee's minimum residual capacity across the window. Employees may hold several concurrent allocations as long as the total stays within capacity.
6. **Gap.** For each line and week: `gap = max(0, required - supplied)`. Coverage, risk and action are computed from these values (see below).
7. **Recommendation rule** (`generate.decide`):
   - `NONE` if there is no gap.
   - `REALLOCATE` if same-family, valid, qualified residual capacity in that week covers the gap.
   - `TRAIN` if enough employees sit one proficiency level below the minimum with a valid skill to cover the gap, at one FTE each.
   - `HIRE` otherwise.
   Each row cites the assumptions behind it in `assumption_refs`. Assumptions are listed in `docs/assumptions.csv`.
8. **Risk.** Weekly utilisation = allocated / effective capacity. HIGH at 0.95 or above, MEDIUM at 0.80 or above. Gap risk is HIGH when the gap is at least half the requirement.

## Labelled anomalies (edge cases)

All four required cases are injected after the valid baseline is built. Each is
recorded only in `ground_truth/ground_truth.json`. The input files contain the
raw defect and carry no label.

| Issue type | Where it lives | Count (full) | Expected handling |
|---|---|---:|---|
| Allocation exceeds availability (leave makes the commitment impossible) | `allocation` | 25 | Reduce or reassign |
| Employee assigned to overlapping projects beyond FTE | `allocation` | 25 | Resolve overlap |
| Skill proficiency stale but recorded as CURRENT | `employee_skill.status_recorded` | 40 | Treat as expired; exclude from supply |
| Demand double-counted in a plan week | `demand_plan_line` (duplicate row) and the derived `demand_plan` total | 30 | Deduplicate on demand and line |

Ground truth holds the expected decision, the expected value, and the rationale
for each anomaly. The validator matches every violation against it by row ID.

## Validation

`validate.py` runs these check families. Each violation must match a labelled
anomaly, and every labelled anomaly must be detected.

- PK uniqueness and FK integrity across all 14 tables
- Enumerations and units (FTE steps, proficiency 1 to 5, status vocabularies)
- Date ordering: hire before verification, leave ranges valid and non-overlapping, training within hire-to-snapshot, leaver after hire
- Lifecycle: project status and cancellation, allocation status against dates, no allocation after leaver week, no demand outside the line window
- Arithmetic: plan totals against bridge rows, capacity identity `nominal - part-time - leave = effective`, weekly totals recomputed from raw rows, gap = max(0, required - supplied), coverage ratio, risk level
- Decision consistency: each recommendation re-run through the rule; `NONE` only when there is no gap; assumption references resolve
- Coverage: one gap row per line-week
- Concurrency: cumulative commitment per employee-week against effective capacity and against nominal FTE

Results on the full dataset are in `docs/validation_summary.md`:
**PASS, 0 unlabelled violations, 120 of 120 injected anomalies detected.**

`mutation_test.py` confirms the validator is not vacuous. It corrupts the data in eight ways (over-commitment, gap arithmetic, duplicate PK, orphan FK, capacity drift, wrong decision, leaver lifecycle, date order) and requires every case to fail.

## Reproducibility

Seed `20261022` is fixed in `PRESETS`. `run_all.sh` generates the full dataset twice,
and `repro_check.py` confirms every file is byte-identical (see
`docs/repro_report.txt`). `manifest.json` records parameters, row counts and
SHA-256 for every file. The manifest contains no timestamps, so the bytes stay
stable across machines.

Starter and full datasets come from the same generator with different
parameters. Scale is set in `PRESETS` or on the command line.

## Known limitations (read before using the results)

- **Utilisation is moderate, not saturated.** Mean weekly utilisation is about 40% and peaks near 66%, so no week reaches HIGH risk. Demand is skill-constrained: many lines need a specific skill, and the people who hold it are often already committed or temporarily expired. Raise `REQ_FTE_W` or `LINES_W` in `generate.py` to push utilisation higher.
- **REALLOCATE is the dominant recommendation.** Greedy allocation commits each line for its full window (ASM-006). Same-family capacity is often free in some weeks of the window but not all of them, so the gap analysis reports reallocatable capacity that a planner could use by splitting windows. A production planner would allow partial windows. That would be the next improvement.
- **Reallocation pools overlap across lines.** The pool is per line and not netted across lines (ASM-007). Treat it as an upper bound.
- **No time-to-hire or training lead time** (ASM-008, ASM-009).
- **Names are synthetic and not unique.** No personal data.
- **Weeks 1 to 52 only.** ISO week 53 of 2026 is excluded from the horizon.

## Extending the project

- Add a column: edit `SCHEMA` (and `ENUMS` if it is an enum). The writer, SQL export, data dictionary and validator's column handling follow. Add a rule check in `validate.py`.
- Add an anomaly: inject it in `build()` after the baseline and append a `gt_entry`. Then extend `mutation_test.py` with a corruption case.
- Change a rule: edit `decide()`, `risk_gap()` or `risk_util()` in `generate.py`. The validator imports these same functions, so a rule change is reflected in both the output and the checks. That means the validator checks consistency with the documented rule, not an independent re-implementation of it. Keep that in mind when reviewing.
