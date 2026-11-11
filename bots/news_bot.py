#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram News Bot - Автоматические публикации новостей
Источники: InfoBrics, Global Research, RT, ZeroHedge
"""

import os
import json
import logging
import asyncio
import hashlib
import re
import html as html_module
import random
from io import BytesIO
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('news_bot')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

MIN_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', os.getenv('MIN_INTERVAL', '2100')))
MAX_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', os.getenv('MAX_INTERVAL', '7200')))
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '24'))
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '7'))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))

if MIN_INTERVAL <= 0:
    MIN_INTERVAL = 60
if MAX_INTERVAL <= MIN_INTERVAL:
    MAX_INTERVAL = MIN_INTERVAL + 60

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'
MAX_CAPTION = 1024
MAX_MESSAGE = 4096
MAX_TRANSLATE_INPUT = 1500
TRANSLATE_CHUNK_SIZE = 450
FALLBACK_IMAGE_URL = os.getenv(
    'FALLBACK_IMAGE_URL',
    'https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Newspaper_icon.png/240px-Newspaper_icon.png'
)
IS_MANUAL_RUN = os.getenv('TEST_MODE', '').lower() == 'true'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ========== ИСКЛЮЧАЕМЫЕ АВТОРЫ ==========
EXCLUDED_AUTHORS = [
    'Уриэль Араухо', 'Uriel Araujo',
    'Ахмед Адель', 'Ahmed Adel',
    'Лукас Лейроз', 'Lucas Leiros',
    'Одри Чайлд', 'Audrey Child',
    'Андрей Корыбко', 'Andrei Korybko',
    'Стив Уотсон', 'Steve Watson',
]

JUNK_PHRASES = [
    'artigo em inglês', 'artigo em ingles',
    'membro da associação', 'membro da associacao',
    'comentário à', 'comentario a',
    'comentário a', 'em nossa página',
    'em nossa pagina', 'pesquisador do centro',
    'especialista militar', 'associado ao centro',
    'pesquisa sobre a globalização',
    'pesquisa sobre a globalizacao',
    'artigo em português', 'artigo em portugues',
    'en español', 'en espanol',
    'leer más', 'leer mas', 'read in english',
    'this article was originally published',
    'комментируйте статьи о глобальных исследованиях',
    'комментарий к статьям глобальных исследований',
    'comment on global research articles',
    'comment on this article',
    'on our facebook page',
    'на нашей странице в facebook',
    'follow us on',
    'подписывайтесь на',
    'поделиться статьёй',
    'share this article',
    'read more articles',
    'читайте больше статей',
    'станьте участником',
    'стать участником',
    'global researchfree books',
    'global research free books',
    'global research online books',
    'global research books',
    'become a member',
    'become a supporter',
    'support global research',
    'поддержите глобальные исследования',
    'поддержите global research',
    'our online e-book project',
    'our e-books are our authors',
    'наши электронные книги',
    'electronic books',
    'gift to global research',
    'подарок для global research',
    'global research e-books',
    'global research ebook',
]

NON_ENGLISH_MARKERS = [
    ' sono ', ' della ', ' degli ', ' delle ', ' nella ', ' nelle ', ' questo ', ' questa ',
    ' questi ', ' queste ', ' perché ', ' perche ', ' dopo ', ' fatto ', ' stato ', ' stata ',
    ' Finalmente ', ' parece ', ' país ', ' regione ', ' programma ', ' asiatico ',
    ' também ', ' tambem ', ' porque ', ' onde ', ' quando ', ' enquanto ',
    ' está ', ' esta ', ' são ', ' sao ', ' têm ', ' tem ',
    ' según ', ' segun ', ' donde ', ' cuando ', ' mientras ', ' hacia ', ' desde ', ' hasta ',
]

FOREIGN_INLINE_WORDS = [
    'trabalhadores', 'setores', 'críticos', 'criticos',
    'não', 'nao', 'será', 'sera', 'para', 'como', 'também', 'tambem',
    'está', 'esta', 'são', 'sao', 'muito', 'mais', 'ainda', 'desde',
    'estrangeiro', 'estrangeiros', 'governo', 'presidente',
    'política', 'politica', 'economia', 'militar',
    'trabajadores', 'sectores', 'críticos', 'criticos',
    'gobierno', 'presidente', 'política', 'politica',
]

IDIOM_FIXES = {
    'забивает мяч в': 'одерживает верх в',
    'забивает гол в': 'одерживает верх в',
    'забить мяч в': 'одержать верх в',
    'забить гол в': 'одержать верх в',
    'UE забивает мяч': 'ЕС одерживает верх',
    'UE fa gol': 'ЕС одерживает верх',
    'es gibt ein Tor': 'одерживает верх',
    'как sabedoria, как учёба': 'как мудрость, так и учёба',
    'kak sabedoria': 'как мудрость',
    'выиграл мяч': 'одержал победу',
    'выиграла мяч': 'одержала победу',
    'мяч в ворота': 'победа',
    'европейская сказка': 'европейская мечта',
    'европеизм кажется': 'европеизм, по-видимому,',
    'европеизм parece': 'европеизм, по-видимому,',
    'parece ter Finalmente prevalecido': 'по-видимому, одержал верх',
    'parece ter finalmente prevalecido': 'по-видимому, одержал верх',
}

FOREIGN_TITLE_WORDS = [
    ' conflito ', ' será ', ' sera ', ' decidido ',
    ' campo ', ' batalha ', ' ucrânia ', ' ucrania ',
    ' na ', ' da ', ' do ', ' dos ', ' das ',
    ' governo ', ' presidente ', ' ministro ',
    ' sobre ', ' com ', ' mais ', ' não ', ' nao ',
    ' também ', ' tambem ', ' está ', ' esta ',
    ' según ', ' segun ', ' tras ', ' desde ',
    ' democracia ', ' declínio ', ' declinio ',
    ' aponta ', ' relatório ', ' relatorio ',
    ' trabalhadores ', ' setores ', ' críticos ',
]

POST_TRANSLATION_FIXES = {
    'Spanish': 'испанское', 'extradition': 'экстрадиция',
    'communist': 'коммунистический', 'centimillionaire': 'мультимиллионер',
    'reported': 'сообщил', 'detained': 'задержан', 'arrest': 'арест',
    'Ibiza': 'Ибица', 'wanted': 'разыскивается',
    'money laundering': 'отмывание денег', 'riot': 'бунт',
    'conspiracy': 'сговор', 'charges': 'обвинения',
    'demonstrations': 'демонстрации', 'transfers': 'переводы',
    'company': 'компания', 'Tunisia': 'Тунис', 'previously': 'ранее',
    'lived': 'проживал', 'spokeswoman': 'представитель',
    'confirmed': 'подтвердил', 'outlet': 'издание', 'courts': 'суды',
    'review': 'рассмотрение', 'request': 'запрос', 'judges': 'судьи',
    'approve': 'одобрят', 'final': 'окончательное', 'decision': 'решение',
    'returns': 'возвращается', 'Prime Minister': 'премьер-министру',
    'Cabinet': 'кабинет', 'Traders': 'трейдеры', 'modest': 'скромные',
    'hopes': 'надежды', 'deal': 'сделку', 'While': 'Хотя',
    'provide': 'предоставим', 'detailed': 'подробный', 'preview': 'обзор',
    'summit': 'саммит', 'subsequent': 'последующем', 'post': 'посте',
    'signs': 'признаки', 'President': 'президент', 'Chinese': 'китайский',
    'counterpart': 'коллега', 'emerge': 'выйти', 'upcoming': 'предстоящего',
    'agreement': 'соглашение', 'artificial': 'искусственного',
    'intelligence': 'интеллекта', 'industry': 'отрасли',
    'increasingly': 'всё более', 'prominent': 'заметной',
    'flash point': 'точкой напряжённости', 'between': 'между',
    'world': 'мира', 'biggest': 'крупнейшими', 'economies': 'экономиками',
    'fund managers': 'управляющие фондами', 'say': 'говорят',
    'unlikely': 'маловероятно', 'significant': 'значительным',
    'enough': 'достаточно', 'sustainable': 'устойчивый', 'boost': 'рост',
    'related': 'связанных', 'stocks': 'акций', 'Over': 'В',
    'weekend': 'выходные', 'Treasury Secretary': 'министр финансов',
    'after': 'после', 'hours': 'часов', 'negotiations': 'переговоров',
    'Vice Premier': 'вице-премьером', 'said': 'сказал',
    'two sides': 'две стороны', 'agreed': 'согласились',
    'create': 'создать', 'called': 'назвал', 'dialogue': 'диалог',
    'technology': 'технологии', 'benefits': 'выгоды', 'threats': 'угрозы',
}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_local_time():
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

def unescape_html(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def balance_brackets(text: str) -> str:
    if not text:
        return text
    if text.count('(') > text.count(')'):
        last_open = text.rfind('(')
        if last_open != -1:
            text = text[:last_open].rstrip()
    if text.count('«') > text.count('»'):
        last_open = text.rfind('«')
        if last_open != -1:
            text = text[:last_open].rstrip()
    if text.count('[') > text.count(']'):
        last_open = text.rfind('[')
        if last_open != -1:
            text = text[:last_open].rstrip()
    return text.strip()

def remove_inline_foreign(text: str) -> str:
    if not text:
        return text
    result = text
    for word in FOREIGN_INLINE_WORDS:
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(word) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s+', ' ', result)
    result = re.sub(r'\s+([,.;:!?])', r'\1', result)
    result = re.sub(r'([,.;:!?])\s+', r'\1 ', result)
    return result.strip()

def remove_repeated_start(text: str) -> str:
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) < 2:
        return text
    first = sentences[0].strip()
    if len(first) < 15:
        return text
    key = first[:40].lower().strip()
    for i in range(1, len(sentences)):
        if sentences[i].lower().strip().startswith(key):
            logger.info(f"⏭️ Отсечён повтор с {i+1}-го предложения")
            return ' '.join(sentences[:i]).strip()
    return text

def is_non_english_body(text: str) -> bool:
    if not text:
        return False
    text_lower = ' ' + text.lower() + ' '
    hits = sum(1 for marker in NON_ENGLISH_MARKERS if marker in text_lower)
    if hits >= 3:
        return True
    return False

def cut_non_english_tail(text: str) -> str:
    if not text:
        return text
    paragraphs = text.split('\n\n')
    clean = []
    for p in paragraphs:
        p_stripped = p.strip()
        if not p_stripped:
            continue
        cyr = len(re.findall(r'[а-яА-ЯёЁ]', p_stripped))
        total = len(re.findall(r'\w', p_stripped))
        cyr_ratio = cyr / total if total > 0 else 0
        if cyr_ratio < 0.5 and is_non_english_body(p_stripped):
            logger.info(f"⏭️ Отсечён иностранный хвост: {p_stripped[:60]}...")
            break
        long_latin = re.findall(r'\b[a-zA-Z]{12,}\b', p_stripped)
        if cyr_ratio < 0.3 and len(long_latin) >= 2:
            logger.info(f"⏭️ Отсечён хвост с длинными латинскими словами: {p_stripped[:60]}...")
            break
        clean.append(p_stripped)
    return '\n\n'.join(clean).strip()

def remove_junk_paragraphs(text: str) -> str:
    if not text:
        return text
    paragraphs = text.split('\n\n')
    clean = []
    for p in paragraphs:
        p_lower = p.lower()
        if any(junk in p_lower for junk in JUNK_PHRASES):
            logger.info(f"⏭️ Удалён мусорный абзац: {p[:60]}...")
            continue
        clean.append(p)
    return '\n\n'.join(clean).strip()

def apply_idiom_fixes(text: str) -> str:
    if not text:
        return text
    result = text
    multiword = {k: v for k, v in IDIOM_FIXES.items() if ' ' in k}
    for src, dst in sorted(multiword.items(), key=lambda x: -len(x[0])):
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(src) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, dst, result, flags=re.IGNORECASE)
    single = {k: v for k, v in IDIOM_FIXES.items() if ' ' not in k}
    for src, dst in single.items():
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(src) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, dst, result, flags=re.IGNORECASE)
    return result

def postprocess_translation(text: str) -> str:
    if not text:
        return text
    result = text
    result = remove_inline_foreign(result)
    result = apply_idiom_fixes(result)
    multiword = {k: v for k, v in POST_TRANSLATION_FIXES.items() if ' ' in k}
    for eng, rus in sorted(multiword.items(), key=lambda x: -len(x[0])):
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(eng) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, rus, result, flags=re.IGNORECASE)
    single = {k: v for k, v in POST_TRANSLATION_FIXES.items() if ' ' not in k}
    for eng, rus in single.items():
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(eng) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, rus, result, flags=re.IGNORECASE)
    result = remove_repeated_start(result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result

def truncate_at_sentence(text: str, max_len: int) -> str:
    """Обрезает по границе предложения. Если не удалось — по границе слова с многоточием."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut_pos = -1
    for punct in ['.', '!', '?']:
        for m in re.finditer(re.escape(punct) + r'(?=\s|$)', text[:max_len + 1]):
            if m.end() > cut_pos:
                cut_pos = m.end()
    if cut_pos != -1:
        return text[:cut_pos].strip()
    for punct in ['.', '!', '?']:
        m = re.search(re.escape(punct) + r'(?=\s|$)', text[max_len:])
        if m:
            end_pos = max_len + m.end()
            if end_pos <= max_len * 2:
                return text[:end_pos].strip()
            break
    cut = text[:max_len]
    last_space = cut.rfind(' ')
    if last_space > max_len * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(' ,.;:') + '…'

