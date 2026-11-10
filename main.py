"""Pico W / Pico 2 W + MQ-2 교육용 가스 누출 경보기.

필수 파일: main.py, config.py, alarm_logic.py, admin_control.py,
dashboard.html, wifi_config.py, admin_config.py(관리자 비밀번호 설정)
추가 pip 패키지 없이 보드용 MicroPython에서 실행합니다.
"""

import gc
import json
import os
import time
import uasyncio as asyncio
from machine import ADC, Pin

import config as C
from alarm_logic import AlarmModel, validate_settings
from admin_control import AdminController, AdminError

try:
    import admin_config as ADMIN_SETTINGS
except ImportError:
    ADMIN_SETTINGS = None

try:
    import network
except ImportError:
    network = None

try:
    from wifi_config import WIFI_SSID, WIFI_PASSWORD
except ImportError:
    WIFI_SSID, WIFI_PASSWORD = "", ""

FIRMWARE_VERSION = "2.1.0-admin"
ADMIN_BODY_LIMIT = 512


class Uptime:
    """짧은 ticks 차분을 누적하여 장기 실행 시 wrap 문제를 피합니다."""

    def __init__(self):
        self.last = time.ticks_ms()
        self.value = 0

    def ms(self):
        now = time.ticks_ms()
        self.value += time.ticks_diff(now, self.last)
        self.last = now
        return self.value


def boot_identifier():
    try:
        return "".join("%02x" % byte for byte in os.urandom(4))
    except (AttributeError, OSError):
        return "boot-%x" % time.ticks_ms()


def validate_hardware_settings():
    validate_settings(C)
    if C.ADC_PIN not in (26, 27, 28):
        raise ValueError("ADC_PIN must be GP26, GP27 or GP28")
    if C.ADC_MEDIAN_READS not in (1, 3, 5, 7, 9):
        raise ValueError("ADC_MEDIAN_READS must be 1, 3, 5, 7 or 9")
    pins = [C.ADC_PIN]
    for name in ("GREEN_LED_PIN", "YELLOW_LED_PIN", "RED_LED_PIN", "BUZZER_PIN"):
        value = getattr(C, name)
        if value is not None:
            if type(value) is not int or not (0 <= value <= 28) or value in (23, 24, 25):
                raise ValueError(name + " must be an exposed GP pin or None")
            if value in pins:
                raise ValueError("GPIO pin is assigned twice: " + str(value))
            pins.append(value)
    if not 1 <= C.HTTP_PORT <= 65535 or not 1 <= C.HTTP_MAX_CLIENTS <= 4:
        raise ValueError("Invalid HTTP_PORT / HTTP_MAX_CLIENTS")
    if not 256 <= C.HTTP_HEADER_LIMIT <= 4096:
        raise ValueError("HTTP_HEADER_LIMIT must be 256..4096")
    if not 100 <= C.HTTP_HEADER_TIMEOUT_MS <= 10000 or not 100 <= C.HTTP_WRITE_TIMEOUT_MS <= 10000:
        raise ValueError("HTTP timeouts must be 100..10000ms")
    if C.STALE_SAMPLE_MS < C.SAMPLE_INTERVAL_MS * 3:
        raise ValueError("STALE_SAMPLE_MS must be at least 3 sample intervals")
    if C.WIFI_CONNECT_TIMEOUT_SECONDS < 1 or C.WIFI_RETRY_SECONDS < 1:
        raise ValueError("Wi-Fi timeout/retry must be positive")


