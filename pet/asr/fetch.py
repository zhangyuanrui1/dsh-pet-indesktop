# -*- coding: utf-8 -*-
"""拉取语音识别模型（229 MB，不入库）——**纯逻辑，无 Qt**，给两个调用方共用。

调用方：

* ``scripts/fetch_asr_models.py``（命令行，仓库根 ``python scripts/fetch_asr_models.py``）
* 设置页「语音输入 → 语音识别模型」那一行的下载按钮（``pet/voice/asr_model_row.py``）

**为什么要抽成模块**：两处逻辑必须逐字一致——下载源回退、断流续传、sha256 校验
任何一条在两边走岔，就会出现"命令行能装上、设置页装上的是坏文件"这种最难查的
问题。所以本模块是唯一实现，两个入口都只是回调不同。

线程纪律：:func:`fetch_models` 是**阻塞**的，调用方自己决定放哪个线程（设置页放在
``threading.Thread(daemon=True)`` 里）。``progress`` / ``notify`` 回调在**调用线程**
执行——UI 侧要经 Qt 信号转回主线程再碰控件，不要在这里替调用方 ``emit``。

来源与许可见 ``THIRD_PARTY_NOTICES.md`` 第 10 节；三个文件的 sha256 常量与那里
的表格是同一份实测值，改版本时两处要一起改。
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

#: 三个必需文件（顺序即校验与提示顺序）
MODEL_FILES: tuple[str, ...] = ("model.int8.onnx", "tokens.txt", "silero_vad.onnx")

#: 上游发行（k2-fsa/sherpa-onnx 的 asr-models tag）。换版本时摘要要一起换。
RELEASE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
SENSEVOICE_ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
VAD_FILE = "silero_vad.onnx"

#: 压缩包里的文件 → 目标文件名（包内目录层级不固定，按 basename 取）
_ARCHIVE_MEMBERS = ("model.int8.onnx", "tokens.txt")

#: 镜像前缀（拼在完整 URL 前面）。第一个实测可用；直连放最后兜。
DEFAULT_MIRRORS: tuple[str, ...] = ("https://ghproxy.net/", "")

#: 目标文件 → sha256（与第三方声明第 10 节同一份实测值）
EXPECTED_SHA256: dict[str, str] = {
    "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    "silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}

#: 期望体积（字节）：进度条与"下到一半"的粗判；真闸门是 sha256。
EXPECTED_SIZE: dict[str, int] = {
    "model.int8.onnx": 239_233_841,
    "tokens.txt": 315_894,
    "silero_vad.onnx": 643_854,
}

#: 装好之后占多少（给界面说人话用）
TOTAL_BYTES = sum(EXPECTED_SIZE.values())

#: 压缩包体积（下之前预估磁盘与时间用；实测 ghproxy 上是这个值）
ARCHIVE_BYTES = 163_002_883


class FetchCancelled(Exception):
    """调用方取消（``CancelToken.cancel()``）：已下的 ``.part`` 保留，下次接着下。"""


class FetchError(Exception):
    """下载/校验失败（消息是给用户看的人话，不含调用栈）。"""


class CancelToken:
    """取消开关。线程安全；下载器每个数据块检查一次。

    为什么不用 ``threading.Event`` 直接传：调用方只知道"取消"这一个动作，不该
    被迫记住"要在哪些位置检查"——检查点收在这里，加新检查点只改一处。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """已取消就抛 :class:`FetchCancelled`（在数据块循环里调）。"""
        if self._event.is_set():
            raise FetchCancelled("已取消")


ProgressCallback = Callable[[int, int], None]
NotifyCallback = Callable[[str], None]


def _notify(callback: NotifyCallback | None, text: str) -> None:
    """阶段文案回调；没传就当没人看（命令行传 print，UI 传信号）。"""
    if callback is not None:
        callback(text)


