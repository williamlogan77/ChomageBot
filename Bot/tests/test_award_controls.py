"""Fixture tests: award controls, all-queues scoring and the per-week
measure picker (queue scope × metric).

Drives utils/awards.compute_results + pick_metric + the renderers with
synthetic inputs — no DB, no Discord (the one SQL-shape test stubs
utils.db in-process). Run from Bot/:

    python tests/test_award_controls.py

Covers: baseline all-queues winners, the ranked default-scope fallback,
Arena's exclusion from death metrics (and explicit opt-in), per-award
exclusions ("on holiday"), forced winners (qualifying, non-qualifying
and excluded-beats-forced), chosen metrics/scopes and their precedence
(exclusion > forced > measure > defaults), disabled awards, custom
taglines, measure notes, the management note, season-reset skips,
value/condemn formatting and the remake-excluding SQL shapes.
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
)

WEEK = dt.date(2026, 8, 3)


def snap(puuid, uid, name, account, tier, division, lp, wins, losses):
    return (puuid, uid, name, account, tier, division, lp, wins, losses)


def g(uid, name, queue, k, d, a, champ, win, match, duo=False, **extra):
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
        duo=duo,
        **extra,
    )


def window(uid, name, queue, prior, week):
    """Per-game window rows: ``prior`` games before the week, ``week`` in it."""
    return [(uid, name, queue, False)] * prior + [(uid, name, queue, True)] * week


# In-week games, mixed queues. Deliberate shapes:
# - Alice (1): 6 ranked + 3 ARAM; her ranked 2/14/5 is the worst RANKED
#   scoreline; 2 first bloods; the 38% damage-share game.
# - Bob (2): 8 ranked, 6 duo'd (the leech), 300s dead every game.
# - Loukia (3): 2 ranked + 18 ARAM (the spam); her ARAM 2/16/3 is the
#   worst scoreline overall; lowest winrate and kill participation.
# - Dan (4): 5 ranked + 1 Arena; the Arena 20-death game is bait that
#   only the explicit arena scope may score; ping machine; Quadra kill.
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
    g(1, "Alice", 420, 5, 3, 7, "Lux", 1, "A2", duo=True, kill_participation=0.6),
    g(1, "Alice", 420, 3, 4, 6, "Lux", 1, "A3", duo=True, kill_participation=0.6),
    g(1, "Alice", 420, 1, 6, 2, "Lux", 0, "A4", duo=True, kill_participation=0.6),
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
    g(2, "Bob", 420, 3, 5, 12, "Braum", 1, "B2", duo=True, time_dead_sec=300),
    g(2, "Bob", 420, 2, 4, 10, "Braum", 1, "B3", duo=True, time_dead_sec=300),
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
        duo=True,
        time_dead_sec=300,
        largest_multi_kill=3,
        first_blood=1,
        team_damage_pct=0.31,
        kill_participation=0.5,
    ),
    g(2, "Bob", 420, 1, 3, 14, "Braum", 1, "B5", duo=True, time_dead_sec=300),
    g(2, "Bob", 420, 2, 8, 6, "Braum", 0, "B6", duo=True, time_dead_sec=300),
    g(2, "Bob", 420, 0, 7, 3, "Braum", 0, "B7", duo=True, time_dead_sec=300),
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
    g(4, "Dan", 420, 2, 3, 5, "Garen", 1, "D1", duo=True, pings_missing=30),
    g(4, "Dan", 420, 1, 4, 6, "Garen", 1, "D2", duo=True, pings_missing=30),
    g(4, "Dan", 420, 0, 5, 2, "Garen", 0, "D3", duo=True, pings_missing=30),
    g(4, "Dan", 420, 2, 6, 3, "Garen", 0, "D4", duo=True, pings_missing=30),
    g(4, "Dan", 420, 5, 2, 9, "Garen", 1, "D5", pings_missing=30, largest_multi_kill=4),
    g(4, "Dan", 1700, 1, 20, 8, "Kayn", 0, "D6", pings_missing=50),
]

# Games-count window: ranked habits for everyone, plus Loukia's ARAM
# spam this week. Per scope: RANKED — Loukia collapses -80%, Dan -50%,
# Alice -14%, Bob over baseline; ALL — Loukia's ARAM spam disqualifies
# her, Dan -50% leads.
WINDOW_ROWS = [
    *window(1, "Alice", 420, 28, 6),
    *window(2, "Bob", 420, 30, 10),
    *window(3, "Loukia", 420, 40, 2),
    *window(3, "Loukia", 450, 0, 18),
    *window(4, "Dan", 420, 24, 3),
]

# (user, partner, partner_name, queue_id, games_together) per queue.
PARTNER_ROWS = [
    (2, 1, "Alice", 420, 5),
    (1, 2, "Bob", 420, 3),
    (4, 1, "Alice", 420, 3),
    (4, 1, "Alice", 1700, 1),
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
    results, season_reset, forced = awards.compute_results(build_inputs())
    assert not season_reset and forced == frozenset()
    assert winner_ids(results[LP_CHAD]) == [1] and results[LP_CHAD][0].value == 30.0
    assert winner_ids(results[LP_LOSS]) == [3] and results[LP_LOSS][0].value == -40.0
    # ALL queues: Loukia's ARAM spam keeps her volume up — Dan's -50% wins.
    assert winner_ids(results[PUSSY]) == [4]
    assert results[PUSSY][0].detail["drop_pct"] == 50
    assert winner_ids(results[DUO_LEECH]) == [2]
    assert results[DUO_LEECH][0].detail["partner"] == "Alice"
    # ALL queues: Loukia's ARAM 2/16/3 out-ints Alice's ranked 2/14/5 —
    # and the queue is named in the detail so the banter lands.
    assert winner_ids(results[INT]) == [3] and results[INT][0].value == 16.0
    assert results[INT][0].detail["queue"] == "ARAM"
    assert results[INT][0].detail["queue_id"] == 450
    # Default measure + default scope: nothing annotated.
    assert "metric" not in results[INT][0].detail
    assert "scope" not in results[INT][0].detail


def test_scope_ranked_default_restores_ranked_only():
    adj = AwardAdjustments(default_scope="ranked")
    results, _, _ = awards.compute_results(build_inputs(), adj)
    # The pre-2026-08 winners: Loukia's ranked collapse, Alice's ranked int.
    assert winner_ids(results[PUSSY]) == [3]
    assert results[PUSSY][0].detail["drop_pct"] == 80
    assert winner_ids(results[INT]) == [1] and results[INT][0].value == 14.0
    assert results[INT][0].detail["queue"] == "Ranked Solo/Duo"
    assert results[INT][0].detail["scope"] == "ranked"
    # LP awards are ranked by nature either way.
    assert winner_ids(results[LP_LOSS]) == [3]


def test_arena_sits_out_of_death_metrics_unless_chosen():
    # Dan's Arena 1/20/8 must not win under the all-queues default...
    results, _, _ = awards.compute_results(build_inputs())
    assert winner_ids(results[INT]) == [3]
    # ...but an explicit per-week Arena scope opts back in.
    adj = AwardAdjustments(scopes={INT: "arena"})
    results, _, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[INT]) == [4] and results[INT][0].value == 20.0
    assert results[INT][0].detail["queue"] == "Arena"
    assert results[INT][0].detail["scope"] == "arena"
    assert awards.measure_note(INT, "most_deaths_game", "arena", "all") == (
        "this week: Arena games only"
    )


# ------------------------------------------------------ award controls


def test_exclusions_recompute():
    adj = AwardAdjustments(excluded={PUSSY: frozenset({4}), LP_LOSS: frozenset({3})})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert forced == frozenset()
    # Dan is "on holiday": Alice's -14% takes the volume award and
    # Bob's -30 LP becomes the biggest loss.
    assert winner_ids(results[PUSSY]) == [1]
    assert results[PUSSY][0].detail["drop_pct"] == 14
    assert winner_ids(results[LP_LOSS]) == [2] and results[LP_LOSS][0].value == -30.0
    # Untouched awards keep their winners.
    assert winner_ids(results[DUO_LEECH]) == [2]
    assert winner_ids(results[INT]) == [3]


def test_forced_winner_from_candidates():
    adj = AwardAdjustments(forced={DUO_LEECH: 4})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert DUO_LEECH in forced
    assert winner_ids(results[DUO_LEECH]) == [4]
    # The forced winner keeps their OWN numbers, not the real leader's.
    assert results[DUO_LEECH][0].detail["games"] == 6
    assert results[DUO_LEECH][0].detail["duo_games"] == 4


def test_forced_winner_must_still_qualify():
    # Bob played over his volume baseline — he can't be handed the award.
    adj = AwardAdjustments(forced={PUSSY: 2})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert PUSSY not in forced
    assert winner_ids(results[PUSSY]) == [4]


def test_exclusion_beats_forcing():
    adj = AwardAdjustments(forced={INT: 3}, excluded={INT: frozenset({3})})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert INT not in forced
    # Without Loukia (and with Arena benched) Alice's 14 deaths lead.
    assert winner_ids(results[INT]) == [1]


def test_season_reset_still_skips_lp_awards():
    results, season_reset, _ = awards.compute_results(
        build_inputs(boundary_reset=True),
        AwardAdjustments(forced={LP_CHAD: 1}, metrics={INT: "lp_loss"}),
    )
    assert season_reset
    assert results[LP_CHAD] == [] and results[LP_LOSS] == []
    # An award re-pointed at an LP metric skips on a reset week too.
    assert results[INT] == []
    assert winner_ids(results[DUO_LEECH]) == [2]


# ------------------------------------------------------ measure picker


def test_chosen_metric_changes_measure():
    adj = AwardAdjustments(metrics={PUSSY: "most_time_dead"})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert forced == frozenset()
    # Bob's 8 × 300s of gray screen beat Alice's lone 600s.
    winner = results[PUSSY][0]
    assert winner.user_id == 2 and winner.value == 2400.0
    assert winner.detail["metric"] == "most_time_dead"
    assert winner.detail["total"] == 2400 and winner.detail["games"] == 8
    assert awards.condemn_line(PUSSY, winner) == "**40m 00s** spent dead across 8 games."
    blocks = awards.build_ceremony_blocks(WEEK, results, measures=awards.measure_notes(adj))
    text = "".join(blocks)
    assert "*this week: measured by most time spent dead*" in text


def test_precedence_exclusion_beats_forced_beats_metric():
    # Under the chosen metric, Loukia's 115 deaths would win...
    adj = AwardAdjustments(metrics={INT: "most_deaths_total"})
    results, _, _ = awards.compute_results(build_inputs(), adj)
    assert winner_ids(results[INT]) == [3] and results[INT][0].value == 115.0
    # ...forcing Alice re-picks HER total under the same metric...
    adj = AwardAdjustments(metrics={INT: "most_deaths_total"}, forced={INT: 1})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert INT in forced
    assert winner_ids(results[INT]) == [1] and results[INT][0].value == 54.0
    assert results[INT][0].detail["metric"] == "most_deaths_total"
    # ...and excluding her beats the forcing outright.
    adj = AwardAdjustments(
        metrics={INT: "most_deaths_total"},
        forced={INT: 1},
        excluded={INT: frozenset({1})},
    )
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert INT not in forced
    assert winner_ids(results[INT]) == [3]


def test_metric_menu_over_scopes():
    pools = pools_from(build_inputs())
    # Ping machine: Dan — Arena counts for non-death metrics under "all".
    [w] = awards.pick_metric("most_missing_pings", pools, "all")
    assert (w.user_id, w.value) == (4, 200.0)
    [w] = awards.pick_metric("most_missing_pings", pools, "ranked")
    assert (w.user_id, w.value) == (4, 150.0)
    # Arena's 20 deaths stay out of the week total under "all".
    [w] = awards.pick_metric("most_deaths_total", pools, "all")
    assert (w.user_id, w.value) == (3, 115.0)
    # Fewest games: habit gate keeps everyone in, Dan's 3 wins; the
    # ranked lens sees Loukia's 2.
    [w] = awards.pick_metric("fewest_games", pools, "all")
    assert (w.user_id, w.value) == (4, 3.0)
    [w] = awards.pick_metric("fewest_games", pools, "ranked")
    assert (w.user_id, w.value) == (3, 2.0)
    # Lowest average kill participation — Loukia's ARAM 0.2s drag her down.
    [w] = awards.pick_metric("lowest_kp", pools, "all")
    assert w.user_id == 3 and round(w.value, 4) == 0.235
    # Lowest winrate over min 5 games.
    [w] = awards.pick_metric("lowest_winrate", pools, "all")
    assert w.user_id == 3 and w.value == 0.35
    assert w.detail == {"wins": 7, "losses": 13, "games": 20}
    # Single-game extremes.
    [w] = awards.pick_metric("lowest_kda_game", pools, "all")
    assert w.user_id == 1 and round(w.value, 2) == 0.22 and w.detail["queue"] == "ARAM"
    [w] = awards.pick_metric("biggest_damage_share", pools, "all")
    assert w.user_id == 1 and w.value == 0.38
    [w] = awards.pick_metric("largest_multikill", pools, "all")
    assert w.user_id == 4 and w.value == 4.0
    assert awards.pick_metric("largest_multikill", pools, "aram") == []
    [w] = awards.pick_metric("most_first_bloods", pools, "all")
    assert (w.user_id, w.value) == (1, 2.0)


# ---------------------------------------------------------- rendering


def test_blocks_default_format_unchanged():
    results, season_reset, _ = awards.compute_results(build_inputs())
    blocks = awards.build_ceremony_blocks(WEEK, results, season_reset=season_reset)
    text = "".join(blocks)
    assert awards.MANAGEMENT_NOTE not in text
    assert "\U0001f4cf" not in text  # no measure note under pure defaults
    for tagline in awards.DEFAULT_TAGLINES.values():
        assert tagline not in text
    int_block = next(b for b in blocks if "Int of the Week" in b)
    assert "**2/16/3** on Jinx (ARAM, loss)." in int_block


def test_blocks_disabled_tagline_and_note():
    adj = AwardAdjustments(
        disabled=frozenset({LP_CHAD}),
        taglines={INT: "Crimes against the minimap."},
        forced={DUO_LEECH: 4},
    )
    results, season_reset, forced = awards.compute_results(build_inputs(), adj)
    blocks = awards.build_ceremony_blocks(
        WEEK,
        results,
        season_reset=season_reset,
        disabled=adj.disabled,
        taglines=adj.taglines,
        forced=forced,
    )
    text = "".join(blocks)
    assert "LP Chad" not in text  # disabled award: no block at all
    assert "*Crimes against the minimap.*" in text
    duo_block = next(b for b in blocks if "Duo Leech" in b)
    assert awards.MANAGEMENT_NOTE in duo_block
    int_block = next(b for b in blocks if "Int of the Week" in b)
    assert awards.MANAGEMENT_NOTE not in int_block


def test_measure_notes():
    assert awards.measure_note(INT, "most_deaths_game", "all", "all") is None
    assert awards.measure_note(INT, "most_time_dead", "all", "all") == (
        "this week: measured by most time spent dead"
    )
    assert awards.measure_note(INT, "most_deaths_game", "aram", "all") == (
        "this week: ARAM games only"
    )
    assert awards.measure_note(INT, "most_deaths_game", "all", "ranked") == (
        "this week: scored across all queues"
    )
    # LP metrics pin to ranked — no scope chatter, ever.
    assert awards.measure_note(LP_LOSS, "lp_loss", "ranked", "all") is None
    adj = AwardAdjustments(metrics={PUSSY: "lowest_winrate"}, scopes={INT: "aram"})
    notes = awards.measure_notes(adj)
    assert notes == {
        PUSSY: "this week: measured by lowest winrate",
        INT: "this week: ARAM games only",
    }
    assert awards.effective_scope(adj, LP_CHAD) == "ranked"


def test_value_text_and_cabinet_compat():
    assert awards.metric_value_text("most_time_dead", 2412, {}) == "40m 12s dead"
    assert awards.metric_value_text("lowest_winrate", 0.35, {}) == "35% winrate"
    assert awards.metric_value_text("largest_multikill", 4, {"queue": "ARAM"}) == (
        "Quadra kill · ARAM"
    )
    assert awards.metric_value_text("lowest_kda_game", 0.2222, {}) == "0.22 KDA"
    assert awards.metric_value_text("fewest_games", 1, {}) == "1 game"
    assert awards.metric_value_text("most_missing_pings", 1, {}) == "1 ? ping"
    assert awards.metric_value_text("biggest_damage_share", 0.38, {"queue": "ARAM"}) == (
        "38% team dmg · ARAM"
    )
    # Historical rows (no metric, no queue) keep their exact old text.
    legacy_int = {"kills": 2, "deaths": 14, "assists": 5}
    assert awards.cabinet_value(INT, 14, legacy_int) == "2/14/5"
    assert awards.cabinet_value(PUSSY, 0.8, {"drop_pct": 80}) == "-80% volume"
    assert awards.cabinet_value(LP_LOSS, -187, {}) == "-187 LP"
    # Metric-driven rows render through the metric's formatter.
    assert awards.cabinet_value(PUSSY, 2400, {"metric": "most_time_dead"}) == "40m 00s dead"
    # New single-game rows carry their queue.
    assert (
        awards.cabinet_value(INT, 16, {"kills": 2, "deaths": 16, "assists": 3, "queue": "ARAM"})
        == "2/16/3 · ARAM"
    )


def test_parse_scope_and_queue_names():
    assert awards.parse_scope(None) == "all"
    assert awards.parse_scope(" Ranked ") == "ranked"
    assert awards.parse_scope("aram") == "aram"
    assert awards.parse_scope("garbage") == "all"
    assert awards.queue_name(450) == "ARAM"
    assert awards.queue_name(None) == "Custom game"
    assert awards.queue_name(31337) == "Other mode"
    assert awards.DEFAULT_METRIC.keys() == set(awards.AWARD_ORDER)
    for key in awards.DEFAULT_METRIC.values():
        assert key in awards.METRICS


def test_compute_results_matches_plain_pickers_under_defaults():
    inputs = build_inputs()
    via_new, reset_new, _ = awards.compute_results(inputs)
    per_user, _ = awards.net_lp_deltas(inputs.baseline, inputs.first_in_week, inputs.last_in_week)
    legacy = {
        LP_LOSS: awards.pick_lp_extreme(per_user, gain=False),
        LP_CHAD: awards.pick_lp_extreme(per_user, gain=True),
        PUSSY: awards.pick_volume_collapse(awards.volume_rows_for_scope(inputs.window_rows, "all")),
        DUO_LEECH: awards.pick_duo_leech(
            awards.duo_rows_for_scope(inputs.games, "all"),
            awards.partner_rows_for_scope(inputs.partner_rows, "all"),
        ),
        INT: awards.pick_int(awards.games_for_scope(inputs.games, "all", "most_deaths_game")),
    }
    assert via_new == legacy
    assert reset_new is False


# ------------------------------------------------------------ SQL shapes


def test_sql_excludes_remakes_and_never_pins_queues():
    captured: list[str] = []

    async def fake_fetchall(sql, params=()):
        captured.append(sql)
        return []

    async def fake_fetchone(sql, params=()):
        captured.append(sql)
        return ("ranked",)

    real_fetchall, real_fetchone = awards.db.fetchall, awards.db.fetchone
    awards.db.fetchall, awards.db.fetchone = fake_fetchall, fake_fetchone
    try:
        start = dt.datetime(2026, 8, 3, tzinfo=awards.LONDON)
        end = dt.datetime(2026, 8, 10, tzinfo=awards.LONDON)
        asyncio.run(awards.fetch_window_rows(start, end))
        asyncio.run(awards.fetch_game_rows(start, end))
        asyncio.run(awards.fetch_partner_rows(start, end))
        for sql in captured:
            # Remakes never count; scope filtering is pure Python, so no
            # queue pinning may creep back into the SQL.
            assert "COALESCE(ms.early_surrender, 0) = 0" in sql
            assert "queue_id = %" not in sql
        assert any("ms.queue_id" in sql for sql in captured)  # queues ARE fetched
        # fetch_adjustments picks up the awards_scope default (fake
        # fetchone answers "ranked" for the scope key).
        captured.clear()
        adj = asyncio.run(awards.fetch_adjustments(WEEK))
        assert adj.default_scope == "ranked"
    finally:
        awards.db.fetchall, awards.db.fetchone = real_fetchall, real_fetchone


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(TESTS)} award-control fixture tests passed")
