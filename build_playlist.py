#!/usr/bin/env python3
"""Build a Russian-language IPTV playlist with music preserved and categories."""

import json
import os
import re
import ssl
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

API = "https://iptv-org.github.io/api"

OUT = Path("playlist.m3u")
INTERNATIONAL_FILE = Path("russian-international.m3u")
REGIONAL_FILE = Path("regional.m3u")
ADULT_FILE = Path("adult.m3u")

ADULT_GROUP = "Для взрослых"

# Публичный бесплатный список официальных FTA-потоков.
FREE_TV_URL = (
    "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
)
FREE_TV_GROUPS = {"Russia", "Belarus"}

# Новые ссылки (которых ещё нет в playlist.m3u) проверяются перед
# добавлением. Отключить: CHECK_NEW_STREAMS=0.
CHECK_NEW_STREAMS = os.environ.get("CHECK_NEW_STREAMS", "1") != "0"
PROBE_TIMEOUT = 12
PROBE_WORKERS = 32
PROBE_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"

# Защита рабочего плейлиста: не перезаписывать его, если новая сборка
# резко меньше предыдущей.
MIN_KEEP_RATIO = 0.6

# Сайты с HTML-страницами плеера, а не прямыми потоками.
PAGE_HOSTS = (
    "youtube.com", "youtu.be", "vk.com", "vkvideo.ru", "ok.ru",
    "rutube.ru", "twitch.tv", "dailymotion.com", "facebook.com",
    "player.smotrim.ru",
)

RUSSIAN_CODES = {"rus", "ru", "russian", "русский"}

EXCLUDED_COUNTRIES = {
    "KZ": "Казахстан",
    "UA": "Украина",
}

EXCLUDED_FEED_RE = re.compile(r"^(kazakhstan|ukraine)$", re.IGNORECASE)

EXCLUDED_NAME_RE = re.compile(
    r"kazakh|казах|ukrain|украин",
    re.IGNORECASE,
)

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

# Буквы казахского, украинского и монгольского алфавитов, которых нет
# в русском: так отсекаем каналы без метаданных не на русском языке.
NON_RUSSIAN_CYRILLIC_RE = re.compile(r"[ӘәҒғҚқҢңҰұҺһІіЄєЇїҐґӨөҮү]")