def fetch_url(url: str, timeout: int = None):
    if timeout is None:
        timeout = REQUEST_TIMEOUT
    try:
        headers = {'User-Agent': UA}
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None

def is_bad_image_url(url: str) -> bool:
    if not url:
        return True
    url_lower = url.lower()
    bad_markers = [
        'logo', 'icon', 'avatar', 'placeholder', 'default',
        'no-image', 'noimage', 'blank', 'spacer', 'pixel',
        '1x1', 'transparent', 'sprite',
    ]
    for marker in bad_markers:
        if marker in url_lower:
            return True
    if re.search(r'/(logo|icon|avatar|banner)[-_./]', url_lower):
        return True
    if re.search(r'/flag\.(jpg|jpeg|png|webp|gif|svg)', url_lower):
        return True
    if re.search(r'/flags?/', url_lower):
        return True
    return False

def _detect_image_format(content: bytes) -> str:
    """Возвращает 'jpeg'|'png'|'gif'|'webp'|'avif'|'bmp'|'unknown' по magic bytes."""
    if not content or len(content) < 12:
        return 'unknown'
    if content[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if content[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if content[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'webp'
    if content[4:8] == b'ftyp' and content[8:12] in (b'avif', b'avis', b'heic', b'heix', b'mif1'):
        return 'avif'
    if content[:2] == b'BM':
        return 'bmp'
    return 'unknown'

def _is_image_content(content: bytes, ct: str = '') -> bool:
    """Проверяет, что это реально картинка — по Content-Type И magic bytes."""
    if not content or len(content) < 100:
        return False
    if _detect_image_format(content) != 'unknown':
        return True
    if ct and ct.lower().startswith('image/'):
        return True
    return False

def _normalize_to_jpeg(content: bytes):
    """
    Конвертирует любое изображение (WebP, PNG, GIF, AVIF-если поддерживается) в JPEG.
    Telegram sendPhoto надёжно принимает только JPEG/PNG/GIF; AVIF/WebP часто
    приводят к 400 Image_process_failed.
    """
    if not content:
        return None
    fmt = _detect_image_format(content)
    if fmt == 'jpeg':
        return content
    if fmt == 'png' or fmt == 'gif':
        # Telegram их принимает нативно, не трогаем
        return content
    if not _HAS_PIL:
        logger.warning("PIL не установлен — не могу конвертировать %s в JPEG", fmt)
        return None
    try:
        img = Image.open(BytesIO(content))
        if img.format and img.format.upper() in ('JPEG', 'JPG'):
            return content
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            background = Image.new('RGB', img.size, (255, 255, 255))
            try:
                background.paste(img, mask=img.convert('RGBA').split()[-1])
            except Exception:
                background.paste(img)
            img = background
        else:
            img = img.convert('RGB')
        if img.width > 1920:
            img.thumbnail((1920, 1920))
        out = BytesIO()
        img.save(out, format='JPEG', quality=85, optimize=True)
        converted = out.getvalue()
        logger.info(f"  🔄 {fmt} → JPEG ({len(content)}b → {len(converted)}b)")
        return converted
    except Exception as e:
        logger.warning(f"PIL convert failed ({fmt}): {e}")
        return None

def _fetch_via_proxy(image_url: str, force_jpeg: bool = True):
    """Пробует получить картинку через бесплатные image-прокси (обход Cloudflare/hotlink/AVIF)."""
    if not image_url:
        return None
    quoted = requests.utils.quote(image_url, safe='')
    if force_jpeg:
        templates = (
            f"https://wsrv.nl/?url={quoted}&w=1280&output=jpg&q=88",
            f"https://images.weserv.nl/?url={quoted}&w=1280&output=jpg&q=88",
            f"https://wsrv.nl/?url={quoted}&w=1200",
            f"https://images.weserv.nl/?url={quoted}&w=1200",
        )
    else:
        templates = (
            f"https://wsrv.nl/?url={quoted}&w=1200",
            f"https://images.weserv.nl/?url={quoted}&w=1200",
        )
    for proxy_url in templates:
        try:
            r = requests.get(proxy_url, headers={'User-Agent': UA},
                             timeout=25, allow_redirects=True)
            ct = r.headers.get('Content-Type', '')
            if r.status_code == 200 and _is_image_content(r.content, ct):
                if force_jpeg:
                    jpeg = _normalize_to_jpeg(r.content)
                    if jpeg:
                        return jpeg
                else:
                    return r.content
        except Exception:
            continue
    return None

def extract_image_url_for_infobrics(soup, base_url: str):
    article = soup.find('article')
    if not article:
        article = soup.find('div', class_=re.compile(r'(article|post|news)', re.IGNORECASE))
    candidates = []
    if article:
        for img in article.find_all('img', src=True):
            candidates.append(img.get('src', ''))
        for img in article.find_all('img', attrs={'data-src': True}):
            candidates.append(img.get('data-src', ''))
        for source in article.find_all('source', attrs={'srcset': True}):
            srcset = source.get('srcset', '')
            first = srcset.split(',')[0].strip().split(' ')[0] if srcset else ''
            if first:
                candidates.append(first)
    if not candidates:
        for img in soup.find_all('img', src=True):
            candidates.append(img.get('src', ''))
        for img in soup.find_all('img', attrs={'data-src': True}):
            candidates.append(img.get('data-src', ''))
        for source in soup.find_all('source', attrs={'srcset': True}):
            srcset = source.get('srcset', '')
            first = srcset.split(',')[0].strip().split(' ')[0] if srcset else ''
            if first:
                candidates.append(first)
    for raw in candidates:
        if not raw:
            continue
        url = raw.strip()
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = urljoin(base_url, url)
        elif not url.startswith('http'):
            continue
        if re.search(r'(icon|logo|avatar|flag|sprite|placeholder)', url, re.IGNORECASE):
            continue
        if is_bad_image_url(url):
            continue
        if not re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', url, re.IGNORECASE):
            continue
        return url
    return None

def collect_image_candidates_generic(soup, base_url: str) -> list:
    """Собирает все возможные URL картинок для универсального поиска."""
    candidates = []
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        candidates.append(meta_img['content'])
    meta_img_secure = soup.find('meta', property='og:image:secure_url')
    if meta_img_secure and meta_img_secure.get('content'):
        candidates.append(meta_img_secure['content'])
    meta_twitter = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta_twitter and meta_twitter.get('content'):
        candidates.append(meta_twitter['content'])
    article = soup.find('article')
    if article:
        for img in article.find_all('img', src=True):
            candidates.append(img.get('src', ''))
        for img in article.find_all('img', attrs={'data-src': True}):
            candidates.append(img.get('data-src', ''))
        for source in article.find_all('source', attrs={'srcset': True}):
            srcset = source.get('srcset', '')
            first = srcset.split(',')[0].strip().split(' ')[0] if srcset else ''
            if first:
                candidates.append(first)
    for img in soup.find_all('img', src=True):
        candidates.append(img.get('src', ''))
    for img in soup.find_all('img', attrs={'data-src': True}):
        candidates.append(img.get('data-src', ''))
    normalized = []
    for raw in candidates:
        if not raw:
            continue
        url = raw.strip()
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = urljoin(base_url, url)
        elif not url.startswith('http'):
            continue
        if is_bad_image_url(url):
            continue
        if not re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', url, re.IGNORECASE):
            continue
        if url not in normalized:
            normalized.append(url)
    return normalized

def extract_image_url(soup, base_url: str, source_name: str = '') -> str:
    if source_name == 'InfoBrics':
        return extract_image_url_for_infobrics(soup, base_url)
    candidates = collect_image_candidates_generic(soup, base_url)
    return candidates[0] if candidates else None

def fetch_zerohedge_image(image_url: str):
    """
    Улучшенная загрузка картинки ZeroHedge:
    - Accept только jpeg/png → Cloudflare не отдаёт AVIF (у него format=auto)
    - Pillow-нормализация → если всё же WebP/AVIF, конвертируем в JPEG
    - Прокси с output=jpg → гарантированно JPEG
    """
    if not image_url:
        return None
    base_headers = {
        'User-Agent': UA,
        'Referer': 'https://www.zerohedge.com/',
        'Origin': 'https://www.zerohedge.com',
        # Ключевое: НЕ просим avif/webp, иначе Cloudflare отдаст AVIF,
        # который Telegram не умеет обрабатывать (400 Image_process_failed).
        'Accept': 'image/jpeg,image/png,*/*;q=0.5',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Dest': 'image',
        'Sec-Fetch-Mode': 'no-cors',
        'Sec-Fetch-Site': 'same-site',
    }
    # 1) Прямая загрузка
    try:
        r = requests.get(image_url, headers=base_headers, timeout=20, allow_redirects=True)
        ct = r.headers.get('Content-Type', '')
        fmt = _detect_image_format(r.content)
        if r.status_code == 200 and _is_image_content(r.content, ct):
            jpeg = _normalize_to_jpeg(r.content)
            if jpeg:
                logger.info(f"ZH ✓ direct {fmt}→{_detect_image_format(jpeg)}: "
                            f"{image_url[:60]} ({len(jpeg)}b)")
                return jpeg
            logger.info(f"ZH ✗ direct: не смог нормализовать {fmt} → JPEG")
        else:
            logger.info(f"ZH ✗ direct: {image_url[:60]} → {r.status_code} {ct[:30]} {fmt}")
    except Exception as e:
        logger.info(f"ZH ✗ direct exc: {image_url[:60]} → {e}")

    # 2) Прокси с output=jpg (конвертирует AVIF/WebP/что угодно в JPEG)
    proxied = _fetch_via_proxy(image_url, force_jpeg=True)
    if proxied:
        logger.info(f"ZH ✓ proxy JPEG: {image_url[:60]} ({len(proxied)}b)")
        return proxied

    logger.info(f"ZH ✗ all failed: {image_url[:60]}")
    return None

def fetch_image_with_fallback(image_url: str, referer: str = None, source: str = None):
    """Стандартная загрузка картинки с нормализацией в JPEG и прокси-фолбэком."""
    if image_url:
        try:
            headers = {
                'User-Agent': UA,
                'Accept': 'image/jpeg,image/png,*/*;q=0.5',
            }
            if referer:
                headers['Referer'] = referer
            r = requests.get(image_url, headers=headers, timeout=15, allow_redirects=True)
            ct = r.headers.get('Content-Type', '')
            if r.status_code == 200 and _is_image_content(r.content, ct):
                jpeg = _normalize_to_jpeg(r.content)
                if jpeg:
                    return jpeg
        except Exception as e:
            logger.warning(f"Не удалось загрузить картинку {image_url[:60]}: {e}")
        proxied = _fetch_via_proxy(image_url, force_jpeg=True)
        if proxied:
            logger.info(f"🖼️ Картинка получена через прокси: {image_url[:60]}")
            return proxied
    return fetch_fallback_image()

def fetch_fallback_image():
    if not FALLBACK_IMAGE_URL:
        return None
    try:
        r = requests.get(FALLBACK_IMAGE_URL, headers={'User-Agent': UA}, timeout=15)
        ct = r.headers.get('Content-Type', '')
        if r.status_code == 200 and _is_image_content(r.content, ct):
            logger.info("🖼️ Использована заглушка для картинки")
            return _normalize_to_jpeg(r.content) or r.content
    except Exception as e:
        logger.error(f"Не удалось загрузить заглушку: {e}")
    return None

def clean_title(title: str):
    if not title:
        return ""
    title = re.sub(r'^#+\s*', '', title)
    title = re.sub(r'^[📰📝📌🔹🔸⭐️✨]\s*', '', title)
    title = re.sub(r'[„“”"\'`]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    if re.search(r'(популярн|popular|most popular|top|trending|daily|roundup|summary|recap)', title, re.IGNORECASE):
        return ""
    return title.strip()

def is_foreign_title(title: str) -> bool:
    if not title:
        return False
    title_lower = ' ' + title.lower() + ' '
    hits = sum(1 for w in FOREIGN_TITLE_WORDS if w in title_lower)
    if hits >= 3:
        return True
    en_words = re.findall(r'\b[a-zA-Z]{4,}\b', title)
    if hits >= 2 and len(en_words) <= 1:
        return True
    return False

def is_excluded_author(text: str):
    if not text:
        return False
    for name in EXCLUDED_AUTHORS:
        if name in text:
            return True
    return False

def contains_link(text: str):
    if not text:
        return False
    if re.search(r'https?://', text):
        return True
    if re.search(r't\.co/|t\.me/|bit\.ly|goo\.gl|tinyurl', text, re.IGNORECASE):
        return True
    if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text):
        return True
    if re.search(r'\b[a-zA-Z0-9-]+\.(com|org|net|ru|io|co|uk|de|fr|info|biz|tv|me|es|it)\b', text):
        return True
    return False

def is_foreign_text(text: str) -> bool:
    if not text:
        return False
    foreign_chars = re.findall(r'[áéíóúñü¿¡àèìòùâêîôûäëïöüçßœæ]', text, re.IGNORECASE)
    if len(foreign_chars) > 3:
        return True
    foreign_words = re.findall(r'\b(el|la|los|las|de|del|en|con|por|para|una|uno|este|esta|como|pero|más|sin|sobre|entre|cuando|donde|qué|quién|también|desde|hasta|hacia|según|tras|durante|contra|mediante)\b', text, re.IGNORECASE)
    if len(foreign_words) > 5:
        return True
    return False

def is_bad_title(title: str) -> bool:
    if not title:
        return True
    allowed_en = {
        'EU', 'US', 'UN', 'NATO', 'BRICS', 'AI', 'IT', 'GDP', 'CEO', 'USA', 'UK',
        'CIA', 'FBI', 'NASA', 'WHO', 'IMF', 'OPEC',
        'Biden', 'Trump', 'Putin', 'Zelensky', 'Netanyahu', 'Merkel', 'Macron',
        'Gates', 'Musk', 'Xi', 'Kim', 'Erdogan', 'Khamenei', 'Sanchez',
        'Iran', 'Iraq', 'Syria', 'Israel', 'Palestine', 'Hamas', 'Hezbollah',
        'Telegram', 'Google', 'Microsoft', 'Apple', 'Meta', 'Facebook', 'Twitter',
    }
    en_words = re.findall(r'\b[a-zA-Z]{3,}\b', title)
    en_words = [w for w in en_words if w not in allowed_en and w.upper() != w]
    if len(en_words) >= 2:
        return True
    if len(en_words) == 1 and en_words[0].lower() not in POST_TRANSLATION_FIXES:
        return True
    return False

# ========== ПЕРЕВОДЧИКИ ==========
def translate_google(text: str) -> str:
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {'client': 'gtx', 'sl': 'en', 'tl': 'ru', 'dt': 't', 'q': text}
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                result = ''.join(item[0] for item in data[0] if item and len(item) > 0)
                if result and re.search('[а-яА-Я]', result):
                    return result
    except Exception as e:
        logger.warning(f"Google ошибка: {e}")
    return None

def translate_mymemory(text: str) -> str:
    if len(text) > 500:
        return None
    try:
        url = "https://api.mymemory.translated.net/get"
        params = {'q': text, 'langpair': 'en|ru', 'de': 'a1b2c3d4e5f6g7h8@example.com'}
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data and 'responseData' in data and 'translatedText' in data['responseData']:
                result = data['responseData']['translatedText']
                if result and re.search('[а-яА-Я]', result):
                    return result
    except Exception as e:
        logger.warning(f"MyMemory ошибка: {e}")
    return None

def translate_lingva(text: str) -> str:
    try:
        url = f"https://lingva.ml/api/v1/en/ru/{requests.utils.quote(text)}"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data and 'translation' in data:
                result = data['translation']
                if result and re.search('[а-яА-Я]', result):
                    return result
    except Exception as e:
        logger.warning(f"Lingva ошибка: {e}")
    return None

def translate_chunk(chunk: str) -> tuple:
    result = translate_google(chunk)
    if result:
        return (result, 'Google')
    result = translate_mymemory(chunk)
    if result:
        return (result, 'MyMemory')
    result = translate_lingva(chunk)
    if result:
        return (result, 'Lingva')
    return (chunk, None)

def split_into_chunks(text: str, max_chunk: int = TRANSLATE_CHUNK_SIZE) -> list:
    if not text:
        return []
    if len(text) <= max_chunk:
        return [text]
    chunks = []
    current = ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_chunk:
            words = sent.split(' ')
            for w in words:
                if len(current) + len(w) + 1 <= max_chunk:
                    current = (current + ' ' + w).strip()
                else:
                    if current:
                        chunks.append(current)
                    current = w
        else:
            if len(current) + len(sent) + 1 <= max_chunk:
                current = (current + ' ' + sent).strip() if current else sent
            else:
                if current:
                    chunks.append(current)
                current = sent
    if current:
        chunks.append(current)
    return chunks

def translate_text(text: str) -> str:
    if not text or len(text) < 3:
        return text
    if re.search('[а-яА-Я]', text):
        return text
    chunks = split_into_chunks(text, max_chunk=TRANSLATE_CHUNK_SIZE)
    logger.info(f"  🔪 Разбито на {len(chunks)} кусков")
    translated_parts = []
    method_stats = {}
    for i, chunk in enumerate(chunks):
        result, method = translate_chunk(chunk)
        if method:
            method_stats[method] = method_stats.get(method, 0) + 1
            translated_parts.append(result)
        else:
            logger.warning(f"  ⚠️ Кусок {i+1}/{len(chunks)} не переведён ({len(chunk)} символов)")
            translated_parts.append(chunk)
    if method_stats:
        stats_str = ', '.join(f"{k}: {v}" for k, v in method_stats.items())
        logger.info(f"  ✅ Методы перевода: {stats_str}")
    return ' '.join(translated_parts)

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)

    def _load_state(self) -> dict:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return {
                        'sent_links': set(data.get('sent_links', [])),
                        'sent_hashes': set(data.get('sent_hashes', [])),
                        'sent_titles': set(data.get('sent_titles', [])),
                        'posts_log': data.get('posts_log', [])
                    }
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")
        return {'sent_links': set(), 'sent_hashes': set(), 'sent_titles': set(), 'posts_log': []}

    def _save_state(self):
        try:
            data = {
                'sent_links': list(self.state['sent_links']),
                'sent_hashes': list(self.state['sent_hashes']),
                'sent_titles': list(self.state['sent_titles']),
                'posts_log': self.state['posts_log']
            }
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния: {e}")

    def _load_meta(self) -> dict:
        try:
            if os.path.exists(META_FILE):
                with open(META_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки мета: {e}")
        return {'posts': {}}

    def _save_meta(self):
        try:
            cutoff = get_local_time() - timedelta(days=30)
            cleaned = {}
            for pid, data in self.meta.get('posts', {}).items():
                try:
                    if datetime.fromisoformat(data.get('time', '')) > cutoff:
                        cleaned[pid] = data
                except Exception:
                    cleaned[pid] = data
            self.meta['posts'] = cleaned
            with open(META_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения мета: {e}")

    def _add_to_meta(self, post_id: str, source: str, url: str, title: str, content_preview: str = ""):
        self.meta['posts'][post_id] = {
            'source': source,
            'url': url,
            'original_title': title,
            'original_content_preview': content_preview[:500] if content_preview else "",
            'time': get_local_time().isoformat()
        }
        self._save_meta()
        logger.info(f"📝 Метаданные сохранены: {source} - {title[:50]}...")

    def _normalize_title(self, title: str) -> str:
        if not title:
            return ""
        title = clean_title(title)
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        common = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        words = [w for w in title.split() if w not in common]
        return ' '.join(words)[:100]

    def _hash_content(self, content: str) -> str:
        if not content:
            return ""
        return hashlib.md5(content[:500].encode('utf-8')).hexdigest()

    def _is_duplicate(self, url: str, title: str, content: str = "") -> bool:
        if url in self.state['sent_links']:
            logger.info(f"Дубликат по URL: {url[:50]}...")
            return True
        norm_title = self._normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            logger.info(f"Дубликат по заголовку: {title[:50]}...")
            return True
        if content:
            h = self._hash_content(content)
            if h and h in self.state['sent_hashes']:
                logger.info(f"Дубликат по содержимому: {title[:50]}...")
                return True
        return False

    def _mark_sent(self, url: str, title: str, content: str = ""):
        self.state['sent_links'].add(url)
        norm_title = self._normalize_title(title)
        if norm_title:
            self.state['sent_titles'].add(norm_title)
        if content:
            h = self._hash_content(content)
            if h:
                self.state['sent_hashes'].add(h)
        self._save_state()

    def _log_post(self, url: str, title: str):
        self.state['posts_log'].append({
            'link': url,
            'title': title[:50],
            'time': get_local_time().isoformat()
        })
        if len(self.state['posts_log']) > 100:
            self.state['posts_log'] = self.state['posts_log'][-100:]
        self._save_state()

    def _can_post(self) -> bool:
        if IS_MANUAL_RUN:
            return True
        now = get_local_time()
        hour = now.hour
        if 23 <= hour or hour < 7:
            logger.info("Ночное время, публикация отложена")
            return False
        today = now.date()
        today_posts = 0
        last_times = []
        for post in self.state['posts_log']:
            try:
                pt = datetime.fromisoformat(post['time'])
                if pt.date() == today:
                    today_posts += 1
                    last_times.append(pt)
            except Exception:
                continue
        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"Дневной лимит {MAX_POSTS_PER_DAY} достигнут")
            return False
        if last_times:
            last_times.sort(reverse=True)
            elapsed = (now - last_times[0]).total_seconds()
            if elapsed < MIN_INTERVAL:
                wait = (MIN_INTERVAL - elapsed) // 60
                logger.info(f"Минимальный интервал: следующий пост через {wait:.0f} минут")
                return False
        return True

    def _next_delay(self) -> int:
        delay = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        delay = int(delay * random.uniform(0.85, 1.15))
        return max(MIN_INTERVAL, min(delay, MAX_INTERVAL))

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        max_len = max_len - 100
        result = truncate_at_sentence(text, max_len)
        return balance_brackets(result)

    # ========== ПАРСИНГ RSS ==========
    def _parse_rss_feed(self, url: str, source_name: str, limit: int = 5) -> list:
        try:
            feed = feedparser.parse(url)
            articles = []
            for entry in feed.entries[:limit]:
                title = entry.get('title', '').strip()
                if re.search(r'(избранные статьи|featured articles|selected articles)', title, re.IGNORECASE):
                    logger.info(f"⏭️ {source_name}: пропущены избранные статьи '{title[:50]}...'")
                    continue
                if re.search(r'\b(video|видео|watch|смотрите|look|взгляните|witness)\b', title, re.IGNORECASE):
                    logger.info(f"⏭️ {source_name}: пропущено видео/смотрите '{title[:50]}...'")
                    continue
                if re.search(r'(популярн|popular|most popular|top|trending|daily|roundup|summary|recap)', title, re.IGNORECASE):
                    logger.info(f"⏭️ {source_name}: пропущен заголовок '{title[:50]}...'")
                    continue
                if is_foreign_title(title):
                    logger.info(f"⏭️ {source_name}: пропущен нерусский/неанглийский заголовок '{title[:50]}...'")
                    continue
                if not title or len(title) < 5:
                    summary = entry.get('summary', '')
                    if summary:
                        summary = re.sub(r'<[^>]+>', '', summary)
                        title = summary.split('.')[0].strip()
                        if len(title) < 5 and len(summary) > 10:
                            title = summary[:100].strip()
                title = clean_title(title)
                if not title:
                    continue

                rss_image = None
                for m in entry.get('media_content', []) or []:
                    u = m.get('url')
                    if u:
                        rss_image = u
                        break
                if not rss_image:
                    for t in entry.get('media_thumbnail', []) or []:
                        u = t.get('url')
                        if u:
                            rss_image = u
                            break
                if not rss_image:
                    for enc in entry.get('enclosures', []) or []:
                        et = (enc.get('type') or '').lower()
                        if et.startswith('image/') or et in ('', 'application/octet-stream'):
                            u = enc.get('href') or enc.get('url')
                            if u:
                                rss_image = u
                                break
                if rss_image and is_bad_image_url(rss_image):
                    rss_image = None

                articles.append({
                    'url': entry.link,
                    'title': title,
                    'source': source_name,
                    'rss_image': rss_image,
                })
                logger.info(f"{source_name} RSS: {title[:80]}...")
            return articles
        except Exception as e:
            logger.error(f"❌ Ошибка RSS {source_name}: {e}")
            return []

    # ========== ПАРСИНГ СТАТЬИ ==========
    def _parse_article(self, url: str, source_name: str):
        try:
            response = fetch_url(url)
            if not response:
                return None
            if 'substack.com' in url or 'asia-pacificresearch.com' in url:
                logger.warning(f"⏭️ {source_name}: статья пропущена (403)")
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            zh_candidates = []
            if source_name == 'ZeroHedge':
                zh_candidates = collect_image_candidates_generic(soup, base_url)
                image_url = zh_candidates[0] if zh_candidates else None
                if zh_candidates:
                    logger.info(f"ZeroHedge: собрано {len(zh_candidates)} кандидатов картинок")
            else:
                image_url = extract_image_url(soup, base_url, source_name)

            if image_url:
                logger.info(f"Найдено изображение: {image_url[:80]}...")
            else:
                logger.info("⚠️ Изображение не найдено, будет использована заглушка")

            content_parts = []
            content_container = None
            selectors = [
                'article',
                'div.entry-content',
                'div.post-content',
                'div.content',
                'div.article-content',
                'div.main-content',
                'div.article__text',
                'main',
                'div.body'
            ]
            for selector in selectors:
                if selector.startswith('div.') or selector.startswith('main') or selector == 'article':
                    container = soup.select_one(selector)
                    if container:
                        content_container = container
                        break

            if content_container:
                for tag in content_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()
                for p in content_container.find_all('p'):
                    text = p.get_text(strip=True)
                    if is_excluded_author(text):
                        logger.info("⏭️ Пропущен абзац с именем автора")
                        continue
                    if contains_link(text):
                        logger.info("⏭️ Пропущен абзац со ссылкой")
                        continue
                    if is_foreign_text(text):
                        logger.info("⏭️ Пропущен абзац на иностранном языке")
                        continue
                    text_lower = text.lower()
                    if any(junk in text_lower for junk in JUNK_PHRASES):
                        logger.info("⏭️ Пропущен мусорный абзац")
                        continue
                    if len(text) > 40:
                        if not text.startswith('Read more') and not text.startswith('Share this'):
                            content_parts.append(text)

            if len(content_parts) < 2:
                logger.info(f"⚠️ {source_name}: ищем p на всей странице")
                for p in soup.find_all('p'):
                    text = p.get_text(strip=True)
                    if is_excluded_author(text):
                        continue
                    if contains_link(text):
                        continue
                    if is_foreign_text(text):
                        continue
                    text_lower = text.lower()
                    if any(junk in text_lower for junk in JUNK_PHRASES):
                        continue
                    if len(text) > 40 and not text.startswith('Read more'):
                        if not re.search(r'(menu|nav|copyright|all rights reserved)', text, re.IGNORECASE):
                            content_parts.append(text)

            if len(content_parts) < 2:
                logger.warning(f"⚠️ {source_name}: недостаточно контента для {url}")
                return None

            content = '\n\n'.join(content_parts[:20])
            content = unescape_html(content)
            content = remove_junk_paragraphs(content)
            content = balance_brackets(content)

            if len(content) < 150:
                logger.warning(f"⚠️ {source_name}: контент слишком короткий ({len(content)} символов)")
                return None

            return {
                'content': content,
                'image': image_url,
                'source': source_name,
                'url': url,
                'image_candidates': zh_candidates,
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга {source_name}: {e}")
            return None

    # ========== МЕТОДЫ ДЛЯ ИСТОЧНИКОВ ==========
    def _get_infobrics_articles(self) -> list:
        return self._parse_rss_feed('https://infobrics.org/rss/en', 'InfoBrics')

    def _parse_infobrics_article(self, url: str):
        return self._parse_article(url, 'InfoBrics')

    def _get_globalresearch_articles(self) -> list:
        return self._parse_rss_feed('https://www.globalresearch.ca/feed', 'Global Research')

    def _parse_globalresearch_article(self, url: str):
        return self._parse_article(url, 'Global Research')

    def _get_rt_articles(self) -> list:
        return self._parse_rss_feed('https://www.rt.com/rss/news/', 'RT')

    def _parse_rt_article(self, url: str):
        return self._parse_article(url, 'RT')

    def _get_zerohedge_articles(self) -> list:
        return self._parse_rss_feed('https://feeds.feedburner.com/zerohedge/feed', 'ZeroHedge')

    def _parse_zerohedge_article(self, url: str):
        return self._parse_article(url, 'ZeroHedge')

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        sources = [
            ('InfoBrics', self._get_infobrics_articles, self._parse_infobrics_article),
            ('Global Research', self._get_globalresearch_articles, self._parse_globalresearch_article),
            ('RT', self._get_rt_articles, self._parse_rt_article),
            ('ZeroHedge', self._get_zerohedge_articles, self._parse_zerohedge_article),
        ]
        for source_name, get_func, parse_func in sources:
            try:
                logger.info(f"📰 Парсинг {source_name}...")
                articles = await asyncio.get_event_loop().run_in_executor(None, get_func)
                for article in articles[:3]:
                    title = article.get('title', '')
                    url = article.get('url', '')
                    if self._is_duplicate(url, title):
                        continue
                    data = await asyncio.get_event_loop().run_in_executor(None, parse_func, url)
                    if data:
                        data['title'] = title
                        data['rss_image'] = article.get('rss_image')
                        logger.info(f"✅ {source_name}: {title[:80]}...")
                        if not self._is_duplicate(url, title, data['content']):
                            items.append(data)
            except Exception as e:
                logger.error(f"❌ Критическая ошибка {source_name}: {e}")
                continue
        logger.info(f"📊 Всего новых статей: {len(items)}")
        return items

    # ========== ПУБЛИКАЦИЯ ==========
    async def publish(self, post: dict):
        try:
            title_en = post.get('title', '')
            content_en = post.get('content', '')
            url = post.get('url', '')
            image_url = post.get('image')
            source_name = post.get('source', '')
            image_candidates = post.get('image_candidates') or []
            rss_image = post.get('rss_image')

            if not title_en or not content_en:
                logger.error("❌ Нет заголовка или содержимого")
                return

            title_en = clean_title(title_en)
            if not title_en:
                logger.warning("⏭️ Пропуск: пустой заголовок")
                return

            if url in self.state['sent_links']:
                logger.warning(f"⛔ Уже опубликовано: {url[:80]}...")
                return

            logger.info(f"📝 Перевод: {title_en[:80]}...")
            loop = asyncio.get_event_loop()
            title_short = truncate_at_sentence(title_en, max_len=300) or title_en[:300]
            content_short = truncate_at_sentence(content_en, max_len=MAX_TRANSLATE_INPUT)

            if not content_short:
                logger.warning("⚠️ Контент не удалось обрезать по предложению, пропуск")
                return

            logger.info(f"  ✂️ Заголовок: {len(title_en)} → {len(title_short)} символов")
            logger.info(f"  ✂️ Контент: {len(content_en)} → {len(content_short)} символов")

            title_ru = await loop.run_in_executor(None, translate_text, title_short)
            title_ru = unescape_html(title_ru)
            title_ru = postprocess_translation(title_ru)
            title_ru = clean_title(title_ru) or title_ru or title_short

            if is_bad_title(title_ru):
                logger.warning("⚠️ Заголовок смешанный, повторный перевод...")
                title_retry = await loop.run_in_executor(None, translate_text, title_short)
                title_retry = unescape_html(title_retry)
                title_retry = postprocess_translation(title_retry)
                title_retry = clean_title(title_retry)
                if title_retry and not is_bad_title(title_retry):
                    title_ru = title_retry
                    logger.info("✅ Повторный перевод удался")

            content_ru = await loop.run_in_executor(None, translate_text, content_short)
            content_ru = unescape_html(content_ru)
            content_ru = postprocess_translation(content_ru)
            content_ru = content_ru or content_short
            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'\([^)]*(?:AP|Associated Press|Ассошиэйтед Пресс)[^)]*\)', '', content_ru, flags=re.IGNORECASE)
            content_ru = unescape_html(content_ru)
            content_ru = cut_non_english_tail(content_ru)
            content_ru = balance_brackets(content_ru)

            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, source_name, url, title_en, content_en)

            title_clean = clean_title(title_ru) or title_ru
            title_escaped = html_module.escape(title_clean, quote=False)

            # ========== ЗАГРУЗКА КАРТИНКИ ==========
            image_trials = []
            if rss_image:
                image_trials.append(rss_image)
            if image_url and image_url not in image_trials:
                image_trials.append(image_url)
            for c in image_candidates:
                if c and c not in image_trials:
                    image_trials.append(c)

            img_bytes = None
            if source_name == 'ZeroHedge':
                logger.info(f"ZeroHedge: {len(image_trials)} кандидатов на картинку")
                for i, cand in enumerate(image_trials):
                    logger.info(f"  ZH try {i+1}/{len(image_trials)}: {cand[:70]}")
                    try:
                        img_bytes = fetch_zerohedge_image(cand)
                    except Exception as e:
                        logger.warning(f"  ZH exc: {e}")
                        img_bytes = None
                    if img_bytes:
                        logger.info(f"  ✅ ZeroHedge image OK: {cand[:60]}")
                        break
            else:
                for i, cand in enumerate(image_trials):
                    try:
                        img_bytes = fetch_image_with_fallback(cand, referer=url, source=source_name)
                    except Exception as e:
                        logger.warning(f"  Image exc: {e}")
                        img_bytes = None
                    if img_bytes:
                        break

            if not img_bytes:
                img_bytes = fetch_fallback_image()

            # Финальная нормализация: Telegram sendPhoto надёжно принимает JPEG/PNG/GIF.
            # AVIF/WebP часто дают 400 Image_process_failed — конвертим.
            if img_bytes:
                norm = _normalize_to_jpeg(img_bytes)
                if norm is None:
                    logger.warning("⚠️ Картинка не конвертируется в JPEG, используем заглушку")
                    img_bytes = fetch_fallback_image()
                else:
                    img_bytes = norm

            # ========== ПУБЛИКАЦИЯ С ФОТО ==========
            if img_bytes:
                try:
                    available = MAX_CAPTION - len(title_clean) - 20
                    if available < 50:
                        available = MAX_CAPTION - 100
                    body_truncated = truncate_at_sentence(content_ru, available)
                    if not body_truncated:
                        body_truncated = content_ru[:available]
                    body_truncated = balance_brackets(body_truncated)
                    body_escaped = html_module.escape(body_truncated, quote=False)

                    caption = f"<b>{title_escaped}</b>\n\n{body_escaped}".strip()
                    if len(caption) > MAX_CAPTION + 200:
                        caption = caption[:MAX_CAPTION + 200]

                    await self.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=img_bytes,
                        caption=caption,
                        parse_mode='HTML'
                    )
                    logger.info("✅ Опубликовано С ФОТО")
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                    return
                except TelegramError as e:
                    logger.warning(f"Ошибка отправки фото: {e}")
                    try:
                        plain_caption = f"{title_clean}\n\n{body_truncated}"[:MAX_CAPTION]
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=img_bytes,
                            caption=plain_caption,
                            parse_mode=None
                        )
                        logger.info("✅ Опубликовано С ФОТО (plain)")
                        self._mark_sent(url, title_en, content_en)
                        self._log_post(url, title_en)
                        return
                    except Exception as e2:
                        logger.warning(f"Повтор фото без parse_mode: {e2}")

            # ========== ПУБЛИКАЦИЯ ТЕКСТОМ ==========
            logger.info("📝 Публикация текстом")
            available = MAX_MESSAGE - len(title_clean) - 20
            if available < 100:
                available = MAX_MESSAGE - 200
            body_truncated = truncate_at_sentence(content_ru, available)
            if not body_truncated:
                body_truncated = content_ru[:available]
            body_truncated = balance_brackets(body_truncated)
            body_escaped = html_module.escape(body_truncated, quote=False)
            text_message = f"<b>{title_escaped}</b>\n\n{body_escaped}".strip()

            try:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text_message,
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                logger.info("✅ Опубликовано ТЕКСТОМ")
                self._mark_sent(url, title_en, content_en)
                self._log_post(url, title_en)
            except TelegramError as e:
                logger.warning(f"Ошибка текста: {e}")
                try:
                    plain = f"{title_clean}\n\n{content_ru}"[:MAX_MESSAGE]
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=plain, parse_mode=None)
                    logger.info("✅ Опубликовано ТЕКСТОМ (plain)")
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"❌ Ошибка plain-отправки: {e2}")

        except Exception as e:
            logger.error(f"❌ Критическая ошибка publish: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 50)
        logger.info(f"🚀 Запуск [{get_local_time().strftime('%H:%M:%S')}]")
        if IS_MANUAL_RUN:
            logger.info("🔓 РЕЖИМ РУЧНОГО ЗАПУСКА")
        logger.info("=" * 50)

        news = await self.fetch_news()
        if not news:
            logger.info("📭 Новых статей нет")
            return

        published_count = 0
        for article in news:
            if not self._can_post():
                logger.info(f"⏸️ Лимит, опубликовано {published_count}")
                break
            logger.info(f"📤 Публикация {published_count + 1}/{len(news)}")
            await self.publish(article)
            published_count += 1
            if published_count < len(news):
                if IS_MANUAL_RUN:
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(60)

        logger.info(f"✅ Опубликовано: {published_count}")

    async def run_forever(self):
        logger.info("🤖 Бот запущен")
        while True:
            try:
                await self.run_once()
                delay = self._next_delay()
                logger.info(f"⏰ Следующий запуск через {delay // 60} минут")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"❌ Ошибка: {e}")
                await asyncio.sleep(300)

async def main():
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан!")
        return
    if not CHANNEL_ID:
        logger.error("❌ CHANNEL_ID не задан!")
        return

    bot = NewsBot()
    if 'GITHUB_ACTIONS' in os.environ:
        await bot.run_once()
    else:
        await bot.run_forever()

if __name__ == '__main__':
    asyncio.run(main())
