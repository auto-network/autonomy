"""auto-8c2df: the egress proxy's policy and forwarding."""

import asyncio
import ipaddress

import pytest

from tools.browser_broker import egress_proxy as proxy


@pytest.mark.parametrize("address,ok", [
    ("93.184.215.14", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True),
    ("10.1.2.3", False), ("172.16.0.1", False), ("172.31.255.255", False), ("192.168.1.10", False),
    ("100.64.0.1", False), ("100.101.102.103", False),   # CGNAT / the tailnet
    ("169.254.169.254", False),                          # cloud metadata
    ("127.0.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fd00::1", False),
    ("::ffff:10.0.0.1", False), ("::ffff:93.184.215.14", True),
    ("not-an-ip", False),
])
def test_address_policy(address, ok):
    assert proxy.address_allowed(address) is ok


def test_parse_targets():
    assert proxy.parse_target("CONNECT", "example.com:443") == ("example.com", 443, "")
    assert proxy.parse_target("CONNECT", "[2606:4700::1]:443") == ("2606:4700::1", 443, "")
    assert proxy.parse_target("GET", "http://example.com/a/b?c=1") == ("example.com", 80, "/a/b?c=1")
    assert proxy.parse_target("GET", "http://example.com:8080") == ("example.com", 8080, "/")
    for method, target in (("CONNECT", "example.com"), ("CONNECT", "example.com:99999"),
                           ("GET", "https://example.com/"), ("GET", "/relative"),
                           ("GET", "http://user:pw@example.com/"), ("GET", "ftp://example.com/")):
        with pytest.raises(ValueError):
            proxy.parse_target(method, target)


def _run(coro):
    return asyncio.run(coro)


async def _with_proxy(subnet, resolve, scenario):
    """Start an upstream echo/HTTP server and the proxy on loopback."""
    seen = {}

    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n") if not seen.get("tunnel") else b""
        seen["request"] = head.decode("latin-1")
        if seen.get("tunnel"):
            data = await reader.read(5)
            writer.write(b"echo:" + data)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    up = await asyncio.start_server(upstream, "127.0.0.1", 0)
    up_port = up.sockets[0].getsockname()[1]
    front_proxy = proxy.Proxy(ipaddress.ip_network(subnet), frozenset({up_port}))
    front = await asyncio.start_server(front_proxy.handle, "127.0.0.1", 0)
    front_port = front.sockets[0].getsockname()[1]

    async def resolved(host, port):
        return resolve(host, port, up_port)

    original = proxy.resolve_checked
    proxy.resolve_checked = resolved
    try:
        return await scenario(front_port, up_port, seen)
    finally:
        proxy.resolve_checked = original
        up.close()
        front.close()


def _public_only(host, port, up_port):
    return ("127.0.0.1", "") if host == "public.example" else (None, "private-destination")


