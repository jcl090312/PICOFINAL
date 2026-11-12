v2.2.0 Pico W 가스 경보기 확장 - 토니(Thonny) 업데이트 안내
2026-11-12

* 이미 Pico에 가스 경보기+관리자 모드 7개 파일이 들어 있는 경우 적용.
* 반드시 전원을 끄고 SHT40, HUSKYLENS, MP3, 레벨 변환기를 연결.
* Pico USB 사용 중 외부 5V(+)를 Pico VBUS에 직결하면 안 됨. GND는 공통.
* Pico의 GP26 ADC 입력에 3.3V를 초과하는 MQ-2 출력을 바로 연결하면 안 됨.

교체할 파일 (Pico 최상위 폴더):
  main.py
  admin_control.py
  dashboard.html

새로 추가할 파일:
  peripheral_config.py
  peripheral_sensors.py
  mp3_player.py
  peripherals.py

Pico 기존 파일 중 보존할 파일:
  alarm_logic.py
  config.py
  wifi_config.py  <- 개인 Wi-Fi 비밀번호 들어 있음
  admin_config.py <- 개인 관리자 인증 설정 들어 있음

모든 파일을 Pico 최상위 폴더 / 에 저장한 뒤 재시작.
HUSKYLENS 화면의 'person: ID번호'를 확인한 뒤
peripheral_config.py의 HUSKY_PERSON_ID를 맞추고
HUSKY_PERSON_ID_CONFIRMED = True 로 바꿔야 실제 사람 감지 자동 소리 작동.

Pico에는 MP3 음원 파일을 넣지 않음.
별도 MP3 카드용 ZIP에서 0001.mp3, 0002.mp3, 0003.mp3만 꺼내
FAT16/FAT32 microSD 최상위 폴더에 복사. Grove MP3 V4 모듈에 삽입.

대시보드 관리자 로그인 -> MP3 소리 5초 단독 시험.
관리자 모의 주의/위험 -> 사람 감지 모의 체크 + 스피커도 울리기 체크.
실제 가스는 사용하지 않음.

GitHub용 ZIP은 테스트/문서까지 포함하므로 Pico에 통째로 업로드하지 않음.
물리 연결 시험은 아직 미실시. 시작 볼륨 낮게 설정.
상세 배선은 GitHub용 EXPANSION_GUIDE.md 참고.
