*** Begin Patch
*** Update File: bots/news_bot.py
@@
-                        try:
-                            if len(message_html) <= 1024:
-                                await self.bot.send_photo(
-                                    chat_id=CHANNEL_ID,
-                                    photo=img_response.content,
-                                    caption=message_html,
-                                    parse_mode='HTML'
-                                )
-                                logger.info(f"✅ Опубликовано С ФОТО (заголовок: {title_ru[:50]}...)")
-                            else:
-                                # Слишком длинная подпись — отправляем фото без подписи и отдельно текст
-                                await self.bot.send_photo(
-                                    chat_id=CHANNEL_ID,
-                                    photo=img_response.content
-                                )
-                                await self.bot.send_message(
-                                    chat_id=CHANNEL_ID,
-                                    text=message_html,
-                                    parse_mode='HTML',
-                                    disable_web_page_preview=False
-                                )
-                                logger.info(f"✅ Опубликовано: фото + длинный текст отправлен отдельно (заголовок: {title_ru[:50]}...)")
-
-                            self._mark_sent(url, title_en, content_en)
-                            self._log_post(url, title_en)
-                            self.total_published += 1
-                            self.queue_count = len(self.state['posts_log'])
-                            logger.info(f"📊 В очереди (неопубликовано): {self.queue_count} статей")
-                            return
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
*** End Patch
