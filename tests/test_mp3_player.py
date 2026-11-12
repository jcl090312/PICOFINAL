"""WT2605C protocol checks using a fake UART; no physical audio test."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mp3_player import MP3Error, WT2605CPlayer


class UART:
    def __init__(self, replies=None, initial=b"", write_size=None):
        self.replies = list(replies if replies is not None else [b"OK\r\n"])
        self.rx = bytearray(initial)
        self.tx = bytearray()
        self.pending = bytearray()
        self.commands = []
        self.write_size = write_size
        self.read_size = None
        self.read_failure = False
        self.write_failure = False

    def any(self):
        if self.read_failure:
            raise OSError("test receive fault")
        return len(self.rx)

    def read(self, count):
        if self.read_size is not None:
            count = min(count, self.read_size)
        result = bytes(self.rx[:count])
        del self.rx[:count]
        return result

    def write(self, data):
        if self.write_failure:
            raise OSError("test transmit fault")
        if self.write_size == 0:
            return None
        if self.write_size is not None:
            data = data[:self.write_size]
        self.tx.extend(data)
        self.pending.extend(data)
        while b"\r" in self.pending:
            command, rest = bytes(self.pending).split(b"\r", 1)
            self.pending = bytearray(rest)
            self.commands.append(command)
            if self.replies:
                self.rx.extend(self.replies.pop(0))
        return len(data)


class MP3Tests(unittest.IsolatedAsyncioTestCase):
    async def assert_kind(self, kind, awaitable):
        with self.assertRaises(MP3Error) as caught:
            await awaitable
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception

    async def test_configure_uses_datasheet_single_play_mode_five(self):
        uart = UART([b"OK\r", b"OK\n", b"OK\r\n"])
        result = await WT2605CPlayer(uart).configure()
        self.assertEqual(uart.commands, [b"AT+STOP", b"AT+VOL=8", b"AT+REPEATMODE=5"])
        self.assertEqual(result, {"command": "configure", "track": None,
                                  "acknowledged": True, "volume": 8})

    async def test_filename_play_and_stop_have_exact_cr_frames(self):
        uart = UART([b"OK\r", b"OK\r"])
        player = WT2605CPlayer(uart)
        self.assertEqual(await player.play("0002.mp3"),
                         {"command": "play", "track": "0002.mp3", "acknowledged": True})
        self.assertEqual(await player.stop(),
                         {"command": "stop", "track": None, "acknowledged": True})
        self.assertEqual(uart.tx, b"AT+SPLAY=sd0,0002.mp3\rAT+STOP\r")

    async def test_fragmented_echo_and_play_notifications_do_not_mask_ack(self):
        uart = UART([b"AT+SPLAY=sd0,0001.mp3\r\n+PLAY:START,sd0,1,12\r\nOK\r\n"])
        uart.read_size = 1
        result = await WT2605CPlayer(uart).play("0001.mp3")
        self.assertTrue(result["acknowledged"])

    async def test_play_notification_alone_is_not_success(self):
        uart = UART([b"+PLAY:START,sd0,1,12\r\n"])
        await self.assert_kind("timeout", WT2605CPlayer(uart, 20).play("0001.mp3"))

    async def test_stale_ack_is_discarded_before_command(self):
        uart = UART([], initial=b"OK\r\n+PLAY:END\r\n")
        await self.assert_kind("timeout", WT2605CPlayer(uart, 20).stop())

    async def test_error_responses_fail_without_fabricated_ack(self):
        for reply in (b"10\r", b"ERROR\r", b"ERROR:10\r\n", b"+ERROR:7\n"):
            with self.subTest(reply=reply):
                await self.assert_kind("rejected", WT2605CPlayer(UART([reply])).play("0001.mp3"))

    async def test_invalid_filename_cannot_inject_command_or_path(self):
        for filename in (None, 1, b"0001.mp3", "", "a", ".mp3", "123456789.mp3",
                         "warn.mp3\rAT+VOL=31", "../x.mp3", "a/b.mp3", "a\\b.mp3",
                         "a,b.mp3", "가스.mp3", "a.flac", "a..mp3", "a .mp3"):
            with self.subTest(filename=filename):
                uart = UART()
                await self.assert_kind("invalid", WT2605CPlayer(uart).play(filename))
                self.assertEqual(uart.tx, b"")

    async def test_ascii_short_names_and_wav_allowed(self):
        for filename in ("0001.mp3", "warning1.MP3", "test-1.wav", "a_B.WAV"):
            self.assertTrue((await WT2605CPlayer(UART()).play(filename))["acknowledged"])

    async def test_invalid_volume_sends_nothing(self):
        for value in (-1, 32, 8.0, True, "8", None):
            uart = UART()
            await self.assert_kind("invalid", WT2605CPlayer(uart).configure(value))
            self.assertEqual(uart.tx, b"")

    async def test_configure_stops_after_first_failed_command(self):
        uart = UART([b"OK\r", b"ERROR:2\r", b"OK\r"])
        await self.assert_kind("rejected", WT2605CPlayer(uart).configure())
        self.assertEqual(uart.commands, [b"AT+STOP", b"AT+VOL=8"])

    async def test_timeout_and_busy_calls_leave_other_async_work_running(self):
        uart = UART([])
        player = WT2605CPlayer(uart, 30)
        task = asyncio.create_task(player.play("0001.mp3"))
        await asyncio.sleep(0)
        await self.assert_kind("busy", player.stop())
        turns = 0
        while not task.done():
            turns += 1
            await asyncio.sleep(0.001)
        await self.assert_kind("timeout", task)
        self.assertGreater(turns, 2)
        uart.replies.append(b"OK\r")
        self.assertTrue((await player.stop())["acknowledged"])

    async def test_optional_cancellation_is_checked_before_and_during_wait(self):
        uart = UART()
        player = WT2605CPlayer(uart)
        await self.assert_kind("cancelled", player.play("0001.mp3", cancelled=lambda: True))
        self.assertEqual(uart.tx, b"")
        uart.replies = []
        cancel = [False]
        task = asyncio.create_task(player.play("0001.mp3", cancelled=lambda: cancel[0]))
        await asyncio.sleep(0.005)
        cancel[0] = True
        await self.assert_kind("cancelled", task)
        self.assertFalse(player._busy)

    async def test_task_cancellation_releases_driver(self):
        player = WT2605CPlayer(UART([]))
        task = asyncio.create_task(player.stop())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(player._busy)

    async def test_partial_uart_writes_preserve_frame(self):
        uart = UART(write_size=2)
        self.assertTrue((await WT2605CPlayer(uart).play("0003.mp3"))["acknowledged"])
        self.assertEqual(uart.tx, b"AT+SPLAY=sd0,0003.mp3\r")

    async def test_nonprogressing_write_is_bounded(self):
        uart = UART(write_size=0)
        await self.assert_kind("timeout", WT2605CPlayer(uart, 20).stop())
        self.assertEqual(uart.tx, b"")

    async def test_uart_failures_have_typed_errors(self):
        for attribute in ("read_failure", "write_failure"):
            uart = UART()
            setattr(uart, attribute, True)
            await self.assert_kind("io", WT2605CPlayer(uart).stop())

    async def test_unterminated_and_flooded_replies_are_bounded(self):
        for reply in (b"x" * 97, b"+PLAY:END\r" * 60):
            await self.assert_kind("protocol", WT2605CPlayer(UART([reply])).stop())
        uart = UART(initial=b"x" * 300)
        await self.assert_kind("protocol", WT2605CPlayer(uart).stop())
        self.assertEqual(uart.tx, b"")

    async def test_tick_wrap_uses_micropython_difference_semantics(self):
        import mp3_player
        from unittest.mock import patch
        values = iter([1020, 1021, 1022, 1023, 0, 1, 22])
        with patch.object(mp3_player, "ticks_ms", lambda: next(values, 22)), \
                patch.object(mp3_player, "ticks_diff", lambda a, b: (a-b+512) % 1024-512):
            await self.assert_kind("timeout", WT2605CPlayer(UART([]), 20).stop())


if __name__ == "__main__":
    unittest.main()
