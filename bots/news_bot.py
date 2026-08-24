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
from deep_translator import GoogleTranslator, MyMemoryTranslator, PonsTranslator

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('news_bot')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

MIN_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '300'))   # 5 минут
MAX_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '1800'))  # 30 минут
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '30'))
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
    'anos', 'faleceu', 'nascimento', 'morreu', 'nasceu',
    'completa', 'aniversário', 'vivo', 'viva',
]

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ОПРЕДЕЛЕНИЯ АКТУАЛЬНЫХ НОВОСТЕЙ ===
RELEVANT_KEYWORDS = [
    'brics', 'russia', 'china', 'india', 'brazil', 'south africa', 'egypt', 'ethiopia', 'iran', 'uae',
    'brics', 'россия', 'китай', 'индия', 'бразилия', 'юар', 'египет', 'эфиопия', 'иран', 'оаэ',
    'nato', 'trump', 'putin', 'zelensky', 'ukraine', 'russia', 'us', 'usa', 'america',
    'energy', 'oil', 'gas', 'trade', 'economy', 'finance', 'bank', 'currency',
    'war', 'peace', 'sanctions', 'security', 'defense', 'military',
    'technology', 'ai', 'digital', 'innovation', 'space',
    'summit', 'meeting', 'conference', 'agreement', 'deal',
    'election', 'vote', 'parliament', 'government',
    'crisis', 'emergency', 'disaster', 'climate',
    'india', 'моди', 'modi',
]

def is_relevant_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья актуальной новостью (по ключевым словам)"""
    combined = (title + ' ' + content).lower()
    
    # Проверяем наличие ключевых слов
    for keyword in RELEVANT_KEYWORDS:
        if keyword.lower() in combined:
            return True
    
    # Если нет ключевых слов, проверяем длину и наличие дат
    if len(content) < 300:
        return False
    
    # Проверяем наличие дат (признак свежей новости)
    date_patterns = [
        r'\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}',
        r'\d{4}-\d{2}-\d{2}',
        r'\d{2}\.\d{2}\.\d{4}',
        r'(?:today|yesterday|tonight|this morning|this afternoon|this evening)',
        r'сегодня|вчера|этой\s+ночью|этим\s+утром',
    ]
    for pattern in date_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    
    return False

def translate_with_fallback(text: str, source: str = 'en', target: str = 'ru') -> str:
    """Переводит текст с использованием нескольких переводчиков (запасные варианты)"""
    if not text or len(text) < 10:
        return text
    
    translators = [
        ('Google', lambda: GoogleTranslator(source=source, target=target).translate(text)),
        ('MyMemory', lambda: MyMemoryTranslator(source=source, target=target).translate(text)),
        ('Pons', lambda: PonsTranslator(source=source, target=target).translate(text)),
    ]
    
    for name, translate_func in translators:
        try:
            result = translate_func()
            if result and result != text:
                logger.info(f"✅ Перевод выполнен ({name}): '{result[:50]}...'")
                return result
        except Exception as e:
            logger.warning(f"⚠️ Ошибка {name} переводчика: {e}")
            continue
    
    logger.warning(f"⚠️ Все переводчики не смогли перевести текст: '{text[:50]}...'")
    return text

def is_portuguese_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья на португальском или другом нежелательном языке"""
    combined = (title + ' ' + content).lower()
    
    pt_patterns = [
        r'anos', r'faleceu', r'morreu', r'nasceu', r'nascimento',
        r'completa', r'aniversário', r'vivo', r'viva',
        r'se fosse vivo', r'teria feito',
        r'https://arquivos\.rtp\.pt', r'https://www\.brasildefato\.com\.br',
        r'fontes:', r'fontes:https://', r'fontes:http://',
    ]
    
    for pattern in pt_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, combined)
    if len(urls) > 2:
        logger.info(f"❌ Обнаружено {len(urls)} ссылок, статья исключена")
        return True
    
    return False

def is_video_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья видео-статьей (без текста)"""
    combined = (title + ' ' + content).lower()
    video_patterns = [
        r'^video:', r'видео:', r'youtube', r'you tube',
        r'screenshot', r'скриншот',
        r'featured image is a screenshot', r'recommended image is a screenshot',
        r'documentary:', r'документальный фильм:',
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
        r'fontes:', r'fonte:', r'sources:', r'source:',
    ]
    
    service_chars = 0
    for pattern in service_patterns:
        matches = re.findall(pattern, combined, re.IGNORECASE)
        service_chars += len(matches) * 50
    
    if len(content) > 0 and service_chars / len(content) > 0.3:
        return True
    
    return False

def is_news_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья новостной (не служебной и актуальной)"""
    if not title:
        return False
    
    combined = (title + ' ' + content).lower()
    
    # Проверка на португальский
    if is_portuguese_article(title, content):
        logger.info(f"❌ Исключена португальская/не новостная статья: {title[:50]}...")
        return False
    
    # Проверка на служебные ключевые слова
    for keyword in EXCLUDED_KEYWORDS:
        if keyword.lower() in combined:
            logger.info(f"❌ Исключена статья (ключевое слово '{keyword}'): {title[:50]}...")
            return False
    
    # Проверка на видео-статьи
    if is_video_article(title, content):
        logger.info(f"❌ Исключена видео-статья: {title[:50]}...")
        return False
    
    # Проверка на служебные статьи
    if is_service_article(title, content):
        logger.info(f"❌ Исключена служебная статья: {title[:50]}...")
        return False
    
    # Проверка на актуальность
    if not is_relevant_article(title, content):
        logger.info(f"❌ Исключена неактуальная статья: {title[:50]}...")
        return False
    
    # Исключаем статьи с очень коротким содержанием (менее 200 символов)
    if len(content) < 200:
        logger.info(f"❌ Исключена статья (слишком короткий контент): {title[:50]}...")
        return False
    
    # Проверка на биографические статьи
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

# ========== ОСТАЛЬНОЙ КОД (НЕ ИЗМЕНЯЕТСЯ) ==========
# ... (весь остальной код остается таким же, как в предыдущей версии)
