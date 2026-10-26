# -*- coding: utf-8 -*-
"""``pet/asr/fetch.py`` 的行为回归：续传、断流重试、校验、取消、幂等。

**不起外网**：全部打本地 HTTP 服务（真 socket、真 HTTP、真 Range），因为这里
要锁的四件事没有一件能靠 mock 说明白：

1. **断流要续传**——镜像在 85% 处掐断是实测遇到的（不是防御性假设），补丁每次
   只发一部分就关连接，必须能接着下完；
2. **下完要校验**——半个 239 MB 的模型比没有模型更糟（识别会静默出错），
   所以摘要不对必须删文件并报错；
3. **取消要留 .part**——用户按下取消不该白下 100 MB；
4. **幂等**——已就绪时一个字节都不该再请求。

为了让测试跑得快，文件体积与摘要都按小文件改写（``EXPECTED_SIZE`` /
``EXPECTED_SHA256`` 是模块级字典，用 monkeypatch 替换）——验证的是流程，不是
那三个真实文件的字节数（真实值另有 ``test_asr_fetch_constants`` 与第三方声明对表）。
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pet.asr import fetch


# ---------------------------------------------------------------- 本地服务
class _Server:
    """极小的 Range 服务器：可指定"只发前 N 字节就断"来模拟镜像断流。"""

    def __init__(self):
        self.payloads: dict[str, bytes] = {}
        #: 前 N 次 GET 只发一半就断（模拟镜像掐流），之后正常
        self.truncate_times: int = 0
        self.range_headers: list[str | None] = []
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def _payload(self) -> bytes | None:
                return outer.payloads.get(self.path)

            def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler 命名
                outer.requests.append(f"HEAD {self.path}")
                body = self._payload()
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def do_GET(self):  # noqa: N802
                outer.requests.append(f"GET {self.path}")
                outer.range_headers.append(self.headers.get("Range"))
                body = self._payload()
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                header = self.headers.get("Range")
                if header and header.startswith("bytes="):
                    start = int(header[6:].split("-")[0])
                piece = body[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(piece)))
                if start:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.end_headers()
                if outer.truncate_times > 0:
                    outer.truncate_times -= 1
                    self.wfile.write(piece[: max(1, len(piece) // 2)])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(piece)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = _Server()
    try:
        yield srv
    finally:
        srv.close()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set_expected(monkeypatch, sizes: dict[str, int], digests: dict[str, str]) -> None:
    for name, size in sizes.items():
        monkeypatch.setitem(fetch.EXPECTED_SIZE, name, size)
    for name, digest in digests.items():
        monkeypatch.setitem(fetch.EXPECTED_SHA256, name, digest)


# ---------------------------------------------------------------- 常量对表
def test_expected_constants_cover_every_model_file():
    """三个文件的体积/摘要都要有值，且与 ``MODEL_FILES`` 一一对应。"""
    assert set(fetch.EXPECTED_SHA256) == set(fetch.MODEL_FILES)
    assert set(fetch.EXPECTED_SIZE) == set(fetch.MODEL_FILES)
    assert fetch.TOTAL_BYTES == sum(fetch.EXPECTED_SIZE.values())
    assert len(fetch.EXPECTED_SHA256["model.int8.onnx"]) == 64


def test_human_size():
    assert fetch.human_size(0) == "0.0 B"
    assert fetch.human_size(1024) == "1.0 KB"
    assert fetch.human_size(fetch.TOTAL_BYTES).endswith("MB")


# ---------------------------------------------------------------- 校验
def test_verify_file_reports_missing_and_wrong_size(tmp_path, monkeypatch):
    _set_expected(monkeypatch, {"tokens.txt": 4}, {"tokens.txt": _sha(b"abcd")})
    ok, reason = fetch.verify_file(tmp_path / "tokens.txt", "tokens.txt")
    assert not ok and "不存在" in reason

    (tmp_path / "tokens.txt").write_bytes(b"ab")
    ok, reason = fetch.verify_file(tmp_path / "tokens.txt", "tokens.txt")
    assert not ok and "体积不对" in reason


def test_verify_file_rejects_wrong_checksum_but_quick_mode_passes(tmp_path, monkeypatch):
    """体积对、内容错：严格模式必须拦下，界面用的 quick 模式放行（不读 239 MB）。"""
    _set_expected(monkeypatch, {"tokens.txt": 4}, {"tokens.txt": _sha(b"abcd")})
    (tmp_path / "tokens.txt").write_bytes(b"wxyz")

    strict_ok, reason = fetch.verify_file(tmp_path / "tokens.txt", "tokens.txt")
    assert not strict_ok and "sha256" in reason
    quick_ok, _ = fetch.verify_file(tmp_path / "tokens.txt", "tokens.txt", checksum=False)
    assert quick_ok


def test_quick_states_only_checks_size(tmp_path, monkeypatch):
    _set_expected(monkeypatch, {"tokens.txt": 4}, {"tokens.txt": _sha(b"abcd")})
    (tmp_path / "tokens.txt").write_bytes(b"zzzz")
    states = fetch.quick_states(tmp_path)
    assert states["tokens.txt"][0] is True
    assert states["model.int8.onnx"][0] is False


# ---------------------------------------------------------------- 下载
def test_download_writes_file_and_follows_content_length(server, tmp_path):
    data = b"x" * 400_000
    server.payloads["/m.bin"] = data
    dest = tmp_path / "m.bin"
    done: list[tuple[int, int]] = []

    fetch.download(server.base + "/m.bin", dest, mirrors=("",),
                   progress=lambda got, total: done.append((got, total)))

    assert dest.read_bytes() == data
    assert done and done[-1][0] == len(data)
    assert done[-1][1] == len(data)
    assert not (tmp_path / "m.bin.part").exists()


def test_download_resumes_from_part_file(server, tmp_path):
    """已有 .part 时带 Range 续传，且**不能**从头重写。"""
    data = bytes(range(256)) * 2000  # 512 KB
    server.payloads["/m.bin"] = data
    dest = tmp_path / "m.bin"
    part = tmp_path / "m.bin.part"
    half = len(data) // 2
    part.write_bytes(data[:half])

    fetch.download(server.base + "/m.bin", dest, mirrors=("",))

    assert dest.read_bytes() == data
    assert server.range_headers == [f"bytes={half}-"]


def test_download_retries_after_truncated_response(server, tmp_path):
    """镜像发一半就断：必须自己接着下完，而不是把半个文件当成品。

    这正是实测里 ghproxy 的行为（85% 处掐断），也是本模块存在的理由。
    """
    data = b"y" * 300_000
    server.payloads["/m.bin"] = data
    server.truncate_times = 1  # 只断第一次，模拟"网络恢复后再连就好"
    dest = tmp_path / "m.bin"

    fetch.download(server.base + "/m.bin", dest, mirrors=("",))

    assert dest.read_bytes() == data
    assert any(header and header.startswith("bytes=") for header in server.range_headers)


def test_download_cancel_keeps_part_file(server, tmp_path):
    data = b"z" * 5_000_000
    server.payloads["/m.bin"] = data
    token = fetch.CancelToken()
    dest = tmp_path / "m.bin"

    def progress(got, total):
        token.cancel()

    with pytest.raises(fetch.FetchCancelled):
        fetch.download(server.base + "/m.bin", dest, mirrors=("",),
                       progress=progress, cancel=token)

    assert not dest.exists()
    assert (tmp_path / "m.bin.part").exists()  # 已下的部分保住


def test_download_reports_all_sources_unreachable(tmp_path):
    with pytest.raises(fetch.FetchError):
        fetch.download("http://127.0.0.1:9/never", tmp_path / "x.bin",
                       mirrors=("",), attempts=1)


# ---------------------------------------------------------------- 整流程
def _archive_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _serve_full_release(server, monkeypatch, *, model=b"MODEL-INT8", tokens=b"TOKENS",
                        vad=b"VAD") -> dict[str, bytes]:
    """把两个下载地址都指向本地服务，并让小文件的摘要与之匹配。"""
    archive = _archive_bytes({"sensevoice/model.int8.onnx": model,
                              "sensevoice/tokens.txt": tokens})
    server.payloads[f"/{fetch.SENSEVOICE_ARCHIVE}"] = archive
    server.payloads[f"/{fetch.VAD_FILE}"] = vad
    monkeypatch.setattr(fetch, "RELEASE_BASE", server.base + "/")
    _set_expected(
        monkeypatch,
        {"model.int8.onnx": len(model), "tokens.txt": len(tokens),
         "silero_vad.onnx": len(vad)},
        {"model.int8.onnx": _sha(model), "tokens.txt": _sha(tokens),
         "silero_vad.onnx": _sha(vad)},
    )
    return {"model.int8.onnx": model, "tokens.txt": tokens, "silero_vad.onnx": vad}


def test_fetch_models_end_to_end(server, tmp_path, monkeypatch):
    files = _serve_full_release(server, monkeypatch)
    notes: list[str] = []

    downloaded = fetch.fetch_models(tmp_path, mirrors=("",), notify=notes.append)

    assert sorted(downloaded) == sorted(files)
    for name, data in files.items():
        assert (tmp_path / name).read_bytes() == data
    assert not (tmp_path / fetch.SENSEVOICE_ARCHIVE).exists()  # 压缩包默认删掉
    assert any("校验通过" in note for note in notes)


def test_fetch_models_is_idempotent(server, tmp_path, monkeypatch):
    """已就绪时不该再发任何请求（重复点下载按钮不能白下 155 MB）。"""
    files = _serve_full_release(server, monkeypatch)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    server.requests.clear()

    downloaded = fetch.fetch_models(tmp_path, mirrors=("",))

    assert downloaded == []
    assert server.requests == []


def test_fetch_models_deletes_file_that_fails_checksum(server, tmp_path, monkeypatch):
    """下完了但内容不对：删掉坏文件并报错，绝不留在目录里让识别静默出错。"""
    _serve_full_release(server, monkeypatch)
    monkeypatch.setitem(fetch.EXPECTED_SHA256, "silero_vad.onnx", "0" * 64)

    with pytest.raises(fetch.FetchError) as excinfo:
        fetch.fetch_models(tmp_path, mirrors=("",))

    assert "校验不过" in str(excinfo.value)
    assert not (tmp_path / "silero_vad.onnx").exists()
    # 校验通过的那两个要留下（别把好的也删了）
    assert (tmp_path / "model.int8.onnx").exists()
    assert (tmp_path / "tokens.txt").exists()


def test_fetch_models_keeps_archive_when_asked(server, tmp_path, monkeypatch):
    _serve_full_release(server, monkeypatch)
    fetch.fetch_models(tmp_path, mirrors=("",), keep_archive=True)
    assert (tmp_path / fetch.SENSEVOICE_ARCHIVE).is_file()


def test_fetch_models_reports_disk_shortage(tmp_path, monkeypatch):
    """空间不够要在下载之前说清楚，不能下到一半才失败。"""
    monkeypatch.setattr(fetch.shutil, "disk_usage",
                        lambda _p: type("U", (), {"free": 1024})(), raising=True)
    with pytest.raises(fetch.FetchError) as excinfo:
        fetch.fetch_models(tmp_path)
    assert "磁盘空间不足" in str(excinfo.value)
    assert not list(tmp_path.glob("*.part"))