# Классификация по названию (имя, альтернативные имена, заголовок потока).
# Порядок важен: срабатывает первое совпадение.
NAME_RULES = [
    ("Детские", (
        r"мульт|\bmult|детск|detsk|карусел|karusel|малыш|malysh|"
        r"смайлик|smilik|smile ?tv|сказк|skazk|рыжий|ryzhiy|уникум|"
        r"unikum|\bkids?\b|nickelodeon|nick ?jr|disney|cartoon|"
        r"boomerang|gulli|tiji|jim ?jam|джим ?джем|\bbaby|\bani\b|"
        r"\bo!|\bо!|duck ?tv|da vinci kids"
    )),
    ("Познавательные", (
        r"discovery|animal planet|national geographic|nat ?geo|"
        r"\bhistory|истори|istoriya|\btlc\b|travel|"
        r"viasat (explore|history|nature)|viju\+? ?(explore|history|"
        r"nature|planet)|da vinci|наука|nauka|science|моя планета|"
        r"moya planeta|живая планета|zhivaya planeta|big planet|"
        r"живая природа|zhivaya priroda|в мире животных|"
        r"v mire zhivotnykh|домашние животные|domashnie zhivotnye|"
        r"лапки|lapki|\bzoo|зоо|охот|okhota|рыбал|rybalka|hunting|"
        r"fishing|explore|докум|docu|познават|\brt ?doc|\brtd\b|"
        r"rt д\b|nature|природ|priroda|english club|english class|"
        r"кто есть кто|kto est kto|unknown russia|неизвестная россия|"
        r"^vremya$|^время|поехали|poehali|усадьба|usadba|загородн|"
        r"zagorodn|365 days|365 дней|\bterra\b|explorers|\bnano\b|"
        r"доктор|\bdoctor\b|здоров|zdorov|medzdrav|fakty|факты"
    )),
    ("Спорт", (
        r"спорт|sport|матч|\bmatch\b|\bkhl\b|кхл|футбол|futbol|"
        r"football|soccer|хокке|hockey|бокс|boxing|\bufc\b|fight|"
        r"бойц|теннис|tennis|setanta|racing|\bf1\b|\bnba\b|\bnfl\b|"
        r"golf|гольф|olymp|олимп|\budar\b|удар|extreme|экстрим|"
        r"бильярд|шахмат|chess"
    )),
    ("Новости", (
        r"новост|novosti|\bnews|вести|vesti|известия|izvestiya|"
        r"\bрбк\b|\brbc\b|euronews|cgtn|(россия|russia|rossiya|мир|"
        r"\bmir|москва|moskva)[ -]?24|соловь|solovyov|current time|"
        r"настоящее время|tv rain|дождь|dozhd|svoboda|свобода|"
        r"belsat|белсат|360|\brt\b"
    )),
    ("Сериалы", (
        r"сериал|serial|series|роман|\broman\b|феникс|feniks|"
        r"детектив|detektiv|мелодрам|melodram|тайны|tayny|fox life|"
        r"sony channel|bollywood|болливуд|\bindia\b|индия|epic drama|"
        r"true crime|amedia|pro lyubov|про любовь|sledstviye|"
        r"следствие вели|нтв[- ]?хит|ntv[- ]?hit"
    )),
    ("Кино", (
        r"\bкино|\bkino|cinema|\bcine|movie|\bfilm|фильм|tv ?1000|"
        r"иллюзион|illyuzion|мосфильм|mosfilm|ленфильм|hollywood|"
        r"голливуд|paramount|\bhbo\b|eurokino|блокбастер|\bfox\b|"
        r"\bamc\b|sci-fi|победа|pobeda|душевн|dushevn|komedia|"
        r"комедия\b"
    )),
    ("Развлекательные", (
        r"юмор|yumor|humor|comedy|комеди|komediyn|\bтнт|\btnt|"
        r"\bстс|\bsts\b|пятниц|friday|^ч[её]!?$|^che!?$|^ю$|^u$|"
        r"супер|\bsuper\b|тв-?3|\btv-?3\b|перец|peretz|суббота|"
        r"subbota|домашн|domashniy|шокирующ|квн|\bkvn\b|2x2|2х2|"
        r"gags|праздник|prazdnik|вопросы и ответы|voprosy i otvety|"
        r"тво[её] тв|tvoe tv|tvoye tv"
    )),
    ("Кулинария", (
        r"кухня|kuhnya|kukhnya|cook|food|\beda\b|^еда|кулинар|"
        r"kulinar|gastro|гастро"
    )),
    ("Общие", (
        r"^(россия[ -]?1|первый канал|нтв|рен ?тв|тв центр|"
        r"пятый канал|звезда|отр|мир)( hd)?$"
    )),
]

NAME_RULES = [
    (group, re.compile(pattern, re.IGNORECASE))
    for group, pattern in NAME_RULES
]

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
    ("Шансон", r"шансон|shanson|chanson"),
    ("Рок", r"rock|metal|punk|\bрок\b"),
    ("Хип-хоп и R&B", r"hip ?-?hop|\brap\b|r&b|\brnb\b|urban|рэп"),
    ("Танцевальная", r"dance|club|\bedm\b|techno|trance|house|"
                     r"electro|электрон|танцев|\bdj\b"),
    ("Ретро", r"retro|ретро|oldies|[6789]0s|[6789]0-|ностальг|"
              r"nostalg|classic hits|\bgold"),
    ("Джаз и блюз", r"jazz|blues|джаз|\bsoul\b"),
    ("Классическая", r"classical|opera|symphon|классич|mezzo|"
                     r"\bmedici"),
    ("Кантри", r"country|кантри|bluegrass"),
    ("Латино", r"latin|reggaeton|salsa|cumbia|tropical|banda|"
               r"ranchera|sertanejo|bachata|merengue|vallenato"),
    ("Релакс", r"relax|chill|lounge|ambient|релакс|\bcalm|"
               r"meditat"),
]

MUSIC_GENRES = [
    (genre, re.compile(pattern, re.IGNORECASE))
    for genre, pattern in MUSIC_GENRES
]

POP_MUSIC_RE = re.compile(
    r"pop|hits|top|хит|muz-?tv|муз-?тв|\bmtv\b|vh1|music ?box|"
    r"музык|эстрад|estrad",
    re.IGNORECASE,
)

RUSSIAN_MUSIC_COUNTRIES = {"RU", "BY"}

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


