*** Begin Patch
*** Update File: bots/news_bot.py
@@
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
-            
-            title_ru = await loop.run_in_executor(None, translate_with_fallback, title_en)
-            content_ru = await loop.run_in_executor(None, translate_with_fallback, content_en)
-
-            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
-            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
-            content_ru = clean_globalresearch_content(content_ru)
-            content_ru = clean_presstv_content(content_ru)
+            # Небольшая предобработка: удаляем явные маркеры источника, чтобы они не переводились некорректно
+            content_pre = re.sub(r"Source:\s*\S+", "", content_en, flags=re.IGNORECASE)
+            content_pre = re.sub(r"By\s+[A-Za-z0-9\-_,. ]{1,100}", "", content_pre, flags=re.IGNORECASE)
+            content_pre = re.sub(r"Источник:\s*\S+", "", content_pre, flags=re.IGNORECASE)
+
+            # Переводим заголовок и контент (translate_with_fallback уже защищает URL и делает чанкинг)
+            title_ru = await loop.run_in_executor(None, translate_with_fallback, title_en)
+            content_ru = await loop.run_in_executor(None, translate_with_fallback, content_pre)
+
+            # После перевода убираем служебные блоки и лишние строки
+            content_ru = re.sub(r'Источник:\s*\S+', '', content_ru, flags=re.IGNORECASE)
+            content_ru = re.sub(r'По материалам\s*\S+', '', content_ru, flags=re.IGNORECASE)
+            content_ru = clean_globalresearch_content(content_ru)
+            content_ru = clean_presstv_content(content_ru)
@@
-            title_escaped = html.escape(title_ru)
-            content_truncated = self._truncate_text(content_ru, is_caption=True)
-
-            message = f"*{title_escaped}*\n\n{content_truncated}"
-
-            if image_url:
-                logger.info(f"🖼️ Загрузка изображения: {image_url[:80]}...")
-                img_response = fetch_url(image_url, timeout=15)
-
-                if img_response and img_response.status_code == 200:
-                    content_type = img_response.headers.get('Content-Type', '')
-                    if 'image' in content_type:
-                        if len(message) <= 1024:
-                            try:
-                                await self.bot.send_photo(
-                                    chat_id=CHANNEL_ID,
-                                    photo=img_response.content,
-                                    caption=message,
-                                    parse_mode='Markdown'
-                                )
-                                logger.info(f"✅ Опубликовано С ФОТО (заголовок: {title_ru[:50]}...)")
-                                self._mark_sent(url, title_en, content_en)
-                                self._log_post(url, title_en)
-                                self.total_published += 1
-                                
-                                self.queue_count = len(self.state['posts_log'])
-                                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
-                                return
-                            except TelegramError as e:
-                                logger.warning(f"Ошибка отправки фото: {e}")
-                        else:
-                            shorter_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru, is_caption=True)}"
-                            if len(shorter_message) > 1024:
-                                shorter_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru[:500], is_caption=True)}"
-                            try:
-                                await self.bot.send_photo(
-                                    chat_id=CHANNEL_ID,
-                                    photo=img_response.content,
-                                    caption=shorter_message,
-                                    parse_mode='Markdown'
-                                )
-                                logger.info(f"✅ Опубликовано С ФОТО (обрезанный текст, заголовок: {title_ru[:50]}...)")
-                                self._mark_sent(url, title_en, content_en)
-                                self._log_post(url, title_en)
-                                self.total_published += 1
-                                
-                                self.queue_count = len(self.state['posts_log'])
-                                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
-                                return
-                            except TelegramError as e:
-                                logger.warning(f"Ошибка отправки фото (обрезанный текст): {e}")
-                    else:
-                        logger.warning(f"URL не ведёт на изображение: {content_type}")
-                else:
-                    logger.warning("Не удалось загрузить изображение")
-
-            logger.info(f"📝 Публикация текстом (без фото, заголовок: {title_ru[:50]}...)")
-            text_message = f"*{title_escaped}*\n\n{self._truncate_text(content_ru, is_caption=False)}"
-            await self.bot.send_message(
-                chat_id=CHANNEL_ID,
-                text=text_message,
-                parse_mode='Markdown',
-                disable_web_page_preview=False
-            )
-            logger.info("✅ Опубликовано ТЕКСТОМ")
-
-            self._mark_sent(url, title_en, content_en)
-            self._log_post(url, title_en)
-            self.total_published += 1
-            
-            self.queue_count = len(self.state['posts_log'])
-            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
+            # Формируем HTML-сообщение и экранируем специальные символы
+            title_html = html.escape(title_ru)
+            content_caption = self._truncate_text(content_ru, is_caption=True)
+            content_html = html.escape(content_caption)
+            message_html = f"<b>{title_html}</b>\n\n{content_html}"
+
+            # Отправка с учётом лимитов Telegram: caption для фото — 1024 символа
+            if image_url:
+                logger.info(f"🖼️ Загрузка изображения: {image_url[:80]}...")
+                img_response = fetch_url(image_url, timeout=15)
+
+                if img_response and img_response.status_code == 200:
+                    content_type = img_response.headers.get('Content-Type', '')
+                    if 'image' in content_type:
+                        try:
+                            if len(message_html) <= 1024:
+                                await self.bot.send_photo(
+                                    chat_id=CHANNEL_ID,
+                                    photo=img_response.content,
+                                    caption=message_html,
+                                    parse_mode='HTML'
+                                )
+                                logger.info(f"✅ Опубликовано С ФОТО (заголовок: {title_ru[:50]}...)")
+                            else:
+                                # Слишком длинная подпись — отправляем фото без подписи и отдельно текст
+                                await self.bot.send_photo(
+                                    chat_id=CHANNEL_ID,
+                                    photo=img_response.content
+                                )
+                                await self.bot.send_message(
+                                    chat_id=CHANNEL_ID,
+                                    text=message_html,
+                                    parse_mode='HTML',
+                                    disable_web_page_preview=False
+                                )
+                                logger.info(f"✅ Опубликовано: фото + длинный текст отправлен отдельно (заголовок: {title_ru[:50]}...)")
+
+                            self._mark_sent(url, title_en, content_en)
+                            self._log_post(url, title_en)
+                            self.total_published += 1
+                            self.queue_count = len(self.state['posts_log'])
+                            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
+                            return
+                        except TelegramError as e:
+                            logger.warning(f"Ошибка отправки фото: {e}")
+                    else:
+                        logger.warning(f"URL не ведёт на изображение: {content_type}")
+                else:
+                    logger.warning("Не удалось загрузить изображение")
+
+            # Если нет изображения или отправка фото не удалась — отправляем текст
+            logger.info(f"📝 Публикация текстом (без фото, заголовок: {title_ru[:50]}...)")
+            text_message_html = f"<b>{title_html}</b>\n\n" + html.escape(self._truncate_text(content_ru, is_caption=False))
+            try:
+                await self.bot.send_message(
+                    chat_id=CHANNEL_ID,
+                    text=text_message_html,
+                    parse_mode='HTML',
+                    disable_web_page_preview=False
+                )
+                logger.info("✅ Опубликовано ТЕКСТОМ")
+
+                self._mark_sent(url, title_en, content_en)
+                self._log_post(url, title_en)
+                self.total_published += 1
+                self.queue_count = len(self.state['posts_log'])
+                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
+            except TelegramError as e:
+                error_msg = str(e)
+                if "Can't parse entities" in error_msg or "Bad Request: can't parse entities" in error_msg:
+                    logger.warning("Ошибка парсинга HTML, отправляем без форматирования")
+                    try:
+                        await self.bot.send_message(
+                            chat_id=CHANNEL_ID,
+                            text=f"{title_ru}\n\n{content_ru}",
+                            parse_mode=None
+                        )
+                        self._mark_sent(url, title_en, content_en)
+                        self._log_post(url, title_en)
+                        self.total_published += 1
+                    except Exception as e2:
+                        logger.error(f"❌ Ошибка при отправке без форматирования: {e2}")
+                else:
+                    logger.error(f"❌ Ошибка Telegram: {e}")
*** End Patch
