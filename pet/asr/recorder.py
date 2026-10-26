# -*- coding: utf-8 -*-
"""录音 + 断句：``QAudioSource`` 采音，Silero VAD 判「说完了没有」。

**零新增录音依赖**：QtMultimedia 随 PySide6 一起来，``QAudioSource`` 直接可用。

两个收尾方式（对应产品上的方案 A / B）：

* **A. 静音自动收尾** —— 检测到说过话之后，连续静音达到 ``silence_seconds``
  （默认 1 秒）就结束。用户不必记得松手。
* **B. 手动收尾** —— 录音中再按一次热键，立刻结束（:meth:`stop`）。想提前
  说完、或环境太吵自动收尾不灵时用它。

**为什么静音计时要自己算，不能直接用 VAD 的翻转**：实测（8.95 秒录音，尾部接
2.5 秒静音）``is_speech_detected()`` 在语音真实结束**之后约 0.2 秒**才转 False
——它反映的是"最近一窗还有没有语音"，不是"静音多久了"。直接等它翻转，用户感受
到的停顿会是「1 秒 + VAD 滞后」而不是 1 秒。所以这里记"最后一帧语音的位置"，
按**采样点距离**算静音时长，并把 VAD 的固定滞后补偿掉（``VAD_LAG_SECONDS``），
让 1 秒就是 1 秒。

**格式自适应**：不同机器的麦克风阵列常见 48 kHz / 立体声。请求 16 kHz 单声道
不一定被接受——Qt 在不支持时会退回设备优先格式，而它**不会**替我们改采样率。
所以 :meth:`_decode` 按 ``source.format()`` 的真实格式解码，并自带音量归一、
声道降混与线性重采样。这条在笔记本上尤其重要（内置阵列几乎都是 48 kHz）。
"""

from __future__ import annotations

import array
import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import VAD_MODEL, availability, model_dir
from .sensevoice import SAMPLE_RATE

logger = logging.getLogger(__name__)

#: Silero VAD 的固定窗口（16 kHz 下 = 32 ms）。模型按这个长度训练，必须对齐。
VAD_WINDOW = 512

#: VAD 判定滞后补偿（秒）。实测值 0.21 s，取 0.2 作为保守估计。
#: 含义：VAD 说"没语音"时，真实语音其实在 0.2 秒前就结束了。
VAD_LAG_SECONDS = 0.2

#: 开头保留的余量：从第一帧语音往前多留 0.2 秒，避免吃掉第一个字的声母。
HEAD_MARGIN_SECONDS = 0.2

#: 结尾保留的余量：最后一帧语音之后再留 0.3 秒，避免截断尾音。
TAIL_MARGIN_SECONDS = 0.3

#: 按下后一直没说话，最多等这么久就放弃（秒）。
MAX_WAIT_SECONDS = 8.0

#: 单次录音硬上限（秒）。防用户误触后忘记、一直占着麦克风。
MAX_RECORD_SECONDS = 60.0

#: 录音期间的自检周期（毫秒）。也用来兜住"设备不回调"的死局。
_WATCHDOG_MS = 200


def _error_name(err) -> str:
    """把 Qt 音频错误枚举取成**名字字符串**（跨枚举类比较用）。

    PySide6 里同一个错误值可能来自两个不同的枚举类：``QAudioSource.error()``
    返回的是内部的 ``QtAudio.Error``，而 ``QAudio.Error`` 是另一个类，两者
    ``==`` 恒为 False（value 都是 0 也不相等）。所以判定"有没有出错"只能比名字
    或数值，不能比枚举对象。取不到名字时退回字符串形式。
    """
    name = getattr(err, "name", None)
    if name:
        return str(name)
    text = str(err or "")
    return text.rsplit(".", 1)[-1] if "." in text else text


