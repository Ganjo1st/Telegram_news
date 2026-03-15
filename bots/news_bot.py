#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Чистые статьи, антидубликат, умное обрезание до последней точки
"""

import os
import sys
import json
import logging
import asyncio
import hashlib
import re
import html
import random
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import aiohttp
import tempfile

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('news_bot')

# ========== КОНФИГУРАЦИЯ ==========
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

# Хаотичный режим (в секундах)
MIN_POST_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '2100'))      # 35 минут
MAX_POST_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '7200'))      # 2 часа
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '24'))
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '7'))

# Таймауты
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))
PUBLISH_TIMEOUT = int(os.getenv('PUBLISH_TIMEOUT', '30'))

# Файл состояния
STATE_FILE = os.getenv('STATE_FILE', 'state_news_bot.json')

# ========== ИСТОЧНИКИ ==========
ALL_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',
        'type': 'rss',
        'priority': 1
    },
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch',
        'type': 'rss',
        'priority': 2
    },
    {
        'name': 'AP News',
        'url': 'https://apnews.com/',
        'enabled': True,
        'type': 'html_apnews_v2',
        'priority': 1
    }
]

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def run_with_timeout(coro, timeout, default=None):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"❌ Таймаут {timeout}с")
        return default

def fetch_with_timeout(func, timeout, *args, **kwargs):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.error(f"❌ Таймаут функции {func.__name__}")
            return None

def get_local_time() -> datetime:
    """Возвращает текущее время в UTC+7"""
    utc_now = datetime.now(timezone.utc)
    local_now = utc_now + timedelta(hours=TIMEZONE_OFFSET)
    return local_now

def format_local_time(dt: datetime) -> str:
    """Форматирует время в читаемый вид"""
    return dt.strftime('%d.%m.%Y %H:%M:%S')

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state_file = STATE_FILE
        self.state = self.load_state()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.session = None
        self.last_post_time = None
        self.next_post_time = None

    # ========== РАБОТА С СОСТОЯНИЕМ ==========
    def load_state(self) -> dict:
        """Загружает состояние из файла"""
        default = {
            'sent_links': [],
            'sent_hashes': [],
            'sent_titles': [],
            'posts_log': []
        }
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    # Преобразуем списки в множества для быстрой проверки
                    return {
                        'sent_links': set(state.get('sent_links', [])),
                        'sent_hashes': set(state.get('sent_hashes', [])),
                        'sent_titles': set(state.get('sent_titles', [])),
                        'posts_log': state.get('posts_log', [])
                    }
            else:
                logger.info(f"📁 Файл {self.state_file} не найден, создаю новый")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки: {e}")
        
        return {
            'sent_links': set(),
            'sent_hashes': set(),
            'sent_titles': set(),
            'posts_log': []
        }

    def save_state(self):
        """Сохраняет состояние в файл"""
        try:
            state_to_save = {
                'sent_links': list(self.state['sent_links']),
                'sent_hashes': list(self.state['sent_hashes']),
                'sent_titles': list(self.state['sent_titles']),
                'posts_log': self.state['posts_log']
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state_to_save, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Состояние сохранено в {self.state_file}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения: {e}")

    # ========== ДЕДУПЛИКАЦИЯ ==========
    def normalize_title(self, title: str) -> str:
        """Нормализует заголовок для сравнения"""
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        # Удаляем общие слова
        common_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by']
        words = [w for w in title.split() if w not in common_words]
        return ' '.join(words)[:100]

    def create_content_hash(self, content: str) -> str:
        """Создает хеш содержимого"""
        if not content:
            return ""
        return hashlib.md5(content[:500].encode('utf-8')).hexdigest()

    def is_duplicate(self, url: str, title: str, content: str = "") -> bool:
        """Трехуровневая проверка на дубликат"""
        if url in self.state['sent_links']:
            logger.info(f"⏭️ Дубликат URL: {title[:50]}...")
            return True
            
        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            logger.info(f"⏭️ Дубликат заголовка: {title[:50]}...")
            return True
            
        if content:
            h = self.create_content_hash(content)
            if h and h in self.state['sent_hashes']:
                logger.info(f"⏭️ Дубликат содержимого: {title[:50]}...")
                return True
                
        return False

    def mark_as_sent(self, url: str, title: str, content: str = ""):
        """Помечает статью как отправленную"""
        self.state['sent_links'].add(url)
        
        norm_title = self.normalize_title(title)
        if norm_title:
            self.state['sent_titles'].add(norm_title)
            
        if content:
            h = self.create_content_hash(content)
            if h:
                self.state['sent_hashes'].add(h)
                
        self.save_state()

    def log_post(self, link: str, title: str):
        """Логирует опубликованный пост"""
        local_time = get_local_time()
        self.state['posts_log'].append({
            'link': link,
            'title': title[:50],
            'time': local_time.isoformat()
        })
        # Оставляем только последние 100 записей
        if len(self.state['posts_log']) > 100:
            self.state['posts_log'] = self.state['posts_log'][-100:]
        self.save_state()
        self.last_post_time = local_time

    # ========== ХАОТИЧНЫЙ РЕЖИМ ==========
    def can_post_now(self) -> bool:
        """Проверяет, можно ли публиковать сейчас"""
        local_now = get_local_time()
        
        # Проверка ночного времени
        hour = local_now.hour
        if 23 <= hour or hour < 7:
            logger.info(f"🌙 Ночное время ({hour}:00), пропускаю")
            return False

        # Проверка дневного лимита
        today = local_now.date()
        today_posts = 0
        last_posts = []
        
        for post in self.state['posts_log']:
            try:
                post_time = datetime.fromisoformat(post['time'])
                if post_time.date() == today:
                    today_posts += 1
                    last_posts.append(post_time)
            except:
                continue

        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Дневной лимит {MAX_POSTS_PER_DAY} достигнут")
            return False

        # Проверка минимального интервала
        if last_posts:
            last_posts.sort(reverse=True)
            time_since_last = (local_now - last_posts[0]).total_seconds()
            if time_since_last < MIN_POST_INTERVAL:
                wait_minutes = (MIN_POST_INTERVAL - time_since_last) / 60
                logger.info(f"⏳ Минимальный интервал: следующий пост через {wait_minutes:.0f} минут")
                return False

        return True

    def get_next_delay(self) -> int:
        """Возвращает случайную задержку между MIN и MAX"""
        delay = random.randint(MIN_POST_INTERVAL, MAX_POST_INTERVAL)
        # Добавляем случайную вариацию ±15%
        variation = random.uniform(0.85, 1.15)
        delay = int(delay * variation)
        # Ограничиваем рамками
        delay = max(MIN_POST_INTERVAL, min(delay, MAX_POST_INTERVAL))
        return delay

    # ========== ОЧИСТКА СТАТЬИ ==========
    def clean_article(self, text: str) -> str:
        """Полная очистка статьи от служебной информации"""
        if not text:
            return ""
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Удаляем мета-информацию
        patterns = [
            r'\d+\s*(hour|min|sec|day|minute|second)s?\s+ago',
            r'Updated\s*:?\s*[\d:APM\s-]+',
            r'Published\s*:?\s*[\d:APM\s-]+',
            r'^By\s+[\w\s,]+\n',
            r'^\([A-Z]+\)\s+',
            r'—\s+(AP|Reuters|AFP)',
            r'Слушайте\s+в\s+Apple\s+Podcasts',
            r'Подписывайтесь\s+на\s+наш\s+канал',
            r'Читайте\s+нас\s+в\s+Telegram',
            r'Следите\s+за\s+нами\s+в\s+соцсетях',
            r'Оставить\s+комментарий',
            r'Поделиться\s+новостью',
            r'Morning Wire',
            r'Afternoon Wire',
            r'Daily Brief',
            r'Newsletter',
            r'Sign up',
            r'Subscribe',
            r'Follow us',
            r'Read more',
            r'Share this',
            r'Advertisement',
            r'Реклама'
        ]
        
        for pattern in patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
        
        # Убираем лишние переносы
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        return text.strip()

    def get_first_sentence(self, text: str) -> str:
        """Извлекает первое предложение из текста"""
        if not text:
            return ""
        # Ищем конец первого предложения
        match = re.search(r'^.*?[.!?]', text)
        if match:
            return match.group(0).strip()
        # Если нет знаков препинания, берем первые 100 символов
        return text[:100].strip() + "..."

    # ========== УМНОЕ ОБРЕЗАНИЕ ТЕКСТА ==========
    def smart_truncate(self, text: str, max_length: int) -> str:
        """
        Обрезает текст до последней точки, не превышая max_length
        """
        if len(text) <= max_length:
            return text
        
        # Ищем последнюю точку в пределах max_length
        last_dot = text.rfind('.', 0, max_length)
        last_excl = text.rfind('!', 0, max_length)
        last_question = text.rfind('?', 0, max_length)
        
        # Берем самый последний знак препинания
        last_punct = max(last_dot, last_excl, last_question)
        
        if last_punct > 0:
            # Обрезаем до последнего знака препинания + 1 (чтобы включить его)
            truncated = text[:last_punct + 1]
            logger.info(f"✂️ Текст обрезан до {len(truncated)} символов (до последней точки)")
            return truncated
        else:
            # Если нет знаков препинания, обрезаем до последнего пробела
            last_space = text.rfind(' ', 0, max_length)
            if last_space > 0:
                truncated = text[:last_space] + "..."
                logger.info(f"✂️ Текст обрезан до {len(truncated)} символов (до пробела)")
                return truncated
            else:
                # В крайнем случае просто обрезаем
                truncated = text[:max_length - 3] + "..."
                logger.info(f"✂️ Текст обрезан принудительно до {len(truncated)} символов")
                return truncated

    # ========== ПАРСЕРЫ ==========
    def get_apnews_articles(self):
        """Получает список статей с AP News"""
        try:
            logger.info("🌐 Парсинг AP News")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = fetch_with_timeout(
                lambda: requests.get('https://apnews.com/', headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []

            for link in soup.find_all('a', href=True):
                href = link['href']
                if '/article/' not in href:
                    continue

                # Формируем URL
                if href.startswith('https://apnews.com/'):
                    url = href
                elif href.startswith('/'):
                    url = 'https://apnews.com' + href
                else:
                    continue

                # Заголовок
                title = link.get_text(strip=True)
                if not title or len(title) < 15:
                    parent = link.find_parent(['h1', 'h2', 'h3', 'h4'])
                    if parent:
                        title = parent.get_text(strip=True)
                if not title or len(title) < 15:
                    continue

                title = re.sub(r'\s+', ' ', title).strip()
                if any(word in title.lower() for word in ['newsletter', 'subscribe', 'sign up']):
                    continue

                articles.append({'url': url, 'title': title})

            # Убираем дубликаты URL
            unique = []
            seen = set()
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)

            return unique[:10]
        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    def parse_apnews_article(self, url: str):
        """Парсит отдельную статью AP News"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

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
            title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст статьи
            article_text = ""
            container = soup.find('article') or soup.find('main') or soup.body
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    tag.decompose()
                
                paragraphs = []
                for p in container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 20:
                        paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            # Очищаем
            article_text = self.clean_article(article_text)

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи: {e}")
            return None

    def parse_infobrics(self, url: str):
        """Парсит статью InfoBrics"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = title_elem.get_text(strip=True)

            # Изображение
            main_image = None
            img = soup.find('img', class_=re.compile(r'article.*image'))
            if img and img.get('src'):
                src = img['src']
                if src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{src}"
                elif not src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{src}"
                else:
                    main_image = src

            # Текст
            article_text = ""
            container = soup.find('div', class_=re.compile(r'article__text')) or soup.find('div', class_=re.compile(r'article'))
            if container:
                for tag in container.find_all(['script', 'style', 'button']):
                    tag.decompose()
                paragraphs = []
                for p in container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 15:
                        paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            article_text = self.clean_article(article_text)

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка InfoBrics: {e}")
            return None

    def parse_globalresearch(self, url: str):
        """Парсит статью Global Research"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('h1') or soup.find('title')
            if title_elem:
                title = title_elem.get_text(strip=True)

            # Изображение
            main_image = None
            img = soup.find('img', class_=re.compile(r'featured|wp-post-image'))
            if img and img.get('src'):
                src = img['src']
                if src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{src}"
                elif not src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{src}"
                else:
                    main_image = src

            # Текст
            article_text = ""
            container = soup.find('div', class_=re.compile(r'entry-content|post-content'))
            if container:
                for tag in container.find_all(['script', 'style', 'button']):
                    tag.decompose()
                paragraphs = []
                for p in container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 15:
                        paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            article_text = self.clean_article(article_text)

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка Global Research: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_from_apnews(self):
        """Собирает новости с AP News"""
        items = []
        try:
            articles = await asyncio.get_event_loop().run_in_executor(None, self.get_apnews_articles)
            for article in articles[:3]:  # Берем первые 3
                url, title = article['url'], article['title']
                if self.is_duplicate(url, title):
                    continue
                logger.info(f"🔍 AP News: {title[:50]}...")
                data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, url
                )
                if data and not self.is_duplicate(url, data['title'], data['content']):
                    items.append({
                        'source': 'AP News',
                        'title': data['title'],
                        'content': data['content'],
                        'link': url,
                        'main_image': data.get('main_image'),
                        'priority': 1
                    })
                await asyncio.sleep(random.randint(2, 4))
        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
        return items

    async def fetch_from_rss(self, feed_config):
        """Собирает новости из RSS"""
        items = []
        try:
            name = feed_config['name']
            parser = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)
            
            parser_func = self.parse_infobrics if parser == 'infobrics' else self.parse_globalresearch
            
            feed = await asyncio.get_event_loop().run_in_executor(None, feedparser.parse, feed_config['url'])
            if feed.bozo:
                logger.warning(f"⚠️ Ошибка RSS {name}: {feed.bozo_exception}")
                return []

            for entry in feed.entries[:3]:  # Берем первые 3
                link = entry.get('link', '')
                title = entry.get('title', '')
                if self.is_duplicate(link, title):
                    continue
                logger.info(f"🔍 {name}: {title[:50]}...")
                data = await asyncio.get_event_loop().run_in_executor(None, parser_func, link)
                if data and not self.is_duplicate(link, data['title'], data['content']):
                    items.append({
                        'source': name,
                        'title': data['title'],
                        'content': data['content'],
                        'link': link,
                        'main_image': data.get('main_image'),
                        'priority': priority
                    })
                await asyncio.sleep(random.randint(2, 4))
        except Exception as e:
            logger.error(f"❌ Ошибка {feed_config['name']}: {e}")
        return items

    async def fetch_all_news(self):
        """Собирает новости из всех источников"""
        all_news = []
        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue
            if feed.get('type') == 'html_apnews_v2':
                news = await self.fetch_from_apnews()
            else:
                news = await self.fetch_from_rss(feed)
            all_news.extend(news)
            await asyncio.sleep(random.randint(3, 5))
        
        # Сортируем по приоритету
        all_news.sort(key=lambda x: x.get('priority', 5))
        logger.info(f"📊 Всего новых: {len(all_news)}")
        return all_news

    # ========== ПУБЛИКАЦИЯ ==========
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def download_image(self, url: str):
        """Скачивает изображение, если есть"""
        if not url:
            return None
        try:
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            session = await self.get_session()
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    with open(path, 'wb') as f:
                        f.write(await response.read())
                    return path
        except Exception as e:
            logger.error(f"Ошибка скачивания: {e}")
        return None

    def translate_text_safe(self, text: str) -> str:
        """Безопасный перевод с обработкой ошибок и ограничением длины"""
        if not text or len(text) < 20:
            return text
        
        try:
            # Ограничиваем длину текста для перевода
            if len(text) > 4000:
                # Переводим по частям
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        translated = self.translator.translate(part)
                        if translated:
                            parts.append(translated)
                        else:
                            parts.append(part)
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка перевода части: {e}")
                        parts.append(part)
                    time.sleep(random.uniform(0.5, 1))
                return ' '.join(parts)
            else:
                return self.translator.translate(text)
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    def escape_html(self, text: str) -> str:
        """Экранирует HTML для Telegram"""
        if not text:
            return ""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    async def create_and_publish(self, item: dict) -> bool:
        """Создает и публикует пост с умным обрезанием"""
        try:
            logger.info(f"\n📝 Публикация: {item['title'][:70]}...")
            
            # Перевод в потоке
            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self.translate_text_safe, item['title'])
            content_ru = await loop.run_in_executor(None, self.translate_text_safe, item['content'])
            
            # Если заголовок пустой после перевода, берем первое предложение из текста
            if not title_ru or title_ru == "Без заголовка":
                title_ru = self.get_first_sentence(content_ru)
            
            # Экранируем
            title_esc = self.escape_html(title_ru)
            content_esc = self.escape_html(content_ru)
            
            # Формируем полный текст поста
            full_text = f"<b>{title_esc}</b>\n\n{content_esc}"
            
            # Максимальная длина для Telegram (1024 символа)
            MAX_LENGTH = 1024
            
            if len(full_text) > MAX_LENGTH:
                logger.info(f"📏 Длина текста {len(full_text)} символов, требуется обрезание")
                
                # Обрезаем текст умно
                text_without_title = content_esc
                truncated_text = self.smart_truncate(text_without_title, MAX_LENGTH - len(title_esc) - 4)
                full_text = f"<b>{title_esc}</b>\n\n{truncated_text}"
                
                logger.info(f"✅ Текст обрезан до {len(full_text)} символов")
            
            # Скачиваем изображение (если есть)
            image_path = None
            if item.get('main_image'):
                logger.info("🖼️ Скачивание изображения...")
                image_path = await self.download_image(item['main_image'])
            
            # Публикуем
            try:
                if image_path:
                    with open(image_path, 'rb') as photo:
                        await run_with_timeout(
                            self.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=photo,
                                caption=full_text,
                                parse_mode='HTML'
                            ),
                            PUBLISH_TIMEOUT
                        )
                    os.unlink(image_path)
                    logger.info("✅ Пост с фото опубликован")
                else:
                    await run_with_timeout(
                        self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=full_text,
                            parse_mode='HTML'
                        ),
                        PUBLISH_TIMEOUT
                    )
                    logger.info("✅ Пост без фото опубликован")
                
                # Отмечаем как отправленное
                self.mark_as_sent(item['link'], item['title'], item['content'])
                self.log_post(item['link'], item['title'])
                
                # Вычисляем время следующей публикации
                next_delay = self.get_next_delay()
                local_now = get_local_time()
                self.next_post_time = local_now + timedelta(seconds=next_delay)
                
                # Логируем следующую публикацию
                logger.info(f"⏰ Следующая публикация через {next_delay//60} минут")
                logger.info(f"📅 Точное время (UTC+{TIMEZONE_OFFSET}): {format_local_time(self.next_post_time)}")
                
                return True
                
            except TelegramError as e:
                if "Too Many Requests" in str(e):
                    logger.warning("⚠️ Лимит Telegram")
                else:
                    logger.error(f"❌ Ошибка Telegram: {e}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False

    async def run_once(self):
        """Один цикл работы бота"""
        local_start = get_local_time()
        logger.info("="*50)
        logger.info(f"🔍 Запуск: {format_local_time(local_start)} (UTC+{TIMEZONE_OFFSET})")
        logger.info("="*50)

        try:
            # Собираем новости
            news = await run_with_timeout(self.fetch_all_news(), timeout=120)
            if not news:
                logger.info("📭 Новых статей нет")
                return

            # Проверяем лимиты
            if not self.can_post_now():
                logger.info("⏰ Нельзя публиковать сейчас")
                return

            # Публикуем первую статью
            await self.create_and_publish(news[0])

        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
        finally:
            if self.session:
                await self.session.close()

# ========== ТОЧКА ВХОДА ==========
def main():
    """Основная функция"""
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        logger.error("❌ Нет TELEGRAM_TOKEN или CHANNEL_ID")
        return
    
    # Создаем и запускаем бота
    bot = NewsBot()
    asyncio.run(bot.run_once())

if __name__ == "__main__":
    main()
