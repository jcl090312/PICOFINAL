"""SHT40 and original SEN0305 HUSKYLENS drivers; no GPIO is created here.

Official references (protocols reimplemented here, no external package needed):
https://github.com/Sensirion/embedded-sht/tree/master/sht4x
https://github.com/Sensirion/arduino-i2c-sht4x/blob/master/src/SensirionI2cSht4x.cpp
https://github.com/HuskyLens/HUSKYLENSArduino/blob/master/HUSKYLENS%20Protocol.md
https://github.com/HuskyLens/HUSKYLENSArduino/blob/master/HUSKYLENS/HUSKYLENS.h
https://wiki.dfrobot.com/sen0305/docs/22637

HUSKYLENS2 uses a different interface and is NOT supported by this module.
ID 1 is a learning-order ID, not a universal person class. Confirm person:ID1
on the original device in Object Recognition before enabling person detection.
No command here erases or creates learning data.

Use separate I2C buses with a finite hardware timeout. Async waits below yield
to gas sampling, but cannot interrupt a blocking machine.I2C C-level call.
"""

import time
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio


class SensorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


async def _sleep_ms(milliseconds):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(milliseconds)
    else:
        await asyncio.sleep(milliseconds / 1000)


def _now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.monotonic() * 1000)


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("{} must be an integer in {}..{}".format(name, low, high))


def crc8_sht4x(data):
    """CRC-8, polynomial 0x31, initial value 0xFF, no final XOR."""
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def parse_sht40(payload):
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 6:
        raise SensorError("length", "SHT40 응답 길이가 올바르지 않습니다.")
    if crc8_sht4x(payload[:2]) != payload[2] or crc8_sht4x(payload[3:5]) != payload[5]:
        raise SensorError("crc", "SHT40 응답의 CRC가 맞지 않습니다.")
    temperature_ticks = (payload[0] << 8) | payload[1]
    humidity_ticks = (payload[3] << 8) | payload[4]
    temperature = -45 + 175 * temperature_ticks / 65535
    humidity = -6 + 125 * humidity_ticks / 65535
    return {
        "temperature_c": round(temperature, 2),
        "humidity_percent": round(max(0, min(100, humidity)), 2),
        "humidity_raw_percent": round(humidity, 2),
    }


def build_huskylens_frame(command, payload=b""):
    _integer(command, 0, 255, "command")
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > 64:
        raise ValueError("HUSKYLENS payload must be at most 64 bytes")
    data = bytes((0x55, 0xAA, 0x11, len(payload), command)) + bytes(payload)
    return data + bytes((sum(data) & 0xFF,))


class HuskyFrameParser:
    """Incremental, bounded parser that can recover after noise/bad checksums."""
    def __init__(self, max_payload=32, max_frames=8):
        _integer(max_payload, 0, 64, "max_payload")
        _integer(max_frames, 1, 16, "max_frames")
        self.max_payload = max_payload
        self.max_frames = max_frames
        self.buffer = bytearray()
        self.bad_frames = 0
        self.dropped_frames = 0

    def clear(self):
        self.buffer = bytearray()

    def feed(self, data):
        # MicroPython bytearray does not support ``del`` item/slice.
        # Rebind a bounded tail instead of deleting in place.
        frames = []
        for value in data:
            self.buffer.append(value)
            while self.buffer:
                if (self.buffer[0] != 0x55
                        or len(self.buffer) > 1 and self.buffer[1] != 0xAA
                        or len(self.buffer) > 2 and self.buffer[2] != 0x11):
                    self.buffer = self.buffer[1:]
                    continue
                if len(self.buffer) < 5:
                    break
                length = self.buffer[3]
                if length > self.max_payload:
                    self.bad_frames += 1
                    self.buffer = self.buffer[1:]
                    continue
                total = length + 6
                if len(self.buffer) < total:
                    break
                if (sum(self.buffer[:total - 1]) & 0xFF) != self.buffer[total - 1]:
                    self.bad_frames += 1
                    self.buffer = self.buffer[1:]
                    continue
                frame = (self.buffer[4], bytes(self.buffer[5:total - 1]))
                self.buffer = self.buffer[total:]
                if len(frames) < self.max_frames:
                    frames.append(frame)
                else:
                    self.dropped_frames += 1
        return frames


def _words(payload):
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 10:
        raise SensorError("length", "HUSKYLENS 정보/블록 길이가 올바르지 않습니다.")
    return [payload[i] | (payload[i + 1] << 8) for i in range(0, 10, 2)]


def parse_huskylens_info(payload):
    count, learned, frame, _, _ = _words(payload)
    if count > 32767 or learned > 32767:
        raise SensorError("protocol", "HUSKYLENS 개수 정보가 올바르지 않습니다.")
    return {"result_count": count, "learned_count": learned, "frame_number": frame}


def parse_huskylens_block(payload):
    x, y, width, height, identifier = _words(payload)
    if not (x < 320 and y < 240 and width <= 320 and height <= 240 and identifier <= 32767):
        raise SensorError("protocol", "HUSKYLENS 블록 좌표 또는 ID가 올바르지 않습니다.")
    return {"x": x, "y": y, "width": width, "height": height, "id": identifier}


