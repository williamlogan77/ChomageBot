"""Fixture tests: the ceremony honors dashboard award controls.

Drives utils/awards.compute_results + build_ceremony_blocks with
synthetic inputs — no DB, no Discord. Run from Bot/:

    python tests/test_award_controls.py

Covers: baseline winners, per-award exclusions ("on holiday"), forced
winners (qualifying, non-qualifying and excluded-beats-forced),
disabled awards, custom taglines, the management note, and the
season-reset skip.
"""

from __future__ import annotations

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
)

WEEK = __import__("datetime").date(2026, 8, 3)


def snap(puuid, uid, name, account, tier, division, lp, wins, losses):
    return (puuid, uid, name, account, tier, division, lp, wins, losses)


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
        # (user, name, prior 4w games, this week) — Loukia -80%, Dan -50%,
        # Alice ~-14%; Bob over his baseline (not a candidate).
        "volume_rows": [
            (1, "Alice", 28, 6),
            (2, "Bob", 30, 10),
            (3, "Loukia", 40, 2),
            (4, "Dan", 24, 3),
        ],
        # (user, name, games, duo, duo_wins, solo_wins) — Bob 6/8, Dan 4/6.
        "duo_rows": [
            (1, "Alice", 9, 3, 2, 3),
            (2, "Bob", 8, 6, 4, 1),
            (4, "Dan", 6, 4, 2, 1),
        ],
        "duo_partner_rows": [(2, 1, "Alice", 5), (1, 2, "Bob", 3), (4, 1, "Alice", 4)],
        # (user, name, k, d, a, champ, win, match) — Alice's 14 deaths lead.
        "int_rows": [
            (1, "Alice", 2, 14, 5, "Ahri", 0, "M1"),
            (2, "Bob", 4, 10, 9, "Braum", 1, "M2"),
            (3, "Loukia", 1, 9, 2, "Jinx", 0, "M3"),
        ],
        "boundary_reset": False,
    }
    fields.update(overrides)
    return AwardInputs(**fields)


def winner_ids(winners):
    return [w.user_id for w in winners]


def test_baseline():
    results, season_reset, forced = awards.compute_results(build_inputs())
    assert not season_reset and forced == frozenset()
    assert winner_ids(results[LP_CHAD]) == [1] and results[LP_CHAD][0].value == 30.0
    assert winner_ids(results[LP_LOSS]) == [3] and results[LP_LOSS][0].value == -40.0
    assert winner_ids(results[PUSSY]) == [3]
    assert results[PUSSY][0].detail["drop_pct"] == 80
    assert winner_ids(results[DUO_LEECH]) == [2]
    assert results[DUO_LEECH][0].detail["partner"] == "Alice"
    assert winner_ids(results[INT]) == [1] and results[INT][0].value == 14.0


def test_exclusions_recompute():
    adj = AwardAdjustments(excluded={PUSSY: frozenset({3}), LP_LOSS: frozenset({3})})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert forced == frozenset()
    # Loukia is "on holiday": Dan's -50% takes the volume award and
    # Bob's -30 LP becomes the biggest loss.
    assert winner_ids(results[PUSSY]) == [4]
    assert results[PUSSY][0].detail["drop_pct"] == 50
    assert winner_ids(results[LP_LOSS]) == [2] and results[LP_LOSS][0].value == -30.0
    # Untouched awards keep their winners.
    assert winner_ids(results[DUO_LEECH]) == [2]
    assert winner_ids(results[INT]) == [1]


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
    assert winner_ids(results[PUSSY]) == [3]


def test_exclusion_beats_forcing():
    adj = AwardAdjustments(forced={INT: 3}, excluded={INT: frozenset({3})})
    results, _, forced = awards.compute_results(build_inputs(), adj)
    assert INT not in forced
    assert winner_ids(results[INT]) == [1]


def test_season_reset_still_skips_lp_awards():
    results, season_reset, _ = awards.compute_results(
        build_inputs(boundary_reset=True), AwardAdjustments(forced={LP_CHAD: 1})
    )
    assert season_reset
    assert results[LP_CHAD] == [] and results[LP_LOSS] == []
    assert winner_ids(results[INT]) == [1]


def test_blocks_default_format_unchanged():
    results, season_reset, _ = awards.compute_results(build_inputs())
    blocks = awards.build_ceremony_blocks(WEEK, results, season_reset=season_reset)
    text = "".join(blocks)
    assert awards.MANAGEMENT_NOTE not in text
    for tagline in awards.DEFAULT_TAGLINES.values():
        assert tagline not in text


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


def test_compute_all_awards_parity_with_plain_compute_results():
    inputs = build_inputs()
    via_new, reset_new, _ = awards.compute_results(inputs)
    # Reproduce the legacy computation path directly from the pickers.
    per_user, reset_accounts = awards.net_lp_deltas(
        inputs.baseline, inputs.first_in_week, inputs.last_in_week
    )
    legacy = {
        LP_LOSS: awards.pick_lp_extreme(per_user, gain=False),
        LP_CHAD: awards.pick_lp_extreme(per_user, gain=True),
        PUSSY: awards.pick_volume_collapse(inputs.volume_rows),
        DUO_LEECH: awards.pick_duo_leech(inputs.duo_rows, inputs.duo_partner_rows),
        INT: awards.pick_int(inputs.int_rows),
    }
    assert via_new == legacy
    assert reset_new is False


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(TESTS)} award-control fixture tests passed")
