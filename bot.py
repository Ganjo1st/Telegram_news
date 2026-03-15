#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Версия для GitHub Actions + Google Drive
Основан на версии 10.1 (абсолютная защита от дубликатов)
Адаптирован для запуска через менеджер manager.py
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
from datetime import datetime, timedelta

# Импорты библиотек
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

# ========== КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

# ХАОТИЧНЫЙ РЕЖИМ (в секундах)
MIN_POST_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '2100'))      # 35 минут
MAX_POST_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '7200'))      # 2 часа
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '24'))
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '7'))

# Таймауты (в секундах)
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))
TRANSLATION_TIMEOUT = int(os.getenv('TRANSLATION_TIMEOUT', '60'))
PUBLISH_TIMEOUT = int(os.getenv('PUBLISH_TIMEOUT', '30'))
TOTAL_BOT_TIMEOUT = int(os.getenv('TOTAL_BOT_TIMEOUT', '240'))  # 4 минуты на всё

# ========== ИСТОЧНИКИ НОВОСТЕЙ ==========
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

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТАЙМАУТОВ ==========
async def run_with_timeout(coro, timeout, default=None):
    """Запускает корутину с таймаутом"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"❌ Операция превысила таймаут {timeout}с")
        return default
    except Exception as e:
        logger.error(f"❌ Ошибка в операции: {e}")
        return default

def fetch_with_timeout(func, timeout, *args, **kwargs):
    """Запускает синхронную функцию с таймаутом"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.error(f"❌ Функция {func.__name__} превысила таймаут {timeout}с")
            return None

