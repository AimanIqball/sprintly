"""
Sprintly - AI Sprint Planner (colorful desktop app, single file)
================================================================
Run:  python sprintly.py          (Python 3.9+, standard library only, uses tkinter)

What it does
  1. You enter: project name, description, expected timeline (weeks) and deadline (date).
  2. Sprintly analyses the description and splits the project into phases and small tasks.
     Every task gets suggested hours, priority, skill and dependencies.
  3. Developers (name + skills) are added once and saved to  ~/.sprintly/team.json
  4. The planner assigns the most suitable developers and builds a sprint-wise schedule
     (DFS cycle check, BFS levels, CPM, A* assignment, Genetic Algorithm optimisation).
  5. You can edit / add / delete generated tasks and export the plan to CSV.
"""

import csv
import heapq
import itertools
import json
import math
import queue
import random
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import pstdev

# =====================================================================
# PART 1: PLANNING ENGINE (no GUI code in this part)
# =====================================================================
STD_SKILLS = ["analysis", "design", "backend", "frontend", "database", "devops", "qa", "security", "mobile", "data"]
FALLBACK_PROFICIENCY = 0.7  # used when nobody has the required skill


def natkey(task_id):
    m = re.search(r"\d+", task_id)
    return int(m.group()) if m else 0


def parse_skills(text):
    """'backend:1.2, qa' -> {'backend': 1.2, 'qa': 1.0}. Returns None when invalid."""
    tokens = text.replace(",", " ").replace(";", " ").replace(":", " ").replace("=", " ").split()
    skills, last = {}, None
    for token in tokens:
        try:
            value = float(token)
        except ValueError:
            last = token.lower()
            skills[last] = 1.0
            continue
        if last is None or value <= 0:
            return None
        skills[last] = value
    return skills or None


def format_skills(skills):
    return ", ".join(f"{k}:{v:g}" for k, v in skills.items())


