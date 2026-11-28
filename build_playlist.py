#!/usr/bin/env python3
"""Build a Russian-language and European TV playlist for OTTPlayer."""

import csv
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
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
API_CONNECT_TIMEOUT = int(os.environ.get("IPTVORG_API_CONNECT_TIMEOUT", "15"))
API_READ_TIMEOUT = int(os.environ.get("IPTVORG_API_READ_TIMEOUT", "30"))
API_TOTAL_TIMEOUT = int(os.environ.get("IPTVORG_API_TOTAL_TIMEOUT", "180"))
API_MAX_RETRIES = min(3, max(0, int(os.environ.get("IPTVORG_API_RETRIES", "3"))))
API_CACHE_MAX_AGE = 24 * 60 * 60
API_CACHE_DIR = Path(os.environ.get("IPTVORG_CACHE_DIR", ".cache/iptv-org"))
_JSON_CACHE = {}
SOURCE_TIMEOUT = int(os.environ.get("SOURCE_TIMEOUT", "45"))
LOCAL_DATABASE_DIR = os.environ.get("IPTVORG_DATABASE_DIR", "")
LOCAL_STREAMS_DIR = os.environ.get("IPTVORG_STREAMS_DIR", "")
_LOCAL_STREAMS_CACHE = None

OUT = Path("playlist.m3u")
CHECK_REPORT = Path("stream_check_report.csv")
EXCLUDED_REPORT = Path("excluded_channels.csv")
INTERNATIONAL_FILE = Path("russian-international.m3u")
REGIONAL_FILE = Path("regional.m3u")
ADULT_FILE = Path("adult.m3u")

ADULT_GROUP = "Для взрослых"  # Retained only to parse legacy M3U metadata.

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
RU_CHECK_LIMIT = int(os.environ.get("RU_CHECK_LIMIT", "120"))
RU_CHECK_WORKERS = 2
ru_limit_reached = []
ru_answers = Counter()
RU_DEAD_RE = re.compile(
    r"no such device|not known|connection refused|no route",
    re.IGNORECASE,
)
PROBE_ALL = os.environ.get("PROBE_ALL", "1") != "0"
EPG_TIMEOUT = int(os.environ.get("EPG_TIMEOUT", "60"))

# Новые ссылки (которых ещё нет в playlist.m3u) проверяются перед
# добавлением. Отключить: CHECK_NEW_STREAMS=0.
CHECK_NEW_STREAMS = os.environ.get("CHECK_NEW_STREAMS", "1") != "0"
PROBE_TIMEOUT = 12
PROBE_WORKERS = 32
AUDIO_PROBE_TIMEOUT = int(os.environ.get("AUDIO_PROBE_TIMEOUT", "12"))
AUDIO_PROBE_WORKERS = int(os.environ.get("AUDIO_PROBE_WORKERS", "6"))
PROBE_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
UNSUPPORTED_CODEC_RE = re.compile(
    r"decoder.*not found|unknown decoder|unsupported codec|"
    r"codec .* not currently supported",
    re.IGNORECASE,
)

# Защита рабочего плейлиста: не перезаписывать его, если новая сборка
# резко меньше предыдущей.
# Existing playlists contain large excluded music and non-European sections.
# Keep a meaningful floor while allowing this intentional policy migration.
MIN_KEEP_RATIO = 0.2
MIN_KEEP_COUNT = 1

EUROPEAN_COUNTRIES = set("""
AL AD AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE
IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM UA
VA XK
""".split())
RADIO_CATEGORY_RE = re.compile(r"\bradio\b|радио|\bfm\b", re.IGNORECASE)
RADIO_NAME_RE = re.compile(r"\bradio\b|радио|\b\d{2,3}(?:[.,]\d)?\s*fm\b", re.IGNORECASE)
MUSIC_SOURCE_GROUP_RE = re.compile(r"music|музык|radio|радио|\bfm\b", re.IGNORECASE)


def music_source_group(value):
    return bool(MUSIC_SOURCE_GROUP_RE.search(str(value or "")))

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

MUSIC_NAME_RE = re.compile(
    r"music|музык|муз[\s-]?тв|музон",
    re.IGNORECASE,
)


