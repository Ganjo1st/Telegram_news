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

# Интервалы публикации (секунды) - УМЕНЬШЕНЫ ДЛЯ ЧАЩЕ ПУБЛИКАЦИИ
MIN_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '600'))   # 10 минут
MAX_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '1800'))  # 30 минут
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '30'))
TIMEZONE_OFFSET = 7

REQUEST_TIMEOUT = 15

STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

MAX_CAPTION = 950
MAX_MESSAGE = 4096

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ИСКЛЮЧЕНИЯ СТАТЕЙ - ОСЛАБЛЕНЫ ===
EXCLUDED_KEYWORDS = [
    # Оставляем только самые важные для исключения
    'donate', 'donation', 'fundraising',
    'пожертвование', 'пожертвовать',
    # Убрали 'reader-funded', 'become a member', 'subscribe' и другие слишком строгие
]

def is_news_article(title: str, content: str) -> bool:
    """Проверяет, является ли статья новостной (не служебной) - ОСЛАБЛЕНА"""
    if not title:
        return False
    
    combined = (title + ' ' + content).lower()
    
    # Проверяем только самые явные служебные ключевые слова
    for keyword in EXCLUDED_KEYWORDS:
        if keyword.lower() in combined:
            logger.info(f"❌ Исключена статья (ключевое слово '{keyword}'): {title[:50]}...")
            return False
    
    # Исключаем статьи с очень коротким содержанием (менее 200 символов)
    if len(content) < 200:
        logger.info(f"❌ Исключена статья (слишком короткий контент): {title[:50]}...")
        return False
    
    # Ослабленная проверка на служебный текст - только если более 50% текста служебный
    service_patterns = [
        r'click the share button',
        r'global research is a reader-funded',
        r'help us stay afloat',
        r'become a member',
        r'пожертвование',
        r'поддержать',
        r'стать участником',
    ]
    
    service_chars = 0
    for pattern in service_patterns:
        service_chars += len(re.findall(pattern, combined, re.IGNORECASE)) * 50
    
    # Увеличили порог с 30% до 50%
    if len(content) > 0 and service_chars / len(content) > 0.5:
        logger.info(f"❌ Исключена статья (слишком много служебного текста >50%): {title[:50]}...")
        return False
    
    return True

# === УВЕЛИЧИВАЕМ КОЛИЧЕСТВО СТАТЕЙ ДЛЯ ПАРСИНГА ===
def _get_infobrics_articles(self) -> list:
    try:
        feed = feedparser.parse('https://infobrics.org/rss/en')
        articles = []
        # Увеличили с 5 до 10 статей за раз
        for entry in feed.entries[:10]:
            title = entry.get('title', '').strip()
            
            if not title or title == '{[title]}' or len(title) < 5:
                summary = entry.get('summary', '')
                summary = re.sub(r'<[^>]+>', '', summary)
                if summary:
                    title = summary.split('.')[0].strip()
                    if len(title) < 5:
                        title = summary[:100].strip()
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

def _get_globalresearch_articles(self) -> list:
    try:
        feed = feedparser.parse('https://www.globalresearch.ca/feed')
        articles = []
        # Увеличили с 5 до 10 статей за раз
        for entry in feed.entries[:10]:
            title = entry.get('title', '').strip()
            
            if not title or len(title) < 5:
                summary = entry.get('summary', '')
                summary = re.sub(r'<[^>]+>', '', summary)
                if summary:
                    title = summary.split('.')[0].strip()
                    if len(title) < 5:
                        title = summary[:100].strip()
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

# === УВЕЛИЧИВАЕМ КОЛИЧЕСТВО ПУБЛИКУЕМЫХ СТАТЕЙ ===
async def fetch_news(self) -> list:
    items = []
    self.total_found = 0
    self.total_excluded = 0

    logger.info("📰 Парсинг InfoBrics...")
    ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
    # Увеличили с 3 до 5 статей для публикации
    for article in ib_articles[:5]:
        if self._is_duplicate(article['url'], article['title']):
            continue
        data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
        if data and not self._is_duplicate(article['url'], article['title'], data['content']):
            items.append(data)
            logger.info(f"✅ InfoBrics: {data['title'][:50]}...")

    logger.info("📰 Парсинг Global Research...")
    gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
    # Увеличили с 3 до 5 статей для публикации
    for article in gr_articles[:5]:
        if self._is_duplicate(article['url'], article['title']):
            continue
        data = await asyncio.get_event_loop().run_in_executor(None, self._parse_globalresearch_article, article['url'])
        if data and not self._is_duplicate(article['url'], article['title'], data['content']):
            items.append(data)
            logger.info(f"✅ Global Research: {data['title'][:50]}...")

    self.total_found = len(items) + self.total_excluded
    logger.info(f"📊 Всего новых статей: {len(items)}")
    logger.info(f"📊 Найдено всего: {self.total_found}, исключено: {self.total_excluded}")
    
    return items

# === ПУБЛИКУЕМ ВСЕ НАЙДЕННЫЕ СТАТЬИ (НЕ ТОЛЬКО ПЕРВУЮ) ===
async def run_once(self):
    logger.info("=" * 50)
    logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
    logger.info("=" * 50)

    news = await self.fetch_news()

    if not news:
        logger.info("📭 Новых статей нет")
        return

    # Публикуем все найденные статьи, а не только первую
    published_count = 0
    for article in news:
        if not self._can_post():
            logger.info(f"⏸️ Достигнут лимит публикаций, опубликовано {published_count} статей")
            break
        
        await self.publish(article)
        published_count += 1
        
        # Небольшая задержка между публикациями
        if len(news) > 1 and published_count < len(news):
            await asyncio.sleep(10)
    
    logger.info(f"📊 Опубликовано статей: {published_count} из {len(news)}")
