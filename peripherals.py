"""Independent environmental, camera and audio tasks for the Pico alarm.

Optional-device failures never change MQ-2 thresholds or suppress gas LEDs.
The only task that writes to the MP3 UART is audio_loop (plus shutdown after
that task is cancelled). No physical playback is inferred from an AT reply.
"""

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio


def setting(settings, name, default=None):
    if isinstance(settings, dict):
        return settings.get(name, default)
    return getattr(settings, name, default)


async def sleep_ms(milliseconds):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(milliseconds)
    else:
        await asyncio.sleep(milliseconds / 1000)


def choose_audio(context, person, settings):
    """Pure decision: real gas + fresh confirmed person, or explicit admin test."""
    result = {"eligible": False, "mode": "idle", "track": None, "key": None,
              "condition": "가스 경보와 사람 감지를 기다리는 중"}
    if not setting(settings, "MP3_ENABLED", False):
        result["condition"] = "MP3 기능 꺼짐"
        return result
    real_alarm = context.get("alarm_level") if context.get("real_mode") else None
    if real_alarm in ("warning", "danger"):
        result["mode"] = "live"
        if not setting(settings, "AUTO_AUDIO_ENABLED", False):
            result["condition"] = "자동 스피커 경보 꺼짐 · LED 경보는 유지"
        elif context.get("gas_stale"):
            result["condition"] = "가스 측정 갱신 확인 필요 · LED 경보는 별도 동작"
        elif person.get("status") != "ok" or person.get("present") is None:
            result["condition"] = "가스 경보 확인 · 사람 감지 상태 확인 필요"
        elif person.get("present") is not True:
            result["condition"] = "가스 경보 확인 · 지정한 사람 ID 감지 대기"
        else:
            result.update({"eligible": True, "key": "live:" + real_alarm,
                           "track": setting(settings, "MP3_DANGER_FILE" if real_alarm == "danger" else "MP3_WARNING_FILE"),
                           "condition": "가스 경보와 사람 감지가 함께 확인됨"})
        # A genuine alarm always wins over every administrator audio request.
        return result
    job = context.get("test") or {}
    if job.get("kind") == "audio":
        result.update({"eligible": True, "mode": "manual", "key": "manual:" + str(job.get("id")),
                       "track": setting(settings, "MP3_TEST_FILE"),
                       "condition": "관리자가 요청한 실제 스피커 시험"})
    elif job.get("kind") in ("sensor", "sequence"):
        result["mode"] = "simulation"
        if not job.get("audio", False):
            result["condition"] = "모의 시험 중 · 스피커 시험 선택 안 함"
        elif not job.get("person", False):
            result["condition"] = "모의 사람 미검출 · 스피커 재생 조건 아님"
        elif context.get("test_alarm") not in ("warning", "danger"):
            result["condition"] = "모의 사람 감지 · 모의 가스 경보 연속 판정 대기"
        else:
            level = context["test_alarm"]
            result.update({"eligible": True, "key": "simulation:" + str(job.get("id")) + ":" + level,
                           "track": setting(settings, "MP3_DANGER_FILE" if level == "danger" else "MP3_WARNING_FILE"),
                           "condition": "모의 가스 경보와 모의 사람 감지 · 실제 스피커 시험"})
    elif not context.get("real_mode"):
        result["condition"] = "기본 모의 측정 중 · 실제 스피커는 관리자 시험으로 확인"
    return result


