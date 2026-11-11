
#!/usr/bin/env python3
"""Build one grouped M3U playlist, including categorized music channels."""

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
]

OUT = Path("playlist.m3u")
REGIONAL_FILE = Path("regional.m3u")

MUSIC_GENRES = [
    ("Рок", ("rock", "metal", "punk", "hard rock")),
    ("Танцевальная", ("dance", "club", "edm", "techno", "trance", "house")),
    ("Ретро", ("retro", "oldies", "classic hits", "80s", "90s", "70s")),
    ("Джаз и блюз", ("jazz", "blues")),
    ("Классическая", ("classical", "opera", "symphony")),
    ("Поп и хиты", ("pop", "hits", "top", "music box", "muz-tv", "муз-тв")),
]


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
            extras = []

            while (
                j < len(lines)
                and lines[j].startswith("#")
                and not lines[j].startswith("#EXTINF:")
            ):
                extras.append(lines[j])
                j += 1

            if j < len(lines) and not lines[j].startswith("#"):
                yield info, extras, lines[j]
                i = j + 1
                continue

        i += 1


def with_group(info: str, group: str) -> str:
    if re.search(r'group-title="[^"]*"', info):
        return re.sub(
            r'group-title="[^"]*"',
            lambda _: f'group-title="{group}"',
            info,
            count=1,
        )

    return info.replace(
        "#EXTINF:",
        f'#EXTINF: group-title="{group}"',
        1,
    )


def get_attribute(info: str, attribute: str) -> str:
    match = re.search(
        r'\b' + re.escape(attribute) + r'="([^"]*)"',
        info,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def music_group(info: str) -> str:
    channel_id = get_attribute(info, "tvg-id").lower()
    language = get_attribute(info, "tvg-language").lower()

    region = "Мир"

    if (
        re.search(r'\.(ru|by|kz)$', channel_id)
        or "russian" in language
        or "русский" in language
    ):
        region = "Россия и СНГ"

    name = info.rsplit(",", 1)[-1].strip().lower()
    genre = "Другие"

    for candidate, keywords in MUSIC_GENRES:
        if any(keyword in name for keyword in keywords):
            genre = candidate
            break

    return f"Музыка | {region} | {genre}"


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

            actual_group = (
                music_group(info) if group == "Музыка" else group
            )

            output.append(with_group(info, actual_group))
            output.extend(extras)
            output.append(stream_url)

            counts[actual_group] = counts.get(actual_group, 0) + 1
            count += 1

        print(f"{group}: {count} entries from {url}")

    if REGIONAL_FILE.exists():
        try:
            local = REGIONAL_FILE.read_text(encoding="utf-8-sig")
            regional_seen = set()

            for info, extras, stream_url in parse_m3u(local):
                if (
                    stream_url.startswith(("http://", "https://"))
                    and stream_url not in regional_seen
                ):
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

    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")


if __name__ == "__main__":
    main()
