
#!/usr/bin/env python3
"""Build a Russian-language IPTV playlist with music preserved and categories."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

API = "https://iptv-org.github.io/api"

OUT = Path("playlist.m3u")
INTERNATIONAL_FILE = Path("russian-international.m3u")
REGIONAL_FILE = Path("regional.m3u"

)

RUSSIAN_CODES = {"rus", "ru", "russian", "русский"}

EXCLUDED_COUNTRIES = {
    "KZ": "Казахстан",
    "UA": "Украина",
}

CATEGORY_MAP = [
    ("music", "Музыка"),
    ("movies", "Кино"),
    ("series", "Сериалы"),
    ("sports", "Спорт"),
    ("news", "Новости"),
    ("entertainment", "Развлекательные"),
    ("comedy", "Развлекательные"),
    ("kids", "Детские"),
    ("animation", "Мультфильмы"),
    ("documentary", "Познавательные"),
    ("education", "Познавательные"),
    ("science", "Познавательные"),
    ("cooking", "Кулинария"),
    ("travel", "Путешествия"),
    ("lifestyle", "Стиль жизни"),
    ("auto", "Авто"),
    ("business", "Бизнес"),
    ("culture", "Культура"),
    ("religious", "Религиозные"),
    ("shop", "Телемагазины"),
    ("weather", "Погода"),
    ("outdoor", "Природа и отдых"),
    ("relax", "Релакс"),
    ("classic", "Классика"),
    ("family", "Семейные"),
    ("legislative", "Государственные"),
    ("public", "Общественные"),
    ("interactive", "Интерактивные"),
    ("general", "Общие"),
]

MUSIC_GENRES = [
    ("Рок", ("rock", "metal", "punk")),
    ("Танцевальная", ("dance", "club", "edm", "techno",
                      "trance", "house")),
    ("Ретро", ("retro", "oldies", "80s", "90s", "70s")),
    ("Джаз и блюз", ("jazz", "blues")),
    ("Классическая", ("classical", "opera", "symphony")),
    ("Поп и хиты", ("pop", "hits", "top", "muz-tv",
                    "муз-тв", "музыка")),
]

MUSIC_NAME_RE = re.compile(
    r"music|музык|муз[\s-]?тв|музон",
    re.IGNORECASE,
)


def fetch_json(name):
    url = f"{API}/{name}.json"
    request = Request(
        url,
        headers={"User-Agent": "OTTPlayer-Russian-Playlist/2.0"},
    )

    print(f"Загружаю {name}.json ...")

    with urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"Некорректный ответ API: {name}.json")

    print(f"  Получено записей: {len(data)}")
    return data


def clean(value):
    return str(value or "").replace('"', "").replace(
        "\r", " "
    ).replace("\n", " ").strip()


def language_is_russian(languages):
    if isinstance(languages, str):
        languages = [languages]

    for language in languages or []:
        value = str(language).strip().lower()

        if value in RUSSIAN_CODES:
            return True

    return False


def is_music_channel(channel):
    categories = set(channel.get("categories") or [])

    if "music" in categories:
        return True

    name = channel.get("name", "")
    return bool(MUSIC_NAME_RE.search(name))


def music_group(channel, stream):
    name = (
        str(channel.get("name", ""))
        + " "
        + str(stream.get("title", ""))
    ).lower()

    genre = "Другие"

    for candidate, keywords in MUSIC_GENRES:
        if any(keyword in name for keyword in keywords):
            genre = candidate
            break

    return f"Музыка | {genre}"


def channel_group(channel, stream):
    if is_music_channel(channel):
        return music_group(channel, stream)

    categories = set(channel.get("categories") or [])

    for category, group in CATEGORY_MAP:
        if category in categories:
            return group

    return "Прочее"


def quality_label(quality):
    """Convert technical resolutions into readable quality labels."""
    value = clean(quality).strip().lower()

    if not value:
        return ""

    if re.search(r"\b4k\b|uhd|2160", value):
        return "4K UHD"

    if re.search(r"\bqhd\b|1440", value):
        return "QHD"

    if re.search(r"\bfhd\b|full\s*hd|1080", value):
        return "Full HD"

    if re.search(r"\bhd\b|720", value):
        return "HD"

    if re.search(r"\bsd\b|576|560|480|360", value):
        return "SD"

    return ""


def normalize_manual_quality(info):
    if "," not in info:
        return info

    header, display_name = info.split(",", 1)

    match = re.search(r"\s*\[([^\]]+)\]\s*$", display_name)

    if not match:
        return info

    label = quality_label(match.group(1))

    if not label:
        return info

    display_name = (
        display_name[:match.start()].rstrip()
        + f" [{label}]"
    )

    return header + "," + display_name


def make_extinf(channel, stream, group):
    channel_id = clean(channel.get("id"))
    channel_name = clean(channel.get("name"))

    if not channel_name:
        channel_name = clean(stream.get("title")) or channel_id

    quality = quality_label(stream.get("quality"))
    display_name = channel_name

    if quality and not re.search(
        r"\[" + re.escape(quality) + r"\]$",
        display_name,
        re.IGNORECASE,
    ):
        display_name += f" [{quality}]"

    return (
        '#EXTINF:-1 '
        f'tvg-id="{channel_id}" '
        f'tvg-name="{channel_name}" '
        f'group-title="{clean(group)}",'
        f'{display_name}'
    )


def find_feed(channel_id, feed_id, feeds_by_key, feeds_by_channel):
    if feed_id:
        return feeds_by_key.get((channel_id, str(feed_id)))

    candidates = feeds_by_channel.get(channel_id, [])

    if candidates and all(
        language_is_russian(feed.get("languages"))
        for feed in candidates
    ):
        return candidates[0]

    return None


def add_headers(output, stream):
    user_agent = clean(stream.get("user_agent"))
    referrer = clean(
        stream.get("referrer") or stream.get("http_referrer")
    )

    if user_agent:
        output.append(f"#EXTVLCOPT:http-user-agent={user_agent}")

    if referrer:
        output.append(f"#EXTVLCOPT:http-referrer={referrer}")


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


def excluded_country(channel=None, channel_id=""):
    """Identify excluded countries by catalog country code or tvg-id."""
    if channel:
        country = str(
            channel.get("country") or ""
        ).strip().upper()

        if country in EXCLUDED_COUNTRIES:
            return EXCLUDED_COUNTRIES[country]

    value = str(channel_id or "").strip()

    match = re.search(
        r"\.(kz|ua)(?:@[^\s]*)?$",
        value,
        re.IGNORECASE,
    )

    if match:
        code = match.group(1).upper()
        return EXCLUDED_COUNTRIES.get(code, "")

    return ""


def add_manual_file(path, default_group, output, seen_urls, counts, stats):
    if not path.exists():
        return

    print(f"Добавляю ручной список: {path}")

    content = path.read_text(encoding="utf-8-sig")

    for info, extras, stream_url in parse_m3u(content):
        if not stream_url.startswith(("http://", "https://")):
            continue

        manual_channel_id = get_attribute(info, "tvg-id")
        excluded = excluded_country(channel_id=manual_channel_id)

        if excluded:
            stats[f"{excluded} исключена (ручные списки)"] += 1
            continue

        if stream_url in seen_urls:
            continue

        seen_urls.add(stream_url)

        old_group = get_attribute(info, "group-title")

        if "музык" in old_group.lower():
            group = "Музыка | Другие"
        else:
            group = default_group

        info = normalize_manual_quality(info)

        rest = re.sub(
            r'\s+group-title="[^"]*"',
            "",
            info,
            flags=re.IGNORECASE,
        )

        match = re.match(
            r"^(#EXTINF:)(-?\d+(?:\.\d+)?)(.*)$",
            rest,
        )

        if match:
            prefix, duration, tail = match.groups()
            info = (
                f'{prefix}{duration} '
                f'group-title="{group}"{tail}'
            )

        output.append(info)
        output.extend(extras)
        output.append(stream_url)

        counts[group] += 1


def main():
    channels = fetch_json("channels")
    feeds = fetch_json("feeds")
    streams = fetch_json("streams")

    channels_by_id = {
        channel["id"]: channel
        for channel in channels
        if channel.get("id")
    }

    feeds_by_key = {}
    feeds_by_channel = defaultdict(list)

    for feed in feeds:
        channel_id = feed.get("channel")
        feed_id = feed.get("id")

        if not channel_id:
            continue

        feeds_by_channel[channel_id].append(feed)

        if feed_id:
            feeds_by_key[(channel_id, str(feed_id))] = feed

    output = ["#EXTM3U"]
    seen_urls = set()
    counts = Counter()
    stats = Counter()
    entries = []

    for stream in streams:
        channel_id = stream.get("channel")
        stream_url = str(stream.get("url") or "").strip()

        if not channel_id or not stream_url:
            stats["Без канала или ссылки"] += 1
            continue

        if not stream_url.startswith(("http://", "https://")):
            stats["Некорректные ссылки"] += 1
            continue

        channel = channels_by_id.get(channel_id)

        if not channel:
            stats["Нет данных о канале"] += 1
            continue

        # Исключаем Казахстан и Украину до проверки языка и музыки.
        excluded = excluded_country(
            channel=channel,
            channel_id=channel_id,
        )

        if excluded:
            stats[f"{excluded} исключена"] += 1
            continue

        if channel.get("is_nsfw"):
            stats["Взрослые каналы"] += 1
            continue

        music = is_music_channel(channel)

        if music:
            group = music_group(channel, stream)
            stats["Музыкальные потоки"] += 1

        else:
            feed = find_feed(
                channel_id,
                stream.get("feed"),
                feeds_by_key,
                feeds_by_channel,
            )

            if not feed:
                stats["Нет подтверждения языка"] += 1
                continue

            if not language_is_russian(feed.get("languages")):
                stats["Другой язык"] += 1
                continue

            group = channel_group(channel, stream)
            stats["Русскоязычные потоки"] += 1

        if stream_url in seen_urls:
            stats["Дубликаты ссылок"] += 1
            continue

        seen_urls.add(stream_url)

        quality = clean(stream.get("quality"))

        entries.append({
            "channel": channel,
            "stream": stream,
            "group": group,
            "url": stream_url,
            "quality": quality,
        })

    entries.sort(
        key=lambda item: (
            item["group"].lower(),
            item["channel"].get("name", "").lower(),
            item["quality"],
        )
    )

    for item in entries:
        output.append(
            make_extinf(
                item["channel"],
                item["stream"],
                item["group"],
            )
        )

        add_headers(output, item["stream"])
        output.append(item["url"])

        counts[item["group"]] += 1

    add_manual_file(
        INTERNATIONAL_FILE,
        "Зарубежные на русском",
        output,
        seen_urls,
        counts,
        stats,
    )

    add_manual_file(
        REGIONAL_FILE,
        "Региональные",
        output,
        seen_urls,
        counts,
        stats,
    )

    if len(entries) < 100:
        raise RuntimeError(
            "Получено подозрительно мало каналов. "
            "Старый playlist.m3u оставлен без изменений."
        )

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
            f"Ошибка формата M3U: {invalid[0]}"
        )

    OUT.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )

    print("\n========== РЕЗУЛЬТАТ ==========")
    print("Потоков из основного каталога:", len(entries))
    print("Всего записей в плейлисте:", sum(counts.values()))

    print("\nФильтрация:")
    for name, count in sorted(stats.items()):
        print(f"  {name}: {count}")

    print("\nКатегории:")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")

    print("\nПлейлист успешно создан:", OUT)


if __name__ == "__main__":
    main()
