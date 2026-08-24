import re
from bots import news_bot


def test_protect_and_restore_urls():
    text = "Visit https://example.com/page and http://test.org?x=1 for info."
    prot, placeholders = news_bot._protect_urls(text)
    # Ensure URLs replaced by placeholders
    assert "__URL_0__" in prot
    assert "__URL_1__" in prot
    restored = news_bot._restore_urls(prot, placeholders)
    assert restored == text


def test_chunk_text_short():
    text = "Short sentence."
    chunks = news_bot._chunk_text(text, max_chunk=100)
    assert isinstance(chunks, list)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_split_sentences():
    # create many sentences to force splitting
    sentences = [f"Sentence {i}." for i in range(50)]
    text = " ".join(sentences)
    chunks = news_bot._chunk_text(text, max_chunk=200)
    # Each chunk should be <= 200 chars
    assert all(len(c) <= 200 for c in chunks)
    # Basic sanity check
    assert "Sentence 0" in chunks[0]


def test_truncate_to_last_sentence_basic():
    text = "First sentence. Second sentence is longer. Third one." 
    # If max_len cuts in middle of second sentence, expect it returns last full sentence that fits
    res = news_bot.NewsBot()._truncate_to_last_sentence(text, max_len=20)
    assert res.endswith('.')
    assert len(res) <= 20


def test_caption_truncation_fits_allowed():
    bot = news_bot.NewsBot()
    title = "Title Example"
    # content with multiple sentences
    content = "This is sentence one. This is sentence two which is somewhat long. Final short sentence."
    allowed = 50
    truncated = bot._truncate_to_last_sentence(content, allowed)
    assert len(truncated) <= allowed
    # truncated should end with sentence punctuation if non-empty
    if truncated:
        assert truncated[-1] in '.!?'
