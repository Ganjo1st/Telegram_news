#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей
Источники: InfoBrics, Global Research, RT, Sputnik, ZeroHedge, Al Mayadeen
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

# Интервалы публикации (секунды)
MIN_INTERVAL = 2100  # 35 минут
MAX_INTERVAL = 7200  # 2 часа
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

# Лимиты Telegram
MAX_CAPTION = 1024
MAX_MESSAGE = 4096

# ========== ОПРЕДЕЛЯЕМ РЕЖИМ ЗАПУСКА ==========
IS_MANUAL_RUN = os.getenv('TEST_MODE', '').lower() == 'true'

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

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

def extract_image_url(soup, base_url: str) -> str | None:
    """Извлекает URL изображения из страницы"""
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

def clean_title(title: str) -> str:
    """Очищает заголовок от лишних символов"""
    if not title:
        return title
    
    title = re.sub(r'^#+\s*', '', title)
    title = re.sub(r'^[📰📝📌🔹🔸⭐️✨]\s*', '', title)
    # Удаляем "популярные статьи" и подобные фразы
    if re.search(r'(популярн|popular|most popular|top|trending|daily|roundup|summary|recap)', title, re.IGNORECASE):
        return ""
    title = title.strip()
    return title

# ========== ИСКЛЮЧАЕМЫЕ ИМЕНА АВТОРОВ ==========
EXCLUDED_AUTHORS = [
    'Уриэль Араухо', 'Uriel Araujo',
    'Ахмед Адель', 'Ahmed Adel',
    'Лукас Лейроз', 'Lucas Leiros',
    'Одри Чайлд', 'Audrey Child',
]

