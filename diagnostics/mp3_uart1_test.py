# Thonny 새 탭에서만 실행. main.py를 덮어쓰지 않습니다.
# 안전한 5V 공급/3.3V UART 레벨 변환, 정상 FAT32 SD카드, 스피커 연결 후.
from machine import UART, Pin
import time

mp3 = UART(1, 115200, tx=Pin(4), rx=Pin(5), timeout=0, rxbuf=1024)

def send(command, wait_ms=1000):
    print("TX:", command)
    mp3.write((command + "\r\n").encode())
    start = time.ticks_ms()
    result = b""
    while time.ticks_diff(time.ticks_ms(), start) < wait_ms:
        if mp3.any():
            data = mp3.read()
            if data:
                print("RX:", data)
                result += data
        time.sleep_ms(20)
    return result

time.sleep(3)
send("AT+GVER?")
send("AT+VOL=7")
send("AT+REPEATMODE=5")
try:
    for filename in ("0001.mp3", "0002.mp3", "0003.mp3"):
        print("PLAY:", filename, "- listen to speaker")
        reply = send("AT+SPLAY=sd0," + filename, 8000)
        if b"ERR:" in reply:
            print("Module reported a playback error; verify card.")
        elif b"+PLAY:START" in reply:
            print("Module reports playback start; audible sound must be checked.")
        else:
            print("No playback-start confirmation; check actual sound and log.")
finally:
    send("AT+STOP")
