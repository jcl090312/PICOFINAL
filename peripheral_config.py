"""v2.2 추가 장치 설정. GP 번호이며 기존 config.py와 별도로 관리합니다."""

# Grove SHT40: 3.3V로 공급. SDA=GP4, SCL=GP5.
SHT40_ENABLED = True
SHT40_I2C_ID = 0
SHT40_SDA_PIN = 4
SHT40_SCL_PIN = 5
SHT40_ADDRESS = 0x44
SHT40_INTERVAL_MS = 2000
SHT40_STALE_MS = 6000

# 원형 HUSKYLENS / SEN0305, I2C 모드, Object Recognition(사물 인식).
# 이미 학습한 사람 ID를 유지합니다. 이 코드는 학습 데이터를 지우지 않습니다.
HUSKY_ENABLED = True
HUSKY_I2C_ID = 1
HUSKY_SDA_PIN = 6
HUSKY_SCL_PIN = 7
HUSKY_ADDRESS = 0x32
HUSKY_PERSON_ID = 1
# 렌즈 화면에서 'person: ID1'을 확인했으면 True.
# 사람이 ID2라면 위 ID도 2로 바꿉니다. 단순히 ID1이라는 이유로 사람으로 간주하지 않습니다.
HUSKY_PERSON_ID_CONFIRMED = False
HUSKY_INTERVAL_MS = 500
HUSKY_TIMEOUT_MS = 250
HUSKY_STALE_MS = 3000
HUSKY_CONFIRM_FRAMES = 2
HUSKY_HOLD_MS = 3000

I2C_FREQUENCY = 100000
I2C_TIMEOUT_US = 20000
PERIPHERAL_RETRY_MS = 3000

# Seeed Grove MP3 v4.0 / WT2605CX 전용. DFPlayer Mini용 코드가 아닙니다.
# 모듈은 5V 전원. Grove UART도 5V 측이므로 두 신호에 3.3V<->5V 레벨 변환 필요.
# Pico GP0(TX) -> 변환기 -> MP3 RX, MP3 TX -> 변환기 -> Pico GP1(RX).
MP3_ENABLED = True
MP3_UART_ID = 0
MP3_TX_PIN = 0
MP3_RX_PIN = 1
MP3_BAUDRATE = 115200
MP3_TIMEOUT_MS = 500
MP3_VOLUME = 8  # 0~31. 작은 값으로 시작하고 실제 소리를 확인합니다.
MP3_STARTUP_MS = 2000

# microSD(FAT16/FAT32)의 최상위 폴더에 아래 파일을 넣습니다.
# 번호 순서가 아니라 파일 이름으로 재생하므로 복사 순서에 의존하지 않습니다.
MP3_WARNING_FILE = "0001.mp3"
MP3_DANGER_FILE = "0002.mp3"
MP3_TEST_FILE = "0003.mp3"
AUTO_AUDIO_ENABLED = True
AUDIO_REPEAT_MS = 15000

