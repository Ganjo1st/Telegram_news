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
    
    # Удаляем блок с переводом на разные языки (включая все варианты)
    patterns = [
        # Английский вариант
        r'To read this article in the following languages, click the.*?button.*?(?:\n|$)',
        r'To read this article in the following languages, click the Translate Website button.*?(?:\n|$)',
        
        # Русский вариант (основной)
        r'Чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?Перевести веб-сайт.*?под именем автора.*?(?:\n|$)',
        r'Чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?под именем автора.*?(?:\n|$)',
        r'Чтобы прочитать эту статью на следующих языках.*?(?:\n|$)',
        
        # Перечисление языков (все варианты)
        r'(?:عربي|עברית|українська мова|فارسی|Español|Português|Русский|中文|Français|Deutsch|Italiano|日本語|한국어|Türkçe|Српски|Arabic|Hebrew|Ukrainian|Farsi|Spanish|Portuguese|Russian|Chinese|French|German|Italian|Japanese|Korean|Turkish|Serbian)[,.\s]*(?:и еще \d+ языков?)?[,.\s]*',
        r'(?:عربي|עברית|українська мова|فارسی|Español|Português|Русский|中文|Français|Deutsch|Italiano|日本語|한국어|Türkçe|Српски)[,.\s]*(?:и еще \d+ языков?)?[,.\s]*',
        
        # Другие варианты
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
        
        # Удаляем строки с переводом (более широкий паттерн)
        r'To read this article in the following languages, click the.*?button.*?(?:\n|$)',
        r'To read this article in.*?(?:language|button).*?(?:\n|$)',
        r'Click the "Translate Website" button.*?(?:\n|$)',
        r'Нажмите кнопку.*?Перевести веб-сайт.*?(?:\n|$)',
        r'Для того чтобы прочитать эту статью на следующих языках, нажмите кнопку.*?(?:\n|$)',
        r'Para leer este artículo en los siguientes idiomas, haga clic en el botón.*?(?:\n|$)',
        r'Pour lire cet article dans les langues suivantes, cliquez sur le bouton.*?(?:\n|$)',
        r'Um diesen Artikel in den folgenden Sprachen zu lesen, klicken Sie auf die Schaltfläche.*?(?:\n|$)',
        r'Per leggere questo articolo nelle seguenti lingue, fare clic sul pulsante.*?(?:\n|$)',
        r'Для того щоб прочитати цю статтю на наступних мовах, натисніть кнопку.*?(?:\n|$)',
        r'לקריאת מאמר זה בשפות הבאות, לחץ על הכפתור.*?(?:\n|$)',
        r'برای خواندن این مقاله به زبان‌های زیر، روی دکمه کلیک کنید.*?(?:\n|$)',
        r'이 기사를 다음 언어로 읽으려면 버튼을 클릭하세요.*?(?:\n|$)',
        r'この記事を次の言語で読むには、ボタンをクリックしてください.*?(?:\n|$)',
        r'Bu makaleyi aşağıdaki dillerde okumak için düğmeye tıklayın.*?(?:\n|$)',
        r'Да бисте прочитали овај чланак на следећим језицима, кликните на дугме.*?(?:\n|$)',
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    
    # Удаляем пустые строки в начале и конце
    text = text.strip()
    
    # Удаляем лишние переводы строк
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text

# ========== ОСНОВНОЙ КЛАСС ==========
class NewsBot:
    def __init__(self):
        self.state = self._load_state()
        self.meta = self._load_meta()
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
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
        if len(text) <= max_len:
            return text

        for punct in ['.', '!', '?']:
            last = text.rfind(punct, 0, max_len)
            if last != -1 and last > max_len // 2:
                return text[:last + 1].strip()

        last_space = text.rfind(' ', 0, max_len)
        if last_space != -1:
            return text[:last_space].strip()

        return text[:max_len].strip()

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
            for entry in feed.entries[:5]:
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

    def _parse_infobrics_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            title = None
            title_div = soup.find('div', class_='title title--big')
            if title_div:
                title = title_div.get_text(strip=True)
                logger.info(f"InfoBrics: заголовок найден в div.title--big: '{title[:50]}'")
            
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    title = re.sub(r'^BRICS Russia\s*[|]\s*', '', title)
                    if title:
                        logger.info(f"InfoBrics: заголовок найден в title: '{title[:50]}'")
            
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    logger.info(f"InfoBrics: заголовок найден в og:title: '{title[:50]}'")

            if not title:
                title = "InfoBrics Article"
                logger.warning(f"InfoBrics: заголовок не найден, используется заглушка")

            title = title.strip()
            logger.info(f"Парсинг InfoBrics: итоговый заголовок '{title[:50]}'")

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
            
            logger.info(f"InfoBrics: найдено изображение {image_url[:50] if image_url else 'None'}")

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
                logger.warning(f"InfoBrics: недостаточно контента для {url}")
                return None

            content = '\n\n'.join(paragraphs)
            if len(content) < 150:
                logger.warning(f"InfoBrics: контент слишком короткий ({len(content)} символов)")
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'InfoBrics', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСИНГ GLOBAL RESEARCH ==========
    def _get_globalresearch_articles(self) -> list:
        try:
            feed = feedparser.parse('https://www.globalresearch.ca/feed')
            articles = []
            for entry in feed.entries[:5]:
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

    def _parse_globalresearch_article(self, url: str) -> dict | None:
        try:
            resp = fetch_url(url)
            if not resp:
                logger.warning(f"Global Research: не удалось загрузить страницу {url}")
                feed = feedparser.parse('https://www.globalresearch.ca/feed')
                for entry in feed.entries[:10]:
                    if entry.link == url:
                        title = entry.get('title', '').strip()
                        if title:
                            summary = entry.get('summary', '')
                            summary = re.sub(r'<[^>]+>', '', summary)
                            summary = clean_globalresearch_content(summary)
                            image_url = None
                            if summary and 'src="' in summary:
                                img_match = re.search(r'src="([^"]+)"', summary)
                                if img_match:
                                    image_url = img_match.group(1)
                            if summary:
                                return {
                                    'title': title,
                                    'content': summary[:500],
                                    'image': image_url,
                                    'source': 'Global Research',
                                    'url': url
                                }
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            base_url = f'https://{url.split("/")[2]}'

            # === ПОИСК ЗАГОЛОВКА ===
            title = None
            
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.get_text(strip=True)
                title = re.sub(r'\s*[-|]\s*(?:Global Research.*|Home.*)$', '', title)
                if title:
                    logger.info(f"Global Research: заголовок найден в title: '{title[:50]}'")
            
            if not title:
                h2 = soup.find('h2', itemprop='headline')
                if h2:
                    title = h2.get_text(strip=True)
                    logger.info(f"Global Research: заголовок найден в h2[itemprop=headline]: '{title[:50]}'")
            
            if not title:
                title_div = soup.find('div', class_='title')
                if title_div:
                    h2 = title_div.find('h2')
                    if h2:
                        title = h2.get_text(strip=True)
                        logger.info(f"Global Research: заголовок найден в div.title > h2: '{title[:50]}'")
            
            if not title:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                    logger.info(f"Global Research: заголовок найден в h1: '{title[:50]}'")
            
            if not title:
                meta_title = soup.find('meta', property='og:title')
                if meta_title and meta_title.get('content'):
                    title = meta_title['content']
                    logger.info(f"Global Research: заголовок найден в og:title: '{title[:50]}'")

            if not title:
                feed = feedparser.parse('https://www.globalresearch.ca/feed')
                for entry in feed.entries[:10]:
                    if entry.link == url:
                        title = entry.get('title', '').strip()
                        if title:
                            logger.info(f"Global Research: заголовок из RSS (запасной): '{title[:50]}'")
                        break

            if not title:
                title = "Global Research Article"
                logger.warning(f"Global Research: заголовок не найден, используется заглушка")

            title = title.strip()
            logger.info(f"Парсинг Global Research: итоговый заголовок '{title[:50]}'")

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
                logger.info(f"Global Research: изображение найдено в og:image")
            
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
                    logger.info(f"Global Research: изображение найдено в attachment-single-post-thumbnail")
            
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
                        logger.info(f"Global Research: изображение найдено в postThumbnail")
            
            if not image_url:
                twitter_img = soup.find('meta', attrs={'name': 'twitter:image'})
                if twitter_img and twitter_img.get('content'):
                    src = twitter_img['content']
                    if src.startswith('//'):
                        image_url = 'https:' + src
                    elif src.startswith('/'):
                        image_url = urljoin(base_url, src)
                    elif src.startswith('http'):
                        image_url = src
                    logger.info(f"Global Research: изображение найдено в twitter:image")
            
            if image_url:
                logger.info(f"Global Research: итоговое изображение {image_url[:80]}...")
            else:
                logger.warning("Global Research: изображение не найдено")

            # === ПОИСК КОНТЕНТА ===
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

            if len(content) < 150:
                logger.warning(f"Global Research: контент слишком короткий ({len(content)} символов)")
                feed = feedparser.parse('https://www.globalresearch.ca/feed')
                for entry in feed.entries[:10]:
                    if entry.link == url:
                        summary = entry.get('summary', '')
                        summary = re.sub(r'<[^>]+>', '', summary)
                        summary = clean_globalresearch_content(summary)
                        if summary:
                            return {
                                'title': title,
                                'content': summary[:500],
                                'image': image_url,
                                'source': 'Global Research',
                                'url': url
                            }
                return None

            return {'title': title, 'content': content, 'image': image_url, 'source': 'Global Research', 'url': url}
        except Exception as e:
            logger.error(f"Ошибка парсинга Global Research: {e}")
            return None

    # ========== СБОР НОВОСТЕЙ ==========
    async def fetch_news(self) -> list:
        items = []

        logger.info("📰 Парсинг InfoBrics...")
        ib_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_infobrics_articles)
        for article in ib_articles[:3]:
            if self._is_duplicate(article['url'], article['title']):
                continue
            data = await asyncio.get_event_loop().run_in_executor(None, self._parse_infobrics_article, article['url'])
            if data and not self._is_duplicate(article['url'], article['title'], data['content']):
                items.append(data)
                logger.info(f"✅ InfoBrics: {data['title'][:50]}...")

        logger.info("📰 Парсинг Global Research...")
        gr_articles = await asyncio.get_event_loop().run_in_executor(None, self._get_globalresearch_articles)
        for article in gr_articles[:3]:
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

            logger.info(f"📝 Перевод: {title_en[:50]}...")

            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self._translate, title_en)
            content_ru = await loop.run_in_executor(None, self._translate, content_en)

            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
            content_ru = clean_globalresearch_content(content_ru)

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
