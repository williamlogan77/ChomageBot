"""Probe LP-history third-party sources for a single test player.

Runs from inside Proxmox container 103. Saves raw HTML + parsed candidates
to /tmp/sources/<source>/ for traceability. Read-only — never writes the DB.

Sources surveyed:
  - op.gg          (Cloudflare-fronted; Next.js c-lol-web.op.gg API)
  - mobalytics.gg  (Cloudflare-challenged HTML)
  - porofessor.gg  (no CF; HTML scrape only)
  - dpm.lol        (Cloudflare-challenged HTML)
  - leagueofgraphs (Cloudflare-challenged HTML)

Usage:
  python3 probe_lp_sources.py [SLUG]
  # default SLUG is Langers69-EUW (known-good u.gg-cached player)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HDR = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
RATE_LIMIT = 1.5


def fetch(url: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
    h = dict(HDR)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body, dict(e.headers) if e.headers else {}
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode(), {}


def hunt_hosts(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="replace")
    hosts = set(
        re.findall(
            r"https?://([a-z0-9.-]+\.(?:op\.gg|mobalytics\.gg|porofessor\.gg|dpm\.lol|leagueofgraphs\.com))",
            text,
            re.I,
        )
    )
    return sorted(hosts)


def hunt_next_data(body: bytes) -> dict | None:
    text = body.decode("utf-8", errors="replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def probe(slug: str) -> None:
    enc = urllib.parse.quote(slug)
    targets = {
        "opgg": f"https://op.gg/lol/summoners/euw/{enc}",
        "mobalytics": f"https://app.mobalytics.gg/lol/profile/euw/{enc}/overview",
        "porofessor": f"https://porofessor.gg/profile/euw/{enc}",
        "dpm": f"https://dpm.lol/{enc}",
        "leagueofgraphs": f"https://www.leagueofgraphs.com/summoner/euw/{enc.replace('-', '.')}",
    }
    for name, url in targets.items():
        status, body, hdrs = fetch(url)
        size = len(body)
        ct = hdrs.get("Content-Type") or hdrs.get("content-type") or ""
        print(f"\n=== {name} ({url}) ===")
        print(f"  HTTP {status}  size={size}  ct={ct}")
        if status == 200 and size > 10000:
            hosts = hunt_hosts(body)
            print(f"  hosts found: {hosts}")
            nd = hunt_next_data(body)
            if nd is not None:
                top = list(nd.keys())[:10] if isinstance(nd, dict) else type(nd).__name__
                print(f"  __NEXT_DATA__: keys={top}")
            # Save sample for offline inspection
            outp = f"/tmp/sources/{name}/probe_{slug.replace('/', '_')}.html"
            os.makedirs(os.path.dirname(outp), exist_ok=True)
            with open(outp, "wb") as f:
                f.write(body)
            print(f"  saved -> {outp}")
        elif status == 403:
            print("  -> 403 (likely Cloudflare challenge)")
        elif status == 404:
            print("  -> 404 (no profile or wrong slug shape)")
        time.sleep(RATE_LIMIT)


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "Langers69-EUW"
    probe(slug)
