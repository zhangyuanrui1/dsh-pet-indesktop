# -*- coding: utf-8 -*-
"""SenseVoice-Small 识别封装（sherpa-onnx + ONNX Runtime，纯 CPU）。

**懒加载是硬要求**：首次构造识别器要读 229 MB 模型，实测 1.03 秒。桌宠启动路径
上不能付这笔钱——只在用户第一次真正说话时才付，付完之后常驻复用（只加载一次）。

**调用方必须在子线程调 :meth:`recognize`**：一次识别 0.2~1.8 秒（取决于 CPU 与
线程数），放主线程会把桌宠动画冻住那么久。本模块只保证自身线程安全，不替你切线程。

语言开关说明：``language="zh"`` 是**提示而非硬约束**——模型本身支持中英日韩粤，
设成 zh 时中英混说照样识别。实测那段 8.95 秒录音里「11 月底」被规范成中文数字，
是 ``use_itn`` 的功劳（逆文本规范化：数字、日期、标点），不是语言的副作用。
"""

from __future__ import annotations

import logging
import os
import threading

from . import RECOGNIZER_MODEL, RECOGNIZER_TOKENS, availability, model_dir

logger = logging.getLogger(__name__)

#: 送进识别器的采样率。SenseVoice 固定 16 kHz——录音侧已经统一到这个值
#: （见 ``recorder`` 的重采样），这里再声明一次是为了让调用方不必猜。
SAMPLE_RATE = 16000


def _thread_count() -> int:
    """推理线程数：CPU 一半，封顶 4。

    实测（i7-12700，8.95 秒录音）：4 线程 0.20 秒 / 2 线程 0.70 秒 / 1 线程 1.82 秒。
    封顶 4 是因为再往上收益趋平，而桌宠是**单进程**——识别期间占满核心会让
    动画掉帧（识别在子线程，但 CPU 是共享的）。低功耗机器（4 核 → 2 线程）
    自动降档，不必手调。
    """
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))


class SenseVoice:
    """识别器持有者：懒加载 + 串行化解码。

    串行化的理由：``OfflineRecognizer`` 的 C++ 侧不是可重入的。语音对话本身是
    单入口（一次只说一句），加锁几乎不会真的阻塞，但能挡住"上一句还在识别、
    用户又按了一次"这种双击场景把 C++ 对象踩坏。
    """

    def __init__(self, directory=None) -> None:
        self._dir = directory
        self._recognizer = None
        self._lock = threading.Lock()
        self._load_error = ""

    # ---------------------------------------------------------------- 状态
    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    @property
    def load_error(self) -> str:
        """上次加载失败的原因（成功后为空串）。"""
        return self._load_error

    def availability(self) -> tuple[bool, str]:
        return availability(self._dir)

    # ---------------------------------------------------------------- 加载
    def ensure_loaded(self) -> bool:
        """加载模型（幂等）。返回是否可用。

        失败**不抛异常**：调用方是 GUI 回调，抛出去就是一条用户看不懂的栈。
        失败原因同时记日志并留在 :attr:`load_error` 里，由调用方转成一句人话。
        """
        if self._recognizer is not None:
            return True
        with self._lock:
            if self._recognizer is not None:
                return True
            ok, reason = availability(self._dir)
            if not ok:
                self._load_error = reason
                logger.warning("本地语音识别不可用：%s", reason)
                return False
            import sherpa_onnx

            target = model_dir() if self._dir is None else self._dir
            try:
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(target / RECOGNIZER_MODEL),
                    tokens=str(target / RECOGNIZER_TOKENS),
                    num_threads=_thread_count(),
                    use_itn=True,   # 逆文本规范化：数字/日期/标点
                    language="zh",
                )
            except Exception as exc:
                self._load_error = f"识别模型加载失败（{type(exc).__name__}: {exc}）"
                logger.exception("加载本地识别模型失败（目录 %s）", target)
                return False
            logger.info("本地语音识别就绪：%s（%d 线程）", target, _thread_count())
            self._load_error = ""
            return True

    # ---------------------------------------------------------------- 识别
    def recognize(self, samples, *, sample_rate: int = SAMPLE_RATE) -> str:
        """识别一段 float 波形（-1.0~1.0），返回文本（可能为空串）。

        空串是合法结果（静音/噪声），不是错误——调用方据此决定"没听清"的提示。
        """
        if not samples:
            return ""
        if not self.ensure_loaded():
            return ""
        with self._lock:
            recognizer = self._recognizer
            if recognizer is None:  # 并发下被别处清掉了
                return ""
            try:
                stream = recognizer.create_stream()
                stream.accept_waveform(int(sample_rate), samples)
                recognizer.decode_stream(stream)
                text = stream.result.text or ""
            except Exception:
                logger.exception("语音识别失败（%d 个采样点）", len(samples))
                return ""
        return normalize_text(text)


def normalize_text(text: str) -> str:
    """收掉识别结果里的多余空白。

    只做「折叠空白 + 去首尾」，**不删标点、不改字**：识别结果要原样进对话，
    改字等于替用户说话。标点保留是刻意的——它同时是提示词里的自然断句。
    """
    return " ".join(str(text or "").split())


__all__ = ["SAMPLE_RATE", "SenseVoice", "normalize_text"]
