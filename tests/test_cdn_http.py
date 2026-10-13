"""Characterize CDN framing and stream cleanup without opening a listener."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from acvd_server.server import AcvdServer


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
        self.drained = False

    def get_extra_info(self, name):
        assert name == "peername"
        return ("127.0.0.1", 12345)

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.drained = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        assert self.closed


def harness(tmp_path, installed=True):
    path = tmp_path / "contents_101.bin"
    if installed:
        path.write_bytes(b"regulation\x00\xff")
    events = []
    server = SimpleNamespace(
        config=SimpleNamespace(regulation_path=path),
        regulation_downloads=SimpleNamespace(get=lambda target: path.read_bytes()),
        _stream_writers=set(),
        telemetry=SimpleNamespace(emit=lambda event, **data: events.append((event, data))),
    )
    return server, events


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "target,installed,status,reason,body",
    [
        ("/regulation/contents_101.bin", True, 200, "OK", b"regulation\x00\xff"),
        ("/regulation/contents_101.bin?x=1", True, 200, "OK", b"regulation\x00\xff"),
        ("/regulation/regulation.bnd", True, 200, "OK", b"regulation\x00\xff"),
        ("/regulation/../private.pem", True, 404, "Not Found", b"not found\n"),
        ("/missing", True, 404, "Not Found", b"not found\n"),
        (
            "/regulation/contents_101.bin",
            False,
            503,
            "Service Unavailable",
            b"regulation file not installed\n",
        ),
    ],
)
async def test_cdn_response_contract(tmp_path, method, target, installed, status, reason, body):
    server, events = harness(tmp_path, installed)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(f"{method} {target} HTTP/1.1\r\nHost: example\r\n\r\n".encode())
    reader.feed_eof()
    await AcvdServer._handle_cdn(server, reader, writer)
    expected = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/octet-stream\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + (b"" if method == "HEAD" else body)
    assert writer.data == expected
    assert writer.drained and writer.closed and not server._stream_writers
    assert events == [
        (
            "cdn_request",
            {
                "peer": ("127.0.0.1", 12345),
                "method": method,
                "target": target,
                "status": status,
            },
        )
    ]


@pytest.mark.parametrize("raw_request", [b"", b"GET\r\n", b"GET / HTTP/1.1 extra\r\n"])
async def test_malformed_request_closes_without_response(tmp_path, raw_request):
    server, events = harness(tmp_path)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(raw_request)
    reader.feed_eof()
    await AcvdServer._handle_cdn(server, reader, writer)
    assert writer.data == b"" and writer.closed and not writer.drained
    assert not server._stream_writers and events[0][1]["status"] == 400


async def test_method_rejection_precedes_file_selection(tmp_path):
    server, events = harness(tmp_path, installed=False)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b"POST /missing HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    await AcvdServer._handle_cdn(server, reader, writer)
    assert writer.data.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n")
    assert writer.data.endswith(b"method not allowed\n")
    assert events[0][1]["status"] == 405


async def test_file_read_failure_returns_service_unavailable_and_closes(tmp_path, monkeypatch):
    server, events = harness(tmp_path)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b"GET /regulation/contents_101.bin HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    def fail_read(path):
        raise OSError("read failed")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    await AcvdServer._handle_cdn(server, reader, writer)
    assert writer.closed and not server._stream_writers
    assert writer.data.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    assert events[-1][1]["status"] == 503