def _human_open_error(exc, device) -> str:
    """把 Qt / WASAPI 的原始错误说成人话（这句话用户真会看到）。

    只翻译**机主实测会撞上**的那几个码，其余原样带上一句中文兜底——宁可啰嗦，
    也不要让用户对着 ``AUDCLNT_E_DEVICE_INVALIDATED`` 猜发生了什么。
    """
    name = ""
    try:
        name = str(device.description() or "")
    except Exception:
        name = ""
    label = f"「{name}」" if name else "麦克风"
    text = str(exc) if exc is not None else ""
    upper = text.upper()
    if "AUDCLNT_E_DEVICE_INVALIDATED" in upper or "0X88890004" in upper:
        detail = "已经断开了（耳机拔了？没插紧？）"
    elif "AUDCLNT_E_DEVICE_IN_USE" in upper or "0X8889000A" in upper:
        detail = "被别的程序占着（关掉会议软件再试）"
    elif "AUDCLNT_E_UNSUPPORTED_FORMAT" in upper or "0X88890008" in upper:
        detail = "不支持 16 kHz 单声道"
    elif _error_name(exc) == "OpenError":
        # Qt 把 WASAPI 的内部失败吞成一句 OpenError，多半就是设备没了 / 被独占
        detail = "打不开（设备已断开，或被别的程序占着）"
    elif text:
        detail = text[:80]
    else:
        detail = "被占用或驱动异常"
    return f"{label}{detail}"


