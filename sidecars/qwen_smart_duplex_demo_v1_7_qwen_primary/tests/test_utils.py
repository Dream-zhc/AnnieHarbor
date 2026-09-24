from utils import PhraseChunker, meaningful_text


def test_hard_punctuation_cut():
    c = PhraseChunker(12, 20)
    assert c.feed("你好呀。后面") == ["你好呀。"]
    assert c.flush() == ["后面"]


def test_first_chunk_threshold():
    c = PhraseChunker(6, 10)
    assert c.feed("这是没有标点的长句") == ["这是没有标点"]


def test_meaningful_text():
    assert not meaningful_text("嗯……")
    assert meaningful_text("等一下")
