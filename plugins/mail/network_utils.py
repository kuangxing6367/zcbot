import errno
import socket


def open_tcp_socket(
    host: str,
    port: int,
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a TCP socket with IPv4-first fallback for mixed-network hosts."""
    addrinfos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrinfos.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)

    errors: list[OSError] = []
    for family, socktype, proto, _, sockaddr in addrinfos:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            if sock is not None:
                sock.close()

    if not errors:
        raise OSError(f"Unable to resolve {host}:{port}")

    if any(exc.errno == errno.ENETUNREACH for exc in errors):
        raise OSError(
            errno.ENETUNREACH,
            (
                f"Unable to connect to {host}:{port}. The current environment may "
                "lack a usable route for one or more resolved addresses."
            ),
        ) from errors[-1]

    raise errors[-1]
