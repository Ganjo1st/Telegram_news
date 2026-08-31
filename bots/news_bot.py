#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей
Источники: InfoBrics, Global Research
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
from urllib.parse import urljoin, quote

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

# Проверяем наличие токена
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN не задан! Проверьте секреты GitHub Actions.")
    exit(1)

if not CHANNEL_ID:
    logger.error("❌ CHANNEL_ID не задан! Проверьте секреты GitHub Actions.")
    exit(1)

# Интервалы публикации
MIN_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '300'))
MAX_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '600'))
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '50'))
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 1024

# ========== ФУНКЦИЯ ПЕРЕВОДА ==========
def translate_text(text: str) -> str:
    if not text:
        return ""
    if re.search('[а-яА-Я]', text):
        return text
    try:
        if len(text) > 500:
            text = text[:500]
        encoded_text = quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ru&dt=t&q={encoded_text}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0 and len(data[0]) > 0:
                translated = ''.join([part[0] for part in data[0] if part[0]])
                if translated:
                    return translated
        # Fallback: MyMemory API
        try:
            url = f"https://api.mymemory.translated.net/get?q={quote(text[:450])}&langpair=en|ru"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and 'responseData' in data and 'translatedText' in data['responseData']:
                    result = data['responseData']['translatedText']
                    if result:
                        return result
        except:
            pass
        return text
    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        return text

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None

def extract_image_url(soup, base_url: str) -> str | None:
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        img_url = meta_img['content']
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
            if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif', 'banner', 'flag', 'donation', 'header', 'print']):
                continue
            if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                if src.startswith('//'):
                    return 'https:' + src
                if src.startswith('/'):
                    return urljoin(base_url, src)
                if src.startswith('http'):
                    return src
    return None

