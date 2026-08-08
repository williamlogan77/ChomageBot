"""Match-V5 per-participant detail: column list, extractor, backfill SQL.

Single source of truth shared by the ingest path (cogs/backfill.py), the
zero-API historical backfill (the /backfill_detail_from_raw command and
scripts/backfill_match_detail_from_raw.py), and anything else that needs
to agree on what "the detail columns" are. Lives in utils so cogs can
import it without utils ever importing cogs.

Field names validated against Riot's API reference schema
(mingweisamuel.com/riotapi-schema OpenAPI export), 2026-08-08.
"""

from __future__ import annotations

# Every *Pings counter Match-V5 exposes on a participant. Summed into
# match_stats.pings_total; enemyMissingPings ("?" spam) also gets its own
# column because it's the banter-relevant one.
PING_FIELDS = (
    "allInPings",
    "assistMePings",
    "baitPings",
    "basicPings",
    "commandPings",
    "dangerPings",
    "enemyMissingPings",
    "enemyVisionPings",
    "getBackPings",
    "holdPings",
    "needVisionPings",
    "onMyWayPings",
    "pushPings",
    "retreatPings",
    "visionClearedPings",
)

# match_stats detail columns beyond the original core extract, in the
# order participant_detail emits their values. The ingest INSERT, the
# raw-payload backfill UPDATE below, and setup.postgres.sql must agree on
# these names — the first two are generated from this tuple so they can't
# drift.
DETAIL_COLUMNS = (
    "damage_to_champs",
    "damage_taken",
    "gold_earned",
    "cs",
    "vision_score",
    "wards_placed",
    "wards_killed",
    "control_wards",
    "pings_total",
    "pings_missing",
    "largest_multi_kill",
    "penta_kills",
    "time_dead_sec",
    "turret_takedowns",
    "objectives_stolen",
    "first_blood",
    "early_surrender",
    "solo_kills",
    "kill_participation",
    "team_damage_pct",
)


def participant_detail(participant: dict) -> tuple:
    """Values for DETAIL_COLUMNS, in order, from one Match-V5 participant.

    Missing fields yield None (SQL NULL, "Riot didn't say") rather than a
    fake 0 — pings and the challenges sub-object don't exist on payloads
    older than the patches that introduced them. pings_total is NULL when
    the payload has no ping counters at all, so a genuine 0-ping game
    stays distinguishable from "no data".
    """

    def _num(key: str) -> int | None:
        value = participant.get(key)
        # bool is an int subclass, so firstBloodKill-style flags also
        # pass through here as 0/1.
        return int(value) if isinstance(value, int | bool) else None

    total_cs = None
    lane_cs, jungle_cs = _num("totalMinionsKilled"), _num("neutralMinionsKilled")
    if lane_cs is not None and jungle_cs is not None:
        total_cs = lane_cs + jungle_cs

    pings_total = None
    if any(key in participant for key in PING_FIELDS):
        pings_total = sum(int(participant.get(key) or 0) for key in PING_FIELDS)

    challenges = participant.get("challenges") or {}
    kill_participation = challenges.get("killParticipation")
    team_damage_pct = challenges.get("teamDamagePercentage")

    return (
        _num("totalDamageDealtToChampions"),
        _num("totalDamageTaken"),
        _num("goldEarned"),
        total_cs,
        _num("visionScore"),
        _num("wardsPlaced"),
        _num("wardsKilled"),
        _num("detectorWardsPlaced"),
        pings_total,
        _num("enemyMissingPings"),
        _num("largestMultiKill"),
        _num("pentaKills"),
        _num("totalTimeSpentDead"),
        _num("turretTakedowns"),
        _num("objectivesStolen"),
        _num("firstBloodKill"),
        _num("gameEndedInEarlySurrender"),
        challenges.get("soloKills"),
        float(kill_participation) if kill_participation is not None else None,
        float(team_damage_pct) if team_damage_pct is not None else None,
    )


# ---------------------------------------------------------------------
# Zero-API historical backfill: fill the detail columns for rows written
# before they existed, straight from the archived match_raw payloads.
# SQL expression per detail column, evaluated against p.part (the
# participant JSONB object).

