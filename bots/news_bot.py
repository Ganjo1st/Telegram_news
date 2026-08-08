#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей
Источники: InfoBrics, Global Research, Press TV
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
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

# Интервалы публикации (секунды)
MIN_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '600'))   # 10 минут
MAX_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '1800'))  # 30 минут
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '40'))
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 950
MAX_MESSAGE = 4096

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ИСКЛЮЧЕНИЯ СТАТЕЙ ===
EXCLUDED_KEYWORDS = [
    'donate', 'donation', 'fundraising',
    'пожертвование', 'пожертвовать',
    'video:', 'видео:', 'Video:',
    'youtube', 'YouTube',
    'global research daily', 'the news behind the news',
    'this week\'s most popular', 'most popular articles',
]

def is_video_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья видео-статьей (без текста)"""
    combined = (title + ' ' + content).lower()
    video_patterns = [
        r'video:',
        r'видео:',
        r'youtube',
        r'you tube',
        r'screenshot',
        r'скриншот',
        r'featured image is a screenshot',
        r'recommended image is a screenshot',
    ]
    for pattern in video_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    return False

def is_news_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья новостной (не служебной)"""
    if not title:
        return False
    
    combined = (title + ' ' + content).lower()
    
    for keyword in EXCLUDED_KEYWORDS:
        if keyword.lower() in combined:
            logger.info(f"❌ Исключена статья (ключевое слово '{keyword}'): {title[:50]}...")
            return False
    
    if is_video_article(title, content):
        logger.info(f"❌ Исключена видео-статья: {title[:50]}...")
        return False
    
    if len(content) < 200:
        logger.info(f"❌ Исключена статья (слишком короткий контент): {title[:50]}...")
        return False
    
    service_patterns = [
        r'click the share button',
        r'global research is a reader-funded',
        r'help us stay afloat',
        r'become a member',
        r'пожертвование',
        r'поддержать',
        r'стать участником',
        r'screenshot',
        r'скриншот',
    ]
    
    service_chars = 0
    for pattern in service_patterns:
        service_chars += len(re.findall(pattern, combined, re.IGNORECASE)) * 50
    
    if len(content) > 0 and service_chars / len(content) > 0.5:
        logger.info(f"❌ Исключена статья (слишком много служебного текста >50%): {title[:50]}...")
        return False
    
    return True

def check_image_available(image_url: str, timeout: int = 10) -> bool:
    """Проверяет, доступно ли изображение по URL"""
    if not image_url:
        return False
    try:
        response = requests.head(image_url, timeout=timeout)
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'image' in content_type:
                return True
        logger.warning(f"Изображение недоступно: {image_url} (статус: {response.status_code})")
        return False
    except Exception as e:
        logger.warning(f"Не удалось проверить изображение {image_url}: {e}")
        return False

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None

