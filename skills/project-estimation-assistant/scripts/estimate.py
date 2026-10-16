#!/usr/bin/env python3
"""Calculator for the project-estimation-assistant skill.

The estimate JSON file is the single source of truth. This script validates it,
computes buffers, the price sheet, the schedule, scenarios, break-even, expiry
and flags, and writes the generated Markdown blocks into the estimate Markdown
file between <!-- name:start --> and <!-- name:end --> markers. Every number the
assistant shows comes from here, never from mental arithmetic.

Usage:
  python estimate.py check     ESTIMATE.json [--today YYYY-MM-DD]
  python estimate.py run       ESTIMATE.json [--md ESTIMATE.md] [--today YYYY-MM-DD]
  python estimate.py change    BASE.json CHANGE.json --out NEW.json [--md NOTE.md] [--today ...]
  python estimate.py leakcheck FILE.md
  python estimate.py calibrate OUTCOME_LOG.md [--divisor 2] [--min 10]
  python estimate.py date      today | add JDATE DAYS | workdays JDATE DAYS [--holidays FILE.json]

Dates are Jalali (YYYY-MM-DD, Latin or Persian digits). Needs Python 3.9+ and
jdatetime (pip install jdatetime).
"""
import argparse
import json
import math
import re
import sys
from copy import deepcopy

try:
    import jdatetime
except ImportError:  # pragma: no cover
    sys.exit("jdatetime is required: pip install jdatetime")

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

DEFAULTS = {
    "sigma_divisor": 2.0,
    "z_target": 0.84,
    "z_range": 1.28,
    "buffer_floor": 0.20,
    "buffer_cap": 0.50,
    "addons": {"outside_expertise": 0.10, "unknown_executor": 0.15, "vague_input": 0.10},
    "max_task_hours": 16,
    "pm_overhead": 0.10,
    "coordination_hours_per_channel_week": 1.0,
    "max_compression": 0.25,
    "approval_days": 7,
    "proposal_validity_days": 14,
    "price_adjust_threshold_months": 2,
    "horizon_months": 24,
    "nowruz_client_pause_days": 14,
    "round_to": 100000,
    "sequential_milestones": True,
    "subcontractor_margin": None,
    "self_expertise": [],
    "roster_confirm_days": 30,
    "start_delay_weeks": 4,
}

CHECKLIST_KEYS = {
    "testing": "تست",
    "deploy": "راه‌اندازی محیط و deploy",
    "foreign_deps": "جایگزینی وابستگی‌های خارجی زمان اجرا",
    "integrations": "راه‌اندازی یکپارچگی‌ها",
    "security": "امنیت پایه",
    "docs": "مستندات",
    "handover": "تحویل",
    "data_migration": "انتقال داده یا ورود محتوا",
    "warranty": "دوره‌ی رفع باگ رایگان",
    "client_revisions": "دورهای اصلاح کارفرما",
}

RISK_PLACES = {
    "task_p": "داخل P یک کار",
    "addon_buffer": "اضافه‌بافر",
    "schedule_wait": "زمان انتظار",
    "assumption": "فرض؛ اگر رخ دهد درخواست تغییر است",
    "contract_clause": "بند قراردادی (مرحله‌ی ۵)",
    "accepted": "پذیرفته‌شده، قیمت‌گذاری نشده",
}

FLAG_NAMES = {
    "outside_expertise": "بیرون از تخصص",
    "unknown_executor": "مجری نامشخص",
    "vague_input": "ورودی مبهم",
}

# McConnell, Software Estimation (2006), Table 10.8: share of actual outcomes
# inside the best-to-worst range -> divisor for the standard deviation.
MCCONNELL_DIVISORS = [
    (10, 0.25), (20, 0.51), (30, 0.77), (40, 1.0), (50, 1.4),
    (60, 1.7), (70, 2.1), (80, 2.6), (90, 3.3), (99.7, 6.0),
]

KINDS = {"self", "team", "subcontractor", "unknown"}


class EstimateError(Exception):
    pass


# ---------------------------------------------------------------- formatting

def fa(value):
    return str(value).translate(FA_DIGITS)


def num(value, decimals=1):
    if value is None:
        return "نامشخص"
    if abs(value - round(value)) < 1e-9:
        text = f"{int(round(value)):,}"
    else:
        text = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
    return fa(text.replace(",", "٬").replace(".", "٫"))


def money(value):
    return "نامشخص" if value is None else num(round(value), 0)


def pct(value):
    return "نامشخص" if value is None else num(value * 100, 1) + "٪"


def money_range(low, high):
    return money(low) if low == high else f"{money(low)} تا {money(high)}"


def weeks(days):
    return None if days is None else days / 7.0


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------- dates

def parse_jdate(text):
    if isinstance(text, jdatetime.date):
        return text
    clean = str(text).translate(EN_DIGITS).strip().replace("/", "-")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", clean)
    if not match:
        raise EstimateError(f"تاریخ نامعتبر: {text} (قالب درست: 1405-08-01)")
    return jdatetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def jfmt(date):
    return fa(date.strftime("%Y/%m/%d"))


def today_jalali(override=None):
    return parse_jdate(override) if override else jdatetime.date.today()


class Calendar:
    """Maps day offsets from the project start to Jalali dates and counts working days."""

    def __init__(self, start, holidays, nowruz_pause):
        self.start = start
        self.holidays = {parse_jdate(h).isoformat() for h in holidays}
        self.nowruz_pause = nowruz_pause

    def date_at(self, offset):
        return self.start + jdatetime.timedelta(days=int(math.ceil(offset - 1e-9)))

    def is_workday(self, date, client):
        if date.weekday() == 6:  # Friday
            return False
        if date.isoformat() in self.holidays:
            return False
        if client and date.month == 1 and date.day <= self.nowruz_pause:
            return False
        return True

    def add_workdays(self, offset, count, client):
        day = int(math.ceil(offset - 1e-9))
        done = 0
        while done < count:
            day += 1
            if self.is_workday(self.start + jdatetime.timedelta(days=day), client):
                done += 1
        return float(day)


# ---------------------------------------------------------------- statistics

def task_expected(task):
    return (task["o"] + 4 * task["m"] + task["p"]) / 6.0


def group_stats(tasks, s):
    """PERT sums, the 80% target and the buffer for one group of tasks."""
    sum_m = sum(t["m"] for t in tasks)
    sum_o = sum(t["o"] for t in tasks)
    sum_e = sum(task_expected(t) for t in tasks)
    variance = sum(((t["p"] - t["o"]) / s["sigma_divisor"]) ** 2 for t in tasks)
    sigma = math.sqrt(variance)
    target = sum_e + s["z_target"] * sigma
    stat_buf = (target / sum_m - 1) if sum_m > 0 else 0.0
    base_buf = max(s["buffer_floor"], stat_buf)
    addon_hours = sum(t["m"] * t["addon_pct"] for t in tasks)
    addon_pct = (addon_hours / sum_m) if sum_m > 0 else 0.0
    return {
        "sum_m": sum_m, "sum_o": sum_o, "sum_e": sum_e, "sigma": sigma, "target": target,
        "stat_buf": stat_buf, "base_buf": base_buf, "addon_pct": addon_pct,
        "final_buf": base_buf + addon_pct, "buffered": sum_m * (1 + base_buf) + addon_hours,
        "range_low": max(sum_o, sum_e - s["z_range"] * sigma),
        "range_high": sum_e + s["z_range"] * sigma,
    }


def apply_buffer(tasks, stats):
    for t in tasks:
        t["buffered"] = t["m"] * (1 + stats["base_buf"]) + t["m"] * t["addon_pct"]


# ---------------------------------------------------------------- loading

