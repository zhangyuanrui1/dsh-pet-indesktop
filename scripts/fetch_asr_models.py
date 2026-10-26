# -*- coding: utf-8 -*-
"""拉取语音对话用的本地识别模型到 ``resources/asr_models/``。

**为什么需要这个脚本**：识别模型 ``model.int8.onnx`` 单个 239 MB，超过 GitHub
的 100 MB 单文件上限，所以不入库（见 ``THIRD_PARTY_NOTICES.md`` 第 10 节）。
干净克隆之后 ``resources/asr_models/`` 是空的，语音对话入口会如实提示"模型未
就绪"；跑一次本脚本即可补齐。

三个文件两个来源：

===================  ==============================================
``model.int8.onnx``  在 sherpa-onnx 的 ``asr-models`` 发行包（tar.bz2，155 MB
``tokens.txt``       压缩）里，下载后只挑这两个文件提出来，压缩包默认删掉
``silero_vad.onnx``  同一次发行的独立文件（629 KB），直接下
===================  ==============================================

用法（在仓库根目录）::

    python scripts/fetch_asr_models.py                  # 装到 resources/asr_models
    python scripts/fetch_asr_models.py --force          # 已就绪也重下（校验不过时用）
    python scripts/fetch_asr_models.py --target D:\\asr # 装到别处：配 DSH_PET_ASR_MODEL_DIR 指向它
    python scripts/fetch_asr_models.py --mirror https://ghproxy.net/
    python scripts/fetch_asr_models.py --keep-archive   # 留着 tar.bz2（默认删）

**默认先走镜像再直连**：``github.com`` 的 release 在国内多数网络下拿不到，而
``ghproxy.net`` 实测可达且字节一致。外网环境可用 ``--mirror ""`` 强制直连。

**每个文件都按 sha256 校验**（常量与 ``THIRD_PARTY_NOTICES.md`` 第 10 节同一份
实测值）：不符即报错并删掉坏文件，不会把半个模型留在目录里让语音对话静默变差。

只依赖标准库（urllib / tarfile / hashlib），不引入任何第三方包。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 仓库根（本脚本在 scripts/ 下）
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = REPO_ROOT / "resources" / "asr_models"

#: 上游发行包（k2-fsa/sherpa-onnx 的 asr-models tag）。改版本时三个摘要要一起换。
RELEASE_BASE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/")
SENSEVOICE_ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
VAD_FILE = "silero_vad.onnx"

#: 镜像前缀（拼在完整 URL 前面）。第一个实测可用；直连放最后。
DEFAULT_MIRRORS = ("https://ghproxy.net/", "")

#: 目标文件 → sha256（与第三方声明第 10 节同一份实测值）
EXPECTED_SHA256 = {
    "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    "silero_vad.onnx": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}

#: 期望体积（字节）；只用于进度显示与"下到一半"的粗判，真闸门是 sha256。
EXPECTED_SIZE = {
    "model.int8.onnx": 239_233_841,
    "tokens.txt": 315_894,
    "silero_vad.onnx": 643_854,
}

#: 回显进度的间隔（秒）：155 MB 按秒刷会刷上千行，2 秒一行刚好。
_PROGRESS_EVERY = 2.0


def sha256_of(path: Path) -> str:
    """流式算摘要（239 MB 不整读进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, name: str) -> tuple[bool, str]:
    """文件是否已就绪：``(通过, 人话原因)``。"""
    if not path.is_file():
        return False, "文件不存在"
    size = path.stat().st_size
    expected_size = EXPECTED_SIZE[name]
    if size != expected_size:
        return False, f"体积不对（{size:,} 字节，应为 {expected_size:,}）"
    digest = sha256_of(path)
    if digest != EXPECTED_SHA256[name]:
        return False, f"sha256 不符（{digest[:16]}…）"
    return True, ""


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _pick_mirror(url: str, mirrors, timeout: float = 20.0) -> str | None:
    """逐个前缀试一次 HEAD，返回第一个连得上的完整地址。

    HEAD 在部分镜像上不被支持（405），所以失败不代表不能下——这里只淘汰
    **连接层就断了**的前缀，真正确认靠下面带进度的 GET。
    """
    for prefix in mirrors:
        candidate = prefix + url if prefix else url
        host = prefix.split("/")[2] if prefix else "github.com"
        try:
            request = urllib.request.Request(candidate, method="HEAD")
            with urllib.request.urlopen(request, timeout=timeout):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405, 501):  # 不支持 HEAD，但站点是通的
                print(f"   [{host}] 不支持 HEAD，直接尝试下载")
                return candidate
            print(f"   [{host}] HTTP {exc.code}，换下一个源")
            continue
        except Exception as exc:
            print(f"   [{host}] 连不上（{type(exc).__name__}），换下一个源")
            continue
        return candidate
    return None