def channel_names(channel, stream=None):
    names = [channel.get("name")]
    names.extend(channel.get("alt_names") or [])

    if stream is not None:
        names.append(stream.get("title"))

    return [clean(name) for name in names if clean(name)]


def music_group(channel, stream):
    names = channel_names(channel, stream)
    text = " | ".join(names)

    for genre, pattern in MUSIC_GENRES:
        if pattern.search(text):
            return f"Музыка | {genre}"

    country = str(channel.get("country") or "").upper()

    if country in RUSSIAN_MUSIC_COUNTRIES or CYRILLIC_RE.search(
        clean(channel.get("name")) or clean(stream.get("title"))
    ):
        return "Музыка | Русская"

    if POP_MUSIC_RE.search(text):
        return "Музыка | Поп и хиты"

    return "Музыка | Другие"


def group_by_name(channel, stream):
    # Сначала имена канала, затем заголовок потока: так все потоки
    # одного канала попадают в одну группу.
    for names in (channel_names(channel), [clean(stream.get("title"))]):
        for group, pattern in NAME_RULES:
            if any(pattern.search(name) for name in names if name):
                return group

    return ""


def channel_group(channel, stream):
    if is_music_channel(channel):
        return music_group(channel, stream)

    group = group_by_name(channel, stream)

    if group:
        return group

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
            stats["Дубликаты ссылок (ручные списки)"] += 1
            continue

        seen_urls.add(stream_url)

        old_group = get_attribute(info, "group-title")

        if default_group == ADULT_GROUP:
            group = ADULT_GROUP
        elif "музык" in old_group.lower():
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


def excluded_feed(feed):
    """Kazakhstan/Ukraine versions of channels (feed "Kazakhstan" etc.)."""
    if not feed:
        return ""

    for label in (feed.get("id"), feed.get("name")):
        match = EXCLUDED_FEED_RE.match(str(label or "").strip())

        if match:
            return EXCLUDED_COUNTRIES[
                "UA" if match.group(1).lower() == "ukraine" else "KZ"
            ]

    return ""


def excluded_title(title):
    """Titles of Kazakhstan/Ukraine channels or non-Russian Cyrillic."""
    return bool(
        EXCLUDED_NAME_RE.search(title)
        or NON_RUSSIAN_CYRILLIC_RE.search(title)
    )


def channel_is_russian(channel, feeds_by_channel):
    """Fallback when the stream feed is missing from the catalog."""
    if str(channel.get("country") or "").upper() == "RU":
        return True

    return any(
        language_is_russian(feed.get("languages"))
        for feed in feeds_by_channel.get(channel.get("id"), [])
    )


def is_page_url(url):
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == page or host.endswith("." + page)
        for page in PAGE_HOSTS
    )


def open_following_redirects(url, headers, context, limit=5):
    for _ in range(limit):
        try:
            return urlopen(
                Request(url, headers=headers),
                timeout=PROBE_TIMEOUT,
                context=context,
            )
        except HTTPError as error:
            location = error.headers.get("Location")

            if error.code not in (307, 308) or not location:
                raise

            url = urljoin(url, location)

    raise HTTPError(url, 310, "Too many redirects", None, None)


def probe_stream(url, user_agent="", referrer="", depth=0):
    """True if the URL answers with a playlist, manifest or media data."""
    headers = {"User-Agent": user_agent or PROBE_USER_AGENT}

    if referrer:
        headers["Referer"] = referrer

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        response = open_following_redirects(url, headers, context)

        with response:
            if response.status >= 400:
                return False

            final_url = response.geturl()
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()
            head = response.read(16384)
    except Exception:
        return False

    text = head.decode("utf-8", errors="ignore")

    if "#EXTM3U" in text:
        if "#EXT-X-STREAM-INF" in text and depth == 0:
            variants = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.startswith("#")
            ]

            if variants:
                return probe_stream(
                    urljoin(final_url, variants[0]),
                    user_agent,
                    referrer,
                    depth + 1,
                )

        return True

    if "<MPD" in text:
        return True

    if head[:1] == b"\x47":
        return True

    return any(
        kind in content_type
        for kind in ("video/", "mp2t", "octet-stream", "audio/")
    ) and not text.lstrip().startswith("<")