class PeripheralManager:
    def __init__(self, settings, clock, context_provider, reserved_pins=(), factories=None):
        self.settings = settings
        self.clock = clock
        self.context_provider = context_provider
        self.factories = factories or {}
        self.closed = False
        self.sht = None
        self.husky = None
        self.player = None
        self._env_time = None
        self._person_read_time = None
        self._frame_time = None
        self._frame_number = None
        self._last_seen = None
        self._positive_frames = 0
        self._play_requested = False
        self._last_play_key = None
        self._last_play_ms = None
        self._audio_retry_at = 0
        self._validation = self._validate(reserved_pins)
        self.environment = self._state("environment", setting(settings, "SHT40_ENABLED", False))
        self.environment.update({"temperature_c": None, "humidity_percent": None, "age_ms": None})
        self.person = self._state("person", setting(settings, "HUSKY_ENABLED", False))
        self.person.update({"present": None, "count": None,
                            "configured_ids": [setting(settings, "HUSKY_PERSON_ID", 1)],
                            "detected_ids": [], "age_ms": None, "hold_remaining_ms": 0,
                            "learned_count": None})
        self.audio = self._state("audio", setting(settings, "MP3_ENABLED", False))
        self.audio.update({"auto_enabled": setting(settings, "AUTO_AUDIO_ENABLED", False),
                           "mode": "idle", "eligible": False, "condition": "MP3 준비 중",
                           "last_command": None, "track": None, "last_sent_ms": None,
                           "acknowledged": None, "volume": setting(settings, "MP3_VOLUME", 8)})

    def _state(self, name, enabled):
        error = self._validation.get(name)
        return {"enabled": bool(enabled), "status": "error" if error else "starting" if enabled else "disabled",
                "error": error}

    def _validate(self, reserved):
        """An invalid add-on configuration disables that add-on, not the gas alarm."""
        errors = {}
        used = {pin: "gas" for pin in reserved if pin is not None}
        groups = (("environment", "SHT40_ENABLED", ("SHT40_SDA_PIN", "SHT40_SCL_PIN")),
                  ("person", "HUSKY_ENABLED", ("HUSKY_SDA_PIN", "HUSKY_SCL_PIN")),
                  ("audio", "MP3_ENABLED", ("MP3_TX_PIN", "MP3_RX_PIN")))
        for group, enabled, names in groups:
            if not setting(self.settings, enabled, False):
                continue
            for name in names:
                pin = setting(self.settings, name)
                if type(pin) is not int or not 0 <= pin <= 28 or pin in (23, 24, 25):
                    errors[group] = "추가 장치 GP 핀 설정을 확인하세요: " + name
                elif pin in used:
                    errors[group] = "다른 장치와 GP 핀이 겹칩니다: " + str(pin)
                    if used[pin] != "gas":
                        errors[used[pin]] = errors[group]
                else:
                    used[pin] = group
        checks = (
            ("environment", "SHT40_INTERVAL_MS", 500, 60000),
            ("environment", "SHT40_STALE_MS", 1000, 120000),
            ("person", "HUSKY_PERSON_ID", 1, 255),
            ("person", "HUSKY_INTERVAL_MS", 100, 5000),
            ("person", "HUSKY_TIMEOUT_MS", 50, 1000),
            ("person", "HUSKY_STALE_MS", 500, 10000),
            ("person", "HUSKY_CONFIRM_FRAMES", 1, 10),
            ("person", "HUSKY_HOLD_MS", 0, 10000),
            ("audio", "MP3_VOLUME", 0, 31),
            ("audio", "MP3_TIMEOUT_MS", 50, 1500),
            ("audio", "MP3_STARTUP_MS", 500, 10000),
            ("audio", "AUDIO_REPEAT_MS", 5000, 120000),
        )
        for group, name, low, high in checks:
            value = setting(self.settings, name)
            if type(value) is not int or not low <= value <= high:
                errors[group] = "추가 장치 설정 범위를 확인하세요: " + name
        for name in ("SHT40_ENABLED", "HUSKY_ENABLED", "MP3_ENABLED", "AUTO_AUDIO_ENABLED", "HUSKY_PERSON_ID_CONFIRMED"):
            if type(setting(self.settings, name)) is not bool:
                group = "environment" if name.startswith("SHT") else "person" if name.startswith("HUSKY") else "audio"
                errors[group] = "True/False 설정을 확인하세요: " + name
        return errors

    def _new_i2c(self, prefix):
        import machine
        # On RP2, I2C accepts a bounded timeout in microseconds. Do not silently
        # fall back to an unbounded bus if an incompatible build rejects it.
        return machine.I2C(setting(self.settings, prefix + "_I2C_ID"),
                           sda=machine.Pin(setting(self.settings, prefix + "_SDA_PIN")),
                           scl=machine.Pin(setting(self.settings, prefix + "_SCL_PIN")),
                           freq=setting(self.settings, "I2C_FREQUENCY", 100000),
                           timeout=setting(self.settings, "I2C_TIMEOUT_US", 20000))

    def _new_sht(self):
        if "environment" in self.factories:
            return self.factories["environment"]()
        from peripheral_sensors import SHT40
        return SHT40(self._new_i2c("SHT40"), address=setting(self.settings, "SHT40_ADDRESS", 0x44),
                     clock_ms=self.clock.ms, sleep_ms=sleep_ms,
                     stale_ms=setting(self.settings, "SHT40_STALE_MS", 6000))

    def _new_husky(self):
        if "person" in self.factories:
            return self.factories["person"]()
        from peripheral_sensors import HuskyLens
        return HuskyLens(self._new_i2c("HUSKY"), address=setting(self.settings, "HUSKY_ADDRESS", 0x32),
                         person_id=setting(self.settings, "HUSKY_PERSON_ID", 1),
                         person_id_confirmed=setting(self.settings, "HUSKY_PERSON_ID_CONFIRMED", False),
                         clock_ms=self.clock.ms, sleep_ms=sleep_ms,
                         timeout_ms=setting(self.settings, "HUSKY_TIMEOUT_MS", 250),
                         stale_ms=setting(self.settings, "HUSKY_STALE_MS", 3000))

    def _new_player(self):
        if "audio" in self.factories:
            return self.factories["audio"]()
        import machine
        from mp3_player import WT2605CPlayer
        uart = machine.UART(setting(self.settings, "MP3_UART_ID", 0),
                            baudrate=setting(self.settings, "MP3_BAUDRATE", 115200),
                            tx=machine.Pin(setting(self.settings, "MP3_TX_PIN", 0)),
                            rx=machine.Pin(setting(self.settings, "MP3_RX_PIN", 1)),
                            bits=8, parity=None, stop=1, timeout=0, timeout_char=1,
                            txbuf=128, rxbuf=256)
        return WT2605CPlayer(uart, timeout_ms=setting(self.settings, "MP3_TIMEOUT_MS", 500))

    @staticmethod
    def _error_text(prefix, exc):
        # Driver errors are curated messages. Never expose arbitrary UART bytes.
        detail = getattr(exc, "message", None)
        return prefix + (detail if type(detail) is str else type(exc).__name__)

    async def environment_loop(self):
        if not self.environment["enabled"] or "environment" in self._validation:
            return
        while not self.closed:
            delay = setting(self.settings, "SHT40_INTERVAL_MS", 2000)
            try:
                if self.sht is None:
                    self.sht = self._new_sht()
                result = await self.sht.read()
                self._env_time = self.clock.ms()
                self.environment.update({"status": "ok", "temperature_c": result["temperature_c"],
                                         "humidity_percent": result["humidity_percent"], "error": None})
            except Exception as exc:
                self.environment.update({"status": "error", "temperature_c": None, "humidity_percent": None,
                                         "error": self._error_text("SHT40 확인: ", exc)})
                self.sht = None
                delay = setting(self.settings, "PERIPHERAL_RETRY_MS", 3000)
            await sleep_ms(delay)

    def accept_person(self, result, now):
        self._person_read_time = now
        frame = result.get("frame_number")
        advanced = frame != self._frame_number or self._frame_time is None
        if advanced:
            self._frame_number = frame
            self._frame_time = now
        matches = result.get("matching_count", 0)
        training = result.get("training_required", False)
        detected = result.get("person_detected")
        self.person.update({"status": "unlearned" if training or detected is None else "ok",
                            "count": None if training else matches,
                            "learned_count": result.get("learned_count"),
                            "detected_ids": sorted(set(block["id"] for block in result.get("blocks", []))),
                            "error": "사물 인식에서 사람 ID와 학습 확인 설정을 확인하세요." if training else None})
        if training or detected is None:
            self._last_seen = None
            self._positive_frames = 0
        elif advanced:
            if detected:
                self._positive_frames += 1
                if self._positive_frames >= setting(self.settings, "HUSKY_CONFIRM_FRAMES", 2):
                    self._last_seen = now
            else:
                self._positive_frames = 0

    async def person_loop(self):
        if not self.person["enabled"] or "person" in self._validation:
            return
        while not self.closed:
            delay = setting(self.settings, "HUSKY_INTERVAL_MS", 500)
            try:
                if self.husky is None:
                    self.husky = self._new_husky()
                    await self.husky.configure()
                    self._frame_number = None
                    self._frame_time = None
                    self._positive_frames = 0
                result = await self.husky.read()
                self.accept_person(result, self.clock.ms())
            except Exception as exc:
                self.person.update({"status": "error", "present": None, "count": None, "detected_ids": [],
                                    "error": self._error_text("HUSKYLENS 확인: ", exc)})
                self._positive_frames = 0
                self._last_seen = None
                self.husky = None
                delay = setting(self.settings, "PERIPHERAL_RETRY_MS", 3000)
            await sleep_ms(delay)

    def person_snapshot(self, now):
        data = dict(self.person)
        data["age_ms"] = None if self._frame_time is None else max(0, now - self._frame_time)
        read_age = None if self._person_read_time is None else now - self._person_read_time
        if data["status"] in ("ok", "unlearned") and (
                data["age_ms"] is None or data["age_ms"] > setting(self.settings, "HUSKY_STALE_MS", 3000)
                or read_age is None or read_age > setting(self.settings, "HUSKY_STALE_MS", 3000)):
            data.update({"status": "stale", "error": "새 카메라 프레임을 확인하지 못했습니다."})
        if data["status"] != "ok":
            data.update({"present": None, "count": None, "hold_remaining_ms": 0})
        else:
            remaining = 0 if self._last_seen is None else max(0, setting(self.settings, "HUSKY_HOLD_MS", 3000) - (now - self._last_seen))
            # With HOLD_MS=0, a fresh current positive frame still counts.
            current_positive = self._last_seen is not None and self._last_seen == self._frame_time and self.person["count"] > 0
            data["present"] = bool(remaining > 0 or current_positive)
            if (data["present"] is False and data["count"] > 0
                    and self._positive_frames < setting(self.settings, "HUSKY_CONFIRM_FRAMES", 2)):
                data["present"] = None
            data["hold_remaining_ms"] = remaining
        return data

    def desired_audio(self, now):
        desired = choose_audio(self.context_provider(), self.person_snapshot(now), self.settings)
        if "audio" in self._validation:
            desired.update({"eligible": False, "condition": self._validation["audio"]})
        return desired

    def snapshot(self, now=None):
        if now is None:
            now = self.clock.ms()
        environment = dict(self.environment)
        environment["age_ms"] = None if self._env_time is None else max(0, now - self._env_time)
        if environment["status"] == "ok" and environment["age_ms"] > setting(self.settings, "SHT40_STALE_MS", 6000):
            environment.update({"status": "stale", "temperature_c": None, "humidity_percent": None,
                                "error": "온습도 측정 갱신을 확인하세요."})
        audio = dict(self.audio)
        desired = self.desired_audio(now)
        audio.update({key: desired[key] for key in ("eligible", "mode", "condition")})
        return {"environment": environment, "person": self.person_snapshot(now), "audio": audio}

    def _audio_success(self, result):
        self.audio.update({"status": "command_sent", "error": None,
                           "last_command": result["command"], "track": result.get("track"),
                           "last_sent_ms": self.clock.ms(), "acknowledged": result.get("acknowledged")})

    def _audio_failure(self, exc):
        self.audio.update({"status": "error", "acknowledged": False,
                           "error": self._error_text("MP3 응답 확인: ", exc)})
        self._audio_retry_at = self.clock.ms() + setting(self.settings, "PERIPHERAL_RETRY_MS", 3000)
        self.player = None

    async def audio_loop(self):
        if not self.audio["enabled"] or "audio" in self._validation:
            return
        await sleep_ms(setting(self.settings, "MP3_STARTUP_MS", 2000))
        while not self.closed:
            now = self.clock.ms()
            try:
                if self.player is None:
                    if now < self._audio_retry_at:
                        await sleep_ms(100)
                        continue
                    self.player = self._new_player()
                    result = await self.player.configure(volume=setting(self.settings, "MP3_VOLUME", 8))
                    self._audio_success(result)
                    self.audio["status"] = "ready"
                    self._play_requested = False
                    self._last_play_key = None
                    self._last_play_ms = None
                desired = self.desired_audio(self.clock.ms())
                if not desired["eligible"]:
                    if self._play_requested:
                        self._audio_success(await self.player.stop())
                        self._play_requested = False
                        self._last_play_key = None
                elif (desired["key"] != self._last_play_key or self._last_play_ms is None
                      or self.clock.ms() - self._last_play_ms >= setting(self.settings, "AUDIO_REPEAT_MS", 15000)):
                    # If a command was transmitted but its reply was lost, a
                    # later stop/configure is still required.
                    self._play_requested = True
                    self._audio_success(await self.player.play(desired["track"]))
                    self._last_play_key = desired["key"]
                    self._last_play_ms = self.clock.ms()
                    current = self.desired_audio(self.clock.ms())
                    if not current["eligible"] or current["key"] != desired["key"]:
                        self._audio_success(await self.player.stop())
                        self._play_requested = False
                        self._last_play_key = None
            except Exception as exc:
                self._audio_failure(exc)
            await sleep_ms(100)

    async def shutdown(self):
        self.closed = True
        if self.player is not None:
            try:
                await self.player.stop()
            except Exception:
                pass