class SpeechRecorder(QObject):
    """一次录音的生命周期：``start()`` → 若干 ``readyRead`` → ``finished``。

    信号：

    * ``finished(list[float] | None)`` —— 成功时给出**已裁剪**的 16 kHz 单声道
      波形（-1.0~1.0）；``None`` 表示这段里没检测到说话（用户没开口 / 没麦克风声音）。
    * ``failed(str)`` —— 起不来或中途坏了，参数是给用户看的一句话。

    两者互斥，一次录音只有一个结局。
    """

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, *, silence_seconds: float = 1.0,
                 max_wait_seconds: float = MAX_WAIT_SECONDS,
                 max_record_seconds: float = MAX_RECORD_SECONDS) -> None:
        super().__init__(parent)
        self._silence_s = max(0.2, float(silence_seconds))
        self._max_wait_s = max(1.0, float(max_wait_seconds))
        self._max_record_s = max(2.0, float(max_record_seconds))
        self._source = None      # QAudioSource（必须持引用，否则被 GC）
        self._io = None          # QAudioSource.start() 给的 QIODevice
        self._vad = None
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(_WATCHDOG_MS)
        self._watchdog.timeout.connect(self._on_watchdog)
        self._reset_buffers()
        self._started_at = 0.0
        self._active = False
        # 设备管理器必须**长期持有**：Qt 只给活着的 QMediaDevices 实例发
        # audioInputsChanged，临时调静态方法拿到的默认设备可能是已经拔走的端点
        # （2026-10-26 机主实测：拔掉耳机后 defaultAudioInput() 仍返回那支耳机麦，
        # 激活即 AUDCLNT_E_DEVICE_INVALIDATED）。
        self._devices = None
        #: 本进程内激活失败过的设备名（拔走的耳机、被独占的设备）。下次录音跳过它，
        #: 设备列表变化（插回来 / 换设备）时清空——见 _on_devices_changed。
        self._bad_devices: set[str] = set()
        #: 每次录音新建的临时 QMediaDevices（保留引用：候选设备的句柄靠它活着）。
        self._probe = None
        #: 本次录音实际用的设备（中途失效时要知道是谁坏掉的）。
        self._device = None
        #: 上一次枚举到的输入设备名集合。变了就说明有人插/拔了设备，
        #: 之前"打不开"的记录必须作废——否则插回来的耳机会一直被跳过。
        self._device_snapshot: set[str] = set()

    # ---------------------------------------------------------------- 状态
    @property
    def recording(self) -> bool:
        return self._active

    def set_silence_seconds(self, seconds: float) -> None:
        """改静音收尾阈值（热生效：下次录音起算，不打断正在进行的这一次）。

        下限 0.2 秒是保护：再短会在换气处被截断；上限交给调用方收紧（见
        ``voice_talk._silence_seconds``）。
        """
        self._silence_s = max(0.2, float(seconds))

    # ---------------------------------------------------------------- 起停
    def start(self) -> tuple[bool, str]:
        """开始录音。返回 ``(是否起来了, 原因)``；起来之前不 emit 任何信号。"""
        if self._active:
            return True, ""
        ok, reason = availability()
        if not ok:
            return False, reason
        # 注意：这两个名字在下面的循环里都要用，必须在 return 之后、无条件导入——
        # 塞进上面的 if 分支会让成功路径上出现 UnboundLocalError。
        # 不 import QAudio：错误枚举的**对象身份不可靠**（见 _error_name 的说明），
        # 判定一律走名字字符串。
        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSource
        except Exception as exc:  # pragma: no cover - 取决于构建
            return False, f"这台机器没有可用的录音组件（{type(exc).__name__}）"

        # 候选设备：默认的那支优先，其余依次兜底。这样拔掉耳机（默认设备失效）后
        # 会自动落到内置麦克风阵列，而不是直接报一句看不懂的英文错误码。
        candidates = self._candidate_devices()
        if not candidates:
            return False, "没有找到麦克风（系统声音设置里把输入设备设为默认再试）"

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)

        last_reason = ""
        for device in candidates:
            source = io = None
            try:
                source = QAudioSource(device, fmt)
                # 50 ms 一包：readyRead 够勤，VAD 判定才跟得上口型；再小只是徒增回调。
                source.setBufferSize(int(SAMPLE_RATE * 2 * 0.05))
                io = source.start()
            except Exception as exc:
                logger.warning("打开输入设备失败：%s", exc)
                reason = _human_open_error(exc, device)
            else:
                reason = _human_open_error(None, device) if io is None else ""
                # start() 给了设备不代表真的在采音：拔走的端点会给一个空壳 io，
                # 只有 error() 才诚实（症状是「录音了但一直没听到说话」）。
                #
                # 巨坑（2026-10-26 实测）：``QAudioSource.error()`` 返回的枚举类是
                # **QtAudio.Error**，而 ``QAudio.Error`` 是另一个 Python 枚举类——
                # 两者 value 都是 0 但 ``==`` 恒为 False！照字面写
                # ``source.error() != QAudio.Error.NoError`` 会让**每一支**设备都被
                # 判成失败（连好端端的麦克风也一样），于是全部拉黑、永远打不开。
                # 所以只认名字 / 数值，不认枚举对象身份。
                err = source.error()
                if not reason and _error_name(err) != "NoError":
                    reason = _human_open_error(err, device)
            if reason:
                last_reason = reason
                self._bad_devices.add(device.description())
                self._discard_source(source)
                continue
            break
        else:
            logger.error("所有输入设备都打不开：%s", last_reason)
            # 逐支重试都失败时才补这句"都试过了"——单支设备的失败消息里不该带上它。
            tried = "、".join(d.description() for d in candidates) or "（设备列表为空）"
            return False, f"{last_reason or '麦克风打不开'}；都试过了：{tried}"

        self._bad_devices.discard(device.description())
        self._device = device
        # 不连 stateChanged：本机 PySide6 会把 QtAudio::State 递给声明成
        # QAudio::State 的槽，转换失败直接抛 "Cannot call meta function ...
        # cannot be converted"，槽压根收不到（换 lambda 转接也一样，错误照旧）。
        # 所以"录音中设备被拔"改由看门狗每 200 ms 轮询一次（见 _device_broken）。

        self._reset_buffers()
        self._source = source
        self._io = io
        self._actual_format = source.format()
        self._vad = self._build_vad()
        if self._vad is None:
            self._teardown()
            return False, f"断句模型加载失败（{VAD_MODEL}），语音输入不可用"
        io.readyRead.connect(self._on_ready)
        self._active = True
        self._started_at = time.monotonic()
        self._watchdog.start()
        self._total = 0
        self._pending = []
        logger.info("开始录音：%s，实际格式 %d Hz / %d 声道",
                    device.description(), self._actual_format.sampleRate(),
                    self._actual_format.channelCount())
        return True, ""

    def stop(self) -> None:
        """手动收尾（方案 B）。未在录音时是 no-op。"""
        if not self._active:
            return
        self._finish("手动结束")

    # ---------------------------------------------------------------- 设备
    def _device_manager(self):
        """长期持有的 ``QMediaDevices``（只建一次，并监听设备增减）。

        Qt 只给**活着的** QMediaDevices 实例发 ``audioInputsChanged``；每次录音
        临时调静态方法，拿到的默认设备可能是早已拔走的端点。
        """
        if self._devices is None:
            try:
                from PySide6.QtMultimedia import QMediaDevices
            except Exception:  # pragma: no cover - 取决于构建
                return None
            self._devices = QMediaDevices(self)
            try:
                self._devices.audioInputsChanged.connect(self._on_devices_changed)
            except Exception:  # pragma: no cover - 取决于构建
                pass
        return self._devices

    def _fresh_device_manager(self):
        """**每次**新建一个 ``QMediaDevices``，只为拿到当前这一刻的设备句柄。

        为什么要重新枚举：驱动在插拔耳机会**重建音频端点**，旧句柄整体作废。
        机主实测——长期持有的实例拔掉耳机后仍把那支消失的 Realtek 端点列出来，
        拿它去激活必报 ``AUDCLNT_E_DEVICE_INVALIDATED``；而新建实例能问到
        系统当下的真实列表。
        """
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except Exception:  # pragma: no cover - 取决于构建
            return None
        try:
            return QMediaDevices()
        except Exception:
            logger.debug("临时枚举输入设备失败", exc_info=True)
            return None

    def _candidate_devices(self) -> list:
        """可尝试的输入设备：默认那支优先，其余兜底；已知坏掉的排后面。

        拔掉耳机后 Windows 仍把那支耳机麦当默认设备（激活必失败），所以必须
        备好第二选择——否则语音输入就成了「插着耳机才能用」。
        """
        ordered: list = []
        seen: set[str] = set()

        def _take(mgr) -> None:
            """把一个实例看到的设备收进候选（去重，默认那支排在最前）。"""
            if mgr is None:
                return
            try:
                default = mgr.defaultAudioInput()
                if default is not None and not default.isNull():
                    name = default.description()
                    if name not in seen:
                        seen.add(name)
                        ordered.append(default)
                for dev in mgr.audioInputs():
                    name = dev.description()
                    if name not in seen:
                        seen.add(name)
                        ordered.append(dev)
            except Exception:
                logger.debug("枚举输入设备失败", exc_info=True)

        # 先问新建的实例（句柄最新），再用长期持有的那个补漏。
        # 注意要留住引用：QAudioDevice 的可用性跟着来源实例走，实例被回收后
        # 拿到的设备可能又变成无效句柄。
        fresh = self._fresh_device_manager()
        if fresh is not None:
            self._probe = fresh
        _take(fresh)
        _take(self._device_manager())
        if not ordered:
            return []
        # 设备增减自测：不能只靠 audioInputsChanged——Qt 在部分机器上不发这个
        # 信号（机主实测：耳机拔了再插回来，录音仍卡在内置麦上）。所以每次录音
        # 都自己比对一次列表；**新出现的设备排到最前面**——刚插上的耳机 / 刚连上
        # 的蓝牙耳机，多半就是用户这时候想用的那支。首次枚举没有"新增"可言，
        # 保持默认设备优先。
        names = {d.description() for d in ordered}
        fresh = set() if not self._device_snapshot else (names - self._device_snapshot)
        if names != self._device_snapshot:
            logger.info("输入设备列表变了（%s）",
                        "、".join(sorted(names ^ self._device_snapshot)) or "")
            self._bad_devices.clear()
            self._device_snapshot = names
        if fresh:
            ordered = ([d for d in ordered if d.description() in fresh]
                       + [d for d in ordered if d.description() not in fresh])
        healthy = [d for d in ordered if d.description() not in self._bad_devices]
        # 全被拉黑过（刚拔完设备、列表还没刷新）时给一次机会：重试默认那支。
        return healthy or ordered

    def _on_devices_changed(self) -> None:
        """输入设备增减（插/拔耳机、USB 麦）：清掉拉黑记录，让新设备能被用上。"""
        if self._bad_devices:
            logger.info("输入设备列表变了，重置失败记录：%s",
                        "、".join(sorted(self._bad_devices)))
            self._bad_devices.clear()

    def _on_source_state_changed(self, state) -> None:
        """录音途中设备没了（耳机被拔）：立刻收尾，别让用户对着空气说话。"""
        if not self._active:
            return
        # 同 _error_name 的坑：stateChanged 递进来的是 QtAudio.State，拿它和
        # QAudio.State.StoppedState 用 == 比永远是 False，所以比名字。
        stopped = _error_name(state) == "StoppedState"
        if not stopped:
            return
        name = ""
        try:
            name = self._device.description() if self._device is not None else ""
        except Exception:
            name = ""
        if name:
            self._bad_devices.add(name)
        logger.warning("录音中设备失效：%s", name or "未知设备")
        self._finish("设备断开")

    @staticmethod
    def _discard_source(source) -> None:
        """丢弃一个打开失败的 QAudioSource（不留句柄、不抛错）。"""
        if source is None:
            return
        try:
            source.stop()
        except Exception:
            pass

    # ---------------------------------------------------------------- 内部
    def _reset_buffers(self) -> None:
        self._chunks: list[float] = []
        self._pending: list[float] = []
        self._total = 0
        self._first_voice = -1
        self._last_voice = -1
        self._actual_format = None

    @staticmethod
    def _build_vad():
        """建一个干净的 VAD（每次录音新建：内部状态不复用，免得上一句的尾巴干扰）。"""
        try:
            import sherpa_onnx
        except Exception:
            logger.exception("导入 sherpa-onnx 失败")
            return None
        try:
            cfg = sherpa_onnx.VadModelConfig()
            cfg.silero_vad.model = str(model_dir() / VAD_MODEL)
            cfg.silero_vad.threshold = 0.5
            # 这里的 min_silence_duration 只影响 VAD 内部切段，**不用于**产品上的
            # 「静音 1 秒收尾」（那个由本类按采样点自己算，见模块说明）。设 0.5 让
            # 内部切段不至于过分滞后。
            cfg.silero_vad.min_silence_duration = 0.5
            cfg.silero_vad.min_speech_duration = 0.25
            cfg.silero_vad.window_size = VAD_WINDOW
            cfg.sample_rate = SAMPLE_RATE
            cfg.num_threads = 1
            if not cfg.validate():
                logger.error("VAD 配置校验失败（模型文件可能损坏）：%s", cfg.silero_vad.model)
                return None
            return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        except Exception:
            logger.exception("创建 VAD 失败")
            return None

    def _on_ready(self) -> None:
        io = self._io
        if io is None:
            return
        try:
            data = bytes(io.readAll().data())
        except Exception:
            logger.exception("读取录音数据失败")
            self._finish("读取失败")
            return
        if not data:
            return
        samples = self._decode(data)
        if samples:
            self._consume(samples)

    def _decode(self, data: bytes) -> list[float]:
        """原始字节 → 16 kHz 单声道 float 波形。

        顺序：按真实位宽解成 float → 声道降混 → 重采样到 16 kHz。
        位宽分支只覆盖 Qt 实际会给的两种（Int16 / Float）；其余情况按 Int16 处理
        并把这件事记进日志——比静默产出噪声强。
        """
        fmt = self._actual_format
        rate = fmt.sampleRate() if fmt is not None else SAMPLE_RATE
        channels = max(1, fmt.channelCount()) if fmt is not None else 1
        kind = fmt.sampleFormat() if fmt is not None else None

        from PySide6.QtMultimedia import QAudioFormat

        if kind == QAudioFormat.Float:
            values = array.array("f")
            usable = len(data) - (len(data) % 4)
            values.frombytes(data[:usable])
            mono = [v for v in values]
        else:
            if kind != QAudioFormat.Int16:
                logger.debug("录音位宽不是 Int16/Float，按 Int16 解码")
            values = array.array("h")
            usable = len(data) - (len(data) % 2)
            values.frombytes(data[:usable])
            mono = [v / 32768.0 for v in values]

        if channels > 1:
            frames = len(mono) // channels
            mono = [sum(mono[i * channels:(i + 1) * channels]) / channels
                    for i in range(frames)]
        if rate != SAMPLE_RATE:
            mono = _resample(mono, rate, SAMPLE_RATE)
        return mono

    def _consume(self, samples: list[float]) -> None:
        """收下一批采样：入缓冲 → 按窗口喂 VAD → 判定是否该收尾。"""
        self._chunks.extend(samples)
        self._total += len(samples)
        self._pending.extend(samples)

        vad = self._vad
        while vad is not None and len(self._pending) >= VAD_WINDOW:
            window = self._pending[:VAD_WINDOW]
            del self._pending[:VAD_WINDOW]
            try:
                vad.accept_waveform(window)
                speaking = bool(vad.is_speech_detected())
            except Exception:
                logger.exception("VAD 判定失败，按静音处理")
                speaking = False
            if speaking:
                # 补偿 VAD 的固定滞后：判定为语音时，真实起点要往前挪一点；
                # 这里只用来记边界，挪常量即可（见模块说明）。
                lag = int(VAD_LAG_SECONDS * SAMPLE_RATE)
                here = self._total - len(self._pending)
                if self._first_voice < 0:
                    self._first_voice = max(0, here - lag)
                self._last_voice = max(0, here - lag)

        if self._total >= self._max_record_s * SAMPLE_RATE:
            self._finish("达到单次上限")
            return
        if self._first_voice < 0:
            if self._total >= self._max_wait_s * SAMPLE_RATE:
                self._finish("一直没听到说话")
            return
        quiet = (self._total - self._last_voice) / SAMPLE_RATE
        if quiet >= self._silence_s:
            self._finish(f"静音 {quiet:.1f} 秒")

    def _device_broken(self) -> bool:
        """录音中设备是不是没了（耳机被拔 / 被别的程序抢走）。

        轮询而不是监听信号——见 start() 里关于 stateChanged 的说明：这个 PySide6
        版本根本递不过来。每 200 ms 问一次设备，代价可以忽略，但能避免用户对着
        一支已经拔走的麦克风说完一整句。
        """
        source = self._source
        if source is None:
            return False
        try:
            if _error_name(source.error()) != "NoError":
                return True
            return _error_name(source.state()) == "StoppedState"
        except Exception:
            logger.debug("查询录音设备状态失败（可能已析构）", exc_info=True)
            return False

    def _on_watchdog(self) -> None:
        """兜住「设备不回调」：readyRead 一次都不来时也要能退出录音态。"""
        if not self._active:
            return
        if self._device_broken():
            try:
                if self._device is not None:
                    self._bad_devices.add(self._device.description())
            except Exception:
                pass
            self._finish("设备断开")
            return
        elapsed = time.monotonic() - self._started_at
        if elapsed >= self._max_wait_s and self._total == 0:
            logger.warning("录音期间没有收到任何音频数据（设备可能无输入）")
            self._finish("麦克风没有声音")
        elif elapsed >= self._max_record_s + 1.0:
            self._finish("达到单次上限")

    def _finish(self, reason: str) -> None:
        """收尾：停设备、裁掉首尾静音、发结果。"""
        if not self._active:
            return
        self._active = False
        clipped = self._clip()
        # 先拆设备再发信号：信号处理方可能立刻开始下一次录音（方案 B 连按），
        # 设备还占着会打不开。
        self._teardown()
        logger.info("录音结束（%s）：有效 %.2f 秒", reason,
                    (len(clipped) / SAMPLE_RATE) if clipped else 0.0)
        self.finished.emit(clipped or None)

    def _clip(self) -> list[float]:
        """裁出真正要识别的那一段：首帧语音前 0.2 秒 ~ 末帧语音后 0.3 秒。

        没检测到语音就返回空列表（调用方据此提示"没听清"），而不是把整段静音
        丢给识别器白跑一遍。
        """
        if self._first_voice < 0 or self._last_voice < 0:
            return []
        start = max(0, self._first_voice - int(HEAD_MARGIN_SECONDS * SAMPLE_RATE))
        end = min(len(self._chunks),
                  self._last_voice + int(TAIL_MARGIN_SECONDS * SAMPLE_RATE))
        if end <= start:
            return []
        return self._chunks[start:end]

    def _teardown(self) -> None:
        self._watchdog.stop()
        io = self._io
        source = self._source
        self._io = None
        self._source = None
        if io is not None:
            try:
                io.readyRead.disconnect(self._on_ready)
            except Exception:
                logger.debug("断开录音信号失败（可能已断开）", exc_info=True)
        if source is not None:
            # 不再连 stateChanged（见 start() 的说明），也就无需断开。
            try:
                source.stop()
            except Exception:
                logger.debug("停止录音设备失败（可能已析构）", exc_info=True)
        self._vad = None


