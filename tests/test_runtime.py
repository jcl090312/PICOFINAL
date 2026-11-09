"""Desktop-only runtime checks with mocked Pico APIs; no hardware was tested.

Run from repository root: python -m unittest discover -s tests -v
Only Python's standard library is needed on the desktop. Do not copy this
test file onto the Pico. The real alarm_logic module is used throughout.
"""

import asyncio
import csv
import importlib.util
import io
import json
import sys
import time as host_time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakePin:
    OUT = 1

    def __init__(self, number, mode=None, value=0):
        self.number = number
        self.mode = mode
        self.level = value

    def value(self, value=None):
        if value is not None:
            self.level = value
        return self.level


class FakeADC:
    def __init__(self, pin):
        self.pin = pin

    def read_u16(self):
        return 10000


async def sleep_ms(milliseconds):
    await asyncio.sleep(milliseconds / 1000)


async def wait_for_ms(awaitable, milliseconds):
    return await asyncio.wait_for(awaitable, milliseconds / 1000)


TICK_PERIOD = 1 << 30


def ticks_diff(a, b):
    return (a - b + TICK_PERIOD // 2) % TICK_PERIOD - TICK_PERIOD // 2


def load_firmware():
    machine = types.ModuleType("machine")
    machine.Pin, machine.ADC = FakePin, FakeADC
    micro_time = types.ModuleType("time")
    micro_time.ticks_ms = lambda: int(host_time.monotonic() * 1000) % TICK_PERIOD
    micro_time.ticks_diff = ticks_diff
    micro_async = types.ModuleType("uasyncio")
    for name in ("sleep", "create_task", "gather", "run", "start_server",
                 "new_event_loop", "TimeoutError"):
        setattr(micro_async, name, getattr(asyncio, name))
    micro_async.sleep_ms, micro_async.wait_for_ms = sleep_ms, wait_for_ms
    wifi = types.ModuleType("wifi_config")
    wifi.WIFI_SSID, wifi.WIFI_PASSWORD = "", ""
    spec = importlib.util.spec_from_file_location("firmware_main_for_tests", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"machine": machine, "time": micro_time,
                                  "uasyncio": micro_async, "wifi_config": wifi}):
        spec.loader.exec_module(module)
    return module


firmware = load_firmware()


class Reader:
    """Respects read(n), while allowing a header to arrive in fragments."""

    def __init__(self, chunks, delay=0):
        self.chunks = list(chunks)
        self.delay = delay

    async def read(self, count):
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.chunks:
            return b""
        data = self.chunks.pop(0)
        if len(data) > count:
            self.chunks.insert(0, data[count:])
        return data[:count]


class NeverReader:
    async def read(self, count):
        await asyncio.Future()


class Writer:
    def __init__(self, drain_delay=0, close_delay=0, failure=False):
        self.data = bytearray()
        self.drain_delay = drain_delay
        self.close_delay = close_delay
        self.failure = failure
        self.closed = False

    def write(self, data):
        if self.failure:
            raise OSError("mock disconnected browser")
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(self.drain_delay)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(self.close_delay)

    def response(self):
        headers, body = bytes(self.data).split(b"\r\n\r\n", 1)
        return headers.decode("utf-8"), body


