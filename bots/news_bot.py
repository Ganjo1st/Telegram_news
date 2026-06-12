#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import asyncio
import hashlib
import re
import html
import random
import signal
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError, TimedOut
from deep_translator import GoogleTranslator, exceptions as translator_exceptions

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('news_bot')

# Добавляем перехват сигналов для корректного завершения
def signal_handler(signum, frame):
    logger.info(f"Получен сигнал {signum}, завершаем работу...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

# Интервалы публикации
MIN_INTERVAL = 2100
MAX_INTERVAL = 7200
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

# ТАЙМАУТЫ - КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ!
REQUEST_TIMEOUT = 15  # Таймаут для HTTP запросов
TRANSLATION_TIMEOUT = 30  # Таймаут для перевода
PARSE_TIMEOUT = 45  # Таймаут на парсинг одной статьи

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 1024
MAX_MESSAGE = 4096

IMAGE_HASH_CACHE = set()

# Запрещенные темы
SKIP_TITLES = [
    'died', 'dies', 'dead', 'killed', 'murdered', 'assassinated',
    'passed away', 'obituary', 'death of', 'умер', 'скончался',
    'biography', 'биография', 'birthday', 'memorial', 'funeral',
]

SKIP_CONTENT_KEYWORDS = [
    'позирует фотографам', 'photo session', 'red carpet',
    'arrives at the premiere', 'poses for photographers',
    'attends the screening',
]

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def decode_html_entities(text: str) -> str:
    if not text:
        return text
    text = text.replace('&quot;', '"').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&apos;', "'").replace('&#39;', "'")
    text = re.sub(r'&#\d+;', '', text)
    return text

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = decode_html_entities(text)
    text = re.sub(r'\([^)]*AP[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*АР[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Photo[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'—\s*AP\s*News.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def is_valid_news(title: str, content: str = "") -> bool:
    if not title:
        return False
    title_lower = title.lower()
    for word in SKIP_TITLES:
        if word in title_lower:
            logger.debug(f"Пропуск по заголовку: '{word}' в '{title[:50]}'")
            return False
    # Для контента проверяем только если он есть
    if content:
        content_lower = content.lower()
        for word in SKIP_CONTENT_KEYWORDS:
            if word in content_lower:
                logger.debug(f"Пропуск по контенту: '{word}'")
                return False
    return True

def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    """Функция с таймаутом"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except requests.exceptions.Timeout:
        logger.error(f"Таймаут при запросе {url[:80]}")
        return None
    except Exception as e:
        logger.error(f"Ошибка запроса {url[:80]}: {e}")
        return None

def extract_image_url(soup, base_url: str) -> str | None:
    """Поиск изображения"""
    exclude = ['logo', 'icon', 'svg', 'gif', 'pixel', 'ap-logo', 'favicon', 'banner', 'avatar']
    
    # 1. Open Graph
    meta = soup.find('meta', property='og:image')
    if meta and meta.get('content'):
        img = meta['content']
        if img.startswith('//'):
            img = 'https:' + img
        elif img.startswith('/'):
            img = urljoin(base_url, img)
        if img.startswith('http') and not any(x in img.lower() for x in exclude):
            return img
    
    # 2. Twitter
    meta = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta and meta.get('content'):
        img = meta['content']
        if img.startswith('//'):
            img = 'https:' + img
        elif img.startswith('/'):
            img = urljoin(base_url, img)
        if img.startswith('http') and not any(x in img.lower() for x in exclude):
            return img
    
    # 3. Поиск в статье
    container = soup.find('article') or soup.find('main')
    if container:
        for img in container.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if not src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = urljoin(base_url, src)
            if src.startswith('http') and not any(x in src.lower() for x in exclude):
                return src
    return None

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN, request_timeout=REQUEST_TIMEOUT)
        # Инициализация переводчика будет ленивой (при первом использовании)
        self._translator = None
        self._load_image_cache()

    @property
    def translator(self):
        """Ленивая инициализация переводчика с обработкой ошибок"""
        if self._translator is None:
            try:
                self._translator = GoogleTranslator(source='en', target='ru')
            except Exception as e:
                logger.error(f"Не удалось инициализировать переводчик: {e}")
                self._translator = None
        return self._translator

    def _load_image_cache(self):
        global IMAGE_HASH_CACHE
        IMAGE_HASH_CACHE = set(self.state.get('used_images', []))

    def _load_state(self) -> dict:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return {
                        'sent_links': set(data.get('sent_links', [])),
                        'sent_hashes': set(data.get('sent_hashes', [])),
                        'sent_titles': set(data.get('sent_titles', [])),
                        'posts_log': data.get('posts_log', []),
                        'used_images': data.get('used_images', [])
                    }
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")
        return {'sent_links': set(), 'sent_hashes': set(), 'sent_titles': set(), 'posts_log': [], 'used_images': []}

    def _save_state(self):
        try:
            data = {
                'sent_links': list(self.state['sent_links']),
                'sent_hashes': list(self.state['sent_hashes']),
                'sent_titles': list(self.state['sent_titles']),
                'posts_log': self.state['posts_log'],
                'used_images': list(IMAGE_HASH_CACHE)
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

    def _add_to_meta(self, post_id: str, source: str, url: str, title: str, preview: str = ""):
        self.meta['posts'][post_id] = {
            'source': source, 'url': url, 'original_title': title,
            'preview': preview[:500], 'time': get_local_time().isoformat()
        }
        self._save_meta()

    def _is_duplicate(self, url: str, title: str, content: str = "") -> bool:
        if url in self.state['sent_links']:
            return True
        norm = title.lower().strip()[:100]
        if norm and norm in self.state['sent_titles']:
            return True
        if content:
            h = hashlib.md5(content[:500].encode()).hexdigest()
            if h in self.state['sent_hashes']:
                return True
        return False

    def _mark_sent(self, url: str, title: str, content: str = "", img: str = None):
        self.state['sent_links'].add(url)
        norm = title.lower().strip()[:100]
        if norm:
            self.state['sent_titles'].add(norm)
        if content:
            self.state['sent_hashes'].add(hashlib.md5(content[:500].encode()).hexdigest())
        if img:
            IMAGE_HASH_CACHE.add(hashlib.md5(img.encode()).hexdigest())
        self._save_state()

    def _log_post(self, url: str, title: str):
        self.state['posts_log'].append({'link': url, 'title': title[:50], 'time': get_local_time().isoformat()})
        if len(self.state['posts_log']) > 100:
            self.state['posts_log'] = self.state['posts_log'][-100:]
        self._save_state()

    def _can_post(self) -> bool:
        now = get_local_time()
        if 23 <= now.hour or now.hour < 7:
            return False
        today_posts = sum(1 for p in self.state['posts_log'] 
                         if datetime.fromisoformat(p['time']).date() == now.date())
        if today_posts >= MAX_POSTS_PER_DAY:
            return False
        if self.state['posts_log']:
            last = datetime.fromisoformat(self.state['posts_log'][-1]['time'])
            if (now - last).total_seconds() < MIN_INTERVAL:
                return False
        return True

    def _truncate_sentence(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        for p in ['.', '!', '?']:
            pos = text.rfind(p, 0, limit)
            if pos > limit // 2:
                return text[:pos + 1].strip()
        pos = text.rfind(' ', 0, limit)
        if pos > 0:
            return text[:pos].strip() + '.'
        return text[:limit].strip() + '.'

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        limit = MAX_CAPTION - 100 if is_caption else MAX_MESSAGE - 100
        paras = re.split(r'\n\s*\n', text)
        result = []
        length = 0
        for p in paras:
            p = p.strip()
            if not p:
                continue
            if length + len(p) + 2 <= limit:
                result.append(p)
                length += len(p) + 2
            else:
                if not result:
                    return self._truncate_sentence(p, limit)
                break
        if result:
            return '\n\n'.join(result)
        return self._truncate_sentence(text, limit)

    def _translate(self, text: str) -> str:
        """Безопасная функция перевода с таймаутом и обработкой ошибок"""
        if not text or len(text) < 10:
            return text
        
        translator_instance = self.translator
        if translator_instance is None:
            logger.warning("Переводчик недоступен, возвращаем оригинальный текст")
            return text[:2000]
        
        try:
            # Запускаем перевод с таймаутом через asyncio
            result = asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: translator_instance.translate(text[:3000])
            )
            # Устанавливаем таймаут на операцию
            result = asyncio.wait_for(result, timeout=TRANSLATION_TIMEOUT)
            # Обрабатываем результат синхронно в этом контексте
            # (для совместимости с существующим кодом)
            translated = asyncio.get_event_loop().run_until_complete(result)
            return clean_text(translated) if translated else text[:2000]
        except asyncio.TimeoutError:
            logger.error(f"Таймаут перевода ({TRANSLATION_TIMEOUT} сек)")
            return text[:2000]
        except translator_exceptions.TranslationError as e:
            logger.error(f"Ошибка API перевода: {e}")
            return text[:2000]
        except Exception as e:
            logger.error(f"Непредвиденная ошибка перевода: {e}")
            return text[:2000]

    # ========== ПАРСИНГ AP NEWS ==========
    async def _parse_apnews_article(self, url: str) -> dict | None:
        """Парсинг с принудительным таймаутом"""
        try:
            # Запускаем парсинг в executor с таймаутом
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._parse_apnews_article_sync, url),
                timeout=PARSE_TIMEOUT
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"Таймаут парсинга AP News: {url[:80]}")
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга AP News: {e}")
            return None

    def _parse_apnews_article_sync(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            base = f'https://{url.split("/")[2]}'

            # Заголовок
            title = None
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                title = og_title['content']
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            if not title:
                return None
            
            title = clean_text(title)
            if not is_valid_news(title):
                return None

            # Изображение
            image = extract_image_url(soup, base)
            if image and hashlib.md5(image.encode()).hexdigest() in IMAGE_HASH_CACHE:
                image = None

            # Контент
            article = soup.find('article')
            if not article:
                article = soup.find('main')
            if not article:
                return None

            for tag in article.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                tag.decompose()

            paragraphs = []
            for p in article.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 50:
                    if any(x in text.lower() for x in ['file -', 'this photo', 'poses for']):
                        continue
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 6:
                    break

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs[:4])  # Не больше 4 параграфов
            content = clean_text(content)
            
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image, 'source': 'AP News', 'url': url}
        except Exception as e:
            logger.error(f"AP News парсинг: {e}")
            return None

    # ========== ПАРСИНГ INFOBRICS ==========
    async def _parse_infobrics_article(self, url: str) -> dict | None:
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._parse_infobrics_article_sync, url),
                timeout=PARSE_TIMEOUT
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"Таймаут InfoBrics: {url[:80]}")
            return None
        except Exception as e:
            logger.error(f"Ошибка InfoBrics: {e}")
            return None

    def _parse_infobrics_article_sync(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            base = f'https://{url.split("/")[2]}'

            title = None
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                title = og['content']
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            if not title or title.lower() in ['brics portal', 'portal']:
                return None
            
            title = clean_text(title)
            if not is_valid_news(title):
                return None

            image = extract_image_url(soup, base)
            if image and hashlib.md5(image.encode()).hexdigest() in IMAGE_HASH_CACHE:
                image = None

            content_div = soup.find('div', class_=re.compile(r'article|content|post'))
            if not content_div:
                content_div = soup.find('article')
            if not content_div:
                return None

            paragraphs = []
            for p in content_div.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 40 and not text.startswith('Read more'):
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 4:
                    break

            if len(paragraphs) < 2:
                return None
            content = '\n\n'.join(paragraphs)
            content = clean_text(content)
            
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"InfoBrics парсинг: {e}")
            return None

    # ========== ПАРСИНГ GLOBAL RESEARCH ==========
    async def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._parse_globalresearch_article_sync, url),
                timeout=PARSE_TIMEOUT
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"Таймаут Global Research: {url[:80]}")
            return None
        except Exception as e:
            logger.error(f"Ошибка Global Research: {e}")
            return None

    def _parse_globalresearch_article_sync(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            base = f'https://{url.split("/")[2]}'

            title = None
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                title = og['content']
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            if not title:
                return None
            
            title = clean_text(title)
            if not is_valid_news(title):
                return None

            image = extract_image_url(soup, base)
            if image and hashlib.md5(image.encode()).hexdigest() in IMAGE_HASH_CACHE:
                image = None

            content_div = soup.find('div', class_=re.compile(r'entry-content|post-content'))
            if not content_div:
                content_div = soup.find('article')
            if not content_div:
                return None

            paragraphs = []
            for p in content_div.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 40 and not text.startswith('Read more'):
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 4:
                    break

            if len(paragraphs) < 2:
                return None
            content = '\n\n'.join(paragraphs)
            content = clean_text(content)
            
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"Global Research парсинг: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        
        # AP News
        logger.info("📰 AP News...")
        try:
            resp = await asyncio.get_event_loop().run_in_executor(None, fetch_url, 'https://apnews.com/hub/world-news')
            if resp and resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                articles = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if '/article/' in href:
                        url = href if href.startswith('https') else 'https://apnews.com' + href
                        title = a.get_text(strip=True)
                        if len(title) < 20:
                            parent = a.find_parent(['h2', 'h3'])
                            if parent:
                                title = parent.get_text(strip=True)
                        if title and len(title) > 15:
                            title = clean_text(title)
                            articles.append({'url': url, 'title': title})
                
                seen = set()
                unique = []
                for a in articles:
                    if a['url'] not in seen:
                        seen.add(a['url'])
                        unique.append(a)
                
                for a in unique[:5]:
                    if not self._is_duplicate(a['url'], a['title']):
                        data = await self._parse_apnews_article(a['url'])
                        if data:
                            items.append(data)
                            logger.info(f"✅ AP: {data['title'][:40]}...")
        except Exception as e:
            logger.error(f"Ошибка сбора AP News: {e}")

        # InfoBrics
        logger.info("📰 InfoBrics...")
        try:
            feed = await asyncio.get_event_loop().run_in_executor(None, feedparser.parse, 'https://infobrics.org/rss/en')
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if not title or title in ['{[title]}', 'BRICS portal']:
                    summary = entry.get('summary', '')
                    if summary:
                        title = re.sub(r'<[^>]+>', '', summary)
                        title = title.split('.')[0][:100]
                if title and len(title) > 10:
                    title = clean_text(title)
                    if not self._is_duplicate(entry.link, title):
                        data = await self._parse_infobrics_article(entry.link)
                        if data:
                            items.append(data)
                            logger.info(f"✅ InfoBrics: {data['title'][:40]}...")
        except Exception as e:
            logger.error(f"Ошибка сбора InfoBrics: {e}")

        # Global Research
        logger.info("📰 Global Research...")
        try:
            feed = await asyncio.get_event_loop().run_in_executor(None, feedparser.parse, 'https://www.globalresearch.ca/feed')
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if title:
                    title = re.sub(r'\s*[-|]\s*Global Research$', '', title)
                    title = clean_text(title)
                    if not self._is_duplicate(entry.link, title):
                        data = await self._parse_globalresearch_article(entry.link)
                        if data:
                            items.append(data)
                            logger.info(f"✅ GR: {data['title'][:40]}...")
        except Exception as e:
            logger.error(f"Ошибка сбора Global Research: {e}")

        logger.info(f"📊 Новостей: {len(items)}")
        return items

    # ========== ПУБЛИКАЦИЯ ==========
    async def publish(self, post: dict):
        try:
            title_en = post.get('title', '')
            content_en = post.get('content', '')
            url = post.get('url', '')
            img = post.get('image')

            if not title_en or not content_en:
                logger.error("Нет заголовка или контента")
                return

            logger.info(f"📝 Перевод: {title_en[:40]}...")
            
            # Переводим с таймаутом
            title_ru = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, self._translate, title_en),
                timeout=TRANSLATION_TIMEOUT
            )
            content_ru = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, self._translate, content_en),
                timeout=TRANSLATION_TIMEOUT
            )

            title_ru = clean_text(title_ru)
            content_ru = clean_text(content_ru)

            # Сохраняем мета
            pid = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(pid, post['source'], url, title_en, content_en[:300])

            title_escaped = html.escape(title_ru)
            
            msg_text = self._truncate_text(content_ru, is_caption=True)
            message = f"*{title_escaped}*\n\n{msg_text}"

            if len(message) > MAX_CAPTION:
                title_len = len(f"*{title_escaped}*\n\n")
                max_text_len = MAX_CAPTION - title_len - 5
                msg_text = self._truncate_sentence(content_ru, max_text_len)
                message = f"*{title_escaped}*\n\n{msg_text}"

            # Публикация
            if img:
                logger.info(f"🖼️ Загрузка изображения...")
                resp = await asyncio.get_event_loop().run_in_executor(None, fetch_url, img)
                if resp and resp.status_code == 200 and 'image' in resp.headers.get('Content-Type', ''):
                    try:
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID, 
                            photo=resp.content, 
                            caption=message, 
                            parse_mode='Markdown'
                        )
                        logger.info("✅ С ФОТО")
                        self._mark_sent(url, title_en, content_en, img)
                        self._log_post(url, title_en)
                        return
                    except TelegramError as e:
                        if "caption is too long" in str(e).lower():
                            await self.bot.send_photo(
                                chat_id=CHANNEL_ID, 
                                photo=resp.content, 
                                caption=f"*{title_escaped}*", 
                                parse_mode='Markdown'
                            )
                            logger.info("✅ ФОТО (коротко)")
                            self._mark_sent(url, title_en, content_en, img)
                            self._log_post(url, title_en)
                            return
                        else:
                            logger.warning(f"Ошибка фото: {e}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            # Без фото
            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"*{title_escaped}*\n\n{text_content}"
            
            if len(text_message) > MAX_MESSAGE:
                title_len = len(f"*{title_escaped}*\n\n")
                max_text_len = MAX_MESSAGE - title_len - 10
                text_content = self._truncate_sentence(content_ru, max_text_len)
                text_message = f"*{title_escaped}*\n\n{text_content}"
            
            await self.bot.send_message(chat_id=CHANNEL_ID, text=text_message, parse_mode='Markdown')
            logger.info("✅ ТЕКСТОМ")
            self._mark_sent(url, title_en, content_en, img)
            self._log_post(url, title_en)

        except asyncio.TimeoutError:
            logger.error("Таймаут при публикации")
        except TelegramError as e:
            if "Can't parse entities" in str(e):
                logger.warning("Ошибка Markdown, отправляем без форматирования")
                try:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=f"{title_ru}\n\n{content_ru}", parse_mode=None)
                except Exception as e2:
                    logger.error(f"Критическая ошибка при отправке: {e2}")
            else:
                logger.error(f"Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 40)
        logger.info(f"🚀 Запуск [{get_local_time().strftime('%H:%M:%S')}]")
        try:
            news = await asyncio.wait_for(self.fetch_news(), timeout=120)
            if not news:
                logger.info("📭 Нет новостей")
                return
            if not self._can_post():
                logger.info("⏸️ Отложено")
                return
            await asyncio.wait_for(self.publish(news[0]), timeout=60)
        except asyncio.TimeoutError:
            logger.error("Таймаут выполнения run_once")
        except Exception as e:
            logger.error(f"Ошибка в run_once: {e}")

    async def run_forever(self):
        logger.info("🤖 Бот запущен")
        while True:
            try:
                await self.run_once()
                delay = random.randint(MIN_INTERVAL, MAX_INTERVAL)
                logger.info(f"⏰ Следующий через {delay // 60} мин")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"Критическая ошибка: {e}")
                await asyncio.sleep(300)


async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        logger.error("❌ Нет TELEGRAM_TOKEN или CHANNEL_ID")
        return
    bot = NewsBot()
    if 'GITHUB_ACTIONS' in os.environ:
        await bot.run_once()
    else:
        await bot.run_forever()


if __name__ == '__main__':
    asyncio.run(main())
