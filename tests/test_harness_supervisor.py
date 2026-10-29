# -*- coding: utf-8 -*-
"""dsh 常驻看护（supervisor）与 spawn 日志落盘 / 早退回报测试。

背景（2026-10-29 吞错 bug）：``_spawn`` 过去把子进程输出全进 DEVNULL，dsh 启动
失败（凭据坏/插件崩/端口冲突）全程静默，GUI 只会一直说「正在后台启动……」。
本文件覆盖修复后的三条链路：

1. 输出落盘（带时间戳日志 + 保留上限）；
2. 进程早退回报（"exited" 事件；主动终止被抑制）；
3. 常驻看护 tick（alive/listening/gated/stopped/backoff/restarted/failed）
   与用户停止闩、退避计数、看护线程生命周期。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from pet import harness_launcher as hl


class _FakeProc:
    """subprocess.Popen 替身：pid + 存活态 + 可等待（退出监视线程需要 wait）。"""

    def __init__(self, pid: int, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return self._exit


class _BlockingProc(_FakeProc):
    """wait() 阻塞到放行的替身：消除退出监视线程与测试断言的时序竞态。"""

    def __init__(self, pid: int):
        super().__init__(pid)
        self._release = threading.Event()

    def wait(self, timeout=None):
        self._release.wait(2.0)
        return self._exit


@pytest.fixture(autouse=True)
def _clean_module_state():
    """模块级状态逐用例复位（登记表/闩/退避/监听者），防用例间串味。"""
    hl._SELF_LAUNCHED_PIDS.clear()
    hl._SELF_LAUNCHED_KIND.clear()
    hl._LAUNCHED_CHILDREN.clear()
    with hl._EXIT_NOTIFY_SUPPRESSED_LOCK:
        hl._EXIT_NOTIFY_SUPPRESSED.clear()
    with hl._OWNERSHIP_LOCK:
        hl._USER_STOPPED = False
    hl._SUPERVISOR_FAILURES = 0
    hl._NEXT_RESTART_AT = 0.0
    with hl._STATUS_LISTENERS_LOCK:
        hl._STATUS_LISTENERS.clear()
    hl._SUPERVISOR_STOP.set()  # 保险：别让上一条用例的看护线程在本文件里空转
    yield
    hl._SELF_LAUNCHED_PIDS.clear()
    hl._SELF_LAUNCHED_KIND.clear()
    hl._LAUNCHED_CHILDREN.clear()
    with hl._EXIT_NOTIFY_SUPPRESSED_LOCK:
        hl._EXIT_NOTIFY_SUPPRESSED.clear()
    with hl._OWNERSHIP_LOCK:
        hl._USER_STOPPED = False
    hl._SUPERVISOR_FAILURES = 0
    hl._NEXT_RESTART_AT = 0.0
    with hl._STATUS_LISTENERS_LOCK:
        hl._STATUS_LISTENERS.clear()
    hl._SUPERVISOR_STOP.set()


class _Recorder:
    """事件监听记录器。"""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def __call__(self, event: str, info: str = "") -> None:
        self.events.append((event, info))


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------- 日志落盘


def test_new_spawn_log_creates_timestamped_file_and_prunes(tmp_path, monkeypatch):
    """每次 spawn 一份带时间戳的日志；超过保留上限的旧文件被清理。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    monkeypatch.setattr(hl, "_spawn_log_dir", lambda: log_dir)
    for i in range(7):
        (log_dir / f"dsh-web-20260101-0000{i:02d}.log").write_text("old", encoding="utf-8")

    path = hl._new_spawn_log()

    assert path is not None and path.parent == log_dir
    assert path.name.startswith("dsh-web-2") and path.suffix == ".log"
    remaining = sorted(log_dir.glob("dsh-web-*.log"))
    assert len(remaining) == hl._SPAWN_LOG_KEEP
    # 新文件在列，且删的是最旧的
    assert path in remaining
    assert "dsh-web-20260101-000000.log" not in [p.name for p in remaining]


