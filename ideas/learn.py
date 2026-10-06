"""What your 1-10 ratings teach the idea finder.

    python -m ideas.learn          (also /taste in Telegram)

The model is a ridge regression over the traits of each idea: category, business
model, sales channel, customer type, geography, research lens, startup cost,
automation share, time to first revenue, profit ceiling and topic tags. Each
trait gets a weight ("Micro-SaaS: +1.3 points"), shrunk towards zero until
enough ratings back it, so one rating is a nudge, not a verdict.

The weights are used three ways:
1. ranking: each day's candidates are sorted by the rating the model expects
   from you, blended with Claude's own score while there are few ratings;
2. prompting: the strongest likes and dislikes, your best and worst rated
   ideas and your notes go into the next day's prompt;
3. lens choice: the research angle whose ideas you rate best is used more.

The report shows whether it's working: the gap between the rating it predicted
and the one you gave, early ideas against recent ones.
"""
from __future__ import annotations

import math
import random
import sys
from collections import Counter, defaultdict
from statistics import mean

NEUTRAL = 5.5             # middle of the 1-10 scale
RIDGE = 3.0               # shrinkage: higher = more ratings needed before a trait counts
TAG_WEIGHT = 0.5          # a topic tag counts half as much as a structured trait
MAX_TAGS = 60             # only the most common tags become features
VARIETY_GAP = 2.0         # a different category may cost at most this much expected rating
WILDCARD_AFTER = 5        # ratings before one idea a day is reserved for exploring
TRUST_RATINGS = 8         # model and Claude's own score weigh equally at this many ratings
CATEGORICAL = ("category", "business_model", "channel", "customer_type", "geography", "lens")
LABEL = {"category": "Category", "business_model": "Business model", "channel": "Sales channel",
         "customer_type": "Customer", "geography": "Market", "lens": "Research lens", "cost": "Startup cost",
         "automation": "Automation", "speed": "First revenue", "ceiling": "Profit ceiling", "tag": "Topic"}


def _bucket(value, edges: list[tuple[float, str]], last: str) -> str | None:
    if value is None:
        return None
    for edge, label in edges:
        if value <= edge:
            return label
    return last


def features(idea: dict) -> dict[str, float]:
    """The traits of one idea as {feature: value}."""
    f: dict[str, float] = {}
    for key in CATEGORICAL:
        if idea.get(key):
            f[f"{key}={idea[key]}"] = 1.0
    buckets = {
        "cost": _bucket(idea.get("startup_cost_eur"), [(500, "≤ €500"), (2000, "€501-2,000")], "over €2,000"),
        "automation": _bucket(idea.get("automation_pct"), [(59, "under 60%"), (79, "60-79%")], "80% or more"),
        "speed": _bucket(idea.get("weeks_to_revenue"), [(3, "within 3 weeks"), (8, "4-8 weeks")], "over 8 weeks"),
        "ceiling": _bucket(idea.get("profit_high_eur"), [(150_000, "up to €150k a year"),
                                                         (1_000_000, "€150k-1M a year")], "over €1M a year"),
    }
    for key, b in buckets.items():
        if b:
            f[f"{key}={b}"] = 1.0
    for t in {str(t).strip().lower() for t in idea.get("tags") or [] if str(t).strip()}:
        f[f"tag={t}"] = TAG_WEIGHT
    return f


def describe(feature: str) -> str:
    key, _, value = feature.partition("=")
    return f"{LABEL.get(key, key)}: {value}"


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (a is symmetric positive definite)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        for r in range(c + 1, n):
            k = m[r][c] / piv
            if k:
                row_r, row_c = m[r], m[c]
                for j in range(c, n + 1):
                    row_r[j] -= k * row_c[j]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][j] * x[j] for j in range(r + 1, n))) / m[r][r]
    return x


