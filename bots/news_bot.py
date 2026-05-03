#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей
Источники: AP News, InfoBrics, Global Research
"""

import os
import json
import logging
import asyncio
import hashlib
import re
import html
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('news_bot')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

# Интервалы публикации (секунды)
MIN_INTERVAL = 2100  # 35 минут
MAX_INTERVAL = 7200  # 2 часа
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 1024  # Максимальная длина подписи к фото в Telegram
MAX_MESSAGE = 4096  # Максимальная длина сообщения

# Для кэширования изображений
IMAGE_HASH_CACHE = set()

# Паттерны для удаления упоминаний источников и другого мусора
SOURCE_PATTERNS = [
    r'\(AP\)', r'\(АР\)', r'\(AP News\)', r'\(Associated Press\)', r'\(Ассошиэйтед Пресс\)',
    r'— AP News$', r'\| AP News', r'AP News —',
    r'— Global Research$', r'\| Global Research', r'Global Research —', r'– Global Research',
    r'— InfoBrics$', r'\| InfoBrics', r'InfoBrics —',
    r'Источник:\s*\S+', r'По материалам\s*\S+',
    r'Photo by\s+\S+', r'Credit:\s*\S+', r'Image:\s*\S+',
    r'Read more:', r'Read more at:', r'Continue reading:',
    r'Click here to read more', r'View original post',
]


def clean_title(title: str) -> str:
    """Очищает заголовок от смайлов, эмодзи и упоминаний источников"""
    if not title:
        return ""
    
    # Удаляем эмодзи и смайлы (включая флаги, символы, иероглифы)
    # Паттерн для удаления любых эмодзи/смайлов
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # Эмодзи смайликов
        "\U0001F300-\U0001F5FF"  # Символы и пиктограммы
        "\U0001F680-\U0001F6FF"  # Транспорт и карты
        "\U0001F700-\U0001F77F"  # Алхимические символы
        "\U0001F780-\U0001F7FF"  # Геометрические фигуры
        "\U0001F800-\U0001F8FF"  # Дополнительные стрелки
        "\U0001F900-\U0001F9FF"  # Дополнительные символы и эмодзи
        "\U0001FA00-\U0001FA6F"  # Дополнительные пиктограммы
        "\U0001FA70-\U0001FAFF"  # Дополнительные символы
        "\U00002702-\U000027B0"  # Декоративные символы
        "\U000024C2-\U0001F251"  # Зашифрованные символы
        "]+",
        flags=re.UNICODE
    )
    title = emoji_pattern.sub('', title)
    
    # Удаляем упоминания источников
    for pattern in SOURCE_PATTERNS:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    
    # Удаляем лишние пробелы и знаки в начале/конце
    title = re.sub(r'\s+', ' ', title)
    title = title.strip(' -|:')
    
    return title


def clean_content(text: str) -> str:
    """Очищает текст от упоминаний источников"""
    if not text:
        return text
    
    for pattern in SOURCE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    # Удаляем строки с email, ссылками, копирайтами
    text = re.sub(r'\S+@\S+\.\S+', '', text)
    text = re.sub(r'©\s*\d{4}\s+\S+', '', text)
    
    # Удаляем лишние пробелы
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    return text


def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None


def hash_image_url(url: str) -> str:
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def is_image_duplicate(image_url: str) -> bool:
    if not image_url:
        return True
    return hash_image_url(image_url) in IMAGE_HASH_CACHE


def mark_image_used(image_url: str):
    if image_url:
        IMAGE_HASH_CACHE.add(hash_image_url(image_url))


def extract_image_url_enhanced(soup, base_url: str, url: str = None) -> str | None:
    """Извлекает URL изображения из страницы"""
    
    # 1. Open Graph image
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        img_url = meta_img['content']
        if img_url.startswith('//'):
            img_url = 'https:' + img_url
        elif img_url.startswith('/'):
            img_url = urljoin(base_url, img_url)
        if img_url.startswith('http'):
            return img_url
    
    # 2. Twitter image
    twitter_img = soup.find('meta', attrs={'name': 'twitter:image'})
    if twitter_img and twitter_img.get('content'):
        img_url = twitter_img['content']
        if img_url.startswith('//'):
            img_url = 'https:' + img_url
        elif img_url.startswith('/'):
            img_url = urljoin(base_url, img_url)
        if img_url.startswith('http'):
            return img_url
    
    # 3. Поиск больших изображений
    best_img = None
    max_size = 0
    
    for img in soup.find_all('img', src=True):
        src = img.get('src', '')
        if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif', 'pixel', '1x1']):
            continue
        
        width = img.get('width', '')
        height = img.get('height', '')
        if width and height:
            try:
                w = int(width)
                h = int(height)
                if w >= 400 and h >= 300:
                    if w * h > max_size:
                        max_size = w * h
                        if src.startswith('//'):
                            best_img = 'https:' + src
                        elif src.startswith('/'):
                            best_img = urljoin(base_url, src)
                        else:
                            best_img = src
            except:
                pass
    
    if best_img:
        return best_img
    
    # 4. Figure с изображением
    figure = soup.find('figure')
    if figure:
        img = figure.find('img')
        if img and img.get('src'):
            src = img['src']
            if src.startswith('//'):
                return 'https:' + src
            if src.startswith('/'):
                return urljoin(base_url, src)
            if src.startswith('http'):
                return src
    
    return None


# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self._load_image_cache()

    def _load_image_cache(self):
        global IMAGE_HASH_CACHE
        if 'used_images' in self.state:
            IMAGE_HASH_CACHE = set(self.state['used_images'])
        else:
            self.state['used_images'] = []
            IMAGE_HASH_CACHE = set()

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

    def _add_to_meta(self, post_id: str, source: str, url: str, title: str, content_preview: str = ""):
        """Сохраняет метаданные статьи в posts_meta.json"""
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
            return True
        norm_title = self._normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            return True
        if content:
            h = self._hash_content(content)
            if h and h in self.state['sent_hashes']:
                return True
        return False

    def _mark_sent(self, url: str, title: str, content: str = "", image_url: str = None):
        self.state['sent_links'].add(url)
        norm_title = self._normalize_title(title)
        if norm_title:
            self.state['sent_titles'].add(norm_title)
        if content:
            h = self._hash_content(content)
            if h:
                self.state['sent_hashes'].add(h)
        if image_url:
            mark_image_used(image_url)
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

    def _truncate_to_last_sentence(self, text: str, max_len: int) -> str:
        """Обрезает текст до последнего полного предложения"""
        if len(text) <= max_len:
            return text

        # Ищем конец предложения
        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 2:
                result = text[:last + 1].strip()
                if result and result[-1] in '.!?':
                    return result

        # Если нет знака препинания, обрезаем по слову
        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1:
            result = text[:last_space].strip()
            if result and not result[-1] in '.!?':
                result = result + '.'
            return result

        result = text[:max_len].strip()
        if result and not result[-1] in '.!?':
            result = result + '.'
        return result

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        """Обрезает текст, сохраняя целые абзацы и предложения"""
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        max_len = max_len - 100  # Запас для заголовка и форматирования
        
        paragraphs = re.split(r'\n\s*\n', text)
        result_paragraphs = []
        current_length = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            if current_length + len(para) + 2 <= max_len:
                result_paragraphs.append(para)
                current_length += len(para) + 2
            else:
                if not result_paragraphs:
                    return self._truncate_to_last_sentence(para, max_len)
                break
        
        if result_paragraphs:
            result = '\n\n'.join(result_paragraphs)
            if len(result) <= max_len:
                # Убедимся, что заканчивается на знак препинания
                if result and not result[-1] in '.!?':
                    result = result + '.'
                return result
        
        return self._truncate_to_last_sentence(text, max_len)

    def _translate(self, text: str) -> str:
        if not text or len(text) < 10:
            return text
        try:
            if len(text) > 3000:
                text = text[:3000]
            result = self.translator.translate(text)
            return clean_content(result) if result else text
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            return text

    # ========== ПАРСИНГ AP NEWS ==========
    def _get_apnews_articles(self) -> list:
        try:
            resp = fetch_url('https://apnews.com/')
            if not resp or resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, 'html.parser')
            articles = []

            for link in soup.find_all('a', href=True):
                href = link['href']
                if '/article/' not in href:
                    continue

                if href.startswith('https://'):
                    url = href
                elif href.startswith('/'):
                    url = 'https://apnews.com' + href
                else:
                    continue

                title = link.get_text(strip=True)
                if not title or len(title) < 15:
                    parent = link.find_parent(['h1', 'h2', 'h3', 'h4'])
                    if parent:
                        title = parent.get_text(strip=True)
                if not title or len(title) < 15:
                    continue

                title = clean_title(title)
                articles.append({'url': url, 'title': title})

            seen = set()
            unique = []
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)
            return unique[:10]
        except Exception as e:
            logger.error(f"Ошибка AP News: {e}")
            return []

    def _parse_apnews_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            # Заголовок
            title = None
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            if not title:
                return None
            
            title = clean_title(title)

            # Изображение
            image_url = extract_image_url_enhanced(soup, base_url, url)
            if image_url and is_image_duplicate(image_url):
                image_url = None

            # Контент
            container = soup.find('article') or soup.find('main')
            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 40:
                        text = clean_content(text)
                        if text:
                            paragraphs.append(text)

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = clean_content(content)

            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'AP News', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга AP News: {e}")
            return None

    # ========== ПАРСИНГ INFOBRICS ==========
    def _get_infobrics_articles(self) -> list:
        try:
            feed = feedparser.parse('https://infobrics.org/rss/en')
            articles = []
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if not title or title == '{[title]}' or title.lower() == 'brics portal':
                    summary = entry.get('summary', '')
                    if summary:
                        title = re.sub(r'<[^>]+>', '', summary)
                        title = title.split('.')[0][:100]
                        title = re.sub(r'\s*(?:BRICS|Portal|brics|portal)\s*$', '', title)
                
                if not title or len(title) < 10:
                    continue
                
                title = clean_title(title)
                articles.append({'url': entry.link, 'title': title})
                logger.info(f"InfoBrics RSS: '{title[:50]}'")
            return articles
        except Exception as e:
            logger.error(f"Ошибка InfoBrics RSS: {e}")
            return []

    def _parse_infobrics_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            # Поиск заголовка
            title = None
            
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[|-]\s*(?:InfoBrics|INFOBRICS|BRICS portal).*$', '', title, flags=re.IGNORECASE)
            
            if not title or title.lower() in ['brics portal', 'portal', 'infobrics']:
                return None
            
            title = clean_title(title)

            # Изображение
            image_url = extract_image_url_enhanced(soup, base_url, url)
            if image_url and is_image_duplicate(image_url):
                image_url = None

            # Контент
            container = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'content|article'))
            paragraphs = []
            if container:
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more'):
                        text = clean_content(text)
                        if text:
                            paragraphs.append(text)

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = clean_content(content)

            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСИНГ GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if not title:
                    continue
                
                title = re.sub(r'\s*[-|]\s*Global Research$', '', title, flags=re.IGNORECASE)
                title = clean_title(title)
                
                articles.append({'url': entry.link, 'title': title})
                logger.info(f"Global Research RSS: '{title[:50]}'")
            return articles
        except Exception as e:
            logger.error(f"Ошибка Global Research RSS: {e}")
            return []

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            # Поиск заголовка
            title = None
            
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[-|]\s*Global Research.*$', '', title, flags=re.IGNORECASE)
            
            if not title:
                return None
            
            title = clean_title(title)

            # Изображение
            image_url = extract_image_url_enhanced(soup, base_url, url)
            if image_url and is_image_duplicate(image_url):
                image_url = None

            # Контент
            container = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'entry-content|post-content'))
            paragraphs = []
            if container:
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more'):
                        text = clean_content(text)
                        if text:
                            paragraphs.append(text)

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = clean_content(content)

            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга Global Research: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        
        # AP News
        logger.info("📰 Парсинг AP News...")
        ap_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_apnews_articles)
        for article in ap_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_apnews_article, article['url'])
            if data:
                items.append(data)
                logger.info(f"✅ AP News: {data['title'][:50]}...")
        
        # InfoBrics
        logger.info("📰 Парсинг InfoBrics...")
        ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for article in ib_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
            if data:
                items.append(data)
                logger.info(f"✅ InfoBrics: {data['title'][:50]}...")
        
        # Global Research
        logger.info("📰 Парсинг Global Research...")
        gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for article in gr_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_globalresearch_article, article['url'])
            if data:
                items.append(data)
                logger.info(f"✅ Global Research: {data['title'][:50]}...")
        
        logger.info(f"📊 Новых статей: {len(items)}")
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

            logger.info(f"📝 Перевод: {title_en[:50]}...")

            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self._translate, title_en)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            # Очистка переведенного текста
            title_ru = clean_title(title_ru)
            content_ru = clean_content(content_ru)

            # Сохраняем метаданные
            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, post.get('source', ''), url, title_en, content_en)

            # Формируем сообщение
            title_escaped = html.escape(title_ru)
            
            # Обрезаем текст для подписи к фото (максимум 1024 символа)
            content_for_caption = self._truncate_text(content_ru, is_caption=True)
            message = f"📰 *{title_escaped}*\n\n{content_for_caption}"
            
            # Проверяем длину сообщения
            if len(message) > MAX_CAPTION:
                # Уменьшаем текст
                content_for_caption = self._truncate_to_last_sentence(content_ru, MAX_CAPTION - len(f"📰 *{title_escaped}*\n\n") - 10)
                message = f"📰 *{title_escaped}*\n\n{content_for_caption}"

            # Публикация с фото
            if image_url:
                logger.info(f"🖼️ Загрузка изображения...")
                img_response = fetch_url(image_url, timeout=15)
                
                if img_response and img_response.status_code == 200:
                    content_type = img_response.headers.get('Content-Type', '')
                    if 'image' in content_type:
                        try:
                            await self.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=img_response.content,
                                caption=message,
                                parse_mode='Markdown'
                            )
                            logger.info("✅ Опубликовано С ФОТО")
                            self._mark_sent(url, title_en, content_en, image_url)
                            self._log_post(url, title_en)
                            return
                        except TelegramError as e:
                            if "caption is too long" in str(e).lower():
                                # Пробуем с еще более коротким текстом
                                shorter_msg = f"📰 *{title_escaped}*"
                                await self.bot.send_photo(
                                    chat_id=CHANNEL_ID,
                                    photo=img_response.content,
                                    caption=shorter_msg,
                                    parse_mode='Markdown'
                                )
                                logger.info("✅ Опубликовано С ФОТО (короткий текст)")
                                self._mark_sent(url, title_en, content_en, image_url)
                                self._log_post(url, title_en)
                                return
                            else:
                                logger.warning(f"Ошибка фото: {e}")
                    else:
                        logger.warning(f"Не изображение: {content_type}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            # Публикация текстом
            logger.info("📝 Публикация текстом")
            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"📰 *{title_escaped}*\n\n{text_content}"
            
            if len(text_message) > MAX_MESSAGE:
                text_content = self._truncate_to_last_sentence(content_ru, MAX_MESSAGE - len(f"📰 *{title_escaped}*\n\n") - 10)
                text_message = f"📰 *{title_escaped}*\n\n{text_content}"
            
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_message,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            logger.info("✅ Опубликовано ТЕКСТОМ")

            self._mark_sent(url, title_en, content_en, image_url)
            self._log_post(url, title_en)

        except TelegramError as e:
            error_msg = str(e)
            if "Can't parse entities" in error_msg:
                logger.warning("Ошибка Markdown, отправляем без форматирования")
                try:
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=f"📰 {title_ru}\n\n{content_ru}",
                        parse_mode=None
                    )
                    self._mark_sent(url, title_en, content_en, image_url)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"Ошибка: {e2}")
            else:
                logger.error(f"Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 50)
        logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
        logger.info("=" * 50)

        news = await self.fetch_news()

        if not news:
            logger.info("📭 Новых статей нет")
            return

        if not self._can_post():
            logger.info("⏸️ Публикация отложена")
            return

        await self.publish(news[0])

    async def run_forever(self):
        logger.info("🤖 Бот запущен")
        while True:
            try:
                await self.run_once()
                delay = self._next_delay()
                logger.info(f"⏰ Следующий запуск через {delay // 60} минут")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"❌ Критическая ошибка: {e}")
                await asyncio.sleep(300)


async def main():
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан")
        return
    if not CHANNEL_ID:
        logger.error("❌ CHANNEL_ID не задан")
        return

    bot = NewsBot()
    if 'GITHUB_ACTIONS' in os.environ:
        await bot.run_once()
    else:
        await bot.run_forever()


if __name__ == '__main__':
    asyncio.run(main())
