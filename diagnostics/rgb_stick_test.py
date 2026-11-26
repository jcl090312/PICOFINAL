# 전원 OFF 후 RGB Stick만 D16에 연결, 실드 3.3V. main.py를 덮어쓰지 않음.
from machine import Pin
import neopixel
import time
led = neopixel.NeoPixel(Pin(16, Pin.OUT), 10, timing=(350, 900, 800, 450))
try:
    for color in ((0,20,0), (20,12,0), (20,0,0), (0,0,20)):
        led.fill(color)
        led.write()
        time.sleep(2)
finally:
    led.fill((0,0,0))
    led.write()
