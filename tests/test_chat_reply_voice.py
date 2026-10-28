"""助手回复朗读（``pet/chat_reply_voice.py``）的行为守卫。

覆盖九件事：两条纯函数（清洗 / 切句）、滴灌顺序、忙时不丢队列、新回复覆盖旧队列、
默认关门禁、启停订阅幂等、``channel_idle`` 公开面、以及"歌词链不许接朗读"。

全部用注入的假通道与假时钟，不建真 ``VoiceChimeService``、不跑 Qt 事件循环：
朗读的价值在"什么时候送下一句"，那部分应当能脱离音频栈被验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pet.chat_reply_voice import (
    MAX_SENTENCE_CHARS,
    ChatReplyReader,
    clean_for_speech,
    pack_sentences,
    split_sentences,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeChannel:
    """假音频通道：``idle`` 手动翻，``heard`` 记录真正被念出来的句子。"""

    def __init__(self, *, idle: bool = True) -> None:
        self.idle = idle
        self.heard: list[str] = []

    def speak(self, sentence: str) -> bool:
        assert self.idle, "通道忙的时候不该被塞新句子（宿主必须先问 channel_idle）"
        self.heard.append(sentence)
        self.idle = False  # 起播即占用，直到测试手动放行
        return True

    def channel_idle(self) -> bool:
        return self.idle


class FakeClock:
    """假调度器：把 ``schedule(ms, fn)`` 存起来，由测试手动 ``tick()``。"""

    def __init__(self) -> None:
        self.pending: list = []

    def schedule(self, ms: int, fn) -> None:
        self.pending.append((ms, fn))

    def tick(self) -> None:
        due, self.pending = self.pending, []
        for _, fn in due:
            fn()


def _reader(channel: FakeChannel, clock: FakeClock, *, enabled: bool = True,
            max_chars: int = MAX_SENTENCE_CHARS) -> ChatReplyReader:
    return ChatReplyReader(
        speak=channel.speak,
        channel_idle=channel.channel_idle,
        enabled=lambda: enabled,
        schedule=clock.schedule,
        max_chars=max_chars,
    )


# --------------------------------------------------------------- 纯函数
def test_clean_for_speech_strips_markdown_keeps_words():
    text = (
        "## 标题\n"
        "看这个[链接](https://example.com)，**重点**在这。\n"
        "```python\nprint('hi')\n```\n"
        "- 列表项一\n"
    )
    cleaned = clean_for_speech(text)
    assert "代码略" in cleaned
    assert "print" not in cleaned, "代码块内容不该被念出来"
    assert "https://example.com" not in cleaned, "链接目标不该被念出来"
    assert "链接" in cleaned and "重点" in cleaned, "链接文字与正文要保留"
    assert "#" not in cleaned and "**" not in cleaned and "- 列表项" not in cleaned
    assert "标题" in cleaned and "列表项一" in cleaned


def test_split_sentences_splits_on_sentence_end():
    sentences = split_sentences("第一句。第二句！第三句？")
    assert sentences == ["第一句。", "第二句！", "第三句？"]


def test_split_sentences_hard_splits_overlong_piece():
    long_piece = "啊" * 300
    sentences = split_sentences(long_piece)
    assert sentences, "超长文本不能被整体丢掉"
    assert all(len(s) <= MAX_SENTENCE_CHARS for s in sentences)
    assert sum(len(s) for s in sentences) == 300, "硬切不许丢字"


# --------------------------------------------------------------- 滴灌
# 这几例验证的是"一句一句送、不一次塞满、忙时不丢"，与合并（pack）无关：
# 上限压到 6 字，保证 4 字的短句不会被合并成一段，队列仍是多条。
def test_sends_one_sentence_at_a_time():
    channel = FakeChannel(idle=True)
    clock = FakeClock()
    reader = _reader(channel, clock, max_chars=6)
    reader.feed("第一句。第二句。第三句。")
    assert channel.heard == ["第一句。"], "通道空闲时只该送第一句"
    assert reader.pending == 2, "剩下的必须留在队列里，不能一次全塞进去"
    channel.idle = True
    clock.tick()
    assert channel.heard == ["第一句。", "第二句。"], "通道空出来才送下一句"
    channel.idle = True
    clock.tick()
    assert channel.heard == ["第一句。", "第二句。", "第三句。"]
    assert reader.pending == 0


def test_busy_channel_keeps_queue_until_idle():
    channel = FakeChannel(idle=False)
    clock = FakeClock()
    reader = _reader(channel, clock, max_chars=6)
    reader.feed("第一句。第二句。")
    assert channel.heard == [], "通道忙时不许塞句子"
    assert reader.pending == 2, "忙的时候必须把句子留在队列里（不是丢掉）"
    assert clock.pending, "忙的时候要排一次重试"
    channel.idle = True
    clock.tick()
    assert channel.heard == ["第一句。"]


def test_new_reply_replaces_old_queue():
    channel = FakeChannel(idle=False)
    clock = FakeClock()
    reader = _reader(channel, clock, max_chars=6)
    reader.feed("旧的甲。旧的乙。")
    assert reader.pending == 2
    reader.feed("新的这一句。")
    assert reader.pending == 1, "新回复覆盖旧队列（与通道「新覆盖旧」同口径）"
    channel.idle = True
    clock.tick()
    assert channel.heard == ["新的这一句。"], "过期的那两句一句都不该再念"


# --------------------------------------------------------------- 开关与订阅
def test_disabled_reader_speaks_nothing():
    channel = FakeChannel(idle=True)
    clock = FakeClock()
    reader = _reader(channel, clock, enabled=False)
    reader.feed("这句不该被念。")
    assert channel.heard == []
    assert reader.pending == 0
    assert not clock.pending


def test_start_stop_are_idempotent():
    from pet.chat.service import ChatService

    channel = FakeChannel(idle=True)
    clock = FakeClock()
    reader = _reader(channel, clock)
    before = len(ChatService._global_finished_listeners)

    assert reader.start() is True
    assert reader.start() is False, "重复 start 不该重复订阅"
    assert len(ChatService._global_finished_listeners) == before + 1
    assert reader.subscribed

    reader.stop()
    assert not reader.subscribed
    assert len(ChatService._global_finished_listeners) == before
    reader.stop()  # 再停一次不许抛


def test_channel_idle_is_public_and_truthful():
    from pet.voice.chime_service import VoiceChimeService

    assert isinstance(VoiceChimeService.channel_idle, property), (
        "朗读要靠公开的 channel_idle 判据，而不是去摸私有 _channel_idle"
    )


def test_lyric_chain_never_wires_reply_voice():
    for name in ("lyric_controller.py", "lyric.py", "now_playing.py"):
        source = (REPO_ROOT / "pet" / "music" / name).read_text(encoding="utf-8")
        assert "chat_reply_voice" not in source, f"{name} 不该接助手回复朗读（只念对话，不念歌词）"


# --------------------------------------------------------------- 相邻短句合并（2026-10-28）
def test_pack_sentences_merges_short_neighbours():
    """相邻短句合并成一段：这是消除句间停顿的关键（一段音频 = 无拼接点）。"""
    assert pack_sentences(["甲。", "乙。", "丙。"]) == ["甲。乙。丙。"]


def test_pack_sentences_respects_char_limit():
    """贪心合并但绝不超上限：40 字 + 40 字恰好 80 可合；再多一句就另起一段。"""
    a, b, c = "甲" * 39 + "。", "乙" * 39 + "。", "丙" * 39 + "。"
    packed = pack_sentences([a, b, c], max_chars=80)
    assert packed == [a + b, c]


def test_pack_sentences_keeps_order_and_loses_nothing():
    """保序、不丢字、不重复。"""
    sentences = ["第一句话。", "第二句话！", "第三句话？", "第四句话。"]
    packed = pack_sentences(sentences, max_chars=80)
    assert "".join(packed) == "".join(sentences)
    assert packed == ["第一句话。第二句话！第三句话？第四句话。"]


def test_pack_sentences_keeps_oversized_single():
    """单句已超上限（hard_split 产物）：原样保留，不与邻居合并。"""
    long_piece = "啊" * 90
    assert pack_sentences([long_piece, "短句。"], max_chars=80) == [long_piece, "短句。"]


def test_pack_sentences_adds_space_between_ascii_words():
    """英文句子合并补空格；中文直接相接。"""
    assert pack_sentences(["Hello.", "World."]) == ["Hello. World."]
    assert pack_sentences(["你好。", "世界。"]) == ["你好。世界。"]


def test_pack_sentences_skips_blank_pieces():
    assert pack_sentences(["甲。", "   ", "", "乙。"]) == ["甲。乙。"]
    assert pack_sentences([]) == []


def test_two_sentence_reply_is_synthesized_as_one_clip():
    """机主真实场景：两句话的短回复只送一次合成，中间不再有停顿。

    这句子原来按「？」「！」「……」被切成 3 段，逐段合成→句间各停一下；
    合并后整条一次合成、一段音频。单独成段的「……」按既有规则丢弃（无文字，
    不值得为它单跑一次合成），所以合并结果不含它——与旧行为一致。
    """
    text = "哼——现在才说？都快一整天没理人家了！……不过我也想你了哦，比你想我多一点～"
    channel = FakeChannel(idle=True)
    clock = FakeClock()
    reader = _reader(channel, clock)
    reader.feed(text)
    assert len(channel.heard) == 1, "整条回复应当只送一次合成（一段音频）"
    assert channel.heard[0] == text.replace("……", "")
    assert reader.pending == 0


def test_long_reply_still_split_into_bounded_segments():
    """长回复按上限分段：首声不会被拖到整条合完（与合并同一个上限收敛）。"""
    sentences = ["第%d句话说的是很长很长的一段内容。" % i for i in range(1, 7)]
    channel = FakeChannel(idle=True)
    clock = FakeClock()
    reader = _reader(channel, clock)
    reader.feed("".join(sentences))
    heard = list(channel.heard)
    while reader.pending:
        channel.idle = True
        clock.tick()
        heard = channel.heard
    assert len(heard) > 1, "超长回复必须分段（不能整条一次合成拖慢首声）"
    assert all(len(segment) <= MAX_SENTENCE_CHARS for segment in heard)
    assert "".join(heard) == "".join(sentences)