def clean_content(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[Pp]lease\s+[Ss]upport.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Dd]onate.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Ss]ubscribe.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Cc]lick\s+[Hh]ere.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Rr]ead\s+[Mm]ore.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Ff]ollow\s+[Uu]s.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Bb]ecome\s+[Aa]\s+[Pp]atron.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Ss]ource:.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'[Vv]ia.*?(?=[.!?]|$)', '', text)
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def clean_title(title: str) -> str:
    if not title:
        return ""
    title = re.sub(r'^(?:БРИКС\s+Россия\s*[|:]\s*|BRICS\s+Russia\s*[|:]\s*|InfoBrics\s*[|:]\s*|Global Research\s*[|:]\s*|GE\s+Global\s+Research\s*[|:]\s*)', '', title, flags=re.IGNORECASE)
    title = re.sub(r'^[📰🗞️📄📑]+\s*', '', title)
    title = re.sub(r'\s*[|:]\s*$', '', title)
    title = re.sub(r'\{[^}]*\}', '', title)
    return title.strip()

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.test_mode = os.getenv('TEST_MODE', 'false').lower() == 'true'
        if self.test_mode:
            logger.info("🧪 ТЕСТОВЫЙ РЕЖИМ: все ограничения отключены")

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
        if self.test_mode:
            return True
        now = get_local_time()
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
        if self.test_mode:
            return 60
        delay = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        delay = int(delay * random.uniform(0.85, 1.15))
        return max(MIN_INTERVAL, min(delay, MAX_INTERVAL))

    def _truncate_to_last_sentence(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 3:
                return text[:last + 1].strip()
        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1:
            return text[:last_space].strip()
        return text[:max_len].strip()

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else 4096
        return self._truncate_to_last_sentence(text, max_len)

    # ========== ПАРСИНГ INFOBRICS ==========
    def _get_infobrics_articles(self) -> list:
        try:
            feed = feedparser.parse('https://infobrics.org/rss/en')
            articles = []
            for entry in feed.entries[:15]:
                title = entry.get('title', '').strip()
                if not title or title == '{[title]}' or len(title) < 5:
                    summary = entry.get('summary', '')
                    if summary:
                        summary = re.sub(r'<[^>]+>', '', summary)
                        title = summary.split('.')[0].strip()
                        if len(title) < 5 and len(summary) > 10:
                            title = summary[:100].strip()
                    if not title or len(title) < 5:
                        link = entry.get('link', '')
                        title = f"InfoBrics Article {link.split('/')[-1] if link else ''}"
                title = clean_title(title)
                if title.lower() in ['brics portal', 'portal', 'info brics'] or len(title) < 5:
                    continue
                articles.append({'url': entry.link, 'title': title})
            return articles
        except Exception as e:
            logger.error(f"Ошибка InfoBrics RSS: {e}")
            return []

    def _parse_infobrics_article(self, url: str) -> dict | None:
        try:
            response = fetch_url(url)
            if not response:
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'
            title = None
            title_div = soup.find('div', class_='title title--big')
            if title_div:
                title = title_div.get_text(strip=True)
                if title and title != '{[title]}':
                    logger.info(f"✅ Найден заголовок title--big: {title[:50]}...")
                else:
                    title = None
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                    if title and title != '{[title]}':
                        logger.info(f"✅ Найден h1: {title[:50]}...")
                    else:
                        title = None
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    if title and title != 'BRICS portal':
                        logger.info(f"✅ Найден og:title: {title[:50]}...")
                    else:
                        title = None
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[|–-]\s*InfoBrics\s*$', '', title, flags=re.IGNORECASE)
                    title = title.strip()
                    if title and title != 'BRICS portal':
                        logger.info(f"✅ Найден title: {title[:50]}...")
                    else:
                        title = None
            if not title:
                logger.warning(f"❌ Не удалось найти заголовок для {url}")
                return None
            title = clean_title(title)
            title = re.sub(r'\s+', ' ', title).strip()
            if len(title) < 5 or title.lower() in ['brics portal', 'portal']:
                logger.warning(f"❌ Заголовок '{title}' не является новостью")
                return None
            logger.info(f"📌 Итоговый заголовок InfoBrics: {title[:50]}...")
            image_url = None
            article_img = soup.find('img', class_='article__image')
            if article_img and article_img.get('src'):
                img_src = article_img['src']
                if img_src.startswith('//'):
                    image_url = 'https:' + img_src
                elif img_src.startswith('/'):
                    image_url = urljoin(base_url, img_src)
                elif img_src.startswith('http'):
                    image_url = img_src
                logger.info(f"✅ Найдено изображение article__image: {image_url[:50]}...")
            if not image_url:
                image_url = extract_image_url(soup, base_url)
                if image_url:
                    logger.info(f"✅ Найдено изображение через extract: {image_url[:50]}...")
            content_container = None
            article_text = soup.find('div', class_='article__text')
            if article_text:
                content_container = article_text
                logger.info("✅ Найден контейнер article__text")
            else:
                for class_name in ['article-content', 'post-content', 'entry-content', 'content', 'main-content']:
                    container = soup.find('div', class_=re.compile(class_name))
                    if container:
                        content_container = container
                        break
            if not content_container:
                content_container = soup.find('article') or soup.find('main')
            paragraphs = []
            if content_container:
                for tag in content_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()
                for p in content_container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more') and not text.startswith('Share this'):
                        paragraphs.append(text)
            if len(paragraphs) < 2:
                logger.warning(f"❌ Недостаточно контента ({len(paragraphs)} параграфов)")
                return None
            content = '\n\n'.join(paragraphs)
            if len(content) < 150:
                logger.warning(f"❌ Контент слишком короткий ({len(content)} символов)")
                return None
            return {
                'title': title,
                'content': content,
                'image': image_url,
                'source': 'InfoBrics',
                'url': url
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСИНГ GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:15]:
                title = entry.get('title', '').strip()
                if not title or len(title) < 5:
                    summary = entry.get('summary', '')
                    if summary:
                        summary = re.sub(r'<[^>]+>', '', summary)
                        title = summary.split('.')[0].strip()
                        if len(title) < 5 and len(summary) > 10:
                            title = summary[:100].strip()
                    if not title or len(title) < 5:
                        link = entry.get('link', '')
                        title = f"Global Research Article {link.split('/')[-1] if link else ''}"
                title = clean_title(title)
                if 'substack.com' in entry.link or 'asia-pacificresearch.com' in entry.link:
                    logger.info(f"⏭️ Пропущен внешний домен: {title[:30]}...")
                    continue
                articles.append({'url': entry.link, 'title': title})
            return articles
        except Exception as e:
            logger.error(f"Ошибка Global Research RSS: {e}")
            return []

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            response = fetch_url(url)
            if not response:
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'
            title = None
            subtitle = None
            title_h2 = soup.find('h2', class_='title', attrs={'itemprop': 'headline'})
            if title_h2:
                title = title_h2.get_text(strip=True)
                if title and len(title) > 5:
                    logger.info(f"✅ Найден h2.title: {title[:50]}...")
                else:
                    title = None
            if not title:
                subtitle_h3 = soup.find('h3', class_='subtitle')
                if subtitle_h3:
                    subtitle = subtitle_h3.get_text(strip=True)
                    logger.info(f"✅ Найден h3.subtitle: {subtitle[:50]}...")
            if not title:
                entry_title = soup.find('h1', class_='entry-title')
                if entry_title:
                    title = entry_title.get_text(strip=True)
                    if title and len(title) > 5:
                        logger.info(f"✅ Найден h1.entry-title: {title[:50]}...")
                    else:
                        title = None
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                    if title and len(title) > 5:
                        logger.info(f"✅ Найден h1: {title[:50]}...")
                    else:
                        title = None
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    if title and len(title) > 5:
                        logger.info(f"✅ Найден og:title: {title[:50]}...")
                    else:
                        title = None
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[|–-]\s*(?:Global Research|GE Global Research)\s*$', '', title, flags=re.IGNORECASE)
                    title = title.strip()
                    if title and len(title) > 5:
                        logger.info(f"✅ Найден title: {title[:50]}...")
                    else:
                        title = None
            if not title and subtitle:
                title = subtitle
                logger.info(f"✅ Используем подзаголовок как заголовок: {title[:50]}...")
            if not title:
                logger.warning(f"❌ Не удалось найти заголовок для {url}")
                return None
            if subtitle and title != subtitle:
                title = f"{title}: {subtitle}"
            title = clean_title(title)
            title = re.sub(r'\s+', ' ', title).strip()
            if len(title) < 10 or title.lower() in ['global research', 'ge global research']:
                logger.warning(f"❌ Заголовок '{title}' не является новостью")
                return None
            logger.info(f"📌 Итоговый заголовок: {title[:50]}...")
            image_url = extract_image_url(soup, base_url)
            if image_url:
                logger.info(f"✅ Найдено изображение: {image_url[:50]}...")
            content_container = None
            content_div = soup.find('div', class_='content', attrs={'itemprop': 'articleBody'})
            if content_div:
                content_container = content_div
                logger.info("✅ Найден контейнер content[itemprop=articleBody]")
            else:
                entry_content = soup.find('div', class_='entry-content')
                if entry_content:
                    content_container = entry_content
                    logger.info("✅ Найден контейнер entry-content")
                else:
                    article_text = soup.find('div', class_='article__text')
                    if article_text:
                        content_container = article_text
                        logger.info("✅ Найден контейнер article__text")
                    else:
                        for class_name in ['post-content', 'article-content']:
                            container = soup.find('div', class_=re.compile(class_name))
                            if container:
                                content_container = container
                                break
            if not content_container:
                content_container = soup.find('article')
            if not content_container:
                content_container = soup.find('main')
            paragraphs = []
            if content_container:
                for tag in content_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe', 'div.sharedaddy', 'div.wp-block-group']):
                    tag.decompose()
                for p in content_container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 40 and not text.startswith('Read more') and not text.startswith('Share this'):
                        paragraphs.append(text)
            if len(paragraphs) < 2:
                logger.warning(f"❌ Недостаточно контента ({len(paragraphs)} параграфов)")
                return None
            content = '\n\n'.join(paragraphs)
            content = clean_content(content)
            if len(content) < 150:
                logger.warning(f"❌ Контент слишком короткий ({len(content)} символов)")
                return None
            return {
                'title': title,
                'content': content,
                'image': image_url,
                'source': 'Global Research',
                'url': url
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга Global Research: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        logger.info("📰 Парсинг InfoBrics...")
        ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for article in ib_articles[:5]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ InfoBrics: {data['title'][:50]}...")
        logger.info("📰 Парсинг Global Research...")
        gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for article in gr_articles[:5]:
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
            caption_text = content_en[:MAX_CAPTION]
            caption_text = self._truncate_text(caption_text, is_caption=True)
            logger.info(f"📝 Перевод заголовка: {title_en[:50]}...")
            logger.info(f"📝 Перевод {len(caption_text)} символов текста")
            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, translate_text, title_en)
            content_ru = await loop.run_in_executor(None, translate_text, caption_text)
            content_ru = clean_content(content_ru)
            content_ru = re.sub(r'\n\s*\n', '\n', content_ru)
            content_ru = re.sub(r'\s+', ' ', content_ru)
            if content_ru and not re.search(r'[.!?…]\s*$', content_ru):
                last_dot = content_ru.rfind('.')
                if last_dot != -1 and last_dot > len(content_ru) // 2:
                    content_ru = content_ru[:last_dot + 1]
            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, post.get('source', ''), url, title_en, content_en)
            title_escaped = html.escape(title_ru)
            message = f"{title_escaped}\n\n{content_ru}"
            if image_url:
                logger.info(f"🖼️ Загрузка изображения: {image_url[:80]}...")
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
                            logger.info("✅ Опубликовано С ФОТО (переведено на русский)")
                            self._mark_sent(url, title_en, content_en)
                            self._log_post(url, title_en)
                            return
                        except TelegramError as e:
                            logger.warning(f"Ошибка отправки фото: {e}")
            logger.info("📝 Публикация текстом (без фото)")
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )
            logger.info("✅ Опубликовано ТЕКСТОМ (переведено на русский)")
            self._mark_sent(url, title_en, content_en)
            self._log_post(url, title_en)
        except TelegramError as e:
            error_msg = str(e)
            if "Can't parse entities" in error_msg:
                logger.warning("Ошибка Markdown, отправляем без форматирования")
                try:
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=f"{title_ru}\n\n{content_ru}",
                        parse_mode=None
                    )
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"❌ Ошибка при отправке без форматирования: {e2}")
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка публикации: {e}")

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
            logger.info("⏸️ Публикация отложена (ограничения)")
            return
        await self.publish(news[0])

    async def run_forever(self):
        logger.info("🤖 Бот запущен в бесконечном режиме")
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
    bot = NewsBot()
    if 'GITHUB_ACTIONS' in os.environ:
        await bot.run_once()
    else:
        await bot.run_forever()

if __name__ == '__main__':
    asyncio.run(main())
