"""Fixture tests: award controls, all-queues scoring, the per-week
measure picker, qualification bars and the data-driven commentary.

Drives utils/awards.compute_results + pick_metric + the renderers with
synthetic inputs — no DB, no Discord (the one SQL-shape test stubs
utils.db in-process). Run from Bot/:

    python tests/test_award_controls.py

Covers: baseline all-queues winners, the ranked default-scope fallback,
Arena's exclusion from death metrics (and explicit opt-in), per-award
exclusions ("on holiday"), forced winners, chosen metrics/scopes,
qualification bars (default 10 LP for the LP awards — the "-4 LP
winner" complaint — custom bars, forced bypass, measure-pick pause),
premade/stack evidence (the Jack-vs-Samuel "contradiction"), commentary
situations (close race, tie, streak, landslide, first win, thin sample,
stack week, near miss), disabled awards, custom taglines, measure
notes, season-reset skips and the remake-excluding SQL shapes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import awards  # noqa: E402
from utils.awards import (  # noqa: E402
    DUO_LEECH,
    INT,
    LP_CHAD,
    LP_LOSS,
    PUSSY,
    AwardAdjustments,
    AwardInputs,
    AwardPools,
    GameRow,
    Winner,
)

WEEK = dt.date(2026, 8, 3)


def snap(puuid, uid, name, account, tier, division, lp, wins, losses):
    return (puuid, uid, name, account, tier, division, lp, wins, losses)


def g(uid, name, queue, k, d, a, champ, win, match, allies=0, **extra):
    return GameRow(
        user_id=uid,
        display_name=name,
        queue_id=queue,
        kills=k,
        deaths=d,
        assists=a,
        champion=champ,
        win=win,
        match_id=match,
        allies=allies,
        **extra,
    )


def window(uid, name, queue, prior, week):
    """Per-game window rows: ``prior`` games before the week, ``week`` in it."""
    return [(uid, name, queue, False)] * prior + [(uid, name, queue, True)] * week


# In-week games, mixed queues. Deliberate shapes:
# - Alice (1): 6 ranked + 3 ARAM; her ranked 2/14/5 is the worst RANKED
#   scoreline; 2 first bloods; the 38% damage-share game.
# - Bob (2): 8 ranked, 6 premade (the leech), 300s dead every game.
# - Loukia (3): 2 ranked + 18 ARAM (the spam); her ARAM 2/16/3 is the
#   worst scoreline overall; lowest winrate and kill participation.
# - Dan (4): 5 ranked + 1 Arena; two of his premades are 3-stacks; the
#   Arena 20-death game is bait only the explicit arena scope may score.
GAMES = [
    g(
        1,
        "Alice",
        420,
        2,
        14,
        5,
        "Ahri",
        0,
        "A1",
        time_dead_sec=600,
        pings_missing=10,
        kill_participation=0.6,
    ),
    g(1, "Alice", 420, 5, 3, 7, "Lux", 1, "A2", allies=1, kill_participation=0.6),
    g(1, "Alice", 420, 3, 4, 6, "Lux", 1, "A3", allies=1, kill_participation=0.6),
    g(1, "Alice", 420, 1, 6, 2, "Lux", 0, "A4", allies=1, kill_participation=0.6),
    g(1, "Alice", 420, 4, 2, 8, "Ahri", 1, "A5", first_blood=1, kill_participation=0.6),
    g(
        1,
        "Alice",
        420,
        6,
        1,
        3,
        "Ahri",
        1,
        "A6",
        first_blood=1,
        team_damage_pct=0.38,
        kill_participation=0.6,
    ),
    g(1, "Alice", 450, 2, 7, 9, "Sona", 1, "A7", kill_participation=0.6),
    g(1, "Alice", 450, 1, 8, 4, "Sona", 0, "A8", kill_participation=0.6),
    g(1, "Alice", 450, 0, 9, 2, "Sona", 0, "A9", kill_participation=0.6),
    g(2, "Bob", 420, 4, 10, 9, "Braum", 1, "B1", time_dead_sec=300, kill_participation=0.5),
    g(2, "Bob", 420, 3, 5, 12, "Braum", 1, "B2", allies=1, time_dead_sec=300),
    g(2, "Bob", 420, 2, 4, 10, "Braum", 1, "B3", allies=1, time_dead_sec=300),
    g(
        2,
        "Bob",
        420,
        6,
        2,
        9,
        "Braum",
        1,
        "B4",
        allies=1,
        time_dead_sec=300,
        largest_multi_kill=3,
        first_blood=1,
        team_damage_pct=0.31,
        kill_participation=0.5,
    ),
    g(2, "Bob", 420, 1, 3, 14, "Braum", 1, "B5", allies=1, time_dead_sec=300),
    g(2, "Bob", 420, 2, 8, 6, "Braum", 0, "B6", allies=1, time_dead_sec=300),
    g(2, "Bob", 420, 0, 7, 3, "Braum", 0, "B7", allies=1, time_dead_sec=300),
    g(2, "Bob", 420, 3, 6, 4, "Braum", 0, "B8", time_dead_sec=300, kill_participation=0.5),
    g(3, "Loukia", 420, 1, 9, 2, "Jinx", 0, "L1", kill_participation=0.5),
    g(3, "Loukia", 420, 3, 5, 4, "Jinx", 1, "L2", kill_participation=0.6),
    g(3, "Loukia", 450, 2, 16, 3, "Jinx", 0, "L3", kill_participation=0.2),
    *[
        g(3, "Loukia", 450, 5, 5, 5, "Jinx", 1, f"L{i}", kill_participation=0.2)
        for i in range(4, 10)
    ],
    *[
        g(3, "Loukia", 450, 5, 5, 5, "Jinx", 0, f"L{i}", kill_participation=0.2)
        for i in range(10, 21)
    ],
    g(4, "Dan", 420, 2, 3, 5, "Garen", 1, "D1", allies=2, pings_missing=30),
    g(4, "Dan", 420, 1, 4, 6, "Garen", 1, "D2", allies=2, pings_missing=30),
    g(4, "Dan", 420, 0, 5, 2, "Garen", 0, "D3", allies=1, pings_missing=30),
    g(4, "Dan", 420, 2, 6, 3, "Garen", 0, "D4", allies=1, pings_missing=30),
    g(4, "Dan", 420, 5, 2, 9, "Garen", 1, "D5", pings_missing=30, largest_multi_kill=4),
    g(4, "Dan", 1700, 1, 20, 8, "Kayn", 0, "D6", pings_missing=50),
]

WINDOW_ROWS = [
    *window(1, "Alice", 420, 28, 6),
    *window(2, "Bob", 420, 30, 10),
    *window(3, "Loukia", 420, 40, 2),
    *window(3, "Loukia", 450, 0, 18),
    *window(4, "Dan", 420, 24, 3),
]

# (user, partner, partner_name, queue_id, games_together) per queue.
# Dan's two 3-stacks mean his per-pair counts (Alice 4 + Bob 2 = 6)
# legitimately exceed his 4 premade games — that's the point.
PARTNER_ROWS = [
    (2, 1, "Alice", 420, 5),
    (1, 2, "Bob", 420, 3),
    (4, 1, "Alice", 420, 3),
    (4, 1, "Alice", 1700, 1),
    (4, 2, "Bob", 420, 2),
]


def build_inputs(**overrides) -> AwardInputs:
    baseline = [
        snap("p1", 1, "Alice", "AliceMain", "GOLD", "II", 15, 55, 50),
        snap("p2", 2, "Bob", "BobAcc", "PLATINUM", "IV", 50, 95, 85),
        snap("p3", 3, "Loukia", "CarolAcc", "BRONZE", "III", 95, 18, 28),
    ]
    last = [
        snap("p1", 1, "Alice", "AliceMain", "GOLD", "II", 45, 60, 55),  # +30
        snap("p2", 2, "Bob", "BobAcc", "PLATINUM", "IV", 20, 100, 90),  # -30
        snap("p3", 3, "Loukia", "CarolAcc", "BRONZE", "III", 55, 20, 30),  # -40
    ]
    fields = {
        "baseline": baseline,
        "first_in_week": baseline,
        "last_in_week": last,
        "window_rows": WINDOW_ROWS,
        "games": GAMES,
        "partner_rows": PARTNER_ROWS,
        "boundary_reset": False,
    }
    fields.update(overrides)
    return AwardInputs(**fields)


# LP deltas too small to matter: Alice +4, Bob -4, Loukia -40 — the
# live-board complaint (a -4 LP "winner") reproduced.
def small_lp_last() -> list[tuple]:
    return [
        snap("p1", 1, "Alice", "AliceMain", "GOLD", "II", 19, 56, 51),  # +4
        snap("p2", 2, "Bob", "BobAcc", "PLATINUM", "IV", 46, 96, 86),  # -4
        snap("p3", 3, "Loukia", "CarolAcc", "BRONZE", "III", 55, 20, 30),  # -40
    ]


def pools_from(inputs: AwardInputs) -> AwardPools:
    per_user, _ = awards.net_lp_deltas(inputs.baseline, inputs.first_in_week, inputs.last_in_week)
    return AwardPools(
        per_user=per_user,
        window_rows=inputs.window_rows,
        games=inputs.games,
        partner_rows=inputs.partner_rows,
    )


def winner_ids(winners):
    return [w.user_id for w in winners]


# ---------------------------------------------------- default behaviour


def test_baseline_all_queues():
    results, season_reset, forced, below_min, runners = awards.compute_results(build_inputs())
    assert not season_reset and forced == frozenset() and below_min == {}
    assert winner_ids(results[LP_CHAD]) == [1] and results[LP_CHAD][0].value == 30.0
    assert winner_ids(results[LP_LOSS]) == [3] and results[LP_LOSS][0].value == -40.0
    # ALL queues: Loukia's ARAM spam keeps her volume up — Dan's -50% wins.
    assert winner_ids(results[PUSSY]) == [4]
    assert results[PUSSY][0].detail["drop_pct"] == 50
    assert winner_ids(results[DUO_LEECH]) == [2]
    # Partners are plural now, best-first, with per-pair counts.
    assert results[DUO_LEECH][0].detail["partners"] == [["Alice", 5]]
    assert "stack_games" not in results[DUO_LEECH][0].detail  # Bob only duos
    # ALL queues: Loukia's ARAM 2/16/3 out-ints Alice's ranked 2/14/5 —
    # and the queue is named in the detail so the banter lands.
    assert winner_ids(results[INT]) == [3] and results[INT][0].value == 16.0
    assert results[INT][0].detail["queue"] == "ARAM"
    # Runners-up ride along for the commentary.
    assert winner_ids(runners[LP_LOSS]) == [2] and runners[LP_LOSS][0].value == -30.0
    assert winner_ids(runners[INT]) == [1] and runners[INT][0].value == 14.0


def test_scope_ranked_default_restores_ranked_only():
    adj = AwardAdjustments(default_scope="ranked")
    results, _, _, _, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[PUSSY]) == [3]
    assert results[PUSSY][0].detail["drop_pct"] == 80
    assert winner_ids(results[INT]) == [1] and results[INT][0].value == 14.0
    assert results[INT][0].detail["queue"] == "Ranked Solo/Duo"
    assert results[INT][0].detail["scope"] == "ranked"
    assert winner_ids(results[LP_LOSS]) == [3]


def test_arena_sits_out_of_death_metrics_unless_chosen():
    results, _, _, _, _ = awards.compute_results(build_inputs())
    assert winner_ids(results[INT]) == [3]
    adj = AwardAdjustments(scopes={INT: "arena"})
    results, _, _, _, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[INT]) == [4] and results[INT][0].value == 20.0
    assert results[INT][0].detail["queue"] == "Arena"


# ------------------------------------------------------ award controls


def test_exclusions_recompute():
    adj = AwardAdjustments(excluded={PUSSY: frozenset({4}), LP_LOSS: frozenset({3})})
    results, _, forced, below_min, _ = awards.compute_results(build_inputs(), adj)
    assert forced == frozenset()
    assert winner_ids(results[PUSSY]) == [1]
    assert results[PUSSY][0].detail["drop_pct"] == 14
    # Bob's -30 clears the default 10 LP bar, so he simply wins.
    assert winner_ids(results[LP_LOSS]) == [2] and LP_LOSS not in below_min
    assert winner_ids(results[DUO_LEECH]) == [2]
    assert winner_ids(results[INT]) == [3]


def test_forced_winner_from_candidates():
    adj = AwardAdjustments(forced={DUO_LEECH: 4})
    results, _, forced, _, runners = awards.compute_results(build_inputs(), adj)
    assert DUO_LEECH in forced
    assert winner_ids(results[DUO_LEECH]) == [4]
    detail = results[DUO_LEECH][0].detail
    assert detail["games"] == 6 and detail["duo_games"] == 4
    # Dan's two 3-stacks: stack count kept, per-pair counts sum past 4.
    assert detail["stack_games"] == 2
    assert detail["partners"] == [["Alice", 4], ["Bob", 2]]
    # Managed wins get no runner-up (and so no commentary).
    assert runners[DUO_LEECH] == []


def test_forced_winner_must_still_qualify():
    adj = AwardAdjustments(forced={PUSSY: 2})
    results, _, forced, _, _ = awards.compute_results(build_inputs(), adj)
    assert PUSSY not in forced
    assert winner_ids(results[PUSSY]) == [4]


def test_exclusion_beats_forcing():
    adj = AwardAdjustments(forced={INT: 3}, excluded={INT: frozenset({3})})
    results, _, forced, _, _ = awards.compute_results(build_inputs(), adj)
    assert INT not in forced
    assert winner_ids(results[INT]) == [1]


def test_season_reset_still_skips_lp_awards():
    results, season_reset, _, below_min, _ = awards.compute_results(
        build_inputs(boundary_reset=True),
        AwardAdjustments(forced={LP_CHAD: 1}, metrics={INT: "lp_loss"}),
    )
    assert season_reset
    assert results[LP_CHAD] == [] and results[LP_LOSS] == []
    assert results[INT] == []
    assert below_min == {}  # a reset skip is not a threshold skip
    assert winner_ids(results[DUO_LEECH]) == [2]


# ------------------------------------------------- qualification bars


def test_small_lp_week_skips_both_lp_awards():
    # Alice +4 / Bob -4: neither clears the default "more than 10 LP" bar.
    inputs = build_inputs(
        last_in_week=[
            snap("p1", 1, "Alice", "AliceMain", "GOLD", "II", 19, 56, 51),  # +4
            snap("p2", 2, "Bob", "BobAcc", "PLATINUM", "IV", 46, 96, 86),  # -4
        ]
    )
    results, _, _, below_min, _ = awards.compute_results(inputs)
    assert results[LP_LOSS] == [] and results[LP_CHAD] == []
    assert below_min[LP_LOSS]["min"] == 10.0
    assert winner_ids(below_min[LP_LOSS]["winners"]) == [2]
    assert below_min[LP_LOSS]["winners"][0].value == -4.0
    assert winner_ids(below_min[LP_CHAD]["winners"]) == [1]
    # Exactly 10 is still out — "10 LP and below should be excluded".
    assert awards.qualifying_magnitude(LP_LOSS, -10.0) <= 10.0
    line = awards.below_min_line(LP_LOSS, WEEK, below_min[LP_LOSS])
    assert "Bob" in line and "-4 LP" in line and "10" in line and "LP lost" in line


def test_exclusion_applies_before_the_bar():
    # Loukia's -40 clears the bar; with her on holiday Bob's -4 is next
    # and gets judged — and skipped.
    inputs = build_inputs(last_in_week=small_lp_last())
    adj = AwardAdjustments(excluded={LP_LOSS: frozenset({3})})
    results, _, _, below_min, _ = awards.compute_results(inputs, adj)
    assert results[LP_LOSS] == []
    assert winner_ids(below_min[LP_LOSS]["winners"]) == [2]


def test_forced_winner_bypasses_the_bar():
    inputs = build_inputs(last_in_week=small_lp_last())
    adj = AwardAdjustments(forced={LP_CHAD: 1})
    results, _, forced, below_min, _ = awards.compute_results(inputs, adj)
    assert LP_CHAD in forced
    assert winner_ids(results[LP_CHAD]) == [1] and results[LP_CHAD][0].value == 4.0
    assert LP_CHAD not in below_min


def test_custom_bars_and_measure_pick_pauses_them():
    # A 20-death bar benches Loukia's 16; the near-miss is recorded.
    adj = AwardAdjustments(minimums={INT: 20})
    results, _, _, below_min, _ = awards.compute_results(build_inputs(), adj)
    assert results[INT] == []
    assert winner_ids(below_min[INT]["winners"]) == [3]
    # A percent bar on the volume award (units: % drop).
    adj = AwardAdjustments(minimums={PUSSY: 60})
    results, _, _, below_min, _ = awards.compute_results(build_inputs(), adj)
    assert results[PUSSY] == [] and winner_ids(below_min[PUSSY]["winners"]) == [4]
    # Re-pointing the measure pauses the bar — it is denominated in the
    # DEFAULT metric's unit, so judging another metric with it would be
    # unit soup (20 what? deaths? seconds?).
    adj = AwardAdjustments(minimums={INT: 200}, metrics={INT: "most_deaths_total"})
    results, _, _, below_min, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[INT]) == [3] and results[INT][0].value == 115.0
    assert INT not in below_min
    assert awards.parse_minimum("12.5", 0.0) == 12.5
    assert awards.parse_minimum("junk", 10.0) == 10.0
    assert awards.parse_minimum("-3", 10.0) == 10.0
    assert awards.qualifying_magnitude(PUSSY, 0.5) == 50.0
    assert awards.qualifying_magnitude(LP_LOSS, -40.0) == 40.0


# ------------------------------------------------- premades and stacks


def test_stack_evidence_pins_the_jack_contradiction():
    # Jack premades 15/15 while Samuel is 5/5 *with Jack* — per-pair
    # counts legitimately sum past duo_games because 3-stacks count for
    # every pair. The plural partner list makes that read as intended.
    rows = [
        (10, "Jack", 15, 15, 8, 0, 9),
        (14, "Samuel", 5, 5, 3, 0, 0),
    ]
    partner_rows = [
        (10, 11, "Gabes", 15),
        (10, 12, "Sanders", 7),
        (10, 13, "Zak", 5),
        (10, 14, "Samuel", 5),
        (14, 10, "Jack", 5),
    ]
    # Jack and Samuel BOTH sit at 100% premade — a genuine tie (which is
    # exactly why the single-partner rendering read as a contradiction).
    jack, samuel = awards.pick_duo_leech(rows, partner_rows)
    assert jack.user_id == 10 and samuel.user_id == 14
    assert samuel.detail["partners"] == [["Jack", 5]]
    assert jack.detail["partners"] == [["Gabes", 15], ["Sanders", 7], ["Samuel", 5], ["Zak", 5]]
    assert jack.detail["stack_games"] == 9
    line = awards.condemn_line(DUO_LEECH, jack)
    assert "**15 of 15** games stacked with the group" in line
    assert "(Gabes 15, Sanders 7, Samuel 5…)" in line  # capped at 3, ellipsis
    assert "premade 8W-7L" in line
    # A pure-duo winner still reads as a duo.
    [bob] = awards.pick_duo_leech([(2, "Bob", 8, 6, 4, 1, 0)], [(2, 1, "Alice", 5)])
    assert "games duo'd (Alice 5)" in awards.condemn_line(DUO_LEECH, bob)
    # Legacy recorded details (single partner keys) still render.
    assert awards.partner_list_text({"partner": "Alice", "partner_games": 5}) == "Alice 5"


# ------------------------------------------------------ measure picker


def test_chosen_metric_changes_measure():
    adj = AwardAdjustments(metrics={PUSSY: "most_time_dead"})
    results, _, forced, _, _ = awards.compute_results(build_inputs(), adj)
    assert forced == frozenset()
    winner = results[PUSSY][0]
    assert winner.user_id == 2 and winner.value == 2400.0
    assert winner.detail["metric"] == "most_time_dead"
    assert awards.condemn_line(PUSSY, winner) == "**40m 00s** spent dead across 8 games."


def test_precedence_exclusion_beats_forced_beats_metric():
    adj = AwardAdjustments(metrics={INT: "most_deaths_total"})
    results, _, _, _, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[INT]) == [3] and results[INT][0].value == 115.0
    adj = AwardAdjustments(metrics={INT: "most_deaths_total"}, forced={INT: 1})
    results, _, forced, _, _ = awards.compute_results(build_inputs(), adj)
    assert INT in forced
    assert winner_ids(results[INT]) == [1] and results[INT][0].value == 54.0
    adj = AwardAdjustments(
        metrics={INT: "most_deaths_total"},
        forced={INT: 1},
        excluded={INT: frozenset({1})},
    )
    results, _, forced, _, _ = awards.compute_results(build_inputs(), adj)
    assert INT not in forced
    assert winner_ids(results[INT]) == [3]


def test_metric_menu_over_scopes():
    pools = pools_from(build_inputs())
    [w] = awards.pick_metric("most_missing_pings", pools, "all")
    assert (w.user_id, w.value) == (4, 200.0)
    [w] = awards.pick_metric("most_missing_pings", pools, "ranked")
    assert (w.user_id, w.value) == (4, 150.0)
    [w] = awards.pick_metric("most_deaths_total", pools, "all")
    assert (w.user_id, w.value) == (3, 115.0)
    [w] = awards.pick_metric("fewest_games", pools, "all")
    assert (w.user_id, w.value) == (4, 3.0)
    [w] = awards.pick_metric("lowest_winrate", pools, "all")
    assert w.user_id == 3 and w.value == 0.35
    [w] = awards.pick_metric("lowest_kda_game", pools, "all")
    assert w.user_id == 1 and round(w.value, 2) == 0.22 and w.detail["queue"] == "ARAM"
    [w] = awards.pick_metric("largest_multikill", pools, "all")
    assert w.user_id == 4 and w.value == 4.0
    assert awards.pick_metric("largest_multikill", pools, "aram") == []


# ---------------------------------------------------------- commentary


def hist(*weeks_and_ids):
    """[(week_start, winner id set)] most-recent-first."""
    return [(week, frozenset(ids)) for week, ids in weeks_and_ids]


def test_commentary_close_race():
    winners = [Winner(3, "Loukia", -40.0, {"delta": -40})]
    runners = [Winner(2, "Bob", -38.0, {"delta": -38})]
    line = awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, [])
    assert line is not None and "Bob" in line and "2 LP" in line
    # Deterministic: the same inputs render the same line, always.
    assert line == awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, [])


def test_commentary_tie_beats_everything():
    winners = [Winner(1, "Alice", 14.0, {}), Winner(3, "Loukia", 14.0, {})]
    runners = [Winner(2, "Bob", 13.9, {})]
    line = awards.commentary_line(INT, "most_deaths_game", WEEK, winners, runners, [])
    assert line in [v.format(n=2) for v in awards.COMMENTARY_VARIANTS["tie"]]


def test_commentary_streak_and_landslide_and_first_win():
    winners = [Winner(3, "Loukia", -80.0, {"delta": -80})]
    runners = [Winner(2, "Bob", -60.0, {"delta": -60})]  # 25% back: no situation
    history = hist((WEEK - dt.timedelta(days=7), {3}), (WEEK - dt.timedelta(days=14), {3}))
    line = awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, history)
    assert line in [v.format(n=3) for v in awards.COMMENTARY_VARIANTS["streak"]]
    # A skipped week breaks the run: same history shifted one week back.
    stale = hist((WEEK - dt.timedelta(days=14), {3}), (WEEK - dt.timedelta(days=21), {3}))
    line = awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, stale)
    assert line not in [v.format(n=3) for v in awards.COMMENTARY_VARIANTS["streak"]]
    # Landslide: runner at less than half the winner's number.
    runners = [Winner(2, "Bob", -30.0, {"delta": -30})]
    line = awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, stale)
    assert line in [
        v.format(runner="Bob", gap="50 LP") for v in awards.COMMENTARY_VARIANTS["landslide"]
    ]
    # First win this season: no history at all.
    runners = [Winner(2, "Bob", -60.0, {"delta": -60})]
    line = awards.commentary_line(LP_LOSS, "lp_loss", WEEK, winners, runners, [])
    title = awards.AWARDS[LP_LOSS].title
    assert line in [v.format(title=title) for v in awards.COMMENTARY_VARIANTS["first_win"]]


def test_commentary_thin_sample_and_silence():
    # Winner has won before (no first-win), gap is mid (no close/landslide),
    # and both parties barely played: thin sample.
    winners = [Winner(4, "Dan", 0.5, {"drop_pct": 50, "this_week": 3, "baseline": 6.0})]
    runners = [Winner(1, "Alice", 0.36, {"drop_pct": 36, "this_week": 4, "baseline": 7.0})]
    history = hist((WEEK - dt.timedelta(days=21), {4}))
    line = awards.commentary_line(PUSSY, "games_drop", WEEK, winners, runners, history)
    assert line in [v.format(n=7) for v in awards.COMMENTARY_VARIANTS["thin_sample"]]
    # Same shape with big samples: nothing worth saying -> None.
    winners = [Winner(4, "Dan", 0.5, {"drop_pct": 50, "this_week": 30, "baseline": 60.0})]
    runners = [Winner(1, "Alice", 0.36, {"drop_pct": 36, "this_week": 30, "baseline": 47.0})]
    assert awards.commentary_line(PUSSY, "games_drop", WEEK, winners, runners, history) is None


def test_commentary_stack_week_and_forced_silence():
    winners = [Winner(10, "Jack", 1.0, {"games": 15, "duo_games": 15, "stack_games": 9})]
    runners = [Winner(2, "Bob", 0.98, {"games": 50, "duo_games": 49})]
    line = awards.commentary_line(DUO_LEECH, "duo_share", WEEK, winners, runners, [])
    assert line in [v.format(stacks=9) for v in awards.COMMENTARY_VARIANTS["stack_week"]]
    # build_commentaries skips forced awards and empty awards.
    adj = AwardAdjustments()
    results = {DUO_LEECH: winners, INT: []}
    runners_up = {DUO_LEECH: runners, INT: []}
    lines = awards.build_commentaries(WEEK, results, runners_up, {}, frozenset({DUO_LEECH}), adj)
    assert lines == {}


def test_commentary_in_ceremony_blocks():
    results, season_reset, forced, below_min, runners = awards.compute_results(build_inputs())
    history = {LP_LOSS: hist((WEEK - dt.timedelta(days=7), {3}))}
    commentary = awards.build_commentaries(
        WEEK, results, runners, history, forced, AwardAdjustments()
    )
    assert LP_LOSS in commentary  # Loukia won last week too: a streak
    blocks = awards.build_ceremony_blocks(
        WEEK, results, season_reset=season_reset, below_min=below_min, commentary=commentary
    )
    lp_block = next(b for b in blocks if "LP Loss Loser" in b)
    assert f"\U0001f4ac *{commentary[LP_LOSS]}*" in lp_block


def test_below_min_block_uses_near_miss_line():
    inputs = build_inputs(last_in_week=small_lp_last()[:2])  # +4 / -4 only
    results, season_reset, _, below_min, _ = awards.compute_results(inputs)
    blocks = awards.build_ceremony_blocks(
        WEEK, results, season_reset=season_reset, below_min=below_min
    )
    lp_block = next(b for b in blocks if "LP Loss Loser" in b)
    assert "no winner" in lp_block
    assert awards.below_min_line(LP_LOSS, WEEK, below_min[LP_LOSS]) in lp_block
    # The generic skip line does NOT appear — this is "nobody earned it",
    # not "nothing happened".
    assert awards.AWARDS[LP_LOSS].skip_line not in lp_block


# ---------------------------------------------------------- rendering


def test_blocks_default_format_unchanged():
    results, season_reset, _, _, _ = awards.compute_results(build_inputs())
    blocks = awards.build_ceremony_blocks(WEEK, results, season_reset=season_reset)
    text = "".join(blocks)
    assert awards.MANAGEMENT_NOTE not in text
    assert "\U0001f4cf" not in text  # no measure note under pure defaults
    assert "\U0001f4ac" not in text  # no commentary unless supplied
    int_block = next(b for b in blocks if "Int of the Week" in b)
    assert "**2/16/3** on Jinx (ARAM, loss)." in int_block


def test_blocks_disabled_tagline_and_note():
    adj = AwardAdjustments(
        disabled=frozenset({LP_CHAD}),
        taglines={INT: "Crimes against the minimap."},
        forced={DUO_LEECH: 4},
    )
    results, season_reset, forced, _, _ = awards.compute_results(build_inputs(), adj)
    blocks = awards.build_ceremony_blocks(
        WEEK,
        results,
        season_reset=season_reset,
        disabled=adj.disabled,
        taglines=adj.taglines,
        forced=forced,
    )
    text = "".join(blocks)
    assert "LP Chad" not in text
    assert "*Crimes against the minimap.*" in text
    duo_block = next(b for b in blocks if "Duo Leech" in b)
    assert awards.MANAGEMENT_NOTE in duo_block


def test_measure_notes():
    assert awards.measure_note(INT, "most_deaths_game", "all", "all") is None
    assert awards.measure_note(INT, "most_time_dead", "all", "all") == (
        "this week: measured by most time spent dead"
    )
    assert awards.measure_note(INT, "most_deaths_game", "aram", "all") == (
        "this week: ARAM games only"
    )
    assert awards.measure_note(LP_LOSS, "lp_loss", "ranked", "all") is None


def test_value_text_and_cabinet_compat():
    assert awards.metric_value_text("most_time_dead", 2412, {}) == "40m 12s dead"
    assert awards.metric_value_text("lowest_winrate", 0.35, {}) == "35% winrate"
    legacy_int = {"kills": 2, "deaths": 14, "assists": 5}
    assert awards.cabinet_value(INT, 14, legacy_int) == "2/14/5"
    assert awards.cabinet_value(PUSSY, 0.8, {"drop_pct": 80}) == "-80% volume"
    assert awards.cabinet_value(LP_LOSS, -187, {}) == "-187 LP"
    assert awards.cabinet_value(PUSSY, 2400, {"metric": "most_time_dead"}) == "40m 00s dead"
    # Gap formatting (commentary) per metric family.
    assert awards.gap_text("lp_loss", 6) == "6 LP"
    assert awards.gap_text("most_deaths_game", 2) == "2 deaths"
    assert awards.gap_text("most_time_dead", 250) == "4m 10s"
    assert awards.gap_text("duo_share", 0.08) == "8%"
    assert awards.gap_text("lowest_kda_game", 0.114) == "0.11 KDA"


def test_parse_scope_and_queue_names():
    assert awards.parse_scope(None) == "all"
    assert awards.parse_scope(" Ranked ") == "ranked"
    assert awards.parse_scope("garbage") == "all"
    assert awards.queue_name(450) == "ARAM"
    assert awards.queue_name(None) == "Custom game"
    assert awards.DEFAULT_METRIC.keys() == set(awards.AWARD_ORDER)
    assert awards.MIN_DEFAULTS.keys() == set(awards.AWARD_ORDER)
    assert awards.MIN_UNITS.keys() == set(awards.AWARD_ORDER)


# ------------------------------------------------------------ SQL shapes


def test_sql_shapes_and_config_reads():
    captured: list[tuple[str, object]] = []

    async def fake_fetchall(sql, params=()):
        captured.append((sql, params))
        if "weekly_awards" in sql:
            return [
                (LP_LOSS, WEEK - dt.timedelta(days=7), 3),
                (LP_LOSS, WEEK - dt.timedelta(days=14), 3),
                (INT, WEEK - dt.timedelta(days=7), 1),
            ]
        if "bot_config" in sql:
            return [("award_lp_loss_min", "25"), ("award_int_of_the_week_min", "junk")]
        return []

    async def fake_fetchone(sql, params=()):
        captured.append((sql, params))
        return ("ranked",)

    real_fetchall, real_fetchone = awards.db.fetchall, awards.db.fetchone
    awards.db.fetchall, awards.db.fetchone = fake_fetchall, fake_fetchone
    try:
        start = dt.datetime(2026, 8, 3, tzinfo=awards.LONDON)
        end = dt.datetime(2026, 8, 10, tzinfo=awards.LONDON)
        asyncio.run(awards.fetch_window_rows(start, end))
        asyncio.run(awards.fetch_game_rows(start, end))
        asyncio.run(awards.fetch_partner_rows(start, end))
        for sql, _params in captured:
            assert "COALESCE(ms.early_surrender, 0) = 0" in sql
            assert "queue_id = %" not in sql
        adj = asyncio.run(awards.fetch_adjustments(WEEK))
        assert adj.default_scope == "ranked"
        # Configured bar parsed; junk falls back to the award's default.
        assert adj.minimum_for(LP_LOSS) == 25.0
        assert adj.minimum_for(INT) == awards.MIN_DEFAULTS[INT]
        assert adj.minimum_for(LP_CHAD) == 10.0  # unset -> default
        history = asyncio.run(awards.fetch_prior_winner_history(WEEK))
        assert history[LP_LOSS] == [
            (WEEK - dt.timedelta(days=7), frozenset({3})),
            (WEEK - dt.timedelta(days=14), frozenset({3})),
        ]
        assert history[INT] == [(WEEK - dt.timedelta(days=7), frozenset({1}))]
    finally:
        awards.db.fetchall, awards.db.fetchone = real_fetchall, real_fetchone


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(TESTS)} award-control fixture tests passed")
