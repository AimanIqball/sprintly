"""Tests for the planning engine. The GUI part of sprintly.py is not loaded, so no display is needed."""
from datetime import date
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "sprintly.py").read_text(encoding="utf-8")
NS = {}
exec(compile(SRC.split("# PART 2: GUI")[0], "sprintly_engine", "exec"), NS)

DESC = ("Smart campus system with student login, course enrollment, attendance tracking, fee payment, "
        "admin dashboard, email notifications, reports and analytics, and a mobile app.")
TEAM = {
    "Ali": {"skills": {"backend": 1.2, "database": 1.0}, "hours_per_week": 40},
    "Sara": {"skills": {"frontend": 1.2, "design": 1.0}, "hours_per_week": 40},
    "Usman": {"skills": {"devops": 1.2, "backend": 1.0, "security": 0.9}, "hours_per_week": 40},
    "Hina": {"skills": {"qa": 1.2, "analysis": 1.0}, "hours_per_week": 40},
    "Bilal": {"skills": {"mobile": 1.1, "frontend": 1.0, "backend": 0.9}, "hours_per_week": 40},
    "Ayesha": {"skills": {"database": 1.2, "data": 1.1, "analysis": 1.0}, "hours_per_week": 40},
}


def fast_settings(hours=480):
    return NS["Settings"](deadline_hours=hours, ga_generations=25, ga_population=30, astar_node_limit=1500)


def test_generated_tasks_are_valid_and_acyclic():
    tasks = NS["generate_tasks"]("Demo", DESC)
    ids = {t["id"] for t in tasks}
    assert len(tasks) >= 25
    assert all(d in ids for t in tasks for d in t["deps"])
    assert all(1 <= t["priority"] <= 5 and t["hours"] > 0 for t in tasks)
    assert NS["Graph"]({t["id"]: t["deps"] for t in tasks}).find_cycle() is None


def test_keywords_add_modules():
    phases = {t["phase"] for t in NS["generate_tasks"]("Demo", DESC)}
    assert {"Payments", "Mobile App", "Notifications", "Admin and Dashboard"} <= phases


def test_cycle_detection():
    assert NS["Graph"]({"T1": ["T2"], "T2": ["T1"]}).find_cycle() is not None


def test_plan_respects_dependencies_and_skills():
    tasks = NS["generate_tasks"]("Demo", DESC)
    r = NS["build_plan"](tasks, TEAM, fast_settings(), date(2026, 1, 5))
    m, start, end = r["model"], r["start"], r["end"]
    for i in range(m.n):
        for d in m.deps[i]:
            assert start[i] >= end[d] - 1e-9
        assert r["plan"].assign[i] in m.eligible[i]
    assert r["evaluation"].late == 0
    assert r["sprints"] >= 1


def test_missing_skill_uses_fallback():
    tasks = [{"id": "T01", "phase": "X", "name": "Rare", "hours": 4, "skill": "cobol", "priority": 3, "deps": []}]
    r = NS["build_plan"](tasks, TEAM, fast_settings(100), date(2026, 1, 5))
    assert r["model"].fallback == ["T01"]


def test_parse_skills():
    assert NS["parse_skills"]("backend:1.2, qa") == {"backend": 1.2, "qa": 1.0}
    assert NS["parse_skills"]("1.2") is None


def test_workdays_skip_weekends():
    assert NS["count_workdays"](date(2026, 1, 5), date(2026, 1, 11)) == 5  # Mon..Sun
    assert NS["add_workdays"](date(2026, 1, 9), 1) == date(2026, 1, 12)    # Fri + 1 = Mon
