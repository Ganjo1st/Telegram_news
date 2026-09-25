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
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('news_bot')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

MIN_INTERVAL = 2100
MAX_INTERVAL = 7200
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 30

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 1024
MAX_MESSAGE = 4096

MAX_TRANSLATE_INPUT = 1500
TRANSLATE_CHUNK_SIZE = 450

IS_MANUAL_RUN = os.getenv('TEST_MODE', '').lower() == 'true'

# ========== ИСКЛЮЧАЕМЫЕ АВТОРЫ ==========
EXCLUDED_AUTHORS = [
    'Уриэль Араухо', 'Uriel Araujo',
    'Ахмед Адель', 'Ahmed Adel',
    'Лукас Лейроз', 'Lucas Leiros',
    'Одри Чайлд', 'Audrey Child',
    'Андрей Корыбко', 'Andrei Korybko',
    'Стив Уотсон', 'Steve Watson',
]

# ========== СЛОВАРЬ ДЛЯ ПОСТОБРАБОТКИ ПЕРЕВОДА ==========
POST_TRANSLATION_FIXES = {
    'Spanish': 'испанское',
    'extradition': 'экстрадиция',
    'communist': 'коммунистический',
    'centimillionaire': 'мультимиллионер',
    'reported': 'сообщил',
    'detained': 'задержан',
    'arrest': 'арест',
    'Ibiza': 'Ибица',
    'wanted': 'разыскивается',
    'money laundering': 'отмывание денег',
    'riot': 'бунт',
    'conspiracy': 'сговор',
    'charges': 'обвинения',
    'demonstrations': 'демонстрации',
    'transfers': 'переводы',
    'company': 'компания',
    'Tunisia': 'Тунис',
    'previously': 'ранее',
    'lived': 'проживал',
    'spokeswoman': 'представитель',
    'confirmed': 'подтвердил',
    'outlet': 'издание',
    'courts': 'суды',
    'review': 'рассмотрение',
    'request': 'запрос',
    'judges': 'судьи',
    'approve': 'одобрят',
    'final': 'окончательное',
    'decision': 'решение',
    'returns': 'возвращается',
    'Prime Minister': 'премьер-министру',
    'Cabinet': 'кабинет',
    'Traders': 'трейдеры',
    'modest': 'скромные',
    'hopes': 'надежды',
    'deal': 'сделку',
    'While': 'Хотя',
    'provide': 'предоставим',
    'detailed': 'подробный',
    'preview': 'обзор',
    'summit': 'саммит',
    'subsequent': 'последующем',
    'post': 'посте',
    'signs': 'признаки',
    'President': 'президент',
    'Chinese': 'китайский',
    'counterpart': 'коллега',
    'emerge': 'выйти',
    'upcoming': 'предстоящего',
    'agreement': 'соглашение',
    'artificial': 'искусственного',
    'intelligence': 'интеллекта',
    'industry': 'отрасли',
    'increasingly': 'всё более',
    'prominent': 'заметной',
    'flash point': 'точкой напряжённости',
    'between': 'между',
    'world': 'мира',
    'biggest': 'крупнейшими',
    'economies': 'экономиками',
    'fund managers': 'управляющие фондами',
    'say': 'говорят',
    'unlikely': 'маловероятно',
    'significant': 'значительным',
    'enough': 'достаточно',
    'sustainable': 'устойчивый',
    'boost': 'рост',
    'related': 'связанных',
    'stocks': 'акций',
    'Over': 'В',
    'weekend': 'выходные',
    'Treasury Secretary': 'министр финансов',
    'after': 'после',
    'hours': 'часов',
    'negotiations': 'переговоров',
    'Vice Premier': 'вице-премьером',
    'said': 'сказал',
    'two sides': 'две стороны',
    'agreed': 'согласились',
    'create': 'создать',
    'called': 'назвал',
    'dialogue': 'диалог',
    'technology': 'технологии',
    'benefits': 'выгоды',
    'threats': 'угрозы',
    'The': 'Этот',
    'narrative': 'нарратив',
    'pushed': 'продвигаемый',
    'certain': 'некоторыми',
    'pro-war': 'провоенными',
    'European': 'европейскими',
    'hard': 'трудно',
    'imagine': 'представить',
    'how': 'как',
    'hackers': 'хакеры',
    'have': 'украли',
    'stolen': 'украли',
    'nearly': 'почти',
    'million': 'миллионов',
    'from': 'из',
    'major': 'крупной',
    'crypto': 'криптобиржи',
    'exchange': 'биржи',
    'Vladimir': 'Владимир',
    'put': 'поставил',
    'his': 'свои',
    'personal': 'личные',
    'interests': 'интересы',
    'first': 'на первое место',
    'wants': 'хочет',
    'colonize': 'колонизировать',
    'Brazil': 'Бразилию',
    'campaign': 'кампания',
    'age': 'эпоха',
    'global': 'глобальных',
    'empires': 'империй',
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

def postprocess_translation(text: str) -> str:
    if not text:
        return text
    result = text
    multiword = {k: v for k, v in POST_TRANSLATION_FIXES.items() if ' ' in k}
    for eng, rus in sorted(multiword.items(), key=lambda x: -len(x[0])):
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(eng) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, rus, result, flags=re.IGNORECASE)
    single = {k: v for k, v in POST_TRANSLATION_FIXES.items() if ' ' not in k}
    for eng, rus in single.items():
        pattern = r'(?<![а-яА-Яa-zA-Z])' + re.escape(eng) + r'(?![а-яА-Яa-zA-Z])'
        result = re.sub(pattern, rus, result, flags=re.IGNORECASE)
    result = re.sub(r'\s+', ' ', result).strip()
    return result