def is_excluded_author(text: str) -> bool:
    """Проверяет, содержит ли текст имя исключаемого автора"""
    if not text:
        return False
    for name in EXCLUDED_AUTHORS:
        if name in text:
            return True
    return False

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')

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
            logger.info("🔓 Ручной запуск - ограничения сняты")
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

    def _truncate_to_last_sentence(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text

        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 2:
                result = text[:last + 1].strip()
                if len(result) <= max_len:
                    return result

        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1:
            return text[:last_space].strip()

        return text[:max_len].strip()

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        max_len = max_len - 100
        return self._truncate_to_last_sentence(text, max_len)

    def _translate_text(self, text: str) -> str:
        if not text:
            return ""

        # Если текст уже содержит кириллицу - возвращаем как есть
        if re.search('[а-яА-Я]', text):
            return text

        try:
            # Ограничиваем текст для перевода
            if len(text) > 4000:
                text = text[:4000]

            # Принудительный перевод с повторной попыткой
            result = self.translator.translate(text)
            
            # Если результат пустой или не содержит кириллицы - пробуем еще раз
            if not result or not re.search('[а-яА-Я]', result):
                logger.warning("⚠️ Первая попытка перевода не дала результат, повторная...")
                result = self.translator.translate(text[:3000])
            
            if result and len(result) > 0:
                # Проверяем, что перевод действительно выполнен (есть кириллица)
                if re.search('[а-яА-Я]', result):
                    logger.info(f"✅ Перевод выполнен. Длина: {len(result)} символов")
                    return result
                else:
                    logger.warning("⚠️ Перевод не содержит кириллицы, возможно ошибка API")
                    # Пробуем альтернативный метод
                    try:
                        from deep_translator import GoogleTranslator as GT
                        alt_translator = GT(source='auto', target='ru')
                        result = alt_translator.translate(text[:3000])
                        if result and re.search('[а-яА-Я]', result):
                            logger.info("✅ Альтернативный перевод выполнен")
                            return result
                    except:
                        pass
                    return text
            else:
                logger.warning("⚠️ Перевод вернул пустой результат")
                return text

        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            # Пробуем альтернативный переводчик
            try:
                from deep_translator import GoogleTranslator as GT
                alt_translator = GT(source='auto', target='ru')
                result = alt_translator.translate(text[:3000])
                if result and re.search('[а-яА-Я]', result):
                    logger.info("✅ Альтернативный перевод выполнен после ошибки")
                    return result
            except:
                pass
            return text

    # ========== УНИВЕРСАЛЬНЫЙ ПАРСИНГ RSS ==========
    def _parse_rss_feed(self, url: str, source_name: str, limit: int = 5) -> list:
        try:
            feed = feedparser.parse(url)
            
            if feed.bozo:
                logger.warning(f"⚠️ {source_name}: возможные проблемы с RSS")
            
            articles = []
            
            for entry in feed.entries[:limit]:
                title = entry.get('title', '').strip()
                
                # Пропускаем "популярные статьи"
                if re.search(r'(популярн|popular|most popular|top|trending|daily|roundup|summary|recap)', title, re.IGNORECASE):
                    logger.info(f"⏭️ {source_name}: пропущен заголовок '{title[:50]}...' (популярные статьи)")
                    continue
                
                if not title or len(title) < 5:
                    summary = entry.get('summary', '')
                    if summary:
                        summary = re.sub(r'<[^>]+>', '', summary)
                        title = summary.split('.')[0].strip()
                        if len(title) < 5 and len(summary) > 10:
                            title = summary[:100].strip()
                    
                    if not title or len(title) < 5:
                        link = entry.get('link', '')
                        title = f"{source_name} Article {link.split('/')[-1] if link else ''}"
                
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
            logger.error(f"❌ Ошибка парсинга RSS {source_name}: {e}")
            return []

    # ========== УНИВЕРСАЛЬНЫЙ ПАРСИНГ СТАТЬИ ==========
    def _parse_article(self, url: str, source_name: str) -> dict | None:
        try:
            response = fetch_url(url)
            if not response:
                return None

            # Проверяем, не Substack ли это (403 ошибка)
            if 'substack.com' in url:
                logger.warning(f"⏭️ {source_name}: пропуск Substack статьи (403)")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            image_url = extract_image_url(soup, base_url)
            if image_url:
                logger.info(f"Найдено изображение: {image_url[:80]}...")

            content_container = None
            for class_name in ['entry-content', 'post-content', 'content', 'article-content', 'main-content', 'article__text', 'body']:
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
                for tag in content_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()

                for p in content_container.find_all('p'):
                    text = p.get_text(strip=True)
                    # Исключаем абзацы с именами авторов
                    if is_excluded_author(text):
                        logger.info(f"⏭️ Пропущен абзац с именем автора")
                        continue
                    if len(text) > 30:
                        if not text.startswith('Read more') and not text.startswith('Share this'):
                            paragraphs.append(text)

            if len(paragraphs) < 2:
                main = soup.find('main')
                if main:
                    for p in main.find_all('p'):
                        text = p.get_text(strip=True)
                        if is_excluded_author(text):
                            logger.info(f"⏭️ Пропущен абзац с именем автора")
                            continue
                        if len(text) > 30:
                            paragraphs.append(text)

            if len(paragraphs) < 2:
                logger.warning(f"Недостаточно контента для {url}")
                return None

            content = '\n\n'.join(paragraphs)

            if len(content) < 150:
                logger.warning(f"Контент слишком короткий ({len(content)} символов)")
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

    # ========== СПЕЦИФИЧНЫЕ МЕТОДЫ ДЛЯ КАЖДОГО ИСТОЧНИКА ==========
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

    def _get_sputnik_articles(self) -> list:
        return self._parse_rss_feed('https://sputnikglobe.com/export/rss2/archive/index.xml', 'Sputnik')

    def _parse_sputnik_article(self, url: str) -> dict | None:
        return self._parse_article(url, 'Sputnik')

    def _get_almayadeen_articles(self) -> list:
        return self._parse_rss_feed('https://english.almayadeen.net/rss', 'Al Mayadeen')

    def _parse_almayadeen_article(self, url: str) -> dict | None:
        return self._parse_article(url, 'Al Mayadeen')

    # ========== СБОР НОВОСТЕЙ С ТАЙМАУТОМ ==========
    async def fetch_news(self) -> list:
        items = []
        
        sources = [
            ('InfoBrics', self._get_infobrics_articles, self._parse_infobrics_article),
            ('Global Research', self._get_globalresearch_articles, self._parse_globalresearch_article),
            ('RT', self._get_rt_articles, self._parse_rt_article),
            ('ZeroHedge', self._get_zerohedge_articles, self._parse_zerohedge_article),
            ('Sputnik', self._get_sputnik_articles, self._parse_sputnik_article),
            ('Al Mayadeen', self._get_almayadeen_articles, self._parse_almayadeen_article),
        ]

        for source_name, get_func, parse_func in sources:
            try:
                logger.info(f"📰 Парсинг {source_name}...")
                
                try:
                    articles = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, get_func),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.error(f"❌ Таймаут при парсинге {source_name} (30 сек)")
                    continue
                
                for article in articles[:3]:
                    title = article.get('title', '')
                    url = article.get('url', '')
                    
                    if self._is_duplicate(url, title):
                        continue
                    
                    try:
                        data = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(None, parse_func, url),
                            timeout=20.0
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"❌ Таймаут при загрузке статьи {source_name}: {url[:80]}...")
                        continue
                    
                    if data:
                        data['title'] = title
                        logger.info(f"✅ {source_name}: {title[:80]}...")
                        
                        if not self._is_duplicate(url, title, data['content']):
                            items.append(data)
            except Exception as e:
                logger.error(f"❌ Критическая ошибка при парсинге {source_name}: {e}")
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
                logger.warning("⏭️ Пропуск: пустой заголовок после очистки")
                return

            if url in self.state['sent_links']:
                logger.warning(f"⛔ Пост уже опубликован по URL: {url[:80]}...")
                return
            
            norm_title = self._normalize_title(title_en)
            if norm_title and norm_title in self.state['sent_titles']:
                logger.warning(f"⛔ Пост уже опубликован по заголовку: {title_en[:50]}...")
                return
            
            if content_en:
                h = self._hash_content(content_en)
                if h and h in self.state['sent_hashes']:
                    logger.warning(f"⛔ Пост уже опубликован по содержимому: {title_en[:50]}...")
                    return

            logger.info(f"📝 Начинается перевод: {title_en[:80]}...")

            loop = asyncio.get_event_loop()

            # ========== ПРИНУДИТЕЛЬНЫЙ ПЕРЕВОД С ПОВТОРНЫМИ ПОПЫТКАМИ ==========
            title_ru = await loop.run_in_executor(None, self._translate_text, title_en)
            
            # Если перевод не удался, пробуем с другим методом
            if not title_ru or not re.search('[а-яА-Я]', title_ru):
                logger.warning("⚠️ Заголовок не переведен, повторная попытка...")
                try:
                    from deep_translator import GoogleTranslator as GT
                    alt_translator = GT(source='auto', target='ru')
                    title_ru = alt_translator.translate(title_en[:500])
                except:
                    pass
                
                if not title_ru or not re.search('[а-яА-Я]', title_ru):
                    logger.error("❌ Не удалось перевести заголовок, используем оригинал")
                    title_ru = title_en
            
            title_ru = clean_title(title_ru)
            if not title_ru:
                title_ru = title_en

            # Перевод контента
            content_ru = ""
            content_en_truncated = content_en[:4000] if len(content_en) > 4000 else content_en
            
            content_ru = await loop.run_in_executor(None, self._translate_text, content_en_truncated)
            
            # Если перевод не удался, пробуем с другим методом
            if not content_ru or not re.search('[а-яА-Я]', content_ru):
                logger.warning("⚠️ Контент не переведен, повторная попытка...")
                try:
                    from deep_translator import GoogleTranslator as GT
                    alt_translator = GT(source='auto', target='ru')
                    content_ru = alt_translator.translate(content_en_truncated[:3000])
                except:
                    pass
                
                if not content_ru or not re.search('[а-яА-Я]', content_ru):
                    logger.error("❌ Не удалось перевести контент, используем оригинал")
                    content_ru = content_en_truncated

            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'\([^)]*(?:AP|Associated Press|Ассошиэйтед Пресс)[^)]*\)', '', content_ru, flags=re.IGNORECASE)

            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, post.get('source', ''), url, title_en, content_en)

            title_clean = clean_title(title_ru)
            if not title_clean:
                title_clean = title_ru
            title_escaped = html.escape(title_clean)
            
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
                                logger.warning(f"⚠️ Caption слишком длинный ({len(message)}), обрезаем...")
                                message = message[:MAX_CAPTION - 50] + "..."
                            
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
                    else:
                        logger.warning(f"URL не ведёт на изображение: {content_type}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            logger.info("📝 Публикация текстом (без фото)")
            text_content = self._truncate_text(content_ru, is_caption=False)
            text_message = f"*{title_escaped}*\n\n{text_content}"
            
            if len(text_message) > MAX_MESSAGE:
                logger.warning(f"⚠️ Сообщение слишком длинное ({len(text_message)}), обрезаем...")
                text_message = text_message[:MAX_MESSAGE - 50] + "..."
            
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_message,
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
                    title_clean = clean_title(title_ru)
                    if not title_clean:
                        title_clean = title_ru
                    text_message = f"{title_clean}\n\n{content_ru}"
                    if len(text_message) > MAX_MESSAGE:
                        text_message = text_message[:MAX_MESSAGE - 50] + "..."
                    
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=text_message,
                        parse_mode=None
                    )
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"❌ Ошибка при отправке без форматирования: {e2}")
            elif "Message is too long" in error_msg:
                logger.warning("⚠️ Сообщение слишком длинное, сокращаем...")
                try:
                    title_clean = clean_title(title_ru)
                    if not title_clean:
                        title_clean = title_ru
                    short_content = content_ru[:2000] + "..."
                    text_message = f"{title_clean}\n\n{short_content}"
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=text_message,
                        parse_mode=None
                    )
                    self._mark_sent(url, title_en, content_en)
                    self._log_post(url, title_en)
                except Exception as e2:
                    logger.error(f"❌ Ошибка при отправке сокращенного сообщения: {e2}")
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 50)
        logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
        if IS_MANUAL_RUN:
            logger.info("🔓 РЕЖИМ РУЧНОГО ЗАПУСКА - ограничения сняты")
        logger.info("=" * 50)

        news = await self.fetch_news()

        if not news:
            logger.info("📭 Новых статей нет")
            return

        published_count = 0
        for article in news:
            if not self._can_post():
                logger.info(f"⏸️ Достигнут лимит публикаций, опубликовано {published_count} статей")
                break
            
            logger.info(f"📤 Публикация статьи {published_count + 1}/{len(news)}")
            await self.publish(article)
            published_count += 1
            
            if published_count < len(news):
                if IS_MANUAL_RUN:
                    logger.info("⏳ Ожидание 10 секунд перед следующей публикацией...")
                    await asyncio.sleep(10)
                else:
                    logger.info("⏳ Ожидание 60 секунд перед следующей публикацией...")
                    await asyncio.sleep(60)
        
        logger.info(f"✅ Опубликовано статей за запуск: {published_count}")

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