def sha256_of(path: Path) -> str:
    """流式算摘要（239 MB 不整读进内存）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, name: str, *, checksum: bool = True) -> tuple[bool, str]:
    """某个模型文件是否已就绪：``(通过, 人话原因)``。

    ``checksum=False`` 时只核对体积（毫秒级），给**界面刷新状态**用：设置页每打
    开一次就为 239 MB 算一遍 sha256 会让页面明显卡顿，而体积对上已经能挡掉
    "没下完/下错文件"这类绝大多数情况。真正的闸门（下载完成后、打包前）一律
    走默认的 ``checksum=True``。
    """
    target = Path(path)
    expected_size = EXPECTED_SIZE[name]
    if not target.is_file():
        return False, "文件不存在"
    size = target.stat().st_size
    if size != expected_size:
        return False, f"体积不对（{size:,} 字节，应为 {expected_size:,}）"
    if not checksum:
        return True, ""
    digest = sha256_of(target)
    if digest != EXPECTED_SHA256[name]:
        return False, f"sha256 不符（{digest[:16]}…）"
    return True, ""


def quick_states(directory: Path | str) -> dict[str, tuple[bool, str]]:
    """只按体积判定的状态表（界面用，不读 239 MB 算摘要）。"""
    root = Path(directory)
    return {name: verify_file(root / name, name, checksum=False) for name in MODEL_FILES}


def missing_files(directory: Path | str) -> list[str]:
    """哪些文件还没就绪（含体积/摘要不对的；空列表 = 齐了）。"""
    root = Path(directory)
    return [name for name in MODEL_FILES if not verify_file(root / name, name)[0]]


def human_size(num: float) -> str:
    """字节 → ``1.5 MB``（进度文案用；不引入第三方格式化库）。"""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _pick_source(url: str, mirrors, notify=None, timeout: float = 20.0) -> str | None:
    """逐个前缀试一次 HEAD，返回第一个连得上的完整地址。

    HEAD 在部分镜像上不被支持（403/405/501），那不代表不能下——只淘汰**连接层
    就断了**的前缀，真正确认靠后面带进度的 GET。
    """
    for prefix in mirrors:
        candidate = prefix + url if prefix else url
        host = prefix.split("/")[2] if prefix else "github.com"
        try:
            request = urllib.request.Request(candidate, method="HEAD")
            with urllib.request.urlopen(request, timeout=timeout):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405, 501):
                _notify(notify, f"[{host}] 不支持 HEAD，直接尝试下载")
                return candidate
            _notify(notify, f"[{host}] HTTP {exc.code}，换下一个源")
            continue
        except Exception as exc:  # noqa: BLE001 - 任何连接失败都换源
            _notify(notify, f"[{host}] 连不上（{type(exc).__name__}），换下一个源")
            continue
        return candidate
    return None


def _download_once(source: str, part: Path, *, progress, cancel: CancelToken,
                   chunk_timeout: float) -> tuple[bool, int]:
    """跑一趟 GET（有 ``.part`` 就带 Range 续传）。返回 ``(是否下满, 本次新增字节)``。

    **必须核对长度**：镜像断流是常态（实测 ghproxy 在 85% 处掐断，之后 tar 解压
    才报 "Compressed file ended before the end-of-stream marker"）。下多少算多少，
    不完整就交给外层换源续传，绝不把半个文件当成品。
    """
    have = part.stat().st_size if part.is_file() else 0
    request = urllib.request.Request(source)
    if have:
        request.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(request, timeout=chunk_timeout) as response:
        resume = bool(have) and response.status == 206
        if not resume:
            have = 0
        total = have + int(response.headers.get("Content-Length") or 0)
        start_bytes = have
        with part.open("ab" if resume else "wb") as handle:
            while True:
                cancel.check()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                have += len(chunk)
                if progress is not None:
                    progress(have, total)
        return (bool(total) and have >= total), have - start_bytes


def download(url: str, dest: Path, *, progress=None, notify=None,
             cancel: CancelToken | None = None, mirrors=None,
             attempts: int = 6) -> Path:
    """下载到 ``dest``：先写 ``.part``，**续传重试到下满**，成功后改名。

    每次重试**换下一个源**：实测同一天里 ghproxy 与直连会各挂一次（一边 502、
    一边超时），钉死一个源重试只会陪着它一起失败。
    """
    token = cancel or CancelToken()
    mirror_list = tuple(mirrors or DEFAULT_MIRRORS)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, attempts + 1):
        token.check()
        order = mirror_list[attempt - 1:] + mirror_list[:attempt - 1]
        source = _pick_source(url, order, notify)
        if source is None:
            _notify(notify, f"第 {attempt} 次：所有下载源都连不上")
            gained = 0
        else:
            _notify(notify, f"第 {attempt} 次源 {source}")
            try:
                complete, gained = _download_once(
                    source, part, progress=progress, cancel=token, chunk_timeout=60.0)
            except FetchCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 断流/超时/连接重置都重试
                complete, gained = False, 0
                _notify(notify, f"中断（{type(exc).__name__}）")
            if complete:
                part.replace(dest)
                return dest
        if attempt == attempts:
            break
        wait = min(10, 2 * attempt)
        token.check()  # 等待前先看一眼取消，别让用户等满 10 秒才停
        _notify(notify, "这次没有进展，换源重试" if gained == 0 else "没下完，续传")
        # 取消要能打断等待：分段睡，每 200ms 看一眼
        for _ in range(int(wait * 5)):
            token.check()
            time.sleep(0.2)

    size = part.stat().st_size if part.is_file() else 0
    raise FetchError(f"重试 {attempts} 次仍未下完（已下 {human_size(size)}）——"
                     f"重跑会接着下（{part.name} 已保留）")


def _extract(archive: Path, dest_dir: Path, names, *, notify=None) -> list[str]:
    """从 tar.bz2 里挑出 ``names`` 提到 ``dest_dir``（包内层级不固定，按 basename）。"""
    wanted = set(names)
    found: list[str] = []
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar:
            base = Path(member.name).name
            if not member.isfile() or base not in wanted or base in found:
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target = dest_dir / base
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            found.append(base)
            _notify(notify, f"解出 {base}（{human_size(target.stat().st_size)}）")
    missing = wanted - set(found)
    if missing:
        raise FetchError(f"发行包里没有这些文件：{'、'.join(sorted(missing))}")
    return found


def check_disk_space(target: Path, need: int) -> None:
    """预检磁盘余量：几百 MB 下到一半才发现放不下是最差的失败方式。

    目标目录可能还不存在，所以往上找到第一个存在的祖先再问。
    """
    probe = Path(target)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < need:
        raise FetchError(f"磁盘空间不足：约需 {need // 1048576} MB，"
                         f"剩余 {free // 1048576} MB")


def fetch_models(target: Path | str, *, progress: ProgressCallback | None = None,
                 notify: NotifyCallback | None = None, cancel: CancelToken | None = None,
                 mirrors=None, keep_archive: bool = False,
                 skip_disk_check: bool = False) -> list[str]:
    """把三个模型文件备齐到 ``target``；返回**本次真的下载了**的文件名。

    已就绪的文件逐个跳过（幂等）；缺任一个识别文件就下整包再挑文件；最后逐个按
    sha256 复验，不过关的文件会被删掉并抛 :class:`FetchError`——不留半成品。
    """
    token = cancel or CancelToken()
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)

    states = {name: verify_file(root / name, name) for name in MODEL_FILES}
    if all(ok for ok, _ in states.values()):
        _notify(notify, "三个模型文件都已就绪，无需下载")
        return []
    for name, (ok, reason) in states.items():
        _notify(notify, f"[{'就绪' if ok else '缺失'}] {name}" + ("" if ok else f"：{reason}"))

    if not skip_disk_check:
        # 压缩包 + 解出来的文件 + 余量：整包留到最后才删，所以要按"包在时"算
        check_disk_space(root, ARCHIVE_BYTES + TOTAL_BYTES + 64 * 1024 * 1024)

    downloaded: list[str] = []
    need_archive = [n for n in _ARCHIVE_MEMBERS if not states[n][0]]
    if need_archive:
        _notify(notify, f"下载识别模型包（{SENSEVOICE_ARCHIVE}，约 155 MB）")
        archive = root / SENSEVOICE_ARCHIVE
        download(RELEASE_BASE + SENSEVOICE_ARCHIVE, archive, progress=progress,
                 notify=notify, cancel=token, mirrors=mirrors)
        try:
            downloaded.extend(_extract(archive, root, need_archive, notify=notify))
        finally:
            if not keep_archive:
                archive.unlink(missing_ok=True)

    if not states[VAD_FILE][0]:
        _notify(notify, f"下载断句模型（{VAD_FILE}）")
        download(RELEASE_BASE + VAD_FILE, root / VAD_FILE, progress=progress,
                 notify=notify, cancel=token, mirrors=mirrors)
        downloaded.append(VAD_FILE)

    failures: list[tuple[str, str]] = []
    for name in MODEL_FILES:
        ok, reason = verify_file(root / name, name)
        if not ok:
            failures.append((name, reason))
            (root / name).unlink(missing_ok=True)
    if failures:
        detail = "；".join(f"{name}：{reason}" for name, reason in failures)
        raise FetchError(f"下载完成但校验不过（{detail}）——已删除坏文件，请重试")

    _notify(notify, "三个模型文件校验通过")
    return downloaded


__all__ = [
    "ARCHIVE_BYTES",
    "CancelToken",
    "DEFAULT_MIRRORS",
    "EXPECTED_SHA256",
    "EXPECTED_SIZE",
    "FetchCancelled",
    "FetchError",
    "MODEL_FILES",
    "RELEASE_BASE",
    "SENSEVOICE_ARCHIVE",
    "TOTAL_BYTES",
    "VAD_FILE",
    "check_disk_space",
    "download",
    "fetch_models",
    "human_size",
    "missing_files",
    "sha256_of",
    "verify_file",
]