def fetch_text(url):
    request = Request(
        url,
        headers={"User-Agent": "OTTPlayer-Russian-Playlist/2.0"},
    )

    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def split_title(info):
    """Display name and quality from an #EXTINF line of a third-party list."""
    title = info.split(",", 1)[1] if "," in info else ""
    title = re.sub(r"[\u24b6-\u24e9]", "", title)
    title = re.sub(r"\[[^\]]*\]", "", title)

    quality = ""
    match = re.search(r"\((\d{3,4}[pi])\)", title)

    if match:
        quality = match.group(1)
        title = title.replace(match.group(0), "")

    return clean(re.sub(r"\s+", " ", title)), quality


def read_previous_playlist():
    if not OUT.exists():
        return set(), 0

    lines = OUT.read_text(encoding="utf-8-sig").splitlines()
    urls = {
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#")
    }
    total = sum(1 for line in lines if line.startswith("#EXTINF:"))

    return urls, total


def validate_m3u(output):
    if not output or output[0] != "#EXTM3U":
        raise ValueError("Ошибка формата M3U: нет заголовка #EXTM3U")

    expect_url = False

    for line in output[1:]:
        if line.startswith("#EXTINF:"):
            if expect_url:
                raise ValueError(f"Ошибка формата M3U: нет ссылки: {line}")

            if not re.match(r"^#EXTINF:-?\d+(?:\.\d+)?(?:\s|,)", line):
                raise ValueError(f"Ошибка формата M3U: {line}")

            expect_url = True
        elif line.startswith("#"):
            continue
        else:
            if not expect_url or not line.startswith(
                ("http://", "https://")
            ):
                raise ValueError(f"Ошибка формата M3U: {line}")

            expect_url = False

    if expect_url:
        raise ValueError("Ошибка формата M3U: последняя запись без ссылки")


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

    previous_urls, previous_total = read_previous_playlist()

    output = ["#EXTM3U"]
    seen_urls = set()
    counts = Counter()
    stats = Counter()
    duplicates = Counter()
    sources = Counter()
    entries = []

    def add_entry(channel, stream, group, stream_url, source):
        if stream_url in seen_urls:
            duplicates[source] += 1
            return

        seen_urls.add(stream_url)

        entries.append({
            "channel": channel,
            "stream": stream,
            "group": group,
            "url": stream_url,
            "quality": clean(stream.get("quality")),
            "source": source,
        })

    for stream in streams:
        channel_id = stream.get("channel")
        stream_url = str(stream.get("url") or "").strip()

        if not stream_url:
            stats["Без ссылки"] += 1
            continue

        if not stream_url.startswith(("http://", "https://")):
            stats["Некорректные ссылки"] += 1
            continue

        if not channel_id:
            # Потоки без привязки к каталогу: берём только с русским
            # названием на кириллице.
            title, _ = split_title("," + str(stream.get("title") or ""))

            if not CYRILLIC_RE.search(title):
                stats["Без канала в каталоге (не русские)"] += 1
                continue

            if excluded_title(title):
                stats["Без канала: KZ/UA/не русская кириллица"] += 1
                continue

            channel = {"id": "", "name": title, "categories": []}
            group = channel_group(channel, stream)
            add_entry(
                channel, stream, group, stream_url,
                "iptv-org без channel-id",
            )
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

        feed = find_feed(
            channel_id,
            stream.get("feed"),
            feeds_by_key,
            feeds_by_channel,
        )

        excluded = excluded_feed(feed)

        if excluded:
            stats[f"{excluded} исключена (версия канала)"] += 1
            continue

        adult = bool(channel.get("is_nsfw"))
        music = is_music_channel(channel) and not adult

        if music:
            group = music_group(channel, stream)
            stats["Музыкальные потоки"] += 1

        else:
            if feed:
                if not language_is_russian(feed.get("languages")):
                    stats[
                        "Другой язык (18+)" if adult else "Другой язык"
                    ] += 1
                    continue
            elif channel_is_russian(channel, feeds_by_channel):
                stats["Язык определён по каналу (нет feed)"] += 1
            else:
                stats["Нет подтверждения языка"] += 1
                continue

            group = ADULT_GROUP if adult else channel_group(
                channel, stream
            )
            stats["Русскоязычные потоки"] += 1

        add_entry(channel, stream, group, stream_url, "iptv-org")

    try:
        free_tv = list(parse_m3u(fetch_text(FREE_TV_URL)))
        print(f"Free-TV: получено записей: {len(free_tv)}")
    except Exception as error:
        print(f"Free-TV недоступен, пропускаю: {error}")
        stats["Free-TV недоступен"] += 1
        free_tv = []

    for info, extras, stream_url in free_tv:
        if get_attribute(info, "group-title") not in FREE_TV_GROUPS:
            continue

        if not stream_url.startswith(("http://", "https://")):
            stats["Некорректные ссылки"] += 1
            continue

        if is_page_url(stream_url):
            stats["Free-TV: ссылка на страницу, а не поток"] += 1
            continue

        tvg_id = get_attribute(info, "tvg-id")
        title, quality = split_title(info)
        channel = channels_by_id.get(tvg_id.split("@")[0])

        if (
            excluded_country(channel=channel, channel_id=tvg_id)
            or get_attribute(info, "tvg-country").upper()
            in EXCLUDED_COUNTRIES
            or excluded_title(title)
        ):
            stats["Free-TV: Казахстан/Украина исключены"] += 1
            continue

        if channel and channel.get("is_nsfw"):
            continue

        if not channel:
            channel = {
                "id": tvg_id,
                "name": get_attribute(info, "tvg-name") or title,
                "categories": [],
                "country": get_attribute(info, "tvg-country"),
            }

        stream = {
            "title": title,
            "quality": quality,
            "user_agent": get_attribute(info, "http-user-agent"),
            "referrer": get_attribute(info, "http-referrer"),
        }

        for extra in extras:
            if extra.lower().startswith("#extvlcopt:http-user-agent="):
                stream["user_agent"] = extra.split("=", 1)[1]
            elif extra.lower().startswith("#extvlcopt:http-referrer="):
                stream["referrer"] = extra.split("=", 1)[1]

        group = channel_group(channel, stream)
        add_entry(channel, stream, group, stream_url, "Free-TV")

    for item in entries:
        sources[item["source"]] += 1

    new_entries = [
        item for item in entries
        if item["url"] not in previous_urls
    ]
    probe = Counter()

    if CHECK_NEW_STREAMS and new_entries:
        print(f"Проверяю новые потоки: {len(new_entries)} ...")

        with ThreadPoolExecutor(PROBE_WORKERS) as pool:
            results = list(pool.map(
                lambda item: probe_stream(
                    item["url"],
                    clean(item["stream"].get("user_agent")),
                    clean(
                        item["stream"].get("referrer")
                        or item["stream"].get("http_referrer")
                    ),
                ),
                new_entries,
            ))

        failed = set()

        for item, ok in zip(new_entries, results):
            probe[(item["source"], ok)] += 1

            if not ok:
                failed.add(item["url"])

        entries = [item for item in entries if item["url"] not in failed]
        seen_urls -= failed

    entries.sort(
        key=lambda item: (
            item["group"] == ADULT_GROUP,
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

    add_manual_file(
        ADULT_FILE,
        ADULT_GROUP,
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

    total = sum(counts.values())

    if previous_total and total < previous_total * MIN_KEEP_RATIO:
        raise RuntimeError(
            f"Каналов стало слишком мало ({total} против "
            f"{previous_total}). Старый playlist.m3u оставлен без "
            "изменений."
        )

    validate_m3u(output)

    temporary = OUT.with_name(OUT.name + ".tmp")
    temporary.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, OUT)

    print("\n========== РЕЗУЛЬТАТ ==========")
    print("Всего записей в плейлисте:", total)
    print("Было в предыдущей версии:", previous_total)

    print("\nИсточники (до проверки новых ссылок):")
    for name, count in sorted(sources.items()):
        print(f"  {name}: {count}")

    print("\nИсключено / отфильтровано:")
    for name, count in sorted(stats.items()):
        print(f"  {name}: {count}")

    print("\nДубликаты ссылок:")
    for name, count in sorted(duplicates.items()):
        print(f"  {name}: {count}")
    print(f"  Всего: {sum(duplicates.values())}")

    if CHECK_NEW_STREAMS:
        print("\nПроверка новых ссылок (нет в прошлой версии):")
        for (source, ok), count in sorted(probe.items()):
            status = "работает" if ok else "не ответил, не добавлен"
            print(f"  {source}: {status}: {count}")
    else:
        print("\nПроверка новых ссылок отключена (CHECK_NEW_STREAMS=0)")

    print("\nКатегории:")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")

    print("\nПлейлист успешно создан:", OUT)


if __name__ == "__main__":
    main()