def fetch_json(name):
    """Fetch one iptv-org API document with bounded retries and safe caching."""
    if name in _JSON_CACHE:
        return _JSON_CACHE[name]

    url = f"{API}/{name}.json"
    request = Request(
        url,
        headers={"User-Agent": "OTTPlayer-Russian-Playlist/2.0"},
    )
    meta_path = API_CACHE_DIR / f"{name}.json.meta"

    def validate(payload):
        data = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(data, list) or not data:
            raise ValueError("ожидался непустой JSON-массив")
        required = {
            "channels": ("id", "name"),
            "feeds": ("id", "channel"),
            # iptv-org legitimately includes unassociated streams with null
            # channel IDs; URL is the required stream record field.
            "streams": ("url",),
        }.get(name, ())
        if not all(isinstance(row, dict) for row in data):
            raise ValueError("массив содержит элементы не-объекты")
        if required and not all(all(row.get(key) for key in required) for row in data):
            raise ValueError(f"записи должны содержать поля {', '.join(required)}")
        if name == "streams" and not all(
            urlparse(str(row.get("url", ""))).scheme.lower()
            in {"http", "https", "rtmp", "rtmps", "rtsp", "srt"}
            for row in data
        ):
            raise ValueError("в streams.json обнаружены некорректные URL")
        return data

    def read_recent_cache():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = float(metadata["fetched_at"])
            age = time.time() - fetched_at
            if age < 0 or age > API_CACHE_MAX_AGE:
                raise ValueError(f"кэш устарел ({age / 3600:.1f} ч)")
            import hashlib
            filename = str(metadata["filename"])
            if (Path(filename).name != filename or not filename.startswith("streams-")
                    or not filename.endswith(".json")):
                raise ValueError("небезопасное имя файла в метаданных кэша")
            payload = (API_CACHE_DIR / filename).read_bytes()
            if hashlib.sha256(payload).hexdigest() != metadata["sha256"]:
                raise ValueError("контрольная сумма кэша не совпала")
            return validate(payload)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    print(
        f"Загружаю {name}.json (connect {API_CONNECT_TIMEOUT} с, "
        f"read {API_READ_TIMEOUT} с) ...", flush=True,
    )
    last_error = None
    for attempt in range(API_MAX_RETRIES + 1):
        started = time.monotonic()
        deadline = started + API_TOTAL_TIMEOUT
        received = 0
        next_progress = 1024 * 1024
        chunks = []
        try:
            with urlopen(request, timeout=API_CONNECT_TIMEOUT) as response:
                status = getattr(response, "status", response.getcode())
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                sock = getattr(
                    getattr(getattr(response, "fp", None), "raw", None),
                    "_sock", None,
                )
                if sock is not None:
                    sock.settimeout(API_READ_TIMEOUT)
                total = int(response.headers.get("Content-Length") or 0)
                if total < 0:
                    raise ValueError("некорректный Content-Length")
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"превышено общее время загрузки {API_TOTAL_TIMEOUT} с"
                        )
                    # read1 returns available data promptly instead of waiting
                    # for a full megabyte, so the total deadline is checked
                    # even when a server trickles bytes continuously.
                    block = response.read1(1024 * 1024)
                    if not block:
                        break
                    chunks.append(block)
                    received += len(block)
                    if received >= next_progress or (total and received == total):
                        size_text = f"{total / 1048576:.2f}" if total else "неизвестен"
                        print(
                            f"  {received / 1048576:.2f} МБ / {size_text} МБ; "
                            f"прошло {time.monotonic() - started:.1f} с",
                            flush=True,
                        )
                        next_progress = received + 1024 * 1024
                if not received or (total and received != total):
                    raise ValueError(
                        f"неполная загрузка: {received} из {total or 'неизвестно'} байт"
                    )
                size_text = f"{total / 1048576:.2f}" if total else "неизвестен"
                print(
                    f"  Загрузка завершена: {received / 1048576:.2f} МБ / "
                    f"{size_text} МБ за {time.monotonic() - started:.1f} с",
                    flush=True,
                )
            payload = b"".join(chunks)
            data = validate(payload)
            if name == "streams":
                import hashlib
                API_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                checksum = hashlib.sha256(payload).hexdigest()
                version_name = f"streams-{checksum}.json"
                version_path = API_CACHE_DIR / version_name
                temp_path = API_CACHE_DIR / f"{version_name}.tmp"
                temp_meta = meta_path.with_suffix(".meta.tmp")
                temp_path.write_bytes(payload)
                os.replace(temp_path, version_path)
                temp_meta.write_text(json.dumps({
                    "fetched_at": time.time(),
                    "sha256": checksum,
                    "size": len(payload),
                    "filename": version_name,
                }), encoding="utf-8")
                os.replace(temp_meta, meta_path)
                for old_path in API_CACHE_DIR.glob("streams-*.json"):
                    if old_path != version_path:
                        old_path.unlink(missing_ok=True)
            _JSON_CACHE[name] = data
            print(
                f"  Загружено записей: {len(data)} за "
                f"{time.monotonic() - started:.1f} с",
                flush=True,
            )
            return data
        except Exception as error:
            last_error = error
            if attempt < API_MAX_RETRIES:
                pause = (1, 3, 7)[attempt]
                print(
                    f"  Попытка {attempt + 1}/{API_MAX_RETRIES + 1} не удалась: "
                    f"{error} (за {time.monotonic() - started:.1f} с); "
                    f"повтор через {pause} с", flush=True,
                )
                time.sleep(pause)

    if name == "streams":
        cached = read_recent_cache()
        if cached is not None:
            _JSON_CACHE[name] = cached
            print("  Используется проверенный streams.json из кэша (не старше 24 ч).", flush=True)
            return cached
        raise RuntimeError(
            f"Не удалось получить корректный iptv-org {name}.json после "
            f"{API_MAX_RETRIES + 1} попыток; свежий проверенный кэш (24 ч) "
            f"недоступен: {last_error}"
        ) from last_error

    try:
        data = load_local_iptvorg_data(name)
    except Exception as local_error:
        raise RuntimeError(
            f"Не удалось загрузить iptv-org {name}.json после "
            f"{API_MAX_RETRIES + 1} попыток: {last_error}; "
            f"резервный каталог недоступен: {local_error}"
        ) from local_error
    _JSON_CACHE[name] = data
    print(f"  Использован локальный официальный каталог iptv-org для {name}")
    return data


def _csv_values(value):
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def load_local_iptvorg_data(name):
    """Fallback to the official iptv-org/database CSV and iptv stream M3Us."""
    if not LOCAL_DATABASE_DIR or not LOCAL_STREAMS_DIR:
        raise RuntimeError("IPTVORG_DATABASE_DIR/IPTVORG_STREAMS_DIR are not configured")

    database = Path(LOCAL_DATABASE_DIR) / "data"
    if name in {"channels", "feeds"}:
        filename = database / f"{name}.csv"
        with filename.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if name == "channels":
            return [{
                **row,
                "alt_names": _csv_values(row.get("alt_names")),
                "categories": _csv_values(row.get("categories")),
                "is_nsfw": str(row.get("is_nsfw", "")).lower() == "true",
            } for row in rows]
        return [{
            **row,
            "languages": _csv_values(row.get("languages")),
        } for row in rows]

    if name != "streams":
        raise ValueError(f"Unsupported local iptv-org data file: {name}")

    global _LOCAL_STREAMS_CACHE
    if _LOCAL_STREAMS_CACHE is not None:
        return _LOCAL_STREAMS_CACHE

    collected = []
    for playlist in sorted(Path(LOCAL_STREAMS_DIR).rglob("*.m3u")):
        try:
            content = playlist.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for info, extras, url in parse_m3u(content):
            raw_id = get_attribute(info, "tvg-id")
            if "@" in raw_id:
                channel_id, feed_id = raw_id.split("@", 1)
            else:
                channel_id, feed_id = raw_id, ""
            if not channel_id or not url.startswith(("http://", "https://")):
                continue
            title, quality = split_title(info)
            stream = stream_from_m3u(info, extras, title, quality)
            collected.append({
                "channel": channel_id,
                "feed": feed_id,
                "url": url,
                "title": title,
                "quality": quality,
                "user_agent": stream.get("user_agent", ""),
                "referrer": stream.get("referrer", ""),
                "source_file": playlist.name,
            })
    _LOCAL_STREAMS_CACHE = collected
    if not collected:
        raise RuntimeError(f"No stream records found under {LOCAL_STREAMS_DIR}")
    return collected


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


def is_music_channel(channel, stream=None):
    categories = set(channel.get("categories") or [])

    if "music" in categories:
        return True

    # Official channel names may be transliterated while the Russian music
    # indicator appears only in an alias or stream title (e.g. Multimuzyka).
    names = channel_names(channel, stream)
    names.append(channel.get("id", ""))
    return any(MUSIC_NAME_RE.search(name) for name in names)


def is_radio_channel(channel, stream=None):
    categories = {str(value).lower() for value in (channel.get("categories") or [])}
    if "radio" in categories:
        return True
    names = channel_names(channel, stream)
    return any(RADIO_NAME_RE.search(name) for name in names)


