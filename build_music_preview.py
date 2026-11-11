
#!/usr/bin/env python3
"""Create a separate music-category preview for OTTPlayer."""

from pathlib import Path
from urllib.request import Request, urlopen
import re

SOURCE_URL = "https://iptv-org.github.io/iptv/categories/music.m3u"
OUTPUT_FILE = Path("music-categories-preview.m3u")

GENRES = [
    ("Рок", ("rock", "metal", "punk", "hard rock")),
    ("Танцевальная", ("dance", "club", "edm", "techno", "trance", "house")),
    ("Ретро", ("retro", "oldies", "classic hits", "80s", "90s", "70s")),
    ("Джаз и блюз", ("jazz", "blues")),
    ("Классическая", ("classical", "opera", "symphony")),
    ("Поп и хиты", ("pop", "hits", "top", "music box", "muz-tv", "муз-тв")),
]


def download_playlist():
    request = Request(
        SOURCE_URL,
        headers={"User-Agent": "OTTPlayer-M3U-builder/1.0"},
    )
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def parse_entries(source):
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    index = 0

    while index < len(lines):
        if not lines[index].startswith("#EXTINF:"):
            index += 1
            continue

        info = lines[index]
        index += 1
        extra_lines = []

        while (
            index < len(lines)
            and lines[index].startswith("#")
            and not lines[index].startswith("#EXTINF:")
        ):
            extra_lines.append(lines[index])
            index += 1

        if index < len(lines) and not lines[index].startswith("#"):
            yield info, extra_lines, lines[index]
            index += 1


def get_attribute(info, attribute):
    match = re.search(
        r'\b' + re.escape(attribute) + r'="([^"]*)"',
        info,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def channel_name(info):
    return info.rsplit(",", 1)[-1].strip().lower()


def region_for(info):
    channel_id = get_attribute(info, "tvg-id").lower()
    language = get_attribute(info, "tvg-language").lower()

    if (
        re.search(r'\.(ru|by|kz)$', channel_id)
        or "russian" in language
        or "русский" in language
    ):
        return "Россия и СНГ"

    return "Мир"


def genre_for(info):
    name = channel_name(info)

    for genre, keywords in GENRES:
        if any(keyword in name for keyword in keywords):
            return genre

    return "Другие"


def replace_group(info, group):
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


def main():
    output = ["#EXTM3U"]
    seen_urls = set()
    counts = {}

    for info, extra_lines, stream_url in parse_entries(download_playlist()):
        if not stream_url.startswith(("http://", "https://")):
            continue

        if stream_url in seen_urls:
            continue

        seen_urls.add(stream_url)

        group = (
            f"Музыка | {region_for(info)} | {genre_for(info)}"
        )

        output.append(replace_group(info, group))
        output.extend(extra_lines)
        output.append(stream_url)

        counts[group] = counts.get(group, 0) + 1

    OUTPUT_FILE.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )

    print(f"Создан файл: {OUTPUT_FILE}")
    print(f"Уникальных потоков: {len(seen_urls)}")

    for group in sorted(counts):
        print(f"{group}: {counts[group]}")


if __name__ == "__main__":
    main()