def merged_settings(est):
    s = deepcopy(DEFAULTS)
    for key, value in (est.get("settings") or {}).items():
        if key == "addons":
            s["addons"].update(value)
        else:
            s[key] = value
    return s


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def normalize_tasks(est, s, people, errors):
    tasks = []
    for raw in est.get("tasks", []):
        t = dict(raw)
        tid = t.get("id")
        for field in ("id", "milestone", "title", "role", "executor"):
            if not t.get(field):
                errors.append(f"کار {tid or '?'}: فیلد «{field}» خالی است.")
        for field in ("o", "m", "p"):
            if not isinstance(t.get(field), (int, float)) or t.get(field) < 0:
                errors.append(f"کار {tid}: «{field}» باید عدد نامنفی باشد.")
                t[field] = 0
        if not (t["o"] <= t["m"] <= t["p"]):
            errors.append(f"کار {tid}: باید O ≤ M ≤ P باشد ({t['o']}, {t['m']}, {t['p']}).")
        t.setdefault("depends_on", [])
        t.setdefault("flags", [])
        if people.get(t.get("executor")) is None:
            errors.append(f"کار {tid}: مجری «{t.get('executor')}» در فهرست people نیست.")
        else:
            derive_flags(t, people, s)
        unknown_flags = [f for f in t["flags"] if f not in s["addons"]]
        if unknown_flags:
            errors.append(f"کار {tid}: پرچم ناشناخته {unknown_flags}.")
        tasks.append(t)
    return tasks


def derive_flags(task, people, s):
    """Add the executor-driven flags and the add-on percentage they imply."""
    person = people[task["executor"]]
    flags = set(task.get("flags", []))
    if person["kind"] == "unknown":
        flags.add("unknown_executor")
    if person["kind"] == "self" and task.get("role") not in s["self_expertise"]:
        flags.add("outside_expertise")
    task["flags"] = sorted(flags)
    task["addon_pct"] = sum(s["addons"].get(f, 0.0) for f in task["flags"])


def load_people(est, errors):
    people = {}
    for raw in est.get("people", []):
        p = dict(raw)
        if p.get("kind") not in KINDS:
            errors.append(f"نفر {p.get('id')}: kind باید یکی از {sorted(KINDS)} باشد.")
        if not isinstance(p.get("weekly_hours"), (int, float)) or p["weekly_hours"] <= 0:
            errors.append(f"نفر {p.get('id')}: weekly_hours باید عدد مثبت باشد (اگر نامعلوم است، فرض را بنویس).")
        p.setdefault("pricing", "hourly")
        p.setdefault("time_factor", {})
        people[p.get("id")] = p
    selves = [p for p in people.values() if p.get("kind") == "self"]
    if len(selves) != 1:
        errors.append("دقیقاً یک نفر با kind=self لازم است (مدیریت پروژه و هماهنگی با اوست).")
    return people


def time_factor(person, role):
    return float(person.get("time_factor", {}).get(role, 1.0))


# ---------------------------------------------------------------- validation

def validate(est, s, people, tasks, today):
    errors, warnings = [], []
    mode = est.get("meta", {}).get("mode", "full")
    milestone_ids = [m.get("id") for m in est.get("milestones", [])]
    if not milestone_ids:
        errors.append("هیچ milestoneی تعریف نشده.")
    order = {m: i for i, m in enumerate(milestone_ids)}
    ids = [t["id"] for t in tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"شناسه‌ی تکراری کار: {sorted(dupes)}")
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        if t["milestone"] not in order:
            errors.append(f"کار {t['id']}: milestone «{t['milestone']}» تعریف نشده.")
        if mode == "full" and t["m"] > s["max_task_hours"] and not t.get("removed_by"):
            errors.append(
                f"کار {t['id']}: M={t['m']} از سقف {s['max_task_hours']} ساعت بیشتر است؛ کار را بشکن.")
        if mode == "full" and not t.get("assumption"):
            warnings.append(f"کار {t['id']}: «فرض پشت برآورد» خالی است.")
        for dep in t["depends_on"]:
            if dep not in by_id:
                errors.append(f"کار {t['id']}: وابستگی «{dep}» وجود ندارد.")
            elif s["sequential_milestones"] and order.get(by_id[dep]["milestone"], 0) > order.get(t["milestone"], 0):
                errors.append(f"کار {t['id']}: به کار {dep} در milestone بعدی وابسته است.")
    # cycles
    state = {}

    def visit(tid, stack):
        if state.get(tid) == 1:
            errors.append(f"وابستگی چرخشی: {' → '.join(stack + [tid])}")
            return
        if state.get(tid) == 2 or tid not in by_id:
            return
        state[tid] = 1
        for dep in by_id[tid]["depends_on"]:
            visit(dep, stack + [tid])
        state[tid] = 2

    for tid in by_id:
        visit(tid, [])
    for w in est.get("external_waits", []):
        if not isinstance(w.get("days"), (int, float)) or w["days"] < 0:
            errors.append(f"انتظار {w.get('id')}: days باید عدد نامنفی باشد.")
        for tid in w.get("blocks", []) + ([w["start_after"]] if w.get("start_after") else []):
            if tid not in by_id:
                errors.append(f"انتظار {w.get('id')}: کار «{tid}» وجود ندارد.")
    if mode == "full":
        checklist = est.get("checklist", {})
        for key, label in CHECKLIST_KEYS.items():
            item = checklist.get(key)
            if not item:
                errors.append(f"چک‌لیست کارهای فراموش‌شده: «{label}» ({key}) نه ردیف دارد نه دلیل.")
                continue
            if item.get("tasks"):
                missing = [x for x in item["tasks"] if x not in by_id]
                if missing:
                    errors.append(f"چک‌لیست «{label}»: کارهای {missing} وجود ندارند.")
            elif not item.get("reason"):
                errors.append(f"چک‌لیست «{label}»: نه کار دارد نه دلیل.")
        for item in est.get("scope", []):
            linked = [x for x in item.get("tasks", []) if x in by_id]
            if not linked:
                errors.append(f"محدوده: «{item.get('item')}» هیچ کاری ندارد.")
        for r in est.get("risks", []):
            place = r.get("accounted_in")
            if place not in RISK_PLACES:
                errors.append(f"ریسک {r.get('id')}: accounted_in باید یکی از {sorted(RISK_PLACES)} باشد.")
            if place == "accepted" and not r.get("approved_by_user"):
                errors.append(f"ریسک {r.get('id')}: «پذیرفته‌شده» فقط با تأیید کاربر (approved_by_user=true).")
    start = est.get("meta", {}).get("start_date")
    if start:
        late = (today - parse_jdate(start)).days
        if late > s["start_delay_weeks"] * 7 and not any(t.get("status", "todo") != "todo" for t in tasks):
            warnings.append(f"شروع پروژه {fa(late)} روز از تاریخ فرضی گذشته و کاری شروع نشده؛ "
                            "شرط برآورد دوباره فعال است.")
    for f in est.get("facts_used", []):
        try:
            if f.get("expires") and parse_jdate(f["expires"]) < today:
                errors.append(f"اطلاعات منقضی: «{f.get('claim')}» ({f['expires']}). دوباره بررسی کن.")
        except EstimateError as exc:
            errors.append(str(exc))
    for p in people.values():
        if p.get("kind") in ("team", "subcontractor") and p.get("confirmed"):
            age = (today - parse_jdate(p["confirmed"])).days
            if age > s["roster_confirm_days"]:
                warnings.append(f"در دسترس بودن «{p.get('name')}» {fa(age)} روز پیش تأیید شده؛ دوباره بپرس.")
        if p.get("kind") != "self" and p.get("pricing") == "hourly" and p.get("rate") is None:
            warnings.append(f"نرخ «{p.get('name')}» نامشخص است؛ برگه‌ی قیمت نهایی نمی‌شود.")
        if p.get("kind") != "self" and p.get("margin") is None and s["subcontractor_margin"] is None:
            warnings.append(f"حاشیه‌ی سود روی کار «{p.get('name')}» تعریف نشده؛ صفر حساب شد.")
    return errors, warnings


# ---------------------------------------------------------------- schedule

