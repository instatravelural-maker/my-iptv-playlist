#!/usr/bin/env python3
"""Build one grouped M3U playlist from publicly listed iptv-org playlists.

This combines channel records, not nested playlist URLs. Availability is not
warranted: public streams can be geo-blocked, offline, or removed upstream.
"""
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
import re

BASE = "https://iptv-org.github.io/iptv"
SOURCES = [
    ("Россия", f"{BASE}/countries/ru.m3u"),
    ("Беларусь и Казахстан", f"{BASE}/countries/by.m3u"),
    ("Беларусь и Казахстан", f"{BASE}/countries/kz.m3u"),
    ("Кино", f"{BASE}/categories/movies.m3u"),
    ("Спорт", f"{BASE}/categories/sports.m3u"),
    ("Новости", f"{BASE}/categories/news.m3u"),
    ("Музыка", f"{BASE}/categories/music.m3u"),
    ("Детские", f"{BASE}/categories/kids.m3u"),
    ("Познавательные", f"{BASE}/categories/documentary.m3u"),
    ("Познавательные", f"{BASE}/categories/education.m3u"),
    ("Познавательные", f"{BASE}/categories/science.m3u"),
    # Regional channel URLs can be added manually in regional.m3u (see README).
]

OUT = Path("playlist.m3u")
REGIONAL_FILE = Path("regional.m3u")


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "OTTPlayer-M3U-builder/1.0"})
    with urlopen(req, timeout=45) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def parse_m3u(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF:"):
            info = lines[i]
            j = i + 1
            # Keep optional VLC/user-agent/header directives attached to this entry.
            extras = []
            while j < len(lines) and lines[j].startswith("#") and not lines[j].startswith("#EXTINF:"):
                extras.append(lines[j])
                j += 1
            if j < len(lines) and not lines[j].startswith("#"):
                yield info, extras, lines[j]
                i = j + 1
                continue
        i += 1


def with_group(info: str, group: str) -> str:
    # Replace existing group-title or add one; preserve all other EXTINF attributes.
    if re.search(r'group-title="[^"]*"', info):
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', info, count=1)
    return info.replace("#EXTINF:", f'#EXTINF: group-title="{group}"', 1)


def main():
    output = ["#EXTM3U"]
    counts = {}
    for group, url in SOURCES:
        try:
            content = fetch(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"SKIP {url}: {exc}")
            continue
        count = 0
        seen_in_group = set()
        for info, extras, stream_url in parse_m3u(content):
            if not stream_url.startswith(("http://", "https://")):
                continue
            if stream_url in seen_in_group:
                continue
            seen_in_group.add(stream_url)
            output.append(with_group(info, group))
            output.extend(extras)
            output.append(stream_url)
            count += 1
        counts[group] = counts.get(group, 0) + count
        print(f"{group}: {count} entries from {url}")

    # User-maintained regional channels are read locally from regional.m3u.
    if REGIONAL_FILE.exists():
        try:
            local = REGIONAL_FILE.read_text(encoding="utf-8-sig")
            regional_seen = set()
            for info, extras, stream_url in parse_m3u(local):
                if stream_url.startswith(("http://", "https://")) and stream_url not in regional_seen:
                    regional_seen.add(stream_url)
                    output.append(with_group(info, "Региональные"))
                    output.extend(extras)
                    output.append(stream_url)
            counts["Региональные"] = len(regional_seen)
        except OSError as exc:
            print(f"Could not read regional.m3u: {exc}")

    OUT.write_text("\n".join(output) + "\n", encoding="utf-8")
    total = sum(counts.values())
    print(f"Created {OUT}: {total} channel entries")
    for group, count in counts.items():
        print(f"  {group}: {count}")


if __name__ == "__main__":
    main()
