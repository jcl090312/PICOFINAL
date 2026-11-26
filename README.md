# 가스 누출 조기 경보 시스템 · v2.3.0 (8일차)

Raspberry Pi Pico 2 W와 MQ-2, Grove RGB LED Stick(10 WS2813 Mini), SHT40, HUSKYLENS, Grove MP3 V4를 사용하는 교육용 프로젝트입니다. 기존 웹 대시보드, 실시간 그래프, CSV, 관리자 로그인/모의 시험을 유지합니다.

> ADC 원시값은 교정 가스 농도(ppm)가 아닙니다. 인증된 가스 안전 경보기를 대체할 수 없습니다. 실제 가스를 방출하지 말고 모의 시험부터 진행합니다.

## 현재 상태 (2026-11-26)

- 6일차: RGB Stick 단독 색상 변경·소등 성공. GP16, 10픽셀, NeoPixel 타이밍 `(350, 900, 800, 450)`.
- 7일차: UART0와 UART1 모두 통신 성공, 기존 카드에서 ERR:6/ERR:9. 다른 정상 카드로 교차 시험 후 실제 재생 성공. 기존 카드의 정확한 결함 유형은 미확인.
- 8일차: RGB Stick 메인·관리자 모드 코드 적용, UART1/CRLF/볼륨7 적용, ERR 응답 처리 개선, SHT40 I2C0 핀 충돌 제거. 새 카드의 경고음 3종 및 전체 하드웨어 통합 시험은 아직 미확인.

## 핀 배치 (Grove Shield for Pi Pico v1.0 기준)

| 장치 | 포트 | GP 핀 | 전원/신호 주의 |
|---|---|---|---|
| MQ-2 | A0 | GP26 | 모듈 5V, AO 분압 후 3.3V 이하 |
| RGB LED Stick | D16 | GP16 | 3.3V 공급, GND 공통 |
| SHT40 | I2C0 | GP8 SDA / GP9 SCL | 3.3V 공급 |
| HUSKYLENS | I2C1 | GP6 SDA / GP7 SCL | 별도 충분한 5V, I2C 신호 3.3V 호환 확인 |
| Grove MP3 V4 | UART1 | GP4 TX / GP5 RX | 별도 5V, 두 UART 신호에 3.3V/5V 레벨 변환 |

**실드의 전압 스위치는 공통입니다. 모든 센서를 연결한 상태에서 무작정 5V로 바꾸지 않습니다.** 배선 변경 전에 USB/외부전원을 모두 끄고, 신호 전압과 전원 분리 구성을 확인합니다. 상세 배선·사람 ID·시험 방법은 [EXPANSION_GUIDE.md](EXPANSION_GUIDE.md)를 참고하세요. 외부 5V+를 USB로 구동 중인 Pico의 VBUS에 직접 연결하지 않습니다. GND는 공통입니다.

## Thonny 설치

Pico의 기존 `wifi_config.py`, `admin_config.py`를 먼저 백업합니다. 이 두 개인 설정은 그대로 보관합니다. Thonny에서 실행 중지 → 보기 > 파일 → ZIP을 푼 PC 폴더 → 아래 실행 파일을 Pico 최상위 `/`에 업로드합니다. `main.py`는 마지막에 올립니다.

| Pico 파일 | 역할 |
|---|---|
| `main.py` | 전체 실행·서버·관리자 |
| `config.py` | ADC/LED/판정 설정 |
| `alarm_logic.py` | 판정·통계·기록 |
| `admin_control.py` | 인증·모의 시험 |
| `dashboard.html` | 웹 화면 |
| `rgb_stick.py` | WS2813 제어 |
| `peripheral_config.py` | 온습도·사람·MP3 핀/설정 |
| `peripheral_sensors.py` | I2C 센서 드라이버 |
| `mp3_player.py` | MP3 UART 명령·응답 |
| `peripherals.py` | 장치 독립 실행·소리 조건 |
| `wifi_config.py` | 기존 개인 설정 (보존) |
| `admin_config.py` | 기존 개인 설정 (보존) |

