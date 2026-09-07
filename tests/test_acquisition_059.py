from __future__ import annotations

import socket
import ssl

import pytest

import aigineering.business.acquisition as acquisition


class _Response:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "text/plain"}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, _limit=-1):
        return self.body


class _Connection:
    responses = []
    seen = []

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.response = self.responses.pop(0)

    def request(self, method, target, headers):
        self.seen.append((self.host, method, target, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def _fetch(monkeypatch, responses, url="https://public.example/a", **kwargs):
    _Connection.responses = list(responses)
    _Connection.seen = []
    monkeypatch.setattr(acquisition, "_PinnedHTTPS", _Connection)
    return acquisition.fetch_public(url, allowed_hosts=("public.example",), **kwargs)


@pytest.mark.parametrize(
    "response, message, max_bytes",
    [
        (_Response(status=404), "HTTP status 404", 4),
        (_Response(body=b"12345"), "exceeds byte limit", 4),
        (_Response(body=b"short", headers={"Content-Length": "10"}), "truncated", 20),
    ],
)
def test_fetch_public_rejects_status_and_response_bounds(
    monkeypatch, response, message, max_bytes
):
    with pytest.raises(ValueError, match=message):
        _fetch(
            monkeypatch,
            [response],
            url="https://public.example/a",
            max_bytes=max_bytes,
        )


def test_fetch_public_accepts_in_scope_redirect_and_records_hops(monkeypatch):
    data, receipt = _fetch(
        monkeypatch,
        [
            _Response(status=302, headers={"Location": "/next"}),
            _Response(body=b"done", headers={"Content-Type": "text/plain"}),
        ],
    )
    assert data == b"done"
    assert receipt["requested_uri"] == "https://public.example/a"
    assert receipt["source_uri"] == "https://public.example/next"
    assert [hop["status"] for hop in receipt["hops"]] == [302, 200]


def test_fetch_public_rejects_out_of_scope_redirect_and_unsafe_urls(monkeypatch):
    with pytest.raises(ValueError, match="outside explicit allowed_hosts"):
        _fetch(
            monkeypatch,
            [_Response(status=302, headers={"Location": "https://other.example/x"})],
        )
    for url in (
        "https://127.0.0.1/private",
        "https://localhost/private",
        "https://user:password@public.example/a",
        "https://public.example/a?api_key=secret",
    ):
        with pytest.raises(ValueError):
            _fetch(monkeypatch, [_Response()], url=url)


class _Socket:
    def __init__(self, *args):
        self.args = args
        self.connected = None
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.connected = address

    def close(self):
        self.closed = True


class _Context:
    def __init__(self):
        self.calls = []
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True

    def wrap_socket(self, sock, server_hostname):
        self.calls.append((sock, server_hostname))
        return (sock, server_hostname)


def _address(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


def test_pinned_https_rejects_private_and_mixed_dns_answers(monkeypatch):
    monkeypatch.setattr(
        acquisition.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("127.0.0.1")],
    )
    with pytest.raises(ValueError, match="non-public"):
        acquisition._PinnedHTTPS("public.example", context=_Context()).connect()

    monkeypatch.setattr(
        acquisition.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("93.184.216.34"), _address("10.0.0.4")],
    )
    with pytest.raises(ValueError, match="non-public"):
        acquisition._PinnedHTTPS("public.example", context=_Context()).connect()


def test_pinned_https_connects_to_selected_ip_with_sni_without_second_lookup(
    monkeypatch,
):
    lookups = []
    sockets = []
    context = _Context()
    selected = _address("93.184.216.34")

    def getaddrinfo(*args, **kwargs):
        lookups.append((args, kwargs))
        return [selected, _address("93.184.216.35")]

    def make_socket(*args):
        value = _Socket(*args)
        sockets.append(value)
        return value

    monkeypatch.setattr(acquisition.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(acquisition.socket, "socket", make_socket)
    connection = acquisition._PinnedHTTPS("public.example", timeout=7, context=context)
    connection.connect()

    assert len(lookups) == 1
    assert lookups[0][1]["type"] == socket.SOCK_STREAM
    assert sockets[0].args == selected[:3]
    assert sockets[0].connected == selected[4]
    assert sockets[0].timeout == 7
    assert context.calls == [(sockets[0], "public.example")]