class Model:
    """Expected rating = mean rating + sum of the weights of the idea's traits."""

    def __init__(self, rated: list[dict]):
        self.n = len(rated)
        self.mean = mean(r["rating"] for r in rated) if rated else NEUTRAL
        self.weights: dict[str, float] = {}
        self.counts: Counter = Counter()
        rows = [features(r) for r in rated]
        tag_counts = Counter(k for row in rows for k in row if k.startswith("tag="))
        keep_tags = {t for t, _ in tag_counts.most_common(MAX_TAGS)}
        rows = [{k: v for k, v in row.items() if not k.startswith("tag=") or k in keep_tags} for row in rows]
        for row in rows:
            self.counts.update(row.keys())
        names = sorted(self.counts)
        if not names:
            return
        index = {k: i for i, k in enumerate(names)}
        d = len(names)
        xtx = [[0.0] * d for _ in range(d)]
        xty = [0.0] * d
        for row, r in zip(rows, rated):
            y = r["rating"] - self.mean
            items = [(index[k], v) for k, v in row.items()]
            for i, vi in items:
                xty[i] += vi * y
                for j, vj in items:
                    xtx[i][j] += vi * vj
        for i in range(d):
            xtx[i][i] += RIDGE
        self.weights = dict(zip(names, _solve(xtx, xty)))

    def contributions(self, idea: dict) -> list[tuple[str, float]]:
        out = [(k, self.weights[k] * v) for k, v in features(idea).items() if k in self.weights]
        return sorted(out, key=lambda kv: -abs(kv[1]))

    def predict(self, idea: dict) -> float:
        return max(1.0, min(10.0, self.mean + sum(c for _, c in self.contributions(idea))))

    def novelty(self, idea: dict) -> float:
        """1.0 for a kind of idea you've never rated, falling towards 0 as ratings pile up."""
        seen = [self.counts.get(f"{k}={idea.get(k)}", 0) for k in ("category", "business_model", "channel")]
        return 1 / math.sqrt(1 + mean(seen))

    def score(self, idea: dict, exploration: float) -> float:
        """Ranking score: the model's guess, trusted more as ratings come in, plus a small bonus for the unknown."""
        trust = self.n / (self.n + TRUST_RATINGS)
        own = idea.get("self_score") or NEUTRAL
        return trust * self.predict(idea) + (1 - trust) * own + exploration * self.novelty(idea)

    def why(self, idea: dict, limit: int = 2) -> str:
        parts = [f"{describe(k)} {c:+.1f}" for k, c in self.contributions(idea) if abs(c) >= 0.3][:limit]
        return ", ".join(parts)

    def top(self, positive: bool, limit: int = 6, min_abs: float = 0.3) -> list[tuple[str, float, int]]:
        ws = [(k, w, self.counts[k]) for k, w in self.weights.items() if (w > 0) == positive and abs(w) >= min_abs]
        return sorted(ws, key=lambda x: -abs(x[1]))[:limit]


def pick(candidates: list[dict], model: Model, k: int, exploration: float, max_per_category: int
         ) -> list[tuple[dict, float, bool]]:
    """Today's batch: one wildcard plus the best by expected rating (varied categories).

    The wildcard is the idea of the kind you've rated least (Claude's score at least 6), so the model
    keeps learning about things it hasn't shown you. Until WILDCARD_AFTER ratings everything is new
    to it anyway, so there's no wildcard and the whole batch is ranked.
    Returns [(idea, predicted rating, is_wildcard)].
    """
    if k < 1:
        return []
    out: list[tuple[dict, float, bool]] = []
    if k > 1 and model.n >= WILDCARD_AFTER:
        strong = [c for c in candidates if (c.get("self_score") or 0) >= 6]
        if strong:
            w = max(strong, key=lambda c: (model.novelty(c), c.get("self_score") or 0))
            out.append((w, model.predict(w), True))
    ranked = sorted(candidates, key=lambda c: -model.score(c, exploration))
    per_cat: Counter = Counter(o[0].get("category") for o in out)
    floor = model.score(ranked[0], exploration) - VARIETY_GAP if ranked else 0
    for c in ranked:
        if len(out) >= k or model.score(c, exploration) < floor:
            break
        if all(c is not o[0] for o in out) and per_cat[c.get("category")] < max_per_category:
            out.append((c, model.predict(c), False))
            per_cat[c.get("category")] += 1
    for c in ranked:                                   # too few: fill up without the variety rule
        if len(out) >= k:
            break
        if all(c is not o[0] for o in out):
            out.append((c, model.predict(c), False))
    # Best first, wildcard last.
    return sorted(out, key=lambda o: (o[2], -model.score(o[0], exploration)))


def choose_lenses(lenses: dict[str, str], rated: list[dict], all_ideas: list[dict], rng: random.Random
                  ) -> list[str]:
    """Two lenses: the best rated (shrunk average, unseen counts as neutral) and the least used of the rest."""
    names = list(lenses)
    if len(names) <= 2:
        return names
    by_lens = defaultdict(list)
    for r in rated:
        by_lens[r.get("lens")].append(r["rating"])
    used = Counter(i.get("lens") for i in all_ideas)

    def shrunk(name):
        rs = by_lens.get(name, [])
        return (sum(rs) + NEUTRAL * 3) / (len(rs) + 3)
    best = max(names, key=lambda n: (shrunk(n), -used[n], rng.random()))
    rest = [n for n in names if n != best]
    least = min(used[n] for n in rest)
    second = rng.choice([n for n in rest if used[n] == least])
    return [best, second]


