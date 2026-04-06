#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram News Bot - Автоматические публикации новостей с переводом на русский
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

# Ограничения Telegram
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
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT):
    """Безопасное получение URL с таймаутом"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8'
        }
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        logger.error(f"Ошибка запроса {url}: {e}")
        return None


def normalize_image_url(url: str, base_url: str = 'https://apnews.com') -> str | None:
    """Нормализует URL изображения"""
    if not url:
        return None
    
    # Убираем параметры размера
    url = re.sub(r'\?.*$', '', url)
    url = re.sub(r'/reflow/.*?/', '/', url)
    
    # Если URL относительный
    if url.startswith('//'):
        return 'https:' + url
    elif url.startswith('/'):
        return urljoin(base_url, url)
    elif url.startswith('http'):
        return url
    else:
        return None


def download_image(url: str) -> bytes | None:
    """Скачивает изображение и возвращает бинарные данные"""
    try:
        # Нормализуем URL
        img_url = normalize_image_url(url)
        if not img_url:
            logger.warning(f"Неверный URL изображения: {url}")
            return None
        
        logger.info(f"Загрузка изображения: {img_url}")
        response = fetch_url(img_url, timeout=10)
        
        if response and response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'image' in content_type:
                logger.info(f"✅ Изображение загружено, размер: {len(response.content)} байт")
                return response.content
            else:
                logger.warning(f"URL не ведёт на изображение: {content_type}")
                return None
        else:
            logger.warning(f"Не удалось загрузить изображение: статус {response.status_code if response else 'None'}")
            return None
    except Exception as e:
        logger.error(f"Ошибка скачивания изображения: {e}")
        return None


# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')

    # ---------- Работа с файлами ----------
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
            logger.info(f"Мета сохранена, записей: {len(cleaned)}")
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

    # ---------- Дедупликация ----------
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

    # ---------- Контроль публикаций ----------
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

    # ---------- Обрезание текста ----------
    def _truncate_to_last_sentence(self, text: str, max_len: int) -> str:
        """Обрезает текст до последнего предложения, не превышая max_len"""
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

    # ---------- Перевод ----------
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

    # ---------- Парсинг AP News ----------
    def _get_articles_list(self) -> list:
        """Получает список статей с главной AP News"""
        try:
            resp = fetch_url('https://apnews.com/')
            if not resp or resp.status_code != 200:
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
            return unique[:10]
        except Exception as e:
            logger.error(f"Ошибка получения списка статей: {e}")
            return []

    def _parse_article(self, url: str) -> dict | None:
        """Парсит одну статью AP News"""
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')

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

            # Изображение — ищем в meta og:image и в article
            image = None
            
            # Сначала пробуем og:image
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                image = meta_img['content']
                logger.info(f"Найдено og:image: {image[:100]}...")
            
            # Если нет — ищем в article
            if not image:
                article = soup.find('article')
                if article:
                    img = article.find('img', src=re.compile(r'\.jpg|\.png|\.jpeg|\.webp'))
                    if img and img.get('src'):
                        image = img['src']
                        logger.info(f"Найдено img в article: {image[:100]}...")
            
            # Если всё ещё нет — ищем любой большой img
            if not image:
                for img in soup.find_all('img', src=True):
                    src = img.get('src', '')
                    if 'logo' in src.lower():
                        continue
                    if src.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        image = src
                        logger.info(f"Найдено img: {image[:100]}...")
                        break

            # Текст статьи
            container = soup.find('article') or soup.find('main')
            paragraphs = []
            if container:
                for tag in container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    tag.decompose()
                for p in container.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 40:
                        paragraphs.append(text)

            if len(paragraphs) < 2:
                return None

            content = '\n\n'.join(paragraphs)
            content = self._clean_text(content)
            content = remove_ap(content)

            if len(content) < 200:
                return None

            return {
                'title': title,
                'content': content,
                'image': image
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга статьи {url}: {e}")
            return None

    def _clean_text(self, text: str) -> str:
        """Удаляет мусор из текста"""
        if not text:
            return text

        text = re.sub(r'<[^>]+>', '', text)

        patterns = [
            r'Copyright \d+.*?(?:All rights reserved|Associated Press).*?$',
            r'Read more at:?\s*\S+',
            r'Follow us on.*$',
            r'Subscribe to.*$',
            r'Sign up for.*$',
            r'Click here.*$',
            r'Join the conversation.*$',
            r'Email.*@.*$',
            r'\(AP\)\s*',
            r'—\s*AP\s*—?\s*',
            r'The Associated Press$',
            r'AP News$',
        ]
        for p in patterns:
            text = re.sub(p, '', text, flags=re.IGNORECASE | re.MULTILINE)

        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def fetch_news(self) -> list:
        """Собирает новые статьи"""
        items = []
        articles = await asyncio.get_event_loop().run_in_executor(None, self._get_articles_list)

        for article in articles[:5]:
            url = article['url']
            title = article['title']

            if self._is_duplicate(url, title):
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

        return items

    # ---------- Публикация ----------
    async def publish(self, post: dict):
        """Публикует пост в Telegram"""
        try:
            title_en = post['title']
            content_en = post['content']
            url = post['url']
            image_url = post.get('image')

            logger.info(f"📝 Перевод: {title_en[:50]}...")

            # Переводим
            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self._translate, title_en)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            # Сохраняем мета-информацию
            post_id = hashlib.md5(url.encode()).hexdigest()[:16]
            self._add_to_meta(post_id, 'AP News', url, title_en)
            logger.info(f"Мета сохранена: {post_id}")

            # Формируем сообщение
            title_escaped = html.escape(title_ru)
            content_truncated = self._truncate_text(content_ru, is_caption=True)
            message = f"📰 *{title_escaped}*\n\n{content_truncated}"

            # Пробуем отправить с фото
            image_sent = False
            if image_url:
                logger.info(f"🖼️ Пробуем загрузить изображение: {image_url[:100]}...")
                image_data = await loop.run_in_executor(None, download_image, image_url)
                
                if image_data:
                    try:
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=image_data,
                            caption=message,
                            parse_mode='Markdown'
                        )
                        logger.info("✅ Опубликовано С ФОТО")
                        image_sent = True
                    except TelegramError as e:
                        logger.warning(f"Ошибка при отправке фото: {e}")
                        image_sent = False
                else:
                    logger.warning("Не удалось загрузить изображение")
            
            # Если фото не отправилось — отправляем текстом
            if not image_sent:
                logger.info("📝 Отправка текстовым сообщением (без фото)")
                text_message = self._truncate_text(f"📰 *{title_escaped}*\n\n{content_ru}", is_caption=False)
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text_message,
                    parse_mode='Markdown',
                    disable_web_page_preview=False
                )
                logger.info("✅ Опубликовано ТЕКСТОМ")

            # Отмечаем как отправленное
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

    # ---------- Основной цикл ----------
    async def run_once(self):
        """Один цикл работы"""
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