def channel_country(channel, stream=None):
    """Read country metadata, including manually imported M3U metadata."""
    values = [channel.get("country")]
    if stream:
        values.extend((stream.get("country"), stream.get("tvg_country")))
    return next((str(value).strip().upper() for value in values
                 if str(value or "").strip()), "")


def allowed_tv_channel(channel, stream=None, feed=None):
    """Allow Russian-language TV or TV catalogued in a European country."""
    if is_music_channel(channel, stream) or is_radio_channel(channel, stream):
        return False, "Музыкальный канал или радио"
    country = channel_country(channel, stream)
    if country == "RU" or country in EUROPEAN_COUNTRIES:
        return True, ""
    if feed and language_is_russian(feed.get("languages")):
        return True, ""
    return False, "Нет подтверждения русского языка или европейской страны"


def discovery_brand(channel):
    text = " ".join(channel_names(channel)).lower()
    if "discovery" not in text:
        return ""
    return "Investigation Discovery (ID)" if "investigation" in text or "id discovery" in text else "Discovery Channel"


def channel_names(channel, stream=None):
    names = [channel.get("name")]
    names.extend(channel.get("alt_names") or [])

    if stream is not None:
        names.append(stream.get("title"))

    return [clean(name) for name in names if clean(name)]


# Канонические русские названия федеральных телеканалов.
# Меняется только отображаемое имя; tvg-id и адрес потока не затрагиваются.
RUSSIAN_OFFICIAL_NAMES = {
    "ChannelOne.ru": "Первый канал",
    "ChannelOneCIS.ru": "Первый канал",
    "Russia1.ru": "Россия 1",
    "RTRPlaneta.ru": "РТР-Планета",
    "Match.ru": "Матч ТВ",
    "NTV.ru": "НТВ",
    "NTVMir.ru": "НТВ Мир",
    "Channel5.ru": "Пятый канал",
    "Channel5International.ru": "Пятый канал",
    "Russia24.ru": "Россия 24",
    "RussiaK.ru": "Россия Культура",
    "Carousel.ru": "Карусель",
    "CarouselInternational.ru": "Карусель",
    "OTR.ru": "Общественное телевидение России",
    "TVCentr.ru": "ТВ Центр",
    "TVCentrInternational.ru": "ТВ Центр",
    "RENTV.ru": "РЕН ТВ",
    "RENTVInternational.ru": "РЕН ТВ",
    "Spas.ru": "Спас",
    "STS.ru": "СТС",
    "STSInternational.ru": "СТС",
    "Domashniy.ru": "Домашний",
    "DomashniyInternational.ru": "Домашний",
    "TV3.ru": "ТВ-3",
    "Friday.ru": "Пятница!",
    "FridayInternational.ru": "Пятница!",
    "Zvezda.ru": "Звезда",
    "Mir.ru": "МИР",
    "TNT.ru": "ТНТ",
    "TNTInternational.ru": "ТНТ",
    "MuzTV.ru": "МУЗ-ТВ",
    "U.ru": "Ю",
    "Subbota.ru": "Суббота!",
    "Che.ru": "Че!",
    "2x2.ru": "2х2",
    "STSLove.ru": "СТС Love",
    "TNT4.ru": "ТНТ4",
    "TNT4International.ru": "ТНТ4",
    "Solnce.ru": "Солнце",
    "Mir24.ru": "МИР 24",
    "RT.ru": "RT",
    "RTDocumentary.ru": "RT Док",
    "RBKTV.ru": "РБК",
    "Izvestia.ru": "Известия",
    "Pobeda.ru": "Победа",
}


def display_channel_name(channel, stream):
    """Выбирает русское официальное имя, не меняя ID и оригинальные бренды."""
    channel_id = catalog_id(channel)
    official = RUSSIAN_OFFICIAL_NAMES.get(channel_id)
    if official:
        return official

    original = clean(channel.get("name"))
    if not original:
        original = clean(stream.get("title")) or channel_id

    country = str(channel.get("country") or "").upper()
    if country in {"RU", "BY"}:
        # У каталога могут быть русские альтернативные названия.
        aliases = channel.get("alt_names") or []
        if isinstance(aliases, str):
            aliases = [aliases]

        candidates = [original] + [
            clean(alias) for alias in aliases if clean(alias)
        ]
        # Предпочитаем исходное русское имя; иначе первую русскую альтернативу.
        for candidate in candidates:
            if CYRILLIC_RE.search(candidate):
                return candidate

    # Международные бренды (Discovery, National Geographic и т.п.)
    # оставляем под официальным оригинальным названием.
    return original


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

    # Time-shifted versions of national Russia 1 are still federal channels.
    shifted_russia_one = re.compile(
        r"^(?:россия|russia|rossiya)[\s-]*1\s*\(\+\d+\)$",
        re.IGNORECASE,
    )
    if any(
        shifted_russia_one.fullmatch(
            re.sub(r"\s*\[[^\]]+\]\s*$", "", name).strip()
        )
        for name in channel_names(channel, stream)
    ):
        return FEDERAL_GROUP

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
    channel_name = display_channel_name(channel, stream)

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


def add_manual_file(path, default_group, entries, seen_urls, stats):
    """Load manual M3U entries into the common pipeline (including probes)."""
    if not path.exists():
        return

    print(f"Добавляю ручной список в общую проверку: {path}")

    content = path.read_text(encoding="utf-8-sig")

    for info, extras, stream_url in parse_m3u(content):
        if not stream_url.startswith(("http://", "https://")):
            stats["Ручные списки: некорректная ссылка"] += 1
            continue

        manual_channel_id = get_attribute(info, "tvg-id")
        excluded = excluded_country(channel_id=manual_channel_id)

        if excluded:
            stats[f"{excluded} исключена (ручные списки)"] += 1
            continue

        title, quality = split_title(info)
        country = get_attribute(info, "tvg-country").upper()
        language = get_attribute(info, "tvg-language").lower()
        manual_channel = {
            "id": manual_channel_id,
            "name": get_attribute(info, "tvg-name") or title,
            "country": country,
            "categories": (["music"] if "музык" in get_attribute(info, "group-title").lower()
                            or "music" in get_attribute(info, "group-title").lower()
                            else ["radio"] if RADIO_CATEGORY_RE.search(get_attribute(info, "group-title"))
                            else []),
        }
        manual_stream = stream_from_m3u(info, extras, title, quality)
        approved, reason = allowed_tv_channel(manual_channel, manual_stream)
        if language in RUSSIAN_CODES:
            approved = not is_music_channel(manual_channel) and not is_radio_channel(manual_channel, manual_stream)
            reason = "" if approved else "Музыкальный канал или радио"
        if default_group == ADULT_GROUP:
            approved, reason = False, "Взрослый поток не является разрешённым телеканалом"
        if not approved:
            stats[f"Ручной список: {reason}"] += 1
            continue

        if stream_url in seen_urls:
            stats["Дубликаты ссылок (ручные списки)"] += 1
            continue

        seen_urls.add(stream_url)
        old_group = get_attribute(info, "group-title")

        if default_group == ADULT_GROUP:
            group = ADULT_GROUP
        else:
            group = default_group

        info = normalize_manual_quality(info)
        rest = re.sub(
            r'\s+group-title="[^"]*"', "", info,
            flags=re.IGNORECASE,
        )
        match = re.match(r"^(#EXTINF:)(-?\d+(?:\.\d+)?)(.*)$", rest)
        if match:
            prefix, duration, tail = match.groups()
            info = f'{prefix}{duration} group-title="{group}"{tail}'

        stream = stream_from_m3u(info, extras, title, quality)
        channel = {
            "id": manual_channel_id,
            "name": get_attribute(info, "tvg-name") or title,
            "categories": [],
            "country": country,
        }
        entries.append({
            "channel": channel,
            "stream": stream,
            "group": group,
            "url": stream_url,
            "quality": clean(quality),
            "source": f"Ручной список: {path.name}",
            "language": language,
            "manual_info": info,
            "manual_extras": extras,
        })


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


