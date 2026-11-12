"""Blocco a istanza singola: evita due bot che fanno polling sullo stesso token."""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator

PORTA = 45999


@contextmanager
def lock(port: int = PORTA) -> Iterator[bool]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    except OSError:
        pass
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        yield True
    except OSError:
        yield False
    finally:
        try:
            sock.close()
        except OSError:
            pass
