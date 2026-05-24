"""Probe a wider set of LP trackers for the 8 missing players.

Includes sources not in original directive: deeplol.gg, blitz.gg, lolprofile.net,
lolskill.net, op.gg via API directly, riftarena.com etc.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HDR = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}

PLAYERS = [
    ("Keith Chegwin", "EUW"),
    ("OLAUGH", "EUW2"),
    ("Pouis Jon", "EUW"),
    ("Spike Tyson", "EUW"),
    ("Thosmabubla", "EUW"),
    ("Trash ADC", "EUW"),
    ("Vyce", "EUW"),
    ("loukia", "GREES"),
]

SOURCES = {
    # source-name -> URL template ({slug}/{name}/{tag})
    "deeplol": "https://www.deeplol.gg/summoner/euw/{slug}",
    "blitz": "https://blitz.gg/lol/profile/euw1/{slug}",
    "lolprofile": "https://lolprofile.net/summoner/euw/{slug}",
    "tracker": "https://tracker.gg/lol/profile/riot/euw1/{slug}/overview",
    "lolvvv": "https://lolvvv.com/profile/euw/{slug}",
    "lolanalytics": "https://lolanalytics.com/lol/{slug}",
    "riftarena": "https://www.riftarena.com/lol/summoner/euw/{slug}",
    "uglol": "https://uglol.gg/summoners/euw/{slug}",
    "leaguespy": "https://leaguespy.gg/summoner/euw/{slug}",
}


def probe(url: str) -> dict:
    req = urllib.request.Request(url, headers=HDR)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        body = r.read()
        return {"status": r.status, "size": len(body)}
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return {"status": e.code, "size": len(body)}
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "size": 0, "err": str(e)[:40]}


def main() -> None:
    sources = list(SOURCES.keys())
    print(f"{'player':24s} | " + " | ".join(f"{s:11s}" for s in sources))
    print("-" * (28 + 14 * len(sources)))
    for name, tag in PLAYERS:
        slug = urllib.parse.quote(f"{name}-{tag}")
        cells = []
        for src in sources:
            url = SOURCES[src].format(slug=slug)
            r = probe(url)
            cell = f"{r['status']:3d}/{r['size']//1024}k"
            cells.append(f"{cell:11s}")
            time.sleep(1.0)
        print(f"{name + ' #' + tag:24s} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
