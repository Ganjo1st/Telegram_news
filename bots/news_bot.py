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
from urllib.parse import urljoin

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

MIN_INTERVAL = 2100
MAX_INTERVAL = 7200
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 1024
MAX_MESSAGE = 4096

IMAGE_HASH_CACHE = set()


def remove_ap_parentheses(text: str) -> str:
    if not text:
        return text
    pattern = r'\([^)]*[AaАа][PpРр][^)]*\)'
    cleaned = re.sub(pattern, '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = remove_ap_parentheses(text)
    text = re.sub(r'\([^)]*InfoBrics[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Global Research[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Photo[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
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


def extract_image_url(soup, base_url: str) -> str | None:
    """Извлекает URL изображения из страницы"""
    
    # 1. Open Graph image
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        url = meta_img['content']
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return urljoin(base_url, url)
        if url.startswith('http'):
            return url
    
    # 2. Twitter image
    meta_img = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta_img and meta_img.get('content'):
        url = meta_img['content']
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return urljoin(base_url, url)
        if url.startswith('http'):
            return url
    
    # 3. Поиск в статье
    container = soup.find('article') or soup.find('main') or soup.find('body')
    if container:
        for img in container.find_all('img', src=True):
            src = img.get('src', '')
            if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif']):
                continue
            if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                if src.startswith('//'):
                    return 'https:' + src
                if src.startswith('/'):
                    return urljoin(base_url, src)
                if src.startswith('http'):
                    return src
    
    return None


class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self._load_image_cache()

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
            return text[:pos].strip() + '...'
        return text[:limit].strip() + '...'

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        return self._truncate_sentence(text, max_len)

    def _translate(self, text: str) -> str:
        if not text or len(text) < 10:
            return text
        try:
            if len(text) > 3000:
                text = text[:3000]
            result = self.translator.translate(text)
            return clean_text(result) if result else text
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            return text

    # ========== AP NEWS ==========
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

                title = re.sub(r'\s+', ' ', title).strip()
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
            title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)
            title = title.strip()

            image_url = extract_image_url(soup, base_url)

            article = soup.find('article')
            if not article:
                article = soup.find('main')
            if not article:
                return None

            for tag in article.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                tag.decompose()

            for figure in article.find_all('figure'):
                figure.decompose()

            paragraphs = []
            for p in article.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 40:
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 8:
                    break

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = clean_text(content)

            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'AP News', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга AP News: {e}")
            return None

    # ========== INFOBRICS (УПРОЩЕННЫЙ) ==========
    def _get_infobrics_articles(self) -> list:
        try:
            feed = feedparser.parse('https://infobrics.org/rss/en')
            articles = []
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                if not title or title in ['{[title]}', 'BRICS portal', 'Портал БРИКС']:
                    summary = entry.get('summary', '')
                    if summary:
                        title = re.sub(r'<[^>]+>', '', summary)
                        title = title.split('.')[0][:150]
                        title = re.sub(r'\s*(?:BRICS|Portal|brics|portal|Портал БРИКС)\s*$', '', title, flags=re.IGNORECASE)
                if title and len(title) > 10:
                    title = clean_text(title)
                    articles.append({'url': entry.link, 'title': title})
                    logger.info(f"InfoBrics: найден заголовок '{title[:50]}'")
            return articles
        except Exception as e:
            logger.error(f"InfoBrics RSS: {e}")
            return []

    def _parse_infobrics_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            # ======== ЗАГОЛОВОК ========
            title = None
            
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                title = og['content']
            
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            
            if not title:
                title_div = soup.find('div', class_='title')
                if title_div:
                    title = title_div.get_text(strip=True)
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[|-]\s*(?:InfoBrics|INFOBRICS|BRICS portal|Портал БРИКС).*$', '', title, flags=re.IGNORECASE)
            
            if not title:
                logger.warning(f"InfoBrics: не удалось найти заголовок для {url}")
                return None
            
            title = clean_text(title)
            logger.info(f"InfoBrics: заголовок '{title[:50]}'")

            # ======== ИЗОБРАЖЕНИЕ ========
            image_url = extract_image_url(soup, base_url)
            if image_url:
                logger.info(f"InfoBrics: найдено изображение")

            # ======== КОНТЕНТ ========
            # Ищем контейнер с контентом
            content_div = soup.find('div', class_=re.compile(r'article__text|article-content|content|post|docs'))
            if not content_div:
                content_div = soup.find('article')
            if not content_div:
                content_div = soup.find('main')
            if not content_div:
                logger.warning(f"InfoBrics: не найден контейнер с контентом для {url}")
                return None

            # Удаляем мусор
            for tag in content_div.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'figure']):
                tag.decompose()

            # Собираем параграфы
            paragraphs = []
            for p in content_div.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 40 and not text.startswith('Read more'):
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 6:
                    break

            if len(paragraphs) < 2:
                # Пробуем найти контент в других местах
                for div in soup.find_all('div', class_=re.compile(r'text|body|content')):
                    for p in div.find_all('p'):
                        text = p.get_text(strip=True)
                        if len(text) > 40 and not text.startswith('Read more'):
                            text = clean_text(text)
                            if text:
                                paragraphs.append(text)
                    if len(paragraphs) >= 4:
                        break

            if len(paragraphs) < 2:
                logger.warning(f"InfoBrics: недостаточно контента для {url}")
                return None
            
            content = '\n\n'.join(paragraphs)
            content = clean_text(content)
            
            if len(content) < 150:
                logger.warning(f"InfoBrics: контент слишком короткий ({len(content)} символов)")
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"InfoBrics парсинг: {e}")
            return None

    # ========== GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                if title:
                    title = re.sub(r'\s*[-|]\s*Global Research$', '', title)
                    articles.append({'url': entry.link, 'title': clean_text(title)})
            return articles
        except Exception as e:
            logger.error(f"Global Research RSS: {e}")
            return []

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

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

            image_url = extract_image_url(soup, base_url)

            content_div = soup.find('div', class_=re.compile(r'entry-content|post-content'))
            if not content_div:
                content_div = soup.find('article')
            if not content_div:
                return None

            for figure in content_div.find_all('figure'):
                figure.decompose()

            paragraphs = []
            for p in content_div.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 40 and not text.startswith('Read more'):
                    text = clean_text(text)
                    if text:
                        paragraphs.append(text)
                if len(paragraphs) >= 6:
                    break

            if len(paragraphs) < 2:
                return None
            content = '\n\n'.join(paragraphs)
            content = clean_text(content)
            
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"Global Research парсинг: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        
        # 1. AP News
        logger.info("📰 Парсинг AP News...")
        ap_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_apnews_articles)
        for article in ap_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_apnews_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ AP News: {data['title'][:50]}...")
        
        # 2. InfoBrics
        logger.info("📰 Парсинг InfoBrics...")
        ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for article in ib_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ InfoBrics: {data['title'][:50]}...")
        
        # 3. Global Research
        logger.info("📰 Парсинг Global Research...")
        gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for article in gr_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_globalresearch_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ Global Research: {data['title'][:50]}...")
        
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

            logger.info(f"📝 Перевод: {title_en[:50]}...")

            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self._translate, title_en)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            title_ru = clean_text(title_ru)
            content_ru = clean_text(content_ru)

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

            if image_url:
                logger.info(f"🖼️ Загрузка изображения...")
                resp = await loop.run_in_executor(None, fetch_url, image_url)
                if resp and resp.status_code == 200 and 'image' in resp.headers.get('Content-Type', ''):
                    try:
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID, 
                            photo=resp.content, 
                            caption=message, 
                            parse_mode='Markdown'
                        )
                        logger.info("✅ С ФОТО")
                        self._mark_sent(url, title_en, content_en, image_url)
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
                            self._mark_sent(url, title_en, content_en, image_url)
                            self._log_post(url, title_en)
                            return
                        else:
                            logger.warning(f"Ошибка фото: {e}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"*{title_escaped}*\n\n{text_content}"
            
            if len(text_message) > MAX_MESSAGE:
                title_len = len(f"*{title_escaped}*\n\n")
                max_text_len = MAX_MESSAGE - title_len - 10
                text_content = self._truncate_sentence(content_ru, max_text_len)
                text_message = f"*{title_escaped}*\n\n{text_content}"
            
            await self.bot.send_message(chat_id=CHANNEL_ID, text=text_message, parse_mode='Markdown')
            logger.info("✅ ТЕКСТОМ")
            self._mark_sent(url, title_en, content_en, image_url)
            self._log_post(url, title_en)

        except TelegramError as e:
            if "Can't parse entities" in str(e):
                logger.warning("Ошибка Markdown, отправка без форматирования")
                try:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=f"{title_ru}\n\n{content_ru}", parse_mode=None)
                except Exception as e2:
                    logger.error(f"Ошибка: {e2}")
            else:
                logger.error(f"Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")

    async def run_once(self):
        logger.info("=" * 40)
        logger.info(f"🚀 Запуск [{get_local_time().strftime('%H:%M:%S')}]")
        try:
            news = await self.fetch_news()
            if not news:
                logger.info("📭 Нет новостей")
                return
            if not self._can_post():
                logger.info("⏸️ Отложено")
                return
            await self.publish(news[0])
        except Exception as e:
            logger.error(f"Ошибка: {e}")

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
