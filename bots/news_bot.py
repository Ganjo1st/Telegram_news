*** Begin Patch
*** Update File: bots/news_bot.py
@@
-                        try:
-                            # Всегда подгоняем текст для подписи под лимит 1024 символа.
-                            # Считаем длину заголовка в plain text и даём оставшееся место для контента.
-                            title_plain_len = len(title_ru or "")
-                            allowed_for_content = 1024 - title_plain_len - 2  # учтём два перевода строки
-                            if allowed_for_content < 0:
-                                allowed_for_content = 0
-
-                            # Обрезаем контент до последнего предложения, которое вместится в allowed_for_content
-                            content_for_caption = self._truncate_to_last_sentence(content_ru, allowed_for_content)
-
-                            title_html_escaped = html.escape(title_ru)
-                            if content_for_caption:
-                                caption_html = f"<b>{title_html_escaped}</b>\n\n{html.escape(content_for_caption)}"
-                            else:
-                                caption_html = f"<b>{title_html_escaped}</b>"
-
-                            await self.bot.send_photo(
-                                chat_id=CHANNEL_ID,
-                                photo=img_response.content,
-                                caption=caption_html,
-                                parse_mode='HTML'
-                            )
-                            logger.info(f"✅ Опубликовано С ФОТО (заголовок: {title_ru[:50]}...)")
-
-                            self._mark_sent(url, title_en, content_en)
-                            self._log_post(url, title_en)
-                            self.total_published += 1
-                            self.queue_count = len(self.state['posts_log'])
-                            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
-                            return
-                        except TelegramError as e:
-                            logger.warning(f"Ошибка отправки фото: {e}")
+                        try:
+                            # Всегда подгоняем текст для подписи под лимит 1024 символа.
+                            # Считаем длину заголовка в plain text и даём оставшееся место для контента.
+                            title_plain_len = len(title_ru or "")
+                            allowed_for_content = 1024 - title_plain_len - 2  # учтём два перевода строки
+                            if allowed_for_content < 0:
+                                allowed_for_content = 0
+
+                            # Обрезаем контент до последнего предложения, которое вместится в allowed_for_content
+                            content_for_caption = self._truncate_to_last_sentence(content_ru, allowed_for_content)
+
+                            title_html_escaped = html.escape(title_ru)
+                            if content_for_caption:
+                                caption_html = f"<b>{title_html_escaped}</b>\n\n{html.escape(content_for_caption)}"
+                            else:
+                                caption_html = f"<b>{title_html_escaped}</b>"
+
+                            await self.bot.send_photo(
+                                chat_id=CHANNEL_ID,
+                                photo=img_response.content,
+                                caption=caption_html,
+                                parse_mode='HTML'
+                            )
+                            logger.info(f"✅ Опубликовано С ФОТО (заголовок: {title_ru[:50]}...)")
+
+                            self._mark_sent(url, title_en, content_en)
+                            self._log_post(url, title_en)
+                            self.total_published += 1
+                            self.queue_count = len(self.state['posts_log'])
+                            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
+                            return
+                        except TelegramError as e:
+                            # Если отправка фото не удалась по причине Telegram API — пропускаем статью,
+                            # потому что требования: посты обязательно должны содержать картинку, заголовок и текст в одном посте.
+                            logger.error(f"❌ Ошибка отправки фото, статья пропущена (требуется изображение): {e}")
+                            self.total_excluded += 1
+                            return
@@
-            # Если нет изображения или отправка фото не удалась — отправляем текст
-            logger.info(f"📝 Публикация текстом (без фото, заголовок: {title_ru[:50]}...)")
-            text_message_html = f"<b>{title_html}</b>\n\n" + html.escape(self._truncate_text(content_ru, is_caption=False))
-            try:
-                await self.bot.send_message(
-                    chat_id=CHANNEL_ID,
-                    text=text_message_html,
-                    parse_mode='HTML',
-                    disable_web_page_preview=False
-                )
-                logger.info("✅ Опубликовано ТЕКСТОМ")
-
-                self._mark_sent(url, title_en, content_en)
-                self._log_post(url, title_en)
-                self.total_published += 1
-                self.queue_count = len(self.state['posts_log'])
-                logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
-            except TelegramError as e:
-                error_msg = str(e)
-                if "Can't parse entities" in error_msg or "Bad Request: can't parse entities" in error_msg:
-                    logger.warning("Ошибка парсинга HTML, отправляем без форматирования")
-                    try:
-                        await self.bot.send_message(
-                            chat_id=CHANNEL_ID,
-                            text=f"{title_ru}\n\n{content_ru}",
-                            parse_mode=None
-                        )
-                        self._mark_sent(url, title_en, content_en)
-                        self._log_post(url, title_en)
-                        self.total_published += 1
-                    except Exception as e2:
-                        logger.error(f"❌ Ошибка при отправке без форматирования: {e2}")
-                else:
-                    logger.error(f"❌ Ошибка Telegram: {e}")
+            # Если нет изображения или загрузка/проверка изображения не удалась — пропускаем публикацию,
+            # потому что требование: посты обязательно должны содержать картинку, заголовок и текст в одном посте.
+            logger.info("⛔ Статья пропущена: отсутствует доступное изображение для публикации (обязательное поле)")
+            self.total_excluded += 1
+            return
*** End Patch