def _download_once(picked: str, part: Path) -> tuple[bool, int]:
    """跑一趟 GET（有 .part 就带 Range 续传）。返回 ``(是否下满, 本次新增字节)``。

    **必须核对长度**：镜像断流是常态（实测 ghproxy 在 85% 处掐断，tar 解压才报
    "Compressed file ended before the end-of-stream marker"）。下多少算多少，
    不完整就交给外层重试续传，绝不把半个文件当成品。
    """
    have = part.stat().st_size if part.is_file() else 0
    request = urllib.request.Request(picked)
    if have:
        request.add_header("Range", f"bytes={have}-")
        print(f"   续传（已有 {_human(have)}）")
    with urllib.request.urlopen(request, timeout=60) as response:
        # 服务端不认 Range（回 200）时只能从头写
        resume = have and response.status == 206
        if not resume:
            have = 0
        total = have + int(response.headers.get("Content-Length") or 0)
        started = last = time.monotonic()
        start_bytes = have
        last_pct = -5
        tty = sys.stdout.isatty()
        with part.open("ab" if resume else "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                have += len(chunk)
                now = time.monotonic()
                pct = have / total * 100 if total else 0.0
                if tty and now - last >= _PROGRESS_EVERY:
                    last = now
                    speed = (have - start_bytes) / max(0.001, now - started)
                    print(f"   {pct:5.1f}%  {_human(have)} / {_human(total)}"
                          f"  {_human(speed)}/s", end="\r", flush=True)
                elif not tty and total and pct - last_pct >= 10:
                    # 管道/日志里只报整十个百分点，免得刷上千行
                    last_pct = pct
                    speed = (have - start_bytes) / max(0.001, now - started)
                    print(f"   {pct:5.1f}%  {_human(have)} / {_human(total)}"
                          f"  {_human(speed)}/s", flush=True)
        if tty:
            print()
        complete = bool(total) and have >= total
        return complete, have - start_bytes


def download(url: str, dest: Path, *, mirrors, attempts: int = 6) -> Path:
    """下载到 ``dest``：先写 ``.part``，**续传重试到下满**，成功后改名。

    每次重试**换下一个源**：实测同一天里 ghproxy 与直连会各挂一次（一边 502、
    一边超时），钉死一个源重试只会陪着它一起失败。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, attempts + 1):
        order = mirrors[attempt - 1:] + mirrors[:attempt - 1]
        picked = _pick_mirror(url, order)
        if picked is None:
            print(f"   第 {attempt} 次：所有下载源都连不上")
            complete, gained = False, 0
        else:
            print(f"   第 {attempt} 次源 {picked}")
            try:
                complete, gained = _download_once(picked, part)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # 断流/超时/连接重置：保留 .part 再试
                complete, gained = False, 0
                print(f"   中断（{type(exc).__name__}）")
        if complete:
            part.replace(dest)
            return dest
        if attempt == attempts:
            break
        wait = min(10, 2 * attempt)
        if gained == 0:
            print(f"   这次没有进展，{wait} 秒后换源重试（Ctrl+C 后重跑也能接着下）")
        else:
            print(f"   没下完，{wait} 秒后续传")
        time.sleep(wait)
    size = part.stat().st_size if part.is_file() else 0
    raise RuntimeError(f"重试 {attempts} 次仍未下完（{_human(size)}），"
                       f".part 已保留：{part.name}——重跑本脚本会接着下")


def extract_from_archive(archive: Path, dest_dir: Path, names) -> None:
    """从 tar.bz2 里挑出 ``names`` 提到 ``dest_dir``（压缩包内目录层级不固定）。"""
    wanted = set(names)
    found: set[str] = set()
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar:
            base = Path(member.name).name
            if not member.isfile() or base not in wanted:
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target = dest_dir / base
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            found.add(base)
            print(f"   解出 {base}（{_human(target.stat().st_size)}）")
    missing = wanted - found
    if missing:
        raise RuntimeError(f"发行包里没有这些文件：{'、'.join(sorted(missing))}")


def fetch(target: Path, *, force: bool, mirrors, keep_archive: bool) -> int:
    """主流程：能跳过的就跳过，缺什么补什么。返回进程退出码。"""
    print(f"目标目录：{target}")
    target.mkdir(parents=True, exist_ok=True)

    if force:
        for name in EXPECTED_SHA256:
            candidate = target / name
            if candidate.exists():
                candidate.unlink()
                print(f"   --force：已删除 {name}")

    ready, reasons = {}, {}
    for name in EXPECTED_SHA256:
        ok, reason = verify(target / name, name)
        ready[name] = ok
        if not ok:
            reasons[name] = reason

    for name, ok in ready.items():
        print(f"   [{'就绪' if ok else '缺失'}] {name}" + ("" if ok else f"：{reasons[name]}"))
    if all(ready.values()):
        print("三个模型文件都已就绪，无需下载。")
        return 0

    # 1) 识别模型与词表在同一压缩包里：缺任一个就下整包
    need_archive = [n for n in ("model.int8.onnx", "tokens.txt") if not ready[n]]
    if need_archive:
        print(f"下载识别模型包（{SENSEVOICE_ARCHIVE}，约 155 MB 压缩）…")
        archive = target / SENSEVOICE_ARCHIVE
        download(RELEASE_BASE + SENSEVOICE_ARCHIVE, archive, mirrors=mirrors)
        try:
            extract_from_archive(archive, target, need_archive)
        finally:
            if not keep_archive:
                archive.unlink(missing_ok=True)
                print("   已删除压缩包（--keep-archive 可保留）")

    # 2) VAD 单独下
    if not ready[VAD_FILE]:
        print(f"下载断句模型（{VAD_FILE}）…")
        download(RELEASE_BASE + VAD_FILE, target / VAD_FILE, mirrors=mirrors)

    # 3) 收尾：逐个按 sha256 复验，不许"下完就算数"
    print("校验：")
    failures = []
    for name in EXPECTED_SHA256:
        ok, reason = verify(target / name, name)
        print(f"   [{'OK ' if ok else 'BAD'}] {name}")
        if not ok:
            failures.append((name, reason))
            (target / name).unlink(missing_ok=True)
    if failures:
        print("\n失败：")
        for name, reason in failures:
            print(f"   {name}：{reason}")
        print("已删除未通过校验的文件，请重跑本脚本（或 --force）。")
        return 2

    print("\n完成。语音对话入口即可用（重启桌宠或直接按热键）。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="下载语音对话所需的本地识别模型（不入库，见第三方声明第 10 节）")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                        help=f"装到哪个目录（默认 {DEFAULT_TARGET}）")
    parser.add_argument("--force", action="store_true", help="已就绪也重新下载")
    parser.add_argument("--mirror", default=None,
                        help='只用这一个镜像前缀（如 https://ghproxy.net/）；传 "" 走直连')
    parser.add_argument("--keep-archive", action="store_true", help="保留下载的 tar.bz2")
    args = parser.parse_args(argv)

    mirrors = DEFAULT_MIRRORS if args.mirror is None else (args.mirror,)
    try:
        return fetch(args.target, force=args.force, mirrors=mirrors,
                     keep_archive=args.keep_archive)
    except KeyboardInterrupt:
        print("\n已中断（.part 文件保留，下次续传）。")
        return 130
    except Exception as exc:
        print(f"\n失败：{type(exc).__name__}: {exc}")
        print("手动兜底：按 THIRD_PARTY_NOTICES.md 第 10 节给出的地址自行下载，"
              f"把三个文件放进 {args.target}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