class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.config_patch = patch.multiple(
            firmware.C, WARMUP_SECONDS=0, SAMPLE_INTERVAL_MS=100,
            STALE_SAMPLE_MS=300, CONSOLE_LOG=False, SIMULATION=False,
            HTTP_HEADER_TIMEOUT_MS=100, HTTP_WRITE_TIMEOUT_MS=100,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_tick_wrap_is_accumulated_without_negative_uptime(self):
        values = iter([TICK_PERIOD - 20, TICK_PERIOD - 5, 7, 1007])
        clock_api = types.SimpleNamespace(ticks_ms=lambda: next(values), ticks_diff=ticks_diff)
        with patch.object(firmware, "time", clock_api):
            uptime = firmware.Uptime()
            self.assertEqual([uptime.ms(), uptime.ms(), uptime.ms()], [15, 27, 1027])

    def test_led_patterns_are_independent_of_wifi(self):
        with patch.object(firmware.C, "BUZZER_PIN", 17):
            outputs = firmware.Outputs()
            for state, tick, expected in [
                ("normal", 0, (1, 0, 0, 0)),
                ("warmup", 0, (0, 1, 0, 0)),
                ("warmup", 600, (0, 0, 0, 0)),
                ("warning", 0, (0, 1, 0, 1)),
                ("warning", 300, (0, 1, 0, 0)),
                ("danger", 0, (0, 0, 1, 1)),
                ("danger", 250, (0, 0, 0, 1)),
                ("fault", 0, (0, 1, 0, 1)),
                ("fault", 600, (0, 0, 1, 0)),
            ]:
                for connected in (False, True):
                    with self.subTest(state=state, tick=tick, connected=connected):
                        outputs.apply(state, tick, connected)
                        self.assertEqual(tuple(p.level for p in (
                            outputs.green, outputs.yellow, outputs.red, outputs.buzzer)), expected)
            outputs.off()
            self.assertEqual(tuple(p.level for p in (
                outputs.green, outputs.yellow, outputs.red, outputs.buzzer)), (0, 0, 0, 0))

    def test_active_low_outputs_and_unused_pins(self):
        with patch.multiple(firmware.C, GREEN_LED_PIN=None, LED_ACTIVE_HIGH=False,
                            BUZZER_PIN=17, BUZZER_ACTIVE_HIGH=False):
            outputs = firmware.Outputs()
            outputs.apply("danger", 0)
            self.assertIsNone(outputs.green)
            self.assertEqual((outputs.yellow.level, outputs.red.level, outputs.buzzer.level), (1, 0, 0))
            outputs.off()
            self.assertEqual((outputs.yellow.level, outputs.red.level, outputs.buzzer.level), (1, 1, 1))

    def test_duplicate_pins_are_rejected(self):
        with patch.object(firmware.C, "RED_LED_PIN", firmware.C.ADC_PIN):
            with self.assertRaises(ValueError):
                firmware.System()

    def test_simulation_cycle_has_the_expected_alarm_inputs(self):
        self.assertEqual([firmware.simulation_value(i) for i in (0, 10, 20, 30, 40, 50, 62, 80)],
                         [10000, 22000, 34000, 24000, 10000, 0, 10000, 10000])

    def test_csv_keeps_zero_missing_input_and_simulation_labels_distinct(self):
        system = firmware.System()
        system.model.update(0, 0, source="sensor")
        system.model.update(None, 1000, source="sensor", error="test")
        system.model.update(10000, 2000, source="simulation")
        data = system.snapshot()
        result = firmware.make_csv(data, "history")
        self.assertTrue(result.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(result.decode("utf-8-sig"))))
        self.assertEqual(rows[1][3], "0")
        self.assertEqual(rows[2][3], "")
        self.assertEqual(rows[3][-1], "simulation")
        self.assertEqual(rows[1][0], system.model.boot_id)

    def test_csv_quotes_embedded_punctuation_and_guards_spreadsheet_formulas(self):
        message = '=SUM(1,2)\n"quoted"'
        data = {"events": [{"boot_id": "b", "id": 1, "time": 0, "raw": None,
                            "status": "센서 점검", "type": "state",
                            "message": message, "source": "sensor"}]}
        rows = list(csv.reader(io.StringIO(firmware.make_csv(data, "events").decode("utf-8-sig"))))
        self.assertEqual(rows[1][6], "'" + message)
        self.assertEqual(rows[1][3], "")
        self.assertEqual(firmware.csv_cell(-120), '"-120"')

    def test_snapshot_has_actual_sample_age_and_no_invented_initial_value(self):
        system = firmware.System()
        system.clock = types.SimpleNamespace(ms=lambda: 500)
        first = system.snapshot()
        self.assertIsNone(first["device"]["last_sample_age_ms"])
        self.assertIsNone(first["current"]["raw"])
        system.last_sample_ms = 200
        self.assertEqual(system.snapshot()["device"]["last_sample_age_ms"], 300)


class AsyncRuntimeChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config_patch = patch.multiple(
            firmware.C, WARMUP_SECONDS=0, SAMPLE_INTERVAL_MS=100,
            STALE_SAMPLE_MS=300, CONSOLE_LOG=False, SIMULATION=False,
            HTTP_HEADER_TIMEOUT_MS=100, HTTP_WRITE_TIMEOUT_MS=100,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.system = firmware.System()

    async def stop_tasks(self, *tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_adc_median_rejects_one_large_outlier(self):
        values = iter([9000, 10000, 65000, 11000, 10000])
        self.system.adc = types.SimpleNamespace(read_u16=lambda: next(values))
        self.assertEqual(await self.system.read_sensor(), 10000)

    async def test_fragmented_http_header_and_query_route(self):
        reader = Reader([b"G", b"ET /da", b"ta?v=2 HTTP/1.1\r", b"\nHost: pico\r\n", b"\r", b"\n"])
        writer = Writer()
        await self.system.handle_client(reader, writer)
        headers, body = writer.response()
        self.assertTrue(headers.startswith("HTTP/1.1 200 OK"))
        self.assertIn("Content-Type: application/json", headers)
        self.assertIn("Content-Length: " + str(len(body)), headers)
        self.assertEqual(json.loads(body)["current"]["state"], "starting")
        self.assertTrue(writer.closed)
        self.assertEqual(self.system.active_clients, 0)

    async def test_non_get_and_unknown_paths_are_rejected(self):
        for request, status in [(b"POST /data HTTP/1.1\r\n\r\n", b"405"),
                                (b"GET /unknown HTTP/1.1\r\n\r\n", b"404")]:
            with self.subTest(request=request):
                writer = Writer()
                await self.system.handle_client(Reader([request]), writer)
                self.assertIn(status, bytes(writer.data).split(b"\r\n", 1)[0])
                self.assertTrue(writer.closed)
                self.assertEqual(self.system.active_clients, 0)

    async def test_invalid_incomplete_or_large_headers_return_400(self):
        cases = [b"GET /data\r\n\r\n", b"GET /data HTTP/1.1\r\n",
                 b"GET /data HTTP/1.1\r\nX: " + b"a" * 3000]
        for request in cases:
            with self.subTest(request_length=len(request)):
                writer = Writer()
                await self.system.handle_client(Reader([request]), writer)
                self.assertTrue(bytes(writer.data).startswith(b"HTTP/1.1 400"))
                self.assertTrue(writer.closed)
                self.assertEqual(self.system.active_clients, 0)

    async def test_header_timeout_closes_client_and_releases_count(self):
        writer = Writer()
        await self.system.handle_client(NeverReader(), writer)
        self.assertTrue(writer.closed)
        self.assertEqual(self.system.active_clients, 0)

    async def test_slow_writer_timeout_and_disconnect_do_not_leak_client_count(self):
        for writer in (Writer(drain_delay=0.2), Writer(failure=True)):
            await self.system.handle_client(Reader([b"GET /data HTTP/1.1\r\n\r\n"]), writer)
            self.assertTrue(writer.closed)
            self.assertEqual(self.system.active_clients, 0)

    async def test_excess_client_is_closed_without_decrementing_active_others(self):
        self.system.active_clients = firmware.C.HTTP_MAX_CLIENTS
        writer = Writer()
        await self.system.handle_client(NeverReader(), writer)
        self.assertTrue(writer.closed)
        self.assertEqual(writer.data, b"")
        self.assertEqual(self.system.active_clients, firmware.C.HTTP_MAX_CLIENTS)

    async def test_csv_endpoint_sets_download_headers_and_bom(self):
        self.system.model.update(0, 0)
        writer = Writer()
        await self.system.handle_client(Reader([b"GET /history.csv HTTP/1.1\r\n\r\n"]), writer)
        headers, body = writer.response()
        self.assertIn("Content-Disposition: attachment; filename=", headers)
        self.assertIn("text/csv; charset=utf-8", headers)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))

    async def test_sensor_sampling_continues_while_http_client_is_slow(self):
        with patch.object(firmware.C, "HTTP_HEADER_TIMEOUT_MS", 1000):
            writer = Writer()
            sensor = asyncio.create_task(self.system.sensor_loop())
            request = asyncio.create_task(self.system.handle_client(NeverReader(), writer))
            try:
                await asyncio.sleep(0.36)
                self.assertGreaterEqual(self.system.model.total_samples, 3)
                self.assertFalse(request.done())
                self.assertEqual(self.system.active_clients, 1)
            finally:
                await self.stop_tasks(sensor, request)
            self.assertTrue(writer.closed)
            self.assertEqual(self.system.active_clients, 0)

    async def test_unsupported_wifi_does_not_stop_sensor_or_led_tasks(self):
        with patch.object(firmware, "network", None), redirect_stdout(io.StringIO()):
            tasks = [asyncio.create_task(fn()) for fn in
                     (self.system.sensor_loop, self.system.output_loop, self.system.wifi_loop)]
            try:
                await asyncio.sleep(0.25)
                self.assertGreaterEqual(self.system.model.total_samples, 2)
                self.assertEqual(self.system.wifi_state, "unsupported")
                self.assertEqual(self.system.outputs.green.level, 1)
            finally:
                await self.stop_tasks(*tasks)

    async def test_adc_read_errors_record_missing_values_and_keep_running(self):
        async def failed_read():
            raise OSError("mock ADC error")
        self.system.read_sensor = failed_read
        with redirect_stdout(io.StringIO()):
            tasks = [asyncio.create_task(self.system.sensor_loop()),
                     asyncio.create_task(self.system.output_loop())]
            try:
                await asyncio.sleep(0.23)
                data = self.system.snapshot()
                self.assertGreaterEqual(data["stats"]["total_count"], 2)
                self.assertEqual(data["stats"]["count"], 0)
                self.assertIsNone(data["current"]["raw"])
                self.assertEqual(data["current"]["state"], "fault")
                self.assertFalse(tasks[0].done())
                self.assertEqual(self.system.outputs.green.level, 0)
            finally:
                await self.stop_tasks(*tasks)

    async def test_wifi_driver_error_does_not_stop_local_measurement(self):
        def failed_wlan(*args):
            raise OSError("mock wireless driver unavailable")
        network = types.SimpleNamespace(WLAN=failed_wlan)
        with patch.object(firmware, "network", network), \
                patch.object(firmware, "WIFI_SSID", "unit-test"), redirect_stdout(io.StringIO()):
            tasks = [asyncio.create_task(self.system.sensor_loop()),
                     asyncio.create_task(self.system.wifi_loop())]
            try:
                await asyncio.sleep(0.23)
                self.assertGreaterEqual(self.system.model.total_samples, 2)
                self.assertEqual(self.system.wifi_state, "error")
                self.assertFalse(self.system.wifi_connected)
                self.assertFalse(tasks[0].done())
            finally:
                await self.stop_tasks(*tasks)

    async def test_stale_normal_input_changes_local_output_to_fault(self):
        self.system.model.update(10000, 0)
        self.system.last_sample_ms = 0
        self.system.clock = types.SimpleNamespace(ms=lambda: 600)
        task = asyncio.create_task(self.system.output_loop())
        try:
            await asyncio.sleep(0.01)
            self.assertEqual((self.system.outputs.green.level, self.system.outputs.red.level), (0, 1))
        finally:
            await self.stop_tasks(task)

    async def test_stale_danger_retains_danger_output(self):
        for index in range(3):
            self.system.model.update(34000, index * 100)
        self.system.last_sample_ms = 200
        self.system.clock = types.SimpleNamespace(ms=lambda: 800)
        task = asyncio.create_task(self.system.output_loop())
        try:
            await asyncio.sleep(0.01)
            self.assertEqual((self.system.outputs.green.level, self.system.outputs.red.level), (0, 1))
            self.assertEqual(self.system.outputs.yellow.level, 0)
        finally:
            await self.stop_tasks(task)


if __name__ == "__main__":
    unittest.main()
