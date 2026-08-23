#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей
Источники: InfoBrics (несколько лент), Global Research, Press TV
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

CLEANUP_DAYS = 30

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
    'reader-funded', 'become a member', 'membership',
    'free books', 'subscribe to our',
    'help us stay afloat', 'make a one-time or recurring donation',
    'comment on global research articles', 'become a member of global research',
    'click the share button', 'follow us on',
    'global research is a reader-funded media',
    # Португальские ключевые слова
    'anos', 'faleceu', 'nascimento', 'morreu', 'nasceu',
    'completa', 'aniversário', 'vivo', 'viva',
]

# === ЯЗЫКИ КОТОРЫЕ НЕ ПУБЛИКУЕМ ===
EXCLUDED_LANGUAGES = ['pt', 'es', 'fr', 'de', 'it', 'ar', 'fa', 'he', 'ja', 'ko', 'zh', 'hi', 'tr']

def is_portuguese_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья на португальском или другом нежелательном языке"""
    combined = (title + ' ' + content).lower()
    
    # Португальские маркеры
    pt_patterns = [
        r'anos',
        r'faleceu',
        r'morreu',
        r'nasceu',
        r'nascimento',
        r'completa',
        r'aniversário',
        r'vivo',
        r'viva',
        r'se fosse vivo',
        r'teria feito',
        r'https://arquivos\.rtp\.pt',
        r'https://www\.brasildefato\.com\.br',
        r'fontes:',
        r'fontes:https://',
        r'fontes:http://',
    ]
    
    for pattern in pt_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    
    # Проверяем наличие ссылок в тексте (признак не новостной статьи)
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, combined)
    if len(urls) > 2:  # Если больше 2 ссылок - это скорее всего не новость
        logger.info(f"❌ Обнаружено {len(urls)} ссылок, статья исключена")
        return True
    
    return False

def is_video_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья видео-статьей (без текста)"""
    combined = (title + ' ' + content).lower()
    video_patterns = [
        r'^video:',
        r'видео:',
        r'youtube',
        r'you tube',
        r'screenshot',
        r'скриншот',
        r'featured image is a screenshot',
        r'recommended image is a screenshot',
        r'documentary:',
        r'документальный фильм:',
    ]
    for pattern in video_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    return False

def is_service_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья служебной (призывы к действию, реклама)"""
    combined = (title + ' ' + content).lower()
    
    service_patterns = [
        r'global research is a reader-funded media',
        r'help us stay afloat',
        r'become a member of global research',
        r'comment on global research articles on our facebook page',
        r'click the share button below to email/forward this article',
        r'follow us on.*?(?:instagram|x|telegram channel)',
        r'make a one-time or recurring donation',
        r'become member of global research',
        r'free books',
        r'reader-funded',
        r'пожертвование',
        r'поддержать',
        r'стать участником',
        r'членство',
        r'подписаться',
        r'поделиться',
        r'подписывайтесь',
        r'помочь нам',
        r'сделать пожертвование',
        r'стать членом',
        r'комментировать статьи',
        r'this text is also available in arabic',
        r'этот текст также доступен на арабском языке',
        r'was this article helpful?',
        r'была ли эта статья полезной?',
        r'support our work',
        r'поддержите нашу работу',
        r'donate to global research',
        r'пожертвовать global research',
        # Португальские служебные фразы
        r'fontes:',
        r'fonte:',
        r'sources:',
        r'source:',
    ]
    
    service_chars = 0
    for pattern in service_patterns:
        matches = re.findall(pattern, combined, re.IGNORECASE)
        service_chars += len(matches) * 50
    
    if len(content) > 0 and service_chars / len(content) > 0.3:
        return True
    
    return False

def is_news_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья новостной (не служебной)"""
    if not title:
        return False
    
    combined = (title + ' ' + content).lower()
    
    # Проверяем на португальский язык
    if is_portuguese_article(title, content):
        logger.info(f"❌ Исключена португальская/не новостная статья: {title[:50]}...")
        return False
    
    # Проверяем на служебные ключевые слова
    for keyword in EXCLUDED_KEYWORDS:
        if keyword.lower() in combined:
            logger.info(f"❌ Исключена статья (ключевое слово '{keyword}'): {title[:50]}...")
            return False
    
    # Проверяем на видео-статьи
    if is_video_article(title, content):
        logger.info(f"❌ Исключена видео-статья: {title[:50]}...")
        return False
    
    # Проверяем на служебные статьи
    if is_service_article(title, content):
        logger.info(f"❌ Исключена служебная статья: {title[:50]}...")
        return False
    
    # Исключаем статьи с очень коротким содержанием (менее 200 символов)
    if len(content) < 200:
        logger.info(f"❌ Исключена статья (слишком короткий контент): {title[:50]}...")
        return False
    
    # Проверяем, содержит ли статья признаки биографии/некролога
    bio_patterns = [
        r'faleceu', r'morreu', r'nasceu', r'nascimento',
        r'completa', r'aniversário', r'anos', r'se fosse vivo',
        r'teria feito', r'nasceu em', r'morreu em',
    ]
    bio_count = 0
    for pattern in bio_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            bio_count += 1
    
    if bio_count > 2:
        logger.info(f"❌ Исключена биографическая статья: {title[:50]}...")
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
        r'Was this article helpful\?.*?(?:\n|$)',
        r'Была ли эта статья полезной\?.*?(?:\n|$)',
        r'Support our work.*?(?:\n|$)',
        r'Поддержите нашу работу.*?(?:\n|$)',
        r'Donate to Global Research.*?(?:\n|$)',
        r'Пожертвовать Global Research.*?(?:\n|$)',
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
        r'This text is also available in Arabic.*?(?:\n|$)',
        r'Этот текст также доступен на арабском языке.*?(?:\n|$)',
        r'Scroll down.*?(?:\n|$)',
        r'Прокрутите вниз.*?(?:\n|$)',
        r'This was done to.*?(?:\n|$)',
        r'Это было сделано для того, чтобы.*?(?:\n|$)',
        r'In San Francisco.*?(?:\n|$)',
        r'В Сан-Франциско.*?(?:\n|$)',
        r'Fontes:.*?(?:\n|$)',
        r'Fonte:.*?(?:\n|$)',
        r'Sources:.*?(?:\n|$)',
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

