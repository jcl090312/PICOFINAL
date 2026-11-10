"""Admin HTTP/runtime integration checks using desktop Pico API mocks.

These tests generate their own test credential. They neither need nor inspect
the user's private admin_config.py, Wi-Fi password, or physical hardware.
"""

import asyncio
import csv
import hashlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch


# Ensure firmware imported by the shared harness cannot load a real credential.
with patch.dict(sys.modules, {"admin_config": types.ModuleType("admin_config")}):
    from test_runtime import firmware, Reader, Writer, NeverReader

# The shared harness imports firmware inside a temporary sys.modules patch.
# Use its controller class so its AdminError identity also matches firmware.
AdminController = firmware.AdminController


TEST_PASSWORD = "unit-test-only-password-42"
TEST_SALT = bytes(range(16))
TEST_HASH = hashlib.sha256(TEST_SALT + TEST_PASSWORD.encode()).hexdigest()
TEST_SETTINGS = {
    "PASSWORD_SALT_HEX": TEST_SALT.hex(),
    "PASSWORD_HASH_HEX": TEST_HASH,
}


class ManualClock:
    def __init__(self, now=1000):
        self.now = now

    def ms(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds


class AdminRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config_patch = patch.multiple(
            firmware.C, WARMUP_SECONDS=0, SAMPLE_INTERVAL_MS=1000,
            STALE_SAMPLE_MS=4500, CONSOLE_LOG=False, SIMULATION=False,
            BUZZER_PIN=17, HTTP_HEADER_TIMEOUT_MS=100,
            HTTP_WRITE_TIMEOUT_MS=100,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        with patch.object(firmware, "ADMIN_SETTINGS", None):
            self.system = firmware.System()
        counter = [0]

        def random_bytes(length):
            counter[0] += 1
            return bytes([counter[0]]) * length

        self.system.admin = AdminController(TEST_SETTINGS, random_bytes=random_bytes)
        self.clock = ManualClock()
        self.system.clock = self.clock
        self.feed_real(10000)

    def feed_real(self, raw, advance_ms=0):
        self.clock.advance(advance_ms)
        self.system.model.update(raw, self.clock.ms(), source="sensor")
        self.system.last_sample_ms = self.clock.ms()

    def tick(self, advance_ms=0, raw=10000):
        self.feed_real(raw, advance_ms)
        self.system.apply_outputs(self.clock.ms())

    async def wire_request(self, request, fragments=None, reader=None):
        writer = Writer()
        chunks = fragments if fragments is not None else [request]
        await self.system.handle_client(reader or Reader(chunks), writer)
        self.assertTrue(writer.closed)
        self.assertEqual(self.system.active_clients, 0)
        if not writer.data:
            return 0, "", b""
        headers, body = writer.response()
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        return int(headers.split(" ", 2)[1]), headers, body

    @staticmethod
    def build_request(method, path, data=None, token=None, origin="http://pico.local",
                      content_type="application/json", extra_headers=(), body=None):
        if body is None and data is not None:
            body = json.dumps(data).encode("utf-8")
        headers = [("Host", "pico.local")]
        if origin is not None:
            headers.append(("Origin", origin))
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        if body is not None:
            if content_type is not None:
                headers.append(("Content-Type", content_type))
            headers.append(("Content-Length", str(len(body))))
        headers.extend(extra_headers)
        lines = [method + " " + path + " HTTP/1.1"] + [k + ": " + v for k, v in headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (body or b"")

    async def request(self, method, path, **kwargs):
        return await self.wire_request(self.build_request(method, path, **kwargs))

    async def login(self):
        status, _, body = await self.request("POST", "/admin/login", data={"password": TEST_PASSWORD})
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(len(payload["token"]), 48)
        self.assertEqual(payload["expires_in"], 600)
        return payload["token"]

    async def start(self, token, kind="sensor", mode="normal", duration=30, raw=None):
        spec = {"kind": kind}
        if kind != "sequence":
            spec["mode"] = mode
        if duration is not None:
            spec["duration"] = duration
        if raw is not None:
            spec["raw"] = raw
        status, _, body = await self.request("POST", "/admin/test", data=spec, token=token)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["test"]["active"])
        return json.loads(body)

    async def test_protected_routes_require_a_valid_token(self):
        for method, path, data in [
            ("GET", "/admin/status", None),
            ("GET", "/admin/audit.csv", None),
            ("GET", "/admin/test-history.csv", None),
            ("POST", "/admin/test", {"kind": "led", "mode": "red"}),
            ("POST", "/admin/stop", {}),
            ("POST", "/admin/logout", {}),
        ]:
            with self.subTest(path=path):
                status, _, _ = await self.request(method, path, data=data)
                self.assertEqual(status, 401)
                self.assertIsNone(self.system.admin.test)

    async def test_fragmented_headers_and_coalesced_body_both_allow_login(self):
        request = self.build_request("POST", "/admin/login", data={"password": TEST_PASSWORD})
        # Includes a split CRLF boundary and body in the final header read.
        marker = request.index(b"\r\n\r\n")
        fragments = [request[:7], request[7:marker + 1], request[marker + 1:marker + 5], request[marker + 5:]]
        status, _, body = await self.wire_request(request, fragments=fragments)
        self.assertEqual(status, 200, body)
        token = json.loads(body)["token"]
        status, _, payload = await self.request("GET", "/admin/status", token=token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["authenticated"])

    async def test_wrong_password_cooldown_and_recovery(self):
        for attempt in range(5):
            status, _, body = await self.request("POST", "/admin/login", data={"password": "wrong-test-password"})
            self.assertEqual(status, 429 if attempt == 4 else 401)
        self.assertEqual(json.loads(body)["retry_after"], 30)
        status, _, _ = await self.request("POST", "/admin/login", data={"password": TEST_PASSWORD})
        self.assertEqual(status, 429)
        self.clock.advance(30000)
        await self.login()

    async def test_disabled_admin_fails_closed(self):
        self.system.admin = AdminController(None)
        status, _, _ = await self.request("POST", "/admin/login", data={"password": TEST_PASSWORD})
        self.assertEqual(status, 503)
        self.assertIsNone(self.system.admin.test)
        status, _, body = await self.request("GET", "/data")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["admin"]["enabled"])

    async def test_cross_origin_and_cross_site_commands_have_no_effect(self):
        token = await self.login()
        await self.start(token)
        identifier = self.system.admin.test["id"]
        for kwargs in [{"origin": "https://unrelated.invalid"},
                       {"origin": "null"},
                       {"extra_headers": [("Sec-Fetch-Site", "cross-site")]}]:
            status, _, _ = await self.request("POST", "/admin/stop", data={}, token=token, **kwargs)
            self.assertEqual(status, 403)
            self.assertEqual(self.system.admin.test["id"], identifier)

    async def test_duplicate_headers_and_transfer_encoding_are_rejected(self):
        for name, value in [("Content-Length", "2"), ("content-length", "999"),
                            ("Host", "different.invalid"), ("Origin", "http://pico.local"),
                            ("Transfer-Encoding", "chunked")]:
            with self.subTest(header=name, value=value):
                request = self.build_request("POST", "/admin/login", data={}, extra_headers=[(name, value)])
                status, _, _ = await self.wire_request(request)
                self.assertEqual(status, 400)
        token = await self.login()
        status, _, _ = await self.request("GET", "/admin/status", token=token,
                                          extra_headers=[("Authorization", "Bearer forged")])
        self.assertEqual(status, 400)

    async def test_non_json_invalid_json_and_non_object_bodies_are_rejected(self):
        for body, content_type, expected in [
            (b'{"password":', "application/json", 400),
            (b'[]', "application/json", 400),
            (b'null', "application/json", 400),
            (b'{}', "text/plain", 415),
            (b'{}', None, 415),
        ]:
            with self.subTest(body=body, content_type=content_type):
                status, _, _ = await self.request("POST", "/admin/login", body=body, content_type=content_type)
                self.assertEqual(status, expected)
                self.assertIsNone(self.system.admin.test)

    async def test_body_limit_invalid_lengths_and_short_body_are_rejected(self):
        large = b'{"password":"' + b'a' * 600 + b'"}'
        status, _, _ = await self.request("POST", "/admin/login", body=large)
        self.assertEqual(status, 413)
        for length, body in [("-1", b"{}"), ("2x", b"{}"), ("20", b"{}"), ("2", b"{}extra")]:
            with self.subTest(length=length, body=body):
                raw = ("POST /admin/login HTTP/1.1\r\nHost: pico.local\r\n"
                       "Content-Type: application/json\r\nContent-Length: " + length + "\r\n\r\n").encode() + body
                status, _, _ = await self.wire_request(raw)
                self.assertEqual(status, 400)
        raw = b"POST /admin/login HTTP/1.1\r\nContent-Type: application/json\r\n\r\n{}"
        status, _, _ = await self.wire_request(raw)
        self.assertEqual(status, 411)

    async def test_get_cannot_start_or_stop_a_test(self):
        token = await self.login()
        for path in ("/admin/login", "/admin/test", "/admin/stop", "/admin/logout"):
            status, headers, _ = await self.request("GET", path, token=token)
            self.assertEqual(status, 405)
            self.assertIn("Allow: POST", headers)
            self.assertIsNone(self.system.admin.test)
        await self.start(token)
        identifier = self.system.admin.test["id"]
        status, _, _ = await self.request("GET", "/admin/stop", token=token)
        self.assertEqual(status, 405)
        self.assertEqual(self.system.admin.test["id"], identifier)

    async def test_forged_token_and_bad_specs_preserve_running_test(self):
        token = await self.login()
        await self.start(token)
        identifier = self.system.admin.test["id"]
        status, _, _ = await self.request("POST", "/admin/stop", data={}, token="f" * 48)
        self.assertEqual(status, 401)
        invalid = [
            {"kind": "sensor", "mode": "raw", "duration": 5, "raw": True},
            {"kind": "sensor", "mode": "raw", "duration": 5, "raw": 65536},
            {"kind": "sensor", "mode": "normal", "duration": 121},
            {"kind": "sensor", "mode": "normal", "duration": 5, "extra": "ignored?"},
            {"kind": "led", "mode": "red", "duration": 120},
        ]
        for spec in invalid:
            status, _, _ = await self.request("POST", "/admin/test", data=spec, token=token)
            self.assertEqual(status, 400)
            self.assertEqual(self.system.admin.test["id"], identifier)

    async def test_public_json_csv_and_audit_do_not_expose_credentials(self):
        token = await self.login()
        await self.start(token, mode="warning")
        for path, auth in [("/data", None), ("/history.csv", None),
                           ("/events.csv", None), ("/admin/audit.csv", token),
                           ("/admin/status", token)]:
            status, headers, body = await self.request("GET", path, token=auth)
            self.assertEqual(status, 200)
            content = headers + body.decode("utf-8")
            for secret in (TEST_PASSWORD, TEST_HASH, TEST_SALT.hex(), token):
                self.assertNotIn(secret, content)

    async def test_session_expiry_is_absolute_and_stops_a_running_job(self):
        token = await self.login()
        self.feed_real(10000, 590000)
        status, _, _ = await self.request("GET", "/admin/status", token=token)
        self.assertEqual(status, 200)
        await self.start(token, duration=30)
        self.tick(10000)
        self.assertIsNone(self.system.admin.test)
        status, _, _ = await self.request("GET", "/admin/status", token=token)
        self.assertEqual(status, 401)

    async def test_public_polling_cannot_keep_a_test_lease_alive(self):
        token = await self.login()
        await self.start(token)
        self.tick(11000)
        self.assertIsNotNone(self.system.admin.test)
        status, _, _ = await self.request("GET", "/data")
        self.assertEqual(status, 200)
        self.tick(1000)
        self.assertIsNone(self.system.admin.test)
        self.assertIsNone(self.system.test_model)

    async def test_private_status_heartbeat_extends_lease_but_job_still_autoends(self):
        token = await self.login()
        await self.start(token, duration=15)
        self.tick(10000)
        status, _, _ = await self.request("GET", "/admin/status", token=token)
        self.assertEqual(status, 200)
        self.tick(4000)
        self.assertIsNotNone(self.system.admin.test)
        self.tick(1000)
        self.assertIsNone(self.system.admin.test)

    async def test_logout_stops_test_and_invalidates_token(self):
        token = await self.login()
        await self.start(token, kind="led", mode="red", duration=None)
        status, _, _ = await self.request("POST", "/admin/logout", data={}, token=token)
        self.assertEqual(status, 200)
        self.assertIsNone(self.system.admin.test)
        self.assertEqual(self.system.outputs.green.level, 1)
        self.assertEqual(self.system.outputs.red.level, 0)
        status, _, _ = await self.request("GET", "/admin/status", token=token)
        self.assertEqual(status, 401)

    async def test_led_commands_drive_real_mock_pins_then_return_to_live_state(self):
        token = await self.login()
        await self.start(token, kind="led", mode="all", duration=None)
        self.assertEqual(tuple(p.level for p in (self.system.outputs.green,
                         self.system.outputs.yellow, self.system.outputs.red)), (1, 1, 1))
        self.assertEqual(self.system.outputs.buzzer.level, 0)
        self.tick(3000)
        self.assertIsNone(self.system.admin.test)
        self.assertEqual(tuple(p.level for p in (self.system.outputs.green,
                         self.system.outputs.yellow, self.system.outputs.red)), (1, 0, 0))

    async def test_virtual_model_is_separate_from_real_model_and_csv(self):
        token = await self.login()
        real_boot = self.system.model.boot_id
        await self.start(token, mode="danger")
        for _ in range(3):
            self.tick(1000)
        self.assertEqual(self.system.model.current["raw"], 10000)
        self.assertEqual(self.system.model.current["state"], "normal")
        self.assertTrue(all(s["source"] == "sensor" and s["raw"] == 10000 for s in self.system.model.samples))
        self.assertEqual(self.system.test_model.current["state"], "danger")
        self.assertNotEqual(self.system.test_model.boot_id, real_boot)
        self.assertTrue(all(s["source"] == "simulation" for s in self.system.test_model.samples))
        self.assertEqual(self.system.outputs.buzzer.level, 0)
        snapshot = self.system.snapshot()
        self.assertEqual(snapshot["live"]["raw"], 10000)
        self.assertEqual(snapshot["current"]["source"], "simulation")
        status, _, body = await self.request("GET", "/history.csv")
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertTrue(all(row[-1] == "simulation" for row in rows[1:]))
        self.assertNotEqual(rows[1][0], real_boot)

    async def test_fault_preset_is_immediate_virtual_read_error(self):
        token = await self.login()
        await self.start(token, mode="fault", duration=5)
        self.assertIsNone(self.system.test_model.current["raw"])
        self.assertEqual(self.system.test_model.current["state"], "fault")
        self.assertEqual(self.system.test_model.current["health"], "read_error")
        self.assertEqual(self.system.model.current["state"], "normal")

    async def test_real_alarm_cancels_virtual_test_and_restores_buzzer_priority(self):
        token = await self.login()
        await self.start(token, mode="normal")
        for _ in range(3):
            self.tick(1000, raw=34000)
        self.assertEqual(self.system.model.current["state"], "danger")
        self.assertIsNone(self.system.admin.test)
        self.assertIsNone(self.system.test_model)
        self.assertEqual(self.system.outputs.buzzer.level, 1)
        self.assertEqual(self.system.snapshot()["current"]["source"], "sensor")
        status, _, _ = await self.request("POST", "/admin/test", token=token,
                                         data={"kind": "led", "mode": "green"})
        self.assertEqual(status, 409)

    async def test_real_fault_is_still_visible_during_virtual_normal_test(self):
        self.system.model.update(None, self.clock.ms(), error="mock failed ADC")
        token = await self.login()
        await self.start(token, mode="normal")
        snapshot = self.system.snapshot()
        self.assertEqual(snapshot["live"]["state"], "fault")
        self.assertEqual(snapshot["current"]["source"], "simulation")
        self.assertIsNone(snapshot["live"]["raw"])

    async def test_stale_real_sampling_cancels_test_without_browser_polling(self):
        token = await self.login()
        await self.start(token)
        self.clock.advance(5000)
        self.system.apply_outputs(self.clock.ms())
        self.assertIsNone(self.system.admin.test)
        self.assertIsNone(self.system.test_model)
        self.assertEqual(self.system.outputs.green.level, 0)

    async def test_latest_virtual_csv_remains_available_after_stop(self):
        token = await self.login()
        status, _, _ = await self.request("GET", "/admin/test-history.csv", token=token)
        self.assertEqual(status, 404)
        await self.start(token, mode="warning")
        self.tick(1000)
        test_boot = self.system.test_model.boot_id
        status, _, _ = await self.request("POST", "/admin/stop", token=token, data={})
        self.assertEqual(status, 200)
        self.assertIsNone(self.system.test_model)
        self.assertEqual(self.system.last_test_model.boot_id, test_boot)
        status, _, body = await self.request("GET", "/admin/test-history.csv", token=token)
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertTrue(all(row[0] == test_boot and row[-1] == "simulation" for row in rows[1:]))
        self.assertEqual(self.system.snapshot()["boot_id"], self.system.model.boot_id)

    async def test_automatic_sequence_reaches_each_phase_and_ends_at_sixty_seconds(self):
        token = await self.login()
        await self.start(token, kind="sequence", duration=60)
        states = {self.system.test_model.current["state"]}
        for second in range(1, 60):
            self.tick(1000)
            if second % 5 == 0:
                status, _, _ = await self.request("GET", "/admin/status", token=token)
                self.assertEqual(status, 200)
            states.add(self.system.test_model.current["state"])
        self.assertTrue({"normal", "warning", "danger"}.issubset(states))
        self.assertEqual(self.system.test_model.current["state"], "normal")
        self.tick(1000)
        self.assertIsNone(self.system.admin.test)
        self.assertIsNotNone(self.system.last_test_model)

    async def test_slow_admin_body_does_not_stop_real_sampling_or_change_outputs(self):
        # Use the normal real-time clock for this concurrency check.
        self.system.clock = firmware.Uptime()
        self.system.model = firmware.AlarmModel(firmware.C, boot_id="real-concurrent")
        self.system.last_sample_ms = None
        header = (b"POST /admin/login HTTP/1.1\r\nHost: pico.local\r\n"
                  b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")

        class SlowBody:
            def __init__(self):
                self.header = header

            async def read(self, count):
                if self.header:
                    part, self.header = self.header[:count], self.header[count:]
                    return part
                await asyncio.Future()

        writer = Writer()
        with patch.multiple(firmware.C, SAMPLE_INTERVAL_MS=100, HTTP_HEADER_TIMEOUT_MS=1000):
            tasks = [asyncio.create_task(self.system.sensor_loop()),
                     asyncio.create_task(self.system.output_loop()),
                     asyncio.create_task(self.system.handle_client(SlowBody(), writer))]
            try:
                await asyncio.sleep(0.36)
                self.assertGreaterEqual(self.system.model.total_samples, 3)
                self.assertFalse(tasks[2].done())
                self.assertIsNone(self.system.admin.test)
                self.assertEqual(self.system.outputs.green.level, 1)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(writer.closed)
        self.assertEqual(self.system.active_clients, 0)


if __name__ == "__main__":
    unittest.main()
