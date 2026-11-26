8일차 v2.3.0 Thonny 업데이트 - 먼저 읽기

기존 Pico 프로그램이 설치되어 있는 경우 사용하는 ZIP입니다.
1. Thonny 실행 중지 후 기존 Pico의 wifi_config.py, admin_config.py를 PC에 백업합니다.
2. ZIP을 PC에서 압축 해제합니다. 보기 > 파일 > PC 폴더에서 아래 10개 실행 파일을 Pico의 루트(/)에 업로드/덮어쓰기합니다. main.py는 마지막에 저장합니다.
   config.py, alarm_logic.py, admin_control.py, dashboard.html, rgb_stick.py,
   peripheral_config.py, peripheral_sensors.py, mp3_player.py, peripherals.py, main.py
3. 개인 설정 2개(wifi_config.py, admin_config.py)는 기존 것을 그대로 보존합니다. 이 ZIP에는 비밀번호 파일이 없습니다. 변경했던 ADC 기준/카메라 ID는 백업에서 확인하여 필요할 때만 새 설정에 반영합니다.
4. 안전한 배선 확인: MQ-2 GP26(5V 모듈 AO 분압), RGB Stick D16(GP16), SHT40 I2C0(GP8/9), HUSKYLENS I2C1(GP6/7), MP3 UART1(GP4/5).
   실드 VCC 스위치는 공통. 전체 연결 상태에서 5V로 무작정 변경하지 않습니다. RGB/SHT40 3.3V, MP3는 분리된 5V 및 UART 양방향 레벨 변환, 공통 GND. MQ-2 AO는 3.3V 이하. 안전한 연결을 마련하지 못했다면 개별 시험만 진행합니다.
5. 정상 FAT32 microSD에는 0001.mp3, 0002.mp3, 0003.mp3만 루트에 복사합니다(Pico에 넣지 않음).
6. 재시작 후 Shell의 v2.3.0-day8 및 IP 주소 확인 > 브라우저 Ctrl+Shift+R > 관리자 LED/MP3 단독 시험.
   실제 센서가 분리되어 있으면 안전한 모의 시험에 한해 config.py SIMULATION=True를 임시 적용. 실제 측정 전 False로 복원.
   새 버전 실물 전체 동작은 아직 미확인. Python 186개, DOM 12개 그룹 모의 검사 통과.

안내서는 Pico에 업로드하지 않습니다. GitHub ZIP 안 README.md/EXPANSION_GUIDE.md에서 자세한 내용을 확인합니다.
