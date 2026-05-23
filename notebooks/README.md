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

## Three surfaces

The analysis code lives in **`Bot/utils/match_analysis.py`** and is shared by:

1. **`/match_stats` Discord slash command** (cog: `Bot/cogs/match_analysis.py`)
   — interactive view with player dropdown + one button per chart.
2. **`notebooks/match_analysis.ipynb`** — exploratory notebook, top-down + per-player.
3. **`notebooks/run_analysis.py`** — batch runner, saves PNGs under `notebooks/charts/`.

Any chart you add to `match_analysis.ALL_PLOTS` shows up in all three.

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

1. **Cumulative winrate over time** — hot/cold streak detector by date.
2. **Lifetime progression** — rolling WR vs *cumulative game number*. Answers
   "does playing more correlate with getting better or worse?". Aggregate
   panel labels each player with their pp/game slope; single-player adds
   a linear fit.
3. **KDA vs outcome** — does carrying correlate with winning?
4. **Game duration** — short stomps vs long scaling games.
5. **Champion winners vs losers** — best WR champs (left) next to worst
   (right), with min-games guard.
6. **Champion learning curve** — does winrate improve with reps on a champ?
7. **Hour of day** — tilt hours, prime hours.
8. **Day of week** — weekend warrior?
9. **Hour × DoW heatmap** — combined view, sparse cells blanked.
10. **Tilt check** — winrate vs entering loss-streak length.
11. **Time since previous game** — back-to-back queue vs fresh session.

All functions live in `Bot/utils/match_analysis.py`. Add a new factor by
appending to `ALL_PLOTS` *and* adding an entry to `CHART_DEFS` in
`Bot/cogs/match_analysis.py` if you want it surfaced in Discord.