def simulate(sched_tasks, people, waits, milestone_ids, s, cal, deduction):
    cap = {}
    for pid, p in people.items():
        weekly = p["weekly_hours"] - deduction.get(pid, 0.0)
        if weekly <= 0.5:
            raise EstimateError(f"ظرفیت هفتگی «{p.get('name')}» بعد از کسر مدیریت و هماهنگی تمام شد.")
        cap[pid] = weekly / 7.0
    in_schedule = {t["id"] for t in sched_tasks}
    duration = {
        t["id"]: t["buffered"] * time_factor(people[t["executor"]], t["role"]) / cap[t["executor"]]
        for t in sched_tasks
    }
    blocks = {}
    for w in waits:
        for tid in w.get("blocks", []):
            blocks.setdefault(tid, []).append(w)
    start, finish, wait_end = {}, {}, {}
    person_free = {pid: 0.0 for pid in people}

    def wait_ready(w):
        if w["id"] in wait_end:
            return wait_end[w["id"]]
        after = w.get("start_after")
        if after and after in in_schedule:
            if after not in finish:
                return None
            begin = finish[after]
        else:
            begin = 0.0
        wait_end[w["id"]] = cal.add_workdays(begin, w["days"], client=(w.get("party") == "client"))
        return wait_end[w["id"]]

    gate = 0.0
    delivery, opened = {}, {}
    for m in milestone_ids:
        opened[m] = gate
        pending = [t for t in sched_tasks if t["milestone"] == m]
        while pending:
            best = None
            for idx, t in enumerate(pending):
                deps = [d for d in t["depends_on"] if d in in_schedule]
                if any(d not in finish for d in deps):
                    continue
                ready = max([gate] + [finish[d] for d in deps])
                blocked = False
                for w in blocks.get(t["id"], []):
                    end = wait_ready(w)
                    if end is None:
                        blocked = True
                        break
                    ready = max(ready, end)
                if blocked:
                    continue
                begin = max(ready, person_free[t["executor"]])
                if best is None or (begin, idx) < best[0]:
                    best = ((begin, idx), t)
            if best is None:
                raise EstimateError(f"زمان‌بندی milestone {m} گیر کرد (وابستگی یا انتظاری که هرگز آزاد نمی‌شود).")
            (begin, _), t = best
            start[t["id"]] = begin
            finish[t["id"]] = begin + duration[t["id"]]
            person_free[t["executor"]] = finish[t["id"]]
            pending.remove(t)
        done = [finish[t["id"]] for t in sched_tasks if t["milestone"] == m]
        delivery[m] = max(done) if done else gate
        if s["sequential_milestones"]:
            gate = delivery[m]
    for w in waits:
        wait_ready(w)
    acceptance = {m: cal.add_workdays(delivery[m], s["approval_days"], client=True) for m in milestone_ids}
    ends = list(acceptance.values()) + [v for v in wait_end.values() if v is not None]
    return {
        "start": start, "finish": finish, "delivery": delivery, "acceptance": acceptance,
        "opened": opened, "wait_end": wait_end, "end": max(ends) if ends else 0.0,
    }


def coordination(sim, sched_tasks, milestone_ids, s):
    """Hours of coordination per milestone: channels n(n-1)/2 in each week of overlapping work."""
    per_ms = {m: 0.0 for m in milestone_ids}
    last = max(sim["finish"].values()) if sim["finish"] else 0.0
    total_weeks = int(math.ceil(last / 7.0))
    for week in range(total_weeks):
        lo, hi = week * 7.0, week * 7.0 + 7.0
        active = {t["executor"] for t in sched_tasks
                  if sim["start"][t["id"]] < hi and sim["finish"][t["id"]] > lo}
        n = len(active)
        hours = n * (n - 1) / 2 * s["coordination_hours_per_channel_week"]
        if hours <= 0:
            continue
        mid = lo + 3.5
        owner = milestone_ids[-1]
        for m in milestone_ids:
            if sim["opened"][m] <= mid <= sim["delivery"][m]:
                owner = m
                break
        per_ms[owner] += hours
    return per_ms


def schedule_two_pass(sched_tasks, people, waits, milestone_ids, s, cal, self_id, pm_total):
    first = simulate(sched_tasks, people, waits, milestone_ids, s, cal, {})
    coord1 = sum(coordination(first, sched_tasks, milestone_ids, s).values())
    work_weeks = max(1.0, (max(first["finish"].values()) if first["finish"] else 7.0) / 7.0)
    deduction = {self_id: (pm_total + coord1) / work_weeks}
    second = simulate(sched_tasks, people, waits, milestone_ids, s, cal, deduction)
    second["coordination"] = coordination(second, sched_tasks, milestone_ids, s)
    second["self_deduction_week"] = deduction[self_id]
    return second


# ---------------------------------------------------------------- costing

def margin_of(person, s):
    if person["kind"] == "self":
        return 0.0
    if person.get("margin") is not None:
        return float(person["margin"])
    return float(s["subcontractor_margin"] or 0.0)


def task_cost(task, people, s):
    """Price of one task's buffered hours for hourly executors (fixed quotes are per milestone)."""
    person = people[task["executor"]]
    if person.get("pricing") == "fixed" and person["kind"] != "self":
        return None
    if person.get("rate") is None:
        return None
    return task["buffered"] * person["rate"] * (1 + margin_of(person, s))


def add(a, b):
    return None if a is None or b is None else a + b


def round_up(value, step):
    if value is None:
        return None
    if not step:
        return value
    return math.ceil(value / step - 1e-9) * step


def price_group(tasks, people, s, self_rate, pm_base_hours):
    """Cost of a standalone group (add-on, change, assumption) with its own buffer stats."""
    stats = group_stats(tasks, s)
    apply_buffer(tasks, stats)
    total = 0.0
    for t in tasks:
        total = add(total, task_cost(t, people, s))
    pm = s["pm_overhead"] * pm_base_hours
    total = add(total, None if self_rate is None else pm * self_rate)
    return stats, total, pm


# ---------------------------------------------------------------- main computation