# ========== ОСНОВНОЙ КЛАСС БОТА ==========
class NewsBot:
    def __init__(self, state_file: str):
        """
        Инициализация бота.
        state_file: путь к JSON файлу для хранения всего состояния.
        """
        self.state_file = state_file
        self.state = self.load_state()

        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.session = None
        self.post_queue = []  # Очередь на этот запуск

        logger.info(f"🤖 Бот инициализирован, файл состояния: {state_file}")
        logger.info(f"📊 Загружено {len(self.state.get('sent_links', []))} ссылок")
        logger.info(f"📊 Загружено {len(self.state.get('sent_hashes', []))} хешей")
        logger.info(f"📊 Загружено {len(self.state.get('sent_titles', []))} заголовков")
        logger.info(f"📊 Загружено {len(self.state.get('posts_log', []))} записей в логе")

    # ========== УПРАВЛЕНИЕ СОСТОЯНИЕМ ==========
    def load_state(self) -> dict:
        """Загружает состояние из единого JSON-файла."""
        default_state = {
            'sent_links': [],
            'sent_hashes': [],
            'sent_titles': [],
            'posts_log': []
        }
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    # Преобразуем списки в множества для удобства и скорости
                    return {
                        'sent_links': set(state.get('sent_links', [])),
                        'sent_hashes': set(state.get('sent_hashes', [])),
                        'sent_titles': set(state.get('sent_titles', [])),
                        'posts_log': state.get('posts_log', [])
                    }
            else:
                logger.info(f"📁 Файл состояния {self.state_file} не найден, создаем новый.")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки состояния: {e}")

        return default_state

    def save_state(self):
        """Сохраняет состояние в единый JSON-файл."""
        try:
            # Преобразуем множества обратно в списки для JSON
            state_to_save = {
                'sent_links': list(self.state['sent_links']),
                'sent_hashes': list(self.state['sent_hashes']),
                'sent_titles': list(self.state['sent_titles']),
                'posts_log': self.state['posts_log']
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state_to_save, f, ensure_ascii=False, indent=2)
            logger.debug("💾 Состояние сохранено")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояния: {e}")

    # ========== МЕТОДЫ ДЕДУПЛИКАЦИИ ==========
    def normalize_title(self, title: str) -> str:
        """Нормализует заголовок для сравнения."""
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        common_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by']
        words = [w for w in title.split() if w not in common_words]
        return ' '.join(words)[:100]

    def create_content_hash(self, content: str) -> str:
        """Создает хеш содержимого статьи."""
        if not content:
            return ""
        sample = content[:500].encode('utf-8')
        return hashlib.md5(sample).hexdigest()

    def is_duplicate(self, url: str, title: str, content: str = "") -> bool:
        """Трёхуровневая проверка на дубликат."""
        if url in self.state['sent_links']:
            logger.info(f"⏭️ ДУБЛИКАТ (URL): {title[:50]}...")
            return True

        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            logger.info(f"⏭️ ДУБЛИКАТ (заголовок): {title[:50]}...")
            return True

        if content:
            content_hash = self.create_content_hash(content)
            if content_hash and content_hash in self.state['sent_hashes']:
                logger.info(f"⏭️ ДУБЛИКАТ (содержимое): {title[:50]}...")
                return True

        return False

    def mark_as_sent(self, url: str, title: str, content: str = ""):
        """Помечает статью как отправленную во всех трёх базах."""
        self.state['sent_links'].add(url)

        norm_title = self.normalize_title(title)
        if norm_title:
            self.state['sent_titles'].add(norm_title)

        if content:
            content_hash = self.create_content_hash(content)
            if content_hash:
                self.state['sent_hashes'].add(content_hash)

        self.save_state()
        logger.info(f"✅ Статья помечена как отправленная")

    def log_post(self, link: str, title: str):
        """Логирует опубликованный пост."""
        self.state['posts_log'].append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        # Оставляем только последние 100 записей
        if len(self.state['posts_log']) > 100:
            self.state['posts_log'] = self.state['posts_log'][-100:]
        self.save_state()

    # ========== ПРОВЕРКА ЛИМИТОВ ПУБЛИКАЦИИ ==========
    def can_post_now(self) -> bool:
        """Проверяет, можно ли публиковать сейчас (по времени и частоте)."""
        # Проверка ночного времени
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время ({local_hour}:00), пропускаю")
            return False

        # Проверка дневного лимита
        today = datetime.now().date()
        today_posts = 0
        last_posts_times = []

        for post in self.state['posts_log']:
            try:
                post_time = datetime.fromisoformat(post['time'])
                if post_time.date() == today:
                    today_posts += 1
                    last_posts_times.append(post_time)
            except:
                continue

        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Дневной лимит {MAX_POSTS_PER_DAY} достигнут")
            return False

        # Проверка минимального интервала между постами (35 минут)
        if last_posts_times:
            last_posts_times.sort(reverse=True)
            time_since_last = (datetime.now() - last_posts_times[0]).total_seconds()
            min_interval_sec = 35 * 60
            if time_since_last < min_interval_sec:
                wait_minutes = (min_interval_sec - time_since_last) / 60
                logger.info(f"⏳ Лимит частоты: следующий пост через {wait_minutes:.0f} минут")
                return False

        return True

    # ========== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ОЧИСТКИ ==========
    def clean_text(self, text: str) -> str:
        """Очищает текст от HTML и лишних пробелов."""
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()

    def remove_metadata(self, text: str) -> str:
        """Удаляет мета-данные из текста (даты, авторы, подписки)."""
        if not text:
            return text
        text = re.sub(r'\d+\s*(hour|min|sec|day|minute|second)s?\s+ago', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Updated\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Published\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^By\s+[\w\s,]+\n', '', text, flags=re.IGNORECASE | re.MULTILINE)
        garbage_phrases = [
            r'Subscribe', r'Newsletter', r'Sign up', r'Follow us',
            r'Share this', r'Read more', r'Comments', r'Advertisement',
        ]
        for phrase in garbage_phrases:
            text = re.sub(phrase, '', text, flags=re.IGNORECASE)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def escape_html_for_telegram(self, text: str) -> str:
        """Экранирует HTML для Telegram."""
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text

    # ========== ПАРСЕР AP NEWS V2 ==========
    def get_apnews_articles_v2(self):
        """Получает список ссылок на статьи с главной AP News."""
        try:
            logger.info("🌐 Парсинг главной страницы AP News (v2)")
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

                # Формируем полный URL
                if href.startswith('https://apnews.com/'):
                    full_url = href
                elif href.startswith('/'):
                    full_url = 'https://apnews.com' + href
                else:
                    continue

                # Ищем заголовок
                title = link.get_text(strip=True)
                if not title or len(title) < 15:
                    parent_heading = link.find_parent(['h1', 'h2', 'h3', 'h4'])
                    if parent_heading:
                        title = parent_heading.get_text(strip=True)

                if not title or len(title) < 15:
                    continue

                title = re.sub(r'\s+', ' ', title).strip()
                if any(phrase in title.lower() for phrase in ['newsletter', 'subscribe', 'sign up']):
                    continue

                articles.append({'url': full_url, 'title': title})

            # Убираем дубликаты URL
            unique_articles = []
            seen_urls = set()
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    unique_articles.append(article)

            logger.info(f"✅ Найдено {len(unique_articles)} статей AP News")
            return unique_articles[:10]  # Возвращаем первые 10

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return []

    def parse_apnews_article_v2(self, url: str):
        """Парсит отдельную статью с AP News."""
        try:
            logger.info(f"🌐 Парсинг статьи AP News: {url}")
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
            title = self.clean_text(title)

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст статьи
            article_text = ""
            main_container = soup.find('article') or soup.find('main') or soup.body
            if main_container:
                for unwanted in main_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    unwanted.decompose()
                paragraphs = []
                for p in main_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 20:
                        if not any(phrase in p_text.lower() for phrase in ['subscribe', 'newsletter', 'sign up']):
                            paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            article_text = self.remove_metadata(article_text)

            return {'title': title, 'content': article_text, 'main_image': main_image}
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи AP News: {e}")
            return None

    # ========== ПАРСЕР INFOBRICS ==========
    def parse_infobrics(self, url: str):
        """Парсит статью с InfoBrics."""
        # (Код из твоей версии 10.1, он уже хорош)
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())
            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                elif not img_src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{img_src}"
                else:
                    main_image = img_src
            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'article__text')) or soup.find('div', class_=re.compile(r'article'))
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button']):
                    unwanted.decompose()
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
            if len(article_text) < 200:
                return None
            return {'title': title, 'content': article_text, 'main_image': main_image}
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСЕР GLOBAL RESEARCH ==========
    def parse_globalresearch(self, url: str):
        """Парсит статью с Global Research."""
        # (Код из твоей версии 10.1)
        try:
            logger.info(f"🌐 Парсинг Global Research: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response or response.status_code != 200:
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            title = "Без заголовка"
            title_elem = soup.find('h1') or soup.find('title')
            if title_elem:
                title = self.clean_text(title_elem.get_text())
            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'featured|wp-post-image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                elif not img_src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{img_src}"
                else:
                    main_image = img_src
            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'entry-content|post-content'))
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button']):
                    unwanted.decompose()
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
            if len(article_text) < 200:
                return None
            return {'title': title, 'content': article_text, 'main_image': main_image}
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга Global Research: {e}")
            return None

    # ========== ЗАГРУЗКА НОВОСТЕЙ ИЗ ВСЕХ ИСТОЧНИКОВ ==========
    async def fetch_from_apnews_v2(self):
        """Загружает новые статьи с AP News."""
        news_items = []
        try:
            articles = await asyncio.get_event_loop().run_in_executor(None, self.get_apnews_articles_v2)
            if not articles:
                return news_items

            for article in articles[:3]:  # Берем первые 3
                url, title = article['url'], article['title']
                if self.is_duplicate(url, title):
                    continue

                logger.info(f"🔍 НОВАЯ (AP News): {title[:50]}...")
                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article_v2, url
                )
                if article_data and not self.is_duplicate(url, article_data['title'], article_data['content']):
                    news_items.append({
                        'source': 'AP News',
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1
                    })
                    logger.info(f"✅ Статья AP News добавлена в очередь")
                else:
                    logger.warning(f"❌ Не удалось спарсить AP News статью")
                await asyncio.sleep(random.randint(2, 5))
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_apnews_v2: {e}")
        return news_items

    async def fetch_from_rss(self, feed_config):
        """Загружает новые статьи из RSS-ленты."""
        news_items = []
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)

            if parser_name == 'infobrics':
                parser_func = self.parse_infobrics
            elif parser_name == 'globalresearch':
                parser_func = self.parse_globalresearch
            else:
                parser_func = self.parse_infobrics

            feed = await asyncio.get_event_loop().run_in_executor(None, feedparser.parse, feed_url)
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []

            logger.info(f"📰 В RSS {source_name} {len(feed.entries)} статей")
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')

                if self.is_duplicate(link, title):
                    continue

                logger.info(f"🔍 НОВАЯ ({source_name}): {title[:50]}...")
                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, parser_func, link
                )
                if article_data and not self.is_duplicate(link, article_data['title'], article_data['content']):
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'main_image': article_data.get('main_image'),
                        'priority': priority
                    })
                    logger.info(f"✅ Статья {source_name} добавлена в очередь")
                else:
                    logger.warning(f"❌ Не удалось спарсить {source_name}")
                await asyncio.sleep(random.randint(2, 5))
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_rss для {feed_config['name']}: {e}")
        return news_items

    async def fetch_all_news(self):
        """Собирает новые статьи из всех включенных источников."""
        all_news = []
        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue
            if feed.get('type') == 'html_apnews_v2':
                news = await self.fetch_from_apnews_v2()
            else:
                news = await self.fetch_from_rss(feed)
            all_news.extend(news)
            await asyncio.sleep(random.randint(3, 7))

        all_news.sort(key=lambda x: x.get('priority', 5))
        logger.info(f"📊 ВСЕГО НОВЫХ УНИКАЛЬНЫХ СТАТЕЙ ЗА ЦИКЛ: {len(all_news)}")
        return all_news

    # ========== ПУБЛИКАЦИЯ В TELEGRAM ==========
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def download_image(self, url: str):
        """Скачивает изображение во временный файл."""
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
            logger.error(f"Ошибка скачивания изображения: {e}")
        return None

    async def translate_text(self, text: str) -> str:
        """Переводит текст с таймаутом."""
        if not text or len(text) < 20:
            return text
        try:
            # Используем fetch_with_timeout для синхронного вызова переводчика
            loop = asyncio.get_event_loop()
            translated = await loop.run_in_executor(
                None,
                lambda: fetch_with_timeout(self.translator.translate, TRANSLATION_TIMEOUT, text)
            )
            return translated if translated else text
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    def truncate_first_paragraph_by_sentences(self, paragraph: str, max_length: int) -> str:
        """Обрезает первый абзац по предложениям."""
        if len(paragraph) <= max_length:
            return paragraph
        sentences = re.split(r'(?<=[.!?])\s+', paragraph)
        result = []
        current_len = 0
        for sent in sentences:
            if current_len + len(sent) + (1 if result else 0) <= max_length:
                if result:
                    result.append(' ')
                    current_len += 1
                result.append(sent)
                current_len += len(sent)
            else:
                if not result:
                    # Если не влезает даже первое предложение, режем по словам
                    words = sent.split()
                    for word in words:
                        if current_len + len(word) + (1 if result else 0) <= max_length:
                            if result:
                                result.append(' ')
                                current_len += 1
                            result.append(word)
                            current_len += len(word)
                        else:
                            break
                break
        return ''.join(result)

    def build_caption(self, title: str, paragraphs: list, max_length: int = 1024) -> str:
        """Формирует подпись к посту с умным обрезанием."""
        title_part = f"<b>{self.escape_html_for_telegram(title)}</b>"
        current_text = title_part
        current_len = len(title_part)
        available = max_length - 5  # небольшой запас

        if current_len >= available:
            title_truncated = title[:50] + "..."
            title_part = f"<b>{self.escape_html_for_telegram(title_truncated)}</b>"
            current_text = title_part
            current_len = len(title_part)

        for i, para in enumerate(paragraphs):
            separator = "\n\n"
            para_with_sep = separator + para
            para_len = len(para_with_sep)

            if current_len + para_len <= available:
                current_text += para_with_sep
                current_len += para_len
            else:
                # Если это первый абзац, пробуем его обрезать
                if i == 0:
                    remaining = available - current_len - len(separator)
                    if remaining > 20:
                        truncated_para = self.truncate_first_paragraph_by_sentences(para, remaining)
                        if truncated_para:
                            current_text += separator + truncated_para
                break
        return current_text

    async def create_and_publish_post(self, news_item: dict) -> bool:
        """Создает пост из статьи и публикует его."""
        try:
            logger.info(f"\n📝 ПОДГОТОВКА К ПУБЛИКАЦИИ: {news_item['title'][:70]}...")
            logger.info(f"   Источник: {news_item['source']}")

            # Параллельный перевод заголовка и текста для скорости
            title_ru, content_ru = await asyncio.gather(
                self.translate_text(news_item['title']),
                self.translate_text(news_item['content'])
            )

            title_escaped = self.escape_html_for_telegram(title_ru)
            content_escaped = self.escape_html_for_telegram(content_ru)
            paragraphs = [p for p in content_escaped.split('\n\n') if p.strip()]

            logger.info(f"📊 Статья содержит {len(paragraphs)} абзацев после перевода")

            # Скачиваем изображение, если есть
            image_path = None
            if news_item.get('main_image'):
                logger.info("🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['main_image'])

            # Формируем финальную подпись
            final_caption = self.build_caption(title_escaped, paragraphs)

            # Публикуем
            if image_path:
                with open(image_path, 'rb') as photo:
                    await run_with_timeout(
                        self.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=photo,
                            caption=final_caption,
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
                        text=final_caption,
                        parse_mode='HTML'
                    ),
                    PUBLISH_TIMEOUT
                )
                logger.info("✅ Пост без фото опубликован")

            # Помечаем как отправленное
            self.mark_as_sent(news_item['link'], news_item['title'], news_item['content'])
            self.log_post(news_item['link'], news_item['title'])
            return True

        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram. Этот запуск пропущен.")
            elif "Can't parse entities" in str(e):
                logger.warning("⚠️ Ошибка парсинга HTML, отправляем без форматирования")
                try:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=final_caption)
                    self.mark_as_sent(news_item['link'], news_item['title'], news_item['content'])
                    self.log_post(news_item['link'], news_item['title'])
                    return True
                except Exception as e2:
                    logger.error(f"❌ Ошибка при повторной отправке: {e2}")
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут публикации")
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при публикации: {e}")

        # Если дошли сюда, публикация не удалась
        if image_path and os.path.exists(image_path):
            try:
                os.unlink(image_path)
            except:
                pass
        return False

    # ========== ОСНОВНОЙ МЕТОД ЗАПУСКА (ОДИН ЦИКЛ) ==========
    async def run_once(self):
        """Запускает один полный цикл работы бота (поиск -> публикация)."""
        logger.info("="*60)
        logger.info(f"🔍 ЗАПУСК ЦИКЛА БОТА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        try:
            # Шаг 1: Собираем новые статьи
            news_items = await run_with_timeout(
                self.fetch_all_news(),
                timeout=120  # 2 минуты на сбор
            )

            if not news_items:
                logger.info("📭 НОВЫХ УНИКАЛЬНЫХ СТАТЕЙ НЕТ")
                return

            self.post_queue = news_items
            logger.info(f"📦 В очереди на этот цикл: {len(self.post_queue)} статей")

            # Шаг 2: Проверяем, можно ли публиковать сейчас
            if not self.can_post_now():
                logger.info("⏰ Сейчас нельзя публиковать по лимитам. Статьи останутся в очереди до следующего запуска.")
                # Здесь мы не сохраняем очередь между запусками. В текущей архитектуре
                # неопубликованные статьи будут найдены снова в следующий раз.
                # Это нормально, т.к. проверка на дубликаты их отсеет.
                return

            # Шаг 3: Пытаемся опубликовать одну статью
            logger.info(f"📝 Пытаемся опубликовать 1 статью...")
            success = await self.create_and_publish_post(self.post_queue[0])

            if success:
                logger.info("✅ Успешно опубликована 1 статья за цикл")
            else:
                logger.info("❌ Не удалось опубликовать статью в этом цикле")

        except asyncio.TimeoutError:
            logger.error("❌ Общий таймаут выполнения run_once")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в run_once: {e}")
        finally:
            if self.session:
                await self.session.close()
                self.session = None
            logger.info("🏁 Цикл бота завершен")

# ========== ТОЧКА ВХОДА, ВЫЗЫВАЕМАЯ МЕНЕДЖЕРОМ ==========
async def main(state_file: str):
    """
    Главная функция, вызываемая менеджером.
    state_file: путь к файлу состояния (передается из manager.py)
    """
    # Проверяем обязательные переменные
    if not TELEGRAM_TOKEN:
        logger.error("❌ Не задан TELEGRAM_TOKEN")
        return
    if not CHANNEL_ID:
        logger.error("❌ Не задан CHANNEL_ID")
        return

    bot = NewsBot(state_file)
    await bot.run_once()

if __name__ == "__main__":
    # Для локального тестирования
    asyncio.run(main("test_state.json"))
