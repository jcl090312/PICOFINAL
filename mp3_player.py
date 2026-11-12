"""Small asynchronous driver for Seeed Grove MP3 V4 / WT2605C.

Official protocol (sections 3.1.1, 4.1.3, 4.1.10, 4.1.13, 4.1.16):
https://files.seeedstudio.com/wiki/grove-mp3-v4/WT2605C-32N-A001-datasheet.pdf
115200 baud, 8N1; AT commands end in CR. Filename playback uses SPLAY.
REPEATMODE=5 is single playback; Arduino's enum value 4 is translated to 5.

Inject a UART configured with timeout=0 and a short timeout_char (e.g. 1).
Each command has its own timeout; configure() sends three commands. The
caller owns retries, power-up settling time and the audio priority policy.
An OK reply acknowledges a command, not audible sound or completed playback.

Electrical warning: Grove connector UART is level-shifted to module VCC.
Use 3.3V/5V level translation for both UART wires when powering the Grove
board at 5V. Do not connect its TX directly to a Pico GPIO. Schematic:
https://files.seeedstudio.com/wiki/grove-mp3-v4/Grove-MP3-V4.pdf (page 2)
"""

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

try:
    from time import ticks_ms, ticks_diff
except ImportError:
    from time import monotonic

    def ticks_ms():
        return int(monotonic() * 1000)

    def ticks_diff(now, before):
        return now - before


async def _yield():
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(2)
    else:
        await asyncio.sleep(0.002)


class MP3Error(Exception):
    def __init__(self, kind, message):
        # Native MicroPython Exception has no directly callable __init__.
        super().__init__(message)
        self.kind = kind
        self.message = message


class WT2605CPlayer:
    READ_SIZE = 32
    MAX_LINE = 96
    MAX_REPLY = 512
    MAX_STALE = 256

    def __init__(self, uart, timeout_ms=500):
        if type(timeout_ms) is not int or not 10 <= timeout_ms <= 5000:
            raise MP3Error("invalid", "MP3 timeout must be 10..5000 ms")
        self.uart = uart
        self.timeout_ms = timeout_ms
        self._busy = False

    def _begin(self):
        if self._busy:
            raise MP3Error("busy", "Another MP3 command is in progress")
        self._busy = True

    def _check(self, started, cancelled):
        if cancelled is not None and cancelled():
            raise MP3Error("cancelled", "MP3 command cancelled")
        if ticks_diff(ticks_ms(), started) >= self.timeout_ms:
            raise MP3Error("timeout", "MP3 acknowledgement timed out")

    def _read(self):
        try:
            available = self.uart.any()
            if not available:
                return b""
            return self.uart.read(min(available, self.READ_SIZE)) or b""
        except (OSError, ValueError) as error:
            raise MP3Error("io", "MP3 UART read failed: " + str(error))

    async def _discard_stale(self, started, cancelled):
        # In particular, an OK buffered before transmission is not its ACK.
        count = 0
        while True:
            self._check(started, cancelled)
            data = self._read()
            if not data:
                return
            count += len(data)
            if count > self.MAX_STALE:
                raise MP3Error("protocol", "MP3 stale input exceeds limit")
            await _yield()

    def _reply(self, line):
        line = line.strip()
        if line == b"OK":
            return True
        # The datasheet lists numeric result codes 1..13. Accept explicit
        # ERROR forms too; never treat a +PLAY notification as an ACK.
        if line.startswith(b"ERROR") or line.startswith(b"+ERROR"):
            raise MP3Error("rejected", "MP3 module rejected the command")
        if line and all(48 <= char <= 57 for char in line):
            raise MP3Error("rejected", "MP3 module returned error " + line.decode())
        return False

    async def _command(self, text, name, track, cancelled):
        started = ticks_ms()
        await self._discard_stale(started, cancelled)
        frame = (text + "\r").encode("ascii")
        offset = 0
        while offset < len(frame):
            self._check(started, cancelled)
            try:
                written = self.uart.write(frame[offset:])
            except (OSError, ValueError) as error:
                raise MP3Error("io", "MP3 UART write failed: " + str(error))
            if written is not None:
                if type(written) is not int or not 0 <= written <= len(frame) - offset:
                    raise MP3Error("protocol", "Invalid MP3 UART write result")
                offset += written
            if offset < len(frame):
                await _yield()

        line = bytearray()
        received = 0
        while True:
            self._check(started, cancelled)
            data = self._read()
            received += len(data)
            if received > self.MAX_REPLY:
                raise MP3Error("protocol", "MP3 reply exceeds limit")
            for char in data:
                if char in (10, 13):
                    if self._reply(bytes(line)):
                        return {"command": name, "track": track, "acknowledged": True}
                    line = bytearray()
                else:
                    line.append(char)
                    if len(line) > self.MAX_LINE:
                        raise MP3Error("protocol", "MP3 reply line exceeds limit")
            await _yield()

    async def configure(self, volume=8, cancelled=None):
        """Stop playback, set volume 0..31 and select single non-loop mode."""
        if type(volume) is not int or not 0 <= volume <= 31:
            raise MP3Error("invalid", "MP3 volume must be 0..31")
        self._begin()
        try:
            await self._command("AT+STOP", "stop", None, cancelled)
            await self._command("AT+VOL=" + str(volume), "volume", None, cancelled)
            await self._command("AT+REPEATMODE=5", "mode", None, cancelled)
            return {"command": "configure", "track": None,
                    "acknowledged": True, "volume": volume}
        finally:
            self._busy = False

    async def play(self, filename, cancelled=None):
        """Play an ASCII 8.3 MP3/WAV filename at the microSD root."""
        if type(filename) is not str:
            raise MP3Error("invalid", "MP3 filename must be a string")
        parts = filename.split(".")
        if (len(parts) != 2 or not 1 <= len(parts[0]) <= 8
                or parts[1].lower() not in ("mp3", "wav")
                or any(not ("A" <= c <= "Z" or "a" <= c <= "z"
                            or "0" <= c <= "9" or c in "_-") for c in parts[0])):
            raise MP3Error("invalid", "Use an ASCII 8.3 filename without a directory")
        self._begin()
        try:
            return await self._command("AT+SPLAY=sd0," + filename, "play", filename, cancelled)
        finally:
            self._busy = False

    async def stop(self, cancelled=None):
        self._begin()
        try:
            return await self._command("AT+STOP", "stop", None, cancelled)
        finally:
            self._busy = False