class Outputs:
    def __init__(self):
        inactive = 0 if C.LED_ACTIVE_HIGH else 1
        self.green = self.pin(C.GREEN_LED_PIN, inactive)
        self.yellow = self.pin(C.YELLOW_LED_PIN, inactive)
        self.red = self.pin(C.RED_LED_PIN, inactive)
        self.buzzer = self.pin(C.BUZZER_PIN, 0 if C.BUZZER_ACTIVE_HIGH else 1)
        self.onboard = None
        if C.USE_ONBOARD_LED:
            try:
                self.onboard = Pin("LED", Pin.OUT, value=0)
            except (ValueError, TypeError):
                pass

    @staticmethod
    def pin(number, initial):
        return None if number is None else Pin(number, Pin.OUT, value=initial)

    @staticmethod
    def set_pin(pin, on, active_high):
        if pin is not None:
            pin.value(int(bool(on) == bool(active_high)))

    def apply(self, state, now_ms, wifi_connected=False, allow_buzzer=True):
        green = state == "normal"
        yellow = state == "warning"
        red = False
        buzz = False
        if state in ("starting", "warmup"):
            yellow = (now_ms % 1000) < 500
        elif state == "warning":
            buzz = (now_ms % 2000) < 200
        elif state == "danger":
            red = (now_ms % 400) < 200
            buzz = True
        elif state == "fault":
            yellow = (now_ms % 1000) < 500
            red = not yellow
            buzz = (now_ms % 3000) < 100
        self.set_pin(self.green, green, C.LED_ACTIVE_HIGH)
        self.set_pin(self.yellow, yellow, C.LED_ACTIVE_HIGH)
        self.set_pin(self.red, red, C.LED_ACTIVE_HIGH)
        self.set_pin(self.buzzer, buzz and allow_buzzer, C.BUZZER_ACTIVE_HIGH)
        if self.onboard is not None:
            self.onboard.value(1 if wifi_connected else int((now_ms % 2000) < 100))

    def apply_test(self, values, now_ms, wifi_connected=False):
        """개별 LED 확인. 장치 부저는 관리자 시험 중 울리지 않습니다."""
        for pin, on in zip((self.green, self.yellow, self.red), values):
            self.set_pin(pin, on, C.LED_ACTIVE_HIGH)
        self.set_pin(self.buzzer, False, C.BUZZER_ACTIVE_HIGH)
        if self.onboard is not None:
            self.onboard.value(1 if wifi_connected else int((now_ms % 2000) < 100))

    def off(self):
        for pin in (self.green, self.yellow, self.red):
            self.set_pin(pin, False, C.LED_ACTIVE_HIGH)
        self.set_pin(self.buzzer, False, C.BUZZER_ACTIVE_HIGH)
        if self.onboard is not None:
            self.onboard.value(0)


def simulation_value(index):
    """80회 측정 주기. 실제 가스 없이 LED·화면·기록 흐름을 확인합니다."""
    # Korean user-visible names live in the model/dashboard; CSV source is simulation.
    position = index % 80
    if position < 10:
        return 10000
    if position < 20:
        return 22000
    if position < 30:
        return 34000
    if position < 40:
        return 24000
    if position < 50:
        return 10000
    if position < 62:
        return 0
    return 10000


def csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    # Protect spreadsheet consumers if a future sensor/event message contains a formula.
    if not isinstance(value, (int, float)) and text[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def make_csv(data, kind):
    """UTF-8 BOM for Excel; retained rows only; elapsed time is not wall clock."""
    if kind == "events":
        headings = ["부팅ID", "기록ID", "경과시간(초)", "ADC값", "상태", "종류", "내용", "입력출처"]
        rows = [[e["boot_id"], e["id"], e["time"], e["raw"], e["status"],
                 e["type"], e["message"], e["source"]] for e in data["events"]]
    else:
        headings = ["부팅ID", "측정ID", "경과시간(초)", "ADC값", "변화량", "상태", "신호점검", "입력출처"]
        rows = [[data["boot_id"], s["id"], s["time"], s["raw"], s["change"],
                 s["status"], s["health"], s["source"]] for s in data["samples"]]
    lines = [",".join(csv_cell(value) for value in headings)]
    for row in rows:
        lines.append(",".join(csv_cell(value) for value in row))
    return ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")


class System:
    def __init__(self):
        validate_hardware_settings()
        self.clock = Uptime()
        self.model = AlarmModel(C, boot_id=boot_identifier())
        self.admin = AdminController(ADMIN_SETTINGS)
        self.test_model = None
        self.test_model_id = None
        self.last_test_model = None
        self.last_test_sample_ms = None
        self.outputs = Outputs()
        self.adc = None  # Lazy creation: ADC errors must not stop Wi-Fi and fault LEDs.
        self.last_sample_ms = None
        self.wifi_connected = False
        self.ip = None
        self.wifi_state = "not_configured"
        self.active_clients = 0
        self.server = None

    async def read_sensor(self):
        if self.adc is None:
            self.adc = ADC(Pin(C.ADC_PIN))
        values = []
        for number in range(C.ADC_MEDIAN_READS):
            values.append(self.adc.read_u16())
            if number + 1 < C.ADC_MEDIAN_READS:
                await asyncio.sleep_ms(2)
        values.sort()
        return values[len(values) // 2]

    async def sensor_loop(self):
        index = 0
        next_due = self.clock.ms()
        while True:
            raw, error = None, None
            source = "simulation" if C.SIMULATION else "sensor"
            try:
                raw = simulation_value(index) if C.SIMULATION else await self.read_sensor()
            except Exception as exc:
                error = "ADC 읽기 실패: " + type(exc).__name__
                self.adc = None
                print(error)
            now = self.clock.ms()
            self.model.update(raw, now, source=source, error=error)
            self.last_sample_ms = now
            self.update_admin(now)
            self.sample_test(now)
            if C.CONSOLE_LOG:
                current = self.model.current
                print("[%s] t=%ss ADC=%s 상태=%s 신호=%s" % (
                    source, current["time"], raw, current["status"], current["health"]))
            index += 1
            next_due += C.SAMPLE_INTERVAL_MS
            now = self.clock.ms()
            # If delayed, do not fabricate missed readings or burst-catch up confirmations.
            if next_due <= now:
                next_due = now + C.SAMPLE_INTERVAL_MS
            await asyncio.sleep_ms(max(1, next_due - now))

    async def output_loop(self):
        while True:
            now = self.clock.ms()
            self.apply_outputs(now)
            await asyncio.sleep_ms(50)

    def samples_stale(self, now_ms):
        return self.last_sample_ms is not None and now_ms - self.last_sample_ms > C.STALE_SAMPLE_MS

    def update_admin(self, now_ms):
        """Called independently of browser polling; expiry never relies on the UI."""
        self.admin.tick(now_ms, real_alarm=self.model.current["alarm_level"],
                        real_mode=not C.SIMULATION, sample_stale=self.samples_stale(now_ms))
        job = self.admin.test
        virtual = job is not None and job["kind"] in ("sensor", "sequence")
        if self.test_model is not None and (not virtual or job["id"] != self.test_model_id):
            self.last_test_model = self.test_model
            self.test_model = None
            self.test_model_id = None
            self.last_test_sample_ms = None
        if virtual and self.test_model is None:
            # Bound memory: one live ring and one current/latest test ring only.
            self.last_test_model = None
            settings = {name: getattr(C, name) for name in dir(C) if name.isupper()}
            settings["SIMULATION"] = True
            self.test_model_id = job["id"]
            self.test_model = AlarmModel(settings, self.model.boot_id + "-test-" + str(job["id"]))
            self.last_test_sample_ms = None

    def sample_test(self, now_ms):
        if self.test_model is None:
            return
        # HTTP commands may start a model immediately, but cannot count as a
        # burst of consecutive samples when the physical sampling task resumes.
        if self.last_test_sample_ms is not None and now_ms - self.last_test_sample_ms < C.SAMPLE_INTERVAL_MS:
            return
        raw = self.admin.sample_value(now_ms)
        error = "관리자 가상 읽기 오류" if raw is None else None
        self.test_model.update(raw, now_ms, source="simulation", error=error)
        self.last_test_sample_ms = now_ms

    def apply_outputs(self, now_ms):
        self.update_admin(now_ms)
        stale = self.samples_stale(now_ms)
        if stale:
            state = "danger" if self.model.current["alarm_level"] == "danger" else "fault"
            self.outputs.apply(state, now_ms, self.wifi_connected)
            return
        self.sample_test(now_ms)
        led_values = self.admin.led_values(now_ms)
        if led_values is not None:
            self.outputs.apply_test(led_values, now_ms, self.wifi_connected)
        elif self.test_model is not None:
            state = self.test_model.current["state"]
            self.outputs.apply(state, now_ms, self.wifi_connected, allow_buzzer=False)
        else:
            self.outputs.apply(self.model.current["state"], now_ms, self.wifi_connected)

    async def wifi_loop(self):
        if network is None or not hasattr(network, "WLAN"):
            self.wifi_state = "unsupported"
            print("Wi-Fi를 지원하는 Pico W/2 W 펌웨어가 필요합니다. 센서와 LED는 계속 실행합니다.")
            while True:
                await asyncio.sleep(30)
        if not WIFI_SSID:
            print("Wi-Fi 미설정: wifi_config.py를 수정하세요. 센서와 LED는 계속 실행합니다.")
            while True:
                await asyncio.sleep(30)
        wlan = None
        while True:
            try:
                if wlan is None:
                    # Works with both old and current RP2 station-interface APIs.
                    try:
                        wlan = network.WLAN()
                    except TypeError:
                        wlan = network.WLAN(network.STA_IF)
                    wlan.active(True)
                if wlan.isconnected():
                    self.wifi_connected = True
                    self.wifi_state = "connected"
                    address = wlan.ifconfig()[0]
                    if address != self.ip:
                        self.ip = address
                        suffix = "" if C.HTTP_PORT == 80 else ":" + str(C.HTTP_PORT)
                        print("대시보드 주소: http://" + address + suffix)
                    await asyncio.sleep(2)
                    continue
                self.wifi_connected, self.ip = False, None
                self.wifi_state = "connecting"
                print("Wi-Fi 연결 시도 중... 센서와 LED는 계속 실행됩니다.")
                try:
                    wlan.disconnect()
                except OSError:
                    pass
                wlan.connect(WIFI_SSID, WIFI_PASSWORD)
                deadline = self.clock.ms() + C.WIFI_CONNECT_TIMEOUT_SECONDS * 1000
                while not wlan.isconnected() and self.clock.ms() < deadline:
                    await asyncio.sleep_ms(500)
                if wlan.isconnected():
                    continue
                self.wifi_state = "retry_wait"
                print("Wi-Fi 연결 실패: %s초 뒤 재시도합니다." % C.WIFI_RETRY_SECONDS)
            except Exception as exc:
                self.wifi_connected, self.ip = False, None
                self.wifi_state = "error"
                # Avoid printing credentials or arbitrary network exception contents.
                print("Wi-Fi 오류:", type(exc).__name__)
                wlan = None
            await asyncio.sleep(C.WIFI_RETRY_SECONDS)

    def snapshot(self):
        now = self.clock.ms()
        self.update_admin(now)
        display_model = self.test_model if self.test_model is not None else self.model
        data = display_model.snapshot()
        data["boot_id"] = display_model.boot_id
        data["device"] = self.device_info(now)
        data["admin"] = self.admin.public_status(now)
        data["live"] = dict(self.model.current)
        data["live"]["last_sample_age_ms"] = None if self.last_sample_ms is None else now - self.last_sample_ms
        return data

    def device_info(self, now):
        sample_time = self.last_test_sample_ms if self.test_model is not None else self.last_sample_ms
        return {
            "wifi_connected": self.wifi_connected,
            "wifi_state": self.wifi_state,
            "ip": self.ip,
            "uptime_ms": now,
            "last_sample_age_ms": None if sample_time is None else now - sample_time,
            "stale_sample_ms": C.STALE_SAMPLE_MS,
            "simulation": bool(C.SIMULATION or self.test_model is not None),
            "base_simulation": C.SIMULATION,
            "free_heap_bytes": gc.mem_free() if hasattr(gc, "mem_free") else None,
            "firmware_version": FIRMWARE_VERSION,
        }

    def admin_status(self, token, now, touch=True):
        self.update_admin(now)
        data = self.admin.private_status(token, now, touch=touch)
        data["device"] = self.device_info(now)
        data["live"] = dict(self.model.current)
        return data

    async def read_headers(self, reader):
        received = b""
        while b"\r\n\r\n" not in received:
            chunk = await reader.read(min(256, C.HTTP_HEADER_LIMIT - len(received)))
            if not chunk:
                raise ValueError("Incomplete HTTP request")
            received += chunk
            if len(received) >= C.HTTP_HEADER_LIMIT and b"\r\n\r\n" not in received:
                raise ValueError("HTTP request headers too large")
        raw_headers, remainder = received.split(b"\r\n\r\n", 1)
        lines = raw_headers.split(b"\r\n")
        headers = {}
        for raw_line in lines[1:]:
            if b":" not in raw_line or raw_line[:1] in (b" ", b"\t"):
                raise ValueError("Malformed HTTP header")
            name, value = raw_line.split(b":", 1)
            name = name.decode("ascii").lower()
            if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in name):
                raise ValueError("Malformed HTTP header name")
            value = value.strip().decode("ascii")
            if name in headers:
                # Do not accept ambiguous auth, host, origin or length headers.
                raise ValueError("Duplicate HTTP header")
            headers[name] = value
        if "transfer-encoding" in headers:
            raise ValueError("Transfer-Encoding is not supported")
        if "content-length" in headers:
            length = headers["content-length"]
            if not length or any(ch < "0" or ch > "9" for ch in length):
                raise ValueError("Invalid Content-Length")
        return lines[0], headers, remainder

    async def read_json_body(self, reader, headers, remainder):
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise AdminError(415, "관리자 요청은 JSON 형식이어야 합니다.")
        if "content-length" not in headers:
            raise AdminError(411, "요청 길이가 필요합니다.")
        length = int(headers["content-length"])
        if length < 2 or length > ADMIN_BODY_LIMIT:
            raise AdminError(413, "요청 본문이 너무 크거나 비어 있습니다.")
        if len(remainder) > length:
            raise AdminError(400, "요청 길이가 맞지 않습니다.")
        body = remainder
        while len(body) < length:
            chunk = await reader.read(length - len(body))
            if not chunk:
                raise AdminError(400, "요청 본문을 끝까지 받지 못했습니다.")
            body += chunk
        try:
            value = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AdminError(400, "JSON 형식을 확인하세요.")
        if not isinstance(value, dict):
            raise AdminError(400, "JSON 객체가 필요합니다.")
        return value

    @staticmethod
    def admin_token(headers):
        authorization = headers.get("authorization", "")
        parts = authorization.split()
        return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""

    @staticmethod
    def check_origin(headers):
        origin = headers.get("origin")
        if origin is not None:
            host = headers.get("host", "")
            if not host or origin != "http://" + host:
                raise AdminError(403, "피코 대시보드에서 직접 요청하세요.")
        if headers.get("sec-fetch-site") == "cross-site":
            raise AdminError(403, "다른 사이트의 관리자 요청은 허용하지 않습니다.")

    async def write_bytes(self, writer, body):
        for position in range(0, len(body), 512):
            writer.write(body[position:position + 512])
            await asyncio.wait_for_ms(writer.drain(), C.HTTP_WRITE_TIMEOUT_MS)

    async def send_headers(self, writer, status, content_type, length, filename=None, allow="GET"):
        header = (
            "HTTP/1.1 " + status + "\r\nContent-Type: " + content_type +
            "\r\nContent-Length: " + str(length) +
            "\r\nCache-Control: no-store\r\nConnection: close" +
            "\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
        )
        if filename:
            header += 'Content-Disposition: attachment; filename="' + filename + '"\r\n'
        if status.startswith("405 "):
            header += "Allow: " + allow + "\r\n"
        if status.startswith("401 "):
            header += 'WWW-Authenticate: Bearer realm="Pico admin"\r\n'
        await self.write_bytes(writer, (header + "\r\n").encode("utf-8"))

    async def respond(self, writer, body, content_type="text/plain; charset=utf-8",
                      status="200 OK", filename=None, allow="GET"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        await self.send_headers(writer, status, content_type, len(body), filename, allow)
        await self.write_bytes(writer, body)

    async def respond_json(self, writer, data, status_code=200, allow="GET"):
        labels = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                  404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                  411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
                  429: "Too Many Requests", 503: "Service Unavailable"}
        status = str(status_code) + " " + labels.get(status_code, "Error")
        await self.respond(writer, json.dumps(data), "application/json; charset=utf-8", status, allow=allow)

    async def handle_admin(self, method, path, headers, remainder, reader, writer):
        get_paths = (b"/admin/status", b"/admin/test-history.csv", b"/admin/audit.csv")
        post_paths = (b"/admin/login", b"/admin/test", b"/admin/stop", b"/admin/logout")
        try:
            if path not in get_paths + post_paths:
                raise AdminError(404, "관리자 경로를 찾을 수 없습니다.")
            expected = b"GET" if path in get_paths else b"POST"
            if method != expected:
                await self.respond_json(writer, {"error": "요청 방식을 확인하세요."}, 405,
                                        allow=expected.decode())
                return
            self.check_origin(headers)
            now = self.clock.ms()
            self.update_admin(now)
            token = self.admin_token(headers)
            if path != b"/admin/login":
                self.admin.require(token, now)
            if method == b"POST":
                body = await asyncio.wait_for_ms(
                    self.read_json_body(reader, headers, remainder), C.HTTP_HEADER_TIMEOUT_MS)
                now = self.clock.ms()
                self.update_admin(now)
                if path == b"/admin/login":
                    result = self.admin.login(body.get("password"), now)
                elif path == b"/admin/test":
                    # Require usable sampling and preserve a genuine live alarm.
                    if self.last_sample_ms is None or self.samples_stale(now):
                        raise AdminError(409, "센서 측정이 갱신된 뒤 시험을 시작하세요.")
                    if not C.SIMULATION and self.model.current["alarm_level"] in ("warning", "danger"):
                        raise AdminError(409, "실제 센서 경보가 우선입니다. 현재 주의·위험 상태를 확인하세요.")
                    self.admin.start_test(token, body, now, C)
                    self.apply_outputs(now)
                    result = self.admin_status(token, now)
                elif path == b"/admin/stop":
                    self.admin.stop_test(token, now)
                    self.apply_outputs(now)
                    result = self.admin_status(token, now)
                else:
                    result = self.admin.logout(token, now)
                if path in (b"/admin/login", b"/admin/logout"):
                    self.apply_outputs(now)
                await self.respond_json(writer, result)
            elif path == b"/admin/status":
                await self.respond_json(writer, self.admin_status(token, now, touch=True))
            elif path == b"/admin/test-history.csv":
                model = self.test_model if self.test_model is not None else self.last_test_model
                if model is None:
                    raise AdminError(404, "아직 보관된 가상 시험 기록이 없습니다.")
                data = model.snapshot()
                await self.respond(writer, make_csv(data, "history"), "text/csv; charset=utf-8",
                                   filename="mq2_test_" + model.boot_id + ".csv")
            else:
                data = self.admin.private_status(token, now)
                rows = [["기록ID", "부팅후시간(초)", "작업", "내용", "시험ID"]]
                for event in data["audit"]:
                    rows.append([event["id"], event["time_ms"] / 1000, event["action"],
                                 event["message"], event["test_id"]])
                content = "\ufeff" + "\r\n".join(",".join(csv_cell(x) for x in row) for row in rows) + "\r\n"
                await self.respond(writer, content, "text/csv; charset=utf-8", filename="mq2_admin_audit.csv")
        except AdminError as exc:
            payload = {"error": exc.message}
            if getattr(exc, "retry_after", None) is not None:
                payload["retry_after"] = exc.retry_after
            await self.respond_json(writer, payload, exc.status)

    async def serve_dashboard(self, writer):
        try:
            length = os.stat("dashboard.html")[6]
            file = open("dashboard.html", "rb")
        except OSError:
            await self.respond(writer, "dashboard.html 파일을 Pico의 루트 폴더에 저장하세요.",
                               status="503 Service Unavailable")
            return
        try:
            await self.send_headers(writer, "200 OK", "text/html; charset=utf-8", length)
            while True:
                chunk = file.read(512)
                if not chunk:
                    break
                await self.write_bytes(writer, chunk)
        finally:
            file.close()

    async def handle_client(self, reader, writer):
        counted = False
        try:
            if self.active_clients >= C.HTTP_MAX_CLIENTS:
                # Close excess connections immediately; do not create more waiting writers.
                return
            self.active_clients += 1
            counted = True
            try:
                line, headers, remainder = await asyncio.wait_for_ms(self.read_headers(reader), C.HTTP_HEADER_TIMEOUT_MS)
                parts = line.split()
                if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
                    raise ValueError("Invalid HTTP request")
                method, path = parts[0], parts[1].split(b"?", 1)[0]
            except ValueError:
                await self.respond(writer, "잘못되었거나 너무 큰 요청입니다.", status="400 Bad Request")
                return
            if path.startswith(b"/admin/"):
                await self.handle_admin(method, path, headers, remainder, reader, writer)
            elif method != b"GET":
                await self.respond(writer, "GET만 지원합니다.", status="405 Method Not Allowed")
            elif path in (b"/", b"/index.html"):
                await self.serve_dashboard(writer)
            elif path == b"/data":
                await self.respond(writer, json.dumps(self.snapshot()), "application/json; charset=utf-8")
            elif path in (b"/history.csv", b"/events.csv"):
                kind = "events" if path == b"/events.csv" else "history"
                data = self.snapshot()
                name = "mq2_" + kind + "_" + data["boot_id"] + ".csv"
                await self.respond(writer, make_csv(data, kind), "text/csv; charset=utf-8", filename=name)
            elif path == b"/favicon.ico":
                await self.respond(writer, b"", content_type="image/x-icon")
            else:
                await self.respond(writer, "페이지를 찾을 수 없습니다.", status="404 Not Found")
        except (OSError, asyncio.TimeoutError):
            pass  # A disconnected/slow browser must not stop local measurement.
        except Exception as exc:
            print("HTTP 요청 처리 오류:", type(exc).__name__)
        finally:
            if counted:
                self.active_clients -= 1
            try:
                writer.close()
                await asyncio.wait_for_ms(writer.wait_closed(), 500)
            except Exception:
                pass

    async def server_loop(self):
        while True:
            if self.wifi_connected and self.server is None:
                try:
                    self.server = await asyncio.start_server(
                        self.handle_client, "0.0.0.0", C.HTTP_PORT, backlog=C.HTTP_MAX_CLIENTS)
                    print("웹 서버 준비 완료")
                except OSError as exc:
                    print("웹 서버 재시도:", type(exc).__name__)
            elif not self.wifi_connected and self.server is not None:
                self.server.close()
                try:
                    await self.server.wait_closed()
                except OSError:
                    pass
                self.server = None
            await asyncio.sleep(1)

    async def maintenance_loop(self):
        while True:
            await asyncio.sleep(30)
            gc.collect()

    async def run(self):
        print("가스 누출 경보기 v" + FIRMWARE_VERSION)
        print("입력 모드:", "가상 시험 — 실제 가스를 측정하지 않습니다." if C.SIMULATION else "실제 ADC")
        print("ADC 기준은 가스 농도(ppm)가 아닙니다. 예열 안내 종료도 교정 완료가 아닙니다.")
        print("관리자 모드:", "대시보드의 관리자 모드 버튼을 사용하세요." if self.admin.enabled else "개인 admin_config.py 설정이 필요합니다.")
        tasks = [asyncio.create_task(fn()) for fn in (
            self.sensor_loop, self.output_loop, self.wifi_loop,
            self.server_loop, self.maintenance_loop)]
        try:
            await asyncio.gather(*tasks)
        except Exception as exc:
            print("프로그램 오류. 전원/설정을 점검하세요:", type(exc).__name__)
            for task in tasks:
                task.cancel()
            if self.server is not None:
                self.server.close()
            while True:
                self.outputs.apply("fault", self.clock.ms(), False)
                await asyncio.sleep_ms(50)
        finally:
            for task in tasks:
                task.cancel()
            if self.server is not None:
                self.server.close()
            self.outputs.off()


if __name__ == "__main__":
    try:
        asyncio.run(System().run())
    except KeyboardInterrupt:
        print("측정을 중지했습니다. 다시 실행하려면 Pico를 재시작하세요.")
    finally:
        asyncio.new_event_loop()
