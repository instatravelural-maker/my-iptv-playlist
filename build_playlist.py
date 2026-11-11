#!/usr/bin/env python3
"""Build a Russian-language IPTV playlist with music preserved and categories."""

import gzip
import json
import os
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
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

# Публичные сборники русских каналов на GitHub. Берём только ссылки без
# токенов и подписей, не с рестрим-хостов операторов, и только после
# проверки: не ответившие ссылки отсюда в плейлист не попадают.
COMMUNITY_SOURCES = {
    "ngrch/iptv": "https://raw.githubusercontent.com/ngrch/iptv/main/ru.m3u",
    "gdydg/iptv": "https://raw.githubusercontent.com/gdydg/iptv/main/ru.m3u",
}

COMMUNITY_GROUP_MAP = {
    "основные": "Общие",
    "основные (региональные)": "Региональные",
    "региональные": "Региональные",
    "кино": "Кино",
    "путешествия": "Путешествия",
    "развлекательные": "Развлекательные",
    "документальные": "Познавательные",
    "познавательные": "Познавательные",
    "природа и животные": "Познавательные",
    "охота и рыбалка": "Познавательные",
    "детские": "Детские",
    "новости": "Новости",
    "спорт": "Спорт",
    "мода и стиль": "Стиль жизни",
    "здоровье и фитнес": "Стиль жизни",
    "авто": "Авто",
    "кулинария": "Кулинария",
    "религиозные": "Религиозные",
}

# Кинозалы с зацикленными фильмами и прочие нелицензионные подборки.
COMMUNITY_SKIP_GROUP_RE = re.compile(r"кинозал", re.IGNORECASE)

# Ретрансляции платных пакетов операторов и пиратские рестримы.
RESTREAM_HOST_RE = re.compile(
    r"cinerama\.uz|mcquack|zabava|-htlive\.cdn\.ngenix|kinowalk|"
    r"hopto\.org|vintera\.tv|bozztv|thestream\.cyou|vslive\.pw|"
    r"\.do\.am$|skygo\.mn|tele2dvr|videoline\.ru|mylifeisgood",
    re.IGNORECASE,
)

# Персональные/подписанные ссылки (токены, ключи, срок действия).
SIGNED_URL_RE = re.compile(
    r"[?&](token|key|auth|sign|signature|s|e|exp|expires|hash|md5|"
    r"wmsauthsign|session|sid|uid|user|pass|password)=",
    re.IGNORECASE,
)

UNVERIFIED_GROUP = "Не проверено"

# Каталожным и официальным спискам доверяем больше: при отказе похожем на
# геоблокировку поток попадает в «Не проверено», а не исчезает.
UNVERIFIED_SOURCES = {
    "iptv-org", "iptv-org без channel-id", "Free-TV", "Официальные сайты",
}

EPG_URL = "https://iptvx.one/epg/epg_lite.xml.gz"

# Официальные публичные потоки вещателей, которых нет в каталоге iptv-org.
OFFICIAL_STREAMS = [
    ("RBKTV.ru", "RBK-TV", "https://online-video.rbc.ru/online/rbctv.m3u8"),
]

# Домен VGTRK без DNS: те же пути отдаёт live.smotrim.ru.
HOST_REWRITES = {"vgtrkregion-reg.cdnvideo.ru": "live.smotrim.ru"}

FEDERAL_GROUP = "Федеральные"

# Федеральные каналы РФ (id каталога iptv-org), включая международные версии.
FEDERAL_IDS = {
    "ChannelOne.ru", "ChannelOneCIS.ru", "Russia1.ru", "RTRPlaneta.ru",
    "Match.ru", "NTV.ru", "NTVMir.ru", "Channel5.ru",
    "Channel5International.ru", "Russia24.ru", "RussiaK.ru", "Carousel.ru",
    "CarouselInternational.ru", "OTR.ru", "TVCentr.ru",
    "TVCentrInternational.ru", "RENTV.ru", "RENTVInternational.ru",
    "Spas.ru", "STS.ru", "STSInternational.ru", "Domashniy.ru",
    "DomashniyInternational.ru", "TV3.ru", "Friday.ru",
    "FridayInternational.ru", "Zvezda.ru", "Mir.ru", "TNT.ru",
    "TNTInternational.ru", "MuzTV.ru", "U.ru", "Subbota.ru", "Che.ru",
    "2x2.ru", "STSLove.ru", "TNT4.ru", "TNT4International.ru", "Solnce.ru",
    "Mir24.ru", "RT.ru", "RTDocumentary.ru", "RBKTV.ru", "Izvestia.ru",
    "Pobeda.ru",
}

