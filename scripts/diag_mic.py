"""麦克风体检：把当前所有输入设备逐个打开试一遍，看谁真的能采到音。

用途：桌宠语音输入"插着耳机能用、拔掉就哑"这类问题，靠猜是猜不出来的——
这个脚本把 Qt 看到的设备列表、每支设备能否激活、以及真的有没有声音数据
全部打出来，方便一眼定位。

用法（在 D:\\dsh-pet-indesktop 下）：

    .\\.venv\\Scripts\\python.exe scripts/diag_mic.py

**请在出问题的那个状态下跑**（比如拔掉耳机之后再跑一次），对比两次输出。
"""

from __future__ import annotations

import sys

SAMPLE_RATE = 16000


def main() -> int:
    try:
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
        from PySide6.QtMultimedia import (
            QAudioFormat,
            QAudioSource,
            QMediaDevices,
        )
    except Exception as exc:  # pragma: no cover
        print(f"导入 PySide6.QtMultimedia 失败：{exc}")
        return 1

    QCoreApplication(sys.argv)  # Qt 对象必须活着才能干活

    def dump(mgr, tag: str) -> list:
        if mgr is None:
            print(f"[{tag}] 无法创建 QMediaDevices")
            return []
        try:
            default = mgr.defaultAudioInput()
            print(f"[{tag}] 默认输入: {default.description() if not default.isNull() else '(空)'}")
            devs = list(mgr.audioInputs())
            print(f"[{tag}] 共 {len(devs)} 个输入设备:")
            for d in devs:
                mark = "  <默认>" if (not default.isNull() and d.description() == default.description()) else ""
                print(f"    - {d.description()}{mark}")
            return devs
        except Exception as exc:
            print(f"[{tag}] 枚举失败: {exc}")
            return []

    print("=" * 60)
    print("1) 长期持有的实例（桌宠里那个）")
    print("=" * 60)
    long_lived = QMediaDevices()
    dump(long_lived, "长期实例")

    print()
    print("=" * 60)
    print("2) 每次新建的实例（重新枚举，句柄最新）")
    print("=" * 60)
    fresh = QMediaDevices()
    dump(fresh, "新建实例")

    # 候选：新建实例的默认优先，然后两个实例的列表去重合并
    ordered: list = []
    seen: set[str] = set()
    for mgr in (fresh, long_lived):
        try:
            d0 = mgr.defaultAudioInput()
            if d0 is not None and not d0.isNull() and d0.description() not in seen:
                seen.add(d0.description())
                ordered.append(d0)
            for d in mgr.audioInputs():
                if d.description() not in seen:
                    seen.add(d.description())
                    ordered.append(d)
        except Exception:
            pass

    print()
    print("=" * 60)
    print("3) 逐个打开试音（每支录 0.8 秒，看有没有真数据）")
    print("=" * 60)
    fmt = QAudioFormat()
    fmt.setSampleRate(SAMPLE_RATE)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.Int16)

    any_ok = False
    for dev in ordered:
        name = dev.description()
        print(f"\n--- {name}")
        source = None
        try:
            source = QAudioSource(dev, fmt)
            source.setBufferSize(int(SAMPLE_RATE * 2 * 0.05))
            io = source.start()
        except Exception as exc:
            print(f"    构造/启动抛异常: {exc}")
            continue
        if io is None:
            print("    start() 返回 None（空壳设备）")
            try:
                source.stop()
            except Exception:
                pass
            continue
        err = getattr(source.error(), "name", str(source.error()))
        state = getattr(source.state(), "name", str(source.state()))
        print(f"    error = {err} | state = {state}")
        if err != "NoError":
            try:
                source.stop()
            except Exception:
                pass
            continue

        total = 0
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(100)

        def pump(_t=timer, _io=io) -> None:
            nonlocal total
            try:
                data = _io.readAll()
                total += len(data)
            except Exception:
                pass

        timer.timeout.connect(pump)
        timer.start()
        QTimer.singleShot(800, loop.quit)
        loop.exec()
        timer.stop()
        try:
            source.stop()
        except Exception:
            pass
        nbytes = total
        print(f"    0.8 秒收到 {nbytes} 字节（16bit/单声道约等于 {nbytes / 2 / SAMPLE_RATE:.2f} 秒音频）")
        if nbytes > 0:
            print("    => 可用")
            any_ok = True
        else:
            print("    => 打开了但没有任何数据（设备静音 / 被系统禁用 / 权限未开）")

    print()
    print("=" * 60)
    if any_ok:
        print("结论：至少有一支设备能采到音，桌宠应当可用。")
        return 0
    print("结论：所有输入设备都采不到音。请检查：")
    print("  - Windows 设置 → 系统 → 声音 → 输入，确认默认设备不是已拔走的那支")
    print("  - 隐私和安全性 → 麦克风，确认「允许应用访问麦克风」已开")
    print("  - 设备管理器里有没有被禁用的输入设备")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
