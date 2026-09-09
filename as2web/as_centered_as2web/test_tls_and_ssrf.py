"""Standalone tests for check_domain.py TLS and internal-network guards.

Hermetic: socket.getaddrinfo is stubbed, so no live DNS is used.

Run with: python3 as2web/as_centered_as2web/test_tls_and_ssrf.py
"""

import socket

import check_domain as m


def _fresh_session():
    m._thread_local.session = None
    return m._session()


def test_tls_strict_by_default():
    m.ALLOW_INVALID_TLS = False
    assert _fresh_session().verify is True


def test_tls_ignored_only_with_flag():
    m.ALLOW_INVALID_TLS = True
    try:
        assert _fresh_session().verify is False
    finally:
        m.ALLOW_INVALID_TLS = False
        m._thread_local.session = None


class _StubDNS:
    """Context manager: replace socket.getaddrinfo with a fixed host -> [ip] map.
    An unlisted host raises socket.gaierror, like a real resolution failure."""

    def __init__(self, mapping):
        self.mapping = mapping
        self._real = None

    def __enter__(self):
        self._real = m.socket.getaddrinfo

        def fake(host, *args, **kwargs):
            if host in self.mapping:
                return [(0, 0, 0, "", (ip, 0)) for ip in self.mapping[host]]
            raise socket.gaierror(f"stub: no record for {host!r}")

        m.socket.getaddrinfo = fake
        return self

    def __exit__(self, *exc):
        m.socket.getaddrinfo = self._real


def test_host_resolves_public_classification():
    cases = {
        "pub.test":      (["93.184.216.34"], True),
        "priv.test":     (["10.0.0.1"], False),
        "loopback.test": (["127.0.0.1"], False),
        "metadata.test": (["169.254.169.254"], False),
        "v6loopback.test": (["::1"], False),
        "v4mapped.test": (["::ffff:127.0.0.1"], False),
        "mixed.test":    (["93.184.216.34", "10.0.0.1"], False),  # any private -> reject
    }
    for host, (ips, expected) in cases.items():
        with _StubDNS({host: ips}):
            assert m._host_resolves_public(host) is expected, (host, ips)
    with _StubDNS({}):
        assert m._host_resolves_public("unresolvable.test") is False
    assert m._host_resolves_public("") is False


def test_streamed_probe_blocks_non_public_host():
    with _StubDNS({"blocked.test": ["10.1.2.3"]}):
        result = m.streamed_get_probe("http://blocked.test/", timeout=1.0)
    assert result == ("unreachable", None, None, "blocked_non_public_host"), result


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} check_domain TLS/SSRF tests passed")
