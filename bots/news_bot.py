---
*** Begin Patch
*** Update File: bots/news_bot.py
@@
-from deep_translator import GoogleTranslator, MyMemoryTranslator, PonsTranslator
+from deep_translator import GoogleTranslator, MyMemoryTranslator, PonsTranslator
+import time
@@
-def translate_with_fallback(text: str, source: str = 'en', target: str = 'ru') -> str:
-    """Переводит текст с использованием нескольких переводчиков (запасные варианты)"""
-    if not text or len(text) < 10:
-        return text
-    
-    translators = [
-        ('Google', lambda: GoogleTranslator(source=source, target=target).translate(text)),
-        ('MyMemory', lambda: MyMemoryTranslator(source=source, target=target).translate(text)),
-        ('Pons', lambda: PonsTranslator(source=source, target=target).translate(text)),
-    ]
-    
-    for name, translate_func in translators:
-        try:
-            result = translate_func()
-            if result and result != text:
-                logger.info(f"✅ Перевод выполнен ({name}): '{result[:50]}...'")
-                return result
-        except Exception as e:
-            logger.warning(f"⚠️ Ошибка {name} переводчика: {e}")
-            continue
-    
-    logger.warning(f"⚠️ Все переводчики не смогли перевести текст: '{text[:50]}...'")
-    return text
+def _protect_urls(text: str):
+    urls = re.findall(r'https?://\S+', text)
+    placeholders = {}
+    for i, u in enumerate(urls):
+        ph = f"__URL_{i}__"
+        placeholders[ph] = u
+        text = text.replace(u, ph)
+    return text, placeholders
+
+
+def _restore_urls(text: str, placeholders: dict):
+    for ph, u in placeholders.items():
+        text = text.replace(ph, u)
+    return text
+
+
+def _chunk_text(text: str, max_chunk: int = 1000):
+    if len(text) <= max_chunk:
+        return [text]
+    chunks = []
+    start = 0
+    while start < len(text):
+        end = min(start + max_chunk, len(text))
+        last = text.rfind('.', start, end)
+        if last <= start:
+            last = end
+        else:
+            last += 1
+        chunks.append(text[start:last].strip())
+        start = last
+    return chunks
+
+
+def translate_with_fallback(text: str, source: str = 'en', target: str = 'ru') -> str:
+    """Безопасный перевод с защитой URL, чанкингом, fallback'ами и паузами"""
+    if not text or len(text) < 5:
+        return text
+
+    text_prot, placeholders = _protect_urls(text)
+    chunks = _chunk_text(text_prot, max_chunk=1000)
+
+    translators = [
+        ('Google', lambda t: GoogleTranslator(source=source, target=target).translate(t)),
+        ('MyMemory', lambda t: MyMemoryTranslator(source=source, target=target).translate(t)),
+        ('Pons', lambda t: PonsTranslator(source=source, target=target).translate(t)),
+    ]
+
+    translated_parts = []
+    for chunk in chunks:
+        translated = None
+        for name, func in translators:
+            try:
+                translated = func(chunk)
+                if translated and translated != chunk:
+                    time.sleep(0.4)
+                    break
+            except Exception as e:
+                logger.warning(f"⚠️ Ошибка {name} переводчика для чанка: {e}")
+                translated = None
+                continue
+        if not translated:
+            translated = chunk
+        translated_parts.append(translated)
+
+    result = ''.join(translated_parts)
+    result = _restore_urls(result, placeholders)
+    return result
*** End Patch
