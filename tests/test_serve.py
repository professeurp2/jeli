import socket

from app.serve import dual_stack_socket


def test_one_socket_accepts_ipv4_and_ipv6():
    # Railway's public proxy connects over IPv4, its private network (WAHA) over IPv6.
    server = dual_stack_socket(0)
    server.listen()
    port = server.getsockname()[1]
    try:
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            with socket.socket(family, socket.SOCK_STREAM) as client:
                client.connect((host, port))
                accepted, _ = server.accept()
                accepted.close()
    finally:
        server.close()
