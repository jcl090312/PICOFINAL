"""Login transport regression checks with a MicroPython-like stream contract.

Unlike the older Writer mock, write() here can immediately send only part of
its input, buffer the rest, and meet a temporarily non-writable socket.
The same stream object supplies reader and writer, as MicroPython does.
This is a CPython transport model, NOT an RP2 hardware/network qualification.

Reference contracts checked during investigation:
https://docs.micropython.org/en/latest/library/asyncio.html
https://github.com/micropython/micropython/blob/master/extmod/asyncio/stream.py
"""

import asyncio
import hashlib
import json
import sys
import types
import unittest
from unittest.mock import patch


with patch.dict(sys.modules, {"admin_config": types.ModuleType("admin_config")}):
    from test_runtime import firmware


PASSWORD = "transport-test-only-암호"
SALT = bytes(range(16))
SETTINGS = {
    "PASSWORD_SALT_HEX": SALT.hex(),
    "PASSWORD_HASH_HEX": hashlib.sha256(SALT + PASSWORD.encode("utf-8")).hexdigest(),
}


class PartialSocket:
    """Nonblocking socket behavior: short read/write and temporary None."""

    def __init__(self, read_sizes=(7, 1, 31, 256), write_sizes=(3, None, 17, 61), blocked=False):
        self.incoming = bytearray()
        self.sent = bytearray()
        self.read_sizes = read_sizes
        self.write_sizes = write_sizes
        self.blocked = blocked
        self.closed = False
        self.read_calls = 0
        self.write_calls = 0
        self.ready = asyncio.Event()

    def feed(self, data):
        self.incoming.extend(data)
        self.ready.set()

    async def readable(self):
        await asyncio.sleep(0)
        await self.ready.wait()

    async def writable(self):
        if self.blocked:
            await asyncio.Future()
        await asyncio.sleep(0)

    def read(self, maximum):
        if not self.incoming:
            return b"" if self.closed else None
        allowance = self.read_sizes[self.read_calls % len(self.read_sizes)]
        self.read_calls += 1
        count = min(maximum, allowance, len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        if not self.incoming:
            self.ready.clear()
        return data

    def write(self, data):
        if self.blocked:
            return None
        allowance = self.write_sizes[self.write_calls % len(self.write_sizes)]
        self.write_calls += 1
        if allowance is None:
            return None
        count = min(allowance, len(data))
        self.sent.extend(data[:count])
        return count

    def close(self):
        self.closed = True
        self.ready.set()


class MicroPythonLikeStream:
    def __init__(self, sock):
        self.sock = sock
        self.pending = b""

    async def read(self, count):
        while True:
            await self.sock.readable()
            chunk = self.sock.read(count)
            if chunk is not None:
                return chunk

    def write(self, data):
        remainder = data
        if not self.pending:
            accepted = self.sock.write(data)
            if accepted is not None:
                remainder = data[accepted:]
        self.pending += remainder

    async def drain(self):
        if not self.pending:
            await asyncio.sleep(0)
        while self.pending:
            await self.sock.writable()
            accepted = self.sock.write(self.pending)
            if accepted is not None:
                self.pending = self.pending[accepted:]

    def close(self):
        # MicroPython's current Stream closes its socket in wait_closed().
        pass

    async def wait_closed(self):
        self.sock.close()


def request(path="/admin/login", password=PASSWORD, body=None, pad=0):
    if path == "/data":
        return b"GET /data HTTP/1.1\r\nHost: pico.local\r\n\r\n"
    if body is None:
        body = json.dumps({"password": password}, ensure_ascii=False).encode("utf-8")
    lines = [b"POST /admin/login HTTP/1.1", b"Host: pico.local",
             b"Origin: http://pico.local", b"Content-Type: application/json",
             b"Content-Length: " + str(len(body)).encode("ascii")]
    if pad:
        lines.append(b"X-Padding: " + b"x" * pad)
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


class LoginTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patch = patch.multiple(
            firmware.C, WARMUP_SECONDS=0, SAMPLE_INTERVAL_MS=100,
            STALE_SAMPLE_MS=300, CONSOLE_LOG=False, SIMULATION=False,
            HTTP_MAX_CLIENTS=2, HTTP_HEADER_TIMEOUT_MS=100,
            HTTP_WRITE_TIMEOUT_MS=100,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with patch.object(firmware, "ADMIN_SETTINGS", None):
            self.system = firmware.System()
        self.nonce = 0

        def random_bytes(length):
            self.nonce += 1
            return bytes([self.nonce]) * length

        self.system.admin = firmware.AdminController(SETTINGS, random_bytes=random_bytes)
        self.system.model.update(10000, self.system.clock.ms())
        self.system.last_sample_ms = self.system.clock.ms()

    async def serve(self, wire, **socket_options):
        sock = PartialSocket(**socket_options)
        sock.feed(wire)
        stream = MicroPythonLikeStream(sock)
        await self.system.handle_client(stream, stream)
        self.assertTrue(sock.closed)
        return sock

    def response(self, sock):
        data = bytes(sock.sent)
        self.assertIn(b"\r\n\r\n", data)
        header, body = data.split(b"\r\n\r\n", 1)
        fields = dict(line.lower().split(b":", 1) for line in header.split(b"\r\n")[1:])
        self.assertEqual(int(fields[b"content-length"]), len(body))
        return int(header.split(b" ")[1]), json.loads(body)

    async def test_coalesced_headers_and_body_survive_partial_writes(self):
        sock = await self.serve(request(), read_sizes=(256,))
        code, data = self.response(sock)
        self.assertEqual(code, 200)
        self.assertEqual(len(data["token"]), 48)
        self.assertGreater(sock.write_calls, 3)
        self.assertEqual(self.system.active_clients, 0)

    async def test_header_delimiter_at_read_boundaries_preserves_body(self):
        base = request()
        header_size = base.index(b"\r\n\r\n") + 4
        for boundary in (253, 255, 256, 257, 511, 512):
            # Added field contributes its name, colon, spaces and terminal CRLF.
            padding = boundary - header_size - len(b"X-Padding: \r\n")
            wire = request(pad=padding)
            self.assertEqual(wire.index(b"\r\n\r\n") + 4, boundary)
            sock = await self.serve(wire, read_sizes=(256,))
            self.assertEqual(self.response(sock)[0], 200)

    async def test_delayed_utf8_body_can_arrive_in_single_byte_fragments(self):
        wire = request()
        split = wire.index(b"\r\n\r\n") + 4
        sock = PartialSocket()
        sock.feed(wire[:split])
        stream = MicroPythonLikeStream(sock)
        task = asyncio.create_task(self.system.handle_client(stream, stream))
        try:
            await asyncio.sleep(0.005)
            for byte in wire[split:]:
                await asyncio.sleep(0)
                sock.feed(bytes([byte]))
            await asyncio.wait_for(task, 0.4)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.response(sock)[0], 200)
        self.assertEqual(self.system.active_clients, 0)

    async def test_incomplete_post_times_out_without_stopping_data_or_sampling(self):
        wire = request(body=b"{" * 100)
        split = wire.index(b"\r\n\r\n") + 4
        sock = PartialSocket()
        sock.feed(wire[:split] + b"{")
        stream = MicroPythonLikeStream(sock)
        sensor = asyncio.create_task(self.system.sensor_loop())
        stalled = asyncio.create_task(self.system.handle_client(stream, stream))
        try:
            await asyncio.sleep(0.02)
            healthy = await self.serve(request("/data"))
            self.assertEqual(self.response(healthy)[0], 200)
            await asyncio.wait_for(stalled, 0.5)
            await asyncio.sleep(0.04)
            self.assertGreaterEqual(self.system.model.total_samples, 2)
            self.assertEqual(self.system.active_clients, 0)
            recovered = await self.serve(request())
            self.assertEqual(self.response(recovered)[0], 200)
        finally:
            sensor.cancel()
            stalled.cancel()
            await asyncio.gather(sensor, stalled, return_exceptions=True)

    async def test_full_client_slots_recover_after_stalled_posts(self):
        wire = request(body=b"{" * 100)
        split = wire.index(b"\r\n\r\n") + 4
        stalled = []
        for _ in range(2):
            sock = PartialSocket()
            sock.feed(wire[:split])
            stream = MicroPythonLikeStream(sock)
            stalled.append(asyncio.create_task(self.system.handle_client(stream, stream)))
        try:
            await asyncio.sleep(0.02)
            self.assertEqual(self.system.active_clients, 2)
            overflow = await self.serve(request())
            # The original firmware closed excess clients; a busy response is
            # also valid. Neither path may create an authenticated session.
            if overflow.sent:
                self.assertEqual(self.response(overflow)[0], 503)
            self.assertIsNone(self.system.admin._token)
            await asyncio.wait_for(asyncio.gather(*stalled), 0.5)
            self.assertEqual(self.system.active_clients, 0)
            recovered = await self.serve(request())
            self.assertEqual(self.response(recovered)[0], 200)
        finally:
            for task in stalled:
                task.cancel()
            await asyncio.gather(*stalled, return_exceptions=True)

    async def test_blocked_response_socket_does_not_leak_client_slot(self):
        failed = await asyncio.wait_for(self.serve(request(), blocked=True), 0.6)
        self.assertTrue(failed.closed)
        self.assertEqual(self.system.active_clients, 0)
        # Login may commit before its response can be delivered. The following
        # login creates a fresh session; the timeout must not wedge the server.
        healthy = await self.serve(request())
        self.assertEqual(self.response(healthy)[0], 200)
        data = await self.serve(request("/data"))
        self.assertEqual(self.response(data)[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