# Повторная проверка с узлов check-host.net в Москве (бесплатный API).
CHECK_FROM_RUSSIA = os.environ.get("CHECK_FROM_RUSSIA", "1") != "0"
CHECK_HOST_API = "https://check-host.net"
CHECK_HOST_NODES = ("ru1.node.check-host.net", "ru2.node.check-host.net")
RU_CHECK_LIMIT = int(os.environ.get("RU_CHECK_LIMIT", "200"))
RU_CHECK_WORKERS = 2
ru_limit_reached = []
ru_answers = Counter()
RU_DEAD_RE = re.compile(
    r"no such device|not known|connection refused|no route",
    re.IGNORECASE,
)
PROBE_ALL = os.environ.get("PROBE_ALL", "1") != "0"
EPG_TIMEOUT = 120

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
        r"доктор|\bdoctor\b|здоров|zdorov|medzdrav|fakty|факты|"
        r"просвещ|prosveshch|univer ?tv|университет"
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
        r"следствие вели|нтв[- ]?хит|ntv[- ]?hit|нтв[- ]?право|"
        r"ntv[- ]?law"
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

# Федеральные общероссийские каналы; остальные «Общие» и «Прочее» из
# России уходят в «Региональные».
FEDERAL_RE = re.compile(
    r"^(channel one|channel one cis|первый канал|первый|russia[- ]?1|"
    r"россия[ -]?1|rossiya[ -]?1|rtr[- ]planeta|ртр[- ]планета|ntv|нтв|"
    r"ntv vitrina|ren tv|рен ?тв|ren tv international|tv centr|"
    r"тв центр|tvc|zvezda|звезда|mir|мир|otr|отр|channel 5|"
    r"пятый канал|5 канал|rtvi|rtvi us|ldpr tv|soyuzny|союзный|"
    r"tv brics russian|yapervyy|kongress tv|radio rossii|radio 1|"
    r"radio mir|russia[- ]?k|россия[ -]?к|kultura|культура)"
    r"( hd| international| cis)?$",
    re.IGNORECASE,
)

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

MUSIC_STYLES = [
    ("Фолк и народная", r"folk|folclor|folklor|народн|narod|volksmusik|"
                        r"tambure|manele|slager|шлягер|chamame|"
                        r"traditional|tiankov|disco polo|gugak|klape|"
                        r"\borient"),
    ("Религиозная", r"gospel|praise|worship|bibel|christ|peniel|"
                    r"kuriakos|3abn|hope channel|spirit tv|nidae"),
    ("Караоке и концерты", r"karaoke|concert|qello|live ?music|"
                           r"livexlive|концерт|караоке"),
]

MUSIC_STYLES = [
    (genre, re.compile(pattern, re.IGNORECASE))
    for genre, pattern in MUSIC_STYLES
]

MUSIC_RADIO_RE = re.compile(
    r"radio|\bfm\b|\d{2,3}[.,]\d ?fm|радио|estereo|stereo",
    re.IGNORECASE,
)

MUSIC_REGIONS = [
    ("Индия и Южная Азия", "IN PK BD NP LK"),
    ("Арабская", "SA AE EG LB JO IQ SY KW QA BH OM YE MA DZ TN LY PS SD"),
    ("Турецкая", "TR"),
    ("Латино", "MX AR CO PE CL VE EC BO PY UY CR PA DO CU PR GT HN SV NI "
               "BR ES"),
    ("Африка и Карибы", "NG GH CI SN CM KE TZ UG ZA CD CG RW ET AO MZ "
                        "ZW ZM ML BF BJ TG GA MG MU RE HT JM TT GP MQ "
                        "AW CW BB BS LC GF"),
    ("Англоязычная", "US CA GB IE AU NZ"),
    ("Азиатская", "CN KR JP TH VN ID MY PH TW HK MN SG KH LA MM IR AF "
                  "UZ TM TJ KG AZ AM GE"),
    ("Европейская", "DE AT CH FR BE NL LU IT PT PL CZ SK HU RO BG GR RS "
                    "HR SI BA ME MK AL XK MD LT LV EE FI SE NO DK IS "
                    "MT CY"),
]

