"""Batch runner — render every plot as a PNG under notebooks/charts/.

Layout:
  notebooks/charts/_aggregate/         all players pooled
  notebooks/charts/<player_slug>/      per-player drill-downs
  notebooks/charts/summary.csv         per-player summary table

Usage (from repo root):
  python notebooks/run_analysis.py                     # all players
  python notebooks/run_analysis.py --players "loukia"  # subset
  python notebooks/run_analysis.py --min-games 100     # skip thin players
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display needed for batch
import matplotlib.pyplot as plt

# The analysis lib lives inside Bot/utils so the Discord cog can import
# it too. Add Bot/ to sys.path so we can use the same module here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Bot"))
from utils import match_analysis as analysis  # noqa: E402

CHARTS_ROOT = Path(__file__).resolve().parent / "charts"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def _render(df, out_dir: Path, player: str | None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for stem, fn in analysis.ALL_PLOTS:
        try:
            fig = fn(df, player=player)
        except Exception as exc:
            print(f"  ! {stem}: {exc!r}")
            continue
        target = out_dir / f"{stem}.png"
        fig.savefig(target, dpi=120, bbox_inches="tight")
        plt.close(fig)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=analysis.DEFAULT_DB)
    parser.add_argument(
        "--players",
        nargs="*",
        default=None,
        help="Specific players to render. Omit to render every player with ≥min_games.",
    )
    parser.add_argument(
        "--min-games",
        type=int,
        default=50,
        help="Skip per-player rendering for players below this game count.",
    )
    parser.add_argument(
        "--no-aggregate",
        action="store_true",
        help="Skip the all-players aggregate folder.",
    )
    args = parser.parse_args()

    print(f"Loading {args.db}...")
    df = analysis.load_matches(args.db)
    print(f"  {len(df)} match-rows across {df['player'].nunique()} players")

    CHARTS_ROOT.mkdir(exist_ok=True)
    summary = analysis.summary_table(df)
    summary.to_csv(CHARTS_ROOT / "summary.csv", index=False)
    print(f"Wrote {CHARTS_ROOT / 'summary.csv'}")

    if not args.no_aggregate:
        print("Rendering aggregate...")
        n = _render(df, CHARTS_ROOT / "_aggregate", player=None)
        print(f"  {n} charts -> {CHARTS_ROOT / '_aggregate'}")

    if args.players:
        targets = args.players
    else:
        targets = summary[summary["games"] >= args.min_games]["player"].tolist()

    for player in targets:
        sub = df[df["player"] == player]
        if sub.empty:
            print(f"  ! {player}: no rows, skipping")
            continue
        print(f"Rendering {player} ({len(sub)} games)...")
        n = _render(df, CHARTS_ROOT / _slug(player), player=player)
        print(f"  {n} charts -> {CHARTS_ROOT / _slug(player)}")


if __name__ == "__main__":
    main()
