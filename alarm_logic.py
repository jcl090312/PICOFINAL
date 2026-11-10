_STATUS = {
    "starting": "측정 준비",
    "warmup": "예열 중",
    "normal": "정상",
    "warning": "주의",
    "danger": "위험",
    "fault": "센서 점검",
}
_LEVEL_NAMES = ("normal", "warning", "danger")
_SETTING_NAMES = (
    "WARNING_LEVEL", "DANGER_LEVEL", "HYSTERESIS", "ENTER_COUNT",
    "EXIT_COUNT", "WARMUP_SECONDS", "SAMPLE_INTERVAL_MS", "HISTORY_SIZE",
    "EVENT_SIZE", "RAPID_RISE_PER_SECOND", "RAPID_RISE_COUNT",
    "ADC_LOW_LIMIT", "ADC_HIGH_LIMIT", "FAULT_CONFIRM_COUNT",
    "FAULT_RECOVERY_COUNT", "SIMULATION",
)


def _setting(settings, name):
    try:
        if isinstance(settings, dict):
            return settings[name]
        return getattr(settings, name)
    except (KeyError, AttributeError):
        raise ValueError("Missing config setting: " + name)


def validate_settings(settings):
    """Raise ValueError early for settings that would invalidate the logic."""
    limits = {
        "WARNING_LEVEL": (1, 65534),
        "DANGER_LEVEL": (2, 65535),
        "HYSTERESIS": (1, 65534),
        "ENTER_COUNT": (1, 60),
        "EXIT_COUNT": (1, 60),
        "WARMUP_SECONDS": (0, 604800),
        "SAMPLE_INTERVAL_MS": (100, 60000),
        "HISTORY_SIZE": (1, 300),
        "EVENT_SIZE": (1, 100),
        "RAPID_RISE_PER_SECOND": (1, 10000000),
        "RAPID_RISE_COUNT": (1, 60),
        "ADC_LOW_LIMIT": (0, 65533),
        "ADC_HIGH_LIMIT": (2, 65535),
        "FAULT_CONFIRM_COUNT": (1, 60),
        "FAULT_RECOVERY_COUNT": (1, 60),
    }
    for name in limits:
        value = _setting(settings, name)
        lo, hi = limits[name]
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError("{} must be an integer in {}..{}".format(name, lo, hi))
    if type(_setting(settings, "SIMULATION")) is not bool:
        raise ValueError("SIMULATION must be True or False")
    warning = _setting(settings, "WARNING_LEVEL")
    danger = _setting(settings, "DANGER_LEVEL")
    hysteresis = _setting(settings, "HYSTERESIS")
    if warning >= danger:
        raise ValueError("WARNING_LEVEL must be lower than DANGER_LEVEL")
    if hysteresis >= warning or hysteresis >= danger - warning:
        raise ValueError("HYSTERESIS must be smaller than WARNING_LEVEL and the threshold gap")
    if _setting(settings, "ADC_LOW_LIMIT") >= warning - hysteresis:
        raise ValueError("ADC_LOW_LIMIT must be below the warning release threshold")
    if _setting(settings, "ADC_HIGH_LIMIT") <= danger:
        raise ValueError("ADC_HIGH_LIMIT must be above DANGER_LEVEL")