def test_spawn_writes_output_to_log_file(tmp_path, monkeypatch):
    """_spawn 的 stdout/stderr 落到日志文件（不再是 DEVNULL），并挂退出监视线程。"""
    log_path = tmp_path / "logs" / "dsh-web-test.log"
    log_path.parent.mkdir(parents=True)
    monkeypatch.setattr(hl, "_new_spawn_log", lambda: log_path)
    captured = {}

    def fake_popen(command, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc(4242)

    monkeypatch.setattr(hl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(hl, "_POPEN_TYPE", _FakeProc)  # 让替身走登记分支
    recorder = _Recorder()
    hl.add_status_listener(recorder)

    proc = hl._spawn(["dsh", "web"])

    assert proc is not None
    kwargs = captured["kwargs"]
    assert kwargs["stdout"] is not hl.subprocess.DEVNULL
    assert kwargs["stderr"] is kwargs["stdout"]
    # 父进程侧句柄已关闭（子进程已继承），文件本体存在
    assert log_path.exists()
    assert hl._SELF_LAUNCHED_PIDS == [4242]
    # 替身进程立即退出且未被抑制：退出监视线程应广播 "exited"（含日志路径）
    assert _wait_for(lambda: ("exited", str(log_path)) in recorder.events), recorder.events


def test_spawn_log_unavailable_falls_back_to_devnull(tmp_path, monkeypatch):
    """日志开不出来不是失败理由：退回 DEVNULL 照常拉起。"""
    monkeypatch.setattr(hl, "_new_spawn_log", lambda: None)
    captured = {}

    def fake_popen(command, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc(4242)

    monkeypatch.setattr(hl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(hl, "_POPEN_TYPE", _FakeProc)

    assert hl._spawn(["dsh", "web"]) is not None
    assert captured["kwargs"]["stdout"] is hl.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is hl.subprocess.DEVNULL


def test_exit_notify_suppressed_after_deliberate_stop(tmp_path, monkeypatch):
    """主动终止（托盘停止/收口）杀掉的进程不得报 "exited"——那不是意外退出。

    用阻塞替身消竞态：监视线程的 wait() 在我们放行前不返回，抑制标记
    一定先于死亡判定落位。
    """
    log_path = tmp_path / "logs" / "dsh-web-test.log"
    log_path.parent.mkdir(parents=True)
    monkeypatch.setattr(hl, "_new_spawn_log", lambda: log_path)
    proc = _BlockingProc(4242)
    monkeypatch.setattr(hl.subprocess, "Popen", lambda command, **kwargs: proc)
    monkeypatch.setattr(hl, "_POPEN_TYPE", _FakeProc)
    recorder = _Recorder()
    hl.add_status_listener(recorder)

    assert hl._spawn(["dsh", "web"]) is proc
    time.sleep(0.1)  # 监视线程进入 wait（阻塞中）
    hl._suppress_exit_notify(proc.pid)
    proc._release.set()
    time.sleep(0.3)  # 给监视线程跑完的机会

    assert not any(event == "exited" for event, _ in recorder.events), recorder.events


# ---------------------------------------------------------------- 看护 tick


def _register_live_web_child(pid: int = 4242) -> _FakeProc:
    proc = _FakeProc(pid)
    hl._LAUNCHED_CHILDREN.append(proc)
    hl._SELF_LAUNCHED_PIDS.append(pid)
    hl._SELF_LAUNCHED_KIND[pid] = "web"
    return proc


def test_tick_alive_resets_backoff(monkeypatch):
    _register_live_web_child()
    hl._SUPERVISOR_FAILURES = 3
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)

    assert hl._supervisor_tick(lambda: True) == "alive"
    assert hl._SUPERVISOR_FAILURES == 0


def test_tick_listening_does_not_double_launch(monkeypatch):
    """端口有人监听（用户自己的实例/别的 dsh）：看护绝不重复拉起。"""
    monkeypatch.setattr(hl, "is_running", lambda port=None: True)
    launched = []
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: launched.append(kwargs) or ("started", "x"))

    assert hl._supervisor_tick(lambda: True) == "listening"
    assert launched == []


def test_tick_gated_when_wanted_false(monkeypatch):
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    launched = []
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: launched.append(kwargs) or ("started", "x"))

    assert hl._supervisor_tick(lambda: False) == "gated"
    assert launched == []


def test_tick_respects_user_stop_latch(monkeypatch):
    """用户主动停止过的实例：门开着也不复活，直到显式启动清闩。"""
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    launched = []
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: launched.append(kwargs) or ("started", "x"))
    hl._mark_user_stop()

    assert hl._supervisor_tick(lambda: True) == "stopped"
    assert launched == []

    hl._clear_user_stop()  # 显式启动（launch_harness 成功 spawn 后清闩）
    assert hl._supervisor_tick(lambda: True) == "restarted"
    assert launched, "清闩后必须恢复重启职责"