def compute(est, today, extras=True):
    errors = []
    s = merged_settings(est)
    people = load_people(est, errors)
    tasks = normalize_tasks(est, s, people, errors)
    if errors:
        raise EstimateError("\n".join(errors))
    meta = est.get("meta", {})
    milestones = est["milestones"]
    milestone_ids = [m["id"] for m in milestones]
    names = {m["id"]: m.get("name", m["id"]) for m in milestones}
    self_id = next(pid for pid, p in people.items() if p["kind"] == "self")
    self_rate = people[self_id].get("rate")
    start = parse_jdate(meta["start_date"]) if meta.get("start_date") else today
    cal = Calendar(start, est.get("holidays", []), s["nowruz_client_pause_days"])
    flags = []

    active = [t for t in tasks if not t.get("removed_by")]
    removed = [t for t in tasks if t.get("removed_by")]
    base = [t for t in active if not t.get("addon") and not t.get("change_id")]
    changes = {}
    for t in active:
        if t.get("change_id"):
            changes.setdefault(t["change_id"], []).append(t)
    addons = {}
    for t in active:
        if t.get("addon") and not t.get("change_id"):
            addons.setdefault(t["addon"], []).append(t)

    ms = {}
    for m in milestone_ids:
        group = [t for t in base if t["milestone"] == m]
        stats = group_stats(group, s)
        apply_buffer(group, stats)
        if stats["final_buf"] > s["buffer_cap"]:
            flags.append(f"بافر milestone «{names[m]}» {pct(stats['final_buf'])} است (بالای سقف "
                         f"{pct(s['buffer_cap'])}): قیمت نده، milestone نیازسنجی پولی پیشنهاد کن.")
        pm_base = sum(t["m"] for t in group) + sum(t["m"] for t in removed if t["milestone"] == m)
        ms[m] = {"stats": stats, "pm_base": pm_base}

    change_info = {}
    for cid, group in changes.items():
        stats = group_stats(group, s)
        apply_buffer(group, stats)
        change_info[cid] = {"stats": stats, "pm_base": sum(t["m"] for t in group)}

    sched_tasks = base + [t for g in changes.values() for t in g]
    pm_hours = {m: s["pm_overhead"] * (ms[m]["pm_base"] + sum(
        t["m"] for g in changes.values() for t in g if t["milestone"] == m)) for m in milestone_ids}
    waits = est.get("external_waits", [])
    sim = schedule_two_pass(sched_tasks, people, waits, milestone_ids, s, cal, self_id,
                            sum(pm_hours.values()))

    # price sheet per milestone
    sheet = []
    for m in milestone_ids:
        in_ms = [t for t in sched_tasks if t["milestone"] == m]
        self_hours = sum(t["buffered"] for t in in_ms if t["executor"] == self_id)
        self_cost = None if self_rate is None else self_hours * self_rate
        overhead_hours = pm_hours[m] + sim["coordination"][m]
        overhead_cost = None if self_rate is None else overhead_hours * self_rate
        others = 0.0
        for pid, p in people.items():
            if pid == self_id:
                continue
            mine = [t for t in in_ms if t["executor"] == pid]
            if not mine:
                continue
            if p.get("pricing") == "fixed":
                quote = (p.get("fixed_quotes") or {}).get(m)
                others = add(others, None if quote is None else quote * (1 + margin_of(p, s)))
            else:
                for t in mine:
                    others = add(others, task_cost(t, people, s))
        extra = sum(c.get("amount", 0) for c in est.get("project_costs", []) if c.get("milestone") == m)
        total = add(add(add(self_cost, overhead_cost), others), extra)
        sheet.append({
            "id": m, "name": names[m], "base_hours": sum(t["m"] for t in in_ms),
            "buffered_hours": sum(t["buffered"] for t in in_ms), "self_cost": self_cost,
            "overhead_hours": overhead_hours, "overhead_cost": overhead_cost, "others": others,
            "extra": extra, "total": total, "rounded": round_up(total, s["round_to"]),
        })
    grand = 0.0
    for row in sheet:
        grand = add(grand, row["rounded"])
    if grand is None:
        flags.append("نرخ یا قیمت حداقل یک مجری نامشخص است؛ برگه‌ی قیمت نهایی نیست.")

    # effective rate
    self_tasks = [t for t in sched_tasks if t["executor"] == self_id]
    overhead = sum(pm_hours.values()) + sum(sim["coordination"].values())
    income = 0.0 if self_rate is not None else None
    if self_rate is not None:
        income = sum(t["buffered"] for t in self_tasks) * self_rate + overhead * self_rate
        for row in sheet:
            for pid, p in people.items():
                if pid == self_id:
                    continue
                share = [t for t in sched_tasks if t["milestone"] == row["id"] and t["executor"] == pid]
                if not share:
                    continue
                if p.get("pricing") == "fixed":
                    quote = (p.get("fixed_quotes") or {}).get(row["id"])
                    gain = None if quote is None else quote * margin_of(p, s)
                else:
                    gain = None if p.get("rate") is None else sum(t["buffered"] for t in share) * p["rate"] * margin_of(p, s)
                income = add(income, gain)
    hours_target = sum(t["buffered"] * time_factor(people[self_id], t["role"]) for t in self_tasks) + overhead
    hours_expected = sum(task_expected(t) * time_factor(people[self_id], t["role"]) for t in self_tasks) + overhead
    effective = {
        "income": income,
        "rate_target": None if income is None or not hours_target else income / hours_target,
        "rate_expected": None if income is None or not hours_expected else income / hours_expected,
        "hours_target": hours_target, "hours_expected": hours_expected,
    }
    result = {
        "s": s, "meta": meta, "people": people, "tasks": tasks, "base": base, "ms": ms,
        "names": names, "milestone_ids": milestone_ids, "sim": sim, "sheet": sheet, "grand": grand,
        "pm_hours": pm_hours, "cal": cal, "self_id": self_id, "flags": flags, "today": today,
        "changes": change_info, "removed": removed, "effective": effective,
    }
    if not extras:
        return result

    result["risks"] = est.get("risks", [])

    # deadline and compression
    end = sim["end"]
    if meta.get("deadline"):
        limit = (parse_jdate(meta["deadline"]) - start).days
        result["deadline_days"] = limit
        if end > limit:
            flags.append(f"با این ترکیب نیرو به مهلت کارفرما ({jfmt(parse_jdate(meta['deadline']))}) نمی‌رسد؛ "
                         "سناریوی بزرگ‌تر، کاهش محدوده، فازبندی یا جابه‌جایی مهلت.")
    if meta.get("requested_weeks"):
        ratio = meta["requested_weeks"] * 7.0 / end if end else 1.0
        result["compression"] = 1 - ratio
        if ratio < 1 - s["max_compression"]:
            flags.append(f"مدت درخواستی {pct(1 - ratio)} کوتاه‌تر از برنامه است (بیش از سقف "
                         f"{pct(s['max_compression'])}): غیرواقعی؛ کاهش محدوده یا فازبندی پیشنهاد کن.")
        elif ratio < 1:
            flags.append(f"مدت درخواستی {pct(1 - ratio)} فشرده‌سازی می‌خواهد؛ مجاز است ولی هزینه‌ی اضافه دارد.")
    months = end / 30.44
    result["months"] = months
    if months > s["price_adjust_threshold_months"]:
        flags.append(f"مدت کل حدود {num(months)} ماه است (بیش از {num(s['price_adjust_threshold_months'])} ماه): "
                     "بند تعدیل قیمت لازم است (برای مرحله‌ی ۴ و ۵).")

    # add-ons and cut menu
    result["addons"] = []
    for aid, group in addons.items():
        group = [dict(t) for t in group]
        pm_base = sum(t["m"] for t in group)
        stats, total, _ = price_group(group, people, s, self_rate, pm_base)
        with_addon = schedule_two_pass(sched_tasks + group, people, waits, milestone_ids, s, cal, self_id,
                                       sum(pm_hours.values()) + s["pm_overhead"] * pm_base)
        result["addons"].append({
            "id": aid, "title": next((a.get("title") for a in est.get("addons", []) if a.get("id") == aid), aid),
            "hours": stats["buffered"], "price": round_up(total, s["round_to"]),
            "delay_weeks": weeks(with_addon["end"] - end),
        })
    result["cuts"] = []
    groups = {}
    for t in base:
        if t.get("cut_group"):
            groups.setdefault(t["cut_group"], []).append(t)
    for gid, group in groups.items():
        ids = {t["id"] for t in group}
        saving = 0.0
        for t in group:
            saving = add(saving, task_cost(t, people, s))
        blockers = sorted({t["id"] for t in sched_tasks if t["id"] not in ids and set(t["depends_on"]) & ids})
        rest = [t for t in sched_tasks if t["id"] not in ids]
        try:
            without = schedule_two_pass(rest, people, waits, milestone_ids, s, cal, self_id, sum(pm_hours.values()))
            gain = weeks(end - without["end"])
        except EstimateError:
            gain = None
        result["cuts"].append({
            "id": gid, "title": next((c.get("title") for c in est.get("cut_groups", []) if c.get("id") == gid), gid),
            "saving": saving, "gain_weeks": gain, "blockers": blockers,
        })

    # scenarios
    result["scenarios"] = []
    for sc in est.get("scenarios", []):
        variant = deepcopy(est)
        for t in variant["tasks"]:
            if t.get("role") in sc.get("assign", {}):
                t["executor"] = sc["assign"][t["role"]]
                t["flags"] = [f for f in t.get("flags", []) if f == "vague_input"]
        try:
            r = compute(variant, today, extras=False)
            used = sorted({t["executor"] for t in r["base"]})
            hours = sum(row["buffered_hours"] + row["overhead_hours"] for row in r["sheet"])
            ok = None
            if meta.get("deadline"):
                ok = r["sim"]["end"] <= (parse_jdate(meta["deadline"]) - start).days
            result["scenarios"].append({
                "name": sc.get("name"), "people": [r["people"][p].get("name", p) for p in used],
                "hours": hours, "cost": r["grand"], "weeks": weeks(r["sim"]["end"]), "meets": ok,
                "rate": r["effective"]["rate_target"],
            })
        except EstimateError as exc:
            result["scenarios"].append({"name": sc.get("name"), "error": str(exc)})

    # operating costs and options
    chosen = next((o["id"] for o in est.get("options", []) if o.get("chosen")), "chosen")
    result["chosen_option"] = chosen
    result["operating"] = [c for c in est.get("operating_costs", []) if c.get("option", chosen) == chosen]
    result["options"] = []
    for o in est.get("options", []):
        rows = [c for c in est.get("operating_costs", []) if c.get("option", chosen) == o["id"]]
        build = o.get("build_cost")
        if build is None and o.get("chosen"):
            build = grand
        one_time = o.get("one_time", sum(c.get("one_time", 0) for c in rows))
        monthly = o.get("monthly", sum((c.get("monthly_low", 0) + c.get("monthly_high", 0)) / 2 for c in rows))
        result["options"].append({"id": o["id"], "name": o.get("name", o["id"]), "build": build,
                                  "one_time": one_time, "monthly": monthly})
    result["breakeven"] = breakeven(result["options"], s["horizon_months"])

    # support plan
    plan = est.get("support_plan")
    if plan and plan.get("components"):
        comps = [dict(c, addon_pct=0.0) for c in plan["components"]]
        stats = group_stats(comps, s)
        hours = math.ceil(stats["sum_e"] + s["z_target"] * stats["sigma"])
        rate = plan.get("rate", self_rate)
        monthly = None if rate is None else round_up(hours * rate, s["round_to"])
        result["support"] = {"hours": hours, "monthly": monthly, "terms": plan.get("terms", [3, 6, 12]),
                             "review": plan.get("review_months", 6), "components": comps}

    # assumptions with "if false" impact
    result["assumptions"] = []
    for a in est.get("assumptions", []):
        extra_tasks = []
        for raw in (a.get("if_false") or {}).get("tasks", []):
            t = dict(raw)
            t.setdefault("id", f"{a.get('id')}-x")
            if t.get("executor") not in people:
                raise EstimateError(f"فرض {a.get('id')}: مجری «{t.get('executor')}» در فهرست people نیست.")
            derive_flags(t, people, s)
            extra_tasks.append(t)
        if extra_tasks:
            stats, total, pm = price_group(extra_tasks, people, s, self_rate, sum(t["m"] for t in extra_tasks))
            days = sum(t["buffered"] * time_factor(people[t["executor"]], t["role"])
                       / (people[t["executor"]]["weekly_hours"] / 7.0) for t in extra_tasks)
            hours = stats["buffered"] + pm
        else:
            total, days, hours = 0.0, 0.0, 0.0
        days += (a.get("if_false") or {}).get("wait_days", 0)
        result["assumptions"].append({"id": a.get("id"), "text": a.get("text"),
                                      "effect": (a.get("if_false") or {}).get("text", ""),
                                      "hours": hours, "cost": total, "days": days})

    # expiry
    expiry = today + jdatetime.timedelta(days=s["proposal_validity_days"])
    source = "اعتبار پروپوزال در settings"
    for f in est.get("facts_used", []):
        if f.get("kind") == "price" and f.get("expires") and parse_jdate(f["expires"]) < expiry:
            expiry = parse_jdate(f["expires"])
            source = f"انقضای «{f.get('claim')}»"
    result["expiry"] = expiry
    result["expiry_source"] = source

    return result