MUSIC_REGIONS = [
    (genre, set(countries.split()))
    for genre, countries in MUSIC_REGIONS
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

    for genre, pattern in MUSIC_STYLES:
        if pattern.search(text):
            return f"Музыка | {genre}"

    if POP_MUSIC_RE.search(text):
        return "Музыка | Поп и хиты"

    if MUSIC_RADIO_RE.search(text):
        return "Музыка | Радио в эфире"

    for genre, countries in MUSIC_REGIONS:
        if country in countries:
            return f"Музыка | {genre}"

    return "Музыка | Другие"


def group_by_name(channel, stream):
    # Сначала имена канала, затем заголовок потока: так все потоки
    # одного канала попадают в одну группу.
    for names in (channel_names(channel), [clean(stream.get("title"))]):
        for group, pattern in NAME_RULES:
            if any(pattern.search(name) for name in names if name):
                return group

    return ""


def catalog_id(channel):
    return clean(channel.get("id")).split("@")[0]


def is_international(channel):
    country = str(channel.get("country") or "").upper()
    return bool(country) and country != "RU" and not is_music_channel(channel)


def channel_group(channel, stream):
    if catalog_id(channel) in FEDERAL_IDS:
        return FEDERAL_GROUP

    if is_music_channel(channel):
        return music_group(channel, stream)

    group = group_by_name(channel, stream)

    if group:
        return group

    categories = set(channel.get("categories") or [])
    group = "Прочее"

    for category, mapped in CATEGORY_MAP:
        if category in categories:
            group = mapped
            break

    if group == "Прочее" and channel.get("source_group"):
        group = channel["source_group"]

    if group in ("Общие", "Прочее", "Региональные"):
        group = region_group(channel, group)

    return group


def region_group(channel, group):
    """Federal channels stay in «Общие», others split by country."""
    if any(FEDERAL_RE.match(name) for name in channel_names(channel)):
        return "Общие"

    country = str(channel.get("country") or "").upper()

    if country == "RU":
        return "Региональные"

    if country == "BY":
        return "Беларусь"

    if country:
        return "СНГ и зарубежье"

    return group


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


def make_extinf(channel, stream, group, tvg_id=""):
    channel_id = clean(tvg_id or channel.get("id"))
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


# Ответы, которые обычно означают геоблокировку или временный сбой, а не
# мёртвую ссылку.
BLOCKED_HTTP_CODES = {401, 403, 429, 451, 500, 502, 503, 504, 520, 521,
                      522, 523, 524, 525, 526}


def classify_probe_error(error):
    if isinstance(error, HTTPError):
        return "blocked" if error.code in BLOCKED_HTTP_CODES else "dead"

    reason = getattr(error, "reason", error)

    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return "dead"

    if isinstance(error, URLError) and isinstance(reason, str):
        return "dead"

    return "blocked"


def probe_stream(url, user_agent="", referrer="", depth=0):
    """"ok" if the URL answers with a playlist, manifest or media data,
    "blocked" if it looks geo-blocked or temporarily down, else "dead"."""
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
                return "dead"

            final_url = response.geturl()
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()
            head = response.read(16384)
    except Exception as error:
        return classify_probe_error(error)

    text = head.decode("utf-8", errors="ignore")

    if "#EXTM3U" in text:
        if "#EXT-X-STREAM-INF" in text and depth == 0:
            variants = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.startswith("#")
            ]

            if variants:
                # Мастер-плейлист ответил, но сегменты недоступны: обычно
                # это геоблокировка CDN, а не мёртвый канал.
                variant = probe_stream(
                    urljoin(final_url, variants[0]),
                    user_agent,
                    referrer,
                    depth + 1,
                )
                return "ok" if variant == "ok" else "blocked"

        return "ok"

    if "<MPD" in text or head[:1] == b"\x47":
        return "ok"

    if any(
        kind in content_type
        for kind in ("video/", "mp2t", "octet-stream", "audio/")
    ) and not text.lstrip().startswith("<"):
        return "ok"

    return "dead"


