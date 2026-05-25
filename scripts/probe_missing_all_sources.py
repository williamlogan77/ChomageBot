"""Quick coverage probe: for each of the 8 missing players, test each source.

Just hit each source's profile page and record HTTP status + body size.
Read-only. Output to stdout.
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
HDR = {"User-Agent": UA, "Accept": "text/html,*/*"}

# (name, tag) for the 8 missing players. tag = riot id tag (uppercase).
MISSING = [
    ("Keith Chegwin", "EUW"),
    ("OLAUGH", "EUW2"),
    ("Pouis Jon", "EUW"),
    ("Spike Tyson", "EUW"),
    ("Thosmabubla", "EUW"),
    ("Trash ADC", "EUW"),
    ("Vyce", "EUW"),
    ("loukia", "GREES"),
]


def make_url(source: str, name: str, tag: str) -> str:
    slug = urllib.parse.quote(f"{name}-{tag}")
    # loukia is GREES (Greek server) on Riot's side; Riot ID format is same
    # but the LoL region for op.gg/u.gg slug differs ("eune"/"euw"). Try both.
    if source == "opgg":
        region = "euw"
        return f"https://op.gg/lol/summoners/{region}/{slug}"
    if source == "ugg":
        region = "euw1"
        return f"https://u.gg/lol/profile/{region}/{slug}/overview"
    if source == "mobalytics":
        region = "euw"
        return f"https://app.mobalytics.gg/lol/profile/{region}/{slug}/overview"
    if source == "porofessor":
        region = "euw"
        return f"https://porofessor.gg/profile/{region}/{slug}"
    if source == "dpm":
        return f"https://dpm.lol/{slug}"
    if source == "leagueofgraphs":
        region = "euw"
        slug2 = urllib.parse.quote(f"{name}.{tag}")
        return f"https://www.leagueofgraphs.com/summoner/{region}/{slug2}"
    raise ValueError(source)


def probe(source: str, name: str, tag: str) -> dict:
    url = make_url(source, name, tag)
    req = urllib.request.Request(url, headers=HDR)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        body = r.read()
        return {"status": r.status, "size": len(body), "ct": r.headers.get("Content-Type", "")}
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return {
            "status": e.code,
            "size": len(body),
            "ct": e.headers.get("Content-Type", "") if e.headers else "",
        }
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "size": 0, "ct": f"ERR: {e}"}


def main() -> None:
    sources = ["opgg", "mobalytics", "porofessor", "dpm", "leagueofgraphs"]
    # Print header
    print(f"{'player':24s} | " + " | ".join(f"{s:14s}" for s in sources))
    print("-" * (28 + 17 * len(sources)))
    for name, tag in MISSING:
        cells = []
        for src in sources:
            r = probe(src, name, tag)
            cell = f"{r['status']:3d}/{r['size']:>6d}b"
            cells.append(f"{cell:14s}")
            time.sleep(1.5)
        print(f"{name + ' #' + tag:24s} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
