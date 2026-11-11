# Roblox Up-and-Coming forecast (Python 3.13, standard library only).
#
# Reads /input/history.ndjson (every read of the Up-and-Coming chart, oldest
# first) and /input/task.json (the scored window, places, output file), and
# writes /output/forecast.json: one probability per (game, place) pair and a
# top 10 in order. The program runs twice on the same history and must write
# the same bytes both times, so every random draw comes from a fixed seed.
#
# Model: a Monte Carlo of the chart's rank dynamics. Each game carries a
# latent score (its current rank, plus a drift from its recent rank velocity
# that fades with a 24-hour half-life). Every simulated hour each score moves
# by its drift plus Gaussian noise whose per-hour size is calibrated from the
# history's consecutive reads; the scores are re-sorted into ranks, games
# that age past 28 days leave the chart (their `created` date comes from the
# history's details when the recorder supplies them), and brand-new games
# enter at ranks drawn from the history's entry-rank distribution. A game's
# probability at a place is its share of simulated slots spent there, so the
# forecast is honest about how fast this chart moves: the archive's top 10
# turns over by a median of 6 places between reads a few hours apart, and
# the close's top 10 holds only about half its places by the end of a 72-hour
# window. Brand-new games are never named (they do not exist yet); the mass
# they take at a place is left unnamed, as the rules score it, with a small
# floor so no place can ever score the -9.21 log floor on a newcomer.
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