class _SensorState:
    def __init__(self, clock_ms, sleep_ms, stale_ms):
        _integer(stale_ms, 100, 600000, "stale_ms")
        self._clock = clock_ms if clock_ms is not None else _now_ms
        self._sleep = sleep_ms if sleep_ms is not None else _sleep_ms
        self._uses_ticks = clock_ms is None and hasattr(time, "ticks_diff")
        self.stale_ms = stale_ms
        self.last_success_ms = None
        self.error_count = 0
        self._error = None
        self._values = None
        self._busy = False

    def _diff(self, now, before):
        return time.ticks_diff(now, before) if self._uses_ticks else now - before

    def _success(self, values):
        self._values = values
        self.last_success_ms = self._clock()
        self._error = None

    def _fail(self, error):
        self._error = error
        self.error_count += 1

    def _state(self, now_ms=None):
        now = self._clock() if now_ms is None else now_ms
        age = None if self.last_success_ms is None else max(0, self._diff(now, self.last_success_ms))
        status = "error" if self._error is not None else "waiting" if age is None else "stale" if age >= self.stale_ms else "ok"
        return {
            "status": status, "available": status == "ok", "age_ms": age,
            "last_success_ms": self.last_success_ms,
            "error": None if self._error is None else self._error.message,
            "error_code": None if self._error is None else self._error.code,
            "error_count": self.error_count,
        }


class SHT40(_SensorState):
    def __init__(self, i2c, address=0x44, clock_ms=None, sleep_ms=None, stale_ms=5000):
        super().__init__(clock_ms, sleep_ms, stale_ms)
        if type(address) is not int or address not in (0x44, 0x45):
            raise ValueError("SHT40 address must be 0x44 or 0x45")
        self.i2c = i2c
        self.address = address

    async def read(self):
        if self._busy:
            raise SensorError("busy", "SHT40 측정이 이미 진행 중입니다.")
        self._busy = True
        try:
            if self.i2c.writeto(self.address, b"\xfd") != 1:
                raise SensorError("i2c", "SHT40 측정 명령 전송에 실패했습니다.")
            # Sensirion's reference high-repeatability driver waits 10 ms.
            await self._sleep(10)
            values = parse_sht40(self.i2c.readfrom(self.address, 6))
            self._success(values)
            return self.snapshot()
        except SensorError as error:
            self._fail(error)
            raise
        except OSError:
            error = SensorError("i2c", "SHT40 I2C 통신에 실패했습니다.")
            self._fail(error)
            raise error
        finally:
            self._busy = False

    def snapshot(self, now_ms=None):
        result = self._state(now_ms)
        for name in ("temperature_c", "humidity_percent", "humidity_raw_percent"):
            result[name] = self._values[name] if result["available"] and self._values is not None else None
        return result