def truncate_at_sentence(text: str, max_len: int) -> str:
    """
    Обрезает текст до max_len, ВСЕГДА заканчивая на границе предложения.
    Если в пределах max_len нет конца предложения — возвращает текст до
    ПРЕДЫДУЩЕГО конца предложения (даже если он короче max_len).
    Если предложений нет вообще — возвращает пустую строку.
    """
    if not text:
        return ""

    text = text.strip()
    if len(text) <= max_len:
        return text

    # Ищем последний знак конца предложения в пределах max_len
    cut_pos = -1
    for punct in ['.', '!', '?']:
        # Ищем позицию, где за пунктуацией идёт пробел или конец строки
        for m in re.finditer(re.escape(punct) + r'(?=\s|$)', text[:max_len + 1]):
            if m.end() > cut_pos:
                cut_pos = m.end()

    if cut_pos != -1:
        return text[:cut_pos].strip()

    # Если в пределах max_len нет ни одного конца предложения,
    # ищем ПЕРВЫЙ конец предложения ЗА пределами max_len
    for punct in ['.', '!', '?']:
        m = re.search(re.escape(punct) + r'(?=\s|$)', text[max_len:])
        if m:
            end_pos = max_len + m.end()
            # Если первое предложение слишком длинное (больше 2×max_len) — не берём
            if end_pos <= max_len * 2:
                return text[:end_pos].strip()
            break

    # Совсем нет конца предложения — возвращаем пусто (лучше ничего, чем обрывок)
    return ""

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None

