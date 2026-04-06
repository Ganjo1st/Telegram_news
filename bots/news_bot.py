#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Чистые статьи, антидубликат, умное обрезание
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

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator

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
MIN_POST_INTERVAL = int(os.getenv('MIN_POST_INTERVAL', '2100'))
MAX_POST_INTERVAL = int(os.getenv('MAX_POST_INTERVAL', '7200'))
MAX_POSTS_PER_DAY = int(os.getenv('MAX_POSTS_PER_DAY', '24'))
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', '7'))

# Таймауты
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))
PUBLISH_TIMEOUT = int(os.getenv('PUBLISH_TIMEOUT', '30'))

# Файлы
STATE_FILE = 'state_news_bot.json'
META_FILE = 'posts_meta.json'

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
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
    utc_now = datetime.now(timezone.utc)
    return utc_now + timedelta(hours=TIMEZONE_OFFSET)

# ========== ФУНКЦИЯ ДЛЯ УДАЛЕНИЯ (AP) ==========
def remove_ap_parentheses(text: str) -> str:
    if not text:
        return text
    pattern = r'\(\s*[AaАа][PpРр]\s*\)'
    cleaned = re.sub(pattern, '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self.load_state()
        self.meta = self.load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')

    # ========== РАБОТА С ФАЙЛАМИ ==========
    def load_state(self) -> dict:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    return {
                        'sent_links': set(state.get('sent_links', [])),
                        'sent_hashes': set(state.get('sent_hashes', [])),
                        'sent_titles': set(state.get('sent_titles', [])),
                        'posts_log': state.get('posts_log', [])
                    }
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")
        return {'sent_links': set(), 'sent_hashes': set(), 'sent_titles': set(), 'posts_log': []}

    def save_state(self):
        try:
            state_to_save = {
                'sent_links': list(self.state['sent_links']),
                'sent_hashes': list(self.state['sent_hashes']),
                'sent_titles': list(self.state['sent_titles']),
                'posts_log': self.state['posts_log']
            }
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния: {e}")

    def load_meta(self) -> dict:
        """Загружает мета-информацию о статьях (источник, автор, и т.д.)"""
        try:
            if os.path.exists(META_FILE):
                with open(META_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки мета: {e}")
        return {'posts': {}}

    def save_meta(self):
        """Сохраняет мета-информацию, очищая записи старше 30 дней"""
        try:
            # Очищаем старые записи (старше 30 дней)
            thirty_days_ago = get_local_time() - timedelta(days=30)
            cleaned_posts = {}
            for post_id, post_data in self.meta.get('posts', {}).items():
                post_time = datetime.fromisoformat(post_data.get('time', ''))
                if post_time > thirty_days_ago:
                    cleaned_posts[post_id] = post_data
            
            self.meta['posts'] = cleaned_posts
            
            with open(META_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.meta, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Мета сохранена, записей: {len(cleaned_posts)}")
        except Exception as e:
            logger.error(f"Ошибка сохранения мета: {e}")

    def add_to_meta(self, post_id: str, source: str, url: str, original_title: str):
        """Добавляет мета-информацию о статье"""
        self.meta['posts'][post_id] = {
            'source': source,
            'url': url,
            'original_title': original_title,
            'time': get_local_time().isoformat()
        }
        self.save_meta()

    # ========== ДЕДУПЛИКАЦИЯ ==========
    def normalize_title(self, title: str) -> str:
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        common_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by']
        words = [w for w in title.split() if w not in common_words]
        return ' '.join(words)[:100]

    def create_content_hash(self, content: str) -> str:
        if not content:
            return ""
        return hashlib.md5(content[:500].encode('utf-8')).hexdigest()

    def is_duplicate(self, url: str, title: str, content: str = "") -> bool:
        if url in self.state['sent_links']:
            return True
        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            return True
        if content:
            h = self.create_content_hash(content)
            if h and h in self.state['sent_hashes']:
                return True
        return False

    def mark_as_sent(self, url: str, title: str, content: str = ""):
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
        local_time = get_local_time()
        self.state['posts_log'].append({
            'link': link,
            'title': title[:50],
            'time': local_time.isoformat()
        })
        if len(self.state['posts_log']) > 100:
            self.state['posts_log'] = self.state['posts_log'][-100:]
        self.save_state()

    # ========== ХАОТИЧНЫЙ РЕЖИМ ==========
    def can_post_now(self) -> bool:
        local_now = get_local_time()
        hour = local_now.hour
        if 23 <= hour or hour < 7:
            return False

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
            return False

        if last_posts:
            last_posts.sort(reverse=True)
            time_since_last = (local_now - last_posts[0]).total_seconds()
            if time_since_last < MIN_POST_INTERVAL:
                return False

        return True

    def get_next_delay(self) -> int:
        delay = random.randint(MIN_POST_INTERVAL, MAX_POST_INTERVAL)
        variation = random.uniform(0.85, 1.15)
        delay = int(delay * variation)
        return max(MIN_POST_INTERVAL, min(delay, MAX_POST_INTERVAL))

    # ========== ОБРЕЗАНИЕ ТЕКСТА (ПО АБЗАЦАМ/ПРЕДЛОЖЕНИЯМ) ==========
    def smart_truncate_by_paragraphs(self, text: str, max_length: int) -> str:
        """
        Обрезает текст до последнего помещающегося абзаца.
        Если абзац один — обрезает до последнего предложения.
        """
        if len(text) <= max_length:
            return text
        
        # Разбиваем на абзацы
        paragraphs = text.split('\n\n')
        
        # Пробуем взять целые абзацы
        result = ""
        for para in paragraphs:
            if len(result) + len(para) + 2 <= max_length:
                if result:
                    result += "\n\n"
                result += para
            else:
                # Если это первый абзац и он не помещается целиком — режем по предложениям
                if not result:
                    return self.truncate_by_sentences(para, max_length)
                else:
                    break
        return result

    def truncate_by_sentences(self, text: str, max_length: int) -> str:
        """Обрезает текст до последнего помещающегося предложения"""
        if len(text) <= max_length:
            return text
        
        # Разбиваем на предложения
        sentences = re.split(r'(?<=[.!?])\s+', text)
        
        result = ""
        for sent in sentences:
            if len(result) + len(sent) + 1 <= max_length:
                if result:
                    result += " "
                result += sent
            else:
                break
        
        return result if result else text[:max_length - 3] + "..."

    # ========== ПЕРЕВОД ==========
    def translate_text(self, text: str) -> str:
        if not text or len(text) < 10:
            return text
        try:
            if len(text) > 4000:
                text = text[:4000]
            translated = self.translator.translate(text)
            return translated if translated else text
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            return text

    # ========== ОЧИСТКА СТАТЬИ ==========
    def clean_article(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'<[^>]+>', '', text)
        
        patterns = [
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
            r'Реклама',
            r'\d+\s*(hour|min|sec|day|minute|second)s?\s+ago',
            r'Updated\s*:?\s*[\d:APM\s-]+',
            r'Published\s*:?\s*[\d:APM\s-]+',
            r'^By\s+[\w\s,]+\n',
        ]
        
        for pattern in patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
        
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ========== ПАРСЕР AP NEWS ==========
    def get_apnews_articles(self):
        try:
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

                if href.startswith('https://apnews.com/'):
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

            unique = []
            seen = set()
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)
            return unique[:10]
        except Exception as e:
            logger.error(f"Ошибка AP News: {e}")
            return []

    def parse_apnews_article(self, url: str):
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = fetch_with_timeout(
                lambda: requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT),
                REQUEST_TIMEOUT
            )
            if not response:
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
            
            # Альтернативный поиск картинки
            if not main_image:
                img = soup.find('img', {'src': re.compile(r'\.jpg|\.png|\.jpeg')})
                if img and img.get('src'):
                    src = img['src']
                    if src.startswith('/'):
                        main_image = 'https://apnews.com' + src
                    elif src.startswith('http'):
                        main_image = src

            # Текст статьи
            article_text = ""
            container = soup.find('article') or soup.find('main')
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    tag.decompose()
                
                paragraphs = []
                for p in container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 30:
                        paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            article_text = self.clean_article(article_text)
            article_text = remove_ap_parentheses(article_text)

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга статьи AP News: {e}")
            return None

    async def fetch_from_apnews(self):
        items = []
        try:
            articles = await asyncio.get_event_loop().run_in_executor(None, self.get_apnews_articles)
            for article in articles[:5]:
                url, title = article['url'], article['title']
                if self.is_duplicate(url, title):
                    continue
                logger.info(f"🔍 AP News: {title[:50]}...")
                data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, url
                )
                if data and not self.is_duplicate(url, title, data['content']):
                    items.append({
                        'title': data['title'],
                        'content': data['content'],
                        'url': url,
                        'source': 'AP News',
                        'image': data.get('main_image')
                    })
        except Exception as e:
            logger.error(f"Ошибка fetch_from_apnews: {e}")
        return items

    # ========== ПУБЛИКАЦИЯ ==========
    async def publish_post(self, post_data: dict):
        """Публикует пост: картинка сверху, текст с обрезанием, без ссылки на источник"""
        try:
            title_en = post_data['title']
            content_en = post_data['content']
            url = post_data['url']
            source = post_data.get('source', '')
            image_url = post_data.get('image')
            
            logger.info(f"📝 Перевод: {title_en[:50]}...")
            
            # Переводим
            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self.translate_text, title_en)
            content_ru = await loop.run_in_executor(None, self.translate_text, content_en)
            
            # Экранируем для Markdown
            title_esc = html.escape(title_ru)
            content_esc = html.escape(content_ru)
            
            # Обрезаем текст по абзацам/предложениям
            # Ограничение Telegram: 4096 символов, оставляем запас под заголовок и ссылку
            MAX_MESSAGE_LEN = 3800
            content_esc = self.smart_truncate_by_paragraphs(content_esc, MAX_MESSAGE_LEN)
            
            # Формируем сообщение (БЕЗ ссылки на источник в тексте)
            message = f"📰 *{title_esc}*\n\n{content_esc}"
            
            # Сохраняем мета-информацию об источнике в отдельный файл
            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self.add_to_meta(post_id, source, url, title_en)
            logger.info(f"📄 Мета сохранена для {post_id}: источник {source}")
            
            # Публикуем с картинкой (если есть)
            try:
                if image_url:
                    # Скачиваем картинку
                    img_response = fetch_with_timeout(
                        lambda: requests.get(image_url, timeout=10),
                        10
                    )
                    if img_response and img_response.status_code == 200:
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=img_response.content,
                            caption=message,
                            parse_mode='Markdown'
                        )
                        logger.info(f"✅ Опубликовано с фото: {title_ru[:50]}...")
                    else:
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=message,
                            parse_mode='Markdown',
                            disable_web_page_preview=False
                        )
                        logger.info(f"✅ Опубликовано без фото: {title_ru[:50]}...")
                else:
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=message,
                        parse_mode='Markdown',
                        disable_web_page_preview=False
                    )
                    logger.info(f"✅ Опубликовано: {title_ru[:50]}...")
                
                # Отмечаем как отправленное
                self.mark_as_sent(url, title_en, content_en)
                self.log_post(url, title_en)
                
            except TelegramError as e:
                if "Can't parse entities" in str(e):
                    logger.warning(f"⚠️ Ошибка Markdown, отправляем без форматирования")
                    plain_message = f"📰 {title_ru}\n\n{content_ru}"
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=plain_message,
                        parse_mode=None
                    )
                    self.mark_as_sent(url, title_en, content_en)
                    self.log_post(url, title_en)
                else:
                    raise e
                    
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("🚀 Запуск сбора новостей...")
        
        all_posts = []
        
        # AP News
        ap_posts = await self.fetch_from_apnews()
        all_posts.extend(ap_posts)
        logger.info(f"📊 AP News: {len(ap_posts)} новых статей")
        
        if not all_posts:
            logger.info("📭 Новых статей не найдено")
            return
        
        # Публикуем первую подходящую
        for post in all_posts:
            if self.can_post_now():
                await self.publish_post(post)
                return
            else:
                logger.info("⏳ Пост отложен из-за ограничений")
                return

    async def run_forever(self):
        logger.info("🤖 Бот запущен в бесконечном режиме")
        while True:
            try:
                await self.run_once()
                delay = self.get_next_delay()
                logger.info(f"⏰ Следующий запуск через {delay // 60} минут")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"❌ Критическая ошибка: {e}")
                await asyncio.sleep(300)

# ========== ТОЧКА ВХОДА ==========
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