SEED = 20261113
N_SIMS = 400
DRIFT_HALF_LIFE_H = 24.0
UNNAMED_FLOOR = 0.2
# The pooled per-hour rank noise is multiplied by this: the raw estimate
# averages over stable games and the re-sort compresses score moves into
# rank moves, so the chart wanders faster than the naive estimate. Tuned by
# backtest on three cutoffs of the practice history (floor 0.2 / x2.0:
# mean log -40.0, vs -52 to -56 for the no-change baseline).
SIGMA_MULT = 2.0
MAX_ROWS_PER_PLACE = 100


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_history(input_dir):
    task = json.load(open(os.path.join(input_dir, "task.json"), encoding="utf-8"))
    reads = []
    with open(os.path.join(input_dir, "history.ndjson"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                reads.append(json.loads(line))
    reads.sort(key=lambda r: r["at"])
    return task, reads


def chart_games(read):
    """The chart of one read, in rank order: (universe_id, rank, playing)."""
    out = []
    for g in sorted(read.get("up_and_coming") or [], key=lambda x: x["rank"]):
        out.append((int(g["universe_id"]), int(g["rank"]), g.get("playing")))
    return out


def calibrate(reads):
    """Per-hour rank noise and the entry-rank distribution, from the history.

    Rank deltas between consecutive reads are pooled after dividing by the
    square root of the hours between the reads: if ranks wandered as a random
    walk, the delta over h hours has stdev sigma*sqrt(h).
    """
    deltas = []  # (delta / sqrt(hours), hours)
    entry_ranks = []
    seen = set()
    prev = None
    prev_at = None
    for read in reads:
        at = parse_time(read["at"])
        games = chart_games(read)
        for uid, rank, _playing in games:
            if uid not in seen:
                entry_ranks.append(rank)
            seen.add(uid)
        if prev is not None:
            hours = max(0.25, (at - prev_at).total_seconds() / 3600.0)
            cur = {uid: rank for uid, rank, _ in games}
            for uid, rank in prev.items():
                if uid in cur:
                    deltas.append((cur[uid] - rank) / math.sqrt(hours))
        prev = {uid: rank for uid, rank, _ in games}
        prev_at = at
    if deltas:
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        sigma = math.sqrt(var) * SIGMA_MULT
    else:
        sigma = 3.0 * SIGMA_MULT
    # New games per hour, from the count of first appearances.
    span_h = 0.0
    if len(reads) >= 2:
        span_h = (parse_time(reads[-1]["at"]) - parse_time(reads[0]["at"])).total_seconds() / 3600.0
    new_per_hour = len(entry_ranks) / span_h if span_h > 0 else 0.5
    return sigma, entry_ranks, new_per_hour


def recent_drift(reads, uid, now):
    """A game's recent rank velocity in ranks per hour, from its last reads.

    Uses the last up to 4 reads that hold the game (about a day of archive
    reads), weighted toward the most recent, and damped: momentum in this
    chart fades fast, so the drift is halved over DRIFT_HALF_LIFE_H.
    """
    pts = []
    for read in reversed(reads):
        at = parse_time(read["at"])
        for g in read.get("up_and_coming") or []:
            if int(g["universe_id"]) == uid:
                pts.append((at, int(g["rank"])))
                break
        if len(pts) >= 4:
            break
    if len(pts) < 2:
        return 0.0
    pts.reverse()
    # velocity over the most recent segment, in ranks per hour (negative rank
    # change = rising, so flip the sign: rising games gain score).
    (t0, r0), (t1, r1) = pts[-2], pts[-1]
    hours = max(0.25, (t1 - t0).total_seconds() / 3600.0)
    vel = -(r1 - r0) / hours
    # Damp by how old the segment is.
    age_h = max(0.0, (now - t1).total_seconds() / 3600.0)
    vel *= 0.5 ** (age_h / DRIFT_HALF_LIFE_H)
    return vel


def created_dates(reads):
    """universe_id -> created datetime, from the history's details (when the
    recorder supplies them; the public archive's reads carry none)."""
    out = {}
    for read in reads:
        details = read.get("details") or {}
        for key, d in details.items():
            if d and d.get("created") and int(key) not in out:
                try:
                    out[int(key)] = parse_time(d["created"])
                except ValueError:
                    pass
    return out


def forecast(task, reads):
    window_from = parse_time(task["window"]["from"])
    window_until = parse_time(task["window"]["until"])
    slots = int(task["window"]["slots"])
    places = int(task["places"])
    slot_minutes = int(task["window"].get("slot_minutes", 60))
    slot_h = slot_minutes / 60.0

    sigma, entry_ranks, new_per_hour = calibrate(reads)
    created = created_dates(reads)
    last = reads[-1]
    games = chart_games(last)
    chart_size = len(games)

    # Per-game drift (ranks per hour, positive = rising).
    drifts = {uid: recent_drift(reads, uid, window_from) for uid, _r, _p in games}
    # A game's latent score starts at -rank; the score is what gets sorted.
    base_score = {uid: -float(rank) for uid, rank, _p in games}

    rng = random.Random(SEED)
    # occupancy[place][uid] = simulated slots spent at that place (named games)
    occupancy = {k: {} for k in range(1, places + 1)}
    # placeholder (brand-new game) occupancy per place
    new_occ = {k: 0 for k in range(1, places + 1)}
    total_slots = slots * N_SIMS

    for _sim in range(N_SIMS):
        scores = dict(base_score)
        # living games: uid -> (score, drift, created)
        live = {
            uid: [scores[uid], drifts[uid], created.get(uid)]
            for uid, _r, _p in games
        }
        next_new = rng.expovariate(new_per_hour) if new_per_hour > 0 else float("inf")
        t = window_from
        for _slot in range(slots):
            # Age out: only games published in the last 28 days are on it.
            for uid in list(live):
                c = live[uid][2]
                if c is not None and t >= c + timedelta(days=28):
                    del live[uid]
            # New entrants arrive as placeholders (unknown ids).
            placeholders = []
            while next_new <= slot_h * (_slot + 1):
                entry_rank = rng.choice(entry_ranks) if entry_ranks else chart_size
                placeholders.append(entry_rank)
                next_new += rng.expovariate(new_per_hour) if new_per_hour > 0 else float("inf")
            # Move every score by its drift (fading) plus noise.
            age_h = (t - window_from).total_seconds() / 3600.0
            decay = 0.5 ** (age_h / DRIFT_HALF_LIFE_H)
            order = []
            for uid, st in live.items():
                st[0] += st[1] * decay * slot_h + rng.gauss(0.0, sigma * math.sqrt(slot_h))
                order.append((st[0], uid))
            # Placeholders enter at their sampled rank: give them a score
            # that puts them there among the sorted live scores.
            order.sort(reverse=True)
            for er in placeholders:
                idx = min(max(er - 1, 0), len(order))
                score = order[idx - 1][0] - 0.001 if idx > 0 else (order[0][0] + 0.001 if order else 0.0)
                order.insert(idx, (score, None))
            for k in range(1, places + 1):
                if k - 1 < len(order):
                    uid = order[k - 1][1]
                    if uid is None:
                        new_occ[k] += 1
                    else:
                        occupancy[k][uid] = occupancy[k].get(uid, 0) + 1
            t += timedelta(minutes=slot_minutes)

    probabilities = []
    for k in range(1, places + 1):
        rows = []
        named_sum = 0.0
        for uid, count in occupancy[k].items():
            p = count / total_slots
            if p > 0.0:
                rows.append((uid, p))
                named_sum += p
        # Reserve the unnamed floor (brand-new games) and the simulated
        # newcomer mass, scaling the named rows down to fit.
        unnamed = max(UNNAMED_FLOOR, new_occ[k] / total_slots)
        unnamed = min(unnamed, 0.9)
        scale = (1.0 - unnamed) / named_sum if named_sum > 0 else 0.0
        rows = [(uid, p * scale) for uid, p in rows if p * scale >= 0.0005]
        rows.sort(key=lambda x: -x[1])
        rows = rows[:MAX_ROWS_PER_PLACE]
        for uid, p in rows:
            probabilities.append({"universe_id": uid, "place": k, "p": round(p, 6)})
    # top10: the games that spend the most simulated slots anywhere in the
    # top 10, tie-broken by their mean place there. (A plain mean rank over
    # all slots would reward a game that visits place 1 once and vanishes.)
    occ_total = {}
    occ_mean_place = {}
    for k in range(1, places + 1):
        for uid, count in occupancy[k].items():
            occ_total[uid] = occ_total.get(uid, 0) + count
            occ_mean_place[uid] = occ_mean_place.get(uid, 0.0) + k * count
    top10 = sorted(
        occ_total,
        key=lambda u: (-occ_total[u], occ_mean_place[u] / occ_total[u]),
    )[:places]
    # Fill any shortfall with the close's top 10 in order.
    for uid, _r, _p in games:
        if len(top10) >= places:
            break
        if uid not in top10:
            top10.append(uid)
    return {"probabilities": probabilities, "top10": top10[:places]}


def main():
    task, reads = load_history(INPUT_DIR)
    result = forecast(task, reads)
    out_path = os.path.join(OUTPUT_DIR, task["output"])
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(result, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
