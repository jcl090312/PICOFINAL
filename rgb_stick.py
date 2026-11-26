"""Grove RGB LED Stick, 10 WS2813 Mini; no GPIO HIGH/LOW emulation."""

class RGBStick:
    def __init__(self, pin, count=10, timing=(350, 900, 800, 450), brightness=20, pixels=None):
        if type(count) is not int or not 3 <= count <= 64:
            raise ValueError("LED count must be 3..64")
        if type(brightness) is not int or not 1 <= brightness <= 255:
            raise ValueError("LED brightness must be 1..255")
        if pixels is None:
            from machine import Pin
            import neopixel
            pixels = neopixel.NeoPixel(Pin(pin, Pin.OUT), count, timing=timing)
        self.pixels = pixels
        self.count = count
        self.colors = ((0, brightness, 0), (brightness, max(1, brightness * 3 // 5), 0), (brightness, 0, 0))
        self.last = None
        self.set_states((False, False, False))

    def set_states(self, states):
        # Single state: all pixels show that color. Multiple (admin ALL):
        # use distinct contiguous groups rather than mixing to white.
        active = [color for color, enabled in zip(self.colors, states) if enabled]
        if not active:
            active = [(0, 0, 0)]
        frame = tuple(active[i * len(active) // self.count] for i in range(self.count))
        if frame == self.last:
            return
        for i, color in enumerate(frame):
            self.pixels[i] = color
        self.pixels.write()
        self.last = frame

    def off(self):
        self.set_states((False, False, False))
