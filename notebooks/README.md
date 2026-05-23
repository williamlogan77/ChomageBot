# Match-stats EDA

Exploratory analysis of the bot's `match_stats` table. What factors actually
influence wins vs. losses for each tracked player.

## One-time setup

```powershell
python -m venv .venv-notebooks
.\.venv-notebooks\Scripts\Activate.ps1
pip install -r notebooks/requirements.txt
```

## Refresh the data

Pull a fresh snapshot of the live DB into `Bot/db/database.sqlite`:

```powershell
.\scripts\sync-db.ps1
```

(Requires `ssh root@192.168.0.3` to be working — see `docs/deployment.md`.)

## Two ways to run

**Notebook — exploratory.** Open `notebooks/match_analysis.ipynb` in
Jupyter / VSCode. Cells are organised by factor category; run top-down,
re-run individual cells to slice by player.

**Script — repeatable batch.** Renders every chart as a PNG under
`notebooks/charts/`. Used for "just give me everything for everyone":

```powershell
python notebooks\run_analysis.py
```

Useful flags:

```powershell
python notebooks\run_analysis.py --players "loukia" "Mr Chomage"
python notebooks\run_analysis.py --min-games 100      # skip thin samples
python notebooks\run_analysis.py --no-aggregate       # per-player only
```

Output layout:

```
notebooks/charts/
  summary.csv               # one row per player: games, WR, KDA, fav champ
  _aggregate/               # all players pooled
    01_cumulative_winrate.png
    02_kda_vs_outcome.png
    ...
  loukia/                   # one folder per player
    01_cumulative_winrate.png
    ...
```

`charts/` is gitignored — re-run the script whenever the data is refreshed.

## What's plotted

Each chart pair answers one factor question:

1. **Cumulative winrate over time** — hot/cold streak detector.
2. **KDA vs outcome** — does carrying correlate with winning?
3. **Game duration** — short stomps vs long scaling games.
4. **Champion winrate + volume** — best/worst picks (≥10 games each).
5. **Champion learning curve** — does winrate improve with reps?
6. **Hour of day** — tilt hours, prime hours.
7. **Day of week** — weekend warrior?
8. **Hour × DoW heatmap** — combined view, sparse cells blanked.
9. **Tilt check** — winrate vs entering loss-streak length.
10. **Time since previous game** — back-to-back queue vs fresh session.

All functions live in `analysis.py` and are used by both the notebook and
the batch runner; add a new factor by appending to `ALL_PLOTS`.
