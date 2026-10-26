# -*- coding: utf-8 -*-
"""语音对话控制器：一个热键串起「打断 → 录音 → 断句 → 识别 → 发送」。

**为什么单独成一个文件而不是塞进 ``app.py``**：``app.py`` 已经三千多行，且这个
状态机有自己的时序（录音/识别跨线程、连按语义、打断优先级），独立出来才能单独
测——不建 Qt 窗口、不建麦克风也能验证它每一步走了哪条分支。

一次按键的三条分支（这就是全部交互）：

===============  ==========================================
当时状态          按下去发生什么
===============  ==========================================
正在朗读          先掐掉声音（免得麦克风录到她自己），开始录音
正在录音          立刻收尾并识别（方案 B：说完想马上发）
空闲              开始录音（方案 A：静音 1 秒自动收尾）
===============  ==========================================

识别中（``_busy``）按键**忽略**并提示——此时麦克风已交还，再开一次会和上一句
的识别抢 CPU，且用户其实是手抖而不是想说话。

**发送落点由宿主决定**（``host`` 的三个方法），控制器不认识聊天窗口。这样它能被
单测替身完整驱动，也避免把"当前哪个界面可见"的判断埋进时序代码里。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

from .asr.recorder import SpeechRecorder
from .asr.sensevoice import SenseVoice

logger = logging.getLogger(__name__)

#: 静音多久自动收尾的默认值（秒）。产品要求 1 秒。
DEFAULT_SILENCE_SECONDS = 1.0

#: 识别结果为空时的提示（用户确实说话了，但没识别出字）。
EMPTY_RESULT_HINT = "没听清，再说一次？"


class VoiceTalkController(QObject):
    """语音对话的时序中枢。

    ``host`` 需要提供（缺哪个就跳过哪个，不会因此崩）：

    * ``voice_talk_interrupt()`` —— 立刻掐断正在播放的朗读/报时；
    * ``voice_talk_deliver(text)`` —— 把识别出的文字送进当前对话界面并发送；
    * ``voice_talk_notice(text)`` —— 一句状态提示（气泡/日志）。

    ``status_changed(str)`` 是给界面的状态广播（``"listening"`` / ``"thinking"``
    / ``""``），气泡想显示"我在听"时接它即可。
    """

    status_changed = Signal(str)

    #: 识别线程 → 主线程的内部通道。**必须走信号，不能用 ``QTimer.singleShot``**：
    #: 后者会创建一个属于**调用线程**的定时器，而 daemon 工作线程没有事件循环，
    #: 那个定时器永远不会到点——识别结果静默消失（本模块第一版就是这么写坏的，
    #: 冒烟测试里"投递结果为空"抓到的）。信号在不同线程间 emit 时按 AutoConnection
    #: 自动排进接收者线程的事件循环，这才是跨线程回主线程的正确方式。
    _recognition_done = Signal(int, str)

    def __init__(self, host, config=None, parent=None) -> None:
        super().__init__(parent)
        self._host = host
        self._config = config
        self._recognizer = SenseVoice()
        self._recorder = SpeechRecorder(self)
        self._recorder.finished.connect(self._on_audio)
        self._recorder.failed.connect(self._on_failed)
        self._recognition_done.connect(self._on_recognition_done)
        self._busy = False
        self._closed = False
        #: 识别线程的代号：每次新识别 +1，旧线程回来时据此作废（用户连按 / 退出）。
        self._generation = 0

    # ---------------------------------------------------------------- 状态
    @property
    def recording(self) -> bool:
        return self._recorder.recording

    @property
    def busy(self) -> bool:
        """是否正在识别（此时按键被忽略）。"""
        return self._busy

    # ---------------------------------------------------------------- 热键入口
    def toggle(self) -> None:
        """热键按下的唯一入口。"""
        if self._closed:
            return
        if self._recorder.recording:
            # 方案 B：录音中再按一次 = 立刻收尾。这里**不**先打断朗读——
            # 录音时本来就没在念。
            self._recorder.stop()
            return
        if self._busy:
            self._notice("还在识别上一句，稍等一下")
            return
        self._interrupt()
        silence = self._silence_seconds()
        self._recorder.set_silence_seconds(silence)
        ok, reason = self._recorder.start()
        if not ok:
            self._notice(reason)
            self.status_changed.emit("")
            return
        self.status_changed.emit("listening")

    def shutdown(self) -> None:
        """退出收口：停止录音、作废在飞的识别结果。"""
        self._closed = True
        self._generation += 1
        try:
            self._recorder.stop()
        except Exception:
            logger.exception("退出时停止录音失败")

    # ---------------------------------------------------------------- 录音结果
    def _on_audio(self, samples) -> None:
        self.status_changed.emit("")
        if self._closed:
            return
        if not samples:
            # 没检测到说话（按了键没开口，或麦克风收不到）：不报警、不重试，
            # 一句话就够——重试的主动权在用户手上。
            self._notice("没听到你说话")
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self.status_changed.emit("thinking")
        threading.Thread(
            target=self._recognize_worker, args=(list(samples), generation),
            name="pet-asr", daemon=True,
        ).start()

    def _recognize_worker(self, samples, generation: int) -> None:
        """子线程：识别 → 用信号回主线程。

        桌宠是单进程，识别占 0.2~1.8 秒；放主线程会把动画冻住那么久。信号跨线程
        emit 时自动排队到接收者（控制器，主线程）的事件循环，所以主线程侧不用加锁。
        """
        text = ""
        try:
            text = self._recognizer.recognize(samples)
        except Exception:
            logger.exception("语音识别线程异常")
        self._recognition_done.emit(generation, text)

    def _on_recognition_done(self, generation: int, text: str) -> None:
        """主线程侧收口：作废判定 + 投递。"""
        if self._closed or generation != self._generation:
            return  # 已被新一次录音 / 退出作废
        self._finish_recognize(text)

    def _finish_recognize(self, text: str) -> None:
        self._busy = False
        self.status_changed.emit("")
        if self._closed:
            return
        cleaned = (text or "").strip()
        if not cleaned:
            self._notice(EMPTY_RESULT_HINT)
            return
        deliver = getattr(self._host, "voice_talk_deliver", None)
        if not callable(deliver):
            logger.warning("宿主没有 voice_talk_deliver，识别结果丢弃：%s", cleaned[:20])
            return
        try:
            deliver(cleaned)
        except Exception:
            logger.exception("把识别结果送进对话失败")

    def _on_failed(self, reason: str) -> None:
        self.status_changed.emit("")
        if not self._closed:
            self._notice(reason)

    # ---------------------------------------------------------------- 内部
    def _interrupt(self) -> None:
        """掐掉正在播放的声音。

        必须做在 ``start()`` 之前：她朗读时麦克风就在旁边，不掐掉就会把她自己的
        声音录进去，识别出来的是一句她自己说的话。
        """
        stop = getattr(self._host, "voice_talk_interrupt", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                logger.exception("打断朗读失败（继续录音）")

    def _notice(self, text: str) -> None:
        show = getattr(self._host, "voice_talk_notice", None)
        if callable(show):
            try:
                show(text)
            except Exception:
                logger.exception("显示语音提示失败：%s", text)
        else:
            logger.info("语音对话提示：%s", text)

    def _silence_seconds(self) -> float:
        """静音收尾秒数（每次现读配置 = 热生效，不必重建对象）。"""
        if self._config is None:
            return DEFAULT_SILENCE_SECONDS
        try:
            value = float(self._config.get("voice_talk_silence_seconds",
                                           DEFAULT_SILENCE_SECONDS))
        except Exception:
            logger.exception("读取静音秒数失败，用默认值")
            return DEFAULT_SILENCE_SECONDS
        # 范围收紧：太短会在换气处被截断，太长等于每句都要等它。
        return min(3.0, max(0.4, value))


__all__ = ["DEFAULT_SILENCE_SECONDS", "EMPTY_RESULT_HINT", "VoiceTalkController"]