def test_connect_tunnels_to_an_allowed_destination():
    async def scenario(front_port, up_port, seen):
        seen["tunnel"] = True
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        writer.write(f"CONNECT public.example:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        status = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"hello")
        await writer.drain()
        body = await reader.read(100)
        writer.close()
        return status, body

    status, body = _run(_with_proxy("127.0.0.0/8", _public_only, scenario))
    assert status.startswith(b"HTTP/1.1 200") and body == b"echo:hello"


def test_plain_http_is_rewritten_to_origin_form_without_proxy_headers():
    async def scenario(front_port, up_port, seen):
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        writer.write(f"GET http://public.example:{up_port}/a?b=1 HTTP/1.1\r\nHost: public.example\r\n"
                     "Proxy-Authorization: secret\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        response = await reader.read(1000)
        writer.close()
        return response, seen["request"]

    response, request = _run(_with_proxy("127.0.0.0/8", _public_only, scenario))
    assert response.startswith(b"HTTP/1.1 200") and response.endswith(b"ok")
    assert request.startswith("GET /a?b=1 HTTP/1.1\r\n")
    assert "proxy-" not in request.lower() and "Connection: close" in request


def test_private_destinations_are_refused():
    async def scenario(front_port, up_port, seen):
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        writer.write(b"CONNECT nas.lan:445 HTTP/1.1\r\n\r\n")
        response = await reader.read(200)
        writer.close()
        return response, "request" in seen

    response, reached = _run(_with_proxy("127.0.0.0/8", _public_only, scenario))
    assert response.startswith(b"HTTP/1.1 403") and not reached


def test_clients_outside_the_browser_network_are_dropped():
    async def scenario(front_port, up_port, seen):
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        try:
            writer.write(f"CONNECT public.example:{up_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read(200)
        except ConnectionResetError:
            response = b""  # dropped outright
        writer.close()
        return response, "request" in seen

    assert _run(_with_proxy("10.213.0.0/24", _public_only, scenario)) == (b"", False)


def test_the_listen_address_is_the_browser_network_interface():
    subnet = ipaddress.ip_network("10.213.0.0/24")
    assert proxy.address_in(subnet, ["127.0.0.1", "10.213.1.2", "10.213.0.2"]) == "10.213.0.2"
    assert proxy.address_in(subnet, ["127.0.0.1", "172.16.0.9"]) is None



@pytest.mark.parametrize("address,ok", [
    ("64:ff9b::a00:1", False),          # NAT64 of 10.0.0.1
    ("64:ff9b::5db8:d70e", True),       # NAT64 of 93.184.215.14
    ("2002:a00:1::1", False),           # 6to4 of 10.0.0.1
    ("2001:0:4136:e378:8000:63bf:f5ff:fffe", False),  # Teredo of 10.0.0.1
])
def test_ipv4_inside_ipv6_is_judged_as_ipv4(address, ok):
    assert proxy.address_allowed(address) is ok


def test_only_web_ports_by_default():
    assert proxy.port_allowed("CONNECT", 443) and proxy.port_allowed("GET", 80)
    for port in (22, 25, 587, 6667, 8443, 80):
        assert not proxy.port_allowed("CONNECT", port)
    assert not proxy.port_allowed("GET", 443) and not proxy.port_allowed("POST", 25)
    assert proxy.port_allowed("CONNECT", 8443, proxy.extra_ports("8443,x,99999"))
    assert proxy.extra_ports("") == frozenset()


def test_smtp_and_ssh_are_refused_with_reason_port(caplog):
    async def scenario(front_port, up_port, seen):
        results = []
        for target in ("public.example:25", "public.example:22"):
            reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
            writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            results.append(await reader.read(200))
            writer.close()
        return results, "request" in seen

    results, reached = _run(_with_proxy("127.0.0.0/8", _public_only, scenario))
    assert all(r.startswith(b"HTTP/1.1 403") for r in results) and not reached
    assert "reason=port" in caplog.text


def test_one_client_cannot_exhaust_the_proxy():
    async def scenario():
        p = proxy.Proxy(ipaddress.ip_network("127.0.0.0/8"))
        p.open["127.0.0.1"] = proxy.MAX_CONNECTIONS_PER_CLIENT
        server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        response = await reader.read(200)
        writer.close()
        server.close()
        return response

    assert _run(scenario()).startswith(b"HTTP/1.1 503")


def test_resolution_asks_for_ipv4_only(monkeypatch):
    import socket
    seen = {}

    async def scenario():
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(host, port, **kw):
            seen.update(kw)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        return await proxy.resolve_checked("example.com", 443)

    assert _run(scenario()) == ("93.184.215.14", "") and seen["family"] == socket.AF_INET


def test_a_stalled_tunnel_is_closed_at_the_idle_timeout(monkeypatch):
    monkeypatch.setattr(proxy, "IDLE_TIMEOUT_S", 0.3)

    async def scenario(front_port, up_port, seen):
        seen["tunnel"] = True
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        writer.write(f"CONNECT public.example:{up_port} HTTP/1.1\r\n\r\n".encode())
        await reader.readuntil(b"\r\n\r\n")
        # send nothing: the upstream waits for 5 bytes, the tunnel idles
        leftover = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        return leftover

    assert _run(_with_proxy("127.0.0.0/8", _public_only, scenario)) == b""
