"""Desktop verification of decisions only; this is not a sensor calibration."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alarm_logic import AlarmModel, validate_settings


def settings(**overrides):
    values = dict(
        WARNING_LEVEL=18000, DANGER_LEVEL=30000, HYSTERESIS=2000,
        ENTER_COUNT=3, EXIT_COUNT=5, WARMUP_SECONDS=0,
        SAMPLE_INTERVAL_MS=1000, HISTORY_SIZE=60, EVENT_SIZE=30,
        RAPID_RISE_PER_SECOND=1000000, RAPID_RISE_COUNT=2,
        ADC_LOW_LIMIT=100, ADC_HIGH_LIMIT=65000,
        FAULT_CONFIRM_COUNT=10, FAULT_RECOVERY_COUNT=5, SIMULATION=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Harness:
    def __init__(self, **overrides):
        self.model = AlarmModel(settings(**overrides), "test-boot")
        self.ms = 0

    def feed(self, raw, count=1, step=1000, **kwargs):
        result = None
        for _ in range(count):
            result = self.model.update(raw, self.ms, **kwargs)
            self.ms += step
        return result


class AlarmLogicTests(unittest.TestCase):
    def test_initial_data_has_no_invented_measurement(self):
        model = AlarmModel(settings())
        snapshot = model.snapshot()
        self.assertEqual(snapshot["current"]["state"], "starting")
        self.assertIsNone(snapshot["current"]["raw"])
        self.assertIsNone(snapshot["stats"]["average"])
        self.assertEqual(snapshot["stats"]["total_count"], 0)

    def test_thresholds_are_inclusive_and_need_three_samples(self):
        h = Harness()
        self.assertEqual(h.feed(17999, 4)["state"], "normal")
        self.assertEqual(h.feed(18000, 2)["state"], "normal")
        self.assertEqual(h.feed(18000)["state"], "warning")
        self.assertEqual(h.feed(29999, 4)["state"], "warning")
        self.assertEqual(h.feed(30000, 2)["state"], "warning")
        self.assertEqual(h.feed(30000)["state"], "danger")

    def test_changing_candidate_breaks_consecutive_evidence(self):
        h = Harness()
        h.feed(20000, 2)
        self.assertEqual(h.feed(17000)["state"], "normal")
        self.assertEqual(h.feed(20000, 2)["state"], "normal")
        self.assertEqual(h.feed(20000)["state"], "warning")

    def test_warning_samples_do_not_shortcut_danger_confirmation(self):
        h = Harness()
        h.feed(20000, 3)
        self.assertEqual(h.feed(31000, 2)["state"], "warning")
        self.assertEqual(h.feed(31000)["state"], "danger")

    def test_warning_hysteresis_and_five_sample_release(self):
        h = Harness()
        h.feed(20000, 3)
        self.assertEqual(h.feed(16000, 8)["state"], "warning")
        self.assertEqual(h.feed(15999, 4)["state"], "warning")
        self.assertEqual(h.feed(15999)["state"], "normal")

    def test_danger_hysteresis_and_staged_release(self):
        h = Harness()
        h.feed(32000, 3)
        self.assertEqual(h.feed(28000, 7)["state"], "danger")
        self.assertEqual(h.feed(27999, 4)["state"], "danger")
        self.assertEqual(h.feed(27999)["state"], "warning")
        self.assertEqual(h.feed(16000, 7)["state"], "warning")
        self.assertEqual(h.feed(15999, 5)["state"], "normal")

    def test_danger_can_release_directly_to_normal_after_safe_evidence(self):
        h = Harness()
        h.feed(32000, 3)
        self.assertEqual(h.feed(10000, 4)["state"], "danger")
        self.assertEqual(h.feed(10000)["state"], "normal")

    def test_warmup_is_visible_but_does_not_suppress_high_alarm(self):
        h = Harness(WARMUP_SECONDS=180)
        first = h.feed(10000)
        self.assertEqual(first["state"], "warmup")
        self.assertEqual(first["warmup_remaining"], 180)
        self.assertEqual(h.feed(32000, 3)["state"], "danger")
        self.assertEqual(h.feed(10000, 5)["state"], "warmup")
        final = h.model.update(10000, 180000)
        self.assertEqual(final["state"], "normal")
        self.assertEqual(final["warmup_remaining"], 0)

    def test_read_error_is_immediate_and_resets_rising_candidate(self):
        h = Harness()
        h.feed(20000, 2)
        self.assertEqual(h.feed(None)["state"], "fault")
        after_two = h.feed(20000, 2)
        self.assertEqual(after_two["alarm_level"], "normal")
        self.assertEqual(after_two["state"], "fault")
        self.assertEqual(h.feed(20000)["alarm_level"], "warning")

    def test_fault_recovery_is_consecutive_and_visible(self):
        h = Harness()
        h.feed(None)
        self.assertEqual(h.feed(10000, 4)["state"], "fault")
        h.feed(None)
        self.assertEqual(h.feed(10000, 4)["state"], "fault")
        self.assertEqual(h.feed(10000)["state"], "normal")

    def test_missing_readings_never_release_latched_danger(self):
        h = Harness()
        h.feed(32000, 3)
        current = h.feed(None, 20)
        self.assertEqual(current["state"], "danger")
        self.assertEqual(current["health"], "read_error")
        self.assertIsNone(current["raw"])
        self.assertEqual(h.feed(10000, 4)["state"], "danger")
        current = h.feed(10000)
        self.assertEqual(current["health"], "ok")
        self.assertEqual(current["state"], "danger")
        self.assertEqual(h.feed(10000, 4)["state"], "normal")

    def test_sustained_low_input_fault_never_clears_danger(self):
        h = Harness()
        h.feed(32000, 3)
        self.assertEqual(h.feed(100, 9)["health"], "checking")
        current = h.feed(100)
        self.assertEqual(current["health"], "low_input")
        self.assertEqual(current["state"], "danger")
        self.assertEqual(h.feed(0, 30)["state"], "danger")

    def test_one_rail_low_sample_breaks_safe_release_streak(self):
        h = Harness()
        h.feed(20000, 3)
        h.feed(10000, 4)
        self.assertEqual(h.feed(0)["alarm_level"], "warning")
        self.assertEqual(h.feed(10000, 4)["state"], "warning")
        self.assertEqual(h.feed(10000)["state"], "normal")

    def test_low_input_without_prior_alarm_shows_sensor_fault(self):
        h = Harness()
        current = h.feed(0, 10)
        self.assertEqual(current["state"], "fault")
        self.assertEqual(current["health"], "low_input")

    def test_high_saturation_diagnostic_preserves_danger(self):
        h = Harness()
        self.assertEqual(h.feed(65000, 3)["state"], "danger")
        current = h.feed(65535, 7)
        self.assertEqual(current["health"], "high_input")
        self.assertEqual(current["state"], "danger")
        self.assertEqual(current["voltage"], 3.3)
        self.assertEqual(current["percent"], 100.0)
        self.assertEqual(h.feed(64999, 5)["health"], "ok")
        self.assertEqual(h.model.current["state"], "danger")

    def test_two_confirmed_rises_warn_below_absolute_threshold(self):
        h = Harness(RAPID_RISE_PER_SECOND=8000)
        h.feed(200)
        self.assertFalse(h.feed(8200)["rapid_rise"])
        current = h.feed(16200)
        self.assertTrue(current["rapid_rise"])
        self.assertEqual(current["state"], "warning")
        self.assertEqual(h.feed(10000, 4)["state"], "warning")
        self.assertEqual(h.feed(10000)["state"], "normal")
        self.assertEqual(sum(e["type"] == "rapid_rise" for e in h.model.events), 1)

    def test_rise_rate_uses_actual_elapsed_time(self):
        model = AlarmModel(settings(RAPID_RISE_PER_SECOND=8000,
                                    WARNING_LEVEL=40000, DANGER_LEVEL=50000))
        model.update(200, 0)
        current = model.update(8200, 2000)
        self.assertEqual(current["rise_per_second"], 4000.0)
        self.assertFalse(current["rapid_rise"])
        current = model.update(16200, 4000)
        self.assertFalse(current["rapid_rise"])
        model.update(24200, 4500)
        current = model.update(32200, 5500)
        self.assertEqual(current["rise_per_second"], 8000.0)
        self.assertEqual(current["state"], "warning")

    def test_error_breaks_rate_evidence_and_first_delta_is_missing(self):
        h = Harness(RAPID_RISE_PER_SECOND=8000)
        self.assertIsNone(h.feed(200)["change"])
        h.feed(8200)
        h.feed(None)
        current = h.feed(16200)
        self.assertFalse(current["rapid_rise"])
        self.assertIsNone(current["change"])
        self.assertIsNone(current["rise_per_second"])

    def test_zero_time_difference_does_not_divide_by_zero(self):
        model = AlarmModel(settings(RAPID_RISE_PER_SECOND=8000))
        model.update(200, 0)
        current = model.update(16200, 0)
        self.assertIsNone(current["rise_per_second"])
        self.assertFalse(current["rapid_rise"])

    def test_rings_are_bounded_and_total_count_is_separate(self):
        h = Harness(ENTER_COUNT=1, EXIT_COUNT=1)
        for i in range(100):
            h.feed(33000 if i % 2 else 10000)
        snapshot = h.model.snapshot()
        self.assertEqual(len(snapshot["samples"]), 60)
        self.assertEqual(len(snapshot["events"]), 30)
        self.assertEqual(snapshot["stats"]["total_count"], 100)
        self.assertEqual(snapshot["stats"]["count"], 60)
        self.assertEqual(snapshot["stats"]["average"], 21500)
        self.assertEqual(snapshot["samples"][0]["id"], 41)
        self.assertTrue(all(e["boot_id"] == "test-boot" for e in snapshot["events"]))

    def test_invalid_measurements_are_missing_not_fake_zeroes(self):
        h = Harness()
        h.feed(10000)
        h.feed(None)
        h.feed(20000)
        h.feed(123, error=OSError("ADC"))
        snapshot = h.model.snapshot()
        self.assertEqual(snapshot["stats"]["count"], 2)
        self.assertEqual(snapshot["stats"]["total_count"], 4)
        self.assertEqual(snapshot["stats"]["average"], 15000)
        self.assertIsNone(snapshot["samples"][-1]["raw"])

    def test_simulation_is_marked_and_skips_only_software_warmup(self):
        h = Harness(SIMULATION=True, WARMUP_SECONDS=180)
        current = h.feed(10000)
        snapshot = h.model.snapshot()
        self.assertEqual(current["state"], "normal")
        self.assertEqual(current["source"], "simulation")
        self.assertEqual(snapshot["settings"]["warmup_seconds"], 0)
        self.assertTrue(all(x["source"] == "simulation" for x in snapshot["samples"]))
        self.assertTrue(all(x["source"] == "simulation" for x in snapshot["events"]))

    def test_unchanged_normal_samples_do_not_fill_event_log(self):
        h = Harness()
        h.feed(10000, 60)
        self.assertEqual(len(h.model.events), 1)

    def test_invalid_raw_types_and_range_are_faults(self):
        for raw in (True, -1, 65536, 3.5, "123"):
            with self.subTest(raw=raw):
                h = Harness()
                self.assertEqual(h.feed(raw)["state"], "fault")

    def test_invalid_time_and_source_are_rejected(self):
        model = AlarmModel(settings())
        model.update(10000, 2000)
        for ms in (-1, 1000, 3.5, True):
            with self.assertRaises(ValueError):
                model.update(10000, ms)
        with self.assertRaises(ValueError):
            model.update(10000, 3000, source="unknown")
        self.assertEqual(model.total_samples, 1)

    def test_settings_validation_rejects_unsafe_or_broken_ranges(self):
        bad = (
            dict(WARNING_LEVEL=30000), dict(HYSTERESIS=12000),
            dict(HYSTERESIS=0), dict(ENTER_COUNT=0), dict(EXIT_COUNT=True),
            dict(ADC_LOW_LIMIT=16000), dict(ADC_HIGH_LIMIT=30000),
            dict(HISTORY_SIZE=10000), dict(SAMPLE_INTERVAL_MS=0),
            dict(SIMULATION="false"),
        )
        for override in bad:
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_settings(settings(**override))
        validate_settings(settings())


if __name__ == "__main__":
    unittest.main(verbosity=2)
