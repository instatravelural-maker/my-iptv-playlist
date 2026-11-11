
#!/usr/bin/env python3
"""Build a Russian-language IPTV playlist for OTTPlayer."""

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
import re

BASE = "https://iptv-org.github.io/iptv"

# Тематические подборки. Из каждой берём только каналы
# с подтверждёнными признаками русского/белорусского языка.
CATEGORY_SOURCES = [
    ("Кино", f"{BASE}/categories/movies.m3u"),
    ("Спорт", f"{BASE}/categories/sports.m3u"),
    ("Новости", f"{BASE}/categories/news.m3u"),
    ("Музыка", f"{BASE}/categories/music.m3u"),
    ("Детские", f"{BASE}/categories/kids.m3u"),
    ("Познавательные", f"{BASE}/categories/documentary.m3u"),
    ("Познавательные", f"{BASE}/categories/education.m3u"),
    ("Познавательные", f"{BASE}/categories/science.m3u"),
]

# Национальные подборки.
COUNTRY_SOURCES = [
    ("Россия", f"{BASE}/countries/ru.m3u"),
    ("Беларусь", f"{BASE}/countries/by.m3u"),
]

OUT = Path("playlist.m3u")

# Дополнительные списки, которые можно пополнять вручную:
# зарубежные каналы с русской аудиодорожкой и местные каналы.
INTERNATIONAL_FILE = Path("russian-international.m3u")
REGIONAL_FILE = Path("regional.m3u")

RUSSIAN_LANGUAGES = (
    "russian", "русский", "рус",
    "belarusian", "belorussian", "белорусский",
)

MUSIC_GENRES = [
    ("Рок", ("rock", "metal", "punk")),
    ("Танцевальная", ("dance", "club", "edm", "techno", "trance", "house")),
    ("Ретро", ("retro", "oldies", "80s", "90s", "70s")),
    ("Джаз и блюз", ("jazz", "blues")),
    ("Классическая", ("classical", "opera", "symphony")),
    ("Поп и хиты", ("pop", "hits", "top", "муз-тв", "muz-tv")),
]


def fetch(url):
    request = Request(
        url,
        headers={"User-Agent": "OTTPlayer-Russian-Playlist/1.0"}
    )
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def parse_m3u(content):
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]

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


def get_attribute(info, attribute):
    match = re.search(
        r'\b' + re.escape(attribute) + r'="([^"]*)"',
        info,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def is_russian_channel(info):
    """Check explicit language metadata and RU/BY channel IDs."""

    channel_id = get_attribute(info, "tvg-id").lower()

    # Учитываем ID вида channel.ru@HD, а не только channel.ru.
    if re.search(r"\.(ru|by)(?:@[^,\s]*)?$", channel_id):
        return True

    language_fields = (
        "tvg-language",
        "audio-language",
        "language",
    )

    for field in language_fields:
        language = get_attribute(info, field).lower()

        if any(word in language for word in RUSSIAN_LANGUAGES):
            return True

    country = (
        get_attribute(info, "tvg-country")
        + " "
        + get_attribute(info, "country")
    ).lower()

    if any(word in country for word in (
        "russia", "россия", "belarus", "беларусь"
    )):
        return True

    return False


def with_group(info, group):
    match = re.match(
        r"^(#EXTINF:)(-?\d+(?:\.\d+)?)(.*)$",
        info,
    )

    if not match:
        return info

    prefix, duration, rest = match.groups()

    rest = re.sub(
        r'\s+group-title="[^"]*"',
        "",
        rest,
        flags=re.IGNORECASE,
    )

    return f'{prefix}{duration} group-title="{group}"{rest}'


def music_group(info):
    name = info.rsplit(",", 1)[-1].strip().lower()
    genre = "Другие"

    for candidate, keywords in MUSIC_GENRES:
        if any(keyword in name for keyword in keywords):
            genre = candidate
            break

    channel_id = get_attribute(info, "tvg-id").lower()

    if re.search(r"\.by(?:@[^,\s]*)?$", channel_id):
        region = "Беларусь"
    elif re.search(r"\.ru(?:@[^,\s]*)?$", channel_id):
        region = "Россия"
    else:
        region = "Зарубежные"

    return f"Музыка | {region} | {genre}"


def main():
    output = ["#EXTM3U"]
    seen_urls = set()
    counts = {}

    def add_channel(info, extras, stream_url, group):
        if not stream_url.startswith(("http://", "https://")):
            return False

        if stream_url in seen_urls:
            return False

        seen_urls.add(stream_url)

        if group == "Музыка":
            group = music_group(info)

        output.append(with_group(info, group))
        output.extend(extras)
        output.append(stream_url)

        counts[group] = counts.get(group, 0) + 1
        return True

    # 1. Тематические разделы: только русскоязычные каналы.
    for group, url in CATEGORY_SOURCES:
        try:
            content = fetch(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"SKIP {url}: {exc}")
            continue

        added = 0
        rejected = 0

        for info, extras, stream_url in parse_m3u(content):
            if not is_russian_channel(info):
                rejected += 1
                continue

            if add_channel(info, extras, stream_url, group):
                added += 1

        print(
            f"{group}: добавлено {added}, "
            f"отсеяно нерусскоязычных/неподтверждённых: {rejected}"
        )

    # 2. Зарубежные каналы с русской дорожкой.
    # Добавляются сюда вручную после проверки.
    if INTERNATIONAL_FILE.exists():
        try:
            content = INTERNATIONAL_FILE.read_text(
                encoding="utf-8-sig"
            )

            for info, extras, stream_url in parse_m3u(content):
                group = (
                    get_attribute(info, "group-title")
                    or "Зарубежные на русском"
                )
                add_channel(info, extras, stream_url, group)

        except OSError as exc:
            print(f"Could not read {INTERNATIONAL_FILE}: {exc}")

    # 3. Остальные каналы России и Беларуси.
    # Те, что уже попали в тематические разделы, повторно не добавляем.
    for group, url in COUNTRY_SOURCES:
        try:
            content = fetch(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"SKIP {url}: {exc}")
            continue

        added = 0

        for info, extras, stream_url in parse_m3u(content):
            if add_channel(info, extras, stream_url, group):
                added += 1

        print(f"{group}: дополнительно добавлено {added}")

    # 4. Пользовательский региональный список.
    if REGIONAL_FILE.exists():
        try:
            content = REGIONAL_FILE.read_text(
                encoding="utf-8-sig"
            )

            for info, extras, stream_url in parse_m3u(content):
                add_channel(
                    info, extras, stream_url, "Региональные"
                )

        except OSError as exc:
            print(f"Could not read {REGIONAL_FILE}: {exc}")

    # Проверка формата перед сохранением.
    invalid = [
        line for line in output
        if line.startswith("#EXTINF:")
        and not re.match(
            r"^#EXTINF:-?\d+(?:\.\d+)?(?:\s|,)",
            line,
        )
    ]

    if invalid:
        raise ValueError(
            f"Invalid EXTINF syntax: {invalid[0]}"
        )

    OUT.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )

    total = sum(counts.values())

    print("\nПлейлист создан:", OUT)
    print("Всего каналов:", total)

    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")


if __name__ == "__main__":
    main()