def breakeven(options, horizon):
    usable = [o for o in options if o["build"] is not None]
    if len(usable) < 2:
        return None
    limit = max(horizon * 3, 60)

    def cost(o, month):
        return o["build"] + o["one_time"] + o["monthly"] * month

    segments = []
    for month in range(0, limit + 1):
        best = min(usable, key=lambda o: (cost(o, month), o["id"]))
        if segments and segments[-1][0]["id"] == best["id"]:
            segments[-1][2] = month
        else:
            segments.append([best, month, month])
    at_horizon = sorted(usable, key=lambda o: cost(o, horizon))
    return {"segments": segments, "limit": limit, "horizon": horizon,
            "at_horizon": [(o, cost(o, horizon)) for o in at_horizon]}


# ---------------------------------------------------------------- rendering

def render_internal(r):
    s, names, sim, cal = r["s"], r["names"], r["sim"], r["cal"]
    out = [f"_تولیدشده با scripts/estimate.py در {jfmt(r['today'])}. این بخش را دستی ویرایش نکن؛ "
           "فایل JSON را عوض کن و اسکریپت را دوباره اجرا کن._", ""]
    out.append("### هشدارها")
    out.append("\n".join(f"- {f}" for f in r["flags"]) if r["flags"] else "- هیچ")
    out.append("")
    out.append("### محاسبه‌ی هر milestone")
    rows = []
    for m in r["milestone_ids"]:
        st = r["ms"][m]["stats"]
        rows.append([names[m], num(st["sum_m"]), num(st["sum_e"]), num(st["sigma"]), num(st["target"]),
                     pct(st["stat_buf"]), pct(st["base_buf"]), pct(st["addon_pct"]), pct(st["final_buf"]),
                     num(st["buffered"]), num(r["pm_hours"][m])])
    out.append(table(["milestone", "ΣM", "ΣE", "σ", "هدف ۸۰٪", "بافر آماری", "بافر با کف",
                      "اضافه‌بافر", "بافر نهایی", "ساعت با بافر", "مدیریت پروژه"], rows))
    out.append("")
    out.append(f"σ هر کار = (P − O) ÷ {num(s['sigma_divisor'])}؛ هدف = ΣE + {num(s['z_target'], 2)}σ؛ "
               f"کف بافر {pct(s['buffer_floor'])}.")
    out.append("")
    out.append("### کارها")
    rows = []
    for t in r["tasks"]:
        status = ""
        if t.get("removed_by"):
            status = f"حذف‌شده ({t['removed_by']})"
        elif t.get("addon"):
            status = f"افزودنی {t['addon']}"
        elif t.get("change_id"):
            status = f"تغییر {t['change_id']}"
        rows.append([t["id"], t["title"], t["role"], t.get("seniority", ""), names.get(t["milestone"], t["milestone"]),
                     r["people"][t["executor"]].get("name", t["executor"]), num(t["o"]), num(t["m"]), num(t["p"]),
                     num(task_expected(t)), "، ".join(FLAG_NAMES.get(f, f) for f in t["flags"]),
                     num(t.get("buffered")) if "buffered" in t else "", status, t.get("assumption", "")])
    out.append(table(["#", "کار", "نقش", "سطح", "milestone", "مجری", "O", "M", "P", "E", "پرچم‌ها",
                      "ساعت با بافر", "وضعیت", "فرض"], rows))
    out.append("")
    out.append("### برگه‌ی قیمت (بدون درصد پرداخت و مالیات)")
    rows = []
    for row in r["sheet"]:
        rows.append([row["name"], num(row["base_hours"]), num(row["buffered_hours"]), money(row["self_cost"]),
                     f"{num(row['overhead_hours'])} ساعت / {money(row['overhead_cost'])}", money(row["others"]),
                     money(row["extra"]), money(row["total"]), money(row["rounded"])])
    out.append(table(["milestone", "ساعت پایه", "ساعت با بافر", "کار تو", "مدیریت و هماهنگی",
                      "تیم و پیمانکار (با حاشیه)", "هزینه‌های جدا", "جمع", "جمع گردشده"], rows))
    out.append("")
    out.append(f"**جمع کل پروژه:** {money(r['grand'])} تومان (گردکردن به بالا با گام {money(s['round_to'])}).")
    for cid, info in r["changes"].items():
        out.append(f"- درخواست تغییر {cid}: بافر {pct(info['stats']['final_buf'])} روی همین کارها، جدا حساب شده.")
    out.append("")
    if r.get("addons"):
        out.append("### افزودنی‌های اختیاری")
        out.append(table(["افزودنی", "ساعت با بافر", "قیمت", "اثر زمانی (هفته)"],
                         [[a["title"], num(a["hours"]), money(a["price"]), num(a["delay_weeks"])] for a in r["addons"]]))
        out.append("")
    if r.get("cuts"):
        out.append("### منوی کاهش محدوده (داخلی، فقط برای مذاکره)")
        out.append(table(["گروه", "صرفه‌جویی تقریبی", "زودتر تمام می‌شود (هفته)", "کارهای وابسته"],
                         [[c["title"], money(c["saving"]), num(c["gain_weeks"]),
                           "، ".join(c["blockers"]) or "ندارد"] for c in r["cuts"]]))
        out.append("")
    out.append("### زمان‌بندی (هدف ۸۰٪)")
    rows = []
    for m in r["milestone_ids"]:
        rows.append([names[m], num(weeks(sim["opened"][m])), num(weeks(sim["delivery"][m])),
                     num(weeks(sim["acceptance"][m])), jfmt(cal.date_at(sim["opened"][m])),
                     jfmt(cal.date_at(sim["delivery"][m])), jfmt(cal.date_at(sim["acceptance"][m]))])
    out.append(table(["milestone", "شروع (هفته)", "تحویل (هفته)", "پذیرش (هفته)", "تاریخ شروع",
                      "تاریخ تحویل", "تاریخ پذیرش"], rows))
    out.append("")
    out.append(f"شروع فرضی: {jfmt(cal.start)}. پایان: هفته‌ی {num(weeks(sim['end']))} ({jfmt(cal.date_at(sim['end']))}). "
               f"مهلت تأیید هر milestone {num(s['approval_days'])} روز کاری؛ جمعه‌ها، تعطیلات رسمی و "
               f"{num(s['nowruz_client_pause_days'])} روز اول نوروز برای کارهای منتظر کارفرما حساب نمی‌شود.")
    out.append(f"ظرفیت هفتگی تو برای مدیریت و هماهنگی {num(sim['self_deduction_week'])} ساعت کم شد.")
    if sim["wait_end"]:
        out.append("")
        out.append(table(["انتظار بیرونی", "پایان (هفته)", "تاریخ"],
                         [[wid, num(weeks(v)), jfmt(cal.date_at(v)) if v is not None else "نامشخص"]
                          for wid, v in sim["wait_end"].items()]))
    coord = sum(sim["coordination"].values())
    if coord:
        out.append(f"\nساعت هماهنگی تیم: {num(coord)} (به‌ازای هر کانال {num(s['coordination_hours_per_channel_week'])} "
                   "ساعت در هفته).")
    out.append("")
    if r.get("scenarios"):
        out.append("### سناریوهای نیرو")
        rows = []
        for sc in r["scenarios"]:
            if "error" in sc:
                rows.append([sc["name"], "—", "—", "—", "—", "—", sc["error"]])
                continue
            meets = "—" if sc["meets"] is None else ("بله" if sc["meets"] else "خیر")
            rows.append([sc["name"], "، ".join(sc["people"]), num(sc["hours"]), money(sc["cost"]),
                         num(sc["weeks"]), meets, money(sc["rate"])])
        out.append(table(["سناریو", "نفرات", "کل ساعت با هماهنگی", "هزینه", "مدت (هفته)", "به مهلت می‌رسد؟",
                          "نرخ مؤثر تو (هدف ۸۰٪)"], rows))
        out.append("")
    if r.get("operating") or r.get("options"):
        out.append("### هزینه‌های بهره‌برداری و مقایسه‌ی گزینه‌ها")
        if r.get("operating"):
            out.append(operating_table(r))
        be = r.get("breakeven")
        if be:
            out.append("")
            out.append(table(["گزینه", f"هزینه‌ی کل در {num(be['horizon'])} ماه"],
                             [[o["name"], money(c)] for o, c in be["at_horizon"]]))
            out.append("")
            out.append(breakeven_sentence(be))
        out.append("")
    if r.get("support"):
        sp = r["support"]
        out.append("### پلن پشتیبانی")
        out.append(f"ساعت رزروشده در ماه: {num(sp['hours'])} (امید زمانی + {num(s['z_target'], 2)}σ)؛ "
                   f"ماهانه {money(sp['monthly'])} تومان؛ بازبینی هر {num(sp['review'])} ماه.")
        out.append("")
    if r.get("assumptions"):
        out.append("### فرض‌ها و اثر «اگر نادرست بود» (داخلی)")
        out.append(table(["فرض", "اگر نادرست بود", "ساعت", "تومان", "روز"],
                         [[a["text"], a["effect"], num(a["hours"]), money(a["cost"]), num(a["days"])]
                          for a in r["assumptions"]]))
        out.append("")
    risks = r.get("risks", [])
    if risks:
        out.append("### ریسک‌ها")
        out.append(table(["ریسک", "احتمال", "اثر", "راه کاهش", "صاحب", "محل حساب‌شدن"],
                         [[x.get("text"), x.get("probability", ""), x.get("impact", ""), x.get("mitigation", ""),
                           x.get("owner", ""), RISK_PLACES.get(x.get("accounted_in"), "?")
                           + (f" ({x['ref']})" if x.get("ref") else "")] for x in risks]))
        out.append("")
    eff = r["effective"]
    out.append("### نرخ مؤثر تو")
    out.append(f"درآمد تو از این پروژه: {money(eff['income'])} تومان. "
               f"اگر به هدف ۸۰٪ برسد: {money(eff['rate_target'])} تومان در ساعت واقعی "
               f"({num(eff['hours_target'])} ساعت)؛ اگر طبق امید زمانی تمام شود: {money(eff['rate_expected'])} "
               f"({num(eff['hours_expected'])} ساعت).")
    out.append("")
    out.append("### انقضا")
    out.append(f"این برآورد تا {jfmt(r['expiry'])} معتبر است ({r['expiry_source']}).")
    return "\n".join(out)