def _read_url_bytes(url, headers, context, limit=262144, byte_range=None):
    request_headers = dict(headers)
    if byte_range:
        request_headers["Range"] = byte_range
    response = open_following_redirects(url, request_headers, context)
    with response:
        if response.status >= 400:
            raise HTTPError(url, response.status, "HTTP error", response.headers, None)
        return response.geturl(), response.headers, response.read(limit)


def _is_media_segment(data, content_type=""):
    """Reject HTML/error pages; accept MPEG-TS, fMP4 and recognizable media."""
    if not data or len(data) < 188:
        return False
    prefix = data[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"{")):
        return False
    # MPEG-TS packets have a 0x47 sync byte every 188 bytes.
    if len(data) >= 376 and data[0] == 0x47 and data[188] == 0x47:
        return True
    # ISO-BMFF / fragmented MP4 boxes.
    if any(marker in data[:256] for marker in (b"ftyp", b"styp", b"moof", b"mdat")):
        return True
    media_types = ("video/", "audio/", "mp2t", "mp4", "octet-stream")
    return any(value in (content_type or "").lower() for value in media_types) and len(data) >= 1024


def probe_stream(url, user_agent="", referrer="", depth=0):
    """Confirm actual media: HLS playlist + at least one real media segment."""
    headers = {"User-Agent": user_agent or PROBE_USER_AGENT, "Accept": "*/*"}
    if referrer:
        headers["Referer"] = referrer

    context = ssl.create_default_context()

    last_state = "blocked"
    for attempt in range(2):
        try:
            final_url, response_headers, head = _read_url_bytes(
                url, headers, context, limit=262144,
            )
            content_type = response_headers.get("Content-Type", "")
            text = head.decode("utf-8-sig", errors="ignore")

            if "#EXTM3U" in text:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if "#EXT-X-STREAM-INF" in text:
                    if depth >= 2:
                        return "blocked"
                    variants = []
                    for index, line in enumerate(lines):
                        if line.startswith("#EXT-X-STREAM-INF"):
                            for candidate in lines[index + 1:]:
                                if not candidate.startswith("#"):
                                    variants.append(candidate)
                                    break
                    if not variants:
                        return "dead"
                # Try up to two variants in case the first rendition is stale.
                    states = [probe_stream(urljoin(final_url, item), user_agent, referrer, depth + 1)
                              for item in variants[:2]]
                    return "ok" if "ok" in states else ("dead" if all(x == "dead" for x in states) else "blocked")

                segments = [
                    line for line in lines
                    if not line.startswith("#") and re.match(r"^(https?://|/|[^#])", line)
                ]
                if not segments:
                    return "blocked"
                checked = 0
                for segment in segments[:3]:
                    segment_url = urljoin(final_url, segment)
                    try:
                        _segment_url, segment_headers, data = _read_url_bytes(
                            segment_url, headers, context, limit=65536,
                            byte_range="bytes=0-65535",
                        )
                        if _is_media_segment(data, segment_headers.get("Content-Type", "")):
                            checked += 1
                    except Exception as error:
                        last_state = classify_probe_error(error)
                if checked >= 3:
                    return "ok"
                if checked == 1:
                    return "blocked"
                return last_state if last_state != "blocked" else "dead"

            if "<MPD" in text:
                # DASH manifest requires resolving BaseURL/SegmentURL, not just HTTP 200.
                return "blocked"
            if _is_media_segment(head, content_type):
                return "ok"
            last_state = "dead"
        except Exception as error:
            last_state = classify_probe_error(error)
        if attempt == 0:
            time.sleep(1.5)
    return last_state


