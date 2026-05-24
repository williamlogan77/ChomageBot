"""DB QA / consistency check for ChomageBot's SQLite snapshot.

Read-only. Runs a battery of invariants across match_stats, league_history,
and league_players and prints a human-readable report. Useful for catching
Riot-API quirks, schema drift, and partial backfills before they show up
as weird chart outliers in /match_stats_panel.

Usage:
    python scripts/qa_match_stats.py [path/to/database.sqlite]

Defaults to Bot/db/database.sqlite. Exits 1 if any [ERROR]-level check
fires, else 0. Never writes — opens the DB with ?mode=ro.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

KNOWN_TIERS = (
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
    "MASTER",
    "GRANDMASTER",
    "CHALLENGER",
    "UNRANKED",
)
APEX_TIERS = ("MASTER", "GRANDMASTER", "CHALLENGER")


@dataclass
class Report:
    ok: int = 0
    warn: int = 0
    error: int = 0
    lines: list[str] = field(default_factory=list)

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"=== {title} ===")

    def emit(self, label: str, value: str, status: str, detail: str = "") -> None:
        # status is one of "OK", "WARN", "ERROR" — counted separately so the
        # summary line can drive exit-code policy.
        if status == "OK":
            self.ok += 1
        elif status == "WARN":
            self.warn += 1
        elif status == "ERROR":
            self.error += 1
        suffix = f" - {detail}" if detail else ""
        self.lines.append(f"{label:<26}: {value} [{status}{suffix}]")

    def samples(self, rows: list[str]) -> None:
        for r in rows:
            self.lines.append(f"    {r}")

    def note(self, text: str) -> None:
        self.lines.append(text)

    def render(self) -> str:
        self.lines.append("")
        self.lines.append(f"Summary: {self.ok} OK, {self.warn} WARN, {self.error} ERROR")
        return "\n".join(self.lines).lstrip("\n")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _fmt_count(n: int) -> str:
    return f"{n:,}"


def _trunc_puuid(p: str | None) -> str:
    if p is None:
        return "<null>"
    return p[:8] + "..." if len(p) > 8 else p


def check_match_stats(con: sqlite3.Connection, r: Report) -> None:
    r.section("match_stats")
    if not _table_exists(con, "match_stats"):
        r.emit(
            "table presence",
            "missing",
            "ERROR",
            "match_stats not in DB; skipping checks 1-11",
        )
        return

    # 1. Total + composite-PK invariant
    total = con.execute("SELECT COUNT(*) FROM match_stats").fetchone()[0]
    distinct = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM match_stats GROUP BY match_id, puuid)"
    ).fetchone()[0]
    r.emit("total rows", _fmt_count(total), "OK")
    if total == distinct:
        r.emit(
            "distinct (match_id,puuid)",
            _fmt_count(distinct),
            "OK",
            "composite PK invariant holds",
        )
    else:
        r.emit(
            "distinct (match_id,puuid)",
            _fmt_count(distinct),
            "ERROR",
            f"{total - distinct} duplicate composite-key rows",
        )

    # 2. Future-dated games
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    fut_n = con.execute("SELECT COUNT(*) FROM match_stats WHERE game_start > ?", (now,)).fetchone()[
        0
    ]
    r.emit(
        "future-dated games",
        _fmt_count(fut_n),
        "OK" if fut_n == 0 else "ERROR",
        "should be 0" if fut_n else "",
    )
    if fut_n:
        rows = con.execute(
            "SELECT game_start, puuid, champion FROM match_stats "
            "WHERE game_start > ? ORDER BY game_start DESC LIMIT 5",
            (now,),
        ).fetchall()
        r.samples([f"{gs} puuid={_trunc_puuid(p)} champion={c}" for gs, p, c in rows])

    # 3. Duration outliers
    short_n = con.execute("SELECT COUNT(*) FROM match_stats WHERE duration_sec < 180").fetchone()[0]
    r.emit(
        "games < 180s",
        _fmt_count(short_n),
        "OK" if short_n == 0 else "WARN",
        "likely remakes/dodges" if short_n else "",
    )
    if short_n:
        rows = con.execute(
            "SELECT game_start, puuid, champion, duration_sec FROM match_stats "
            "WHERE duration_sec < 180 ORDER BY game_start DESC LIMIT 5"
        ).fetchall()
        r.samples(
            [f"{gs} puuid={_trunc_puuid(p)} champion={c} duration={d}s" for gs, p, c, d in rows]
        )

    long_n = con.execute("SELECT COUNT(*) FROM match_stats WHERE duration_sec > 7200").fetchone()[0]
    r.emit(
        "duration > 2h",
        _fmt_count(long_n),
        "OK" if long_n == 0 else "WARN",
        "suspiciously long" if long_n else "",
    )
    if long_n:
        rows = con.execute(
            "SELECT game_start, puuid, champion, duration_sec FROM match_stats "
            "WHERE duration_sec > 7200 ORDER BY duration_sec DESC LIMIT 5"
        ).fetchall()
        r.samples(
            [f"{gs} puuid={_trunc_puuid(p)} champion={c} duration={d}s" for gs, p, c, d in rows]
        )

    # 4. Win invariant
    bad_win = con.execute("SELECT COUNT(*) FROM match_stats WHERE win NOT IN (0, 1)").fetchone()[0]
    r.emit(
        "win NOT IN (0,1)",
        _fmt_count(bad_win),
        "OK" if bad_win == 0 else "ERROR",
    )

    # 5. Negative KDA
    neg_kda = con.execute(
        "SELECT COUNT(*) FROM match_stats WHERE kills < 0 OR deaths < 0 OR assists < 0"
    ).fetchone()[0]
    r.emit(
        "negative kda values",
        _fmt_count(neg_kda),
        "OK" if neg_kda == 0 else "ERROR",
    )

    # 6. Suspicious KDA (top end)
    hi_kda = con.execute(
        "SELECT COUNT(*) FROM match_stats WHERE kills > 50 OR deaths > 30 OR assists > 70"
    ).fetchone()[0]
    r.emit(
        "extreme kda outliers",
        _fmt_count(hi_kda),
        "OK" if hi_kda == 0 else "WARN",
        "kills>50 or deaths>30 or assists>70" if hi_kda else "",
    )
    if hi_kda:
        rows = con.execute(
            "SELECT game_start, puuid, champion, kills, deaths, assists "
            "FROM match_stats WHERE kills > 50 OR deaths > 30 OR assists > 70 "
            "ORDER BY (kills + assists + deaths) DESC LIMIT 5"
        ).fetchall()
        r.samples(
            [f"{gs} puuid={_trunc_puuid(p)} champion={c} {k}/{d}/{a}" for gs, p, c, k, d, a in rows]
        )

    # 7. NULL critical fields. Schema declares all NOT NULL, but check anyway —
    # a corrupt restore or future ALTER could let NULLs slip in.
    null_crit = con.execute(
        "SELECT COUNT(*) FROM match_stats "
        "WHERE champion IS NULL OR queue_id IS NULL OR game_start IS NULL"
    ).fetchone()[0]
    r.emit(
        "null critical fields",
        _fmt_count(null_crit),
        "OK" if null_crit == 0 else "ERROR",
        "champion/queue_id/game_start" if null_crit else "",
    )

    # 8. Orphan puuids
    orphan_n = con.execute(
        "SELECT COUNT(*) FROM match_stats "
        "WHERE puuid NOT IN (SELECT puuid FROM league_players WHERE puuid IS NOT NULL)"
    ).fetchone()[0]
    r.emit(
        "orphan puuid rows",
        _fmt_count(orphan_n),
        "OK" if orphan_n == 0 else "WARN",
        "matches for untracked accounts" if orphan_n else "",
    )
    if orphan_n:
        rows = con.execute(
            "SELECT DISTINCT puuid FROM match_stats "
            "WHERE puuid NOT IN (SELECT puuid FROM league_players WHERE puuid IS NOT NULL) "
            "LIMIT 5"
        ).fetchall()
        r.samples([f"puuid={_trunc_puuid(p)}" for (p,) in rows])

    # 9. Queue distribution (top 10)
    queues = con.execute(
        "SELECT queue_id, COUNT(*) FROM match_stats GROUP BY queue_id "
        "ORDER BY COUNT(*) DESC LIMIT 10"
    ).fetchall()
    r.emit("queue distribution top10", f"{len(queues)} buckets", "OK")
    r.samples([f"queue_id={q} count={_fmt_count(c)}" for q, c in queues])

    # 10. Per-puuid game count
    per_puuid = con.execute(
        "SELECT puuid, COUNT(*) FROM match_stats GROUP BY puuid ORDER BY COUNT(*) DESC"
    ).fetchall()
    r.emit("per-puuid game count", f"{len(per_puuid)} puuids", "OK")
    if per_puuid:
        top5 = per_puuid[:5]
        bot5 = per_puuid[-5:]
        r.note("    top 5:")
        r.samples([f"puuid={_trunc_puuid(p)} games={_fmt_count(n)}" for p, n in top5])
        r.note("    bottom 5:")
        r.samples([f"puuid={_trunc_puuid(p)} games={_fmt_count(n)}" for p, n in bot5])
        low_n = sum(1 for _, n in per_puuid if n < 10)
        r.emit(
            "puuids with <10 games",
            _fmt_count(low_n),
            "OK" if low_n == 0 else "WARN",
            "possibly incomplete backfill" if low_n else "",
        )

    # 11. Duplicates — under composite PK should be zero, but worth verifying.
    dup_rows = con.execute(
        "SELECT match_id, puuid, COUNT(*) c FROM match_stats "
        "GROUP BY match_id, puuid HAVING c > 1 LIMIT 5"
    ).fetchall()
    dup_n = len(dup_rows)
    r.emit(
        "duplicate (match_id,puuid)",
        _fmt_count(dup_n),
        "OK" if dup_n == 0 else "ERROR",
    )
    if dup_rows:
        r.samples([f"match_id={m} puuid={_trunc_puuid(p)} count={c}" for m, p, c in dup_rows])


def check_league_history(con: sqlite3.Connection, r: Report) -> None:
    r.section("league_history")
    if not _table_exists(con, "league_history"):
        r.emit("table presence", "missing", "ERROR", "league_history not in DB")
        return

    # 12. Totals
    total = con.execute("SELECT COUNT(*) FROM league_history").fetchone()[0]
    distinct_puuid = con.execute("SELECT COUNT(DISTINCT puuid) FROM league_history").fetchone()[0]
    r.emit("total rows", _fmt_count(total), "OK")
    r.emit("distinct puuids", _fmt_count(distinct_puuid), "OK")

    # 13. Future-dated rows
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    fut_n = con.execute(
        "SELECT COUNT(*) FROM league_history WHERE timestamp > ?", (now,)
    ).fetchone()[0]
    r.emit(
        "future-dated rows",
        _fmt_count(fut_n),
        "OK" if fut_n == 0 else "ERROR",
        "should be 0" if fut_n else "",
    )
    if fut_n:
        rows = con.execute(
            "SELECT timestamp, puuid, tier, division, lp FROM league_history "
            "WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 5",
            (now,),
        ).fetchall()
        r.samples(
            [
                f"{t} puuid={_trunc_puuid(p)} {ti or '?'} {d or '?'} lp={lp}"
                for t, p, ti, d, lp in rows
            ]
        )

    # 14. Tri-state NULL invariant for (lp, tier, division)
    # All three should be present together or all absent. XOR any pair.
    mixed = con.execute(
        "SELECT COUNT(*) FROM league_history WHERE "
        "((lp IS NULL) <> (tier IS NULL)) OR ((tier IS NULL) <> (division IS NULL))"
    ).fetchone()[0]
    r.emit(
        "lp/tier/division mixed NULL",
        _fmt_count(mixed),
        "OK" if mixed == 0 else "WARN",
        "should be all-present or all-NULL" if mixed else "",
    )
    if mixed:
        rows = con.execute(
            "SELECT timestamp, puuid, lp, tier, division FROM league_history "
            "WHERE ((lp IS NULL) <> (tier IS NULL)) "
            "OR ((tier IS NULL) <> (division IS NULL)) LIMIT 5"
        ).fetchall()
        r.samples(
            [f"{t} puuid={_trunc_puuid(p)} lp={lp} tier={ti} div={d}" for t, p, lp, ti, d in rows]
        )

    # 15. Orphan puuids
    orphan_n = con.execute(
        "SELECT COUNT(*) FROM league_history "
        "WHERE puuid NOT IN (SELECT puuid FROM league_players WHERE puuid IS NOT NULL)"
    ).fetchone()[0]
    r.emit(
        "orphan puuid rows",
        _fmt_count(orphan_n),
        "OK" if orphan_n == 0 else "WARN",
        "history for untracked accounts" if orphan_n else "",
    )

    # 16. Per-puuid history span
    spans = con.execute(
        "SELECT puuid, MIN(timestamp), MAX(timestamp), COUNT(*) "
        "FROM league_history GROUP BY puuid ORDER BY COUNT(*) DESC"
    ).fetchall()
    r.emit("per-puuid history span", f"{len(spans)} puuids", "OK")
    if spans:
        r.note("    top 5 by row count:")
        r.samples(
            [
                f"puuid={_trunc_puuid(p)} first={mn} last={mx} rows={_fmt_count(n)}"
                for p, mn, mx, n in spans[:5]
            ]
        )
        r.note("    bottom 5 by row count:")
        r.samples(
            [
                f"puuid={_trunc_puuid(p)} first={mn} last={mx} rows={_fmt_count(n)}"
                for p, mn, mx, n in spans[-5:]
            ]
        )

    # 17. Longest day-gap per puuid (top 5). SQLite has no native datediff; pull
    # rows ordered and compute deltas in Python — clearer than a CTE here.
    per_puuid_ts = defaultdict(list)
    for puuid, ts in con.execute(
        "SELECT puuid, timestamp FROM league_history ORDER BY puuid, timestamp"
    ):
        per_puuid_ts[puuid].append(ts)
    gap_records: list[tuple[str, float, str, str]] = []
    for puuid, ts_list in per_puuid_ts.items():
        if len(ts_list) < 2:
            continue
        max_gap = 0.0
        gap_start = gap_end = ts_list[0]
        for prev, nxt in zip(ts_list, ts_list[1:], strict=False):
            try:
                d_prev = datetime.fromisoformat(prev)
                d_nxt = datetime.fromisoformat(nxt)
            except (TypeError, ValueError):
                continue
            delta_days = (d_nxt - d_prev).total_seconds() / 86400.0
            if delta_days > max_gap:
                max_gap = delta_days
                gap_start, gap_end = prev, nxt
        if max_gap > 0:
            gap_records.append((puuid, max_gap, gap_start, gap_end))
    gap_records.sort(key=lambda x: x[1], reverse=True)
    r.emit("longest gaps (days)", f"top 5 across {len(gap_records)} puuids", "OK")
    r.samples(
        [f"puuid={_trunc_puuid(p)} gap={g:.1f}d from={s} to={e}" for p, g, s, e in gap_records[:5]]
    )

    # 18. Invalid tier values
    bad_tier = con.execute(
        f"SELECT COUNT(*) FROM league_history "
        f"WHERE tier IS NOT NULL "
        f"AND UPPER(tier) NOT IN ({','.join(['?'] * len(KNOWN_TIERS))})",
        KNOWN_TIERS,
    ).fetchone()[0]
    r.emit(
        "invalid tier values",
        _fmt_count(bad_tier),
        "OK" if bad_tier == 0 else "ERROR",
    )
    if bad_tier:
        rows = con.execute(
            f"SELECT DISTINCT tier FROM league_history "
            f"WHERE tier IS NOT NULL "
            f"AND UPPER(tier) NOT IN ({','.join(['?'] * len(KNOWN_TIERS))}) LIMIT 5",
            KNOWN_TIERS,
        ).fetchall()
        r.samples([f"tier={t!r}" for (t,) in rows])

    # 19. Invalid LP for non-apex tiers (Master+ uncapped).
    placeholders = ",".join(["?"] * len(APEX_TIERS))
    bad_lp = con.execute(
        f"SELECT COUNT(*) FROM league_history "
        f"WHERE lp IS NOT NULL AND (lp < 0 OR lp > 100) "
        f"AND (tier IS NULL OR UPPER(tier) NOT IN ({placeholders}))",
        APEX_TIERS,
    ).fetchone()[0]
    r.emit(
        "invalid lp (non-apex)",
        _fmt_count(bad_lp),
        "OK" if bad_lp == 0 else "WARN",
        "LP outside [0,100] for non-Master+" if bad_lp else "",
    )
    if bad_lp:
        rows = con.execute(
            f"SELECT timestamp, puuid, tier, division, lp FROM league_history "
            f"WHERE lp IS NOT NULL AND (lp < 0 OR lp > 100) "
            f"AND (tier IS NULL OR UPPER(tier) NOT IN ({placeholders})) LIMIT 5",
            APEX_TIERS,
        ).fetchall()
        r.samples([f"{t} puuid={_trunc_puuid(p)} {ti} {d} lp={lp}" for t, p, ti, d, lp in rows])


def check_league_players(con: sqlite3.Connection, r: Report) -> None:
    r.section("league_players")
    if not _table_exists(con, "league_players"):
        r.emit("table presence", "missing", "ERROR", "league_players not in DB")
        return

    # 20. Totals
    total = con.execute("SELECT COUNT(*) FROM league_players").fetchone()[0]
    distinct_users = con.execute(
        "SELECT COUNT(DISTINCT discord_user_id) FROM league_players"
    ).fetchone()[0]
    r.emit("total rows", _fmt_count(total), "OK")
    r.emit("distinct discord_user_id", _fmt_count(distinct_users), "OK")

    # 21. NULL / empty puuid
    null_puuid = con.execute(
        "SELECT COUNT(*) FROM league_players WHERE puuid IS NULL OR puuid = ''"
    ).fetchone()[0]
    r.emit(
        "null/empty puuid",
        _fmt_count(null_puuid),
        "OK" if null_puuid == 0 else "ERROR",
    )

    # 22. Accounts-per-user distribution (informational, not a violation).
    per_user = con.execute(
        "SELECT discord_user_id, COUNT(*) FROM league_players GROUP BY discord_user_id"
    ).fetchall()
    bucket_1 = sum(1 for _, n in per_user if n == 1)
    bucket_2 = sum(1 for _, n in per_user if n == 2)
    bucket_3p = sum(1 for _, n in per_user if n >= 3)
    r.emit(
        "accounts per user",
        f"1:{bucket_1} 2:{bucket_2} 3+:{bucket_3p}",
        "OK",
        "informational; multi-account is normal",
    )

    # 23. Orphan discord_user_id (league_players row without a matching users row).
    # Schema doesn't enforce FK so this can drift legitimately; WARN, not ERROR.
    if _table_exists(con, "users"):
        orphan_n = con.execute(
            "SELECT COUNT(*) FROM league_players "
            "WHERE discord_user_id NOT IN (SELECT user_id FROM users)"
        ).fetchone()[0]
        r.emit(
            "orphan discord_user_id",
            _fmt_count(orphan_n),
            "OK" if orphan_n == 0 else "WARN",
            "no matching users row" if orphan_n else "",
        )
        if orphan_n:
            rows = con.execute(
                "SELECT discord_user_id, league_username FROM league_players "
                "WHERE discord_user_id NOT IN (SELECT user_id FROM users) LIMIT 5"
            ).fetchall()
            r.samples([f"discord_user_id={u} league_username={n}" for u, n in rows])
    else:
        r.emit("orphan discord_user_id", "skipped", "WARN", "users table missing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "db_path",
        nargs="?",
        default="Bot/db/database.sqlite",
        help="Path to SQLite DB (default: Bot/db/database.sqlite)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"[ERROR] DB file not found: {db_path}")
        return 1

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"[ERROR] failed to open DB read-only: {exc}")
        return 1

    report = Report()
    report.note(f"DB: {db_path.resolve()}")
    report.note(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        check_match_stats(con, report)
        check_league_history(con, report)
        check_league_players(con, report)
    finally:
        con.close()

    print(report.render())
    return 1 if report.error > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
