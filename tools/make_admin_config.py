"""PC에서 개인 admin_config.py를 만드는 도구. 실제 비밀번호는 파일에 쓰지 않습니다.

실행: python tools/make_admin_config.py
기존 설정을 명시적으로 교체: python tools/make_admin_config.py --force
생성된 파일은 Pico에만 복사하고 GitHub에 올리지 마세요.
"""

import argparse
import getpass
import hashlib
import os
from pathlib import Path


def render_config(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError("관리자 비밀번호는 8~128자로 입력하세요.")
    salt = os.urandom(16)
    digest = hashlib.sha256(salt + password.encode("utf-8")).hexdigest()
    return '''"""개인 관리자 설정. Pico에만 저장하고 GitHub에는 올리지 마세요.

비밀번호 자체는 저장하지 않고 salt와 검증용 해시를 보관합니다.
이 파일도 공개하지 마세요. 로컬 HTTP 연결의 암호화를 제공하는 설정은 아닙니다.
"""

PASSWORD_SALT_HEX = "{salt}"
PASSWORD_HASH_HEX = "{digest}"

SESSION_SECONDS = 600
MAX_TEST_SECONDS = 120
TEST_LEASE_SECONDS = 12
LOGIN_MAX_FAILURES = 5
LOGIN_COOLDOWN_SECONDS = 30
AUDIT_SIZE = 30
'''.format(salt=salt.hex(), digest=digest)


def main():
    parser = argparse.ArgumentParser(description="Pico 관리자 개인 설정 생성")
    parser.add_argument("--force", action="store_true", help="기존 admin_config.py를 교체")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "admin_config.py"
    if destination.exists() and not args.force:
        raise SystemExit("admin_config.py가 이미 있습니다. 교체하려면 --force를 사용하세요.")
    password = getpass.getpass("새 관리자 비밀번호(화면에 표시되지 않음): ")
    confirmation = getpass.getpass("한 번 더 입력: ")
    if password != confirmation:
        raise SystemExit("두 입력이 달라 파일을 만들지 않았습니다.")
    try:
        content = render_config(password)
    except ValueError as exc:
        raise SystemExit(str(exc))
    destination.write_text(content, encoding="utf-8")
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    print("admin_config.py 생성 완료. Pico 최상위 폴더에 저장한 뒤 다시 시작하세요.")
    print("이 파일을 GitHub에 올리지 마세요.")


if __name__ == "__main__":
    main()
