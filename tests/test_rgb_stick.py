import sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rgb_stick import RGBStick
from test_runtime import firmware
import peripheral_config as P
from peripherals import PeripheralManager

class Pixels:
    def __init__(self):
        self.values = [None] * 10
        self.writes = 0
    def __setitem__(self, key, value):
        self.values[key] = value
    def write(self):
        self.writes += 1

class StickTests(unittest.TestCase):
    def setUp(self):
        self.pixels = Pixels()
        self.stick = RGBStick(16, pixels=self.pixels)
    def test_single_colors_and_off(self):
        for states, color in [((True, False, False),(0,20,0)), ((False,True,False),(20,12,0)), ((False,False,True),(20,0,0)), ((False,False,False),(0,0,0))]:
            self.stick.set_states(states)
            self.assertEqual(self.pixels.values, [color]*10)
    def test_all_uses_three_visible_groups(self):
        self.stick.set_states((True,True,True))
        self.assertEqual(set(self.pixels.values), {(0,20,0),(20,12,0),(20,0,0)})
        self.assertEqual(len(self.pixels.values),10)
    def test_identical_frames_do_not_retransmit(self):
        self.stick.set_states((True,False,False))
        writes = self.pixels.writes
        self.stick.set_states((True,False,False))
        self.assertEqual(writes,self.pixels.writes)
    def test_alarm_and_admin_use_stick(self):
        with patch.multiple(firmware.C, LED_STICK_ENABLED=True, LED_STICK_PIN=16), patch('rgb_stick.RGBStick',return_value=self.stick):
            outputs = firmware.Outputs()
            self.assertIsNone(outputs.red)
            outputs.apply('normal',0)
            self.assertEqual(self.pixels.values,[(0,20,0)]*10)
            outputs.apply('warning',0)
            self.assertEqual(self.pixels.values,[(20,12,0)]*10)
            outputs.apply('danger',0)
            self.assertEqual(self.pixels.values,[(20,0,0)]*10)
            outputs.apply('danger',250)
            self.assertEqual(self.pixels.values,[(0,0,0)]*10)
            outputs.apply_test((True,True,True),0)
            self.assertEqual(len(set(self.pixels.values)),3)
            outputs.off()
            self.assertEqual(self.pixels.values,[(0,0,0)]*10)
    def test_current_pin_plan_has_no_overlap(self):
        with patch.multiple(firmware.C, LED_STICK_ENABLED=True, LED_STICK_PIN=16), patch('rgb_stick.RGBStick',return_value=self.stick):
            system=firmware.System()
            self.assertEqual(system.peripherals._validation,{})
            self.assertEqual((P.MP3_UART_ID,P.MP3_TX_PIN,P.MP3_RX_PIN),(1,4,5))
            self.assertEqual((P.SHT40_I2C_ID,P.SHT40_SDA_PIN,P.SHT40_SCL_PIN),(0,8,9))
    def test_init_failure_is_reported_without_disabling_gas_model(self):
        with patch.multiple(firmware.C, LED_STICK_ENABLED=True), patch('rgb_stick.RGBStick',side_effect=OSError('mock')):
            outputs=firmware.Outputs()
            self.assertIsNotNone(outputs.led_error)
            outputs.apply('danger',0)
