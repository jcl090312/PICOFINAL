"""PC virtual preview using the SAME HTTP/admin/runtime code as the Pico.

    python tools/preview.py
    python tools/preview.py --admin

Open http://127.0.0.1:8765. Extra pip packages are not required.
--admin prompts for a temporary preview password without echoing it.
For automated local checks only, PICO_PREVIEW_PASSWORD can supply that password.
All sensor values and GPIO outputs here are virtual; no Pico is accessed.
"""

import argparse
import asyncio
import getpass
import hashlib
import importlib.util
import os
import sys
import time as host_time
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TICK_PERIOD = 1 << 30


class VirtualPin:
    """In-memory GPIO value; never controls a computer or attached board."""

    OUT = 1

    def __init__(self, number, mode=None, value=0):
        self.number = number
        self.mode = mode
        self.level = value

    def value(self, value=None):
        if value is not None:
            self.level = value
        return self.level


class VirtualADC:
    def __init__(self, pin):
        self.pin = pin

    def read_u16(self):
        # System.sensor_loop uses its own simulation sequence in this preview.
        return 10000


async def sleep_ms(milliseconds):
    await asyncio.sleep(milliseconds / 1000)


async def wait_for_ms(awaitable, milliseconds):
    return await asyncio.wait_for(awaitable, milliseconds / 1000)


def load_firmware():
    """Provide only compatibility APIs, without copying routes or auth logic."""
    machine = types.ModuleType("machine")
    machine.Pin, machine.ADC = VirtualPin, VirtualADC
    micro_time = types.ModuleType("time")
    for name in dir(host_time):
        setattr(micro_time, name, getattr(host_time, name))
    micro_time.ticks_ms = lambda: int(host_time.monotonic() * 1000) % TICK_PERIOD
    micro_time.ticks_diff = lambda a, b: (a - b + TICK_PERIOD // 2) % TICK_PERIOD - TICK_PERIOD // 2
    micro_async = types.ModuleType("uasyncio")
    for name in ("sleep", "create_task", "gather", "run", "start_server",
                 "new_event_loop", "TimeoutError"):
        setattr(micro_async, name, getattr(asyncio, name))
    micro_async.sleep_ms, micro_async.wait_for_ms = sleep_ms, wait_for_ms

    compatibility = {"machine": machine, "time": micro_time, "uasyncio": micro_async}
    previous = {name: sys.modules.get(name) for name in compatibility}
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        sys.modules.update(compatibility)
        spec = importlib.util.spec_from_file_location("pico_pc_preview_firmware", ROOT / "main.py")
        firmware = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(firmware)
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return firmware


def temporary_admin_settings(password):
    """Make one in-memory credential; never save or print password/hash/salt."""
    salt = os.urandom(16)
    return {
        "PASSWORD_SALT_HEX": salt.hex(),
        "PASSWORD_HASH_HEX": hashlib.sha256(salt + password.encode("utf-8")).hexdigest(),
    }


async def run_preview(port, password=None):
    firmware = load_firmware()
    firmware.C.SIMULATION = True
    firmware.C.CONSOLE_LOG = False
    firmware.C.HTTP_PORT = port
    firmware.FIRMWARE_VERSION = "2.2.0-PC-preview"
    # Desktop preview must never initialise real I2C/UART devices.
    if firmware.P is not None:
        firmware.P.SHT40_ENABLED = False
        firmware.P.HUSKY_ENABLED = False
        firmware.P.MP3_ENABLED = False
    system = firmware.System()
    if password is not None:
        system.admin = firmware.AdminController(temporary_admin_settings(password))
    system.wifi_connected = True
    system.wifi_state = "pc_preview"
    system.ip = "127.0.0.1"

    # main.py reads dashboard.html relative to the firmware directory.
    os.chdir(ROOT)
    server = await asyncio.start_server(
        system.handle_client, "127.0.0.1", port,
        backlog=firmware.C.HTTP_MAX_CLIENTS,
    )
    system.server = server
    tasks = [asyncio.create_task(fn()) for fn in (
        system.sensor_loop, system.output_loop, system.maintenance_loop)]
    tasks.append(asyncio.create_task(server.serve_forever()))
    print("PC 가상 미리보기: 센서 값과 LED·부저 출력은 모두 가상입니다.", flush=True)
    print("같은 main.py의 관리자 로그인·시험·CSV 기능을 실행합니다.", flush=True)
    print("접속: http://127.0.0.1:{}  /  종료: Ctrl+C".format(port), flush=True)
    if system.admin.enabled:
        print("관리자 기능 사용 가능. 설정한 미리보기 또는 로컬 관리자 비밀번호로 로그인하세요.", flush=True)
    else:
        print("관리자 기능 꺼짐. --admin 옵션으로 재실행하면 임시 비밀번호를 설정할 수 있습니다.", flush=True)
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()
        system.outputs.off()


def main():
    parser = argparse.ArgumentParser(description="Pico 대시보드와 관리자 기능의 PC 가상 미리보기")
    parser.add_argument("--port", type=int, default=8765, help="로컬 포트 번호 (기본 8765)")
    parser.add_argument("--admin", action="store_true", help="임시 관리자 비밀번호를 숨겨서 입력")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("포트는 1~65535 범위여야 합니다.")
    password = os.environ.pop("PICO_PREVIEW_PASSWORD", None)
    if args.admin and password is None:
        password = getpass.getpass("PC 미리보기용 임시 관리자 비밀번호: ")
    if password is not None and not 1 <= len(password) <= 128:
        parser.error("임시 관리자 비밀번호는 1~128자여야 합니다.")
    try:
        asyncio.run(run_preview(args.port, password))
    except KeyboardInterrupt:
        print("PC 가상 미리보기를 종료했습니다.")
    except OSError as exc:
        # Do not print exception text that might include user-controlled data.
        print("미리보기 서버를 시작하지 못했습니다. 포트 사용 여부를 확인하세요. ({}).".format(
            type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
