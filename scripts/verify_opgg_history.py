"""Fetch full op.gg HTML (no RSC header) and grep for all season anchors.

Final verification step before declaring op.gg's coverage of OLAUGH/Vyce/loukia
exhausted. If multiple distinct in-gap seasons appear, the RSC parser missed
them and we need to write a backfill. If only one current-season anchor
appears, op.gg has no usable historic data.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
HDR = {"User-Agent": UA, "Accept": "text/html,*/*"}
CACHE = "/tmp/sources/opgg/full"

PLAYERS = [
    ("OLAUGH", "EUW2"),
    ("Vyce", "EUW"),
    ("loukia", "GREES"),
]


def fetch(slug: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = f"{CACHE}/{slug}.html"
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return open(path).read()
    enc = urllib.parse.quote(slug)
    url = f"https://op.gg/lol/summoners/euw/{enc}"
    req = urllib.request.Request(url, headers=HDR)
    body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
    with open(path, "w") as f:
        f.write(body)
    time.sleep(1.5)
    return body


def main() -> None:
    for name, tag in PLAYERS:
        slug = f"{name}-{tag}"
        print(f"\n############ {slug} ############")
        try:
            body = fetch(slug)
        except Exception as e:
            print(f"  fetch error: {e}")
            continue
        print(f"  size: {len(body)} bytes")

        # Look for distinct tier strings
        # Look for embedded RSC chunks inside script tags: self.__next_f.push([N, "..."])
        pushes = re.findall(r"self\.__next_f\.push\(\[\d+,\s*(\"(?:\\.|[^\"])*\")\]\)", body)
        print(f"  __next_f.push() chunks: {len(pushes)}")

        # Concat all into one big string, then look for season/tier patterns
        all_rsc = ""
        for p in pushes:
            # Unescape the JS string
            try:
                s = json.loads(p)
                all_rsc += s
            except json.JSONDecodeError:
                continue
        print(f"  concatenated RSC: {len(all_rsc)} chars")

        # Save for inspection
        with open(f"/tmp/sources/opgg/full/{slug}.rsc.concat.txt", "w") as f:
            f.write(all_rsc)

        # Now find all (season, tier, lp) triples — match the structure we saw in JS chunks
        # data: [{season: "S5", rank_entries: {rank_info: {tier: "silver 1", lp: "0"}}}, ...]
        entries = []
        # Match "season":"..." followed by "rank_entries":{...}
        pattern = re.compile(
            r'"season"\s*:\s*"([^"]+)"\s*,\s*"rank_entries"\s*:\s*\{[^{}]*?"rank_info"\s*:\s*\{[^{}]*?"tier"\s*:\s*"([^"]*)"[^{}]*?"lp"\s*:\s*("[^"]*"|null|\d+)'
        )
        for m in pattern.finditer(all_rsc):
            season, tier, lp = m.group(1), m.group(2), m.group(3)
            entries.append((season, tier, lp))

        # Also check inverse order
        pattern2 = re.compile(
            r'"rank_entries"\s*:\s*\{[^{}]*?"rank_info"\s*:\s*\{[^{}]*?"tier"\s*:\s*"([^"]*)"[^{}]*?"lp"\s*:\s*("[^"]*"|null|\d+)[^{}]*?\}[^{}]*?\}\s*,\s*"season"\s*:\s*"([^"]+)"'
        )
        for m in pattern2.finditer(all_rsc):
            tier, lp, season = m.group(1), m.group(2), m.group(3)
            entries.append((season, tier, lp))

        print(f"  found (season, tier, lp) tuples: {len(entries)}")
        for season, tier, lp in entries:
            print(f"    season={season!r} tier={tier!r} lp={lp}")

        # Look for game_type / queue identifiers near the rank_entries
        game_types = set(re.findall(r'"game_type"\s*:\s*"([^"]+)"', all_rsc))
        print(f"  game_types referenced: {sorted(game_types)}")


if __name__ == "__main__":
    main()