def extract_image_url(soup, base_url: str):
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        img_url = meta_img['content']
        if img_url.startswith('//'):
            return 'https:' + img_url
        if img_url.startswith('/'):
            return urljoin(base_url, img_url)
        if img_url.startswith('http'):
            return img_url

    meta_twitter = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta_twitter and meta_twitter.get('content'):
        img_url = meta_twitter['content']
        if img_url.startswith('//'):
            return 'https:' + img_url
        if img_url.startswith('/'):
            return urljoin(base_url, img_url)
        if img_url.startswith('http'):
            return img_url

    article = soup.find('article')
    if article:
        for img in article.find_all('img', src=True):
            src = img.get('src', '')
            if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif', 'banner', 'flag']):
                continue
            if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                if src.startswith('//'):
                    return 'https:' + src
                if src.startswith('/'):
                    return urljoin(base_url, src)
                if src.startswith('http'):
                    return src

    for img in soup.find_all('img', src=True):
        src = img.get('src', '')
        if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif', 'flag']):
            continue
        if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            if src.startswith('//'):
                return 'https:' + src
            if src.startswith('/'):
                return urljoin(base_url, src)
            if src.startswith('http'):
                return src

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
                except:
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
            except:
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
        # truncate_at_sentence гарантирует, что текст всегда заканчивается на границе предложения
        return truncate_at_sentence(text, max_len)

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

                articles.append({
                    'url': entry.link,
                    'title': title,
                    'source': source_name
                })
                logger.info(f"{source_name} RSS: {title[:80]}...")

            return articles
        except Exception as e:
            logger.error(f"❌ Ошибка RSS {source_name}: {e}")
            return []

    # ========== ПАРСИНГ СТАТЬИ ==========
    def _parse_article(self, url: str, source_name: str) -> dict | None:
        try:
            response = fetch_url(url)
            if not response:
                return None

            if 'substack.com' in url or 'asia-pacificresearch.com' in url:
                logger.warning(f"⏭️ {source_name}: статья пропущена (403)")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            image_url = extract_image_url(soup, base_url)
            if image_url:
                logger.info(f"Найдено изображение: {image_url[:80]}...")

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
                        logger.info(f"⏭️ Пропущен абзац с именем автора")
                        continue

                    if contains_link(text):
                        logger.info(f"⏭️ Пропущен абзац со ссылкой")
                        continue

                    if is_foreign_text(text):
                        logger.info(f"⏭️ Пропущен абзац на иностранном языке")
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

                    if len(text) > 40 and not text.startswith('Read more'):
                        if not re.search(r'(menu|nav|copyright|all rights reserved)', text, re.IGNORECASE):
                            content_parts.append(text)

            if len(content_parts) < 2:
                logger.warning(f"⚠️ {source_name}: недостаточно контента для {url}")
                return None

            content = '\n\n'.join(content_parts[:20])
            content = unescape_html(content)

            if len(content) < 150:
                logger.warning(f"⚠️ {source_name}: контент слишком короткий ({len(content)} символов)")
                return None

            return {
                'content': content,
                'image': image_url,
                'source': source_name,
                'url': url
            }

        except Exception as e:
            logger.error(f"Ошибка парсинга {source_name}: {e}")
            return None

    # ========== МЕТОДЫ ДЛЯ ИСТОЧНИКОВ ==========
    def _get_infobrics_articles(self) -> list:
        return self._parse_rss_feed('https://infobrics.org/rss/en', 'InfoBrics')

    def _parse_infobrics_article(self, url: str) -> dict | None:
        return self._parse_article(url, 'InfoBrics')

    def _get_globalresearch_articles(self) -> list:
        return self._parse_rss_feed('https://www.globalresearch.ca/feed', 'Global Research')

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        return self._parse_article(url, 'Global Research')

    def _get_rt_articles(self) -> list:
        return self._parse_rss_feed('https://www.rt.com/rss/news/', 'RT')

    def _parse_rt_article(self, url: str) -> dict | None:
        return self._parse_article(url, 'RT')

    def _get_zerohedge_articles(self) -> list:
        return self._parse_rss_feed('https://feeds.feedburner.com/zerohedge/feed', 'ZeroHedge')

    def _parse_zerohedge_article(self, url: str) -> dict | None:
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

            # Обрезаем ДО перевода, всегда по границе предложения
            title_short = truncate_at_sentence(title_en, max_len=300)
            content_short = truncate_at_sentence(content_en, max_len=MAX_TRANSLATE_INPUT)

            if not title_short:
                title_short = title_en[:300]
            if not content_short:
                logger.warning("⚠️ Контент не удалось обрезать по предложению, пропуск")
                return

            logger.info(f"  ✂️ Заголовок: {len(title_en)} → {len(title_short)} символов")
            logger.info(f"  ✂️ Контент: {len(content_en)} → {len(content_short)} символов")

            # Перевод заголовка
            title_ru = await loop.run_in_executor(None, translate_text, title_short)
            title_ru = unescape_html(title_ru)
            title_ru = postprocess_translation(title_ru)
            title_ru = clean_title(title_ru) or title_ru or title_short

            if is_bad_title(title_ru):
                logger.warning(f"⚠️ Заголовок смешанный, повторный перевод...")
                title_retry = await loop.run_in_executor(None, translate_text, title_short)
                title_retry = unescape_html(title_retry)
                title_retry = postprocess_translation(title_retry)
                title_retry = clean_title(title_retry)
                if title_retry and not is_bad_title(title_retry):
                    title_ru = title_retry
                    logger.info(f"✅ Повторный перевод удался")

            # Перевод контента
            content_ru = await loop.run_in_executor(None, translate_text, content_short)
            content_ru = unescape_html(content_ru)
            content_ru = postprocess_translation(content_ru)
            content_ru = content_ru or content_short

            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'\([^)]*(?:AP|Associated Press|Ассошиэйтед Пресс)[^)]*\)', '', content_ru, flags=re.IGNORECASE)

            content_ru = unescape_html(content_ru)

            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, post.get('source', ''), url, title_en, content_en)

            title_clean = clean_title(title_ru) or title_ru
            title_escaped = html_module.escape(title_clean)

            content_truncated = self._truncate_text(content_ru, is_caption=True)
            message = f"*{title_escaped}*\n\n{content_truncated}"

            if image_url:
                logger.info(f"🖼️ Загрузка изображения: {image_url[:80]}...")
                img_response = fetch_url(image_url, timeout=15)

                if img_response and img_response.status_code == 200:
                    content_type = img_response.headers.get('Content-Type', '')
                    if 'image' in content_type:
                        try:
                            if len(message) > MAX_CAPTION:
                                # Обрезаем caption по границе предложения
                                title_part = f"*{title_escaped}*\n\n"
                                available = MAX_CAPTION - len(title_part) - 50
                                truncated_body = truncate_at_sentence(content_ru, available)
                                if not truncated_body:
                                    truncated_body = content_ru[:available]
                                message = f"*{title_escaped}*\n\n{truncated_body}"
                            await self.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=img_response.content,
                                caption=message,
                                parse_mode='Markdown'
                            )
                            logger.info("✅ Опубликовано С ФОТО")
                            self._mark_sent(url, title_en, content_en)
                            self._log_post(url, title_en)
                            return
                        except TelegramError as e:
                            logger.warning(f"Ошибка фото: {e}")

            logger.info("📝 Публикация текстом")
            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"*{title_escaped}*\n\n{text_content}"

            if len(text_message) > MAX_MESSAGE:
                title_part = f"*{title_escaped}*\n\n"
                available = MAX_MESSAGE - len(title_part) - 50
                truncated_body = truncate_at_sentence(content_ru, available)
                if not truncated_body:
                    truncated_body = content_ru[:available]
                text_message = f"*{title_escaped}*\n\n{truncated_body}"

            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )
            logger.info("✅ Опубликовано ТЕКСТОМ")

            self._mark_sent(url, title_en, content_en)
            self._log_post(url, title_en)

        except TelegramError as e:
            error_msg = str(e)
            if "Can't parse entities" in error_msg:
                try:
                    text_message = f"{title_ru}\n\n{content_ru}"
                    if len(text_message) > MAX_MESSAGE:
                        text_message = text_message[:MAX_MESSAGE - 50] + "..."
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=text_message, parse_mode=None)
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"❌ Ошибка отправки: {e2}")
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")

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
