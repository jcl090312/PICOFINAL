"""MicroPython/CPython admin control; HTTP and GPIO are handled by main.py."""

import hashlib
import binascii
import os


class AdminError(Exception):
    def __init__(self, status, message, retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _get(settings, key, default=None):
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _equal(a, b):
    """Bytewise XOR without early mismatch exits."""
    different = len(a) ^ len(b)
    for i in range(max(len(a), len(b))):
        av = a[i] if i < len(a) else 0
        bv = b[i] if i < len(b) else 0
        different |= av ^ bv
    return different == 0


def _seconds(milliseconds):
    return max(0, (milliseconds + 999) // 1000)


class AdminController:
    def __init__(self, settings=None, random_bytes=None):
        self.enabled = False
        self.test = None
        self._random = random_bytes if random_bytes is not None else getattr(os, "urandom", None)
        self._salt = None
        self._password_hash = None
        self._token = None
        self._expires_ms = 0
        self._last_touch_ms = 0
        self._issued_tokens = []
        self._failures = 0
        self._cooldown_until = 0
        self._audit = []
        self._audit_id = 0
        self._test_id = 0
        self._stop_reason = ""
        self.session_seconds = 600
        self.max_seconds = 120
        self.lease_seconds = 12
        self.login_max_failures = 5
        self.login_cooldown_seconds = 30
        self.audit_size = 30
        limits = (
            ("SESSION_SECONDS", "session_seconds", 600, 1, 3600),
            ("MAX_TEST_SECONDS", "max_seconds", 120, 5, 120),
            ("TEST_LEASE_SECONDS", "lease_seconds", 12, 1, 30),
            ("LOGIN_MAX_FAILURES", "login_max_failures", 5, 1, 20),
            ("LOGIN_COOLDOWN_SECONDS", "login_cooldown_seconds", 30, 1, 300),
            ("AUDIT_SIZE", "audit_size", 30, 1, 100),
        )
        try:
            for key, attribute, default, minimum, maximum in limits:
                value = _get(settings, key, default)
                if type(value) is not int or not minimum <= value <= maximum:
                    return
                setattr(self, attribute, value)
            salt_hex = _get(settings, "PASSWORD_SALT_HEX", "")
            hash_hex = _get(settings, "PASSWORD_HASH_HEX", "")
            if type(salt_hex) is not str or len(salt_hex) != 32:
                return
            if type(hash_hex) is not str or len(hash_hex) != 64:
                return
            self._salt = binascii.unhexlify(salt_hex)
            self._password_hash = binascii.unhexlify(hash_hex)
            if len(self._salt) != 16 or len(self._password_hash) != 32:
                return
            self.enabled = True
        except (ValueError, TypeError, AttributeError):
            # Broken configuration keeps admin disabled.
            self.enabled = False

    def _time(self, now_ms):
        if type(now_ms) is not int or now_ms < 0:
            raise AdminError(400, "올바르지 않은 장치 시간입니다.")

    def _record(self, now_ms, action, message, test_id=None):
        self._audit_id += 1
        self._audit.append({
            "id": self._audit_id, "time_ms": now_ms, "action": action,
            "message": message, "test_id": test_id,
        })
        if len(self._audit) > self.audit_size:
            self._audit.pop(0)

    def _cancel(self, now_ms, reason):
        if self.test is not None:
            test_id = self.test["id"]
            self.test = None
            self._stop_reason = reason
            self._record(now_ms, "test_stopped", reason, test_id)

    def _advance(self, now_ms):
        self._time(now_ms)
        if self._token is not None and now_ms >= self._expires_ms:
            self._cancel(now_ms, "관리자 세션 만료")
            self._token = None
            self._record(now_ms, "session_expired", "관리자 세션이 만료되었습니다.")
        if self.test is not None:
            if now_ms >= self.test["ends_ms"]:
                self._cancel(now_ms, "설정한 테스트 시간 종료")
            elif now_ms - self._last_touch_ms >= self.lease_seconds * 1000:
                self._cancel(now_ms, "관리자 연결 확인 시간 초과")

    def _available(self):
        if not self.enabled:
            raise AdminError(503, "관리자 비밀번호 설정이 없어 관리자 기능이 꺼져 있습니다.")

    def _issue_token(self, now_ms):
        try:
            random_value = self._random(24)
            if not isinstance(random_value, bytes) or len(random_value) != 24:
                raise ValueError("invalid random bytes")
            token = binascii.hexlify(random_value).decode("ascii")
            if token in self._issued_tokens:
                raise ValueError("repeated random token")
        except Exception:
            # No timestamp/device-ID/counter/weak-PRNG fallback.
            self._cancel(now_ms, "보안 난수 생성 실패")
            self._token = None
            self._record(now_ms, "login_unavailable", "보안 세션을 만들지 못했습니다.")
            raise AdminError(503, "보안 세션을 만들 수 없어 로그인하지 않았습니다.")
        self._issued_tokens.append(token)
        if len(self._issued_tokens) > 8:
            self._issued_tokens.pop(0)
        return token

    def login(self, password, now_ms):
        self._advance(now_ms)
        self._available()
        if now_ms < self._cooldown_until:
            raise AdminError(429, "로그인 시도가 많습니다. 잠시 후 다시 시도하세요.",
                             _seconds(self._cooldown_until - now_ms))
        if self._cooldown_until:
            self._cooldown_until = 0
            self._failures = 0
        valid = type(password) is str and 1 <= len(password) <= 128
        if valid:
            try:
                candidate = hashlib.sha256(self._salt + password.encode("utf-8")).digest()
                valid = _equal(candidate, self._password_hash)
            except UnicodeError:
                valid = False
        if not valid:
            self._failures += 1
            self._record(now_ms, "login_failed", "관리자 로그인 실패")
            if self._failures >= self.login_max_failures:
                self._cooldown_until = now_ms + self.login_cooldown_seconds * 1000
                raise AdminError(429, "로그인 시도가 많습니다. 잠시 후 다시 시도하세요.",
                                 self.login_cooldown_seconds)
            raise AdminError(401, "비밀번호가 올바르지 않습니다.")
        token = self._issue_token(now_ms)
        self._cancel(now_ms, "새 로그인으로 기존 테스트 종료")
        self._token = token
        self._expires_ms = now_ms + self.session_seconds * 1000
        self._last_touch_ms = now_ms
        self._failures = 0
        self._cooldown_until = 0
        self._record(now_ms, "login", "관리자 로그인")
        return {"token": token, "expires_in": self.session_seconds}

    def require(self, token, now_ms, touch=False):
        self._advance(now_ms)
        self._available()
        if type(token) is not str or len(token) > 128:
            raise AdminError(401, "관리자 로그인이 필요합니다.")
        try:
            token_bytes = token.encode("utf-8")
        except UnicodeError:
            raise AdminError(401, "관리자 세션이 올바르지 않습니다.")
        if self._token is None or not _equal(token_bytes, self._token.encode("ascii")):
            raise AdminError(401, "관리자 세션이 만료되었거나 올바르지 않습니다.")
        if touch:
            # Polling renews the test lease, not the session.
            self._last_touch_ms = now_ms
        return True

    def logout(self, token, now_ms):
        self.require(token, now_ms)
        self._cancel(now_ms, "관리자 로그아웃")
        self._token = None
        self._record(now_ms, "logout", "관리자 로그아웃")
        return {"authenticated": False, "test": self.public_status(now_ms)}

    def _levels(self, alarm_settings):
        names = ("WARNING_LEVEL", "DANGER_LEVEL", "HYSTERESIS", "ADC_LOW_LIMIT", "ADC_HIGH_LIMIT")
        values = [_get(alarm_settings, name) for name in names]
        if any(type(value) is not int for value in values):
            raise AdminError(400, "경보 기준 설정을 확인하세요.")
        warning, danger, hysteresis, low, high = values
        if not (0 <= low < warning - hysteresis - 1 < warning < danger < high <= 65535):
            raise AdminError(400, "모의 입력을 만들 수 없는 경보 기준입니다.")
        if hysteresis <= 0:
            raise AdminError(400, "복귀 기준 설정을 확인하세요.")
        return {
            "normal": (low + warning - hysteresis) // 2,
            "warning": warning + (danger - warning) // 2,
            "danger": danger + (high - danger) // 2,
            "fault": None,
        }

    def _duration(self, value):
        if type(value) is not int or not 5 <= value <= self.max_seconds:
            raise AdminError(400, "모의 측정 시간은 5~{}초의 정수여야 합니다.".format(self.max_seconds))
        return value

    def _validate_spec(self, spec, alarm_settings):
        if type(spec) is not dict:
            raise AdminError(400, "테스트 설정이 올바르지 않습니다.")
        kind = spec.get("kind")
        mode = spec.get("mode")
        levels = None
        raw = None
        if kind == "led":
            durations = {"green": 5, "yellow": 5, "red": 5, "all": 3, "sequence": 9}
            labels = {"green": "초록 LED 확인", "yellow": "노랑 LED 확인", "red": "빨강 LED 확인",
                      "all": "모든 LED 확인", "sequence": "LED 순서 확인"}
            if type(mode) is not str or mode not in durations:
                raise AdminError(400, "지원하지 않는 LED 테스트입니다.")
            duration = spec.get("duration", durations[mode])
            if type(duration) is not int or duration != durations[mode] or duration > self.max_seconds:
                raise AdminError(400, "이 LED 테스트의 시간은 {}초로 고정되어 있습니다.".format(durations[mode]))
            allowed = ("kind", "mode", "duration")
            label = labels[mode]
        elif kind == "sensor":
            labels = {"normal": "정상 판정 모의 시험", "warning": "주의 판정 모의 시험",
                      "danger": "위험 판정 모의 시험", "fault": "ADC 읽기 오류 모의 시험", "raw": "직접 입력 모의 시험"}
            if type(mode) is not str or mode not in labels:
                raise AdminError(400, "지원하지 않는 모의 측정입니다.")
            duration = self._duration(spec.get("duration"))
            levels = self._levels(alarm_settings)
            allowed = ("kind", "mode", "duration", "raw") if mode == "raw" else ("kind", "mode", "duration")
            raw = spec.get("raw") if mode == "raw" else levels[mode]
            if mode != "fault" and (type(raw) is not int or not 0 <= raw <= 65535):
                raise AdminError(400, "모의 ADC 입력은 0~65535의 정수여야 합니다.")
            label = labels[mode]
        elif kind == "sequence":
            duration = spec.get("duration", 60)
            if type(duration) is not int or duration != 60 or duration > self.max_seconds:
                raise AdminError(400, "연속 시나리오는 60초로 고정되어 있습니다.")
            levels = self._levels(alarm_settings)
            mode = "sequence"
            allowed = ("kind", "duration")
            label = "정상 → 주의 → 위험 → 정상 모의 시험"
        else:
            raise AdminError(400, "지원하지 않는 테스트 종류입니다.")
        if any(key not in allowed for key in spec):
            raise AdminError(400, "지원하지 않는 테스트 설정이 포함되어 있습니다.")
        return {"kind": kind, "mode": mode, "duration": duration,
                "label": label, "levels": levels, "raw": raw}

    def start_test(self, token, spec, now_ms, alarm_settings):
        self.require(token, now_ms)
        job = self._validate_spec(spec, alarm_settings)
        # Validate before replacing an existing test.
        self._cancel(now_ms, "새 테스트 시작으로 이전 테스트 종료")
        self._test_id += 1
        job["id"] = "admin-{}-{}".format(now_ms, self._test_id)
        job["started_ms"] = now_ms
        job["ends_ms"] = now_ms + job["duration"] * 1000
        self.test = job
        self._stop_reason = ""
        self._last_touch_ms = now_ms
        self._record(now_ms, "test_started", job["label"], job["id"])
        return self.private_status(token, now_ms, touch=False)

    def stop_test(self, token, now_ms, reason="관리자 중지"):
        self.require(token, now_ms)
        # reason is a trusted server-side argument, never the password/token.
        self._cancel(now_ms, reason)
        return self.private_status(token, now_ms, touch=True)

    def tick(self, now_ms, real_alarm=None, real_mode=True, sample_stale=False):
        self._advance(now_ms)
        if self.test is not None:
            if isinstance(real_alarm, dict):
                real_alarm = real_alarm.get("alarm_level", real_alarm.get("state"))
            if sample_stale:
                self._cancel(now_ms, "새 센서 측정이 없어 테스트 종료")
            elif real_mode and real_alarm in ("warning", "danger", "주의", "위험"):
                self._cancel(now_ms, "실제 센서 주의·위험 경보로 테스트 종료")
        return self.test is not None

    def _phase(self, now_ms):
        job = self.test
        elapsed = now_ms - job["started_ms"]
        if job["kind"] == "sequence":
            if elapsed < 10000:
                return "normal"
            if elapsed < 25000:
                return "warning"
            if elapsed < 40000:
                return "danger"
            return "recovery"
        if job["kind"] == "led" and job["mode"] == "sequence":
            return ("green", "yellow", "red")[min(2, max(0, elapsed // 3000))]
        return job["mode"]

    def public_status(self, now_ms):
        self._advance(now_ms)
        if self.test is None:
            return {
                "enabled": self.enabled, "active": False, "id": None,
                "kind": None, "mode": None, "label": "관리자 테스트 없음",
                "phase": None, "remaining_seconds": 0, "stop_reason": self._stop_reason,
            }
        job = self.test
        return {
            "enabled": self.enabled, "active": True, "id": job["id"],
            "kind": job["kind"], "mode": job["mode"], "label": job["label"],
            "phase": self._phase(now_ms), "remaining_seconds": _seconds(job["ends_ms"] - now_ms),
            "stop_reason": "",
        }

    def private_status(self, token, now_ms, touch=True):
        self.require(token, now_ms, touch=touch)
        return {
            "authenticated": True, "expires_in": _seconds(self._expires_ms - now_ms),
            "test": self.public_status(now_ms),
            "audit": [dict(item) for item in self._audit],
            "limits": {"max_seconds": self.max_seconds, "lease_seconds": self.lease_seconds},
        }

    def sample_value(self, now_ms):
        # Active sensor/fault returns None to inject a read error. Check .test.
        self._advance(now_ms)
        if self.test is None:
            return None
        if self.test["kind"] == "sensor":
            return self.test["raw"]
        if self.test["kind"] == "sequence":
            phase = self._phase(now_ms)
            return self.test["levels"]["normal" if phase == "recovery" else phase]
        return None

    def led_values(self, now_ms):
        self._advance(now_ms)
        if self.test is None or self.test["kind"] != "led":
            return None
        phase = self._phase(now_ms)
        return (phase in ("green", "all"), phase in ("yellow", "all"), phase in ("red", "all"))
