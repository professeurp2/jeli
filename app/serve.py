"""Production entry point: `python -m app.serve`.

Jeli is reached over IPv4 (Railway's public proxy: /health, /dashboard) and over IPv6 (Railway's
private network: WAHA's webhooks). `uvicorn --host ::` listens on IPv6 only (asyncio sets
IPV6_V6ONLY), `--host 0.0.0.0` on IPv4 only, so Jeli serves one dual-stack socket instead.
"""

import os
import socket

import uvicorn


def dual_stack_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    return sock


def main() -> None:
    sock = dual_stack_socket(int(os.environ.get("PORT", "8000")))
    uvicorn.Server(uvicorn.Config("app.main:app")).run(sockets=[sock])


if __name__ == "__main__":
    main()