def test_tick_backoff_window_skips_restart(monkeypatch):
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    hl._NEXT_RESTART_AT = time.monotonic() + 60
    launched = []
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: launched.append(kwargs) or ("started", "x"))

    assert hl._supervisor_tick(lambda: True) == "backoff"
    assert launched == []


def test_tick_restart_uses_service_target(monkeypatch):
    """重启走 headless service 目标：看护救活的是服务，不是替用户开界面。"""
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    calls = []
    monkeypatch.setattr(
        hl, "launch_harness",
        lambda **kwargs: calls.append(kwargs) or ("started", "http://127.0.0.1:38080"))
    recorder = _Recorder()
    hl.add_status_listener(recorder)

    assert hl._supervisor_tick(lambda: True) == "restarted"
    assert len(calls) == 1
    assert calls[0]["target"] == "service"
    assert calls[0]["open_browser"] is False
    assert ("supervisor-restarted", "http://127.0.0.1:38080") in recorder.events


def test_tick_failure_increases_backoff_and_notifies(monkeypatch):
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: ("not-found", "http://x"))
    recorder = _Recorder()
    hl.add_status_listener(recorder)

    assert hl._supervisor_tick(lambda: True) == "failed"
    assert hl._SUPERVISOR_FAILURES == 1
    assert hl._NEXT_RESTART_AT > time.monotonic()
    assert recorder.events and recorder.events[0][0] == "supervisor-failed"

    # 二连失败：退避按 2 的幂增长（有上限）。手动把退避窗口清零模拟
    # 「退避期已过」（看护 tick 在窗口内只返回 backoff，不会走到重启分支）。
    first_delay = hl._NEXT_RESTART_AT - time.monotonic()
    hl._NEXT_RESTART_AT = 0.0
    assert hl._supervisor_tick(lambda: True) == "failed"
    second_delay = hl._NEXT_RESTART_AT - time.monotonic()
    assert second_delay > first_delay
    assert hl._SUPERVISOR_FAILURES == 2
    for _ in range(10):  # 顶到上限后不再翻倍
        hl._NEXT_RESTART_AT = 0.0
        hl._supervisor_tick(lambda: True)
    assert hl._SUPERVISOR_FAILURES == 12
    assert hl._NEXT_RESTART_AT - time.monotonic() <= hl._SUPERVISOR_RESTART_MAX_SECONDS + 1


def test_tick_abort_is_not_failure(monkeypatch):
    """cancel_check 命中（门在慢探测期间翻转）不是失败：不累计退避。"""
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: ("aborted", "http://x"))

    assert hl._supervisor_tick(lambda: True) == "gated"
    assert hl._SUPERVISOR_FAILURES == 0


def test_tick_ignores_desktop_registry_entries(monkeypatch):
    """desktop 登记项不在看护职责内（用户主力应用，崩溃也不归我们救）。"""
    proc = _FakeProc(4242)
    hl._LAUNCHED_CHILDREN.append(proc)
    hl._SELF_LAUNCHED_PIDS.append(4242)
    hl._SELF_LAUNCHED_KIND[4242] = "desktop"
    monkeypatch.setattr(hl, "is_running", lambda port=None: False)
    monkeypatch.setattr(hl, "launch_harness", lambda **kwargs: ("started", "x"))

    assert hl._supervisor_tick(lambda: True) == "restarted"


# ---------------------------------------------------------------- 生命周期/闩


def test_start_supervisor_idempotent_and_stoppable():
    recorder = _Recorder()
    assert hl.start_supervisor(lambda: False, interval=0.05) is True
    assert hl.start_supervisor(lambda: False, interval=0.05) is False  # 单例
    hl.stop_supervisor()
    assert hl._SUPERVISOR_STOP.is_set()


def test_stop_self_launched_marks_user_stop(monkeypatch):
    """收口终止成功 → 用户停止闩置位（联动关闭/退出后看护不得复活）。"""
    proc = _register_live_web_child()
    terminated: list[int] = []

    def _terminate(pid, proc=None) -> bool:
        terminated.append(pid)
        proc._exit = 0
        return True

    monkeypatch.setattr(hl, "is_running_pid", lambda pid: pid == 4242)
    monkeypatch.setattr(hl, "process_command_line", lambda pid: "dsh web --port 38080")
    monkeypatch.setattr(hl, "_pid_image_path", lambda pid: "node.exe")
    monkeypatch.setattr(hl, "_terminate_process_tree", _terminate)

    assert hl.stop_self_launched_harness() == [4242]
    assert terminated == [4242]
    with hl._OWNERSHIP_LOCK:
        assert hl._USER_STOPPED is True
