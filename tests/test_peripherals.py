"""Behavioural requirements for extra-device failure isolation and sound gating."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import peripheral_config as defaults
import peripherals
from admin_control import AdminController, AdminError


def settings(**overrides):
    values = {name: getattr(defaults, name) for name in dir(defaults) if name.isupper()}
    values.update(overrides)
    return values


class Clock:
    def __init__(self, now=0):
        self.now = now

    def ms(self):
        return self.now


def context(**overrides):
    result = {"real_mode": True, "alarm_level": "normal", "gas_stale": False,
              "test": None, "test_alarm": None}
    result.update(overrides)
    return result


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()
        self.detected = {"status": "ok", "present": True}

    def test_real_audio_requires_both_gas_and_confirmed_recent_person(self):
        for gas in ("normal", "warning", "danger"):
            for person in (True, False, None):
                result = peripherals.choose_audio(context(alarm_level=gas), {"status": "ok", "present": person}, self.config)
                self.assertEqual(result["eligible"], gas != "normal" and person is True)
        self.assertEqual(peripherals.choose_audio(context(alarm_level="warning"), self.detected, self.config)["track"], "0001.mp3")
        self.assertEqual(peripherals.choose_audio(context(alarm_level="danger"), self.detected, self.config)["track"], "0002.mp3")

    def test_camera_error_staleness_and_unlearned_id_cannot_authorize_audio(self):
        for status in ("starting", "error", "stale", "unlearned", "disabled"):
            result = peripherals.choose_audio(context(alarm_level="danger"), {"status": status, "present": True}, self.config)
            self.assertFalse(result["eligible"])
        self.assertFalse(peripherals.choose_audio(context(alarm_level="danger", gas_stale=True), self.detected, self.config)["eligible"])

    def test_global_simulation_and_optional_auto_switch_are_respected(self):
        self.assertFalse(peripherals.choose_audio(context(real_mode=False, alarm_level="danger"), self.detected, self.config)["eligible"])
        self.assertFalse(peripherals.choose_audio(context(alarm_level="danger"), self.detected, settings(AUTO_AUDIO_ENABLED=False))["eligible"])

    def test_virtual_audio_needs_explicit_opt_in_person_and_alarm(self):
        for kind in ("sensor", "sequence"):
            for enabled in (False, True):
                for person in (False, True):
                    for level in ("normal", "warning", "danger"):
                        job = {"kind": kind, "id": "job-1", "audio": enabled, "person": person}
                        result = peripherals.choose_audio(context(test=job, test_alarm=level), {"status": "error", "present": None}, self.config)
                        self.assertEqual(result["eligible"], enabled and person and level != "normal")
                        self.assertEqual(result["mode"], "simulation")

    def test_real_alarm_preempts_even_manual_and_simulated_audio(self):
        for job in ({"kind": "audio", "id": "a"}, {"kind": "sensor", "id": "s", "audio": True, "person": True}):
            result = peripherals.choose_audio(context(alarm_level="danger", test=job, test_alarm="warning"), self.detected, self.config)
            self.assertEqual((result["mode"], result["track"]), ("live", "0002.mp3"))
            result = peripherals.choose_audio(context(alarm_level="danger", test=job), {"status": "ok", "present": False}, self.config)
            self.assertFalse(result["eligible"])

    def test_manual_audio_does_not_need_camera_or_fake_gas(self):
        result = peripherals.choose_audio(context(test={"kind": "audio", "id": "a"}), {"status": "disabled", "present": None}, self.config)
        self.assertEqual((result["eligible"], result["mode"], result["track"]), (True, "manual", "0003.mp3"))


class SensorStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.manager = peripherals.PeripheralManager(settings(), self.clock, context, reserved_pins=(14, 15, 16, 26))

    def frame(self, number, matches=1, training=False):
        return {"frame_number": number, "matching_count": matches, "learned_count": 1,
                "person_detected": None if training else matches > 0, "training_required": training,
                "blocks": [{"id": 1}] if matches else []}

    def test_distinct_frames_confirmation_hold_and_clear(self):
        self.manager.accept_person(self.frame(1), 0)
        self.assertIsNone(self.manager.person_snapshot(0)["present"])
        self.manager.accept_person(self.frame(1), 100)
        self.assertIsNone(self.manager.person_snapshot(100)["present"])
        self.manager.accept_person(self.frame(2), 500)
        self.assertTrue(self.manager.person_snapshot(500)["present"])
        self.manager.accept_person(self.frame(3, 0), 1000)
        self.assertTrue(self.manager.person_snapshot(1000)["present"])
        self.manager.accept_person(self.frame(4, 0), 3500)
        self.assertFalse(self.manager.person_snapshot(3500)["present"])

    def test_repeated_frame_cannot_keep_detection_fresh(self):
        self.manager.accept_person(self.frame(1), 0)
        self.manager.accept_person(self.frame(2), 500)
        self.manager.accept_person(self.frame(2), 4000)
        data = self.manager.person_snapshot(4000)
        self.assertEqual(data["status"], "stale")
        self.assertIsNone(data["present"])

    def test_camera_failure_is_unknown_and_learning_confirmation_is_required(self):
        self.manager.accept_person(self.frame(1, training=True), 0)
        self.assertEqual(self.manager.person_snapshot(0)["status"], "unlearned")
        self.assertIsNone(self.manager.person_snapshot(0)["present"])
        self.manager.person["status"] = "error"
        self.assertIsNone(self.manager.person_snapshot(100)["present"])

    def test_stale_temperature_is_not_rendered_as_current_or_zero(self):
        self.manager.environment.update(status="ok", temperature_c=23.5, humidity_percent=45)
        self.manager._env_time = 0
        data = self.manager.snapshot(7000)["environment"]
        self.assertEqual(data["status"], "stale")
        self.assertIsNone(data["temperature_c"])
        self.assertIsNone(data["humidity_percent"])

    def test_pin_collision_disables_only_conflicting_addon(self):
        manager = peripherals.PeripheralManager(settings(SHT40_SDA_PIN=14), self.clock, context, reserved_pins=(14, 15, 16, 26))
        self.assertIn("environment", manager._validation)
        self.assertNotIn("audio", manager._validation)
        self.assertEqual(manager.environment["status"], "error")


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_during_play_ack_immediately_requests_stop(self):
        clock = Clock()
        current = context(test={"kind": "audio", "id": "job"})
        calls = []
        class Player:
            async def configure(self, volume):
                calls.append("configure")
                return {"command": "configure", "acknowledged": True}
            async def play(self, filename):
                calls.append("play")
                current["test"] = None
                return {"command": "play", "track": filename, "acknowledged": True}
            async def stop(self):
                calls.append("stop")
                return {"command": "stop", "acknowledged": True}
        manager = peripherals.PeripheralManager(settings(), clock, lambda: current, factories={"audio": Player})
        async def tick(ms):
            clock.now += ms
            if calls and calls[-1] == "stop":
                manager.closed = True
            await asyncio.sleep(0)
        with patch.object(peripherals, "sleep_ms", tick):
            await manager.audio_loop()
        self.assertEqual(calls, ["configure", "play", "stop"])

    async def test_repeats_are_spaced_and_danger_escalation_does_not_wait(self):
        clock = Clock()
        current = context(test={"kind": "sensor", "id": "job", "audio": True, "person": True}, test_alarm="warning")
        plays = []
        class Player:
            async def configure(self, volume):
                return {"command": "configure", "acknowledged": True}
            async def play(self, name):
                plays.append((name, clock.now))
                return {"command": "play", "track": name, "acknowledged": True}
            async def stop(self):
                return {"command": "stop", "acknowledged": True}
        manager = peripherals.PeripheralManager(settings(), clock, lambda: current, factories={"audio": Player})
        async def tick(ms):
            clock.now += ms
            if clock.now >= 4000:
                current["test_alarm"] = "danger"
            if clock.now >= 19500:
                manager.closed = True
            await asyncio.sleep(0)
        with patch.object(peripherals, "sleep_ms", tick):
            await manager.audio_loop()
        self.assertEqual([name for name, now in plays], ["0001.mp3", "0002.mp3", "0002.mp3"])
        self.assertLess(plays[1][1] - plays[0][1], 15000)
        self.assertGreaterEqual(plays[2][1] - plays[1][1], 15000)

    async def test_disconnected_environment_does_not_cancel_other_work(self):
        clock = Clock()
        counters = {"samples": 0, "reads": 0}
        class Sensor:
            async def read(self):
                counters["reads"] += 1
                raise OSError("mock missing sensor")
        manager = peripherals.PeripheralManager(settings(), clock, context, factories={"environment": Sensor})
        async def tick(ms):
            clock.now += ms
            counters["samples"] += 1
            if counters["samples"] >= 3:
                manager.closed = True
            await asyncio.sleep(0)
        with patch.object(peripherals, "sleep_ms", tick):
            await manager.environment_loop()
        self.assertEqual(counters["reads"], 3)
        self.assertEqual(manager.environment["status"], "error")
        self.assertIsNone(manager.environment["temperature_c"])


if __name__ == "__main__":
    unittest.main()

