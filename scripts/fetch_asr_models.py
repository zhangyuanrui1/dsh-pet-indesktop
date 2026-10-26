# -*- coding: utf-8 -*-
"""拉取语音对话用的本地识别模型到 ``resources/asr_models/``（命令行入口）。

**为什么需要下载**：识别模型 ``model.int8.onnx`` 单个 239 MB，超过 GitHub 的
100 MB 单文件上限，所以不入库（见 ``THIRD_PARTY_NOTICES.md`` 第 10 节）。干净
克隆之后目录是空的，语音对话入口会如实提示"模型未就绪"。

下载逻辑**不在这里**：唯一实现在 ``pet/asr/fetch.py``，设置页的下载按钮与本
脚本共用它（两处若各写一套，迟早出现"命令行装得上、设置页装的是坏文件"）。
本文件只负责：解析参数、把进度打到终端、把异常翻成人话。

用法（在仓库根目录）::

    python scripts/fetch_asr_models.py                  # 装到 resources/asr_models
    python scripts/fetch_asr_models.py --force          # 已就绪也重下（校验不过时用）
    python scripts/fetch_asr_models.py --target D:\\asr # 装到别处：配 DSH_PET_ASR_MODEL_DIR 指向它
    python scripts/fetch_asr_models.py --mirror https://ghproxy.net/
    python scripts/fetch_asr_models.py --keep-archive   # 留着 tar.bz2（默认删）

**默认先走镜像再直连**：``github.com`` 的 release 在国内多数网络下拿不到，而
``ghproxy.net`` 实测可达且字节一致。外网环境可用 ``--mirror ""`` 强制直连。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # 直接 `python scripts/fetch_asr_models.py` 时 sys.path[0] 是 scripts/，
    # 不加这一行 import pet 会失败（不去动 PYTHONPATH，双击/计划任务也能跑）。
    sys.path.insert(0, str(REPO_ROOT))

from pet.asr import fetch  # noqa: E402 - 必须在 sys.path 调整之后

DEFAULT_TARGET = REPO_ROOT / "resources" / "asr_models"

#: 进度回显：终端里每 2 秒刷一行，管道/日志里只报整十个百分点。
_PROGRESS_EVERY = 2.0


def _printer():
    """返回 ``(progress, notify)`` 两个回调，内部记住上次打印状态。"""
    state = {"last": 0.0, "pct": -10, "tty": sys.stdout.isatty()}

    def progress(got: int, total: int) -> None:
        now = time.monotonic()
        pct = got * 100 // total if total else 0
        if state["tty"]:
            if now - state["last"] < _PROGRESS_EVERY:
                return
            state["last"] = now
            print(f"   {pct:5d}%  {fetch.human_size(got)} / {fetch.human_size(total)}",
                  end="\r", flush=True)
            return
        if pct - state["pct"] >= 10:
            state["pct"] = pct
            print(f"   {pct:5d}%  {fetch.human_size(got)} / {fetch.human_size(total)}",
                  flush=True)

    def notify(text: str) -> None:
        if state["tty"]:
            print(" " * 60, end="\r")  # 盖掉上一行进度，免得和阶段文案糊在一起
        print(text, flush=True)

    return progress, notify


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

    target: Path = args.target
    mirrors = fetch.DEFAULT_MIRRORS if args.mirror is None else (args.mirror,)
    print(f"目标目录：{target}")

    if args.force:
        for name in fetch.MODEL_FILES:
            candidate = target / name
            if candidate.exists():
                candidate.unlink()
                print(f"   --force：已删除 {name}")

    progress, notify = _printer()
    try:
        downloaded = fetch.fetch_models(target, progress=progress, notify=notify,
                                        mirrors=mirrors, keep_archive=args.keep_archive)
    except fetch.FetchCancelled:
        print("\n已取消（.part 保留，下次续传）。")
        return 130
    except KeyboardInterrupt:
        print("\n已中断（.part 保留，下次续传）。")
        return 130
    except fetch.FetchError as exc:
        print(f"\n失败：{exc}")
        print("手动兜底：按 THIRD_PARTY_NOTICES.md 第 10 节的地址自行下载，"
              f"把三个文件放进 {target}")
        return 2

    if downloaded:
        print(f"\n完成（本次下载 {len(downloaded)} 个文件）。"
              "语音对话入口即可用：重启桌宠，或直接在设置页按热键试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
