"""Generate original demonstration tones, then encode real MP3 with FFmpeg.

python tools/make_demo_audio.py
No speech, external audio recordings or downloaded sound effects are used.
The three resulting files belong on the MP3 module's microSD, not on Pico.
"""

from array import array
from pathlib import Path
import math
import shutil
import subprocess
import tempfile
import wave


ROOT = Path(__file__).resolve().parents[1]
RATE = 32000


def samples(kind):
    durations = {"warning": 2.4, "danger": 3.0, "test": 1.2}
    result = array("h")
    for index in range(round(durations[kind] * RATE)):
        t = index / RATE
        if kind == "warning":
            phase = t % 0.6
            active = phase < 0.22
            frequency = 700
            edge = min(phase, 0.22 - phase)
        elif kind == "danger":
            phase = t % 0.30
            active = phase < 0.24
            frequency = 880 if int(t / 0.3) % 2 == 0 else 1175
            edge = min(phase, 0.24 - phase)
        else:
            phase = t % 0.4
            active = phase < 0.18
            frequency = (523, 659, 784)[min(2, int(t / 0.4))]
            edge = min(phase, 0.18 - phase)
        envelope = max(0.0, min(1.0, edge / 0.015)) if active else 0.0
        value = 0.16 * envelope * math.sin(2 * math.pi * frequency * t)
        result.append(round(32767 * value))
    return result


def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("FFmpeg is required only to regenerate the supplied MP3 files.")
    output = ROOT / "sd_card"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gas-alarm-tones-") as scratch:
        for filename, kind in (("0001.mp3", "warning"), ("0002.mp3", "danger"), ("0003.mp3", "test")):
            wav = Path(scratch) / (kind + ".wav")
            data = samples(kind)
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(data.tobytes())
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(wav), "-map_metadata", "-1", "-codec:a", "libmp3lame",
                            "-b:a", "96k", "-write_xing", "0", "-id3v2_version", "0",
                            str(output / filename)], check=True)
            print(filename, kind, "tone generated")


if __name__ == "__main__":
    main()

