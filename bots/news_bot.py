#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей AP News
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

MAX_CAPTION = 1024
MAX_MESSAGE = 4096


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)


def remove_ap(text: str) -> str:
    """Удаляет (AP), (АР) и подобное из текста"""
    if not text:
        return text
    cleaned = re.sub(r'\(\s*[AaАа][PpРр]\s*\)', '', text)
    cleaned = re.sub(r'\(AP\s+[^)]+\)', '', cleaned)  # (AP Photo/...)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None


def extract_image_url(soup, base_url: str) -> str | None:
    """Извлекает URL изображения из страницы"""
    
    # 1. Пробуем meta og:image
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        url = meta_img['content']
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return urljoin(base_url, url)
        if url.startswith('http'):
            return url
        logger.info(f"Найдено og:image: {url[:80]}...")
        return url
    
    # 2. Ищем в article img с высоким разрешением
    for img in soup.find_all('img', src=True):
        src = img.get('src', '')
        
        # Пропускаем логотипы и иконки
        if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'svg', 'gif']):
            continue
        
        # Пропускаем слишком маленькие изображения
        width = img.get('width', '')
        height = img.get('height', '')
        if width and height:
            try:
                if int(width) < 200 and int(height) < 200:
                    continue
            except:
                pass
        
        # Проверяем расширение
        if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            if src.startswith('//'):
                full_url = 'https:' + src
            elif src.startswith('/'):
                full_url = urljoin(base_url, src)
            elif src.startswith('http'):
                full_url = src
            else:
                continue
            
            logger.info(f"Найдено img: {full_url[:80]}...")
            return full_url
    
    # 3. Пробуем picture source
    picture = soup.find('picture')
    if picture:
        source = picture.find('source', srcset=True)
        if source:
            srcset = source.get('srcset', '')
            if srcset:
                first_url = srcset.split(',')[0].strip().split(' ')[0]
                if first_url.startswith('//'):
                    return 'https:' + first_url
                if first_url.startswith('/'):
                    return urljoin(base_url, first_url)
                if first_url.startswith('http'):
                    return first_url
    
    return None


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
                if datetime.fromisoformat(data.get('time', '')) > cutoff:
                    cleaned[pid] = data
            self.meta['posts'] = cleaned
            with open(META_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения мета: {e}")

    def _add_to_meta(self, post_id: str, source: str, url: str, title: str):
        self.meta['posts'][post_id] = {
            'source': source,
            'url': url,
            'original_title': title,
            'time': get_local_time().isoformat()
        }
        self._save_meta()

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
            return True
        norm_title = self._normalize_title(title)
        if norm_title and norm_title in self.state['sent_titles']:
            return True
        if content:
            h = self._hash_content(content)
            if h and h in self.state['sent_hashes']:
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
                return text[:last + 1].strip()

        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1:
            return text[:last_space].strip() + "..."

        return text[:max_len - 3].strip() + "..."

    def _truncate_text(self, text: str, is_caption: bool = False) -> str:
        max_len = MAX_CAPTION if is_caption else MAX_MESSAGE
        return self._truncate_to_last_sentence(text, max_len)

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

    # ========== ПАРСИНГ AP NEWS ==========
    def _get_articles_list(self) -> list:
        try:
            resp = fetch_url('https://apnews.com/')
            if not resp or resp.status_code != 200:
                logger.error("Не удалось получить главную страницу AP News")
                return []

            soup = BeautifulSoup(resp.text, 'html.parser')
            articles = []

            for link in soup.find_all('a', href=True):
                href = link['href']
                if '/article/' not in href:
                    continue

                if href.startswith('https://'):
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

            seen = set()
            unique = []
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)
            
            logger.info(f"Найдено статей на главной: {len(unique)}")
            return unique[:10]
        except Exception as e:
            logger.error(f"Ошибка получения списка статей: {e}")
            return []

    def _parse_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp or resp.status_code != 200:
                logger.error(f"Не удалось загрузить статью: {url}")
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

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
            title = title.strip()

            # Изображение
            image_url = extract_image_url(soup, base_url)
            if image_url:
                logger.info(f"✅ Найдено изображение: {image_url[:80]}...")
            else:
                logger.info("❌ Изображение не найдено")

            # Текст статьи
            container = soup.find('article') or soup.find('main')
            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 40:
                        # Удаляем (AP Photo/...) из текста
                        text = re.sub(r'\(AP\s+[^)]+\)', '', text)
                        paragraphs.append(text)

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = remove_ap(content)

            if len(content) < 200:
                return None

            return {
                'title': title,
                'content': content,
                'image': image_url
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга статьи {url}: {e}")
            return None

    async def fetch_news(self) -> list:
        items = []
        articles = await asyncio.get_event_loop().run_in_executor(None, self._get_articles_list)

        for article in articles[:5]:
            url = article['url']
            title = article['title']

            if self._is_duplicate(url, title):
                logger.info(f"Дубликат: {title[:40]}...")
                continue

            logger.info(f"🔍 Парсинг: {title[:50]}...")
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_article, url)

            if data and not self._is_duplicate(url, title, data['content']):
                items.append({
                    'title': data['title'],
                    'content': data['content'],
                    'url': url,
                    'image': data.get('image')
                })
                logger.info(f"✅ Добавлена статья: {data['title'][:50]}...")

        return items

    # ========== ПУБЛИКАЦИЯ ==========
    async def publish(self, post: dict):
        try:
            title_en = post['title']
            content_en = post['content']
            url = post['url']
            image_url = post.get('image')

            logger.info(f"📝 Перевод: {title_en[:50]}...")

            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self._translate, title_en)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, 'AP News', url, title_en)

            title_escaped = html.escape(title_ru)
            content_truncated = self._truncate_text(content_ru, is_caption=True)
            message = f"📰 *{title_escaped}*\n\n{content_truncated}"

            # Публикация с фото
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
                            logger.info("✅ Опубликовано С ФОТО")
                            self._mark_sent(url, title_en, content_en)
                            self._log_post(url, title_en)
                            return
                        except TelegramError as e:
                            logger.warning(f"Ошибка отправки фото: {e}")
                    else:
                        logger.warning(f"URL не ведёт на изображение: {content_type}")
                else:
                    logger.warning("Не удалось загрузить изображение")

            # Фолбэк: публикация текстом
            logger.info("📝 Публикация текстом (без фото)")
            text_message = self._truncate_text(f"📰 *{title_escaped}*\n\n{content_ru}", is_caption=False)
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_message,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )
            logger.info("✅ Опубликовано ТЕКСТОМ")

            self._mark_sent(url, title_en, content_en)
            self._log_post(url, title_en)

        except TelegramError as e:
            if "Can't parse entities" in str(e):
                logger.warning("Ошибка Markdown, отправляем без форматирования")
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"📰 {title_ru}\n\n{content_ru}",
                    parse_mode=None
                )
                self._mark_sent(url, title_en, content_en)
                self._log_post(url, title_en)
            else:
                logger.error(f"Ошибка Telegram: {e}")
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")

    # ========== ОСНОВНОЙ ЦИКЛ ==========
    async def run_once(self):
        logger.info("=" * 50)
        logger.info(f"🚀 Запуск сбора новостей [{get_local_time().strftime('%H:%M:%S')}]")
        logger.info("=" * 50)

        news = await self.fetch_news()
        logger.info(f"📊 Найдено новых статей: {len(news)}")

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
                logger.error(f"Критическая ошибка: {e}")
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
