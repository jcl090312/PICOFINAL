"""Pico W / Pico 2 W + MQ-2 교육용 가스 누출 경보기.

필수 파일: main.py, config.py, alarm_logic.py, dashboard.html, wifi_config.py
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

try:
    import network
except ImportError:
    network = None

try:
    from wifi_config import WIFI_SSID, WIFI_PASSWORD
except ImportError:
    WIFI_SSID, WIFI_PASSWORD = "", ""

FIRMWARE_VERSION = "2.0.0"


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

    def apply(self, state, now_ms, wifi_connected=False):
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
        self.set_pin(self.buzzer, buzz, C.BUZZER_ACTIVE_HIGH)
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
            state = self.model.current["state"]
            if self.last_sample_ms is not None and now - self.last_sample_ms > C.STALE_SAMPLE_MS:
                # A stopped sample stream cannot continue to display normal green.
                state = "danger" if self.model.current["alarm_level"] == "danger" else "fault"
            self.outputs.apply(state, now, self.wifi_connected)
            await asyncio.sleep_ms(50)

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
        data = self.model.snapshot()
        now = self.clock.ms()
        data["boot_id"] = self.model.boot_id
        data["device"] = {
            "wifi_connected": self.wifi_connected,
            "wifi_state": self.wifi_state,
            "ip": self.ip,
            "uptime_ms": now,
            "last_sample_age_ms": None if self.last_sample_ms is None else now - self.last_sample_ms,
            "stale_sample_ms": C.STALE_SAMPLE_MS,
            "simulation": C.SIMULATION,
            "firmware_version": FIRMWARE_VERSION,
        }
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
        return received.split(b"\r\n", 1)[0]

    async def write_bytes(self, writer, body):
        for position in range(0, len(body), 512):
            writer.write(body[position:position + 512])
            await asyncio.wait_for_ms(writer.drain(), C.HTTP_WRITE_TIMEOUT_MS)

    async def send_headers(self, writer, status, content_type, length, filename=None):
        header = (
            "HTTP/1.1 " + status + "\r\nContent-Type: " + content_type +
            "\r\nContent-Length: " + str(length) +
            "\r\nCache-Control: no-store\r\nConnection: close" +
            "\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
        )
        if filename:
            header += 'Content-Disposition: attachment; filename="' + filename + '"\r\n'
        if status.startswith("405 "):
            header += "Allow: GET\r\n"
        await self.write_bytes(writer, (header + "\r\n").encode("utf-8"))

    async def respond(self, writer, body, content_type="text/plain; charset=utf-8",
                      status="200 OK", filename=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        await self.send_headers(writer, status, content_type, len(body), filename)
        await self.write_bytes(writer, body)

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
                line = await asyncio.wait_for_ms(self.read_headers(reader), C.HTTP_HEADER_TIMEOUT_MS)
                parts = line.split()
                if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
                    raise ValueError("Invalid HTTP request")
                method, path = parts[0], parts[1].split(b"?", 1)[0]
            except ValueError:
                await self.respond(writer, "잘못되었거나 너무 큰 요청입니다.", status="400 Bad Request")
                return
            if method != b"GET":
                await self.respond(writer, "GET만 지원합니다.", status="405 Method Not Allowed")
            elif path in (b"/", b"/index.html"):
                await self.serve_dashboard(writer)
            elif path == b"/data":
                await self.respond(writer, json.dumps(self.snapshot()), "application/json; charset=utf-8")
            elif path in (b"/history.csv", b"/events.csv"):
                kind = "events" if path == b"/events.csv" else "history"
                data = self.snapshot()
                name = "mq2_" + kind + "_" + self.model.boot_id + ".csv"
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
