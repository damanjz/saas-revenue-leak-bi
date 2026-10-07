"""Day-by-day simulation of a B2B SaaS business.

Each account carries a hidden health value (0..1). Health drives product usage,
support load, CSAT, expansion, contraction and churn. Planted incidents (see
config.INCIDENTS) push health or churn for a defined set of accounts, and every
churn is labelled with its true cause in the answer key.

State is held in numpy arrays (one slot per account) and stepped one day at a
time, so a full 24-month run takes well under a minute.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from faker import Faker

from . import config as C

INC = {i.kind: i for i in C.INCIDENTS}

NOT_SIGNED, ACTIVE, CHURNED = 0, 1, 2


def _month_starts(start: date, end: date) -> list[date]:
    out, d = [], start.replace(day=1)
    while d <= end:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _iso(d: date, seconds: float) -> str:
    """UTC ISO-8601 timestamp for a day plus an offset in seconds."""
    ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(seconds=float(seconds))
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class Simulation:
    def __init__(self, seed: int = 42, scale: float = 1.0):
        self.rng = np.random.default_rng(seed)
        self.fake = Faker()
        Faker.seed(seed)
        self.scale = scale
        self.days = [C.START + timedelta(days=i) for i in range((C.END - C.START).days + 1)]

        self.stripe_events: list[dict] = []
        self.tickets: list[dict] = []
        self.sessions: list[pd.DataFrame] = []
        self.crm_changes: list[dict] = []
        self.churn_truth: list[dict] = []
        self._event_seq = 0
        self._ticket_seq = 100000
        self._sub_seq = 0
        self._clock: dict[tuple[int, date], int] = {}

        self._build_ams()
        self._build_accounts()
        self._build_am_schedule()

    # ------------------------------------------------------------------ setup
    def _build_ams(self):
        names = set()
        while len(names) < C.N_AMS:
            names.add(self.fake.name())
        self.am_names = sorted(names)
        seg_of_am = {}
        for seg, pool in C.AM_POOLS.items():
            for a in pool:
                seg_of_am[a] = seg
        self.am_segment = [seg_of_am[i] for i in range(C.N_AMS)]

    def _build_accounts(self):
        rng = self.rng
        months = _month_starts(C.START, C.END)
        rows = []
        n_months = len(months)
        for m_idx, m in enumerate(months):
            target = C.SIGNUPS_FIRST_MONTH + (C.SIGNUPS_LAST_MONTH - C.SIGNUPS_FIRST_MONTH) * m_idx / (n_months - 1)
            n_new = rng.poisson(target * self.scale)
            m_end = min((m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), C.END)
            span = (m_end - m).days + 1
            in_partner_window = INC["partner_cohort"].start <= m <= INC["partner_cohort"].end
            for _ in range(n_new):
                rows.append((m + timedelta(days=int(rng.integers(0, span))), in_partner_window))
        rows.sort(key=lambda r: r[0])

        n = len(rows)
        self.n = n
        self.signup = np.array([(r[0] - C.START).days for r in rows])
        self.partner_window = np.array([r[1] for r in rows])
        self.segment = rng.choice(3, size=n, p=C.SEGMENT_MIX)
        self.plan = np.array([rng.choice(3, p=C.PLAN_MIX[C.SEGMENTS[s]]) for s in self.segment])
        lo = np.array([C.SEATS_RANGE[C.SEGMENTS[s]][0] for s in self.segment])
        hi = np.array([C.SEATS_RANGE[C.SEGMENTS[s]][1] for s in self.segment])
        self.seats0 = rng.integers(lo, hi + 1)
        self.seats = np.zeros(n, dtype=int)
        self.annual = rng.random(n) < np.array([C.ANNUAL_SHARE[C.SEGMENTS[s]] for s in self.segment])
        self.anchor = np.array([min(self.days[d].day, 28) for d in self.signup])
        self.region = rng.choice(len(C.REGIONS), size=n, p=C.REGION_MIX)
        self.industry = rng.integers(0, len(C.INDUSTRIES), size=n)
        partner_p = np.where(self.partner_window, INC["partner_cohort"].params["partner_share"], 0.10)
        u = rng.random(n)
        self.channel = np.where(u < partner_p, 2, np.where(u < partner_p + (1 - partner_p) * 0.55, 0, 1))
        self.reports_heavy = rng.random(n) < 0.35
        self.health_base0 = np.clip(rng.beta(6, 2.2, size=n), 0.25, 0.97)
        self.health = self.health_base0.copy()
        self.status = np.full(n, NOT_SIGNED)
        self.unit_price = np.zeros(n)          # locked list price (USD/seat/month) on current sub
        self.sub_id = np.array([""] * n, dtype=object)
        self.churn_day = np.full(n, -1)
        self.price_shock_left = np.zeros(n, dtype=int)   # remaining renewals with sticker-shock churn
        self.price_migrated = np.zeros(n, dtype=bool)
        self.neglect = np.zeros(n)
        self.partner_decay = np.zeros(n)
        self.release_hit = np.zeros(n)
        self.employees = np.array([
            int(rng.integers(*{"SMB": (10, 200), "Mid-Market": (200, 2000), "Enterprise": (2000, 40000)}[C.SEGMENTS[s]]))
            for s in self.segment
        ])
        self.company = [self.fake.unique.company() for _ in range(n)]
        self.domain = [
            "".join(ch for ch in c.lower().split(",")[0].split(" ")[0] if ch.isalnum()) + f"{i}.com"
            for i, c in enumerate(self.company)
        ]
        self.account_id = [f"ACC-{10000 + i}" for i in range(n)]
        self.stripe_customer = [f"cus_{rng.integers(16**11, 16**12):012x}" for _ in range(n)]

    def _build_am_schedule(self):
        """Pre-plan AM assignments: initial owner, random reassignments, and a
        territory realignment on 2026-01-01."""
        rng = self.rng
        realign_day = (date(2026, 1, 1) - C.START).days
        self.am = np.zeros(self.n, dtype=int)
        self.am_changes: dict[int, list[tuple[int, int]]] = {}
        for i in range(self.n):
            pool = C.AM_POOLS[C.SEGMENTS[self.segment[i]]]
            first = int(rng.choice(pool))
            self.am[i] = first
            self.am_changes.setdefault(int(self.signup[i]), []).append((i, first))
            current = first
            change_days = []
            if rng.random() < 0.20 and self.signup[i] < len(self.days) - 60:
                change_days.append(int(rng.integers(self.signup[i] + 30, len(self.days))))
            if self.segment[i] == 1 and self.signup[i] < realign_day and rng.random() < 0.30:
                change_days.append(realign_day)
            for d in sorted(change_days):
                options = [a for a in pool if a != current]
                current = int(rng.choice(options))
                self.am_changes.setdefault(d, []).append((i, current))

    # --------------------------------------------------------------- helpers
    def _price_for(self, plan_idx: int, day: date) -> float:
        p = C.PLANS[plan_idx]
        inc = INC["price_hike"]
        if p == inc.params["plan"] and day >= inc.start:
            return float(inc.params["new_price"])
        return float(C.LIST_PRICE[p])

    def _mrr_cents(self, i: int) -> int:
        monthly = self.seats[i] * self.unit_price[i] * (1 - C.ANNUAL_DISCOUNT if self.annual[i] else 1)
        return int(round(monthly * 100))

    def _emit(self, i: int, day: date, etype: str, reason: str, prev: dict | None = None):
        self._event_seq += 1
        # events for the same account on the same day keep their causal order
        last = self._clock.get((i, day), 0)
        sec = max(int(self.rng.integers(6 * 3600, 20 * 3600)), last + 60)
        self._clock[(i, day)] = sec
        unit = self.unit_price[i] * (12 * (1 - C.ANNUAL_DISCOUNT) if self.annual[i] else 1)
        self.stripe_events.append({
            "event_id": f"evt_{self._event_seq:08d}{self.rng.integers(0, 16**6):06x}",
            "event_type": etype,
            "occurred_at": _iso(day, sec),
            "customer": self.stripe_customer[i],
            "subscription_id": self.sub_id[i],
            "plan_id": C.PLANS[self.plan[i]],
            "billing_interval": "year" if self.annual[i] else "month",
            "quantity": int(self.seats[i]),
            "unit_amount_cents": int(round(unit * 100)),
            "mrr_cents": 0 if etype == "customer.subscription.deleted" else self._mrr_cents(i),
            "previous_plan_id": prev.get("plan") if prev else None,
            "previous_quantity": prev.get("quantity") if prev else None,
            "previous_mrr_cents": prev.get("mrr") if prev else None,
            "change_reason": reason,
            "amount_paid_cents": None,
        })

    def _invoice(self, i: int, day: date):
        self._event_seq += 1
        amount = self._mrr_cents(i) * (12 if self.annual[i] else 1)
        self.stripe_events.append({
            "event_id": f"evt_{self._event_seq:08d}{self.rng.integers(0, 16**6):06x}",
            "event_type": "invoice.paid",
            "occurred_at": _iso(day, self.rng.integers(0, 6 * 3600)),
            "customer": self.stripe_customer[i],
            "subscription_id": self.sub_id[i],
            "plan_id": C.PLANS[self.plan[i]],
            "billing_interval": "year" if self.annual[i] else "month",
            "quantity": int(self.seats[i]),
            "unit_amount_cents": None,
            "mrr_cents": None,
            "previous_plan_id": None, "previous_quantity": None, "previous_mrr_cents": None,
            "change_reason": None,
            "amount_paid_cents": amount,
        })

    def _crm(self, i: int, day: date, **changes):
        lag = int(self.rng.integers(0, 4))
        if self.rng.random() < C.DIRTY["crm_late_record_rate"]:
            lag = int(self.rng.integers(30, 60))          # late-arriving dimension change
        self.crm_changes.append({
            "account_id": self.account_id[i],
            "effective_at": day.isoformat(),
            "recorded_at": (day + timedelta(days=lag)).isoformat(),
            **changes,
        })

    def _snapshot(self, i):
        return {"plan": C.PLANS[self.plan[i]], "quantity": int(self.seats[i]), "mrr": self._mrr_cents(i)}

    def _start_subscription(self, i: int, day: date, reason: str):
        self._sub_seq += 1
        self.sub_id[i] = f"sub_{self._sub_seq:06d}{self.rng.integers(0, 16**4):04x}"
        self.unit_price[i] = self._price_for(self.plan[i], day)
        self.price_migrated[i] = day >= INC["price_hike"].start
        self.status[i] = ACTIVE
        self._emit(i, day, "customer.subscription.created", reason)
        self._invoice(i, day)

    def _churn_cause(self, i: int, day: date) -> str:
        if self.price_shock_left[i] > 0:
            return INC["price_hike"].id
        hits = {
            INC["am_neglect"].id: self.neglect[i],
            INC["partner_cohort"].id: self.partner_decay[i],
            INC["bad_release"].id: self.release_hit[i],
        }
        cause, size = max(hits.items(), key=lambda kv: kv[1])
        return cause if size >= 0.08 else "organic"

    # ------------------------------------------------------------- main loop
    def run(self):
        rng = self.rng
        p_inc = INC["price_hike"].params
        r_inc = INC["bad_release"]
        a_inc = INC["am_neglect"]
        c_inc = INC["partner_cohort"]
        size_bands = [(0, 50, "1-50"), (51, 200, "51-200"), (201, 1000, "201-1000"),
                      (1001, 5000, "1001-5000"), (5001, 10**9, "5000+")]

        def band(e):
            return next(b for lo, hi, b in size_bands if lo <= e <= hi)

        for t, day in enumerate(self.days):
            weekday = day.weekday() < 5

            # --- AM reassignments and CRM change log
            for i, am in self.am_changes.get(t, []):
                self.am[i] = am
                if t == self.signup[i]:
                    continue  # written with the creation record below
                self._crm(i, day, change_type="am_reassignment", account_manager=self.am_names[am])

            # --- signups
            for i in np.where((self.signup == t) & (self.status == NOT_SIGNED))[0]:
                self.seats[i] = self.seats0[i]
                self._start_subscription(i, day, "new")
                self._crm(
                    i, day, change_type="created",
                    company_name=self.company[i], domain=self.domain[i],
                    industry=C.INDUSTRIES[self.industry[i]], region=C.REGIONS[self.region[i]],
                    segment=C.SEGMENTS[self.segment[i]], employee_count=int(self.employees[i]),
                    company_size_band=band(self.employees[i]),
                    cs_tier={0: "Tech-touch", 1: "Pooled", 2: "Named"}[self.segment[i]],
                    acquisition_channel=C.CHANNELS[self.channel[i]],
                    account_manager=self.am_names[self.am[i]],
                )

            active = self.status == ACTIVE

            # --- incident pressure on health
            if day >= a_inc.start:
                on_book = active & (self.am == a_inc.params["am_index"])
                self.neglect = np.where(on_book,
                                        np.minimum(self.neglect + a_inc.params["ramp_per_day"], a_inc.params["max_health_hit"]),
                                        np.maximum(self.neglect - a_inc.params["ramp_per_day"], 0))
            tenure = t - self.signup
            decaying = active & self.partner_window & (self.channel == 2) & (tenure > c_inc.params["decay_start_days"])
            self.partner_decay = np.where(decaying,
                                          np.minimum(self.partner_decay + c_inc.params["decay_per_day"],
                                                     np.maximum(self.health_base0 - c_inc.params["floor"], 0)),
                                          self.partner_decay)
            bug_on = r_inc.start <= day <= r_inc.end
            if bug_on:
                hit = active & self.reports_heavy
                self.release_hit = np.where(hit, np.minimum(self.release_hit + r_inc.params["health_hit"] / 21, r_inc.params["health_hit"]), self.release_hit)
            elif day > r_inc.end:
                self.release_hit = np.maximum(self.release_hit - 0.002, 0)

            base = np.clip(self.health_base0 - self.neglect - self.partner_decay - self.release_hit, 0.03, 0.99)
            self.health = np.clip(self.health + 0.05 * (base - self.health) + rng.normal(0, 0.015, self.n), 0.02, 0.99)

            # --- billing anniversaries: renewals, seat and plan changes, churn
            billing = active & (self.anchor == day.day) & (self.signup < t)
            for i in np.where(billing)[0]:
                self._billing_day(i, t, day, p_inc)

            # --- reactivations (checked monthly on the anchor day)
            lapsed = (self.status == CHURNED) & (self.anchor == day.day)
            for i in np.where(lapsed)[0]:
                months_out = (t - self.churn_day[i]) / 30.4
                if 0.5 < months_out <= C.REACTIVATION_WINDOW_MONTHS and rng.random() < C.REACTIVATION_MONTHLY_P:
                    self.seats[i] = max(1, int(round(self.seats0[i] * rng.uniform(0.5, 1.0))))
                    self.health[i] = min(0.95, self.health[i] + 0.25)
                    self.price_shock_left[i] = 0
                    self._start_subscription(i, day, "reactivation")

            # --- occasional company-size changes in CRM
            if day.day == 15:
                grow = np.where(active & (rng.random(self.n) < 0.012))[0]
                for i in grow:
                    old = band(self.employees[i])
                    self.employees[i] = int(self.employees[i] * rng.uniform(1.3, 2.5))
                    if band(self.employees[i]) != old:
                        self._crm(i, day, change_type="company_size_change",
                                  employee_count=int(self.employees[i]), company_size_band=band(self.employees[i]))

            active = self.status == ACTIVE
            self._support_day(t, day, active, bug_on, r_inc)
            self._telemetry_day(day, active, weekday, bug_on, r_inc)

        return self

    def _billing_day(self, i: int, t: int, day: date, p_inc: dict):
        rng = self.rng
        seg = C.SEGMENTS[self.segment[i]]
        months_on = round((t - self.signup[i]) / 30.4)
        is_renewal = (not self.annual[i]) or (months_on > 0 and months_on % 12 == 0)
        h = self.health[i]

        if is_renewal:
            # price migration for existing Starter subs after the hike
            if (C.PLANS[self.plan[i]] == p_inc["plan"] and not self.price_migrated[i]
                    and day >= INC["price_hike"].start):
                prev = self._snapshot(i)
                self.price_migrated[i] = True
                self.unit_price[i] = float(p_inc["new_price"])
                self.price_shock_left[i] = len(p_inc["shock_churn_p"])
                self._emit(i, day, "customer.subscription.updated", "price_change", prev)

            if self.annual[i]:
                p = C.CHURN_BASE_ANNUAL * np.exp(4 * (0.65 - h))
            else:
                p = C.CHURN_BASE_MONTHLY[seg] * np.exp(4 * (0.65 - h))
            if self.price_shock_left[i] > 0:
                k = len(p_inc["shock_churn_p"]) - self.price_shock_left[i]
                p += p_inc["shock_churn_p"][k]
            if rng.random() < min(p, 0.95):
                self.churn_truth.append({
                    "account_id": self.account_id[i],
                    "subscription_id": self.sub_id[i],
                    "churn_date": day.isoformat(),
                    "true_cause": self._churn_cause(i, day),
                    "health_at_churn": round(float(h), 3),
                })
                self._emit(i, day, "customer.subscription.deleted", "cancelled")
                self.status[i] = CHURNED
                self.churn_day[i] = t
                self.price_shock_left[i] = 0
                return
            if self.price_shock_left[i] > 0:
                self.price_shock_left[i] -= 1

        # seat / plan changes (contraction only allowed at renewal for annual)
        prev = self._snapshot(i)
        changed = False
        if rng.random() < min(0.05 * (h / 0.7) ** 3, 0.25):
            self.seats[i] += max(1, int(round(self.seats[i] * rng.uniform(0.1, 0.35))))
            changed = True
        elif is_renewal and rng.random() < min(0.03 * ((1 - h) / 0.3) ** 2, 0.30) and self.seats[i] > 1:
            self.seats[i] = max(1, self.seats[i] - max(1, int(round(self.seats[i] * rng.uniform(0.1, 0.3)))))
            changed = True
        if h > 0.70 and self.plan[i] < 2 and rng.random() < 0.015:
            self.plan[i] += 1
            self.unit_price[i] = self._price_for(self.plan[i], day)
            changed = True
        elif is_renewal and h < 0.45 and self.plan[i] > 0 and rng.random() < 0.02:
            self.plan[i] -= 1
            self.unit_price[i] = self._price_for(self.plan[i], day)
            changed = True
        if changed:
            reason = "plan_change" if prev["plan"] != C.PLANS[self.plan[i]] else "seat_change"
            self._emit(i, day, "customer.subscription.updated", reason, prev)
            new_tier = "Strategic" if self._mrr_cents(i) >= 1_500_000 else None
            if new_tier and prev["mrr"] < 1_500_000:
                self._crm(i, day, change_type="tier_change", cs_tier=new_tier)
        if is_renewal:
            self._invoice(i, day)

    def _support_day(self, t: int, day: date, active, bug_on: bool, r_inc):
        rng = self.rng
        lam = np.where(active, 0.005 * self.seats ** 0.6 * (1 + 2.5 * (1 - self.health)), 0)
        bug_lam = np.where(active & self.reports_heavy & bug_on, r_inc.params["extra_ticket_rate"], 0)
        n_reg = rng.poisson(lam)
        n_bug = rng.poisson(bug_lam)
        cats = ["how-to", "billing", "integration", "bug", "feature-request", "account-access"]
        cat_p = [0.34, 0.14, 0.17, 0.17, 0.10, 0.08]
        for i in np.where((n_reg + n_bug) > 0)[0]:
            for k in range(n_reg[i] + n_bug[i]):
                is_bug = k >= n_reg[i]
                h = self.health[i]
                if is_bug:
                    category, tags = "bug", "bug;reports;release-5.2"
                    priority = rng.choice(["normal", "high", "urgent"], p=[0.25, 0.45, 0.30])
                else:
                    category = rng.choice(cats, p=cat_p)
                    tags = category
                    pu = 0.03 + 0.10 * (1 - h)            # unhealthy accounts escalate more
                    priority = rng.choice(["low", "normal", "high", "urgent"],
                                          p=[0.25, 1 - 0.25 - (0.06 + pu) - pu, 0.06 + pu, pu])
                created = rng.integers(0, 86400)
                frt_min = rng.lognormal(np.log({"urgent": 20, "high": 60, "normal": 180, "low": 480}[priority]), 0.6)
                res_h = rng.lognormal(np.log(C.SLA_HOURS[priority] * 0.6), 0.7) * (1.6 if is_bug else 1)
                solved = created + res_h * 3600
                answered = rng.random() < 0.6
                slow = res_h / C.SLA_HOURS[priority]
                csat = None
                if answered:
                    score = 1 + 4 * (0.25 + 0.6 * h - 0.18 * min(slow, 2.5) - (0.15 if is_bug else 0)) + rng.normal(0, 0.6)
                    csat = int(np.clip(round(score), 1, 5))
                still_open = day + timedelta(seconds=float(solved)) > C.END + timedelta(days=1)
                self._ticket_seq += 1
                self.tickets.append({
                    "ticket_id": self._ticket_seq,
                    "organization_external_id": self.account_id[i],
                    "requester_email": f"{self.fake.user_name()}@{self.domain[i]}",
                    "created_at": _iso(day, created),
                    "first_response_at": _iso(day, created + frt_min * 60),
                    "solved_at": None if still_open else _iso(day, solved),
                    "status": "open" if still_open else "solved",
                    "priority": priority,
                    "channel": rng.choice(["email", "chat", "web", "phone"], p=[0.45, 0.30, 0.20, 0.05]),
                    "category": category,
                    "tags": tags,
                    "csat_score": csat,
                })

    def _telemetry_day(self, day: date, active, weekday: bool, bug_on: bool, r_inc):
        rng = self.rng
        p_active = np.where(active, (0.05 + 0.35 * self.health) * (1.0 if weekday else 0.15), 0)
        users = rng.binomial(np.where(active, self.seats, 0), p_active)
        total = int(users.sum())
        if total == 0:
            return
        acct = np.repeat(np.arange(self.n), users)
        h = self.health[acct]
        seats = self.seats[acct]
        user_idx = rng.integers(0, seats)
        start = rng.normal(13 * 3600, 3 * 3600, total).clip(0, 86000)
        dur = rng.lognormal(np.log(600 * (0.5 + h)), 0.7)
        rep_rate = np.where(self.reports_heavy[acct], 2.5, 0.4) * (r_inc.params["usage_multiplier"] if bug_on else 1.0)
        api_rate = np.where(self.segment[acct] == 2, 18, np.where(self.segment[acct] == 1, 6, 1))
        base_ts = np.datetime64(day.isoformat()) .astype("datetime64[s]")
        start_ts = base_ts + start.astype("timedelta64[s]")
        self.sessions.append(pd.DataFrame({
            "account_id": np.array(self.account_id, dtype=object)[acct],
            "user_id": [f"u_{a}_{u}" for a, u in zip(acct, user_idx)],
            "session_start": start_ts,
            "session_end": start_ts + dur.astype("timedelta64[s]"),
            "dashboards_viewed": rng.poisson(1 + 3 * h),
            "reports_run": rng.poisson(rep_rate),
            "integrations_synced": rng.poisson(0.5, total),
            "exports": rng.poisson(0.3 + 0.4 * h),
            "api_calls": rng.poisson(api_rate),
        }))

    # ---------------------------------------------------------------- output
    def frames(self) -> dict[str, pd.DataFrame]:
        sessions = pd.concat(self.sessions, ignore_index=True)
        sessions.insert(0, "session_id", [f"s_{i:09d}" for i in range(len(sessions))])
        stripe_customers = pd.DataFrame({
            "customer": self.stripe_customer,
            "email": [f"billing@{d}" for d in self.domain],
            "metadata_account_id": self.account_id,
            "created": [_iso(self.days[s], 0) for s in self.signup],
        })
        return {
            "stripe_events": pd.DataFrame(self.stripe_events),
            "stripe_customers": stripe_customers,
            "zendesk_tickets": pd.DataFrame(self.tickets),
            "telemetry_sessions": sessions,
            "crm_account_changes": pd.DataFrame(self.crm_changes),
            "churn_truth": pd.DataFrame(self.churn_truth),
            "account_truth": pd.DataFrame({
                "account_id": self.account_id,
                "reports_heavy": self.reports_heavy,
                "partner_launch_cohort": self.partner_window & (self.channel == 2),
                "am_name_final": [self.am_names[a] for a in self.am],
            }),
        }