class HuskyLens(_SensorState):
    """Original HUSKYLENS, 100 kHz I2C, learned object ID only.

    configure() selects Object Recognition and never forgets learned items.
    The operator must verify that person_id really labels 'person' in that mode.
    Successful bus reads with a frozen camera frame become stale/unknown.
    """
    def __init__(self, i2c, address=0x32, person_id=1, person_id_confirmed=False,
                 clock_ms=None, sleep_ms=None, timeout_ms=250, poll_ms=5,
                 max_results=8, stale_ms=3000):
        super().__init__(clock_ms, sleep_ms, stale_ms)
        _integer(address, 1, 127, "address")
        _integer(person_id, 1, 32767, "person_id")
        _integer(timeout_ms, 20, 1000, "timeout_ms")
        _integer(poll_ms, 1, 20, "poll_ms")
        _integer(max_results, 1, 32, "max_results")
        if type(person_id_confirmed) is not bool:
            raise ValueError("person_id_confirmed must be True or False")
        self.i2c = i2c
        self.address = address
        self.person_id = person_id
        self.person_id_confirmed = person_id_confirmed
        self.timeout_ms = timeout_ms
        self.poll_ms = poll_ms
        self.max_results = max_results
        self.configured = False
        self.parser = HuskyFrameParser(max_payload=32)
        self._pending = []
        self._frame_number = None
        self._frame_changed_ms = None

    def _reset_parser(self):
        self.parser.clear()
        self._pending = []

    def _write(self, command, payload=b""):
        frame = build_huskylens_frame(command, payload)
        if self.i2c.writeto(self.address, frame) != len(frame):
            raise SensorError("i2c", "HUSKYLENS 명령 전송에 실패했습니다.")

    async def _next_frame(self, started, budget):
        while self._diff(self._clock(), started) < self.timeout_ms:
            if self._pending:
                return self._pending.pop(0)
            if budget[0] <= 0:
                break
            await self._sleep(self.poll_ms)
            if self._diff(self._clock(), started) >= self.timeout_ms:
                break
            budget[0] -= 1
            # The original vendor driver requests 16-byte I2C chunks as well.
            chunk = self.i2c.readfrom(self.address, 16)
            if not isinstance(chunk, (bytes, bytearray, memoryview)) or len(chunk) != 16:
                raise SensorError("length", "HUSKYLENS I2C 응답 길이가 올바르지 않습니다.")
            self._pending.extend(self.parser.feed(chunk))
        raise SensorError("timeout", "HUSKYLENS 응답 시간이 초과되었습니다.")

    def _budget(self):
        # Also finite when a mocked/broken clock fails to advance.
        return [self.timeout_ms // self.poll_ms + 2]

    async def _acknowledge(self, command, payload=b""):
        self._reset_parser()
        started = self._clock()
        budget = self._budget()
        self._write(command, payload)
        while True:
            reply, data = await self._next_frame(started, budget)
            if reply == 0x2E:
                if data:
                    raise SensorError("protocol", "HUSKYLENS 확인 응답이 올바르지 않습니다.")
                return
            # A knock acknowledgment provides a boundary after an aborted reply.

    async def configure(self):
        if self._busy:
            raise SensorError("busy", "HUSKYLENS 통신이 이미 진행 중입니다.")
        self._busy = True
        self.configured = False
        try:
            await self._acknowledge(0x2C)  # Knock: establish contact/resynchronize.
            await self._acknowledge(0x2D, b"\x02\x00")  # Object Recognition.
            self.configured = True
            self._error = None
            return True
        except SensorError as error:
            self._fail(error)
            self._reset_parser()
            raise
        except OSError:
            error = SensorError("i2c", "HUSKYLENS I2C 설정 통신에 실패했습니다.")
            self._fail(error)
            self._reset_parser()
            raise error
        finally:
            self._busy = False

    async def read(self):
        if not self.configured:
            await self.configure()
        if self._busy:
            raise SensorError("busy", "HUSKYLENS 통신이 이미 진행 중입니다.")
        self._busy = True
        try:
            self._reset_parser()
            started = self._clock()
            budget = self._budget()
            identifier = bytes((self.person_id & 0xFF, self.person_id >> 8))
            self._write(0x27, identifier)  # Request blocks carrying the chosen ID.
            while True:
                command, payload = await self._next_frame(started, budget)
                if command == 0x29:
                    info = parse_huskylens_info(payload)
                    break
            if info["result_count"] > self.max_results:
                raise SensorError("overflow", "HUSKYLENS 결과가 보관 한도를 초과했습니다.")
            blocks = []
            for _ in range(info["result_count"]):
                command, payload = await self._next_frame(started, budget)
                if command != 0x2A:
                    raise SensorError("protocol", "HUSKYLENS 블록 응답 순서가 올바르지 않습니다.")
                blocks.append(parse_huskylens_block(payload))
            now = self._clock()
            if info["frame_number"] != self._frame_number:
                self._frame_changed_ms = now
                self._frame_number = info["frame_number"]
            matching = sum(1 for block in blocks if block["id"] == self.person_id
                           and block["width"] > 0 and block["height"] > 0)
            training = not self.person_id_confirmed or info["learned_count"] < self.person_id
            info.update({"blocks": blocks, "matching_count": matching,
                         "training_required": training,
                         "person_detected": None if training else matching > 0})
            self._success(info)
            return self.snapshot()
        except SensorError as error:
            self.configured = False
            self._fail(error)
            self._reset_parser()
            raise
        except OSError:
            self.configured = False
            error = SensorError("i2c", "HUSKYLENS I2C 측정 통신에 실패했습니다.")
            self._fail(error)
            self._reset_parser()
            raise error
        finally:
            self._busy = False

    def snapshot(self, now_ms=None):
        now = self._clock() if now_ms is None else now_ms
        result = self._state(now)
        frame_age = None if self._frame_changed_ms is None else max(0, self._diff(now, self._frame_changed_ms))
        if result["status"] == "ok" and frame_age is not None and frame_age >= self.stale_ms:
            result["status"], result["available"] = "stale", False
        if result["status"] == "ok" and not self.configured:
            result["status"], result["available"] = "waiting", False
        result.update({"person_id": self.person_id, "person_id_confirmed": self.person_id_confirmed,
                       "configured": self.configured, "frame_age_ms": frame_age,
                       "frame_number": None, "learned_count": None, "result_count": None,
                       "blocks": [], "matching_count": None, "training_required": True,
                       "person_detected": None})
        if self._values is not None:
            for key in ("frame_number", "learned_count", "result_count"):
                result[key] = self._values[key]
            result["training_required"] = not self.person_id_confirmed or self._values["learned_count"] < self.person_id
            if result["available"]:
                result["blocks"] = [dict(block) for block in self._values["blocks"]]
                result["matching_count"] = sum(1 for block in result["blocks"] if block["id"] == self.person_id
                                               and block["width"] > 0 and block["height"] > 0)
                result["person_detected"] = None if result["training_required"] else result["matching_count"] > 0
        return result