def check_host_request(path):
    request = Request(
        CHECK_HOST_API + path,
        headers={"Accept": "application/json", "User-Agent": "curl/8.5.0"},
    )

    with urlopen(request, timeout=30) as response:
        return json.load(response)


def check_from_russia(url, has_headers=False):
    """"ok", "dead", "forbidden", "unknown" or "limit" by the answer of
    check-host nodes in Russia."""
    if ru_limit_reached:
        return "limit"

    query = urlencode(
        [("host", url)] + [("node", node) for node in CHECK_HOST_NODES]
    )

    try:
        for _attempt in range(2):
            answer = check_host_request("/check-http?" + query)

            if answer.get("error") != "limit_exceeded":
                break

            time.sleep(60)
        else:
            ru_limit_reached.append(True)
            return "limit"

        if "request_id" not in answer:
            ru_answers["отказ: " + str(answer.get("error"))[:40]] += 1
            return "unknown"

        request_id = answer["request_id"]
        result = {}

        for _attempt in range(20):
            time.sleep(4)
            result = check_host_request("/check-result/" + request_id)

            if result and all(value is not None for value in result.values()):
                break
    except Exception as error:
        ru_answers[f"ошибка {type(error).__name__}: {str(error)[:40]}"] += 1
        return "unknown"

    if not result or all(value is None for value in result.values()):
        ru_answers["узлы не ответили за 80 с"] += 1
        return "unknown"

    answers = []

    for value in result.values():
        if not value or not value[0]:
            continue

        entry = list(value[0]) + [None, None, None, None]
        message, code = str(entry[2] or ""), str(entry[3] or "")
        ru_answers[code or message[:40] or "нет ответа"] += 1

        if code.startswith("2"):
            return "ok"

        if code in ("404", "410") or (
            not code and RU_DEAD_RE.search(message)
        ):
            answers.append("dead")
        elif code in ("401", "403") and not has_headers:
            answers.append("forbidden")
        else:
            answers.append("unknown")

    for state in ("dead", "forbidden"):
        if state in answers and set(answers) <= {state, "unknown"}:
            return state

    return "unknown"


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
    """URLs of the previous playlist, URLs in «Не проверено», entry count."""
    if not OUT.exists():
        return set(), set(), 0

    content = OUT.read_text(encoding="utf-8-sig")
    urls = set()
    unverified = set()
    total = 0

    for info, _extras, url in parse_m3u(content):
        urls.add(url)
        total += 1

        if get_attribute(info, "group-title") == UNVERIFIED_GROUP:
            unverified.add(url)

    return urls, unverified, total


def epg_key(name):
    value = clean(name).lower().replace("ё", "е")
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", value)
    value = re.sub(r"\b(hd|sd|fhd|uhd|4k|full hd|orig)\b", "", value)
    return re.sub(r"[^a-zа-я0-9+]", "", value)