def _resample(samples: list[float], src_rate: int, dst_rate: int) -> list[float]:
    """线性插值重采样。

    为什么不用更好的算法：语音识别的输入前端本来就会做降采样与特征抽取，线性插值
    引入的误差在这里被后级吃掉了；而它零依赖、O(n)、不会在录音回调里抖一下。
    只在设备不给我们 16 kHz 时才走这条（笔记本内置阵列常见 48 kHz）。
    """
    if not samples or src_rate == dst_rate:
        return samples
    if src_rate <= 0 or dst_rate <= 0:
        return samples
    ratio = dst_rate / src_rate
    out_len = int(len(samples) * ratio)
    if out_len <= 0:
        return []
    last = len(samples) - 1
    step = 1.0 / ratio
    out = [0.0] * out_len
    pos = 0.0
    for i in range(out_len):
        left = int(pos)
        if left >= last:
            out[i] = samples[last]
        else:
            frac = pos - left
            out[i] = samples[left] * (1.0 - frac) + samples[left + 1] * frac
        pos += step
    return out


__all__ = [
    "HEAD_MARGIN_SECONDS",
    "MAX_RECORD_SECONDS",
    "MAX_WAIT_SECONDS",
    "SpeechRecorder",
    "TAIL_MARGIN_SECONDS",
    "VAD_LAG_SECONDS",
    "VAD_WINDOW",
    "_resample",
]
