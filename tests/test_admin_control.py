"""Admin authentication and demonstration safety checks; no real credentials."""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_control import AdminController, AdminError


PASSWORD = "unit-test-only-password"
SALT = bytes(range(16))
ALARM = SimpleNamespace(WARNING_LEVEL=18000, DANGER_LEVEL=30000,
                        HYSTERESIS=2000, ADC_LOW_LIMIT=100, ADC_HIGH_LIMIT=65000)


def config(**overrides):
    result = {
        "PASSWORD_SALT_HEX": SALT.hex(),
        "PASSWORD_HASH_HEX": hashlib.sha256(SALT + PASSWORD.encode()).hexdigest(),
    }
    result.update(overrides)
    return result


class CounterRandom:
    def __init__(self):
        self.calls = 0

    def __call__(self, count):
        self.calls += 1
        return bytes([self.calls % 256]) * count


def controller(**overrides):
    return AdminController(config(**overrides), random_bytes=CounterRandom())


class AdminControlTests(unittest.TestCase):
    def assert_error(self, expected, function, *args, **kwargs):
        with self.assertRaises(AdminError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, expected)
        return caught.exception

    def authenticated(self, **overrides):
        admin = controller(**overrides)
        return admin, admin.login(PASSWORD, 0)["token"]

    def start(self, admin, token, kind="sensor", mode="warning", duration=30, now=0, **extra):
        spec = {"kind": kind, "duration": duration}
        if kind != "sequence":
            spec["mode"] = mode
        spec.update(extra)
        return admin.start_test(token, spec, now, ALARM)

    def test_missing_or_invalid_configuration_disables_admin(self):
        cases = (None, {}, config(PASSWORD_SALT_HEX=""),
                 config(PASSWORD_SALT_HEX="z" * 32), config(PASSWORD_HASH_HEX="0" * 63),
                 config(PASSWORD_HASH_HEX=None), config(SESSION_SECONDS=True),
                 config(MAX_TEST_SECONDS=121), config(TEST_LEASE_SECONDS=0))
        for settings in cases:
            with self.subTest(settings=settings):
                admin = AdminController(settings)
                self.assertFalse(admin.public_status(0)["enabled"])
                self.assert_error(503, admin.login, PASSWORD, 0)
                self.assert_error(503, admin.require, "anything", 0)

    def test_login_uses_salted_hash_and_returns_only_session_fields(self):
        admin = controller()
        response = admin.login(PASSWORD, 1234)
        self.assertEqual(set(response), {"token", "expires_in"})
        self.assertEqual(len(response["token"]), 48)
        self.assertEqual(response["expires_in"], 600)
        self.assertTrue(admin.require(response["token"], 1235))
        other = AdminController(config(PASSWORD_SALT_HEX="ff" * 16), CounterRandom())
        self.assert_error(401, other.login, PASSWORD, 0)

    def test_unicode_password_uses_utf8(self):
        password = "단위시험 전용 암호"
        setting = config(PASSWORD_HASH_HEX=hashlib.sha256(SALT + password.encode("utf-8")).hexdigest())
        admin = AdminController(SimpleNamespace(**setting), CounterRandom())
        self.assertEqual(len(admin.login(password, 0)["token"]), 48)

    def test_wrong_password_and_malformed_password_are_not_authenticated(self):
        for value in ("wrong", "", None, True, 123, b"password", "x" * 129, "\ud800"):
            with self.subTest(kind=type(value).__name__):
                admin = controller()
                self.assert_error(401, admin.login, value, 0)
                self.assertIsNone(admin._token)

    def test_fifth_wrong_attempt_locks_out_even_correct_password(self):
        admin = controller()
        for i in range(4):
            self.assert_error(401, admin.login, "wrong", i * 100)
        failure = self.assert_error(429, admin.login, "wrong", 400)
        self.assertEqual(failure.retry_after, 30)
        blocked = self.assert_error(429, admin.login, PASSWORD, 1400)
        self.assertEqual(blocked.retry_after, 29)
        token = admin.login(PASSWORD, 30400)["token"]
        self.assertTrue(admin.require(token, 30400))
        self.assert_error(401, admin.login, "wrong", 30401)

    def test_success_resets_failed_attempt_count(self):
        admin = controller()
        for _ in range(4):
            self.assert_error(401, admin.login, "wrong", 0)
        admin.login(PASSWORD, 1)
        for _ in range(4):
            self.assert_error(401, admin.login, "wrong", 2)

    def test_relogin_revokes_old_token_and_cancels_existing_test(self):
        admin, first = self.authenticated()
        self.start(admin, first)
        second = admin.login(PASSWORD, 1000)["token"]
        self.assertNotEqual(first, second)
        self.assert_error(401, admin.require, first, 1000)
        self.assertTrue(admin.require(second, 1000))
        self.assertIsNone(admin.test)
        self.assertIn("로그인", admin.public_status(1000)["stop_reason"])

    def test_logged_out_token_cannot_replay_commands(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        result = admin.logout(token, 1000)
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["test"]["active"])
        self.assert_error(401, self.start, admin, token, now=1001)
        self.assert_error(401, admin.private_status, token, 1001)
        self.assert_error(401, admin.logout, token, 1001)

    def test_touch_cannot_extend_absolute_session_expiry(self):
        admin, token = self.authenticated()
        self.assertEqual(admin.private_status(token, 590000)["expires_in"], 10)
        self.start(admin, token, duration=120, now=590000)
        self.assertEqual(admin.private_status(token, 599999)["expires_in"], 1)
        admin.tick(600000)
        self.assertIsNone(admin.test)
        self.assert_error(401, admin.require, token, 600000, touch=True)
        self.assertIn("세션 만료", admin.public_status(600000)["stop_reason"])

    def test_random_failure_never_uses_predictable_fallback(self):
        def failing(_count):
            raise OSError("random unavailable")
        for random_bytes in (failing, lambda n: b"short", lambda n: "x" * n, lambda n: None):
            admin = AdminController(config(), random_bytes)
            self.assert_error(503, admin.login, PASSWORD, 0)
            self.assertIsNone(admin._token)
            self.assertFalse(admin.public_status(0)["active"])

    def test_repeated_random_token_fails_closed_and_revokes_old_session(self):
        admin = AdminController(config(), lambda n: b"x" * n)
        token = admin.login(PASSWORD, 0)["token"]
        self.start(admin, token)
        self.assert_error(503, admin.login, PASSWORD, 1)
        self.assertIsNone(admin.test)
        self.assert_error(401, admin.require, token, 1)

    def test_invalid_token_types_and_unicode_fail_with_401(self):
        admin, token = self.authenticated()
        for candidate in (None, False, 1, b"token", "", "x" * 129, "\ud800", token[:-1] + "f"):
            with self.subTest(kind=type(candidate).__name__):
                self.assert_error(401, admin.require, candidate, 0)

    def test_public_polling_and_invalid_auth_do_not_keep_test_alive(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        for now in (1000, 5000, 11000):
            self.assertTrue(admin.public_status(now)["active"])
            self.assert_error(401, admin.require, "wrong", now, touch=True)
        admin.tick(12000)
        self.assertIsNone(admin.test)
        self.assertIn("연결 확인", admin.public_status(12000)["stop_reason"])
        self.assertTrue(admin.require(token, 12000))

    def test_authenticated_polling_keeps_lease_but_late_poll_cannot_restart_test(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        self.assertTrue(admin.private_status(token, 11000)["test"]["active"])
        self.assertTrue(admin.public_status(22000)["active"])
        self.assertFalse(admin.private_status(token, 23000)["test"]["active"])
        self.assertIsNone(admin.test)

    def test_test_duration_stops_despite_heartbeat(self):
        admin, token = self.authenticated()
        self.start(admin, token, duration=10)
        admin.private_status(token, 9999)
        admin.tick(10000)
        self.assertIsNone(admin.test)
        self.assertIn("시간 종료", admin.public_status(10000)["stop_reason"])

    def test_invalid_command_does_not_replace_active_job(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        original = dict(admin.test)
        for spec in (None, [], {}, {"kind": "unknown"},
                     {"kind": "sensor", "mode": "warning", "duration": True},
                     {"kind": "sensor", "mode": "warning", "duration": 121},
                     {"kind": "sensor", "mode": "warning", "duration": 10, "token": "unwanted"}):
            self.assert_error(400, admin.start_test, token, spec, 1, ALARM)
            self.assertEqual(admin.test, original)

    def test_sensor_durations_are_bounded_integers(self):
        admin, token = self.authenticated()
        for duration in (5, 10, 30, 60, 120):
            self.start(admin, token, duration=duration)
            self.assertEqual(admin.test["ends_ms"], duration * 1000)
        for duration in (None, -1, 0, 4, 121, True, 5.0, "10"):
            self.assert_error(400, self.start, admin, token, duration=duration)

    def test_led_presets_have_fixed_duration_and_expected_outputs(self):
        admin, token = self.authenticated()
        for mode, seconds, expected in (("green", 5, (True, False, False)),
                                        ("yellow", 5, (False, True, False)),
                                        ("red", 5, (False, False, True)),
                                        ("all", 3, (True, True, True))):
            self.start(admin, token, kind="led", mode=mode, duration=seconds)
            self.assertEqual(admin.led_values(0), expected)
            self.assertIsNone(admin.sample_value(0))
            self.assertEqual(admin.test["ends_ms"], seconds * 1000)
            self.assert_error(400, self.start, admin, token, kind="led", mode=mode, duration=seconds + 1)

    def test_led_sequence_is_three_seconds_per_colour_and_stops_at_nine(self):
        admin, token = self.authenticated()
        self.start(admin, token, kind="led", mode="sequence", duration=9)
        self.assertEqual(admin.led_values(0), (True, False, False))
        self.assertEqual(admin.led_values(2999), (True, False, False))
        self.assertEqual(admin.led_values(3000), (False, True, False))
        self.assertEqual(admin.led_values(6000), (False, False, True))
        self.assertIsNone(admin.led_values(9000))

    def test_sensor_presets_are_derived_from_current_thresholds(self):
        admin, token = self.authenticated()
        custom = SimpleNamespace(WARNING_LEVEL=12000, DANGER_LEVEL=20000,
                                 HYSTERESIS=1000, ADC_LOW_LIMIT=200, ADC_HIGH_LIMIT=60000)
        readings = {}
        for mode in ("normal", "warning", "danger"):
            admin.start_test(token, {"kind": "sensor", "mode": mode, "duration": 10}, 0, custom)
            readings[mode] = admin.sample_value(0)
            self.assertIsNone(admin.led_values(0))
        self.assertTrue(200 < readings["normal"] < 11000)
        self.assertTrue(12000 <= readings["warning"] < 20000)
        self.assertTrue(20000 <= readings["danger"] < 60000)

    def test_fault_preset_is_missing_reading_and_raw_zero_is_separate(self):
        admin, token = self.authenticated()
        self.start(admin, token, mode="fault", duration=5)
        self.assertTrue(admin.public_status(0)["active"])
        self.assertEqual(admin.test["mode"], "fault")
        self.assertIsNone(admin.sample_value(0))
        self.assertIn("읽기 오류", admin.public_status(0)["label"])
        self.start(admin, token, mode="raw", duration=30, raw=0)
        self.assertEqual(admin.sample_value(0), 0)

    def test_raw_mode_accepts_only_adc_range_integers(self):
        admin, token = self.authenticated()
        for raw in (0, 12345, 65535):
            self.start(admin, token, mode="raw", raw=raw)
            self.assertEqual(admin.sample_value(0), raw)
        for raw in (None, -1, 65536, True, 1.0, "20000"):
            self.assert_error(400, self.start, admin, token, mode="raw", raw=raw)

    def test_invalid_alarm_settings_leave_existing_led_job_running(self):
        admin, token = self.authenticated()
        self.start(admin, token, kind="led", mode="green", duration=5)
        original_id = admin.test["id"]
        for alarm in (None, {}, {"WARNING_LEVEL": "18000"},
                      dict(WARNING_LEVEL=100, DANGER_LEVEL=200, HYSTERESIS=20,
                           ADC_LOW_LIMIT=90, ADC_HIGH_LIMIT=500)):
            self.assert_error(400, admin.start_test, token,
                              {"kind": "sensor", "mode": "normal", "duration": 10}, 1, alarm)
            self.assertEqual(admin.test["id"], original_id)

    def test_sixty_second_sequence_has_exact_phase_boundaries(self):
        admin, token = self.authenticated()
        self.start(admin, token, kind="sequence", duration=60)
        levels = dict(admin.test["levels"])
        previous = 0
        phases = ((0, "normal"), (9999, "normal"), (10000, "warning"),
                  (24999, "warning"), (25000, "danger"), (39999, "danger"),
                  (40000, "recovery"), (59999, "recovery"))
        for now, phase in phases:
            while now - previous > 9000:
                previous += 9000
                admin.private_status(token, previous)
            admin.private_status(token, now)
            previous = now
            self.assertEqual(admin.public_status(now)["phase"], phase)
            self.assertEqual(admin.sample_value(now), levels["normal" if phase == "recovery" else phase])
        self.assertIsNone(admin.sample_value(60000))
        self.assert_error(400, self.start, admin, token, kind="sequence", duration=59, now=60000)

    def test_real_warning_or_danger_preempts_every_test_kind(self):
        for kind, mode, duration in (("led", "green", 5), ("sensor", "normal", 30),
                                     ("sequence", None, 60)):
            for real_alarm in ("warning", "danger", {"alarm_level": "warning", "state": "fault"}):
                admin, token = self.authenticated()
                self.start(admin, token, kind=kind, mode=mode, duration=duration)
                self.assertFalse(admin.tick(1, real_alarm=real_alarm, real_mode=True))
                self.assertIsNone(admin.sample_value(1))
                self.assertIsNone(admin.led_values(1))
                self.assertIn("실제 센서", admin.public_status(1)["stop_reason"])

    def test_simulated_main_alarm_is_not_misidentified_as_real_alarm(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        self.assertTrue(admin.tick(1, real_alarm="danger", real_mode=False))
        self.assertTrue(admin.public_status(1)["active"])

    def test_stale_sampling_preempts_even_when_base_mode_is_simulated(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        self.assertFalse(admin.tick(1, real_mode=False, sample_stale=True))
        self.assertIn("새 센서 측정", admin.public_status(1)["stop_reason"])

    def test_manual_stop_is_authenticated_and_safe_to_repeat(self):
        admin, token = self.authenticated()
        self.start(admin, token)
        self.assert_error(401, admin.stop_test, "invalid", 1)
        self.assertIsNotNone(admin.test)
        result = admin.stop_test(token, 2)
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["test"]["active"])
        self.assertFalse(admin.stop_test(token, 3)["test"]["active"])

    def test_public_status_and_audit_never_expose_secrets_or_configuration(self):
        admin, token = self.authenticated()
        self.start(admin, token, mode="danger")
        public = admin.public_status(0)
        self.assertEqual(set(public), {"enabled", "active", "id", "kind", "mode", "label",
                                       "phase", "remaining_seconds", "stop_reason", "person", "audio"})
        private = admin.private_status(token, 1)
        document = json.dumps({"public": public, "private": private}, ensure_ascii=False)
        for secret in (PASSWORD, token, config()["PASSWORD_SALT_HEX"], config()["PASSWORD_HASH_HEX"]):
            self.assertNotIn(secret, document)
        for forbidden_key in ("PASSWORD_SALT_HEX", "PASSWORD_HASH_HEX", '"token"', '"levels"'):
            self.assertNotIn(forbidden_key, document)
        self.assertTrue(all(set(e) == {"id", "time_ms", "action", "message", "test_id"}
                            for e in private["audit"]))

    def test_audit_is_bounded_and_returned_entries_are_copies(self):
        admin, token = self.authenticated(AUDIT_SIZE=5)
        for now in range(6):
            self.start(admin, token, now=now)
        audit = admin.private_status(token, 7)["audit"]
        self.assertEqual(len(audit), 5)
        audit[0]["message"] = "changed by caller"
        self.assertNotEqual(admin.private_status(token, 8)["audit"][0]["message"], "changed by caller")

    def test_job_ids_are_unique_even_when_commands_share_timestamp(self):
        admin, token = self.authenticated()
        ids = set()
        for _ in range(4):
            self.start(admin, token)
            ids.add(admin.test["id"])
        self.assertEqual(len(ids), 4)

    def test_time_validation_does_not_accept_booleans_or_negative_values(self):
        admin = controller()
        for now in (True, -1, None, 1.2, "1"):
            self.assert_error(400, admin.login, PASSWORD, now)


    def test_optional_virtual_person_and_audio_are_strict_boolean(self):
        admin, token = self.authenticated()
        for options in ({}, {"person": False, "audio": False},
                        {"person": True}, {"person": True, "audio": True}):
            self.start(admin, token, mode="danger", **options)
            self.assertEqual(admin.test["person"], options.get("person", False))
            self.assertEqual(admin.test["audio"], options.get("audio", False))
            state = admin.public_status(0)
            self.assertEqual(state["person"], options.get("person", False))
            self.assertEqual(state["audio"], options.get("audio", False))
        prior = dict(admin.test)
        for bad in ({"person": "true"}, {"person": 1}, {"person": None},
                    {"audio": "true"}, {"audio": 1}, {"audio": None}):
            self.assert_error(400, self.start, admin, token, mode="danger", **bad)
            self.assertEqual(admin.test, prior)

    def test_audio_trial_is_five_seconds_no_sensor_override(self):
        admin, token = self.authenticated()
        status = admin.start_test(token, {"kind": "audio", "mode": "test", "duration": 5}, 100, ALARM)
        self.assertTrue(status["test"]["active"])
        self.assertEqual(status["test"]["kind"], "audio")
        self.assertIsNone(admin.sample_value(100))
        self.assertIsNone(admin.led_values(100))
        self.assertTrue(admin.test["audio"])
        self.assertIsNone(admin.test["person"])
        for spec in ({"kind":"audio", "mode":"test", "duration":6},
                     {"kind":"audio", "mode":"test", "duration":5, "person":True},
                     {"kind":"audio", "mode":"test", "duration":5, "audio":True},
                     {"kind":"audio", "mode":"stop", "duration":5}):
            self.assert_error(400, admin.start_test, token, spec, 101, ALARM)
            self.assertIsNotNone(admin.test)
        self.assertTrue(admin.public_status(5099)["active"])
        self.assertFalse(admin.public_status(5100)["active"])

    def test_sequence_virtual_person_audio_and_real_alarm_cancel(self):
        admin, token = self.authenticated()
        response = admin.start_test(token, {"kind":"sequence", "duration":60,
                                            "person":True,"audio":True}, 0, ALARM)
        self.assertEqual(response["test"]["kind"], "sequence")
        self.assertTrue(response["test"]["person"])
        self.assertTrue(response["test"]["audio"])
        self.assertTrue(admin.tick(3000, real_alarm="normal", real_mode=True))
        self.assertFalse(admin.tick(4000, real_alarm="warning", real_mode=True))
        self.assertIsNone(admin.test)


if __name__ == "__main__":
    unittest.main(verbosity=2)
