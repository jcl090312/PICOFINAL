"""Pico W 가스 경보기 설정. 핀 번호는 물리 번호가 아닌 GP 번호입니다."""

# 센서: MQ-2 AO에는 반드시 3.3V 이하가 되도록 분압 회로를 사용하세요.
ADC_PIN = 26
ADC_MEDIAN_READS = 5  # 1회 측정마다 5번 읽은 중앙값, 홀수만 허용

# 각 LED: GP 핀 -> 330옴 -> LED 긴 다리(+), 짧은 다리(-) -> GND
# 사용하지 않는 LED는 None. 빨간 LED만 있다면 GREEN/YELLOW를 None으로 변경.
GREEN_LED_PIN = 14
YELLOW_LED_PIN = 15
RED_LED_PIN = 16
LED_ACTIVE_HIGH = True
USE_ONBOARD_LED = True  # Wi-Fi 상태 표시. 외부 경보 LED와 역할이 다릅니다.

# 선택 기능: 3.3V 논리 입력을 받는 능동 부저 구동회로만 연결하세요.
# 부저를 GPIO에 직접 연결하지 마세요. 기본값 None이면 출력하지 않습니다.
# 수동 부저는 PWM 구동이 필요하므로 이 설정의 대상이 아닙니다.
BUZZER_PIN = None
BUZZER_ACTIVE_HIGH = True

# 아래 값은 분압 후 ADC 원시값 기준의 실험용 예시이며 ppm 기준이 아닙니다.
WARNING_LEVEL = 18000
DANGER_LEVEL = 30000
HYSTERESIS = 2000
ENTER_COUNT = 3  # 같은 상위 상태 후보가 3회 연속일 때 승격
EXIT_COUNT = 5   # 낮은 상태 후보가 5회 연속일 때 복귀

SAMPLE_INTERVAL_MS = 1000
HISTORY_SIZE = 60
EVENT_SIZE = 30

# 시작 후 관찰 안내 시간입니다. 제조사 예열/안정화/교정을 대체하지 않습니다.
# 이 시간 중에도 높은 값에 대한 주의/위험 판정은 계속합니다.
WARMUP_SECONDS = 180

# 실제 측정 간격으로 환산한 변화 속도. 2회 연속이면 최소 '주의'로 전환.
RAPID_RISE_PER_SECOND = 8000
RAPID_RISE_COUNT = 2

# 신호 점검 보조 규칙. 단선이나 센서 고장을 완전히 검출하는 기능은 아닙니다.
ADC_LOW_LIMIT = 100
ADC_HIGH_LIMIT = 65000
FAULT_CONFIRM_COUNT = 10
FAULT_RECOVERY_COUNT = 5

# True: 실제 ADC 대신 가상값을 사용하며 LED도 실제로 바뀝니다.
# 화면/CSV에 simulation으로 기록되고 예열 안내는 생략됩니다.
# 실제 센서를 사용할 때는 반드시 False로 저장하고 Pico를 재시작하세요.
SIMULATION = False

WIFI_CONNECT_TIMEOUT_SECONDS = 15
WIFI_RETRY_SECONDS = 30
HTTP_PORT = 80
HTTP_MAX_CLIENTS = 2
HTTP_HEADER_LIMIT = 2048
HTTP_HEADER_TIMEOUT_MS = 2000
HTTP_WRITE_TIMEOUT_MS = 2000
STALE_SAMPLE_MS = 4500
CONSOLE_LOG = True
