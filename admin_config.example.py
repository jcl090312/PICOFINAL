"""관리자 설정 예시. 실제 admin_config.py에는 개인 설정을 넣어 Pico에만 저장하세요.

빈 값이면 관리자 로그인과 테스트 기능은 비활성화됩니다.
실제 비밀번호와 실제 admin_config.py를 GitHub에 업로드하지 마세요.
PASSWORD_HASH_HEX 계산 규칙: SHA256(16바이트 salt + 비밀번호 UTF-8 바이트).
설정 생성 및 설치 방법은 ADMIN_GUIDE.md를 참고하세요.
"""

PASSWORD_SALT_HEX = ""  # 16바이트 salt의 32자리 16진수 표현
PASSWORD_HASH_HEX = ""  # SHA-256 결과의 64자리 16진수 표현

SESSION_SECONDS = 600       # 로그인 후 절대 만료 시간. 조회해도 연장되지 않습니다.
MAX_TEST_SECONDS = 120      # 모의 센서 시험의 최대 시간
TEST_LEASE_SECONDS = 12     # 인증된 화면의 확인이 끊기면 테스트 종료
LOGIN_MAX_FAILURES = 5
LOGIN_COOLDOWN_SECONDS = 30
AUDIT_SIZE = 30             # 메모리에 남기는 관리자 동작 기록 수
