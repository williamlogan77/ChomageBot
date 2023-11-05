from rank_sorting_class import Ranker
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

current_ticks = []
new_ticks = []
for league in Ranker.LEAGUES.keys():
    for subleague in Ranker.SUBLEAGUE.keys():
        x = Ranker(league, subleague, "0")
        current_ticks.append((x._score + 50))
        new_ticks.append(str(x).split(" - ")[0])

fig, ax = plt.subplots(figsize=(5, 5), dpi=1200)

y_lim = plt.gca().get_ylim()[1]

opacity = 0.8

colours = ["#a19d94", "#CD7F32", "#C0C0C0", "#FFD700", "#E5E4E2", "#50C878", "#b9f2ff"]

ax.set_yticks(current_ticks, new_ticks, rotation=90, fontsize=10, va="center")

for idx, colour in enumerate(colours):
    ax.axhspan(
        ymin=current_ticks[idx * 4] - 50,
        ymax=current_ticks[idx * 4 + 3] + 51,
        facecolor=colour,
        alpha=opacity,
    )

for idx, tick in enumerate(current_ticks):
    ax.axhline(
        y=current_ticks[idx] - 50,
        linewidth=1,
        color="black",
        linestyle="dashed",
        dashes=(5, 10),
    )


ax.xaxis.set_major_locator(mdates.DayLocator(interval=7))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%0d/%m/%y"))
plt.xticks(rotation=45, ha="center")


import pickle

with open("utils/my_fig.pickle", "wb") as f:
    pickle.dump(fig, file=f)