def operating_table(r):
    s = r["s"]
    rows = []
    for c in r["operating"]:
        low = c.get("one_time", 0) + c.get("monthly_low", 0) * s["horizon_months"]
        high = c.get("one_time", 0) + c.get("monthly_high", 0) * s["horizon_months"]
        rows.append([c.get("title"), money(c.get("one_time", 0)),
                     money_range(c.get("monthly_low", 0), c.get("monthly_high", 0)),
                     money_range(low, high), c.get("payer_label", "کارفرما، مستقیم به سرویس‌دهنده"),
                     f"{c.get('source', 'بدون منبع: تأیید نشده')} ({c.get('checked', '?')})"])
    return table(["هزینه", "یک‌باره", "ماهانه", f"جمع {num(s['horizon_months'])} ماه", "پرداخت‌کننده", "منبع"], rows)


def breakeven_sentence(be):
    segs = be["segments"]
    if len(segs) == 1:
        return f"در کل بازه‌ی {num(be['limit'])} ماه، «{segs[0][0]['name']}» ارزان‌ترین گزینه است."
    parts = []
    for i, (opt, first, last) in enumerate(segs):
        if i == 0:
            parts.append(f"اگر کمتر از {num(last + 1)} ماه از سیستم استفاده شود، «{opt['name']}» در مجموع ارزان‌تر است")
        elif i == len(segs) - 1:
            parts.append(f"و اگر بیشتر از {num(first)} ماه، «{opt['name']}»")
        else:
            parts.append(f"از ماه {num(first)} تا {num(last)}، «{opt['name']}»")
    return "، ".join(parts) + "."