def load_epg_channels():
    """Map of normalized channel names to EPG ids (only <channel> part)."""
    names = {}

    try:
        with urlopen(
            Request(EPG_URL, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=EPG_TIMEOUT,
        ) as response:
            with gzip.GzipFile(fileobj=response) as data:
                for _event, element in ET.iterparse(data):
                    if element.tag == "programme":
                        break

                    if element.tag != "channel":
                        continue

                    for display in element.findall("display-name"):
                        key = epg_key(display.text)

                        if key:
                            names.setdefault(key, element.get("id"))

                    element.clear()
    except Exception as error:
        print(f"EPG недоступен, пропускаю сопоставление: {error}")
        return {}

    print(f"EPG: каналов в программе: {len(set(names.values()))}")
    return names


def stream_from_m3u(info, extras, title, quality):
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

    return stream


def validate_m3u(output):
    if not output or not output[0].startswith("#EXTM3U"):
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

    previous_urls, previous_unverified, previous_total = (
        read_previous_playlist()
    )

    output = [f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"']
    seen_urls = set()
    counts = Counter()
    stats = Counter()
    duplicates = Counter()
    sources = Counter()
    entries = []

    def add_entry(channel, stream, group, stream_url, source):
        parsed = urlparse(stream_url)

        if parsed.hostname in HOST_REWRITES:
            stream_url = parsed._replace(netloc=parsed.netloc.replace(
                parsed.hostname, HOST_REWRITES[parsed.hostname],
            )).geturl()

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

        stream = stream_from_m3u(info, extras, title, quality)
        group = channel_group(channel, stream)
        add_entry(channel, stream, group, stream_url, "Free-TV")

    for official_id, official_name, stream_url in OFFICIAL_STREAMS:
        channel = channels_by_id.get(official_id) or {
            "id": official_id, "name": official_name, "categories": [],
        }
        stream = {"title": official_name, "quality": ""}
        add_entry(
            channel, stream, channel_group(channel, stream), stream_url,
            "Официальные сайты",
        )

    for source, url in COMMUNITY_SOURCES.items():
        try:
            community = list(parse_m3u(fetch_text(url)))
            print(f"{source}: получено записей: {len(community)}")
        except Exception as error:
            print(f"{source} недоступен, пропускаю: {error}")
            stats[f"{source}: недоступен"] += 1
            continue

        for info, extras, stream_url in community:
            source_group = get_attribute(info, "group-title")

            if COMMUNITY_SKIP_GROUP_RE.search(source_group):
                stats[f"{source}: кинозалы/рестримы пропущены"] += 1
                continue

            if not stream_url.startswith(("http://", "https://")):
                stats["Некорректные ссылки"] += 1
                continue

            host = urlparse(stream_url).hostname or ""

            if is_page_url(stream_url) or RESTREAM_HOST_RE.search(host):
                stats[f"{source}: кинозалы/рестримы пропущены"] += 1
                continue

            if SIGNED_URL_RE.search(stream_url):
                stats[f"{source}: персональные/подписанные ссылки"] += 1
                continue

            tvg_id = get_attribute(info, "tvg-id")
            title, quality = split_title(info)
            channel = channels_by_id.get(tvg_id.split("@")[0])

            if channel and channel.get("is_nsfw"):
                continue

            if (
                excluded_country(channel=channel, channel_id=tvg_id)
                or excluded_title(title)
            ):
                stats[f"{source}: Казахстан/Украина исключены"] += 1
                continue

            if channel:
                if not (
                    is_music_channel(channel)
                    or channel_is_russian(channel, feeds_by_channel)
                ):
                    stats[f"{source}: не русскоязычные"] += 1
                    continue
            elif not CYRILLIC_RE.search(title):
                stats[f"{source}: не русскоязычные"] += 1
                continue
            else:
                mapped = COMMUNITY_GROUP_MAP.get(source_group.lower(), "")
                channel = {
                    "id": "",
                    "name": title,
                    "categories": (
                        ["music"] if "музык" in source_group.lower()
                        else source_group.lower().split(";")
                    ),
                    "country": "RU",
                    "source_group": mapped,
                }

            stream = stream_from_m3u(info, extras, title, quality)
            group = channel_group(channel, stream)
            add_entry(channel, stream, group, stream_url, source)

    for item in entries:
        sources[item["source"]] += 1

    def is_new(item):
        return (
            item["url"] not in previous_urls
            or item["url"] in previous_unverified
        )

    def has_headers(item):
        return bool(
            item["stream"].get("user_agent")
            or item["stream"].get("referrer")
            or item["stream"].get("http_referrer")
        )

    def ru_priority(item):
        if catalog_id(item["channel"]) in FEDERAL_IDS:
            return 0

        if is_international(item["channel"]):
            return 1

        return 2 if is_new(item) else 3

    probe = Counter()
    ru_checked = Counter()

    if CHECK_NEW_STREAMS:
        targets = entries if PROBE_ALL else [
            item for item in entries if is_new(item)
        ]
        print(f"Проверяю потоки из США: {len(targets)} ...")

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
                targets,
            ))

        candidates = sorted(
            (item for item, us in zip(targets, results) if us != "ok"),
            key=ru_priority,
        )
        ru_status = {}

        if CHECK_FROM_RUSSIA and candidates:
            batch = candidates[:RU_CHECK_LIMIT]
            print(f"Проверяю из России (check-host.net): {len(batch)} "
                  f"из {len(candidates)} ...")

            with ThreadPoolExecutor(RU_CHECK_WORKERS) as pool:
                ru_results = list(pool.map(
                    lambda item: check_from_russia(
                        item["url"], has_headers(item),
                    ),
                    batch,
                ))

            ru_status = {
                item["url"]: result
                for item, result in zip(batch, ru_results)
            }
            ru_checked.update(ru_results)

        failed = set()

        for item, us in zip(targets, results):
            ru = ru_status.get(item["url"], "")
            new = is_new(item)

            if us == "ok":
                status = "ok"
            elif ru == "ok":
                status = "ok_ru"
            elif ru == "dead" or (us == "dead" and new):
                status = "dead"
            elif ru == "forbidden" and new:
                status = "forbidden"
            else:
                status = "unverified"

            if status in ("dead", "forbidden"):
                failed.add(item["url"])
            elif status == "unverified" and new:
                if item["source"] in UNVERIFIED_SOURCES:
                    item["group"] = UNVERIFIED_GROUP
                else:
                    status = "blocked"
                    failed.add(item["url"])

            item["status"] = status
            probe[("новые" if new else "уже были", status)] += 1

        entries = [item for item in entries if item["url"] not in failed]
        seen_urls -= failed
    else:
        for item in entries:
            if item["url"] in previous_unverified:
                item["group"] = UNVERIFIED_GROUP

    epg_names = load_epg_channels()
    epg_matched = 0

    for item in entries:
        for name in channel_names(item["channel"], item["stream"]):
            epg_id = epg_names.get(epg_key(name))

            if epg_id:
                item["epg_id"] = epg_id
                epg_matched += 1
                break

    entries.sort(
        key=lambda item: (
            item["group"] == ADULT_GROUP,
            item["group"] == UNVERIFIED_GROUP,
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
                item.get("epg_id", ""),
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
        print("\nПроверка потоков (США, затем узлы check-host.net в Москве):")
        labels = {
            "ok": "работает (проверено из США)",
            "ok_ru": "работает (проверено из России)",
            "unverified": "требует проверки, оставлен",
            "blocked": "требует проверки, не добавлен",
            "dead": "не работает, удалён / не добавлен",
            "forbidden": "доступ закрыт и из России (403), не добавлен",
        }

        for (kind, status), count in sorted(probe.items()):
            print(f"  {kind}: {labels[status]}: {count}")

        print(f"  Проверено из России: "
              f"{sum(ru_checked.values()) - ru_checked['limit']} "
              f"(работают {ru_checked['ok']}, не работают "
              f"{ru_checked['dead']}, 403 {ru_checked['forbidden']}, "
              f"неясно {ru_checked['unknown']}); не проверено из-за "
              f"лимита check-host: {ru_checked['limit']}")
        print("  Ответы узлов в России: " + ", ".join(
            f"{answer} — {count}" for answer, count in ru_answers.most_common(8)
        ))

        working = {"ok", "ok_ru"}
        federal = defaultdict(Counter)
        international = defaultdict(Counter)

        for item in entries:
            key = catalog_id(item["channel"]) or item["channel"].get("name")
            state = "ok" if item.get("status") in working else "other"

            if catalog_id(item["channel"]) in FEDERAL_IDS:
                federal[key][state] += 1
            elif is_international(item["channel"]):
                international[key][state] += 1

        print("\nУчёт каналов:")
        for title, found in (("Федеральные каналы РФ", federal),
                             ("Международные на русском", international)):
            alive = sum(1 for value in found.values() if value["ok"])
            print(f"  {title}: найдено {len(found)}, с работающим "
                  f"потоком {alive}")

        print("\nФедеральные каналы (работает / всего потоков):")
        for key in sorted(FEDERAL_IDS):
            value = federal.get(key, Counter())
            total_streams = value["ok"] + value["other"]
            print(f"  {key}: {value['ok']} / {total_streams}"
                  + ("" if total_streams else " — нет публичного потока"))
    else:
        print("\nПроверка новых ссылок отключена (CHECK_NEW_STREAMS=0)")

    print(f"\nEPG: сопоставлено {epg_matched} из {len(entries)} записей")

    print("\nКатегории:")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")

    print("\nПлейлист успешно создан:", OUT)


if __name__ == "__main__":
    main()