# ---------------------------------------------------------------- dates
def add_workdays(start, days):
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def count_workdays(start, end):
    """Working days from start to end, both inclusive (Mon-Fri)."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def hours_to_date(start, hours, hours_per_day, is_end=False):
    day = int((hours - 1e-9) // hours_per_day) if is_end else int(hours // hours_per_day)
    return add_workdays(start, max(0, day))


# ---------------------------------------------------------------- settings
@dataclass
class Settings:
    deadline_hours: float = 160.0
    hours_per_day: int = 8
    sprint_weeks: int = 1
    seed: int = 42
    group_limit: int = 90
    w_makespan: float = 1.0
    w_priority: float = 0.4
    w_balance: float = 0.3
    w_overflow: float = 15.0
    w_late: float = 30.0
    astar_weight: float = 1.1
    astar_node_limit: int = 6000
    ga_population: int = 60
    ga_generations: int = 120
    ga_mutation_rate: float = 0.04
    ga_crossover_rate: float = 0.85
    ga_tournament: int = 3
    ga_elite: int = 2
    ga_patience: int = 40

    @property
    def sprint_hours(self):
        return self.sprint_weeks * self.hours_per_day * 5


# ---------------------------------------------------------------- task generator
class Builder:
    def __init__(self, factor):
        self.factor, self.tasks, self.refs, self.groups = factor, [], {}, {}

    def add(self, key, phase, name, hours, skill, priority, deps=(), group=None):
        tid = f"T{len(self.tasks) + 1:02d}"
        resolved = [self.refs[k] for k in deps if k in self.refs]
        h = max(1.0, round(hours * self.factor * 2) / 2)
        self.tasks.append({"id": tid, "phase": phase, "name": name, "hours": h, "skill": skill,
                           "priority": priority, "deps": resolved})
        self.refs[key] = tid
        if group:
            self.groups.setdefault(group, []).append(key)

    def grp(self, name):
        return self.groups.get(name, [])


# (module, keywords as regex, phase, [(key, task name, hours, skill, priority, deps, group)])
MODULES = [
    ("payments", ["payment", "checkout", "billing", "invoice", "subscription", "stripe", "wallet", r"fees?\b",
                  "tuition"], "Payments", [
        ("pay_api", "Payment gateway integration", 12, "backend", 5, ["auth_api", "schema"], "api"),
        ("pay_ui", "Payment and checkout screens", 8, "frontend", 4, ["pay_api", "design"], "ui"),
        ("pay_sec", "Payment security audit", 6, "security", 5, ["pay_api"], "sec")]),
    ("ecommerce", ["cart", r"orders?\b", "shop", "inventory", r"e-?commerce", r"products?\b", "catalog"],
     "Catalog and Orders", [
         ("cat_api", "Catalog and inventory API", 10, "backend", 4, ["auth_api", "schema"], "api"),
         ("ord_api", "Cart and order processing API", 12, "backend", 5, ["cat_api"], "api"),
         ("ord_ui", "Catalog and cart screens", 10, "frontend", 4, ["cat_api", "design"], "ui")]),
    ("booking", ["booking", "appointment", "reservation", "timetable", "calendar", "attendance"], "Scheduling", [
        ("sch_api", "Scheduling / attendance API", 12, "backend", 4, ["auth_api", "schema"], "api"),
        ("sch_ui", "Calendar and scheduling screens", 10, "frontend", 4, ["sch_api", "design"], "ui"),
        ("sch_rule", "Reminder and conflict rules", 6, "backend", 3, ["sch_api"], "api")]),
    ("realtime", ["chat", "messag", r"real-?time", "video", "meeting", r"live\b"], "Communication", [
        ("rt_api", "Real-time messaging service", 12, "backend", 4, ["auth_api"], "api"),
        ("rt_ui", "Chat / live interface", 10, "frontend", 4, ["rt_api", "design"], "ui")]),
    ("notify", ["notif", "email", "sms", "alert", "reminder"], "Notifications", [
        ("not_api", "Notification service (email/SMS)", 8, "backend", 3, ["auth_api"], "api"),
        ("not_ui", "Notification preferences screen", 5, "frontend", 2, ["not_api", "design"], "ui")]),
    ("dashboard", ["dashboard", "admin", "manag"], "Admin and Dashboard", [
        ("adm_api", "Admin dashboard API", 8, "backend", 3, ["auth_api", "rbac"], "api"),
        ("adm_ui", "Admin dashboard UI", 10, "frontend", 3, ["adm_api", "design"], "ui"),
        ("usr_ui", "User management screens", 6, "frontend", 3, ["adm_api"], "ui")]),
    ("search", ["search", "filter", "listing", "browse", "directory", "library"], "Search and Listing", [
        ("se_api", "Search and listing API", 8, "backend", 3, ["auth_api", "schema"], "api"),
        ("se_ui", "Search and listing screens", 8, "frontend", 3, ["se_api", "design"], "ui")]),
    ("files", ["upload", r"files?\b", r"documents?\b", r"images?\b", "media"], "File Handling", [
        ("fi_api", "File storage and upload service", 8, "backend", 3, ["auth_api"], "api"),
        ("fi_ui", "Upload and preview screens", 6, "frontend", 3, ["fi_api", "design"], "ui")]),
    ("analytics", ["report", "analytic", "chart", "statistic", "insight", "predict", "recommend", r"ai\b", r"ml\b",
                   "result"], "Analytics and Reports", [
        ("dw", "Reporting data model", 6, "data", 2, ["schema"], "api"),
        ("an_api", "Analytics and report engine", 10, "data", 3, ["dw"], "api"),
        ("an_ui", "Charts and report screens", 8, "frontend", 2, ["an_api", "design"], "ui")]),
    ("maps", [r"maps?\b", "location", "gps"], "Location", [
        ("map_api", "Location service", 8, "backend", 3, ["auth_api"], "api"),
        ("map_ui", "Map view", 8, "frontend", 3, ["map_api", "design"], "ui")]),
    ("mobile", ["mobile", "android", r"ios\b", "flutter", "react native"], "Mobile App", [
        ("mob_arch", "Mobile app architecture", 6, "mobile", 3, ["api_c", "design"], "mobile"),
        ("mob_login", "Mobile login and onboarding", 6, "mobile", 3, ["mob_arch", "auth_api"], "mobile"),
        ("mob_core", "Mobile core screens", 12, "mobile", 4, ["mob_login"], "mobile"),
        ("mob_push", "Push notifications", 5, "mobile", 2, ["mob_login"], "mobile")]),
]
AUTH_KEYS = ["login", "signup", "sign up", "auth", "user", "account", "role", "register", "password", "student"]


def _hit(keys, text):
    return bool(re.search(r"\b(?:" + "|".join(keys) + ")", text))


def extract_features(description, known_keys):
    """Short phrases of the description that no built-in module covers become custom features."""
    out = []
    for seg in re.split(r"[\n,;•*:]|\.\s|\.$|\band\b|\baur\b", description):
        s = re.sub(r"^[\s\-\d\)\.]+", "", seg).strip(" .-")
        words = s.split()
        low = s.lower()
        if not 2 <= len(words) <= 7:
            continue
        if re.match(r"(a|an|the|this|it|we|i|my|our|users?|should|will|can|for)\b", low):
            continue
        if re.search(r"\b(system|platform|application|website|software|project|app)\b", low):
            continue
        if any(_hit(k, low) for k in known_keys):
            continue
        title = s[0].upper() + s[1:]
        if title.lower() not in [x.lower() for x in out]:
            out.append(title)
    return out[:6]


def generate_tasks(name, description):
    """Analyses the description and returns a list of task dictionaries (phases, hours, skills, deps)."""
    text = (name + " " + description).lower()
    words = len(description.split())
    factor = min(1.5, max(0.8, 0.8 + words / 150))
    api_only = bool(re.search(r"api only|backend only|no ui|no frontend|\bcli\b", text))
    mobile = _hit(MODULES[-1][1], text)
    web = bool(re.search(r"\b(web|website|portal|dashboard|admin|browser|desktop)", text))
    ui = not api_only and not (mobile and not web)
    b = Builder(factor)

    P, D, S, A = "Planning and Analysis", "Design and Architecture", "Setup and Infrastructure", "Accounts and Security"
    b.add("req", P, "Requirement gathering", 8, "analysis", 5)
    b.add("risk", P, "Feasibility and risk study", 5, "analysis", 3, ["req"])
    b.add("srs", P, "Software requirement specification (SRS)", 8, "analysis", 5, ["req"])
    b.add("arch", D, "System architecture design", 10, "backend", 5, ["srs"])
    b.add("dbd", D, "Database design (ER diagram)", 8, "database", 5, ["srs"])
    b.add("api_c", D, "API contract design", 6, "backend", 4, ["arch"])
    if not api_only:
        b.add("design", D, "UI/UX wireframes and style guide", 8, "design", 4, ["srs"])
    b.add("repo", S, "Git repository and CI pipeline", 6, "devops", 4, ["risk", "arch"])
    b.add("env", S, "Dev and staging environment setup", 8, "devops", 4, ["repo"])
    b.add("schema", S, "Create database schema", 8, "database", 5, ["dbd"])
    b.add("auth_api", A, "Authentication API (login, signup, tokens)", 10, "backend", 5, ["api_c", "schema"], "api")
    b.add("rbac", A, "Role based access control", 6, "backend", 4, ["auth_api"], "api")
    b.add("sec_val", A, "Input validation and attack protection", 6, "security", 4, ["auth_api"], "sec")
    if ui:
        b.add("auth_ui", A, "Login and signup screens", 8, "frontend", 4, ["design", "auth_api"], "ui")

    used = []
    for key, keys, phase, items in MODULES:
        if not _hit(keys, text):
            continue
        used.append(keys)
        for k, tname, hours, skill, prio, deps, group in items:
            if group == "ui" and not ui:
                continue
            b.add(k, phase, tname, hours, skill, prio, deps, group)

    custom = extract_features(description, used + [AUTH_KEYS])
    for n, title in enumerate(custom, 1):
        b.add(f"cf{n}_api", "Core Features", f"{title} - backend logic", 8, "backend", 3,
              ["auth_api", "schema", "api_c"], "api")
        if ui:
            b.add(f"cf{n}_ui", "Core Features", f"{title} - screens", 8, "frontend", 3,
                  [f"cf{n}_api", "design"], "ui")

    Q, R = "Quality Assurance", "Release and Handover"
    b.add("tplan", Q, "Test plan and test cases", 8, "qa", 4, ["srs"])
    b.add("t_api", Q, "Backend unit and API tests", 10, "qa", 4, ["tplan"] + b.grp("api"))
    if ui:
        b.add("t_ui", Q, "Frontend UI tests", 8, "qa", 4, ["tplan"] + b.grp("ui"))
    if b.grp("mobile"):
        b.add("t_mob", Q, "Mobile app testing", 8, "qa", 3, ["tplan"] + b.grp("mobile"))
    b.add("integ", Q, "Integration testing", 10, "qa", 5, ["t_api", "t_ui", "t_mob"])
    b.add("t_sec", Q, "Security and penetration testing", 8, "security", 4, ["integ"] + b.grp("sec"))
    b.add("perf", Q, "Performance and load testing", 8, "qa", 3, ["integ"])
    b.add("bugs", Q, "Bug fixing round", 10, "backend", 5, ["integ", "t_sec", "perf"])
    b.add("uat", Q, "User acceptance testing (UAT)", 8, "qa", 5, ["bugs"])
    b.add("stage", R, "Containerize and deploy to staging", 8, "devops", 5, ["env", "integ"])
    b.add("ops", R, "Monitoring, logging and backup setup", 6, "devops", 3, ["stage"])
    b.add("docs", R, "User and API documentation", 6, "analysis", 3, ["integ"])
    b.add("train", R, "Training material and guides", 5, "analysis", 2, ["docs"])
    b.add("prod", R, "Deploy to production", 6, "devops", 5, ["uat", "stage", "ops", "bugs"])
    b.add("final", R, "Final report and handover", 5, "analysis", 3, ["prod", "docs", "train"])
    return b.tasks


# ---------------------------------------------------------------- graph algorithms
class Graph:
    """Dependency graph: DFS cycle detection and BFS levels."""

    def __init__(self, deps):
        self.deps = deps
        self.children = {t: [] for t in deps}
        for t, ds in deps.items():
            for d in ds:
                self.children.setdefault(d, []).append(t)

    def find_cycle(self):
        color = {t: 0 for t in self.deps}
        path = []

        def visit(t):
            color[t] = 1
            path.append(t)
            for d in self.deps[t]:
                if d not in color:
                    continue
                if color[d] == 1:
                    return path[path.index(d):] + [d]
                if color[d] == 0:
                    found = visit(d)
                    if found:
                        return found
            path.pop()
            color[t] = 2
            return None

        for t in sorted(self.deps, key=natkey):
            if color[t] == 0:
                cycle = visit(t)
                if cycle:
                    return cycle
        return None

    def levels(self):
        waiting = {t: len(ds) for t, ds in self.deps.items()}
        level = {t: 0 for t in waiting if waiting[t] == 0}
        q = deque(sorted(level, key=natkey))
        while q:
            cur = q.popleft()
            for ch in self.children[cur]:
                waiting[ch] -= 1
                if waiting[ch] == 0:
                    level[ch] = max(level[d] for d in self.deps[ch]) + 1
                    q.append(ch)
        return level


# ---------------------------------------------------------------- model
class Model:
    """Index based model of tasks and developers."""

    def __init__(self, tasks, devs, st):
        self.st = st
        self.tasks = sorted(tasks, key=lambda t: natkey(t["id"]))
        self.ids = [t["id"] for t in self.tasks]
        self.n = len(self.ids)
        self.index = {t: i for i, t in enumerate(self.ids)}
        self.dev_names = list(devs)
        self.m = len(self.dev_names)
        self.hours = [float(t["hours"]) for t in self.tasks]
        self.priority = [int(t["priority"]) for t in self.tasks]
        self.deps = [[self.index[d] for d in dict.fromkeys(t["deps"]) if d in self.index and d != t["id"]]
                     for t in self.tasks]
        self.children = [[] for _ in range(self.n)]
        for i in range(self.n):
            for d in self.deps[i]:
                self.children[d].append(i)
        weeks = max(1.0, st.deadline_hours / (st.hours_per_day * 5))
        self.capacity = [devs[d]["hours_per_week"] * weeks for d in self.dev_names]

        self.duration = [[None] * self.m for _ in range(self.n)]
        self.eligible = [[] for _ in range(self.n)]
        self.fallback = []
        for i, t in enumerate(self.tasks):
            skill = t["skill"].lower()
            for d, name in enumerate(self.dev_names):
                prof = devs[name]["skills"].get(skill)
                if prof:
                    self.duration[i][d] = self.hours[i] / prof
                    self.eligible[i].append(d)
            if not self.eligible[i]:
                self.fallback.append(t["id"])
                for d in range(self.m):
                    self.duration[i][d] = self.hours[i] / FALLBACK_PROFICIENCY
                    self.eligible[i].append(d)

        level = Graph({self.ids[i]: [self.ids[d] for d in self.deps[i]] for i in range(self.n)}).levels()
        self.level_of = [level[t] for t in self.ids]
        self.topological = sorted(range(self.n), key=lambda i: (self.level_of[i], i))
        self.tail = [0.0] * self.n
        for i in reversed(self.topological):
            self.tail[i] = self.hours[i] + max((self.tail[c] for c in self.children[i]), default=0.0)
        self.urgency = [self.priority[i] * 15 + self.tail[i] for i in range(self.n)]


def critical_path(m):
    es, ef = [0.0] * m.n, [0.0] * m.n
    for i in m.topological:
        es[i] = max((ef[d] for d in m.deps[i]), default=0.0)
        ef[i] = es[i] + m.hours[i]
    length = max(ef) if ef else 0.0
    ls = [0.0] * m.n
    for i in reversed(m.topological):
        lf = min((ls[c] for c in m.children[i]), default=length)
        ls[i] = lf - m.hours[i]
    return length, {i for i in range(m.n) if abs(ls[i] - es[i]) < 1e-9}


@dataclass
class Plan:
    assign: list
    key: list
    cost: float = 0.0


@dataclass
class Evaluation:
    cost: float
    makespan: float
    imbalance: float
    overflow: float
    late: float
    load: list
    utilisation: list


class Evaluator:
    """Decoder (serial schedule generation) and cost function."""

    def __init__(self, model):
        self.p = model
        self.s = model.st

    def decode(self, assign, key):
        p = self.p
        start, end, free = [0.0] * p.n, [0.0] * p.n, [0.0] * p.m
        missing = [len(p.deps[i]) for i in range(p.n)]
        ready = [i for i in range(p.n) if missing[i] == 0]
        while ready:
            task = min(ready, key=key.__getitem__)
            ready.remove(task)
            dev = assign[task]
            ready_time = max((end[d] for d in p.deps[task]), default=0.0)
            begin = max(ready_time, free[dev])
            finish = begin + p.duration[task][dev]
            start[task], end[task], free[dev] = begin, finish, finish
            for child in p.children[task]:
                missing[child] -= 1
                if missing[child] == 0:
                    ready.append(child)
        return start, end

    def evaluate(self, assign, key):
        p, s = self.p, self.s
        _, end = self.decode(assign, key)
        makespan = max(end) if end else 0.0
        load = [0.0] * p.m
        for i in range(p.n):
            load[assign[i]] += p.duration[i][assign[i]]
        util = [load[d] / p.capacity[d] * 100 for d in range(p.m)]
        imbalance = pstdev(util) if p.m > 1 else 0.0
        overflow = sum(max(0.0, load[d] - p.capacity[d]) for d in range(p.m))
        prio = sum(p.priority[i] * end[i] for i in range(p.n)) / max(1, sum(p.priority))
        late = max(0.0, makespan - s.deadline_hours)
        cost = (s.w_makespan * makespan + s.w_priority * prio + s.w_balance * imbalance
                + s.w_overflow * overflow + s.w_late * late)
        return Evaluation(cost, makespan, imbalance, overflow, late, load, util)


# ---------------------------------------------------------------- baseline, A*, GA
def least_loaded_plan(p):
    order = sorted(range(p.n), key=lambda i: (p.level_of[i], i))
    loads, assign = [0.0] * p.m, [None] * p.n
    for i in order:
        dev = min(p.eligible[i], key=lambda d: loads[d] + p.duration[i][d])
        assign[i] = dev
        loads[dev] += p.duration[i][dev]
    return Plan(assign, [p.level_of[i] * 100 + i for i in range(p.n)])


def split_groups(p):
    """BFS levels are cut into groups (mini sprints) of limited work for the A* search."""
    order = sorted(range(p.n), key=lambda i: (p.level_of[i], -p.urgency[i]))
    groups, current, work = [], [], 0.0
    for i in order:
        if current and work + p.hours[i] > p.st.group_limit:
            groups.append(current)
            current, work = [], 0.0
        current.append(i)
        work += p.hours[i]
    if current:
        groups.append(current)
    return groups


def _finish_greedy(p, sequence, k, loads, assign):
    loads, assign = list(loads), list(assign)
    for i in sequence[k:]:
        dev = min(p.eligible[i], key=lambda d: loads[d] + p.duration[i][d])
        assign.append(dev)
        loads[dev] += p.duration[i][dev]
    return assign


def _astar(p, group, base_loads):
    """A* over (assigned tasks, developer loads). g = growth of sum of squared loads (balance)."""
    s = p.st
    seq = sorted(group, key=lambda i: -p.hours[i])
    n, m = len(seq), p.m
    min_dur = [min(p.duration[i][d] for d in p.eligible[i]) for i in seq]

    def heuristic(k, loads):
        marginal, remaining = 0.0, 0.0
        for j in range(k, n):
            i = seq[j]
            remaining += min_dur[j]
            marginal += min(2 * loads[d] * p.duration[i][d] + p.duration[i][d] ** 2 for d in p.eligible[i])
        total = sum(loads) + remaining
        return max(marginal, total * total / m - sum(x * x for x in loads))

    start = tuple(base_loads)
    counter = itertools.count()
    heap = [(s.astar_weight * heuristic(0, start), 0, next(counter), 0.0, 0, start, ())]
    best_g, explored = {}, 0
    while heap:
        _, _, _, g, k, loads, assign = heapq.heappop(heap)
        if k == n:
            return {seq[j]: assign[j] for j in range(n)}
        state = (k, tuple(round(x, 1) for x in loads))
        if state in best_g and best_g[state] <= g + 1e-9:
            continue
        best_g[state] = g
        explored += 1
        if explored > s.astar_node_limit:
            full = _finish_greedy(p, seq, k, loads, assign)
            return {seq[j]: full[j] for j in range(n)}
        i = seq[k]
        for dev in p.eligible[i]:
            dur = p.duration[i][dev]
            if loads[dev] + dur > p.capacity[dev]:
                continue
            nl = list(loads)
            nl[dev] += dur
            nl = tuple(nl)
            ng = g + 2 * loads[dev] * dur + dur * dur
            heapq.heappush(heap, (ng + s.astar_weight * heuristic(k + 1, nl), -(k + 1), next(counter), ng,
                                  k + 1, nl, assign + (dev,)))
    full = _finish_greedy(p, seq, 0, base_loads, [])
    return {seq[j]: full[j] for j in range(n)}


def astar_plan(p, progress=lambda msg: None):
    groups = split_groups(p)
    loads, assign = [0.0] * p.m, [None] * p.n
    for number, group in enumerate(groups, 1):
        progress(f"A* assignment: group {number} of {len(groups)} ...")
        for i, dev in _astar(p, group, loads).items():
            assign[i] = dev
            loads[dev] += p.duration[i][dev]
    key = [0.0] * p.n
    for number, group in enumerate(groups):
        for i in group:
            key[i] = number * 1000 - p.urgency[i]
    return Plan(assign, key)


class GeneticOptimizer:
    """Chromosome = (developer of every task, priority key). Tournament selection, uniform crossover, elitism."""

    def __init__(self, model, evaluator):
        self.p, self.ev, self.s = model, evaluator, model.st

    def _make(self, assign, key):
        return Plan(assign, key, self.ev.evaluate(assign, key).cost)

    def _mutate(self, assign, key, rate):
        assign, key = assign[:], key[:]
        for i in range(self.p.n):
            if random.random() < rate:
                assign[i] = random.choice(self.p.eligible[i])
            if random.random() < rate:
                key[i] += random.gauss(0, 8)
        return assign, key

    def _crossover(self, a, b):
        n = self.p.n
        assign = [a.assign[i] if random.random() < 0.5 else b.assign[i] for i in range(n)]
        if random.random() < 0.3:
            key = [(a.key[i] + b.key[i]) / 2 for i in range(n)]
        else:
            key = [a.key[i] if random.random() < 0.5 else b.key[i] for i in range(n)]
        return assign, key

    def optimise(self, seeds, progress=lambda msg: None):
        p, s = self.p, self.s
        size = s.ga_population
        pop = [self._make(x.assign[:], x.key[:]) for x in seeds]
        while len(pop) < size // 2:
            seed = random.choice(seeds)
            pop.append(self._make(*self._mutate(seed.assign, seed.key, 0.15)))
        while len(pop) < size:
            pop.append(self._make([random.choice(p.eligible[i]) for i in range(p.n)],
                                  [random.uniform(0, p.n) for _ in range(p.n)]))
        best_cost, stale = float("inf"), 0
        for gen in range(s.ga_generations):
            pop.sort(key=lambda x: x.cost)
            if gen % 20 == 0:
                progress(f"Genetic Algorithm: generation {gen} of {s.ga_generations} (best cost {pop[0].cost:.1f})")
            if pop[0].cost < best_cost - 1e-9:
                best_cost, stale = pop[0].cost, 0
            else:
                stale += 1
                if stale >= s.ga_patience:
                    break
            nxt = pop[:s.ga_elite]
            while len(nxt) < size:
                a = min(random.sample(pop, s.ga_tournament), key=lambda x: x.cost)
                b = min(random.sample(pop, s.ga_tournament), key=lambda x: x.cost)
                if random.random() < s.ga_crossover_rate:
                    assign, key = self._crossover(a, b)
                else:
                    assign, key = a.assign, a.key
                nxt.append(self._make(*self._mutate(assign, key, s.ga_mutation_rate)))
            pop = nxt
        pop.sort(key=lambda x: x.cost)
        return pop[0]


def build_plan(tasks, devs, st, start_date, progress=lambda msg: None):
    """Full pipeline. Returns a dictionary with everything the GUI needs."""
    random.seed(st.seed)
    p = Model(tasks, devs, st)
    ev = Evaluator(p)
    progress("Analysing dependencies (BFS levels, critical path) ...")
    cp_length, critical = critical_path(p)
    records = []

    def record(name, plan, seconds):
        records.append((name, plan, ev.evaluate(plan.assign, plan.key), seconds))

    t0 = time.perf_counter()
    least = least_loaded_plan(p)
    record("Least-loaded greedy", least, time.perf_counter() - t0)
    t0 = time.perf_counter()
    astar = astar_plan(p, progress)
    astar_time = time.perf_counter() - t0
    record("A* (sprint-wise)", astar, astar_time)
    t0 = time.perf_counter()
    hybrid = GeneticOptimizer(p, ev).optimise([astar, least], progress)
    record("A* + Genetic Algorithm", hybrid, time.perf_counter() - t0 + astar_time)

    best = min(enumerate(records), key=lambda x: (round(x[1][2].cost, 6), -x[0]))[1]
    name, plan, evaluation, _ = best
    start, end = ev.decode(plan.assign, plan.key)
    sprint_of = [int(start[i] // st.sprint_hours) + 1 for i in range(p.n)]
    return {"model": p, "plan": plan, "evaluation": evaluation, "method": name, "records": records,
            "start": start, "end": end, "sprint_of": sprint_of, "cp_length": cp_length, "critical": critical,
            "start_date": start_date, "settings": st, "sprints": max(sprint_of) if sprint_of else 0}


# =====================================================================
# PART 2: GUI (tkinter)
# =====================================================================
import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

BG, CARD, DARK, MUTED = "#F4F1FF", "#FFFFFF", "#2D3436", "#636E72"
PRIMARY, GREEN, PINK, YELLOW, BLUE, ORANGE, RED = ("#6C5CE7", "#00B894", "#FD79A8", "#FDCB6E", "#0984E3",
                                                   "#E17055", "#D63031")
FONT, FONT_B, FONT_T = ("Segoe UI", 10), ("Segoe UI", 10, "bold"), ("Segoe UI", 13, "bold")
PASTELS = ["#E8F0FF", "#FFF0E6", "#E6FFF4", "#FFE8F1", "#F3E8FF", "#FFFBE0", "#E0FAFF", "#EAFFE0"]
BRIGHTS = ["#74B9FF", "#FAB1A0", "#55EFC4", "#FD79A8", "#A29BFE", "#FFEAA7", "#81ECEC", "#B8E994"]

SAMPLE_TEAM = {
    "Ali": {"skills": {"backend": 1.2, "database": 1.0}, "hours_per_week": 40},
    "Sara": {"skills": {"frontend": 1.2, "design": 1.0}, "hours_per_week": 40},
    "Usman": {"skills": {"devops": 1.2, "backend": 1.0, "security": 0.9}, "hours_per_week": 40},
    "Hina": {"skills": {"qa": 1.2, "analysis": 1.0}, "hours_per_week": 40},
    "Bilal": {"skills": {"mobile": 1.1, "frontend": 1.0, "backend": 0.9}, "hours_per_week": 40},
    "Ayesha": {"skills": {"database": 1.2, "data": 1.1, "analysis": 1.0}, "hours_per_week": 40},
    "Zain": {"skills": {"security": 1.2, "qa": 1.0, "devops": 0.9}, "hours_per_week": 40},
    "Maryam": {"skills": {"design": 1.2, "frontend": 1.0, "analysis": 1.1}, "hours_per_week": 40},
}
EXAMPLE_TEXT = ("Smart campus management system with student registration and login, course enrollment, "
                "attendance tracking, fee payment, library management, exam results, admin dashboard, "
                "email notifications, reports and analytics, and a mobile app for students.")


def lerp_color(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def shade(color, amount):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(color[i:i + 2], 16) + amount)) for i in (1, 3, 5))


class FlatButton(tk.Label):
    """Colorful flat button that looks the same on Windows, macOS and Linux."""

    def __init__(self, parent, text, command, color=PRIMARY, **kw):
        super().__init__(parent, text=text, bg=color, fg="white", font=FONT_B, padx=16, pady=8, cursor="hand2", **kw)
        self.color, self.command, self.enabled = color, command, True
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=shade(self.color, -22)))
        self.bind("<Leave>", lambda e: self.enabled and self.config(bg=self.color))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, value):
        self.enabled = value
        self.config(bg=self.color if value else "#B2BEC3", cursor="hand2" if value else "arrow")


def make_tree(parent, columns, height=12):
    """columns: (key, title, width, anchor, stretch)"""
    frame = tk.Frame(parent, bg=CARD)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=height)
    for key, title, width, anchor, stretch in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, anchor=anchor, stretch=stretch)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return frame, tree


def card(parent, **kw):
    return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground="#DCD6F7", **kw)


class Store:
    """Developers are saved once and loaded automatically next time."""

    def __init__(self):
        self.path = Path.home() / ".sprintly" / "team.json"

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {n: {"skills": {k.lower(): float(v) for k, v in d["skills"].items()},
                        "hours_per_week": float(d.get("hours_per_week", 40))} for n, d in data.items()}
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            return {}

    def save(self, devs):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(devs, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------- task dialog
class TaskDialog(tk.Toplevel):
    def __init__(self, app, task=None):
        super().__init__(app)
        self.app, self.task, self.result = app, task, None
        self.title("Edit task" if task else "Add task")
        self.configure(bg=BG, padx=20, pady=16)
        self.transient(app)
        self.resizable(False, False)
        t = task or {"phase": "Core Features", "name": "", "hours": 4, "skill": "backend", "priority": 3, "deps": []}

        self.v_name = tk.StringVar(value=t["name"])
        self.v_phase = tk.StringVar(value=t["phase"])
        self.v_hours = tk.StringVar(value=f"{t['hours']:g}")
        self.v_prio = tk.StringVar(value=str(t["priority"]))
        self.v_skill = tk.StringVar(value=t["skill"])
        phases = list(dict.fromkeys(x["phase"] for x in app.tasks)) or ["Core Features"]
        skills = sorted(set(STD_SKILLS) | {s for d in app.devs.values() for s in d["skills"]} | {t["skill"]})

        def label(r, text):
            tk.Label(self, text=text, bg=BG, fg=DARK, font=FONT_B).grid(row=r, column=0, sticky="w", pady=5, padx=(0, 12))

        label(0, "Task name")
        ttk.Entry(self, textvariable=self.v_name, width=48).grid(row=0, column=1, sticky="we")
        label(1, "Phase")
        ttk.Combobox(self, textvariable=self.v_phase, values=phases, width=45).grid(row=1, column=1, sticky="we")
        label(2, "Estimated hours")
        tk.Spinbox(self, textvariable=self.v_hours, from_=0.5, to=500, increment=0.5, width=8, font=FONT) \
            .grid(row=2, column=1, sticky="w")
        label(3, "Priority (1 low - 5 urgent)")
        tk.Spinbox(self, textvariable=self.v_prio, from_=1, to=5, width=8, font=FONT).grid(row=3, column=1, sticky="w")
        label(4, "Required skill")
        ttk.Combobox(self, textvariable=self.v_skill, values=skills, width=22).grid(row=4, column=1, sticky="w")
        label(5, "Depends on")
        box = tk.Frame(self, bg=BG)
        box.grid(row=5, column=1, sticky="we")
        self.lb = tk.Listbox(box, selectmode="multiple", height=10, width=52, exportselection=False, font=FONT,
                             selectbackground=PRIMARY, selectforeground="white")
        sb = ttk.Scrollbar(box, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left")
        sb.pack(side="left", fill="y")
        self.dep_ids = []
        own = task["id"] if task else None
        for x in sorted(app.tasks, key=lambda z: natkey(z["id"])):
            if x["id"] == own:
                continue
            self.lb.insert("end", f"{x['id']}  {x['name']}")
            self.dep_ids.append(x["id"])
            if task and x["id"] in task["deps"]:
                self.lb.selection_set("end")
        row = tk.Frame(self, bg=BG)
        row.grid(row=6, column=0, columnspan=2, pady=(14, 0), sticky="e")
        FlatButton(row, "Cancel", self.destroy, MUTED).pack(side="right", padx=(8, 0))
        FlatButton(row, "Save task", self._save, GREEN).pack(side="right")
        try:
            self.grab_set()
        except tk.TclError:
            pass
        app.wait_window(self)

    def _save(self):
        name = self.v_name.get().strip()
        skill = self.v_skill.get().strip().lower().replace(" ", "-")
        try:
            hours = float(self.v_hours.get())
            prio = int(float(self.v_prio.get()))
            if hours <= 0 or not 1 <= prio <= 5:
                raise ValueError
        except ValueError:
            messagebox.showerror("Sprintly", "Hours must be greater than 0 and priority must be 1 to 5.", parent=self)
            return
        if not name or not skill:
            messagebox.showerror("Sprintly", "Task name and skill are required.", parent=self)
            return
        deps = [self.dep_ids[i] for i in self.lb.curselection()]
        tid = self.task["id"] if self.task else self.app.next_id()
        trial = {t["id"]: list(t["deps"]) for t in self.app.tasks if t["id"] != tid}
        trial[tid] = deps
        cycle = Graph(trial).find_cycle()
        if cycle:
            messagebox.showerror("Sprintly", "This creates a circular dependency:\n" + " -> ".join(cycle), parent=self)
            return
        self.result = {"id": tid, "phase": self.v_phase.get().strip() or "General", "name": name, "hours": hours,
                       "skill": skill, "priority": prio, "deps": deps}
        self.destroy()


# ---------------------------------------------------------------- main application
class SprintlyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        sys.setrecursionlimit(5000)
        self.title("Sprintly - AI Sprint Planner")
        self.geometry("1320x800")
        self.minsize(1100, 680)
        self.configure(bg=BG)
        self.store = Store()
        self.devs = self.store.load()
        self.tasks = []
        self.result = None
        self.working = False
        self._deadline_auto = True
        self._style()
        self._header()
        self.status = tk.StringVar(value="Welcome to Sprintly! Start by describing your project.")
        tk.Label(self, textvariable=self.status, bg=DARK, fg="white", anchor="w", font=FONT, padx=14, pady=6) \
            .pack(side="bottom", fill="x")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(10, 8))
        self._tab_project()
        self._tab_team()
        self._tab_tasks()
        self._tab_plan()
        self._tab_gantt()
        self.refresh_team()
        self._update_info()
        if not self.devs:
            self.status.set("Tip: add your developers in the Team tab first (they are saved for next time).")

    # ----- look and feel
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", font=FONT, background=BG)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(20, 9), font=FONT_B, background="#DDD6FE", foreground=DARK)
        st.map("TNotebook.Tab", background=[("selected", PRIMARY)], foreground=[("selected", "white")])
        st.configure("Treeview", rowheight=28, font=FONT, background="white", fieldbackground="white")
        st.configure("Treeview.Heading", font=FONT_B, background=PRIMARY, foreground="white", relief="flat",
                     padding=6)
        st.map("Treeview.Heading", background=[("active", shade(PRIMARY, -20))])
        st.map("Treeview", background=[("selected", PRIMARY)], foreground=[("selected", "white")])

    def _header(self):
        canvas = tk.Canvas(self, height=72, highlightthickness=0, bd=0)
        canvas.pack(fill="x")

        def draw(event=None):
            canvas.delete("all")
            w = max(canvas.winfo_width(), 800)
            for x in range(0, w, 4):
                t = x / w
                canvas.create_rectangle(x, 0, x + 4, 72, outline="", fill=lerp_color(PRIMARY, PINK, t))
            canvas.create_text(24, 30, text="⚡ Sprintly", anchor="w", fill="white", font=("Segoe UI", 22, "bold"))
            canvas.create_text(26, 56, anchor="w", fill="#F1ECFF", font=("Segoe UI", 10),
                               text="Describe your project  →  tasks  →  smart developer assignment  →  sprint plan")

        canvas.bind("<Configure>", draw)

    def busy(self, message):
        self.status.set(message)
        self.update_idletasks()

    # ----- tab 1: project
    def _tab_project(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="🏠  Project")
        box = card(tab, padx=26, pady=20)
        box.pack(fill="both", expand=True, padx=8, pady=10)
        box.columnconfigure(1, weight=1)
        tk.Label(box, text="Tell Sprintly about your project", bg=CARD, fg=PRIMARY, font=FONT_T) \
            .grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.v_name = tk.StringVar()
        self.v_weeks = tk.StringVar(value="8")
        self.v_deadline = tk.StringVar(value=(date.today() + timedelta(weeks=8)).isoformat())
        self.v_sprint = tk.StringVar(value="1")

        def label(r, text, hint=""):
            tk.Label(box, text=text, bg=CARD, fg=DARK, font=FONT_B).grid(row=r, column=0, sticky="nw", pady=8,
                                                                         padx=(0, 18))
            if hint:
                tk.Label(box, text=hint, bg=CARD, fg=MUTED, font=("Segoe UI", 9)).grid(row=r, column=1, sticky="e")

        label(1, "Project name")
        ttk.Entry(box, textvariable=self.v_name, font=FONT).grid(row=1, column=1, sticky="we", pady=6)
        label(2, "Description")
        self.t_desc = tk.Text(box, height=9, font=FONT, wrap="word", relief="solid", bd=1, padx=8, pady=6)
        self.t_desc.grid(row=2, column=1, sticky="we", pady=6)
        tk.Label(box, text="Tip: list the features separated by commas, e.g. login, payments, admin dashboard, "
                           "mobile app, reports ...", bg=CARD, fg=MUTED, font=("Segoe UI", 9)) \
            .grid(row=3, column=1, sticky="w")

        row = tk.Frame(box, bg=CARD)
        row.grid(row=4, column=0, columnspan=2, sticky="we", pady=(14, 4))
        tk.Label(row, text="Expected timeline (weeks)", bg=CARD, fg=DARK, font=FONT_B).pack(side="left")
        tk.Spinbox(row, textvariable=self.v_weeks, from_=1, to=104, width=6, font=FONT, command=self._weeks_changed) \
            .pack(side="left", padx=(8, 28))
        tk.Label(row, text="Deadline (YYYY-MM-DD)", bg=CARD, fg=DARK, font=FONT_B).pack(side="left")
        entry = ttk.Entry(row, textvariable=self.v_deadline, width=14, font=FONT)
        entry.pack(side="left", padx=(8, 28))
        entry.bind("<KeyRelease>", self._deadline_typed)
        tk.Label(row, text="Sprint length (weeks)", bg=CARD, fg=DARK, font=FONT_B).pack(side="left")
        ttk.Combobox(row, textvariable=self.v_sprint, values=["1", "2", "3"], width=4, state="readonly") \
            .pack(side="left", padx=8)
        self.v_weeks.trace_add("write", lambda *a: self._weeks_changed())
        self.v_deadline.trace_add("write", lambda *a: self._update_info())

        self.info = tk.Label(box, text="", bg="#F3F0FF", fg=DARK, font=FONT, anchor="w", justify="left", padx=12, pady=8)
        self.info.grid(row=5, column=0, columnspan=2, sticky="we", pady=(10, 14))

        buttons = tk.Frame(box, bg=CARD)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w")
        FlatButton(buttons, "✨  Analyze and generate tasks", self.generate, PRIMARY).pack(side="left")
        FlatButton(buttons, "🚀  Build sprint plan", self.build, GREEN).pack(side="left", padx=10)
        FlatButton(buttons, "📄  Load example", self.load_example, BLUE).pack(side="left")

    def load_example(self):
        self.v_name.set("Smart Campus")
        self.t_desc.delete("1.0", "end")
        self.t_desc.insert("1.0", EXAMPLE_TEXT)
        self.v_weeks.set("12")

    def _weeks_changed(self):
        if self._deadline_auto:
            try:
                weeks = int(self.v_weeks.get())
                self.v_deadline.set((date.today() + timedelta(weeks=weeks)).isoformat())
            except ValueError:
                pass
        self._update_info()

    def _deadline_typed(self, event=None):
        self._deadline_auto = False

    def _targets(self):
        """Returns (weeks, deadline_date, target_hours) or raises ValueError with a readable message."""
        try:
            weeks = int(self.v_weeks.get())
            if not 1 <= weeks <= 104:
                raise ValueError
        except ValueError:
            raise ValueError("Timeline must be a whole number of weeks (1 to 104).")
        try:
            deadline = datetime.strptime(self.v_deadline.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Deadline must be a date in the format YYYY-MM-DD, for example 2026-12-31.")
        if deadline <= date.today():
            raise ValueError("The deadline must be a future date.")
        deadline_hours = count_workdays(date.today(), deadline) * 8
        return weeks, deadline, min(deadline_hours, weeks * 40.0)

    def _update_info(self):
        try:
            weeks, deadline, target = self._targets()
            self.info.config(text=f"📅  {count_workdays(date.today(), deadline)} working days until the deadline,  "
                                  f"timeline = {weeks} weeks.   Planner target: finish within {target:g} working hours "
                                  f"(about {target / 40:.1f} weeks).", fg=DARK)
        except ValueError as error:
            self.info.config(text="⚠  " + str(error), fg=RED)

    # ----- tab 2: team
    def _tab_team(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="👥  Team")
        left = card(tab, padx=10, pady=10)
        left.pack(side="left", fill="both", expand=True, padx=(8, 6), pady=10)
        frame, self.team_tree = make_tree(left, [("name", "Developer", 150, "w", False),
                                                 ("skills", "Skills (proficiency)", 380, "w", True),
                                                 ("hpw", "Hours/week", 90, "center", False)], 16)
        frame.pack(fill="both", expand=True)
        self.team_tree.bind("<<TreeviewSelect>>", self._team_select)

        right = card(tab, padx=20, pady=16)
        right.pack(side="right", fill="y", padx=(6, 8), pady=10)
        tk.Label(right, text="Add / edit developer", bg=CARD, fg=PRIMARY, font=FONT_T).pack(anchor="w", pady=(0, 10))
        self.v_dname, self.v_dskills, self.v_dhpw = tk.StringVar(), tk.StringVar(), tk.StringVar(value="40")
        for text, var in (("Name", self.v_dname), ("Skills", self.v_dskills), ("Available hours per week", self.v_dhpw)):
            tk.Label(right, text=text, bg=CARD, fg=DARK, font=FONT_B).pack(anchor="w", pady=(8, 2))
            ttk.Entry(right, textvariable=var, width=34, font=FONT).pack(anchor="w")
        tk.Label(right, text="Format:  backend:1.2, qa, database:0.9\n1.0 = normal, 1.2 = 20% faster, 0.8 = 25% slower\n"
                             "Common skills: " + ", ".join(STD_SKILLS), bg=CARD, fg=MUTED, justify="left",
                 wraplength=270, font=("Segoe UI", 9)).pack(anchor="w", pady=10)
        for text, cmd, color in (("💾  Save developer", self.save_dev, GREEN), ("🗑  Delete selected", self.delete_dev, RED),
                                 ("🧹  Clear form", self.clear_dev_form, MUTED),
                                 ("🎲  Load sample team", self.load_sample_team, BLUE)):
            FlatButton(right, text, cmd, color).pack(fill="x", pady=4)
        self.team_note = tk.Label(right, text="", bg=CARD, fg=GREEN, font=FONT_B)
        self.team_note.pack(anchor="w", pady=6)

    def refresh_team(self):
        self.team_tree.delete(*self.team_tree.get_children())
        for name, d in self.devs.items():
            self.team_tree.insert("", "end", iid=name, values=(name, format_skills(d["skills"]), f"{d['hours_per_week']:g}"))

    def _team_select(self, event=None):
        sel = self.team_tree.selection()
        if sel:
            d = self.devs[sel[0]]
            self.v_dname.set(sel[0])
            self.v_dskills.set(format_skills(d["skills"]))
            self.v_dhpw.set(f"{d['hours_per_week']:g}")

    def clear_dev_form(self):
        self.v_dname.set("")
        self.v_dskills.set("")
        self.v_dhpw.set("40")
        self.team_tree.selection_remove(self.team_tree.selection())

    def _persist_team(self):
        ok = self.store.save(self.devs)
        self.team_note.config(text="Saved ✓" if ok else "Could not save the team file!", fg=GREEN if ok else RED)
        self.refresh_team()

    def save_dev(self):
        name = self.v_dname.get().strip()
        skills = parse_skills(self.v_dskills.get())
        try:
            hpw = float(self.v_dhpw.get())
            if not 1 <= hpw <= 80:
                raise ValueError
        except ValueError:
            messagebox.showerror("Sprintly", "Hours per week must be a number between 1 and 80.")
            return
        if not name or not skills:
            messagebox.showerror("Sprintly", "Please enter a name and skills, for example:  backend:1.2, qa")
            return
        selected = self.team_tree.selection()
        if selected and selected[0] != name:
            self.devs.pop(selected[0], None)  # rename
        self.devs[name] = {"skills": skills, "hours_per_week": hpw}
        self._persist_team()
        self.clear_dev_form()

    def delete_dev(self):
        sel = self.team_tree.selection()
        if sel and messagebox.askyesno("Sprintly", f"Delete developer '{sel[0]}'?"):
            self.devs.pop(sel[0], None)
            self._persist_team()
            self.clear_dev_form()

    def load_sample_team(self):
        if self.devs and not messagebox.askyesno("Sprintly", "Add the sample developers to your team?"):
            return
        for name, d in SAMPLE_TEAM.items():
            self.devs.setdefault(name, {"skills": dict(d["skills"]), "hours_per_week": d["hours_per_week"]})
        self._persist_team()

    # ----- tab 3: tasks
    def _tab_tasks(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="📋  Tasks")
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", padx=8, pady=(10, 4))
        for text, cmd, color in (("➕  Add", self.add_task, GREEN), ("✏️  Edit", self.edit_task, BLUE),
                                 ("🗑  Delete", self.delete_task, RED), ("🚀  Build sprint plan", self.build, PRIMARY)):
            FlatButton(bar, text, cmd, color).pack(side="left", padx=(0, 8))
        self.task_summary = tk.Label(bar, text="", bg=BG, fg=DARK, font=FONT_B)
        self.task_summary.pack(side="right")
        holder = card(tab, padx=8, pady=8)
        holder.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        frame, self.task_tree = make_tree(holder, [("id", "ID", 60, "center", False), ("phase", "Phase", 190, "w", False),
                                                   ("name", "Task", 330, "w", True), ("hours", "Hours", 65, "center", False),
                                                   ("prio", "Priority", 100, "w", False), ("skill", "Skill", 90, "center", False),
                                                   ("deps", "Depends on", 260, "w", True)], 18)
        frame.pack(fill="both", expand=True)
        self.task_tree.bind("<Double-1>", lambda e: self.edit_task())

    def refresh_tasks(self):
        tree = self.task_tree
        tree.delete(*tree.get_children())
        phases = list(dict.fromkeys(t["phase"] for t in self.tasks))
        for k, ph in enumerate(phases):
            tree.tag_configure(f"p{k}", background=PASTELS[k % len(PASTELS)])
        for t in sorted(self.tasks, key=lambda x: natkey(x["id"])):
            tree.insert("", "end", iid=t["id"], tags=(f"p{phases.index(t['phase'])}",),
                        values=(t["id"], t["phase"], t["name"], f"{t['hours']:g}", f"{t['priority']}  " + "★" * t["priority"],
                                t["skill"], ", ".join(t["deps"]) or "-"))
        total = sum(t["hours"] for t in self.tasks)
        self.task_summary.config(text=f"{len(self.tasks)} tasks  |  {len(phases)} phases  |  {total:g} base hours")

    def next_id(self):
        return f"T{max([natkey(t['id']) for t in self.tasks], default=0) + 1:02d}"

    def add_task(self):
        dialog = TaskDialog(self)
        if dialog.result:
            self.tasks.append(dialog.result)
            self.refresh_tasks()
            self.status.set(f"Task {dialog.result['id']} added. Rebuild the plan to update the schedule.")

    def edit_task(self):
        sel = self.task_tree.selection()
        if not sel:
            messagebox.showinfo("Sprintly", "Select a task first.")
            return
        task = next(t for t in self.tasks if t["id"] == sel[0])
        dialog = TaskDialog(self, task)
        if dialog.result:
            self.tasks[self.tasks.index(task)] = dialog.result
            self.refresh_tasks()
            self.status.set(f"Task {task['id']} updated. Rebuild the plan to update the schedule.")

    def delete_task(self):
        sel = self.task_tree.selection()
        if not sel:
            messagebox.showinfo("Sprintly", "Select a task first.")
            return
        tid = sel[0]
        users = [t["id"] for t in self.tasks if tid in t["deps"]]
        note = f"\n\n{', '.join(users)} depend on it; the dependency will be removed." if users else ""
        if messagebox.askyesno("Sprintly", f"Delete task {tid}?{note}"):
            self.tasks = [t for t in self.tasks if t["id"] != tid]
            for t in self.tasks:
                t["deps"] = [d for d in t["deps"] if d != tid]
            self.refresh_tasks()

    # ----- tab 4: plan
    def _tab_plan(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="🗓  Sprint plan")
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", padx=8, pady=(10, 4))
        FlatButton(bar, "🚀  Rebuild plan", self.build, PRIMARY).pack(side="left")
        FlatButton(bar, "💾  Export CSV", self.export_csv, GREEN).pack(side="left", padx=8)
        tk.Label(bar, text="Show:", bg=BG, fg=DARK, font=FONT_B).pack(side="left", padx=(18, 6))
        self.v_filter = tk.StringVar(value="All sprints")
        self.filter_box = ttk.Combobox(bar, textvariable=self.v_filter, values=["All sprints"], state="readonly", width=34)
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_plan_tree())

        paned = tk.PanedWindow(tab, orient="horizontal", bg=BG, sashwidth=6, bd=0)
        paned.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        left = card(paned, padx=6, pady=6)
        frame, self.plan_tree = make_tree(left, [("sp", "Sprint", 55, "center", False), ("id", "ID", 50, "center", False),
                                                 ("name", "Task", 230, "w", True), ("dev", "Developer", 80, "w", False),
                                                 ("start", "Start", 90, "center", False), ("end", "End", 90, "center", False),
                                                 ("hours", "Hours", 55, "center", False), ("pr", "P", 28, "center", False),
                                                 ("cp", "CP", 32, "center", False)], 20)
        frame.pack(fill="both", expand=True)
        paned.add(left, minsize=520, stretch="always")
        right = tk.Frame(paned, bg=BG)
        paned.add(right, minsize=330)
        box = card(right, padx=6, pady=6)
        box.pack(fill="both", expand=True)
        self.summary = tk.Text(box, width=44, height=20, font=("Segoe UI", 10), wrap="word", relief="flat", bd=0,
                               padx=10, pady=8, state="disabled", bg=CARD)
        self.summary.pack(fill="both", expand=True)
        for tag, kw in (("h", dict(foreground=PRIMARY, font=("Segoe UI", 12, "bold"))), ("good", dict(foreground=GREEN, font=FONT_B)),
                        ("bad", dict(foreground=RED, font=FONT_B)), ("b", dict(font=FONT_B)), ("m", dict(foreground=MUTED))):
            self.summary.tag_configure(tag, **kw)
        work = card(right, padx=6, pady=6)
        work.pack(fill="both", expand=True, pady=(8, 0))
        self.load_canvas = tk.Canvas(work, bg=CARD, highlightthickness=0, height=230)
        self.load_canvas.pack(fill="both", expand=True)
        self.load_canvas.bind("<Configure>", lambda e: self.draw_load())

    def _write_summary(self, parts):
        s = self.summary
        s.config(state="normal")
        s.delete("1.0", "end")
        for text, tag in parts:
            s.insert("end", text, tag)
        s.config(state="disabled")

    def sprint_dates(self, r, sprint):
        st = r["settings"]
        a = add_workdays(r["start_date"], (sprint - 1) * st.sprint_weeks * 5)
        b = add_workdays(a, st.sprint_weeks * 5 - 1)
        return a, b

    def refresh_plan(self):
        r = self.result
        if not r:
            return
        values = ["All sprints"] + [f"Sprint {k}  ({self.sprint_dates(r, k)[0]:%d %b} - {self.sprint_dates(r, k)[1]:%d %b})"
                                    for k in range(1, r["sprints"] + 1)]
        self.filter_box.config(values=values)
        self.v_filter.set("All sprints")
        self.refresh_plan_tree()
        self.refresh_summary()
        self.draw_load()
        self.draw_gantt()

    def refresh_plan_tree(self):
        r = self.result
        tree = self.plan_tree
        tree.delete(*tree.get_children())
        if not r:
            return
        p, st = r["model"], r["settings"]
        for k in range(8):
            tree.tag_configure(f"s{k}", background=PASTELS[k])
        wanted = self.v_filter.get()
        sel = int(wanted.split()[1]) if wanted.startswith("Sprint") else None
        for i in sorted(range(p.n), key=lambda i: (r["start"][i], p.ids[i])):
            if sel and r["sprint_of"][i] != sel:
                continue
            dev = r["plan"].assign[i]
            tree.insert("", "end", tags=(f"s{(r['sprint_of'][i] - 1) % 8}",), values=(
                r["sprint_of"][i], p.ids[i], p.tasks[i]["name"], p.dev_names[dev],
                f"{hours_to_date(r['start_date'], r['start'][i], st.hours_per_day):%a %d %b}",
                f"{hours_to_date(r['start_date'], r['end'][i], st.hours_per_day, True):%a %d %b}",
                f"{p.duration[i][dev]:.1f}", p.priority[i], "●" if i in r["critical"] else ""))

    def refresh_summary(self):
        r = self.result
        p, e, st = r["model"], r["evaluation"], r["settings"]
        finish = hours_to_date(r["start_date"], e.makespan, st.hours_per_day, True)
        parts = [(f"{self.v_name.get().strip() or 'Project'}\n", "h"),
                 (f"Best method: {r['method']}\n\n", "m"),
                 ("Finish date: ", "b"), (f"{finish:%A, %d %B %Y}\n", ""),
                 ("Duration: ", "b"), (f"{e.makespan:.0f} working hours ({math.ceil(e.makespan / st.hours_per_day)} days)\n", ""),
                 ("Target: ", "b"), (f"{st.deadline_hours:g} hours  |  Sprints: {r['sprints']}\n", "")]
        if e.late <= 0:
            parts.append((f"✔ Deadline MET with {st.deadline_hours - e.makespan:.0f} hours to spare\n", "good"))
        else:
            parts.append((f"✖ Deadline MISSED by {e.late:.0f} hours - add developers, cut tasks or extend the deadline\n", "bad"))
        parts += [("Critical path: ", "b"), (f"{r['cp_length']:.0f} h even with unlimited developers\n", "")]
        over = [p.dev_names[d] for d in range(p.m) if e.load[d] > p.capacity[d] + 1e-9]
        parts.append(("Capacity: ", "b"))
        parts.append((("over capacity: " + ", ".join(over) + "\n") if over else "nobody is over capacity\n", "bad" if over else ""))
        if p.fallback:
            parts.append((f"⚠ Nobody has the skill for {', '.join(p.fallback)}; the work was given to any developer at "
                          f"{FALLBACK_PROFICIENCY:g}x speed. Add a developer with that skill.\n", "bad"))
        parts.append(("\nAlgorithm comparison\n", "b"))
        for name, _, ev, secs in r["records"]:
            parts.append((f"• {name}: {ev.makespan:.0f}h, imbalance {ev.imbalance:.1f}, cost {ev.cost:.0f}  ({secs:.1f}s)\n", "m"))
        parts.append(("\nCP ● = task on the critical path (any delay delays the project).\n", "m"))
        self._write_summary(parts)

    def draw_load(self):
        c = self.load_canvas
        c.delete("all")
        r = self.result
        if not r:
            c.create_text(20, 20, anchor="w", fill=MUTED, font=FONT, text="Developer workload will appear here.")
            return
        p, e = r["model"], r["evaluation"]
        c.create_text(10, 12, anchor="w", fill=PRIMARY, font=("Segoe UI", 12, "bold"), text="Developer workload")
        w = max(c.winfo_width(), 300)
        left, right, top, row = 80, 70, 34, max(18, min(26, (c.winfo_height() - 40) // max(1, p.m)))
        for d in range(p.m):
            y = top + d * row
            util = e.utilisation[d]
            color = GREEN if util <= 80 else (YELLOW if util <= 100 else RED)
            c.create_text(8, y + row / 2 - 2, anchor="w", text=p.dev_names[d], font=("Segoe UI", 9), fill=DARK)
            c.create_rectangle(left, y + 3, w - right, y + row - 5, fill="#EEE9FF", outline="")
            c.create_rectangle(left, y + 3, left + (w - right - left) * min(util, 100) / 100, y + row - 5, fill=color, outline="")
            c.create_text(w - right + 6, y + row / 2 - 2, anchor="w", text=f"{e.load[d]:.0f}h {util:.0f}%", font=("Segoe UI", 9), fill=DARK)

    # ----- tab 5: gantt
    def _tab_gantt(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="📊  Gantt")
        holder = card(tab, padx=4, pady=4)
        holder.pack(fill="both", expand=True, padx=8, pady=10)
        self.gantt = tk.Canvas(holder, bg=CARD, highlightthickness=0)
        hs = ttk.Scrollbar(holder, orient="horizontal", command=self.gantt.xview)
        vs = ttk.Scrollbar(holder, orient="vertical", command=self.gantt.yview)
        self.gantt.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.gantt.pack(fill="both", expand=True)
        self.draw_gantt()

    def draw_gantt(self):
        c = self.gantt
        c.delete("all")
        r = self.result
        if not r:
            c.create_text(20, 24, anchor="w", fill=MUTED, font=FONT, text="Build a sprint plan to see the Gantt chart.")
            return
        p, st = r["model"], r["settings"]
        px, left, top, row = 7.0, 100, 46, 40
        end_h = max(max(r["end"]), st.deadline_hours)
        c.configure(scrollregion=(0, 0, left + end_h * px + 80, top + p.m * row + 30))
        for k in range(r["sprints"] + 1):
            x = left + k * st.sprint_hours * px
            c.create_line(x, top - 8, x, top + p.m * row, fill="#DCD6F7")
            if k < r["sprints"]:
                a, b = self.sprint_dates(r, k + 1)
                c.create_text(x + 6, 12, anchor="w", text=f"Sprint {k + 1}", font=FONT_B, fill=PRIMARY)
                c.create_text(x + 6, 30, anchor="w", text=f"{a:%d %b} - {b:%d %b}", font=("Segoe UI", 8), fill=MUTED)
        for d in range(p.m):
            y = top + d * row
            c.create_text(10, y + row / 2, anchor="w", text=p.dev_names[d], font=FONT_B, fill=DARK)
            c.create_line(left, y + row, left + end_h * px + 40, y + row, fill="#F0ECFF")
        for i in range(p.n):
            d = r["plan"].assign[i]
            x1, x2 = left + r["start"][i] * px, left + r["end"][i] * px
            y = top + d * row
            color = BRIGHTS[(r["sprint_of"][i] - 1) % len(BRIGHTS)]
            c.create_rectangle(x1, y + 5, x2, y + row - 7, fill=color, outline=PRIMARY if i in r["critical"] else "white",
                               width=2 if i in r["critical"] else 1)
            if x2 - x1 > 24:
                c.create_text((x1 + x2) / 2, y + row / 2 - 1, text=p.ids[i], font=("Segoe UI", 8, "bold"), fill=DARK)
        x = left + st.deadline_hours * px
        c.create_line(x, top - 8, x, top + p.m * row, fill=RED, dash=(5, 3), width=2)
        c.create_text(x + 4, top + p.m * row + 14, anchor="w", text="Deadline", fill=RED, font=FONT_B)

    # ----- actions
    def generate(self):
        try:
            self._targets()
        except ValueError as error:
            messagebox.showerror("Sprintly", str(error))
            return
        name, desc = self.v_name.get().strip(), self.t_desc.get("1.0", "end").strip()
        if not name:
            messagebox.showerror("Sprintly", "Please enter a project name.")
            return
        if len(desc.split()) < 4:
            messagebox.showerror("Sprintly", "Please describe the project in a few words or sentences.")
            return
        if self.tasks and not messagebox.askyesno("Sprintly", "This replaces the current tasks (including your edits). Continue?"):
            return
        self.tasks = generate_tasks(name, desc)
        self.result = None
        self.refresh_tasks()
        self.refresh_plan_tree()
        self.draw_load()
        self.draw_gantt()
        phases = len({t["phase"] for t in self.tasks})
        self.status.set(f"Generated {len(self.tasks)} tasks in {phases} phases. Review or edit them, then build the plan.")
        self.nb.select(2)

    def build(self):
        if self.working:
            return
        try:
            weeks, deadline, target = self._targets()
        except ValueError as error:
            messagebox.showerror("Sprintly", str(error))
            self.nb.select(0)
            return
        if not self.tasks:
            messagebox.showinfo("Sprintly", "There are no tasks yet. Click 'Analyze and generate tasks' first.")
            self.nb.select(0)
            return
        if not self.devs:
            messagebox.showinfo("Sprintly", "Please add at least one developer in the Team tab (or load the sample team).")
            self.nb.select(1)
            return
        cycle = Graph({t["id"]: [d for d in t["deps"] if d in {x["id"] for x in self.tasks}] for t in self.tasks}).find_cycle()
        if cycle:
            messagebox.showerror("Sprintly", "Circular dependency found:\n" + " -> ".join(cycle))
            return
        st = Settings(deadline_hours=target, sprint_weeks=int(self.v_sprint.get()))
        tasks = [dict(t, deps=list(t["deps"])) for t in self.tasks]
        devs = {n: {"skills": dict(d["skills"]), "hours_per_week": d["hours_per_week"]} for n, d in self.devs.items()}
        self.working = True
        messages = queue.Queue()

        def work():
            try:
                messages.put(("done", build_plan(tasks, devs, st, date.today(), lambda m: messages.put(("msg", m)))))
            except Exception as error:  # shown to the user, not swallowed
                messages.put(("err", f"{type(error).__name__}: {error}"))

        threading.Thread(target=work, daemon=True).start()
        self.status.set("Building the plan ...")
        self.after(100, lambda: self._poll(messages))

    def _poll(self, messages):
        try:
            while True:
                kind, payload = messages.get_nowait()
                if kind == "msg":
                    self.status.set("⏳ " + payload)
                elif kind == "err":
                    self.working = False
                    self.status.set("Planning failed.")
                    messagebox.showerror("Sprintly", "The plan could not be built:\n" + payload)
                    return
                else:
                    self.working = False
                    self.result = payload
                    self.refresh_plan()
                    e = payload["evaluation"]
                    self.status.set(("✔ Plan ready - deadline met." if e.late <= 0 else "⚠ Plan ready - the deadline is missed.")
                                    + f"  {payload['model'].n} tasks in {payload['sprints']} sprints.")
                    self.nb.select(3)
                    return
        except queue.Empty:
            pass
        self.after(100, lambda: self._poll(messages))

    def export_csv(self):
        r = self.result
        if not r:
            messagebox.showinfo("Sprintly", "Build a sprint plan first.")
            return
        base = re.sub(r"[^\w\-]+", "_", self.v_name.get().strip() or "project")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"sprintly_{base}_plan.csv",
                                            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        p, st = r["model"], r["settings"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                w = csv.writer(handle)
                w.writerow(["Sprint", "TaskID", "Phase", "Task", "Skill", "Priority", "Developer", "StartDate", "EndDate",
                            "StartHour", "EndHour", "HoursForDeveloper", "CriticalPath", "DependsOn"])
                for i in sorted(range(p.n), key=lambda i: (r["start"][i], p.ids[i])):
                    dev = r["plan"].assign[i]
                    t = p.tasks[i]
                    w.writerow([r["sprint_of"][i], t["id"], t["phase"], t["name"], t["skill"], t["priority"], p.dev_names[dev],
                                hours_to_date(r["start_date"], r["start"][i], st.hours_per_day).isoformat(),
                                hours_to_date(r["start_date"], r["end"][i], st.hours_per_day, True).isoformat(),
                                round(r["start"][i], 1), round(r["end"][i], 1), round(p.duration[i][dev], 1),
                                "yes" if i in r["critical"] else "", " ".join(t["deps"])])
            self.status.set(f"Plan exported to {path}")
            messagebox.showinfo("Sprintly", f"Plan saved to:\n{path}")
        except OSError as error:
            messagebox.showerror("Sprintly", f"The file could not be saved:\n{error}")


if __name__ == "__main__":
    SprintlyApp().mainloop()