def probe_audio_track(url, user_agent="", referrer=""):
    """Confirm audio packets and decode a short sample with FFmpeg.

    This does not guarantee that a particular TV can decode the codec or that
    the broadcast contains audible sound. A missing track is definitive only
    when video packets were read successfully and no audio stream was exposed.
    Transient/unsupported probes remain 'unknown' and do not delete a channel.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {
            "status": "unknown",
            "codecs": "",
            "details": "ffprobe is not installed",
        }

    command = [
        ffprobe,
        "-v", "error",
        "-rw_timeout", "10000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-read_intervals", "%+5",
        "-show_streams",
        "-show_packets",
        "-show_entries",
        "stream=index,codec_type,codec_name,channels,channel_layout,sample_rate:packet=stream_index,size",
        "-of", "json",
    ]

    if user_agent:
        command.extend(["-user_agent", user_agent])

    if referrer:
        safe_referrer = str(referrer).replace("\r", "").replace("\n", "")
        command.extend(["-headers", f"Referer: {safe_referrer}\r\n"])

    command.extend(["-i", url])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=AUDIO_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "codecs": "", "details": "audio probe timeout"}
    except OSError as error:
        return {
            "status": "unknown",
            "codecs": "",
            "details": f"ffprobe error: {str(error)[:120]}",
        }

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}

    streams = payload.get("streams") or []
    packets = payload.get("packets") or []
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]

    audio_indexes = {
        str(stream.get("index")) for stream in audio_streams
        if stream.get("index") is not None
    }
    video_indexes = {
        str(stream.get("index")) for stream in video_streams
        if stream.get("index") is not None
    }

    audio_packets = [
        packet for packet in packets
        if str(packet.get("stream_index")) in audio_indexes
        and int(packet.get("size") or 0) > 0
    ]
    video_packets = [
        packet for packet in packets
        if str(packet.get("stream_index")) in video_indexes
        and int(packet.get("size") or 0) > 0
    ]

    codec_labels = []
    for stream in audio_streams:
        codec = clean(stream.get("codec_name")) or "unknown-codec"
        channels = stream.get("channels")
        sample_rate = stream.get("sample_rate")
        label = codec
        if channels:
            label += f"/{channels}ch"
        if sample_rate:
            label += f"/{sample_rate}Hz"
        codec_labels.append(label)

    codecs = "; ".join(dict.fromkeys(codec_labels))

    if audio_streams and audio_packets:
        # Presence of packets alone is not enough: try decoding five seconds
        # of the first audio track to catch corrupt/unsupported audio payloads.
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {
                "status": "unknown",
                "codecs": codecs,
                "details": "audio packets found, but ffmpeg decoder is unavailable",
            }

        decode_command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v", "error",
            "-rw_timeout", "10000000",
            "-analyzeduration", "5000000",
            "-probesize", "5000000",
        ]
        if user_agent:
            decode_command.extend(["-user_agent", user_agent])
        if referrer:
            safe_referrer = str(referrer).replace("\r", "").replace("\n", "")
            decode_command.extend(["-headers", f"Referer: {safe_referrer}\r\n"])
        decode_command.extend([
            "-i", url,
            "-map", "0:a:0",
            "-t", "5",
            "-vn", "-sn", "-dn",
            "-f", "null", "-",
        ])

        try:
            decoded = subprocess.run(
                decode_command,
                capture_output=True,
                text=True,
                timeout=AUDIO_PROBE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "unknown",
                "codecs": codecs,
                "details": "audio decoder timeout",
            }
        except OSError as error:
            return {
                "status": "unknown",
                "codecs": codecs,
                "details": f"audio decoder error: {str(error)[:120]}",
            }

        if decoded.returncode == 0:
            return {
                "status": "ok",
                "codecs": codecs,
                "details": "audio packets detected and decoded",
            }

        details = (decoded.stderr or "audio decoder returned an error").strip()
        return {
            "status": "unsupported" if UNSUPPORTED_CODEC_RE.search(details) else "unknown",
            "codecs": codecs,
            "details": f"audio packets found; decoding not confirmed: {details[-180:]}",
        }

    if not audio_streams and video_streams and video_packets and completed.returncode == 0:
        return {
            "status": "no_audio",
            "codecs": "",
            "details": "video packets detected, audio track absent",
        }

    details = (completed.stderr or "audio packets not confirmed").strip()
    return {
        "status": "unknown",
        "codecs": codecs,
        "details": details[-240:],
    }


def probe_audio_with_retry(url, user_agent="", referrer=""):
    result = probe_audio_track(url, user_agent, referrer)
    if result.get("status") == "unknown":
        time.sleep(1)
        retry = probe_audio_track(url, user_agent, referrer)
        if retry.get("status") != "unknown":
            return retry
        if retry.get("details"):
            result["details"] = f"retry: {retry['details']}"
    return result


def probe_video_decode(url, user_agent="", referrer=""):
    """Require a real video stream, packets, and successful FFmpeg decoding."""
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        return {"status": "unknown", "details": "ffmpeg/ffprobe unavailable"}
    headers = ""
    if referrer:
        headers = f"Referer: {str(referrer).replace(chr(13), '').replace(chr(10), '')}\r\n"
    common = ["-rw_timeout", "10000000"]
    if user_agent:
        common += ["-user_agent", user_agent]
    if headers:
        common += ["-headers", headers]
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", *common, "-select_streams", "v:0",
             "-read_intervals", "%+5", "-show_packets",
             "-show_streams", "-show_entries", "stream=codec_name:packet=size", "-of", "json", "-i", url],
            capture_output=True, text=True, timeout=AUDIO_PROBE_TIMEOUT, check=False,
        )
        data = json.loads(probe.stdout or "{}")
        if probe.returncode:
            return {"status": "unknown", "details": "ffprobe could not inspect video packets"}
        if not data.get("streams"):
            return {"status": "no_video", "details": "no video track present"}
        if not any(
            int(packet.get("size") or 0) > 0 for packet in data.get("packets", [])
        ):
            return {"status": "unknown", "details": "video stream or packets not confirmed"}
        decoded = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *common,
             "-i", url, "-map", "0:v:0", "-t", "5", "-an", "-sn", "-dn",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=AUDIO_PROBE_TIMEOUT, check=False,
        )
        decode_status = "ok" if decoded.returncode == 0 else (
            "unsupported" if UNSUPPORTED_CODEC_RE.search(decoded.stderr or "") else "unknown"
        )
        return {"status": decode_status,
                "details": "video packets decoded" if decoded.returncode == 0
                else (decoded.stderr or "video decode failed")[-200:]}
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        return {"status": "unknown", "details": f"video decode unverified: {str(error)[:160]}"}


def probe_video_with_retry(url, user_agent="", referrer=""):
    result = probe_video_decode(url, user_agent, referrer)
    if result.get("status") == "unknown":
        time.sleep(1)
        retry = probe_video_decode(url, user_agent, referrer)
        if retry.get("status") != "unknown":
            return retry
        if retry.get("details"):
            result["details"] = f"retry: {retry['details']}"
    return result


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
            try:
                answer = check_host_request("/check-http?" + query)
            except HTTPError as error:
                if error.code != 429:
                    raise
                answer = {"error": "limit_exceeded"}

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

        time.sleep(4)

        for _attempt in range(12):
            time.sleep(6)

            try:
                result = check_host_request("/check-result/" + request_id)
            except HTTPError as error:
                if error.code != 429:
                    raise
                time.sleep(20)
                continue

            if result and all(value is not None for value in result.values()):
                break
    except Exception as error:
        ru_answers[f"ошибка {type(error).__name__}: {str(error)[:40]}"] += 1
        return "unknown"

    if not result or all(value is None for value in result.values()):
        ru_answers["узлы не ответили"] += 1
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

    with urlopen(request, timeout=SOURCE_TIMEOUT) as response:
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

    current_info = None
    current_group = ""
    seen_urls = set()

    for line in output[1:]:
        if line.startswith("#EXTINF:"):
            if current_info is not None:
                raise ValueError(f"Ошибка формата M3U: нет ссылки: {current_info}")

            if not re.match(r"^#EXTINF:-?\d+(?:\.\d+)?(?:\s|,)", line):
                raise ValueError(f"Ошибка формата M3U: {line}")

            current_info = line
            current_group = ""
            title = line.rsplit(",", 1)[-1].strip()
            if not title:
                raise ValueError("Ошибка формата M3U: пустое название канала")
            group = get_attribute(line, "group-title").strip()
            if not group or group.lower() == "unsorted":
                raise ValueError(f"Ошибка формата M3U: пустая/unsorted группа: {line}")
            if "музык" in group.lower() or group == ADULT_GROUP:
                raise ValueError(f"Запрещённая категория в итоговом плейлисте: {group}")
        elif line.startswith("#EXTGRP:"):
            if current_info is None:
                raise ValueError(f"Ошибка формата M3U: EXTGRP без EXTINF: {line}")
            current_group = line.split(":", 1)[1].strip()
        elif line.upper().startswith("#EXTVLCOPT:"):
            if current_info is None:
                raise ValueError(f"VLC-директива вне записи канала: {line}")
            option = line.split(":", 1)[1].split("=", 1)[0].strip().lower()
            if option not in {"http-user-agent", "http-referrer"}:
                raise ValueError(f"Неподдерживаемая VLC-директива: {line}")
        elif line.startswith("#"):
            continue
        else:
            if current_info is None or not line.startswith(("http://", "https://")):
                raise ValueError(f"Ошибка формата M3U: {line}")
            parsed = urlparse(line)
            if not parsed.hostname or line in seen_urls:
                raise ValueError(f"Пустой/дублирующий URL в M3U: {line}")
            if RADIO_NAME_RE.search(current_info.rsplit(",", 1)[-1]):
                raise ValueError(f"Радиоканал в итоговом плейлисте: {current_info}")
            if MUSIC_NAME_RE.search(current_info.rsplit(",", 1)[-1]):
                raise ValueError(f"Музыкальный канал в итоговом плейлисте: {current_info}")
            seen_urls.add(line)
            if not current_group:
                raise ValueError(f"Ошибка формата M3U: нет #EXTGRP для {current_info}")

            declared_group = get_attribute(current_info, "group-title")
            if declared_group and declared_group != current_group:
                raise ValueError(
                    f"Ошибка формата M3U: group-title ({declared_group}) "
                    f"не совпадает с EXTGRP ({current_group})"
                )

            current_info = None
            current_group = ""

    if current_info is not None:
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
    excluded_rows = []

    def record_exclusion(name, country, language, group, source, url, reason):
        excluded_rows.append({
            "название": clean(name), "страна": clean(country),
            "язык": clean(language), "категория": clean(group),
            "источник": clean(source), "URL": clean(url),
            "результат видео": "не проверялось",
            "результат звука": "не проверялось",
            "аудиокодек": "", "декодирование": "не проверялось",
            "OTTPlayer": "не проверялся", "решение": "исключить",
            "причина": clean(reason),
        })

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
            title, _ = split_title("," + str(stream.get("title") or ""))
            stats["Без channel-id/надёжных метаданных"] += 1
            record_exclusion(title, "", "", "", "iptv-org без channel-id", stream_url,
                             "Нет каталожных метаданных страны или языка")
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
            record_exclusion(channel.get("name"), channel.get("country"), "", "",
                             "iptv-org", stream_url, f"Страна {excluded} исключена")
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
            record_exclusion(channel.get("name"), channel.get("country"),
                             ",".join(feed.get("languages") or []) if feed else "", "",
                             "iptv-org", stream_url, f"Версия канала для {excluded}")
            continue

        approved, reason = allowed_tv_channel(channel, stream, feed)
        if not approved:
            stats[f"Исключено политикой: {reason}"] += 1
            record_exclusion(channel.get("name"), channel.get("country"),
                             ",".join(feed.get("languages") or []) if feed else "",
                             ";".join(channel.get("categories") or []),
                             "iptv-org", stream_url, reason)
            continue

        group = channel_group(channel, stream)
        stats["Разрешённые русские/европейские телеканалы"] += 1

        add_entry(channel, stream, group, stream_url, "iptv-org")
        if entries and entries[-1]["url"] == stream_url:
            entries[-1]["language"] = ",".join(feed.get("languages") or []) if feed else ""

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

        approved, reason = allowed_tv_channel(
            channel,
            {"title": title, "country": get_attribute(info, "tvg-country")},
        )
        if not approved:
            stats[f"Free-TV: {reason}"] += 1
            continue

        stream = stream_from_m3u(info, extras, title, quality)
        group = channel_group(channel, stream)
        add_entry(channel, stream, group, stream_url, "Free-TV")
        if entries and entries[-1]["url"] == stream_url:
            entries[-1]["language"] = get_attribute(info, "tvg-language")

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

            if music_source_group(source_group):
                stats[f"{source}: музыка и радио исключены"] += 1
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

            if not channel:
                stats[f"{source}: нет каталожных метаданных страны/языка"] += 1
                continue

            feed = find_feed(
                tvg_id.split("@")[0], get_attribute(info, "tvg-feed"),
                feeds_by_key, feeds_by_channel,
            )
            approved, reason = allowed_tv_channel(channel, None, feed)
            if not approved:
                stats[f"{source}: {reason}"] += 1
                continue

            stream = stream_from_m3u(info, extras, title, quality)
            group = channel_group(channel, stream)
            add_entry(channel, stream, group, stream_url, source)
            if entries and entries[-1]["url"] == stream_url:
                entries[-1]["language"] = ",".join(feed.get("languages") or []) if feed else ""

    # Manual M3U files must pass through exactly the same media probe as API feeds.
    add_manual_file(INTERNATIONAL_FILE, "Зарубежные на русском", entries, seen_urls, stats)
    add_manual_file(REGIONAL_FILE, "Региональные", entries, seen_urls, stats)
    # Adult sources are intentionally not loaded into the television playlist.

    for item in entries:
        sources[item["source"]] += 1

    discovery_candidates = [item for item in entries if discovery_brand(item["channel"])]

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
    audio_status_counts = Counter()

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

        # Audio verification runs only after a real video/media segment was
        # confirmed. ffprobe reads stream metadata and a short sample of packets.
        audio_targets = [item for item, result in zip(targets, results) if result == "ok"]
        audio_results = []
        if audio_targets:
            print(f"Проверяю звуковые дорожки: {len(audio_targets)} ...")
            with ThreadPoolExecutor(AUDIO_PROBE_WORKERS) as pool:
                audio_results = list(pool.map(
                    lambda item: probe_audio_with_retry(
                        item["url"],
                        clean(item["stream"].get("user_agent")),
                        clean(
                            item["stream"].get("referrer")
                            or item["stream"].get("http_referrer")
                        ),
                    ),
                    audio_targets,
                ))
        audio_status = {
            item["url"]: result
            for item, result in zip(audio_targets, audio_results)
        }
        video_results = []
        if audio_targets:
            print(f"Проверяю видеодорожки и декодирование: {len(audio_targets)} ...")
            with ThreadPoolExecutor(AUDIO_PROBE_WORKERS) as pool:
                video_results = list(pool.map(
                    lambda item: probe_video_with_retry(
                        item["url"], clean(item["stream"].get("user_agent")),
                        clean(item["stream"].get("referrer") or item["stream"].get("http_referrer")),
                    ), audio_targets,
                ))
        video_status = {item["url"]: result for item, result in zip(audio_targets, video_results)}

        audio_counts = Counter(result.get("status", "unknown") for result in audio_results)
        audio_status_counts.update(audio_counts)
        print(
            "Звуковые дорожки: "
            f"подтверждены {audio_counts['ok']}, "
            f"отсутствуют {audio_counts['no_audio']}, "
            f"не удалось подтвердить {audio_counts['unknown']}"
        )

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

            # A check-host HTTP 200 only proves that the URL responds; it does not
            # prove that HLS media segments are playable. Only the segment-aware
            # local probe may mark a stream as publishable.
            audio = audio_status.get(item["url"], {})
            audio_state = audio.get("status", "not_tested")
            video_decode = video_status.get(item["url"], {}).get("status", "not_tested")

            if us == "ok" and video_decode == "no_video":
                status = "no_video"
            elif us == "ok" and audio_state == "no_audio":
                status = "no_audio"
            elif us == "ok" and video_decode != "ok":
                status = "video_unverified"
            elif us == "ok" and audio_state in {"unknown", "unsupported"}:
                # Do not silently delete a working video stream because the
                # audio probe timed out or the container was not understood.
                status = "audio_unverified"
            elif us == "ok":
                status = "ok"
            elif us == "dead" or ru == "dead":
                status = "dead"
            elif ru == "forbidden":
                status = "forbidden"
            else:
                status = "unverified"

            if status not in {"ok", "audio_unverified", "video_unverified"}:
                failed.add(item["url"])

            item["status"] = status
            item["audio_status"] = audio_state
            item["audio_codecs"] = audio.get("codecs", "")
            item["audio_details"] = audio.get("details", "")
            item["video_decode"] = video_decode
            item["video_details"] = video_status.get(item["url"], {}).get("details", "")
            probe[("новые" if new else "уже были", status)] += 1

            if status == "unverified" and item["url"] in previous_urls:
                item["group"] = UNVERIFIED_GROUP

        retain_unverified = {item["url"] for item in targets
                             if item.get("status") == "unverified"
                             and item["url"] in previous_urls}
        entries = [item for item in entries
                   if item["url"] not in failed or item["url"] in retain_unverified]
        seen_urls -= failed - retain_unverified

        # Human-readable audit trail: one row for every tested candidate.
        report_rows = []
        for item, result in zip(targets, results):
            audio = audio_status.get(item["url"], {})
            report_rows.append({
                "channel": clean(item["channel"].get("name")),
                "country": channel_country(item["channel"]),
                "language": clean(item.get("language", "")),
                "category": clean(item["group"]),
                "url": item["url"],
                "source": item["source"],
                "video_probe": result,
                "video_decode": video_status.get(item["url"], {}).get("status", "not_tested"),
                "video_details": video_status.get(item["url"], {}).get("details", ""),
                "audio_status": audio.get("status", "not_tested"),
                "audio_codecs": audio.get("codecs", ""),
                "audio_details": audio.get("details", ""),
                "decode_result": "video and audio decoded" if audio.get("status") == "ok"
                                 and video_status.get(item["url"], {}).get("status") == "ok"
                                 else "unverified",
                "ottplayer_format": "valid" if result == "ok" else "valid structure; stream unverified",
                "decision": "keep" if result == "ok" and video_status.get(item["url"], {}).get("status") in {"ok", "unknown"}
                    and audio.get("status") in {"ok", "unknown", "unsupported"} else "exclude",
                "exclusion_reason": "" if result == "ok" and video_status.get(item["url"], {}).get("status") in {"ok", "unknown"}
                                    and audio.get("status") in {"ok", "unknown", "unsupported"}
                                    else audio.get("details", "") or video_status.get(item["url"], {}).get("details", "")
                                    or f"video probe: {result}; audio: {audio.get('status', 'not_tested')}",
                "published": (
                    "no" if audio.get("status") == "no_audio" or result != "ok"
                    or video_status.get(item["url"], {}).get("status") == "no_video"
                    else "yes" if audio.get("status") == "ok"
                    and video_status.get(item["url"], {}).get("status") == "ok"
                    else "yes - probe incomplete"
                ),
            })
            last_row = report_rows[-1]
            if last_row["decision"] == "exclude":
                record_exclusion(
                    last_row["channel"], last_row["country"], last_row["language"],
                    last_row["category"], last_row["source"], last_row["url"],
                    last_row["exclusion_reason"],
                )
        with CHECK_REPORT.open("w", newline="", encoding="utf-8-sig") as report_file:
            writer = csv.DictWriter(
                report_file,
                fieldnames=["channel", "country", "language", "category", "source", "url",
                            "video_probe", "video_decode", "video_details", "audio_status",
                            "audio_codecs", "audio_details", "decode_result", "ottplayer_format",
                            "decision", "exclusion_reason", "published"],
            )
            writer.writeheader()
            writer.writerows(report_rows)
        print(f"Отчёт проверки сохранён: {CHECK_REPORT} ({len(report_rows)} ссылок)")
    else:
        for item in entries:
            if item["url"] in previous_unverified:
                item["group"] = UNVERIFIED_GROUP
        report_fields = ["channel", "country", "language", "category", "source", "url",
                         "video_probe", "video_decode", "video_details", "audio_status",
                         "audio_codecs", "audio_details", "decode_result", "ottplayer_format",
                         "decision", "exclusion_reason", "published"]
        with CHECK_REPORT.open("w", newline="", encoding="utf-8-sig") as report_file:
            writer = csv.DictWriter(report_file, fieldnames=report_fields)
            writer.writeheader()
            for item in entries:
                writer.writerow({
                    "channel": clean(item["channel"].get("name")),
                    "country": channel_country(item["channel"]),
                    "language": item.get("language", ""),
                    "category": item["group"], "source": item["source"], "url": item["url"],
                    "video_probe": "not_checked", "video_decode": "not_checked",
                    "video_details": "CHECK_NEW_STREAMS=0", "audio_status": "not_checked",
                    "audio_codecs": "", "audio_details": "CHECK_NEW_STREAMS=0",
                    "decode_result": "not_checked", "ottplayer_format": "structure validated; media not checked",
                    "decision": "keep pending stream check",
                    "exclusion_reason": "", "published": "yes - stream not rechecked",
                })
        print(f"Непроверенные потоки записаны в {CHECK_REPORT}: {len(entries)}")

    # Discovery candidates are probed like other streams, but are published
    # only when their source is explicitly marked official.
    discovery_rows = []
    for brand in ("Discovery Channel", "Investigation Discovery (ID)"):
        relevant = [item for item in discovery_candidates if discovery_brand(item["channel"]) == brand]
        usable = [item for item in relevant if item.get("status") == "ok"
                  and item.get("video_decode") == "ok" and item.get("audio_status") == "ok"]
        authorised = [item for item in usable if item.get("source") == "Официальные сайты"]
        discovery_rows.append({
            "канал": brand,
            "тип записи": "итог",
            "результат": "Официальный публичный поток прошёл проверку" if authorised else
                         "Нет подтверждённого официального публичного потока; агрегаторные ссылки не включены",
            "кандидатов": len(relevant),
            "видео/звук подтверждены": len(usable),
            "добавлено": len(authorised),
            "URL": "", "источник": "", "видео": "", "декодирование видео": "",
            "звук": "", "аудиокодек": "", "решение": "",
        })
        for item in relevant:
            discovery_rows.append({
                "канал": brand, "тип записи": "поток",
                "результат": item.get("status", "not_checked"), "кандидатов": "",
                "видео/звук подтверждены": "", "добавлено": "",
                "URL": item["url"], "источник": item["source"],
                "видео": item.get("status", "not_checked"),
                "декодирование видео": item.get("video_decode", "not_checked"),
                "звук": item.get("audio_status", "not_checked"),
                "аудиокодек": item.get("audio_codecs", ""),
                "решение": "добавить" if item in authorised else "не добавлять: источник/проверка не подтверждены",
            })
        for item in usable:
            if item not in authorised:
                record_exclusion(item["channel"].get("name"), channel_country(item["channel"]),
                                 item.get("language", ""), item.get("group", ""), item["source"],
                                 item["url"], "Поток технически доступен, но источник не подтверждён официальным")
    entries = [item for item in entries if not discovery_brand(item["channel"])
               or (item.get("source") == "Официальные сайты" and item.get("status") == "ok"
                   and item.get("video_decode") == "ok" and item.get("audio_status") == "ok")]
    with Path("discovery_status.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["канал", "тип записи", "результат", "кандидатов",
                            "видео/звук подтверждены", "добавлено", "URL", "источник", "видео",
                            "декодирование видео", "звук", "аудиокодек", "решение"])
        writer.writeheader()
        writer.writerows(discovery_rows)

    with EXCLUDED_REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["название", "страна", "язык", "категория", "источник", "URL",
                  "результат видео", "результат звука", "аудиокодек", "декодирование",
                  "OTTPlayer", "решение", "причина"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(excluded_rows)

    epg_names = load_epg_channels()
    epg_matched = 0

    for item in entries:
        for name in channel_names(item["channel"], item["stream"]):
            epg_id = epg_names.get(epg_key(name))

            if epg_id:
                item["epg_id"] = epg_id
                epg_matched += 1
                break

    # Дубли одного канала в одном качестве внутри одной группы: оставляем
    # одну ссылку, предпочитая проверенную (работает из США или из России),
    # затем ссылку, которая уже была в плейлисте, затем остальные.
    def dedupe_name(item):
        name = clean(item["channel"].get("name")).lower()
        name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
        name = re.sub(r"\b(full\s*hd|fhd|uhd|4k|hd|sd)\b", " ", name)
        return re.sub(r"[^\w]+", "", name)

    def dedupe_rank(item):
        status = item.get("status", "")

        if status in ("ok", "ok_ru"):
            return 0

        return 1 if not status else 2

    deduped = {}
    quality_removed = 0

    for item in entries:
        key = (
            item["group"],
            dedupe_name(item),
            quality_label(item["quality"]),
        )

        if not key[1]:
            deduped[object()] = item
            continue

        current = deduped.get(key)

        if current is None:
            deduped[key] = item
        else:
            quality_removed += 1

            if dedupe_rank(item) < dedupe_rank(current):
                deduped[key] = item

    entries = list(deduped.values())
    print(f"Удалено дублей одного качества: {quality_removed}")

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
        if item.get("manual_info"):
            output.append(item["manual_info"])
        else:
            output.append(
                make_extinf(
                    item["channel"],
                    item["stream"],
                    item["group"],
                    item.get("epg_id", ""),
                )
            )

        # OTTPlayer's published M3U example uses EXTGRP. Put it immediately
        # after EXTINF; keep group-title too for other IPTV players.
        output.append(f'#EXTGRP:{clean(item["group"])}')

        if item.get("manual_info"):
            # Preserve source options, but replace any old EXTGRP with our
            # normalized category above.
            output.extend(
                line for line in item.get("manual_extras", [])
                if line.lower().startswith((
                    "#extvlcopt:http-user-agent=", "#extvlcopt:http-referrer=",
                ))
            )
        else:
            add_headers(output, item["stream"])

        output.append(item["url"])
        counts[item["group"]] += 1

    if len(entries) < MIN_KEEP_COUNT:
        raise RuntimeError(
            "Не осталось ни одного разрешённого канала. "
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

    russian_channels = {
        catalog_id(item["channel"]) or clean(item["channel"].get("name"))
        for item in entries
        if channel_country(item["channel"]) == "RU"
        or language_is_russian(item.get("language", ""))
    }
    european_channels = {
        catalog_id(item["channel"]) or clean(item["channel"].get("name"))
        for item in entries
        if channel_country(item["channel"]) in EUROPEAN_COUNTRIES
        and channel_country(item["channel"]) != "RU"
    }
    confirmed_av = sum(1 for item in entries if item.get("status") == "ok")
    audio_problems = (audio_status_counts["no_audio"] + audio_status_counts["unknown"]
                      + audio_status_counts["unsupported"])
    unverified_count = sum(count for (_kind, status), count in probe.items()
                           if status in {"audio_unverified", "video_unverified", "unverified"})
    print(f"Российские/русскоязычные каналы: {len(russian_channels)}")
    print(f"Европейские каналы (по стране): {len(european_channels)}")
    if CHECK_NEW_STREAMS:
        print(f"Видео и звук подтверждены: {confirmed_av}; проблемы аудио: {audio_problems}; не удалось полностью проверить: {unverified_count}")
    else:
        print("Видео/аудио проверки отключены; результаты не подтверждены")

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
            "ok": "видео и аудиодорожка подтверждены",
            "audio_unverified": "видео работает, звук не удалось подтвердить",
            "no_audio": "видеопоток без аудиодорожки — исключён",
            "ok_ru": "HTTP доступен из России (сегмент не подтверждён)",
            "unverified": "не подтверждено сегментами, не опубликован",
            "blocked": "не подтверждено, не опубликован",
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

        working = {"ok", "audio_unverified"}
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
