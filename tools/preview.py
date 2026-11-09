"""PC용 가상 대시보드. 실제 Pico, ADC, LED, Wi-Fi를 시험하는 프로그램이 아닙니다.

GitHub 폴더에서: python tools/preview.py
브라우저에서: http://127.0.0.1:8765
추가 pip 패키지가 필요하지 않습니다.
"""

import csv
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
from alarm_logic import AlarmModel

SETTINGS = {name: getattr(config, name) for name in dir(config) if name.isupper()}
SETTINGS["SIMULATION"] = True
MODEL = AlarmModel(SETTINGS, boot_id="pc-demo")
LOCK = threading.Lock()
STARTED = time.monotonic()
LAST_SAMPLE_MS = None
STOP = threading.Event()


def sample_value(index):
    index %= 80
    if index < 10:
        return 10000
    if index < 20:
        return 22000
    if index < 30:
        return 34000
    if index < 40:
        return 24000
    if index < 50:
        return 10000
    if index < 62:
        return 0
    return 10000


def sample_loop():
    global LAST_SAMPLE_MS
    index = 0
    while not STOP.is_set():
        now = int((time.monotonic() - STARTED) * 1000)
        with LOCK:
            MODEL.update(sample_value(index), now, source="simulation")
            LAST_SAMPLE_MS = now
        index += 1
        STOP.wait(SETTINGS["SAMPLE_INTERVAL_MS"] / 1000)


def snapshot():
    with LOCK:
        # Detach the bounded history from the sampling thread.
        data = json.loads(json.dumps(MODEL.snapshot(), ensure_ascii=False))
        now = int((time.monotonic() - STARTED) * 1000)
        data["device"] = {
            "wifi_connected": True,
            "wifi_state": "pc_preview",
            "ip": "127.0.0.1",
            "uptime_ms": now,
            "last_sample_age_ms": None if LAST_SAMPLE_MS is None else now - LAST_SAMPLE_MS,
            "stale_sample_ms": max(config.STALE_SAMPLE_MS, config.SAMPLE_INTERVAL_MS * 3),
            "simulation": True,
            "firmware_version": "2.0.0-pc-preview",
        }
        return data


def as_csv(data, kind):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    if kind == "events":
        writer.writerow(["부팅ID", "기록ID", "경과시간(초)", "ADC값", "상태", "종류", "내용", "입력출처"])
        for e in data["events"]:
            writer.writerow([e["boot_id"], e["id"], e["time"], e["raw"], e["status"],
                             e["type"], e["message"], e["source"]])
    else:
        writer.writerow(["부팅ID", "측정ID", "경과시간(초)", "ADC값", "변화량", "상태", "신호점검", "입력출처"])
        for s in data["samples"]:
            writer.writerow([data["boot_id"], s["id"], s["time"], s["raw"], s["change"],
                             s["status"], s["health"], s["source"]])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        filename = None
        if path in ("/", "/index.html"):
            body = (ROOT / "dashboard.html").read_bytes()
            content_type = "text/html; charset=utf-8"
        elif path == "/data":
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif path in ("/history.csv", "/events.csv"):
            kind = "events" if path == "/events.csv" else "history"
            body = as_csv(snapshot(), kind)
            content_type = "text/csv; charset=utf-8"
            filename = "mq2_" + kind + "_pc-demo.csv"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if args and str(args[0]).startswith("GET /data "):
            return
        super().log_message(format, *args)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(target=sample_loop, daemon=True)
    thread.start()
    print("PC 가상 시험 전용: 실제 센서 값이 아닙니다.")
    print("접속: http://127.0.0.1:8765  /  종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
