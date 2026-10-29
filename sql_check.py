#!/usr/bin/env python3
"""sql_check.py - loads seed.sql into SQLite with FK checks and runs integrity queries.

Usage: python3 sql_check.py out/full_run1
"""
import os, sqlite3, sys

root = sys.argv[1]
con = sqlite3.connect(":memory:")
con.executescript(open(os.path.join(root, "seed.sql"), encoding="utf-8").read())
fk = con.execute("PRAGMA foreign_key_check").fetchall()
integ = con.execute("PRAGMA integrity_check").fetchone()[0]
bad_window = con.execute("""SELECT COUNT(*) FROM gap_analysis g JOIN demand_line l USING(demand_line_id)
                            WHERE g.iso_week NOT BETWEEN l.start_week AND l.demand_end_week""").fetchone()[0]
print(f"seed.sql loads: FK violations={len(fk)} integrity={integ} gap rows outside line window={bad_window}")
ok = not fk and integ == "ok" and bad_window == 0
sys.exit(0 if ok else 1)