_PING_SUM = " + ".join(f"COALESCE((p.part ->> '{field}')::int, 0)" for field in PING_FIELDS)
_PING_PRESENT = "p.part ?| ARRAY[" + ", ".join(f"'{field}'" for field in PING_FIELDS) + "]"

_COLUMN_EXPRESSIONS: dict[str, str] = {
    "damage_to_champs": "(p.part ->> 'totalDamageDealtToChampions')::int",
    "damage_taken": "(p.part ->> 'totalDamageTaken')::int",
    "gold_earned": "(p.part ->> 'goldEarned')::int",
    "cs": "(p.part ->> 'totalMinionsKilled')::int + (p.part ->> 'neutralMinionsKilled')::int",
    "vision_score": "(p.part ->> 'visionScore')::int",
    "wards_placed": "(p.part ->> 'wardsPlaced')::int",
    "wards_killed": "(p.part ->> 'wardsKilled')::int",
    "control_wards": "(p.part ->> 'detectorWardsPlaced')::int",
    # NULL (not 0) when the payload predates ping counters, mirroring
    # participant_detail's "no data" vs "0 pings" distinction.
    "pings_total": f"CASE WHEN {_PING_PRESENT} THEN {_PING_SUM} END",
    "pings_missing": "(p.part ->> 'enemyMissingPings')::int",
    "largest_multi_kill": "(p.part ->> 'largestMultiKill')::smallint",
    "penta_kills": "(p.part ->> 'pentaKills')::smallint",
    "time_dead_sec": "(p.part ->> 'totalTimeSpentDead')::int",
    "turret_takedowns": "(p.part ->> 'turretTakedowns')::smallint",
    "objectives_stolen": "(p.part ->> 'objectivesStolen')::smallint",
    "first_blood": "((p.part ->> 'firstBloodKill')::boolean)::int",
    "early_surrender": "((p.part ->> 'gameEndedInEarlySurrender')::boolean)::int",
    "solo_kills": "(p.part -> 'challenges' ->> 'soloKills')::smallint",
    "kill_participation": "(p.part -> 'challenges' ->> 'killParticipation')::real",
    "team_damage_pct": "(p.part -> 'challenges' ->> 'teamDamagePercentage')::real",
}

assert set(_COLUMN_EXPRESSIONS) == set(DETAIL_COLUMNS), (
    "raw-backfill expressions out of sync with DETAIL_COLUMNS: "
    f"{set(_COLUMN_EXPRESSIONS) ^ set(DETAIL_COLUMNS)}"
)

_SET_CLAUSE = ",\n    ".join(
    f"{column} = {_COLUMN_EXPRESSIONS[column]}" for column in DETAIL_COLUMNS
)

# The participants array is exploded once in a subquery (10 rows per
# match) and joined back on puuid — UPDATE targets can't be referenced
# from a LATERAL item, so the explode-then-join shape is the portable one.
# Only rows still missing detail (damage_to_champs IS NULL) are touched:
# idempotent, and it can never overwrite a value the live stream wrote.
RAW_BACKFILL_UPDATE_SQL = f"""
UPDATE match_stats AS ms
SET {_SET_CLAUSE}
FROM (
    SELECT mr.match_id, parts.part
    FROM match_raw mr
    CROSS JOIN LATERAL jsonb_array_elements(
        mr.payload -> 'info' -> 'participants'
    ) AS parts(part)
) AS p
WHERE p.match_id = ms.match_id
  AND p.part ->> 'puuid' = ms.puuid
  AND ms.damage_to_champs IS NULL
"""

# fixable = rows this UPDATE can fill (payload archived); missing_payload
# = rows needing /backfill_all all_history=True first (API re-fetch).
RAW_BACKFILL_COUNT_SQL = """
SELECT
    COUNT(*) FILTER (WHERE mr.match_id IS NOT NULL) AS fixable,
    COUNT(*) FILTER (WHERE mr.match_id IS NULL) AS missing_payload
FROM match_stats ms
LEFT JOIN match_raw mr ON mr.match_id = ms.match_id
WHERE ms.damage_to_champs IS NULL
"""