def prompt_brief(rated: list[dict], model: Model) -> str:
    """What the next prompt should know about your taste."""
    if not rated:
        return "No ratings yet: this is an early run, so vary the kinds of ideas widely."
    lines = [f"{len(rated)} ideas rated so far, average {model.mean:.1f}/10."]
    likes, dislikes = model.top(True), model.top(False)
    if likes:
        lines.append("Rates higher: " + "; ".join(f"{describe(k)} ({w:+.1f})" for k, w, _ in likes))
    if dislikes:
        lines.append("Rates lower: " + "; ".join(f"{describe(k)} ({w:+.1f})" for k, w, _ in dislikes))
    by_score = sorted(rated, key=lambda r: (-r["rating"], r.get("rated_at") or ""))
    best = [r for r in by_score if r["rating"] >= 7][:5]
    worst = [r for r in reversed(by_score) if r["rating"] <= 4][:5]
    if best:
        lines.append("\nHighest rated:")
        lines += [f"- {r['rating']}/10 {r['title']}: {r.get('one_liner') or ''}"
                  + (f" (founder's note: {r['note']})" if r.get("note") else "") for r in best]
    if worst:
        lines.append("\nLowest rated:")
        lines += [f"- {r['rating']}/10 {r['title']}: {r.get('one_liner') or ''}"
                  + (f" (founder's note: {r['note']})" if r.get("note") else "") for r in worst]
    notes = [r for r in rated[-15:] if r.get("note") and r not in best and r not in worst]
    if notes:
        lines.append("\nOther recent notes from the founder:")
        lines += [f"- on '{r['title']}' ({r['rating']}/10): {r['note']}" for r in notes]
    return "\n".join(lines)


def accuracy(rated: list[dict]) -> tuple[float, int, float | None, int]:
    """Mean gap between predicted and actual rating: (first half, n, recent half, n)."""
    with_pred = [r for r in rated if r.get("predicted") is not None]
    if len(with_pred) < 4:
        return (math.nan, len(with_pred), None, 0)
    half = len(with_pred) // 2
    early, recent = with_pred[:half], with_pred[half:]
    gap = lambda rs: mean(abs(r["rating"] - r["predicted"]) for r in rs)   # noqa: E731
    return (gap(early), len(early), gap(recent), len(recent))


def report(conn=None) -> str:
    from . import store
    conn = conn or store.connect()
    rated = store.rated(conn)
    if not rated:
        return ("No ratings yet. Rate the ideas 1-10 with the buttons under each one; "
                "after a handful I'll show what I've learned here.")
    model = Model(rated)
    lines = [f"What your ratings taught me: {len(rated)} rated, average {model.mean:.1f}/10", ""]
    if len(rated) >= 10:
        first, last = rated[:len(rated) // 2], rated[len(rated) // 2:]
        lines.append(f"Average rating, first {len(first)}: {mean(r['rating'] for r in first):.1f} · "
                     f"latest {len(last)}: {mean(r['rating'] for r in last):.1f}")
    e, ne, rcent, nr = accuracy(rated)
    if rcent is not None:
        lines.append(f"Prediction gap (lower = knows you better): earlier {e:.1f} pts ({ne}) · "
                     f"recent {rcent:.1f} pts ({nr})")
    for title, positive in (("👍 You rate these higher:", True), ("👎 You rate these lower:", False)):
        top = model.top(positive)
        if top:
            lines += ["", title] + [f"  {describe(k)}  {w:+.1f}  ({n} rated)" for k, w, n in top]
    lens_rs = defaultdict(list)
    for r in rated:
        lens_rs[r.get("lens") or "?"].append(r["rating"])
    if lens_rs:
        lines += ["", "Research lenses:"] + [f"  {name}: {mean(rs):.1f} avg ({len(rs)})"
                                             for name, rs in sorted(lens_rs.items(), key=lambda kv: -mean(kv[1]))]
    trust = len(rated) / (len(rated) + TRUST_RATINGS)
    lines += ["", f"Ranking now: {trust:.0%} your learned taste, {1 - trust:.0%} Claude's own score."]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(report())
