"""Run on MicroPython itself to catch differences hidden by CPython mocks.

From the repository root: micropython tests/micropython_admin_smoke.py
On Pico: save admin_control.py and this script, then run this script in Thonny.
This creates its own dummy credential in RAM and never imports admin_config.py.
It does not access GPIO or Wi-Fi and never prints a password or session token.
"""

import sys
import binascii
import hashlib

sys.path.insert(0, ".")
from admin_control import AdminController, AdminError


def expect_error(action, expected_status):
    try:
        action()
    except AdminError as exc:
        assert exc.status == expected_status, "Unexpected admin error status"
        assert isinstance(exc.message, str), "Missing admin error message"
        return
    raise AssertionError("Expected an AdminError")


def main():
    print("Runtime:", sys.implementation)

    # Exception.__init__(...) is not available on MicroPython; super() is.
    error = AdminError(401, "test message", retry_after=3)
    assert error.status == 401 and error.message == "test message"
    assert error.retry_after == 3
    print("PASS: exception constructor and readable attributes")

    salt = b"0123456789abcdef"
    dummy_password = "micro-runtime-check-only"
    settings = {
        "PASSWORD_SALT_HEX": binascii.hexlify(salt).decode(),
        "PASSWORD_HASH_HEX": binascii.hexlify(
            hashlib.sha256(salt + dummy_password.encode()).digest()).decode(),
    }
    controller = AdminController(settings)
    assert controller.enabled
    expect_error(lambda: controller.login("incorrect", 1000), 401)
    print("PASS: wrong password produces 401")

    expect_error(lambda: AdminController(None).login("dummy", 1000), 503)
    print("PASS: missing configuration produces 503")

    result = controller.login(dummy_password, 1001)
    token = result["token"]
    assert isinstance(token, str) and len(token) == 48
    assert controller.require(token, 1002)
    print("PASS: correct login and token authentication")

    expect_error(lambda: controller.require("invalid-token", 1003), 401)
    print("PASS: invalid token produces 401")

    controller.logout(token, 1004)
    expect_error(lambda: controller.require(token, 1005), 401)
    print("PASS: logout invalidates the token")
    print("6 MicroPython compatibility checks passed; no physical hardware was tested.")


if __name__ == "__main__":
    main()