def cleanup_old_data(state: dict, meta: dict) -> tuple:
    """Очищает старые данные (старше CLEANUP_DAYS дней)"""
    cutoff = get_local_time() - timedelta(days=CLEANUP_DAYS)
    cutoff_str = cutoff.isoformat()
    
    logger.info(f"🧹 Очистка данных старше {CLEANUP_DAYS} дней ({cutoff.strftime('%Y-%m-%d')})")
    
    old_count = len(state.get('posts_log', []))
    new_posts_log = []
    for post in state.get('posts_log', []):
        try:
            post_time = datetime.fromisoformat(post.get('time', ''))
            if post_time > cutoff:
                new_posts_log.append(post)
        except:
            new_posts_log.append(post)
    
    state['posts_log'] = new_posts_log
    new_count = len(new_posts_log)
    logger.info(f"🧹 posts_log: {old_count} → {new_count} (удалено {old_count - new_count})")
    
    old_meta_count = len(meta.get('posts', {}))
    new_posts = {}
    for pid, data in meta.get('posts', {}).items():
        try:
            post_time = datetime.fromisoformat(data.get('time', ''))
            if post_time > cutoff:
                new_posts[pid] = data
        except:
            new_posts[pid] = data
    
    meta['posts'] = new_posts
    new_meta_count = len(new_posts)
    logger.info(f"🧹 posts_meta: {old_meta_count} → {new_meta_count} (удалено {old_meta_count - new_meta_count})")
    
    return state, meta

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        
        self.state, self.meta = cleanup_old_data(self.state, self.meta)
        self._save_state()
        self._save_meta()
        
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
        """Обрезает текст до последнего предложения в пределах max_len"""
        if len(text) <= max_len:
            return text

        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 2:
                result = text[:last + 1].strip()
                if result and result[-1] in '.!?':
                    return result
        
        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1 and last_space > max_len // 2:
            result = text[:last_space].strip()
            if result and result[-1] not in '.!?':
                for punct in ['.', '!', '?']:
                    last_punct = result.rfind(punct)
                    if last_punct != -1 and last_punct > len(result) // 2:
                        return result[:last_punct + 1].strip()
                return result
            return result

        result = text[:max_len].strip()
        if result and result[-1] not in '.!?':
            for punct in ['.', '!', '?']:
                last_punct = result.rfind(punct)
                if last_punct != -1:
                    return result[:last_punct + 1].strip()
            last_space = result.rfind(' ')
            if last_space != -1:
                result = result[:last_space].strip()
            return result + '.'

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        truncated = self._truncate_to_last_sentence(text, max_len)

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
        """Переводит текст на русский язык с повторными попытками"""
        if not text:
            return text
        
        if len(text) < 10:
            logger.info(f"⚠️ Текст слишком короткий для перевода: '{text[:30]}...'")
            return text
        
        try:
            if len(text) > 3000:
                text = text[:3000]
            
            result = self.translator.translate(text)
            
            if result and result != text:
                logger.info(f"✅ Перевод выполнен: '{result[:50]}...'")
                return result
            else:
                logger.warning(f"⚠️ Перевод не изменил текст: '{text[:50]}...'")
                return text
                
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    # ========== ПАРСИНГ INFOBRICS (НЕСКОЛЬКО ЛЕНТ) ==========
    def _get_infobrics_articles(self) -> list:
        """Получает список статей с InfoBrics из нескольких RSS-лент"""
        all_articles = []
        
        # Основная лента
        feeds = [
            'https://infobrics.org/rss/en',
            'https://infobrics.org/rss/en/economic/',
            'https://infobrics.org/rss/en/politics/',
            'https://infobrics.org/rss/en/society/',
        ]
        
        for feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)
                logger.info(f"📡 InfoBrics RSS ({feed_url.split('/')[-2] or 'main'}): {len(feed.entries)} статей")
                for entry in feed.entries[:5]:
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
                    
                    if not title or len(title) < 5:
                        link = entry.get('link', '')
                        url_id = link.split('/')[-1] if link else ''
                        title = f"InfoBrics Article {url_id}"
                    
                    is_duplicate = False
                    for existing in all_articles:
                        if existing['url'] == entry.link:
                            is_duplicate = True
                            break
                    
                    if not is_duplicate:
                        all_articles.append({
                            'url': entry.link, 
                            'title': title
                        })
                        logger.info(f"InfoBrics RSS: найден заголовок '{title[:50]}'")
            except Exception as e:
                logger.warning(f"Ошибка парсинга RSS {feed_url}: {e}")
        
        logger.info(f"📊 InfoBrics: найдено {len(all_articles)} уникальных статей")
        return all_articles[:15]

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
            
            if not title