def render_client(r):
    s, names, sim = r["s"], r["names"], r["sim"]
    out = ["_مواد خروجی کارفرما؛ بدون ساعت، نرخ، بافر و حاشیه. تولیدشده با scripts/estimate.py._", ""]
    rows = []
    for row in r["sheet"]:
        m = row["id"]
        begin = max(1, int(math.floor(weeks(sim["opened"][m]))) + 1)
        finish = max(begin, int(math.ceil(weeks(sim["acceptance"][m]))))
        rows.append([row["name"], f"هفته‌ی {fa(begin)} تا {fa(finish)} بعد از پیش‌پرداخت", money(row["rounded"])])
    out.append(table(["مرحله", "زمان", "مبلغ (تومان)"], rows))
    out.append("")
    out.append(f"**مبلغ کل:** {money(r['grand'])} تومان")
    if r.get("addons"):
        out.append("")
        out.append("**افزودنی‌های اختیاری**")
        out.append(table(["افزودنی", "مبلغ (تومان)", "اثر بر زمان"],
                         [[a["title"], money(a["price"]),
                           f"حدود {num(max(0.0, round(a['delay_weeks'] * 2) / 2))} هفته" if a["delay_weeks"] else "بدون اثر"]
                          for a in r["addons"]]))
    if r.get("operating"):
        out.append("")
        out.append(f"**هزینه‌های بهره‌برداری** (برآورد به قیمت امروز، نه تعهد؛ کارفرما مستقیم به سرویس‌دهنده‌ها می‌پردازد)")
        rows = []
        for c in r["operating"]:
            rows.append([c.get("title"), money(c.get("one_time", 0)),
                         money_range(c.get("monthly_low", 0), c.get("monthly_high", 0))])
        out.append(table(["هزینه", "یک‌باره (تومان)", "ماهانه (تومان)"], rows))
    if r.get("breakeven"):
        out.append("")
        out.append(breakeven_sentence(r["breakeven"]))
    if r.get("support") and r["support"]["monthly"] is not None:
        sp = r["support"]
        out.append("")
        out.append(f"**پلن پشتیبانی اختیاری بعد از دوره‌ی رفع باگ رایگان:** تا {num(sp['hours'])} ساعت کار در ماه، "
                   f"ماهانه {money(sp['monthly'])} تومان، برای دوره‌های "
                   + "، ".join(f"{fa(t)}" for t in sp["terms"]) + " ماهه؛ مبلغ هر "
                   f"{num(sp['review'])} ماه بازبینی می‌شود.")
    out.append("")
    out.append(f"اعتبار این قیمت‌ها تا {jfmt(r['expiry'])}.")
    return "\n".join(out)


def render_quick(est, today):
    errors = []
    s = merged_settings(est)
    people = load_people(est, errors)
    tasks = normalize_tasks(est, s, people, errors)
    if errors:
        raise EstimateError("\n".join(errors))
    names = {m["id"]: m.get("name", m["id"]) for m in est["milestones"]}
    self_id = next(pid for pid, p in people.items() if p["kind"] == "self")
    rows, tot_low, tot_high, c_low, c_high, c_mid = [], 0.0, 0.0, 0.0, 0.0, 0.0
    load = {}
    for m in names:
        group = [t for t in tasks if t["milestone"] == m]
        st = group_stats(group, s)
        low, high = st["range_low"], st["range_high"]
        factor_low = low / st["sum_e"] if st["sum_e"] else 0
        factor_high = high / st["sum_e"] if st["sum_e"] else 0
        cost_e = 0.0
        for t in group:
            p = people[t["executor"]]
            rate = p.get("rate")
            cost_e = add(cost_e, None if rate is None else task_expected(t) * rate * (1 + margin_of(p, s)))
            load[t["executor"]] = load.get(t["executor"], 0.0) + task_expected(t) * time_factor(p, t["role"])
        pm = s["pm_overhead"] * st["sum_e"] * (people[self_id].get("rate") or 0)
        lo_cost = None if cost_e is None else (cost_e + pm) * factor_low
        hi_cost = None if cost_e is None else (cost_e + pm) * factor_high
        mid_cost = None if cost_e is None else cost_e + pm
        rows.append([names[m], f"{num(low)} تا {num(high)}", num(st["sum_e"]),
                     f"{money(lo_cost)} تا {money(hi_cost)}", money(mid_cost)])
        tot_low += low
        tot_high += high
        c_low = add(c_low, lo_cost)
        c_high = add(c_high, hi_cost)
        c_mid = add(c_mid, mid_cost)
    bottleneck = max((h / people[p]["weekly_hours"] for p, h in load.items()), default=0.0)
    total_e = sum(task_expected(t) for t in tasks) or 1.0
    approvals = len(names) * s["approval_days"] * 7 / 6 / 7
    w_low = bottleneck * tot_low / total_e + approvals
    w_high = bottleneck * tot_high / total_e + approvals
    out = ["**برآورد سریع؛ برای کارفرما نیست.** بازه‌ها ۸۰٪ اطمینان دارند (امید زمانی ± "
           f"{num(s['z_range'], 2)}σ) و از اجزای درشت ساخته شده‌اند.", ""]
    out.append(table(["milestone", "ساعت (بازه‌ی ۸۰٪)", "امید ساعت", "هزینه (بازه‌ی ۸۰٪)", "امید هزینه"], rows))
    out.append("")
    out.append(f"**جمع:** {num(tot_low)} تا {num(tot_high)} ساعت؛ {money(c_low)} تا {money(c_high)} تومان "
               f"(امید هزینه {money(c_mid)})؛ حدود {num(w_low)} تا {num(w_high)} هفته (تقریبی، بدون مسیر بحرانی).")
    missing = sorted({people[t["executor"]].get("name", t["executor"]) for t in tasks
                      if people[t["executor"]].get("rate") is None})
    if missing:
        out.append(f"\nنرخ این مجری‌ها نامشخص است و هزینه بدون آن کامل نمی‌شود: {'، '.join(missing)}.")
    out.append(f"\n_تولیدشده با scripts/estimate.py در {jfmt(today)}._")
    return "\n".join(out)


# ---------------------------------------------------------------- markdown blocks