class AlarmModel:
    def __init__(self, settings, boot_id="boot"):
        validate_settings(settings)
        self.cfg = {}
        for name in _SETTING_NAMES:
            self.cfg[name] = _setting(settings, name)
        self.boot_id = str(boot_id)
        self.samples = []
        self.events = []
        self.total_samples = 0
        self._event_id = 0
        self._alarm = 0
        self._candidate = None
        self._candidate_count = 0
        self._low_count = 0
        self._high_count = 0
        self._fault = None
        self._recovery_count = 0
        self._previous_raw = None
        self._previous_ms = None
        self._previous_trusted = False
        self._rise_count = 0
        self._rapid = False
        self._last_ms = -1
        self._warmup_seconds = 0 if self.cfg["SIMULATION"] else self.cfg["WARMUP_SECONDS"]
        self.current = {
            "raw": None,
            "voltage": None,
            "percent": None,
            "change": None,
            "rise_per_second": None,
            "state": "starting",
            "status": _STATUS["starting"],
            "alarm_level": "normal",
            "time": 0,
            "sample_id": 0,
            "source": "simulation" if self.cfg["SIMULATION"] else "sensor",
            "warmup_remaining": self._warmup_seconds,
            "health": "ok",
            "health_message": "아직 측정하지 않았습니다.",
            "rapid_rise": False,
            "pending": "첫 측정 대기",
        }

    def _reset_candidate(self):
        self._candidate = None
        self._candidate_count = 0

    def _check_health(self, raw, valid):
        """Rail checks are wiring clues, not proof that a sensor is healthy."""
        if not valid:
            self._fault = "read_error"
            self._low_count = 0
            self._high_count = 0
            self._recovery_count = 0
        elif raw <= self.cfg["ADC_LOW_LIMIT"]:
            self._low_count += 1
            self._high_count = 0
            self._recovery_count = 0
            if self._low_count >= self.cfg["FAULT_CONFIRM_COUNT"]:
                self._fault = "low_input"
        elif raw >= self.cfg["ADC_HIGH_LIMIT"]:
            self._high_count += 1
            self._low_count = 0
            self._recovery_count = 0
            if self._high_count >= self.cfg["FAULT_CONFIRM_COUNT"]:
                self._fault = "high_input"
        else:
            self._low_count = 0
            self._high_count = 0
            if self._fault is not None:
                self._recovery_count += 1
                if self._recovery_count >= self.cfg["FAULT_RECOVERY_COUNT"]:
                    self._fault = None
                    self._recovery_count = 0

        if self._fault is not None:
            messages = {
                "read_error": "ADC 읽기 실패. 센서 연결과 프로그램을 점검하세요.",
                "low_input": "낮은 입력이 지속됩니다. 전원·배선·분압 회로를 점검하세요.",
                "high_input": "상한 부근 입력이 지속됩니다. 높은 센서 출력과 입력 포화를 구분해 점검하세요.",
            }
            message = messages[self._fault]
            if self._recovery_count:
                message += " 정상 입력 복귀 확인 {}/{}회.".format(
                    self._recovery_count, self.cfg["FAULT_RECOVERY_COUNT"])
            return self._fault, message
        if self._low_count or self._high_count:
            count = self._low_count if self._low_count else self._high_count
            side = "하한" if self._low_count else "상한"
            return "checking", "{} 부근 입력 확인 중 {}/{}회.".format(
                side, count, self.cfg["FAULT_CONFIRM_COUNT"])
        return "ok", "ADC 입력에 지속적인 범위 이상이 발견되지 않았습니다."

    def _threshold_target(self, raw):
        warning = self.cfg["WARNING_LEVEL"]
        danger = self.cfg["DANGER_LEVEL"]
        hysteresis = self.cfg["HYSTERESIS"]
        if self._alarm == 2:
            if raw >= danger - hysteresis:
                return 2
            if raw >= warning - hysteresis:
                return 1
            return 0
        if raw >= danger:
            return 2
        if raw >= warning - (hysteresis if self._alarm == 1 else 0):
            return 1
        return 0

    def _advance_alarm(self, raw, valid, trusted_input):
        if not valid or raw <= self.cfg["ADC_LOW_LIMIT"]:
            # A missing or grounded signal cannot prove that gas has cleared.
            self._reset_candidate()
            return "센서 입력 확인 전까지 경보 해제 보류"

        # RAPID_RISE_COUNT already confirms consecutive rises. Applying another
        # ENTER_COUNT gate here would discard a short, confirmed rise warning.
        if self._rapid and self._alarm == 0:
            self._alarm = 1
            if self._candidate != 2:
                self._reset_candidate()

        target = self._threshold_target(raw)
        if self._rapid and target < 1:
            target = 1
        if target == self._alarm:
            self._reset_candidate()
            return ""
        if target < self._alarm and (not trusted_input or self._fault is not None):
            self._reset_candidate()
            return "센서 입력 복귀 확인 전까지 경보 해제 보류"

        if target == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = target
            self._candidate_count = 1
        needed = self.cfg["ENTER_COUNT"] if target > self._alarm else self.cfg["EXIT_COUNT"]
        if self._candidate_count >= needed:
            self._alarm = target
            self._reset_candidate()
            return ""
        label = ("정상", "주의", "위험")[target]
        return "{} 전환 확인 {}/{}회".format(label, self._candidate_count, needed)

    def _event(self, kind, message):
        self._event_id += 1
        item = self.current
        self.events.append({
            "id": self._event_id,
            "boot_id": self.boot_id,
            "time": item["time"],
            "raw": item["raw"],
            "state": item["state"],
            "status": item["status"],
            "type": kind,
            "message": message,
            "source": item["source"],
        })
        if len(self.events) > self.cfg["EVENT_SIZE"]:
            self.events.pop(0)

    def update(self, raw, uptime_ms, source="sensor", error=None):
        """Record one attempt; uptime_ms must be monotonic, including tick wrap.

        A read exception is passed as update(None, uptime_ms, error=exc).
        The retained history contains failed attempts with raw=None; aggregate
        measurement statistics exclude these missing values.
        """
        if type(uptime_ms) is not int or uptime_ms < 0 or uptime_ms < self._last_ms:
            raise ValueError("uptime_ms must be a nondecreasing nonnegative integer")
        if source not in ("sensor", "simulation"):
            raise ValueError("source must be sensor or simulation")
        if self.cfg["SIMULATION"]:
            source = "simulation"
        self._last_ms = uptime_ms
        previous = self.current
        previous_rapid = self._rapid
        valid = type(raw) is int and 0 <= raw <= 65535 and error is None
        if not valid:
            raw = None
        trusted = valid and self.cfg["ADC_LOW_LIMIT"] < raw < self.cfg["ADC_HIGH_LIMIT"]
        health, health_message = self._check_health(raw, valid)

        change = None
        rise_rate = None
        if valid and self._previous_raw is not None:
            change = raw - self._previous_raw
            delta_ms = uptime_ms - self._previous_ms
            if delta_ms > 0:
                rise_rate = change * 1000 / delta_ms
        if (trusted and self._previous_trusted and self._fault is None
                and rise_rate is not None
                and rise_rate >= self.cfg["RAPID_RISE_PER_SECOND"]):
            self._rise_count += 1
        else:
            self._rise_count = 0
        self._rapid = self._rise_count >= self.cfg["RAPID_RISE_COUNT"]
        pending = self._advance_alarm(raw, valid, trusted)

        remaining_ms = max(0, self._warmup_seconds * 1000 - uptime_ms)
        remaining = (remaining_ms + 999) // 1000
        if self._alarm == 2:
            state = "danger"
        elif self._fault is not None:
            state = "fault"
        elif self._alarm == 1:
            state = "warning"
        elif remaining:
            state = "warmup"
        else:
            state = "normal"

        self.total_samples += 1
        self.current = {
            "raw": raw,
            "voltage": round(raw * 3.3 / 65535, 3) if valid else None,
            "percent": round(raw * 100 / 65535, 1) if valid else None,
            "change": change,
            "rise_per_second": round(rise_rate, 1) if rise_rate is not None else None,
            "state": state,
            "status": _STATUS[state],
            "alarm_level": _LEVEL_NAMES[self._alarm],
            "time": uptime_ms / 1000,
            "sample_id": self.total_samples,
            "source": source,
            "warmup_remaining": remaining,
            "health": health,
            "health_message": health_message,
            "rapid_rise": self._rapid,
            "pending": pending,
        }
        self.samples.append({
            "id": self.total_samples,
            "time": self.current["time"],
            "raw": raw,
            "change": change,
            "state": state,
            "status": self.current["status"],
            "source": source,
            "health": health,
        })
        if len(self.samples) > self.cfg["HISTORY_SIZE"]:
            self.samples.pop(0)
        if state != previous["state"]:
            self._event("state", "{} → {}".format(previous["status"], self.current["status"]))
        if health != previous["health"]:
            self._event("health", health_message)
        if self._rapid and not previous_rapid:
            self._event("rapid_rise", "급상승 확인: 초당 {} 이상 {}회 연속".format(
                self.cfg["RAPID_RISE_PER_SECOND"], self.cfg["RAPID_RISE_COUNT"]))

        self._previous_raw = raw
        self._previous_ms = uptime_ms if valid else None
        self._previous_trusted = trusted
        return self.current

    def snapshot(self):
        values = [item["raw"] for item in self.samples if item["raw"] is not None]
        return {
            "boot_id": self.boot_id,
            "current": self.current,
            "samples": self.samples,
            "events": self.events,
            "stats": {
                "average": round(sum(values) / len(values), 1) if values else None,
                "maximum": max(values) if values else None,
                "minimum": min(values) if values else None,
                "count": len(values),
                "total_count": self.total_samples,
            },
            "settings": {
                "warning_level": self.cfg["WARNING_LEVEL"],
                "danger_level": self.cfg["DANGER_LEVEL"],
                "hysteresis": self.cfg["HYSTERESIS"],
                "enter_count": self.cfg["ENTER_COUNT"],
                "exit_count": self.cfg["EXIT_COUNT"],
                "warmup_seconds": self._warmup_seconds,
                "history_size": self.cfg["HISTORY_SIZE"],
                "event_size": self.cfg["EVENT_SIZE"],
                "sample_interval_ms": self.cfg["SAMPLE_INTERVAL_MS"],
                "rapid_rise_per_second": self.cfg["RAPID_RISE_PER_SECOND"],
                "rapid_rise_count": self.cfg["RAPID_RISE_COUNT"],
            },
        }
