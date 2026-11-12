"""Pure protocol and policy smoke test on MicroPython, without connected sensors.

Run on a MicroPython build with asyncio/uasyncio available. For the stripped
Unix validation build, add qa_asyncio_stub to sys.path (not a Pico file).
No real sensor, MP3 or camera is accessed, and no private configuration loaded.
"""
import sys
sys.path.insert(0, '.')

from peripheral_sensors import (crc8_sht4x, parse_sht40,
                                build_huskylens_frame, HuskyFrameParser, SensorError)
from mp3_player import WT2605CPlayer, MP3Error
from peripherals import choose_audio


def check():
    print('MicroPython peripheral smoke:', sys.implementation)
    payload = bytearray((0x66, 0x66, 0, 0x80, 0x00, 0))
    payload[2] = crc8_sht4x(payload[:2])
    payload[5] = crc8_sht4x(payload[3:5])
    decoded = parse_sht40(bytes(payload))
    assert 24 < decoded['temperature_c'] < 26
    assert 55 < decoded['humidity_percent'] < 60
    try:
        parse_sht40(bytes((0, 0, 0, 0, 0, 0)))
    except SensorError as err:
        assert err.code == 'crc'
    else:
        raise AssertionError('SHT40 bad CRC was not rejected')
    command = build_huskylens_frame(0x2C)
    assert command == b'\x55\xaa\x11\x00\x2c\x3c'
    parser = HuskyFrameParser()
    assert not parser.feed(command[:3])
    assert parser.feed(command[3:]) == [(0x2C, b'')]
    try:
        WT2605CPlayer(None, timeout_ms=0)
    except MP3Error as err:
        assert err.kind == 'invalid'
    else:
        raise AssertionError('invalid MP3 timeout accepted')
    settings = {'MP3_ENABLED': True, 'AUTO_AUDIO_ENABLED': True,
                'MP3_WARNING_FILE': '0001.mp3', 'MP3_DANGER_FILE': '0002.mp3',
                'MP3_TEST_FILE': '0003.mp3'}
    context = {'real_mode': True, 'alarm_level': 'danger', 'gas_stale': False, 'test': None}
    person = {'status': 'ok', 'present': True}
    assert choose_audio(context, person, settings)['track'] == '0002.mp3'
    assert not choose_audio(context, {'status':'error','present':None}, settings)['eligible']
    print('PASS: SHT CRC, HUSKY packet, MP3 validation, person-gated alarm policy')


if __name__ == '__main__':
    check()


class _Clock:
    def __init__(self):
        self.now = 0
    def ms(self):
        return self.now
    async def delay(self, count):
        self.now += count


class _SHTBus:
    def writeto(self, address, payload):
        assert address == 0x44 and payload == b'\xfd'
        return len(payload)
    def readfrom(self, address, count):
        assert address == 0x44 and count == 6
        return bytes((0x66, 0x66, 0x93, 0x80, 0x00, 0xA2))


class _HuskyBus:
    def __init__(self):
        self.pending = bytearray()
    def writeto(self, address, data):
        assert address == 0x32
        if data[4] in (0x2C, 0x2D):
            self.pending.extend(build_huskylens_frame(0x2E))
        elif data[4] == 0x27:
            # One learned person, frame number 5, then one object block.
            words = lambda *parts: b''.join(bytes((v & 255, v >> 8)) for v in parts)
            self.pending.extend(build_huskylens_frame(0x29, words(1, 1, 5, 0, 0)))
            self.pending.extend(build_huskylens_frame(0x2A, words(160, 120, 60, 90, 1)))
        return len(data)
    def readfrom(self, address, count):
        assert address == 0x32 and count == 16
        chunk = bytes(self.pending[:count])
        self.pending = self.pending[count:]
        return chunk + b'\x00' * (count - len(chunk))


class _UART:
    def __init__(self):
        self.pending = bytearray()
        self.commands = []
    def any(self):
        return len(self.pending)
    def write(self, data):
        assert data.endswith(b'\r')
        self.commands.append(bytes(data))
        self.pending.extend(b'OK\r\n')
        return len(data)
    def read(self, count):
        chunk = bytes(self.pending[:count])
        self.pending = self.pending[count:]
        return chunk


def _drive(coro):
    """All awaited stubs are synchronous coroutines; no event loop needed."""
    while True:
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value


async def _async_smoke():
    from peripheral_sensors import SHT40, HuskyLens
    clock = _Clock()
    sht = SHT40(_SHTBus(), clock_ms=clock.ms, sleep_ms=clock.delay)
    result = await sht.read()
    assert result['status'] == 'ok' and result['temperature_c'] == 25.0
    husky = HuskyLens(_HuskyBus(), clock_ms=clock.ms, sleep_ms=clock.delay,
                       person_id=1, person_id_confirmed=True)
    assert await husky.configure()
    result = await husky.read()
    assert result['status'] == 'ok' and result['person_detected']
    uart = _UART()
    player = WT2605CPlayer(uart, timeout_ms=100)
    assert (await player.configure(volume=8))['acknowledged']
    assert (await player.play('0002.mp3'))['acknowledged']
    assert (await player.stop())['acknowledged']
    assert uart.commands == [b'AT+STOP\r', b'AT+VOL=8\r', b'AT+REPEATMODE=5\r',
                             b'AT+SPLAY=sd0,0002.mp3\r', b'AT+STOP\r']
    print('PASS: MicroPython async SHT40 / HUSKY / MP3 command paths with fake buses')


if __name__ == '__main__':
    try:
        import uasyncio as _asyncio
    except ImportError:
        import asyncio as _asyncio
    if hasattr(_asyncio, 'run'):
        _asyncio.run(_async_smoke())
    else:
        # For the intentionally stripped local Unix import shim only.
        _drive(_async_smoke())
