"""Hand your Mac's current sign-in to your phone through a SHORT, one-time, 2-minute address.

For testing on your home Wi-Fi only (a long access token is hard to move between devices).

    1. On the Mac: open /login, click "Refresh token", then "Copy access token".
    2. Run:  python scripts/phone_login.py
    3. Paste the token when asked (the input is hidden).
    4. Type the short address it prints into Safari on your phone.

The address works ONCE, expires after 2 minutes, then this script exits. It forwards the phone to
the login page with the session, which signs it in and scrubs the token from the address bar. The
token is never printed or logged. Treat it like a password for the hour it lasts.
"""
import argparse
import getpass
import http.server
import secrets
import socket
import sys
import threading
import time


def lan_ip() -> str:
    """The address other devices on the Wi-Fi use to reach this Mac (no traffic is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts) and 100 < len(token) < 6000 and token.isascii() and " " not in token


def make_server(token: str, ip: str, *, app_port: int = 8000, port: int = 8001, ttl: int = 120):
    """Returns (server, code). The server answers GET /<code> exactly once with a redirect."""
    code = secrets.token_urlsafe(6)
    target = f"http://{ip}:{app_port}/login#access_token={token}"
    state = {"used": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.strip("/") == code and not state["used"]:
                state["used"] = True
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(404)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

        def log_message(self, *args):  # never log paths or tokens
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    expiry = threading.Timer(ttl, server.shutdown)
    expiry.daemon = True  # must never keep the process alive after the address was used
    expiry.start()
    return server, code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--app-port", type=int, default=8000)
    ap.add_argument("--ttl", type=int, default=120, help="seconds before the address stops working")
    args = ap.parse_args()

    token = getpass.getpass("Paste the access token (input hidden), then press Enter: ").strip()
    if not looks_like_jwt(token):
        print("That does not look like an access token. Use 'Copy access token' on /login.", file=sys.stderr)
        return 2

    ip = lan_ip()
    server, code = make_server(token, ip, app_port=args.app_port, port=args.port, ttl=args.ttl)
    print(f"\nOn your phone, in Safari, type:\n\n    http://{ip}:{args.port}/{code}\n")
    print(f"It works once and expires in {args.ttl} seconds. Waiting...")
    started = time.time()
    server.serve_forever()
    server.server_close()
    print("Used." if time.time() - started < args.ttl - 1 else "Expired, nothing was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
