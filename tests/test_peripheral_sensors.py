"""Protocol and async peripheral tests; no actual sensor hardware is implied."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peripheral_sensors import (
    SensorError, SHT40, HuskyLens, HuskyFrameParser, crc8_sht4x,
    parse_sht40, build_huskylens_frame, parse_huskylens_info,
    parse_huskylens_block,
)


SHT_25C = bytes.fromhex("6666938000a2")
INFO_REFERENCE = bytes.fromhex("55aa110a29010001000500000000004a")
BLOCK_REFERENCE = bytes.fromhex("55aa110a2a2c01c8000a001400010058")


def words(*values):
    return b"".join(bytes((value & 255, value >> 8)) for value in values)


def result(blocks=(), learned=1, frame=1):
    output = build_huskylens_frame(0x29, words(len(blocks), learned, frame, 0, 0))
    return output + b"".join(build_huskylens_frame(0x2A, words(*block)) for block in blocks)


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def ms(self):
        return self.now

    async def sleep_ms(self, milliseconds):
        self.now += milliseconds
        self.waits.append(milliseconds)
        await asyncio.sleep(0)


class SHTBus:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
        self.write_result = 1

    def writeto(self, address, payload):
        self.writes.append((address, payload))
        if isinstance(self.write_result, Exception):
            raise self.write_result
        return self.write_result

    def readfrom(self, address, length):
        assert address in (0x44, 0x45) and length == 6
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class HuskyBus:
    """Keeps old unread bytes so failure/recovery really needs resynchronization."""
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.pending = bytearray()
        self.writes = []
        self.reads = 0
        self.fail_read = False
        self.fail_write = False
        self.short_read = False
        self.acknowledge = True
        self.repeat_last = False
        self.last = b""

    def writeto(self, address, payload):
        assert address == 0x32
        if self.fail_write:
            raise OSError("mock disconnected bus")
        self.writes.append(bytes(payload))
        if payload[4] in (0x2C, 0x2D) and self.acknowledge:
            # I2C can pad short ACK frames to its 16-byte transfer size.
            ack = build_huskylens_frame(0x2E)
            self.pending.extend(ack + b"\0" * (16 - len(ack)))
        elif payload[4] == 0x27:
            if self.replies:
                self.last = self.replies.pop(0)
                self.pending.extend(self.last)
            elif self.repeat_last:
                self.pending.extend(self.last)
        return len(payload)

    def readfrom(self, address, length):
        assert address == 0x32 and length == 16
        self.reads += 1
        if self.fail_read:
            raise OSError("mock disconnected bus")
        if self.short_read:
            return b"short"
        output = bytes(self.pending[:length])
        del self.pending[:length]
        return output + b"\0" * (length - len(output))


class ParserTests(unittest.TestCase):
    def test_sensirion_published_crc_example(self):
        self.assertEqual(crc8_sht4x(b"\xbe\xef"), 0x92)

    def test_temperature_humidity_conversion_and_crc(self):
        reading = parse_sht40(SHT_25C)
        self.assertEqual(reading["temperature_c"], 25.0)
        self.assertAlmostEqual(reading["humidity_percent"], 56.5, places=2)
        for location in (2, 5):
            damaged = bytearray(SHT_25C)
            damaged[location] ^= 1
            with self.assertRaises(SensorError) as failure:
                parse_sht40(damaged)
            self.assertEqual(failure.exception.code, "crc")

    def test_humidity_is_clamped_while_unclamped_value_is_retained(self):
        low = parse_sht40(bytes.fromhex("666693000081"))
        high = parse_sht40(bytes.fromhex("666693ffffac"))
        self.assertEqual((low["humidity_percent"], low["humidity_raw_percent"]), (0, -6))
        self.assertEqual((high["humidity_percent"], high["humidity_raw_percent"]), (100, 119))

    def test_sht_invalid_payloads_are_explicit_failures(self):
        for payload in (None, "123456", b"", b"12345", b"1234567"):
            with self.subTest(payload=payload), self.assertRaises(SensorError):
                parse_sht40(payload)

    def test_huskylens_command_matches_vendor_byte_example(self):
        self.assertEqual(build_huskylens_frame(0x2C), bytes.fromhex("55aa11002c3c"))
        self.assertEqual(build_huskylens_frame(0x27, b"\x01\x00"),
                         bytes.fromhex("55aa11022701003a"))

    def test_vendor_info_and_block_examples_decode_little_endian(self):
        parsed = HuskyFrameParser().feed(INFO_REFERENCE + BLOCK_REFERENCE)
        self.assertEqual(parse_huskylens_info(parsed[0][1]),
                         {"result_count": 1, "learned_count": 1, "frame_number": 5})
        self.assertEqual(parse_huskylens_block(parsed[1][1]),
                         {"x": 300, "y": 200, "width": 10, "height": 20, "id": 1})

    def test_every_split_boundary_preserves_complete_frame(self):
        for cut in range(len(BLOCK_REFERENCE) + 1):
            parser = HuskyFrameParser()
            parsed = parser.feed(BLOCK_REFERENCE[:cut])
            parsed += parser.feed(BLOCK_REFERENCE[cut:])
            self.assertEqual(parsed, [(0x2A, BLOCK_REFERENCE[5:-1])])

    def test_noise_bad_checksum_and_oversized_length_resynchronize(self):
        parser = HuskyFrameParser()
        bad = bytearray(BLOCK_REFERENCE)
        bad[-1] ^= 1
        junk = b"\x55\xaa\x11\xff\x29" + b"\x00" * 100
        parsed = parser.feed(b"\0\xff\x55" + bytes(bad) + junk + INFO_REFERENCE)
        self.assertEqual(parsed, [(0x29, INFO_REFERENCE[5:-1])])
        self.assertGreaterEqual(parser.bad_frames, 2)
        self.assertLessEqual(len(parser.buffer), parser.max_payload + 6)

    def test_parser_memory_and_returned_frame_count_are_bounded(self):
        parser = HuskyFrameParser(max_frames=2)
        self.assertEqual(parser.feed(b"\x55" * 20000), [])
        self.assertLessEqual(len(parser.buffer), 2)
        frames = parser.feed(INFO_REFERENCE * 20)
        self.assertEqual(len(frames), 2)
        self.assertEqual(parser.dropped_frames, 18)
        self.assertLessEqual(len(parser.buffer), 38)

    def test_malformed_info_and_coordinates_are_rejected(self):
        for payload in (None, b"", words(1, 1, 1, 0)):
            with self.assertRaises(SensorError):
                parse_huskylens_info(payload)
        with self.assertRaises(SensorError):
            parse_huskylens_info(words(65535, 1, 1, 0, 0))
        for payload in (words(400, 1, 20, 20, 1), words(1, 300, 20, 20, 1),
                        words(1, 1, 400, 20, 1), words(1, 1, 20, 20, 65535)):
            with self.assertRaises(SensorError):
                parse_huskylens_block(payload)


class SensorDriverTests(unittest.IsolatedAsyncioTestCase):
    def sht(self, replies, **kwargs):
        clock = Clock()
        bus = SHTBus(replies)
        return SHT40(bus, clock_ms=clock.ms, sleep_ms=clock.sleep_ms, **kwargs), bus, clock

    def husky(self, replies=(), confirmed=True, **kwargs):
        clock = Clock()
        bus = HuskyBus(replies)
        driver = HuskyLens(bus, clock_ms=clock.ms, sleep_ms=clock.sleep_ms,
                           person_id_confirmed=confirmed, **kwargs)
        return driver, bus, clock

    async def test_sht_uses_high_precision_command_and_async_ten_ms_wait(self):
        driver, bus, clock = self.sht([SHT_25C], address=0x45)
        self.assertEqual(driver.snapshot()["status"], "waiting")
        self.assertIsNone(driver.snapshot()["temperature_c"])
        reading = await driver.read()
        self.assertEqual(bus.writes, [(0x45, b"\xfd")])
        self.assertEqual(clock.waits, [10])
        self.assertEqual(reading["temperature_c"], 25)
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["last_success_ms"], 10)

    async def test_sht_error_hides_old_measurement_and_recovery_restores_it(self):
        driver, _, clock = self.sht([SHT_25C, OSError("mock"), SHT_25C])
        await driver.read()
        with self.assertRaises(SensorError):
            await driver.read()
        failed = driver.snapshot()
        self.assertEqual(failed["status"], "error")
        self.assertIsNone(failed["temperature_c"])
        self.assertEqual(failed["last_success_ms"], 10)
        recovered = await driver.read()
        self.assertEqual(recovered["temperature_c"], 25)
        self.assertEqual(recovered["error_count"], 1)
        self.assertIsNone(recovered["error"])

    async def test_sht_stale_measurement_is_unknown(self):
        driver, _, clock = self.sht([SHT_25C], stale_ms=500)
        await driver.read()
        clock.now += 500
        state = driver.snapshot()
        self.assertEqual(state["status"], "stale")
        self.assertFalse(state["available"])
        self.assertIsNone(state["humidity_percent"])

    async def test_sht_partial_write_and_bad_crc_are_errors(self):
        driver, bus, _ = self.sht([b"\0" * 6])
        bus.write_result = 0
        with self.assertRaises(SensorError) as failed:
            await driver.read()
        self.assertEqual(failed.exception.code, "i2c")
        bus.write_result = 1
        with self.assertRaises(SensorError) as failed:
            await driver.read()
        self.assertEqual(failed.exception.code, "crc")
        self.assertEqual(driver.error_count, 2)

    async def test_husky_configuration_only_knocks_and_selects_object_recognition(self):
        driver, bus, _ = self.husky()
        self.assertTrue(await driver.configure())
        self.assertTrue(driver.configured)
        self.assertEqual([frame[4] for frame in bus.writes], [0x2C, 0x2D])
        self.assertEqual(bus.writes[1][5:-1], b"\x02\x00")
        self.assertEqual(driver.snapshot()["status"], "waiting")

    async def test_confirmed_learned_person_id_can_be_detected(self):
        driver, bus, _ = self.husky([result([(150, 100, 40, 80, 2)], learned=2)], person_id=2)
        value = await driver.read()
        self.assertTrue(value["available"])
        self.assertTrue(value["person_detected"])
        self.assertFalse(value["training_required"])
        self.assertEqual(value["matching_count"], 1)
        self.assertEqual(bus.writes[-1][4], 0x27)
        self.assertEqual(bus.writes[-1][5:-1], b"\x02\x00")

    async def test_unconfirmed_id_is_never_called_a_person(self):
        driver, _, _ = self.husky([result([(150, 100, 40, 80, 1)])], confirmed=False)
        value = await driver.read()
        self.assertTrue(value["available"])
        self.assertIsNone(value["person_detected"])
        self.assertTrue(value["training_required"])
        self.assertEqual(value["matching_count"], 1)

    async def test_unlearned_id_is_unknown_even_if_a_matching_packet_arrives(self):
        driver, _, _ = self.husky([result([(150, 100, 40, 80, 1)], learned=0)])
        value = await driver.read()
        self.assertIsNone(value["person_detected"])
        self.assertTrue(value["training_required"])

    async def test_complete_fresh_zero_result_means_no_matching_detection(self):
        driver, _, _ = self.husky([result(learned=1)])
        value = await driver.read()
        self.assertFalse(value["person_detected"])
        self.assertEqual(value["matching_count"], 0)
        self.assertEqual(value["blocks"], [])

    async def test_wrong_id_and_zero_area_do_not_count_as_matching_people(self):
        driver, _, _ = self.husky([result([(150, 100, 40, 80, 2), (100, 100, 0, 80, 1)], learned=2)])
        value = await driver.read()
        self.assertFalse(value["person_detected"])
        self.assertEqual(value["matching_count"], 0)

    async def test_no_response_times_out_with_finite_reads_and_yields(self):
        driver, bus, clock = self.husky(timeout_ms=40)
        bus.acknowledge = False
        ticks = [0]

        async def unrelated_work():
            while True:
                ticks[0] += 1
                await asyncio.sleep(0)

        work = asyncio.create_task(unrelated_work())
        try:
            with self.assertRaises(SensorError) as failure:
                await driver.read()
            self.assertEqual(failure.exception.code, "timeout")
            self.assertLessEqual(clock.now, 40)
            self.assertLessEqual(bus.reads, 8)
            self.assertGreater(ticks[0], 1)
            self.assertIsNone(driver.snapshot()["person_detected"])
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def test_nonadvancing_mock_clock_is_still_bounded(self):
        bus = HuskyBus()
        bus.acknowledge = False

        async def cooperative_sleep(_milliseconds):
            await asyncio.sleep(0)

        driver = HuskyLens(bus, clock_ms=lambda: 0, sleep_ms=cooperative_sleep, timeout_ms=20)
        with self.assertRaises(SensorError):
            await driver.read()
        self.assertLessEqual(bus.reads, 6)

    async def test_truncated_result_set_never_becomes_false_absence(self):
        incomplete = build_huskylens_frame(0x29, words(2, 1, 1, 0, 0)) + BLOCK_REFERENCE
        driver, _, _ = self.husky([incomplete], timeout_ms=40)
        with self.assertRaises(SensorError):
            await driver.read()
        self.assertEqual(driver.snapshot()["status"], "error")
        self.assertIsNone(driver.snapshot()["person_detected"])

    async def test_bad_checksum_is_resynchronized_before_good_response(self):
        damaged = bytearray(INFO_REFERENCE)
        damaged[-1] ^= 1
        driver, _, _ = self.husky([b"\xff\0" + damaged + INFO_REFERENCE + BLOCK_REFERENCE])
        reading = await driver.read()
        self.assertTrue(reading["person_detected"])
        self.assertGreaterEqual(driver.parser.bad_frames, 1)

    async def test_invalid_block_checksum_cannot_publish_a_stale_person(self):
        good = result([(150, 100, 40, 80, 1)], frame=1)
        damaged = bytearray(result([(150, 100, 40, 80, 1)], frame=2))
        damaged[-1] ^= 1
        driver, _, _ = self.husky([good, damaged], timeout_ms=40)
        await driver.read()
        with self.assertRaises(SensorError):
            await driver.read()
        self.assertIsNone(driver.snapshot()["person_detected"])
        self.assertEqual(driver.snapshot()["blocks"], [])

    async def test_result_overflow_is_bounded_and_next_read_resynchronizes(self):
        too_many = result([(150, 100, 40, 80, 1)] * 4, frame=1)
        next_ok = result([(150, 100, 40, 80, 1)], frame=2)
        driver, bus, _ = self.husky([too_many, next_ok], max_results=2)
        with self.assertRaises(SensorError) as failure:
            await driver.read()
        self.assertEqual(failure.exception.code, "overflow")
        self.assertFalse(driver.configured)
        self.assertIsNone(driver.snapshot()["person_detected"])
        self.assertTrue((await driver.read())["person_detected"])
        self.assertEqual([frame[4] for frame in bus.writes], [0x2C, 0x2D, 0x27, 0x2C, 0x2D, 0x27])

    async def test_i2c_error_and_short_read_are_explicit_errors(self):
        for mode in ("fail_read", "fail_write", "short_read"):
            driver, bus, _ = self.husky()
            setattr(bus, mode, True)
            with self.assertRaises(SensorError):
                await driver.read()
            self.assertEqual(driver.snapshot()["status"], "error")
            self.assertIsNone(driver.snapshot()["person_detected"])
            self.assertFalse(driver.configured)

    async def test_frozen_frame_becomes_unknown_despite_successful_bus_reads(self):
        same = result([(150, 100, 40, 80, 1)], frame=10)
        newer = result([(150, 100, 40, 80, 1)], frame=11)
        driver, _, clock = self.husky([same, same, same, newer], stale_ms=500)
        self.assertTrue((await driver.read())["person_detected"])
        clock.now += 400
        self.assertTrue((await driver.read())["person_detected"])
        clock.now += 100
        stale = await driver.read()
        self.assertEqual(stale["age_ms"], 0)
        self.assertGreaterEqual(stale["frame_age_ms"], 500)
        self.assertEqual(stale["status"], "stale")
        self.assertIsNone(stale["person_detected"])
        self.assertTrue((await driver.read())["person_detected"])

    async def test_frame_counter_wrap_is_a_fresh_frame(self):
        driver, _, clock = self.husky([result(frame=65535), result(frame=0)], stale_ms=100)
        await driver.read()
        clock.now += 110
        value = await driver.read()
        self.assertEqual(value["frame_number"], 0)
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["frame_age_ms"], 0)

    async def test_expired_success_becomes_unknown_without_any_more_reads(self):
        driver, _, clock = self.husky([result([(150, 100, 40, 80, 1)])], stale_ms=100)
        await driver.read()
        clock.now += 100
        value = driver.snapshot()
        self.assertEqual(value["status"], "stale")
        self.assertIsNone(value["person_detected"])
        self.assertIsNone(value["matching_count"])

    async def test_snapshot_does_not_expose_mutable_internal_blocks(self):
        driver, _, _ = self.husky([result([(150, 100, 40, 80, 1)])])
        reading = await driver.read()
        reading["blocks"][0]["id"] = 999
        self.assertEqual(driver.snapshot()["blocks"][0]["id"], 1)
        driver.person_id_confirmed = False
        self.assertIsNone(driver.snapshot()["person_detected"])

    async def test_invalid_driver_settings_are_rejected(self):
        for kwargs in ({"person_id": 0}, {"person_id": True}, {"timeout_ms": 0},
                       {"max_results": 1000}, {"poll_ms": 0}, {"person_id_confirmed": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                HuskyLens(HuskyBus(), **kwargs)
        with self.assertRaises(ValueError):
            SHT40(SHTBus([]), address=0x46)


if __name__ == "__main__":
    unittest.main(verbosity=2)
