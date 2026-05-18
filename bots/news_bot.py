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
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКА ==========
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
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

# Паттерны для удаления мусора
BRACKET_SOURCE_PATTERNS = [
    r'\([^)]*AP[^)]*\)', r'\([^)]*АР[^)]*\)',
    r'\([^)]*Associated Press[^)]*\)', r'\([^)]*InfoBrics[^)]*\)',
    r'\([^)]*Global Research[^)]*\)', r'\([^)]*Photo[^)]*\)',
]

SOURCE_PATTERNS = [
    r'— AP News$', r'\| AP News', r'AP News —',
    r'— Global Research$', r'— InfoBrics$',
    r'Источник:\s*\S+', r'По материалам\s*\S+',
    r'Read more:', r'Click here',
]


def clean_text(text: str) -> str:
    if not text:
        return ""
    for pattern in BRACKET_SOURCE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    for pattern in SOURCE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error(f"Ошибка {url}: {e}")
        return None


def extract_image_url(soup, base_url: str) -> str | None:
    """Улучшенное извлечение изображений для AP News и других сайтов"""
    exclude = ['logo', 'icon', 'svg', 'gif', 'pixel', 'ap-logo', 'favicon', 'banner', 'avatar', 'button']
    
    # 1. Open Graph image (самый надежный)
    meta = soup.find('meta', property='og:image')
    if meta and meta.get('content'):
        img = meta['content']
        if img.startswith('//'):
            img = 'https:' + img
        if img.startswith('http') and not any(x in img.lower() for x in exclude):
            logger.info(f"Найдено og:image: {img[:80]}...")
            return img
    
    # 2. Twitter image
    meta = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta and meta.get('content'):
        img = meta['content']
        if img.startswith('//'):
            img = 'https:' + img
        if img.startswith('http') and not any(x in img.lower() for x in exclude):
            logger.info(f"Найдено twitter:image: {img[:80]}...")
            return img
    
    # 3. Поиск в article/main контейнере
    container = soup.find('article') or soup.find('main')
    if container:
        best_img = None
        max_size = 0
        
        for img in container.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if not src:
                continue
            
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = urljoin(base_url, src)
            
            if not src.startswith('http'):
                continue
            
            # Пропускаем мусор
            if any(x in src.lower() for x in exclude):
                continue
            
            # Проверяем размеры
            w = img.get('width', '')
            h = img.get('height', '')
            
            if w and h:
                try:
                    w_int = int(w)
                    h_int = int(h)
                    # Берем изображения больше 400x300
                    if w_int >= 400 and h_int >= 300 and w_int * h_int > max_size:
                        max_size = w_int * h_int
                        best_img = src
                except:
                    pass
            
            # Если нет информации о размерах, но классы указывают на главное изображение
            if not best_img:
                classes = img.get('class', [])
                class_str = ' '.join(classes).lower()
                if 'hero' in class_str or 'featured' in class_str or 'lead' in class_str or 'main' in class_str:
                    best_img = src
            
            # Если нашли подходящее, возвращаем
            if best_img and max_size > 0:
                logger.info(f"Найдено изображение по размеру: {best_img[:80]}...")
                return best_img
        
        if best_img:
            logger.info(f"Найдено изображение по классу: {best_img[:80]}...")
            return best_img
    
    # 4. Поиск во всех figure
    for figure in soup.find_all('figure'):
        img = figure.find('img')
        if img:
            src = img.get('src') or img.get('data-src')
            if src:
                if src.startswith('//'):
                    src = 'https:' + src
                elif src.startswith('/'):
                    src = urljoin(base_url, src)
                if src.startswith('http') and not any(x in src.lower() for x in exclude):
                    logger.info(f"Найдено изображение в figure: {src[:80]}...")
                    return src
    
    # 5. Поиск по data-src (для lazy loading)
    for img in soup.find_all('img', {'data-src': re.compile(r'\.(jpg|jpeg|png|webp)')}):
        src = img['data-src']
        if src.startswith('//'):
            src = 'https:' + src
        elif src.startswith('/'):
            src = urljoin(base_url, src)
        if src.startswith('http') and not any(x in src.lower() for x in exclude):
            w = img.get('width', '')
            h = img.get('height', '')
            if w and h:
                try:
                    if int(w) >= 400 and int(h) >= 300:
                        logger.info(f"Найдено data-src: {src[:80]}...")
                        return src
                except:
                    pass
    
    logger.warning("Изображение не найдено")
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
            logger.error(f"Ошибка загрузки: {e}")
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
            logger.error(f"Ошибка сохранения: {e}")

    def _load_meta(self) -> dict:
        try:
            if os.path.exists(META_FILE):
                with open(META_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка мета: {e}")
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
            logger.error(f"Ошибка: {e}")

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
        if not text or len(text) < 10:
            return text
        try:
            result = self.translator.translate(text[:3000])
            return clean_text(result) if result else text
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            return text

    # ========== AP NEWS ==========
    def _get_apnews_articles(self) -> list:
        try:
            resp = fetch_url('https://apnews.com/hub/world-news')
            if not resp or resp.status_code != 200:
                return []
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
                        articles.append({'url': url, 'title': clean_text(title)})
            seen = set()
            unique = []
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)
            return unique[:5]
        except Exception as e:
            logger.error(f"AP News список: {e}")
            return []

    def _parse_apnews_article(self, url: str) -> dict | None:
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
            logger.info(f"Заголовок: {title[:60]}...")

            # Изображение
            image = extract_image_url(soup, base)
            if image:
                img_hash = hashlib.md5(image.encode()).hexdigest()
                if img_hash in IMAGE_HASH_CACHE:
                    logger.info(f"Изображение уже использовалось, пропускаем")
                    image = None
                else:
                    logger.info(f"Найдено новое изображение")

            # Контент
            article = soup.find('article')
            if not article:
                article = soup.find('main')
            if not article:
                return None

            # Удаляем мусор
            for tag in article.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'figure']):
                tag.decompose()

            paragraphs = []
            for p in article.find_all('p'):
                text = p.get_text(strip=True)
                # Фильтруем короткие параграфы и подписи
                if len(text) > 60 and not text.startswith('FILE -') and not text.startswith('This photo'):
                    text = clean_text(text)
                    if text and len(text) > 40:
                        paragraphs.append(text)
                if len(paragraphs) >= 8:
                    break

            if len(paragraphs) < 2:
                logger.warning(f"Недостаточно параграфов: {len(paragraphs)}")
                return None

            content = '\n\n'.join(paragraphs)
            if len(content) < 200:
                logger.warning(f"Контент слишком короткий: {len(content)} символов")
                return None

            logger.info(f"Контент: {len(content)} символов, {len(paragraphs)} параграфов")
            return {'title': title, 'content': content, 'image': image, 'source': 'AP News', 'url': url}
        except Exception as e:
            logger.error(f"AP News парсинг {url}: {e}")
            return None

    # ========== INFOBRICS ==========
    def _get_infobrics_articles(self) -> list:
        try:
            feed = feedparser.parse('https://infobrics.org/rss/en')
            articles = []
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if not title or title in ['{[title]}', 'BRICS portal']:
                    summary = entry.get('summary', '')
                    if summary:
                        title = re.sub(r'<[^>]+>', '', summary)
                        title = title.split('.')[0][:100]
                if title and len(title) > 10:
                    articles.append({'url': entry.link, 'title': clean_text(title)})
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
                if len(paragraphs) >= 6:
                    break

            if len(paragraphs) < 2:
                return None
            content = '\n\n'.join(paragraphs)
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"InfoBrics парсинг: {e}")
            return None

    # ========== GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                if title:
                    title = re.sub(r'\s*[-|]\s*Global Research$', '', title)
                    articles.append({'url': entry.link, 'title': clean_text(title)})
            return articles
        except Exception as e:
            logger.error(f"GR RSS: {e}")
            return []

    def _parse_globalresearch_article(self, url: str) -> dict | None:
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
                if len(paragraphs) >= 6:
                    break

            if len(paragraphs) < 2:
                return None
            content = '\n\n'.join(paragraphs)
            if len(content) < 150:
                return None

            return {'title': title, 'content': content, 'image': image, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"GR парсинг: {e}")
            return None

    # ========== СБОР ==========
    async def fetch_news(self) -> list:
        items = []
        
        logger.info("📰 AP News...")
        ap_list = await asyncio.get_event_loop().run_in_executor(None, self._get_apnews_articles)
        for a in ap_list:
            if not self._is_duplicate(a['url'], a['title']):
                data = await asyncio.get_event_loop().run_in_executor(None, self._parse_apnews_article, a['url'])
                if data:
                    items.append(data)
                    logger.info(f"✅ AP: {data['title'][:40]}...")

        logger.info("📰 InfoBrics...")
        ib_list = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for a in ib_list:
            if not self._is_duplicate(a['url'], a['title']):
                data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, a['url'])
                if data:
                    items.append(data)
                    logger.info(f"✅ InfoBrics: {data['title'][:40]}...")

        logger.info("📰 Global Research...")
        gr_list = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for a in gr_list:
            if not self._is_duplicate(a['url'], a['title']):
                data = await asyncio.get_event_loop().run_in_executor(None, self._parse_globalresearch_article, a['url'])
                if data:
                    items.append(data)
                    logger.info(f"✅ GR: {data['title'][:40]}...")

        logger.info(f"📊 Новых: {len(items)}")
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
            loop = asyncio.get_event_loop()
            title_ru = clean_text(await loop.run_in_executor(None, self._translate, title_en))
            content_ru = clean_text(await loop.run_in_executor(None, self._translate, content_en))

            pid = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(pid, post['source'], url, title_en, content_en[:300])

            title_escaped = html.escape(title_ru)
            
            # Формируем пост
            msg_text = self._truncate_text(content_ru, is_caption=True)
            message = f"*{title_escaped}*\n\n{msg_text}"

            # Проверяем длину подписи
            if len(message) > MAX_CAPTION:
                title_len = len(f"*{title_escaped}*\n\n")
                max_text_len = MAX_CAPTION - title_len - 5
                msg_text = self._truncate_sentence(content_ru, max_text_len)
                message = f"*{title_escaped}*\n\n{msg_text}"

            # Публикация с фото
            if img:
                logger.info(f"🖼️ Загрузка изображения: {img[:80]}...")
                resp = fetch_url(img, timeout=15)
                if resp and resp.status_code == 200:
                    content_type = resp.headers.get('Content-Type', '')
                    if 'image' in content_type:
                        try:
                            await self.bot.send_photo(
                                chat_id=CHANNEL_ID, 
                                photo=resp.content, 
                                caption=message, 
                                parse_mode='Markdown'
                            )
                            logger.info("✅ Опубликовано С ФОТО")
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
                                logger.info("✅ Опубликовано С ФОТО (только заголовок)")
                                self._mark_sent(url, title_en, content_en, img)
                                self._log_post(url, title_en)
                                return
                            else:
                                logger.warning(f"Ошибка отправки фото: {e}")
                    else:
                        logger.warning(f"Не изображение: {content_type}")
                else:
                    logger.warning(f"Не удалось загрузить изображение, статус: {resp.status_code if resp else 'None'}")
            else:
                logger.info("📷 Изображение не найдено для этой статьи")

            # Публикация без фото
            logger.info("📝 Публикация текстом")
            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"*{title_escaped}*\n\n{text_content}"
            
            if len(text_message) > MAX_MESSAGE:
                title_len = len(f"*{title_escaped}*\n\n")
                max_text_len = MAX_MESSAGE - title_len - 10
                text_content = self._truncate_sentence(content_ru, max_text_len)
                text_message = f"*{title_escaped}*\n\n{text_content}"
            
            await self.bot.send_message(
                chat_id=CHANNEL_ID, 
                text=text_message, 
                parse_mode='Markdown'
            )
            logger.info("✅ Опубликовано ТЕКСТОМ")
            self._mark_sent(url, title_en, content_en, img)
            self._log_post(url, title_en)

        except TelegramError as e:
            if "Can't parse entities" in str(e):
                logger.warning("Ошибка Markdown, отправляем без форматирования")
                await self.bot.send_message(
                    chat_id=CHANNEL_ID, 
                    text=f"{title_ru}\n\n{content_ru}", 
                    parse_mode=None
                )
            else:
                logger.error(f"Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")

    async def run_once(self):
        logger.info("=" * 40)
        logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
        news = await self.fetch_news()
        if not news:
            logger.info("📭 Новых статей нет")
            return
        if not self._can_post():
            logger.info("⏸️ Публикация отложена (ограничения)")
            return
        await self.publish(news[0])

    async def run_forever(self):
        logger.info("🤖 Бот запущен в бесконечном режиме")
        while True:
            try:
                await self.run_once()
                delay = random.randint(MIN_INTERVAL, MAX_INTERVAL)
                logger.info(f"⏰ Следующий запуск через {delay // 60} минут")
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
