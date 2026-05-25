"""Chart-suite smoke test for ChomageBot's match-analysis plots.

Runs every entry in ``Bot.utils.match_analysis.ALL_PLOTS`` against a DB,
in four modes: aggregate (all players) plus per-person for the highest /
median / lowest game-count player. Reports pass/fail per chart, exits 0
when every call succeeded.

Catches regressions before they reach the ``/match_stats_panel`` view.
Read-only — never writes to the DB.

Usage:
    python scripts/smoke_test_plots.py [path/to/database.sqlite]

Defaults to ``Bot/db/database.sqlite``.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import matplotlib

# Headless backend before any pyplot import (transitive via match_analysis).
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

# Make Bot.utils importable when running from project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from Bot.utils.match_analysis import ALL_PLOTS, load_matches  # noqa: E402

_DEFAULT_DB = _PROJECT_ROOT / "Bot" / "db" / "database.sqlite"
_STEM_WIDTH = 35
_ERR_WIDTH = 80


def _format_error(exc: BaseException) -> str:
    """Single-line ``Type: message``, truncated for table readability."""
    msg = f"{type(exc).__name__}: {exc}"
    msg = msg.replace("\n", "; ").replace("\r", "")
    if len(msg) > _ERR_WIDTH:
        msg = msg[: _ERR_WIDTH - 3] + "..."
    return msg


def _pick_sample_players(df) -> tuple[str, str, str]:
    """Return (highest, median, lowest) by game count.

    Sort descending; pick index 0, n//2, n-1. With 10 people that's the
    1st / 6th / 10th most-active person.
    """
    counts = df.groupby("person").size().sort_values(ascending=False)
    names = counts.index.tolist()
    n = len(names)
    return names[0], names[n // 2], names[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "db_path",
        nargs="?",
        default=str(_DEFAULT_DB),
        help=f"Path to SQLite DB (default: {_DEFAULT_DB})",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    print("=== Chart smoke test ===")
    print(f"DB: {db_path}")

    try:
        df = load_matches(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"No data, can't smoke test: {_format_error(exc)}")
        return 1

    if df.empty:
        print("No data, can't smoke test: load_matches returned 0 rows")
        return 1

    n_people = df["person"].nunique()
    print(f"Rows: {len(df):,} | People: {n_people}")

    if n_people < 1:
        print("No data, can't smoke test: no people in df")
        return 1

    high, mid, low = _pick_sample_players(df)
    counts = df.groupby("person").size()
    print(
        f"Sample players: {high} ({counts[high]}), "
        f"{mid} ({counts[mid]}), "
        f"{low} ({counts[low]})"
    )
    print()

    modes: list[tuple[str, str | None]] = [
        ("agg", None),
        ("high", f"person:{high}"),
        ("mid", f"person:{mid}"),
        ("low", f"person:{low}"),
    ]

    failures: list[tuple[str, str, str]] = []  # (stem, mode_label, err)
    total = len(ALL_PLOTS)

    for i, (stem, fn) in enumerate(ALL_PLOTS, start=1):
        parts: list[str] = []
        for mode_label, player in modes:
            fig = None
            try:
                fig = fn(df, player=player)
                parts.append(f"{mode_label} OK")
            except Exception as exc:  # noqa: BLE001
                err = _format_error(exc)
                failures.append((stem, mode_label, err))
                parts.append(f"{mode_label} FAIL: {err}")
            finally:
                # Belt-and-suspenders: close the returned figure if any,
                # then close any stray figure left by a partial render.
                if fig is not None:
                    plt.close(fig)
                plt.close("all")

        idx = f"[{i}/{total}]"
        print(f"{idx:<8} {stem:<{_STEM_WIDTH}} ... " + "  | ".join(parts))

    print()
    print("=== Summary ===")
    n_calls = total * len(modes)
    n_failed = len(failures)
    n_passed = n_calls - n_failed
    failed_stems = {stem for stem, _, _ in failures}
    print(f"{total} charts x {len(modes)} modes = {n_calls} calls")
    print(f"Passed: {n_passed}")
    print(
        f"Failed: {n_failed}"
        + (
            f" ({len(failed_stems)} chart{'s' if len(failed_stems) != 1 else ''})"
            if n_failed
            else ""
        )
    )
    for stem, mode_label, err in failures:
        print(f"  - {stem} @ {mode_label}: {err}")

    exit_code = 0 if n_failed == 0 else 1
    print()
    print(f"Exit code: {exit_code}")
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
