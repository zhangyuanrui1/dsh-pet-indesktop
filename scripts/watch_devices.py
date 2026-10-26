"""设备观察器：在一个**持续运行的进程**里盯住 Qt 看到的输入/输出设备。

为什么要这个脚本：``diag_mic.py`` 每次都是新进程，新进程当然能枚举到最新的
设备；真正的问题在于**桌宠那个跑了几小时的进程**——Qt 会不会在进程内刷新
设备列表？拔掉耳机后它还认不认得出新情况？只有长跑进程能回答。

用法（在 D:\\dsh-pet-indesktop 下）：

    .\\.venv\\Scripts\\python.exe scripts/watch_devices.py

然后**在脚本运行期间做插拔动作**（默认观察 90 秒）：

    第 0~20 秒   什么都不动
    第 20 秒左右 拔掉耳机
    第 50 秒左右 再插回去

观察打印出来的默认设备有没有跟着变。变了 → Qt 会刷新，改代码就能解决；
不变 → Qt 在进程内缓存了设备列表，得换别的办法（比如录音/播放前后重建
多媒体后端，或改用 winmm 通道）。
"""

from __future__ import annotations

import sys
from datetime import datetime

INTERVAL_MS = 2000
TOTAL_SECONDS = 90


def main() -> int:
    try:
        from PySide6.QtCore import QCoreApplication, QTimer
        from PySide6.QtMultimedia import QMediaDevices
    except Exception as exc:  # pragma: no cover
        print(f"导入 PySide6.QtMultimedia 失败：{exc}")
        return 1

    QCoreApplication(sys.argv)
    # 一直活着的实例（模拟桌宠长期持有的那个）
    long_lived = QMediaDevices()
    deadline = datetime.now().timestamp() + TOTAL_SECONDS
    last_in = last_out = ""
    n = 0

    def describe(dev) -> str:
        try:
            return "(空)" if dev.isNull() else dev.description()
        except Exception:
            return "(读不到)"

    def tick() -> None:
        nonlocal n, last_in, last_out
        n += 1
        now = datetime.now()
        if now.timestamp() >= deadline:
            print("\n观察结束。")
            app.quit()
            return
        # 长期实例看到的
        try:
            cur_in = describe(long_lived.defaultAudioInput())
            cur_out = describe(long_lived.defaultAudioOutput())
            n_in = len(long_lived.audioInputs())
            n_out = len(long_lived.audioOutputs())
        except Exception as exc:
            print(f"[{now:%H:%M:%S}] 枚举失败: {exc}")
            return
        # 每次新建的实例看到的（用来对比：新实例是否比老实例"新"）
        try:
            fresh = QMediaDevices()
            fresh_in = describe(fresh.defaultAudioInput())
            fresh_out = describe(fresh.defaultAudioOutput())
            fresh_n_in = len(fresh.audioInputs())
        except Exception:
            fresh_in = fresh_out = "(读不到)"
            fresh_n_in = -1
        changed = (cur_in != last_in) or (cur_out != last_out)
        flag = "  <<<< 变了" if changed and n > 1 else ""
        print(f"[{now:%H:%M:%S}]{flag}")
        print(f"    长期实例  默认输入={cur_in} (共{n_in})  默认输出={cur_out} (共{n_out})")
        print(f"    新建实例  默认输入={fresh_in} (共{fresh_n_in})  默认输出={fresh_out}")
        last_in, last_out = cur_in, cur_out

    app = QCoreApplication.instance()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(INTERVAL_MS)
    print(f"开始观察 {TOTAL_SECONDS} 秒（每 {INTERVAL_MS // 1000} 秒打一次）。")
    print("请在这期间拔掉耳机、过一会儿再插回去。\n")
    tick()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
