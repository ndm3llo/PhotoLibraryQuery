"""The phone-login helper hands out a session once, and only through its secret short path."""
import importlib.util
import threading
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location("phone_login", Path(__file__).resolve().parent.parent / "scripts" / "phone_login.py")
phone_login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone_login)

TOKEN = "aaaa." + "b" * 200 + ".cccc"


def start(port, ttl=30):
    server, code = phone_login.make_server(TOKEN, "10.0.0.7", port=port, ttl=ttl)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, code, thread


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_redirects_once_then_shuts_down():
    port = free_port()
    server, code, thread = start(port)
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/wrong", follow_redirects=False).status_code == 404
    assert httpx.get(f"{base}/", follow_redirects=False).status_code == 404

    r = httpx.get(f"{base}/{code}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"http://10.0.0.7:8000/login#access_token={TOKEN}"
    assert r.headers["cache-control"] == "no-store"
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server stops after the first use"
    server.server_close()


def test_code_is_short_and_random():
    codes = {phone_login.make_server(TOKEN, "10.0.0.7", port=free_port(), ttl=1)[1] for _ in range(5)}
    assert len(codes) == 5 and all(len(c) <= 10 for c in codes)


def test_token_shape_check():
    assert phone_login.looks_like_jwt(TOKEN)
    for bad in ["", "abc", "a.b", "a.b.c", "has space." + "b" * 200 + ".c", "x" * 7000]:
        assert not phone_login.looks_like_jwt(bad)
