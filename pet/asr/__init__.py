# -*- coding: utf-8 -*-
"""本地语音输入门面：录音 → 断句 → 识别，全在本机完成。

分两层，各自独立可测：

* :mod:`.recorder` —— ``QAudioSource`` 录音 + Silero VAD 断句。**不引入新依赖**：
  录音用 PySide6 自带的 QtMultimedia，VAD 用下面这个可选的 sherpa-onnx。
* :mod:`.sensevoice` —— SenseVoice-Small 识别（ONNX Runtime，CPU 推理）。

**为什么是本地而不是云端**（实测依据，非估算）：

* i7-12700 + ``model.int8.onnx``，8.95 秒录音：4 线程 0.20 秒、2 线程 0.70 秒、
  1 线程 1.82 秒（RTF 0.022 / 0.078 / 0.204）。即使按最弱的单线程算，也**快于**
  走一趟云端往返，且不受服务端高峰期限流影响。
* 与项目已有的本地 OCR（``pet/screen_capture/``）同一套技术栈：onnxruntime +
  CPU + ``resources/`` 下的模型文件。不引入新的运行时形态。
* 语音不出本机。

**依赖是可选的**：``sherpa-onnx`` 或模型文件缺失时，:func:`availability` 返回
不可用与一句人话原因，语音对话入口据此拒绝启动并提示——其余功能一切照旧，
不会因为缺一个包就让桌宠起不来（与「显示歌词」「快速识屏」同一口径）。

模型目录可用环境变量 ``DSH_PET_ASR_MODEL_DIR`` 指到别处（换机器/模型放 D 盘时
用得上，见 :func:`model_dir`）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 模型在源码树 / 冻结包里的相对目录（冻结包按 ``sys._MEIPASS`` 为根，与
#: ``pet.screen_capture.ocr_models`` 同口径）。
MODEL_RELATIVE_DIR = ("resources", "asr_models")

#: 识别模型文件名（int8 量化：229 MB 原始 → 体积与速度都更适合 CPU）。
RECOGNIZER_MODEL = "model.int8.onnx"

#: 识别器词表。
RECOGNIZER_TOKENS = "tokens.txt"

#: 断句用的 Silero VAD（629 KB；只判断「有没有人在说话」，不做识别）。
VAD_MODEL = "silero_vad.onnx"

#: 覆盖模型目录的环境变量。显式指定优先于随包路径——换机器、模型放外置盘、
#: 或想换一个更大的识别模型时用得上，不必动代码。
MODEL_DIR_ENV = "DSH_PET_ASR_MODEL_DIR"


def model_dir() -> Path:
    """模型目录：环境变量 > 随包 ``resources/asr_models``。

    与 ``ocr_models.resolve_model_dir`` 的差别：这里**不做**「回退到 wheel 自带」
    那一档——sherpa-onnx 的 wheel 不带识别模型，没有可回退的同批文件，硬编一条
    不存在的回退只会让报错指错地方。所以两个来源都没有时返回随包路径，让提示
    文案说的是用户机器上那一个目录。
    """
    override = os.environ.get(MODEL_DIR_ENV)
    if override:
        return Path(override)
    meipass = getattr(sys, "_MEIPASS", None)
    # pet/asr/__init__.py → pet/asr → pet → 仓库根
    root = Path(meipass) if meipass else Path(__file__).resolve().parents[2]
    return root.joinpath(*MODEL_RELATIVE_DIR)


def missing_files(directory: Path | None = None) -> list[str]:
    """缺哪些模型文件（空列表 = 齐了）。返回文件名而非布尔值，提示能指名道姓。"""
    target = Path(directory) if directory is not None else model_dir()
    need = (RECOGNIZER_MODEL, RECOGNIZER_TOKENS, VAD_MODEL)
    return [name for name in need if not (target / name).is_file()]


def availability(directory: Path | None = None) -> tuple[bool, str]:
    """能否做本地语音输入。返回 ``(可用, 不可用时的原因)``。

    原因文案是**给用户看的**（会进气泡/日志），所以要说"缺什么、放哪里"，
    不是抛一个 ImportError 的栈。
    """
    try:
        import sherpa_onnx  # noqa: F401
    except Exception as exc:  # pragma: no cover - 取决于环境
        return False, f"缺少语音识别组件 sherpa-onnx（{type(exc).__name__}）"
    target = Path(directory) if directory is not None else model_dir()
    gaps = missing_files(target)
    if gaps:
        # 提示里带上两个入口：设置页的下载按钮（对人不要求命令行）和脚本（对
        # 服务器/远程场景）。模型 229 MB 不入库，干净克隆后这里一定为空。
        return False, ("语音识别模型未就绪（缺 " + "、".join(gaps)
                       + "）：到 设置 → 语音 → 语音输入 点「下载模型」，"
                       f"或运行 python scripts/fetch_asr_models.py（模型目录 {target}）")
    return True, ""


__all__ = [
    "MODEL_DIR_ENV",
    "MODEL_RELATIVE_DIR",
    "RECOGNIZER_MODEL",
    "RECOGNIZER_TOKENS",
    "VAD_MODEL",
    "availability",
    "missing_files",
    "model_dir",
]