def write_blocks(md_path, blocks, title):
    try:
        with open(md_path, encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        content = f"# {title}\n"
    for name, text in blocks.items():
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        new = f"{start}\n{text}\n{end}"
        if start in content and end in content:
            pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
            content = pattern.sub(lambda _: new, content, count=1)
        else:
            content = content.rstrip("\n") + "\n\n" + new + "\n"
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(content)


# ---------------------------------------------------------------- commands

def cmd_check(args):
    est = load_json(args.file)
    today = today_jalali(args.today)
    s = merged_settings(est)
    errors = []
    people = load_people(est, errors)
    tasks = normalize_tasks(est, s, people, errors)
    more, warnings = validate(est, s, people, tasks, today)
    errors += more
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if not errors:
        print("ok: فایل برآورد معتبر است.")
    return 1 if errors else 0


def cmd_run(args):
    est = load_json(args.file)
    today = today_jalali(args.today)
    if cmd_check(args):
        return 1
    title = est.get("meta", {}).get("project", "برآورد")
    if est.get("meta", {}).get("mode") == "quick":
        text = render_quick(est, today)
        blocks = {"quick": text}
    else:
        r = compute(est, today)
        blocks = {"calc": render_internal(r), "client": render_client(r)}
        text = blocks["calc"] + "\n\n---\n\n" + blocks["client"]
    if args.md:
        write_blocks(args.md, blocks, title)
        print(f"ok: بخش‌های {', '.join(blocks)} در {args.md} نوشته شد.")
    else:
        print(text)
    return 0


def cmd_change(args):
    base = load_json(args.base)
    change = load_json(args.change)
    today = today_jalali(args.today)
    cid = change.get("id", "C1")
    kind = change.get("classification")
    if kind not in ("clarification", "addition", "modification", "removal"):
        print("error: classification باید یکی از clarification, addition, modification, removal باشد.")
        return 1
    if kind == "clarification":
        print(f"درخواست {cid} توضیح است و در محدوده‌ی فعلی بوده؛ قیمت و زمان عوض نمی‌شود.\nدلیل: {change.get('reason', '')}")
        return 0
    before = compute(deepcopy(base), today, extras=False)
    merged = deepcopy(base)
    by_id = {t["id"]: t for t in merged["tasks"]}
    for tid in change.get("remove_tasks", []):
        t = by_id.get(tid)
        if t is None:
            print(f"error: کار {tid} در برآورد پایه نیست.")
            return 1
        if t.get("status", "todo") != "todo":
            print(f"error: کار {tid} شروع شده ({t.get('status')}); فقط کار شروع‌نشده حذف می‌شود.")
            return 1
        t["removed_by"] = cid
    for raw in change.get("add_tasks", []) + change.get("rework_tasks", []):
        t = dict(raw)
        t["change_id"] = cid
        merged["tasks"].append(t)
    merged.setdefault("meta", {})["version"] = merged.get("meta", {}).get("version", 1) + 1
    after = compute(deepcopy(merged), today, extras=False)
    delta_price = None if before["grand"] is None or after["grand"] is None else after["grand"] - before["grand"]
    delta_weeks = weeks(after["sim"]["end"] - before["sim"]["end"])
    validity = today + jdatetime.timedelta(days=merged_settings(base)["proposal_validity_days"])
    internal = [f"### درخواست تغییر {cid} ({kind})", "",
                f"- قیمت قبل: {money(before['grand'])}؛ بعد: {money(after['grand'])}؛ تفاوت: {money(delta_price)} تومان",
                f"- پایان پروژه: هفته‌ی {num(weeks(before['sim']['end']))} → هفته‌ی {num(weeks(after['sim']['end']))} "
                f"({num(delta_weeks)} هفته)"]
    if cid in after["changes"]:
        st = after["changes"][cid]["stats"]
        internal.append(f"- کارهای جدید: ΣM={num(st['sum_m'])}، بافر {pct(st['final_buf'])} (فقط روی همین کارها)")
    if change.get("remove_tasks"):
        internal.append("- حذف فقط کارهای شروع‌نشده را شامل شد؛ سهم مدیریت پروژه کم نشد.")
    rows = []
    for b, a in zip(before["sheet"], after["sheet"]):
        if a["rounded"] != b["rounded"]:
            rows.append([a["name"], money(b["rounded"]), money(a["rounded"])])
    if rows:
        internal += ["", table(["milestone", "قبل", "بعد"], rows)]
    client = [f"**یادداشت تغییر {cid}:** {change.get('title', '')}", "",
              f"- اثر بر مبلغ: {'افزایش' if (delta_price or 0) >= 0 else 'کاهش'} {money(abs(delta_price) if delta_price is not None else None)} تومان",
              f"- اثر بر زمان: {'بدون تغییر' if abs(delta_weeks) < 0.25 else ('حدود ' + num(round(delta_weeks * 2) / 2) + ' هفته')}",
              f"- اعتبار این قیمت تا {jfmt(validity)}"]
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
    text = {"change": "\n".join(internal), "change-client": "\n".join(client)}
    if args.md:
        write_blocks(args.md, text, f"درخواست تغییر {cid}")
        print(f"ok: یادداشت در {args.md} و برآورد جدید در {args.out} نوشته شد.")
    else:
        print(text["change"] + "\n\n---\n\n" + text["change-client"])
        print(f"\nok: برآورد جدید در {args.out} نوشته شد.")
    return 0


# "داخلی" alone also means "domestic" (داخلی hosting), so only internal-note phrases are listed.
LEAK_TERMS = ["بافر", "حاشیه", "نرخ ساعتی", "نرخ مؤثر", "ساعت", "σ", "Σ", "PERT", "O/M/P", "هدف ۸۰",
              "نیازمند تأیید وکیل", "یادداشت داخلی", "فقط داخلی", "(داخلی", "buffer", "margin", "hourly", "rate"]


def cmd_leakcheck(args):
    with open(args.file, encoding="utf-8") as handle:
        content = handle.read()
    regions = re.findall(r"<!-- client:start -->(.*?)<!-- client:end -->", content, re.DOTALL)
    regions += re.findall(r"<!-- change-client:start -->(.*?)<!-- change-client:end -->", content, re.DOTALL)
    text = "\n".join(regions) if regions else content
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("_") and "estimate.py" in line:
            continue
        for term in LEAK_TERMS:
            if term == "ساعت" and "پلن پشتیبانی" in line:
                continue  # reserved support hours are part of the offer, not internal effort
            if term.lower() in line.lower():
                hits.append((number, term, line.strip()))
    for number, term, line in hits:
        print(f"warning: خط {number}: «{term}» → {line[:160]}")
    if not hits:
        print("ok: واژه‌ی داخلی پیدا نشد.")
        return 0
    print("هر مورد را بررسی کن و از متن کارفرما حذف کن، یا اگر واقعاً بیرونی است دلیلش را بنویس.")
    return 1


def parse_number(cell):
    clean = cell.translate(EN_DIGITS).replace("٬", "").replace(",", "").replace("٫", ".").strip()
    try:
        return float(clean)
    except ValueError:
        return None


def cmd_calibrate(args):
    with open(args.file, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    rows, header = [], None
    for line in lines:
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if any("هدف" in c for c in cells) and any("واقعی" in c for c in cells):
                header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(dict(zip(header, cells)))

    def col(row, word):
        for key, value in row.items():
            if word in key:
                return parse_number(value)
        return None

    data = [(col(r, "هدف"), col(r, "واقعی")) for r in rows]
    data = [(t, a) for t, a in data if t and a is not None]
    n = len(data)
    if n < args.min:
        print(f"داده کافی نیست: {fa(n)} milestone ثبت شده، حداقل {fa(args.min)} لازم است. پیشنهادی نمی‌دهم.")
        return 0
    hit = sum(1 for t, a in data if a <= t) / n
    ratio = sum(a / t for t, a in data) / n
    print(f"تعداد milestone: {fa(n)}؛ نرخ رسیدن به هدف ۸۰٪: {pct(hit)}؛ میانگین واقعی ÷ هدف: {num(ratio, 2)}")
    steps = [d for _, d in MCCONNELL_DIVISORS]
    current = min(steps, key=lambda d: abs(d - args.divisor))
    idx = steps.index(current)
    if hit < 0.7 and idx > 0:
        print(f"پیشنهاد: σ کم برآورد شده. مقسوم‌علیه را از {num(args.divisor)} به {num(steps[idx - 1])} ببر "
              "(فقط با تأیید کاربر).")
    elif hit > 0.9 and idx < len(steps) - 1:
        print(f"پیشنهاد: برآوردها محافظه‌کارانه‌اند. می‌شود مقسوم‌علیه را از {num(args.divisor)} به "
              f"{num(steps[idx + 1])} برد (فقط با تأیید کاربر).")
    else:
        print("نرخ رسیدن به هدف در محدوده‌ی قابل‌قبول است؛ تغییری لازم نیست.")
    return 0


def cmd_date(args):
    if args.action == "today":
        d = today_jalali()
        print(f"{d.isoformat()} ({jfmt(d)})")
        return 0
    if len(args.values) != 2:
        print("error: add و workdays دو مقدار می‌خواهند: JDATE DAYS")
        return 1
    d = parse_jdate(args.values[0])
    days = int(args.values[1].translate(EN_DIGITS))
    if args.action == "add":
        result = d + jdatetime.timedelta(days=days)
    else:
        holidays = load_json(args.holidays) if args.holidays else []
        cal = Calendar(d, holidays, 0)
        result = cal.date_at(cal.add_workdays(0, days, client=False))
    print(f"{result.isoformat()} ({jfmt(result)})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Estimate calculator (project-estimation-assistant)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check")
    p.add_argument("file")
    p.add_argument("--today")
    p = sub.add_parser("run")
    p.add_argument("file")
    p.add_argument("--md")
    p.add_argument("--today")
    p = sub.add_parser("change")
    p.add_argument("base")
    p.add_argument("change")
    p.add_argument("--out", required=True)
    p.add_argument("--md")
    p.add_argument("--today")
    p = sub.add_parser("leakcheck")
    p.add_argument("file")
    p = sub.add_parser("calibrate")
    p.add_argument("file")
    p.add_argument("--divisor", type=float, default=DEFAULTS["sigma_divisor"])
    p.add_argument("--min", type=int, default=10)
    p = sub.add_parser("date")
    p.add_argument("action", choices=["today", "add", "workdays"])
    p.add_argument("values", nargs="*")
    p.add_argument("--holidays")
    args = parser.parse_args(argv)
    handlers = {"check": cmd_check, "run": cmd_run, "change": cmd_change, "leakcheck": cmd_leakcheck,
                "calibrate": cmd_calibrate, "date": cmd_date}
    try:
        return handlers[args.command](args)
    except EstimateError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