기존 설정값을 직접 조정한 적이 있다면 백업 후 새 config에 필요한 값만 옮기세요. 처음 설치하는 경우 `wifi_config.example.py`를 `wifi_config.py`로 복사해 설정합니다. PC에서 `python tools/make_admin_config.py`로 관리자 설정을 만들고 Pico에만 저장합니다. 관리자 설정 빈 값은 로그인을 비활성화합니다. 자세한 방법은 [ADMIN_GUIDE.md](ADMIN_GUIDE.md). 별도 pip 패키지는 필요하지 않습니다. Pico 2 W용 MicroPython에는 `neopixel`이 포함되어 있어야 합니다.

## MP3 카드

`sd_card/0001.mp3` (주의), `0002.mp3` (위험), `0003.mp3` (시험)을 정상 작동이 확인된 FAT32/32GB 이하 microSD 카드 최상위에 복사합니다. 제공 음원은 테스트용 신호음입니다. Pico 파일 시스템에 넣지 않습니다. 카드 교체는 전원을 끈 후 진행합니다. `diagnostics/mp3_uart1_test.py`는 단독 시험용으로 Thonny 새 탭에서 실행하며 `main.py`로 저장하지 않습니다.

## 연동 시험

1. 새 카드의 세 음원 실제 재생 확인.
2. 안전한 배선 확인 후 업데이트된 Pico를 재시작하고 Shell의 `2.3.0-day8` 및 Wi-Fi IP를 확인.
3. 웹 관리자 로그인 → 초록/노랑/빨강/전체 LED 시험 → MP3 단독 5초 시험.
4. 관리자 모의 주의/위험 + 사람 감지 모의 + 스피커도 울리기 체크. 실제 센서 경보 우선과 시험 종료 후 복귀 확인.
5. SHT40 값 갱신과 HUSKYLENS 실제 사람 ID를 확인. ID1을 사람으로 확인하기 전에는 `HUSKY_PERSON_ID_CONFIRMED=False` 유지.

실제 가스 주의/위험 상태와 신선한 확인된 사람 감지가 동시에 성립할 때 자동 MP3가 재생됩니다. 사람 미검출이나 카메라 오류에도 가스 LED 경보는 독립적으로 유지됩니다. 센서가 연결되지 않은 시험에서는 `SIMULATION=True`를 임시 사용하거나 해당 추가 장치 ENABLED 값을 끌 수 있습니다. 실제 센서를 연결한 뒤 `SIMULATION=False`로 되돌립니다. 명령 ACK는 실제 소리 확인과 다릅니다.

## GitHub 업로드

GitHub ZIP 내부 파일·폴더 전체를 기존 저장소 루트에 복사/커밋합니다(압축파일 자체를 올리지 않음). `.gitignore`와 비밀 없는 `.example.py` 두 개는 포함합니다. 개인 `wifi_config.py`, `admin_config.py`, 실제 측정 CSV/개인 비밀번호는 공개 저장소에 올리지 않습니다. GitHub는 코드 보관용이며 대시보드는 Pico가 제공하는 로컬 IP에서 실행됩니다.

- [개발 기록 1~7일차](DEVELOPMENT_LOG.md) 및 8일차 준비 기록
- [전체 배선과 설치](EXPANSION_GUIDE.md)
- [관리자 가이드](ADMIN_GUIDE.md)
- [테스트 순서](TESTING.md) / [검증 결과](VALIDATION.md)

PC에서 `python -m unittest discover -s tests -p "test_*.py" -q`와 `node tests/test_dashboard.js`로 검증할 수 있습니다. 이번 코드의 CPython/DOM 모의 시험 결과는 각 186개/12개 그룹 통과입니다. 실제 Pico 2 W의 통합 배선, 신호 전압, 무선 연결, 음원과 사람 인식은 아직 재시험해야 합니다.