def clean_globalresearch_content(text: str) -> str:
    """Очищает текст от служебных блоков Global Research"""
    if not text:
        return text
    
    patterns = [
        r'To read this article in the following languages, click the.*?button.*?(?:\n|$)',
        r'To read this article in the following languages, click the Translate Website button.*?(?:\n|$)',
        r'Чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?Перевести веб-сайт.*?под именем автора.*?(?:\n|$)',
        r'Чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?под именем автора.*?(?:\n|$)',
        r'Чтобы прочитать эту статью на следующих языках.*?(?:\n|$)',
        r'Для того чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?(?:\n|$)',
        r'(?:Русский|Китайский|Иврит|Арабский|Персидский|Испанский|Португальский|Португалия|Португальцы|Французский|Немецкий|Итальянский|Японский|Корейский|Турецкий|Сербский|Украинский|中文|Hebrew|عربي|Farsi|Español|Português|Français|Deutsch|Italiano|日本語|한국어|Türkçe|Српски|українська мова)[,.\s]*(?:и еще \d+ языков?|and \d+ more languages?)?[,.\s]*',
        r'[,.\s]*(?:и еще \d+ языков?|and \d+ more languages?)[,.\s]*',
        r'(?:и еще \d+ языков?|and \d+ more languages?)',
        r'Click the share button below to email/forward this article.*?(?:\n|$)',
        r'Follow us on.*?(?:Instagram|X|Telegram Channel).*?(?:\n|$)',
        r'Feel free to repost Global Research articles with proper attribution.*?(?:\n|$)',
        r'Global Research is a reader-funded media.*?(?:\n|$)',
        r'Help us stay afloat.*?(?:\n|$)',
        r'Become Member of Global Research.*?(?:\n|$)',
        r'Free Books!.*?(?:\n|$)',
        r'Make a one-time or recurring donation.*?(?:\n|$)',
        r'Copyright ©.*?(?:\n|$)',
        r'The original source of this article is Global Research.*?(?:\n|$)',
        r'Click the "Translate Website" button.*?(?:\n|$)',
        r'Нажмите кнопку.*?Перевести веб-сайт.*?(?:\n|$)',
        r'To read this article in.*?(?:language|button).*?(?:\n|$)',
        r'Comment on Global Research Articles on our Facebook page.*?(?:\n|$)',
        r'Become a Member of Global Research.*?(?:\n|$)',
        r'Пожертвование.*?(?:\n|$)',
        r'Поддержать.*?(?:\n|$)',
        r'Стать участником.*?(?:\n|$)',
        r'This article was originally published on.*?(?:\n|$)',
        r'Recommended image is a screenshot from the video.*?(?:\n|$)',
        r'Рекомендованное изображение — скриншот из видео.*?(?:\n|$)',
        r'Featured image is a screenshot.*?(?:\n|$)',
        r'^[\s,.;:]+$',
        r'^[,.\s]+$',
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    
    language_names = [
        'португальский', 'португалия', 'португальцы', 'испанский', 
        'французский', 'немецкий', 'итальянский', 'японский', 
        'корейский', 'турецкий', 'сербский', 'иврит', 'персидский', 
        'арабский', 'китайский', 'украинский'
    ]
    for lang in language_names:
        text = re.sub(r'^' + lang + r'[,.\s]*$', '', text, flags=re.IGNORECASE)
    
    text = text.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^[,.\s;]+', '', text)
    
    return text.strip()

def clean_presstv_content(text: str) -> str:
    """Очищает текст Press TV от служебных блоков"""
    if not text:
        return text
    
    patterns = [
        r'Press TV.*?(?:\n|$)',
        r'Published On.*?(?:\n|$)',
        r'Last Updated.*?(?:\n|$)',
        r'Read more.*?(?:\n|$)',
        r'Follow us on.*?(?:\n|$)',
        r'Share this.*?(?:\n|$)',
        r'©.*?Press TV.*?(?:\n|$)',
        r'Source:.*?(?:\n|$)',
        r'By.*?(?:\n|$)',
        # Удаляем текст о переводе на арабский
        r'This text is also available in Arabic.*?(?:\n|$)',
        r'Этот текст также доступен на арабском языке.*?(?:\n|$)',
        r'Scroll down.*?(?:\n|$)',
        r'Прокрутите вниз.*?(?:\n|$)',
        r'This was done to.*?(?:\n|$)',
        r'Это было сделано для того, чтобы.*?(?:\n|$)',
        r'In San Francisco.*?(?:\n|$)',
        r'В Сан-Франциско.*?(?:\n|$)',
        r'^[\s,.;:]+$',
        r'^[,.\s]+$',
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    
    text = text.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

def fix_title_encoding(title: str) -> str:
    """Исправляет проблемы с кодировкой в заголовках"""
    if not title:
        return title
    
    title = html.unescape(title)
    title = title.replace('&#x27;', "'")
    title = title.replace('&quot;', '"')
    title = title.replace('&amp;', '&')
    title = title.replace('&lt;', '<')
    title = title.replace('&gt;', '>')
    title = title.replace('&#39;', "'")
    title = title.replace('’', "'")
    title = title.replace('‘', "'")
    title = re.sub(r'\s+', ' ', title).strip()
    
    return title

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.total_found = 0
        self.total_excluded = 0
        self.total_published = 0
        self.queue_count = 0
        if TEST_MODE:
            logger.info("🧪 ТЕСТОВЫЙ РЕЖИМ ВКЛЮЧЕН - ограничения отключены")

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
        if TEST_MODE:
            logger.info("🧪 Тестовый режим: публикация разрешена")
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
        if TEST_MODE:
            logger.info("🧪 Тестовый режим: задержка 5 секунд")
            return 5
            
        delay = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        delay = int(delay * random.uniform(0.85, 1.15))
        return max(MIN_INTERVAL, min(delay, MAX_INTERVAL))

    def _truncate_to_last_sentence(self, text: str, max_len: int) -> str:
        """Обрезает текст до последнего предложения в пределах max_len, без троеточия"""
        if len(text) <= max_len:
            return text

        # Ищем конец предложения (.!?) в пределах max_len
        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 2:
                result = text[:last + 1].strip()
                # Убеждаемся, что результат не обрывается на середине предложения
                # Проверяем, что последний символ - это пунктуация
                if result and result[-1] in '.!?':
                    return result
        
        # Если нет конца предложения в пределах max_len, ищем последний пробел
        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1 and last_space > max_len // 2:
            result = text[:last_space].strip()
            # Проверяем, что результат заканчивается на пунктуацию, если нет - добавляем точку
            if result and result[-1] not in '.!?':
                # Пробуем найти ближайшую пунктуацию перед обрезкой
                for punct in ['.', '!', '?']:
                    last_punct = result.rfind(punct)
                    if last_punct != -1 and last_punct > len(result) // 2:
                        return result[:last_punct + 1].strip()
                # Если пунктуации нет, оставляем как есть (но это плохо)
                return result
            return result

        # Крайний случай: просто обрезаем и добавляем точку, если нет пунктуации
        result = text[:max_len].strip()
        if result and result[-1] not in '.!?':
            # Пробуем найти последнюю пунктуацию
            for punct in ['.', '!', '?']:
                last_punct = result.rfind(punct)
                if last_punct != -1:
                    return result[:last_punct + 1].strip()
            # Если нет пунктуации, обрезаем по последнему слову и добавляем точку
            last_space = result.rfind(' ')
            if last_space != -1:
                result = result[:last_space].strip()
            return result + '.'

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        truncated = self._truncate_to_last_sentence(text, max_len)

        # Дополнительная проверка: если первый абзац слишком короткий, добавляем второй
        paragraphs = truncated.split('\n\n')
        if len(paragraphs) == 1 and len(paragraphs[0]) < 200 and len(paragraphs[0]) < len(text) * 0.5:
            second_para_start = text.find('\n\n', len(paragraphs[0]))
            if second_para_start != -1:
                second_para_end = text.find('\n\n', second_para_start + 2)
                if second_para_end == -1:
                    second_para_end = len(text)
                additional = text[second_para_start:second_para_end]
                combined = truncated + '\n\n' + additional
                if len(combined) <= max_len:
                    return self._truncate_to_last_sentence(combined, max_len)

        return truncated

    def _translate(self, text: str) -> str:
        if not text or len(text) < 10:
            return text
        try:
            if len(text) > 3000:
                text = text[:3000]
            result = self.translator.translate(text)
            return result if result else text
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            return text

    # ========== ПАРСИНГ INFOBRICS ==========
    def _get_infobrics_articles(self) -> list:
        try:
            feed = feedparser.parse('https://infobrics.org/rss/en')
            articles = []
            for entry in feed.entries[:10]:
                title = entry.get('title', '').strip()
                title = fix_title_encoding(title)
                
                if not title or title == '{[title]}' or len(title) < 5:
                    summary = entry.get('summary', '')
                    summary = re.sub(r'<[^>]+>', '', summary)
                    if summary:
                        title = summary.split('.')[0].strip()
                        if len(title) < 5:
                            title = summary[:100].strip()
                        title = fix_title_encoding(title)
                    logger.info(f"InfoBrics: заголовок извлечен из summary: '{title[:50]}'")
                
                if not title or len(title) < 5:
                    link = entry.get('link', '')
                    url_id = link.split('/')[-1] if link else ''
                    title = f"InfoBrics Article {url_id}"
                    logger.warning(f"InfoBrics: создан заглушечный заголовок: '{title}'")

                articles.append({
                    'url': entry.link, 
                    'title': title
                })
                logger.info(f"InfoBrics RSS: найден заголовок '{title[:50]}'")
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
            base_url = 'https://infobrics.org'

            title = None
            title_div = soup.find('div', class_='title title--big')
            if title_div:
                title = title_div.get_text(strip=True)
                title = fix_title_encoding(title)
                logger.info(f"InfoBrics: заголовок найден в div.title--big: '{title[:50]}'")
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'^BRICS Russia\s*[|]\s*', '', title)
                    title = fix_title_encoding(title)
                    if title:
                        logger.info(f"InfoBrics: заголовок найден в title: '{title[:50]}'")
            
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    title = fix_title_encoding(title)
                    logger.info(f"InfoBrics: заголовок найден в og:title: '{title[:50]}'")

            if not title:
                logger.warning(f"❌ InfoBrics: заголовок не найден для {url}")
                self.total_excluded += 1
                return None

            title = title.strip()
            logger.info(f"InfoBrics: итоговый заголовок '{title[:50]}'")

            image_url = None
            article_img = soup.find('img', class_='article__image')
            if article_img and article_img.get('src'):
                src = article_img['src']
                if src.startswith('//'):
                    image_url = 'https:' + src
                elif src.startswith('/'):
                    image_url = urljoin(base_url, src)
                elif src.startswith('http'):
                    image_url = src
                logger.info(f"InfoBrics: изображение найдено в article__image: {image_url}")
            
            if not image_url:
                meta_img = soup.find('meta', property='og:image')
                if meta_img and meta_img.get('content'):
                    src = meta_img['content']
                    if src.startswith('//'):
                        image_url = 'https:' + src
                    elif src.startswith('/'):
                        image_url = urljoin(base_url, src)
                    elif src.startswith('http'):
                        image_url = src
                    logger.info(f"InfoBrics: изображение найдено в og:image: {image_url}")
            
            if not image_url:
                logger.warning(f"❌ InfoBrics: НЕТ ИЗОБРАЖЕНИЯ, статья пропущена: {title[:50]}")
                self.total_excluded += 1
                return None
            
            if 'infobrics.org' not in image_url:
                if not check_image_available(image_url):
                    logger.warning(f"❌ InfoBrics: ИЗОБРАЖЕНИЕ НЕДОСТУПНО, статья пропущена: {title[:50]}")
                    self.total_excluded += 1
                    return None
            else:
                logger.info(f"InfoBrics: пропущена проверка доступности для infobrics.org")
            
            logger.info(f"✅ InfoBrics: изображение доступно: {image_url}")

            container = soup.find('div', class_='article__text')
            if not container:
                container = soup.find('article')
            if not container:
                container = soup.find('main')

            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more') and not text.startswith('Share this'):
                        text = re.sub(r'См\.\s*$', '', text)
                        paragraphs.append(text)

            if len(paragraphs) < 2:
                logger.warning(f"❌ InfoBrics: недостаточно контента для {url}")
                self.total_excluded += 1
                return None

            content = '\n\n'.join(paragraphs)
            if len(content) < 150:
                logger.warning(f"❌ InfoBrics: контент слишком короткий ({len(content)} символов)")
                self.total_excluded += 1
                return None

            if not is_news_article(title, content):
                logger.info(f"❌ InfoBrics: статья исключена (не новостная): {title[:50]}")
                self.total_excluded += 1
                return None

            self.total_found += 1
            return {'title': title, 'content': content, 'image': image_url, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСИНГ GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:10]:
                title = entry.get('title', '').strip()
                title = fix_title_encoding(title)
                
                if not title or len(title) < 5:
                    summary = entry.get('summary', '')
                    summary = re.sub(r'<[^>]+>', '', summary)
                    if summary:
                        title = summary.split('.')[0].strip()
                        if len(title) < 5:
                            title = summary[:100].strip()
                        title = fix_title_encoding(title)
                    logger.info(f"Global Research: заголовок извлечен из summary: '{title[:50]}'")

                if not title or len(title) < 5:
                    link = entry.get('link', '')
                    url_id = link.split('/')[-1] if link else ''
                    title = f"Global Research Article {url_id}"
                    logger.warning(f"Global Research: создан заглушечный заголовок: '{title}'")

                articles.append({
                    'url': entry.link, 
                    'title': title
                })
                logger.info(f"Global Research RSS: найден заголовок '{title[:50]}'")
            return articles
        except Exception as e:
            logger.error(f"Ошибка Global Research RSS: {e}")
            return []

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                logger.warning(f"Global Research: не удалось загрузить страницу {url}")
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = 'https://www.globalresearch.ca'

            title = None
            
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.get_text(strip=True)
                title = re.sub(r'\s*[-|]\s*(?:Global Research.*|Home.*)$', '', title)
                title = fix_title_encoding(title)
                if title:
                    logger.info(f"Global Research: заголовок найден в title: '{title[:50]}'")
            
            if not title:
                h2 = soup.find('h2', itemprop='headline')
                if h2:
                    title = h2.get_text(strip=True)
                    title = fix_title_encoding(title)
                    logger.info(f"Global Research: заголовок найден в h2[itemprop=headline]: '{title[:50]}'")
            
            if not title:
                title_div = soup.find('div', class_='title')
                if title_div:
                    h2 = title_div.find('h2')
                    if h2:
                        title = h2.get_text(strip=True)
                        title = fix_title_encoding(title)
                        logger.info(f"Global Research: заголовок найден в div.title > h2: '{title[:50]}'")
            
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                    title = fix_title_encoding(title)
                    logger.info(f"Global Research: заголовок найден в h1: '{title[:50]}'")
            
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    title = fix_title_encoding(title)
                    logger.info(f"Global Research: заголовок найден в og:title: '{title[:50]}'")

            if not title:
                feed = feedparser.parse('https://www.globalresearch.ca/feed')
                for entry in feed.entries[:10]:
                    if entry.link == url:
                        title = entry.get('title', '').strip()
                        title = fix_title_encoding(title)
                        if title:
                            logger.info(f"Global Research: заголовок из RSS (запасной): '{title[:50]}'")
                        break

            if not title:
                logger.warning(f"❌ Global Research: заголовок не найден для {url}")
                self.total_excluded += 1
                return None

            title = title.strip()
            logger.info(f"Global Research: итоговый заголовок '{title[:50]}'")

            image_url = None
            
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                src = meta_img['content']
                if src.startswith('//'):
                    image_url = 'https:' + src
                elif src.startswith('/'):
                    image_url = urljoin(base_url, src)
                elif src.startswith('http'):
                    image_url = src
                logger.info(f"Global Research: изображение найдено в og:image: {image_url}")
            
            if not image_url:
                img = soup.find('img', class_='attachment-single-post-thumbnail')
                if img and img.get('src'):
                    src = img['src']
                    if src.startswith('//'):
                        image_url = 'https:' + src
                    elif src.startswith('/'):
                        image_url = urljoin(base_url, src)
                    elif src.startswith('http'):
                        image_url = src
                    logger.info(f"Global Research: изображение найдено в attachment-single-post-thumbnail: {image_url}")
            
            if not image_url:
                thumbnail_div = soup.find('div', class_='postThumbnail')
                if thumbnail_div:
                    img = thumbnail_div.find('img')
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'):
                            image_url = 'https:' + src
                        elif src.startswith('/'):
                            image_url = urljoin(base_url, src)
                        elif src.startswith('http'):
                            image_url = src
                        logger.info(f"Global Research: изображение найдено в postThumbnail: {image_url}")
            
            is_video = False
            video_elem = soup.find('video') or soup.find('iframe') or soup.find('div', class_='video')
            if video_elem:
                is_video = True
                logger.info(f"Global Research: обнаружена видео-статья: {title[:50]}")
            
            if not image_url and not is_video:
                logger.warning(f"❌ Global Research: НЕТ ИЗОБРАЖЕНИЯ, статья пропущена: {title[:50]}")
                self.total_excluded += 1
                return None
            
            if image_url and not check_image_available(image_url):
                if is_video:
                    logger.info(f"Global Research: видео-статья без изображения, пропускаем: {title[:50]}")
                    self.total_excluded += 1
                    return None
                logger.warning(f"❌ Global Research: ИЗОБРАЖЕНИЕ НЕДОСТУПНО, статья пропущена: {title[:50]}")
                self.total_excluded += 1
                return None
            
            if image_url:
                logger.info(f"✅ Global Research: изображение доступно: {image_url}")

            container = soup.find('div', itemprop='articleBody')
            if not container:
                container = soup.find('div', class_='content')
            if not container:
                container = soup.find('div', class_='post-content')
            if not container:
                container = soup.find('div', class_='entry-content')
            if not container:
                container = soup.find('article')
            if not container:
                container = soup.find('main')

            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more') and not text.startswith('Share this'):
                        if not text.startswith('Copyright') and not text.startswith('©'):
                            if not text.startswith('Image:'):
                                text = clean_globalresearch_content(text)
                                if text:
                                    paragraphs.append(text)

            content = '\n\n'.join(paragraphs)
            content = clean_globalresearch_content(content)

            if len(content) < 150 and not is_video:
                logger.warning(f"❌ Global Research: контент слишком короткий ({len(content)} символов)")
                self.total_excluded += 1
                return None

            if is_video and len(content) < 100:
                logger.info(f"❌ Global Research: видео-статья без текста, пропускаем: {title[:50]}")
                self.total_excluded += 1
                return None

            if not is_news_article(title, content):
                logger.info(f"❌ Global Research: статья исключена (не новостная): {title[:50]}")
                self.total_excluded += 1
                return None

            self.total_found += 1
            return {'title': title, 'content': content, 'image': image_url, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга Global Research: {e}")
            return None

    # ========== ПАРСИНГ PRESS TV ==========
    def _get_presstv_articles(self) -> list:
        """Получает список статей с Press TV через RSS"""
        try:
            feed = feedparser.parse('https://www.presstv.ir/Feed/FeedRss')
            articles = []
            for entry in feed.entries[:10]:
                title = entry.get('title', '').strip()
                title = fix_title_encoding(title)
                
                if not title or len(title) < 5:
                    continue
                
                articles.append({
                    'url': entry.link,
                    'title': title
                })
                logger.info(f"Press TV RSS: найден заголовок '{title[:50]}'")
            return articles
        except Exception as e:
            logger.error(f"Ошибка Press TV RSS: {e}")
            return []

    def _parse_presstv_article(self, url: str) -> dict | None:
        """Парсит отдельную статью Press TV"""
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = 'https://www.presstv.ir'

            # === ПОИСК ЗАГОЛОВКА ===
            title = None
            
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)
                title = fix_title_encoding(title)
                logger.info(f"Press TV: заголовок найден в h1: '{title[:50]}'")
            
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    title = fix_title_encoding(title)
                    logger.info(f"Press TV: заголовок найден в og:title: '{title[:50]}'")
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'\s*[-|]\s*Press TV.*$', '', title)
                    title = fix_title_encoding(title)
                    if title:
                        logger.info(f"Press TV: заголовок найден в title: '{title[:50]}'")

            if not title:
                logger.warning(f"❌ Press TV: заголовок не найден для {url}")
                self.total_excluded += 1
                return None

            title = title.strip()
            logger.info(f"Press TV: итоговый заголовок '{title[:50]}'")

            # === ПОИСК ИЗОБРАЖЕНИЯ ===
            image_url = None
            
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                src = meta_img['content']
                if src.startswith('//'):
                    image_url = 'https:' + src
                elif src.startswith('/'):
                    image_url = urljoin(base_url, src)
                elif src.startswith('http'):
                    image_url = src
                logger.info(f"Press TV: изображение найдено в og:image: {image_url}")
            
            if not image_url:
                article = soup.find('article') or soup.find('div', class_='content')
                if article:
                    img = article.find('img')
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'):
                            image_url = 'https:' + src
                        elif src.startswith('/'):
                            image_url = urljoin(base_url, src)
                        elif src.startswith('http'):
                            image_url = src
                        logger.info(f"Press TV: изображение найдено в article img: {image_url}")
            
            if not image_url:
                logger.warning(f"❌ Press TV: НЕТ ИЗОБРАЖЕНИЯ, статья пропущена: {title[:50]}")
                self.total_excluded += 1
                return None
            
            if not check_image_available(image_url):
                logger.warning(f"❌ Press TV: ИЗОБРАЖЕНИЕ НЕДОСТУПНО, статья пропущена: {title[:50]}")
                self.total_excluded += 1
                return None
            
            logger.info(f"✅ Press TV: изображение доступно: {image_url}")

            # === ПОИСК КОНТЕНТА ===
            container = soup.find('div', class_='content') or soup.find('article') or soup.find('main')
            
            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style', 'iframe']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 30 and not text.startswith('Read more'):
                        text = clean_presstv_content(text)
                        if text:
                            paragraphs.append(text)

            content = '\n\n'.join(paragraphs)
            content = clean_presstv_content(content)

            if len(content) < 150:
                logger.warning(f"❌ Press TV: контент слишком короткий ({len(content)} символов)")
                self.total_excluded += 1
                return None

            if not is_news_article(title, content):
                logger.info(f"❌ Press TV: статья исключена (не новостная): {title[:50]}")
                self.total_excluded += 1
                return None

            self.total_found += 1
            return {'title': title, 'content': content, 'image': image_url, 'source': 'Press TV', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга Press TV: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []
        self.total_found = 0
        self.total_excluded = 0

        # 1. InfoBrics
        logger.info("📰 Парсинг InfoBrics...")
        ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for article in ib_articles[:5]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ InfoBrics: {data['title'][:50]}...")

        # 2. Global Research
        logger.info("📰 Парсинг Global Research...")
        gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for article in gr_articles[:5]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_globalresearch_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ Global Research: {data['title'][:50]}...")

        # 3. Press TV
        logger.info("📰 Парсинг Press TV...")
        pt_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_presstv_articles)
        for article in pt_articles[:5]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_presstv_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ Press TV: {data['title'][:50]}...")

        self.total_found = len(items) + self.total_excluded
        logger.info(f"📊 Всего новых статей: {len(items)}")
        logger.info(f"📊 Найдено всего: {self.total_found}, исключено: {self.total_excluded}")
        
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
            title_ru = fix_title_encoding(title_ru)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = clean_globalresearch_content(content_ru)
            content_ru = clean_presstv_content(content_ru)

            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, post.get('source', ''), url, title_en, content_en)

            title_escaped = html.escape(title_ru)
            content_truncated = self._truncate_text(content_ru, is_caption=True)

            message = f"*{title_escaped}*\n\n{content_truncated}"

            if image_url:
                logger.info(f"🖼️ Загрузка изображения: {image_url[:80]}...")
                img_response = fetch_url(image_url, timeout=15)

                if img_response and img_response.status_code == 200:
                    content_type = img_response.headers.get('Content-Type', '')
                    if 'image' in content_type:
                        if len(message) <= 1024:
                            try:
                                await self.bot.send_photo(
                                    chat_id=CHANNEL_ID,
                                    photo=img_response.content,
                                    caption=message,
                                    parse_mode='Markdown'
                                )
                                logger.info("✅ Опубликовано С ФОТО")
                                self._mark_sent(url, title_en, content_en)
                                self._log_post(url, title_en)
                                self.total_published += 1
                                
                                self.queue_count = len(self.state['posts_log'])
                                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
                                return
                            except TelegramError as e:
                                logger.warning(f"Ошибка отправки фото: {e}")
                        else:
                            shorter_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru, is_caption=True)}"
                            if len(shorter_message) > 1024:
                                shorter_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru[:500], is_caption=True)}"
                            try:
                                await self.bot.send_photo(
                                    chat_id=CHANNEL_ID,
                                    photo=img_response.content,
                                    caption=shorter_message,
                                    parse_mode='Markdown'
                                )
                                logger.info("✅ Опубликовано С ФОТО (обрезанный текст)")
                                self._mark_sent(url, title_en, content_en)
                                self._log_post(url, title_en)
                                self.total_published += 1
                                
                                self.queue_count = len(self.state['posts_log'])
                                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
                                return
                            except TelegramError as e:
                                logger.warning(f"Ошибка отправки фото (обрезанный текст): {e}")
                    else:
                        logger.warning(f"URL не ведёт на изображение: {content_type}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            logger.info("📝 Публикация текстом (без фото)")
            text_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru, is_caption=False)}"
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )
            logger.info("✅ Опубликовано ТЕКСТОМ")

            self._mark_sent(url, title_en, content_en)
            self._log_post(url, title_en)
            self.total_published += 1
            
            self.queue_count = len(self.state['posts_log'])
            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")

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
                    self.total_published += 1
                except Exception as e2:
                    logger.error(f"❌ Ошибка при отправке без форматирования: {e2}")
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 50)
        logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
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
            
            await self.publish(article)
            published_count += 1
            
            if len(news) > 1 and published_count < len(news):
                await asyncio.sleep(10)
        
        logger.info(f"📊 Опубликовано статей: {published_count} из {len(news)}")

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
