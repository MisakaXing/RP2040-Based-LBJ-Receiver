import importlib.util
import pathlib
import sys
import types
import unittest


class _FakePIO:
    SHIFT_LEFT = 0
    JOIN_RX = 1


fake_rp2 = types.ModuleType("rp2")
fake_rp2.PIO = _FakePIO
fake_rp2.asm_pio = lambda **_kwargs: (lambda func: func)
fake_rp2.StateMachine = object
sys.modules.setdefault("rp2", fake_rp2)
sys.modules.setdefault("machine", types.ModuleType("machine"))

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "rp2040-main-program"
    / "lbj_receiver.py"
)
spec = importlib.util.spec_from_file_location("lbj_receiver_under_test", MODULE_PATH)
lbj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lbj)


class FakeClock:
    now = 0

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(value, delta):
        return value + delta

    @staticmethod
    def sleep_ms(_delay):
        return None


class EmptyStateMachine:
    def rx_fifo(self):
        return 0


class LBJReceiverTests(unittest.TestCase):
    def setUp(self):
        FakeClock.now = 0
        lbj.time = FakeClock

    def make_fragment_receiver(self):
        receiver = lbj.LBJReceiver.__new__(lbj.LBJReceiver)
        receiver.pending_fragments = []
        receiver.callback_errors = 0
        receiver.callback = None
        receiver.last_corrupt_log = -receiver.DIAGNOSTIC_LOG_INTERVAL_MS
        receiver.last_resync_log = -receiver.DIAGNOSTIC_LOG_INTERVAL_MS
        receiver.last_align_log = -receiver.DIAGNOSTIC_LOG_INTERVAL_MS
        receiver.last_queue_drop_log = -receiver.DIAGNOSTIC_LOG_INTERVAL_MS
        receiver.resync_count = 0
        receiver.corrupt_messages = 0
        receiver.corrupt_since_log = 0
        receiver.batch_position = 0
        receiver.codewords_seen = 0
        receiver.corrected_codewords = 0
        receiver.corrected_sync_words = 0
        receiver.soft_sync_locks = 0
        receiver.uncorrectable_codewords = 0
        receiver.lbj_address_words = 0
        receiver.foreign_address_words = 0
        receiver.current_address = ""
        receiver.numeric_output = ""
        receiver.bad_codeword_streak = 0
        receiver.pending_fei_hz = None
        receiver.pending_afc_hz = None
        receiver.rx_group_id = 0
        return receiver

    def test_fixed_queue_is_bounded_and_keeps_newest_items(self):
        queue = lbj.FixedQueue(3)
        self.assertTrue(queue.put(1))
        queue.put(2)
        queue.put(3)
        self.assertFalse(queue.put(4))
        self.assertEqual(queue.dropped, 1)
        self.assertEqual([queue.get(), queue.get(), queue.get()], [2, 3, 4])
        self.assertIsNone(queue.get())

    def test_raw_message_keeps_its_receive_group(self):
        receiver = self.make_fragment_receiver()
        receiver.raw_queue = lbj.FixedQueue(2)
        receiver.current_address = "1234000-F1"
        receiver.numeric_output = "153 313 1233"
        receiver.current_rssi = "-90.0dBm"
        receiver._report_pending_frequency_error = lambda: None

        receiver._flush_message()
        receiver._advance_rx_group()

        self.assertEqual(
            receiver.raw_queue.get(),
            ("1234000-F1", "153 313 1233", "-90.0dBm", 0),
        )
        self.assertEqual(receiver.rx_group_id, 1)

    def test_stable_radio_profile_constants_are_unchanged(self):
        self.assertEqual(lbj.LBJReceiver.BASE_FREQ_HZ, 821237500)
        self.assertEqual(lbj.LBJReceiver.RXBW, 0x0D)
        self.assertEqual(lbj.LBJReceiver.AFCBW, 0x0B)
        self.assertEqual(lbj.LBJReceiver.LNA_FIXED_GAIN_BOOST, 0x23)
        self.assertEqual(lbj.LBJReceiver.RXCONFIG_AFC_PREAMBLE, 0x16)

    def test_sync_path_collects_rssi_and_fei_diagnostics(self):
        receiver = self.make_fragment_receiver()
        receiver.synced = False
        receiver.bit_count = 12
        receiver.current_cw = 123
        receiver.bad_codeword_streak = 2
        receiver.last_sync_time = 0
        receiver.sync_count = 0
        receiver.current_rssi = "N/A"
        receiver.rssi_val = "N/A"
        receiver.batch_position = 0
        receiver.pending_fei_hz = None
        receiver.pending_afc_hz = None
        receiver.get_rssi = lambda: "-90.0dBm"
        receiver._read_fei_hz = lambda: 122.0
        receiver._read_afc_hz = lambda: -61.0
        receiver._record_sync(100)
        self.assertTrue(receiver.synced)
        self.assertEqual(receiver.current_rssi, "-90.0dBm")
        self.assertEqual(receiver.pending_fei_hz, 122.0)
        self.assertEqual(receiver.pending_afc_hz, -61.0)

    def test_address_codeword_frame_bits_form_full_ric(self):
        receiver = self.make_fragment_receiver()
        receiver.synced = True
        receiver.batch_position = 6
        receiver.current_address = ""
        receiver.numeric_output = ""
        receiver.bad_codeword_streak = 0
        receiver.codewords_seen = 0
        receiver.corrected_codewords = 0
        receiver.uncorrectable_codewords = 0
        receiver.POCSAG_SYNC = 0x7CD215D8
        receiver.POCSAG_IDLE = 0x7A89C197
        receiver._correct_bch = lambda value: (value, 0)

        address_field = 154250
        function = 2
        payload = (address_field << 2) | function
        receiver._decode_codeword(payload << 11, 0)

        self.assertEqual(receiver.current_address, "1234003-F2")

    def test_corrected_batch_sync_resets_frame_position(self):
        receiver = self.make_fragment_receiver()
        receiver.synced = True
        receiver.batch_position = 11
        receiver.POCSAG_SYNC = 0x7CD215D8
        receiver._correct_bch = lambda _value: (receiver.POCSAG_SYNC, 2)
        receiver.sync_count = 0
        receiver.last_sync_time = 0
        receiver.bit_count = 9
        receiver.current_cw = 0xFF
        receiver.get_rssi = lambda: "-90.0dBm"

        receiver._decode_codeword(0x12345678, 77)

        self.assertEqual(receiver.batch_position, 0)
        self.assertEqual(receiver.sync_count, 1)
        self.assertEqual(receiver.corrected_sync_words, 1)

    def test_bch_correctable_sync_can_acquire_receiver_lock(self):
        receiver = self.make_fragment_receiver()
        receiver.POCSAG_SYNC = 0x7CD215D8
        receiver.sync_window = receiver.POCSAG_SYNC ^ (1 << 5)
        receiver.synced = False
        receiver.bit_count = 0
        receiver.current_cw = 0
        receiver.batch_position = 0
        receiver.bad_codeword_streak = 0
        receiver.last_sync_time = 0
        receiver.sync_count = 0
        receiver.current_rssi = "N/A"
        receiver.rssi_val = "N/A"
        receiver.get_rssi = lambda: "-90.0dBm"
        receiver._read_fei_hz = lambda: 0.0
        receiver._read_afc_hz = lambda: 0.0

        self.assertTrue(receiver._try_acquire_sync(200))
        self.assertTrue(receiver.synced)
        self.assertEqual(receiver.soft_sync_locks, 1)
        self.assertEqual(receiver.corrected_sync_words, 1)

    def test_foreign_address_cannot_take_over_lbj_decoder(self):
        receiver = self.make_fragment_receiver()
        receiver.synced = True
        receiver.batch_position = 0
        receiver.current_address = "1234000-F1"
        receiver.numeric_output = "153 313 1233"
        receiver.POCSAG_SYNC = 0x7CD215D8
        receiver.POCSAG_IDLE = 0x7A89C197
        receiver._correct_bch = lambda value: (value, 0)
        flushed = []

        def flush_message():
            flushed.append(True)
            receiver.current_address = ""
            receiver.numeric_output = ""

        receiver._flush_message = flush_message
        foreign_address_field = 40000
        receiver._decode_codeword((foreign_address_field << 2) << 11, 0)

        self.assertEqual(flushed, [True])
        self.assertEqual(receiver.current_address, "")
        self.assertEqual(receiver.foreign_address_words, 1)

    def test_radio_profile_health_check_detects_register_drift(self):
        receiver = self.make_fragment_receiver()
        receiver._expected_frf = 0xD23456
        registers = {
            0x42: 0x12,
            0x01: 0x05,
            receiver.REG_FRF_MSB: 0xD2,
            receiver.REG_FRF_MID: 0x34,
            receiver.REG_FRF_LSB: 0x56,
            receiver.REG_RXBW: receiver.RXBW,
            receiver.REG_AFCBW: receiver.AFCBW,
            0x0C: receiver.LNA_FIXED_GAIN_BOOST,
            0x31: 0x00,
            0x40: 0x00,
            receiver.REG_PREAMBLEDETECT: receiver.PREAMBLE_DETECT,
            receiver.REG_RXCONFIG: receiver.RXCONFIG_AFC_PREAMBLE,
        }
        receiver._r = registers.__getitem__
        self.assertIsNone(receiver._profile_fault())
        registers[receiver.REG_RXBW] = 0x00
        self.assertEqual(receiver._profile_fault(), "rxbw")

    def test_lbj_basic_and_extended_ric_pair_merges_across_function_numbers(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        basic = {
            "type": "basic_only",
            "ric": "1234000-F0",
            "_rx_group": 7,
            "rssi": "-90.0dBm",
            "raw": "153 313 1233",
            "basic": {"train_no": "153", "speed_kmh": "313", "km_post": 123.3},
        }
        unrelated = {"type": "time_sync", "time": "12:34", "raw": "*1234"}
        extended = {
            "type": "extended_only",
            "ric": "1234002-F3",
            "_rx_group": 7,
            "rssi": "-88.0dBm",
            "raw": "extended",
            "extended": {"loco_type": "CR400BF-5395"},
        }

        receiver._handle_parsed_msg(basic)
        FakeClock.now = 250
        receiver._handle_parsed_msg(unrelated)
        FakeClock.now = 500
        receiver._handle_parsed_msg(extended)

        self.assertEqual([item["type"] for item in emitted], ["time_sync", "train_data_merged"])
        self.assertEqual(emitted[1]["basic"]["train_no"], "153")
        self.assertEqual(emitted[1]["extended"]["loco_type"], "CR400BF-5395")
        self.assertEqual(emitted[1]["basic_ric"], "1234000-F0")
        self.assertEqual(emitted[1]["extended_ric"], "1234002-F3")

    def test_unrelated_lbj_address_cannot_merge_with_basic(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F1",
            "raw": "153 313 1233",
            "basic": {"train_no": "153", "speed_kmh": "313", "km_post": 123.3},
        })
        FakeClock.now = 250
        receiver._handle_parsed_msg({
            "type": "extended_only",
            "ric": "1234003-F1",
            "raw": "not an LBJ extended address",
            "extended": {"loco_type": "CR400BF-5395"},
        })

        self.assertEqual([item["type"] for item in emitted], ["extended_only"])
        FakeClock.now = receiver.MERGE_TIMEOUT_MS + 1
        receiver._flush_pending_fragments(FakeClock.now)
        self.assertEqual([item["type"] for item in emitted], ["extended_only", "basic_only"])

    def test_fragments_from_different_receive_groups_never_merge(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F1",
            "_rx_group": 21,
            "raw": "101 100 1000",
            "basic": {"train_no": "101", "speed_kmh": "100", "km_post": 100.0},
        })
        FakeClock.now = 20
        receiver._handle_parsed_msg({
            "type": "extended_only",
            "ric": "1234002-F1",
            "_rx_group": 22,
            "raw": "extended",
            "extended": {"loco_type": "HXD3D-0097"},
        })

        self.assertEqual([item["type"] for item in emitted], ["basic_only"])
        self.assertEqual(len(receiver.pending_fragments), 1)
        self.assertEqual(receiver.pending_fragments[0][1]["_rx_group"], 22)
        FakeClock.now = receiver.MERGE_TIMEOUT_MS + 30
        receiver._flush_pending_fragments(FakeClock.now)
        self.assertEqual([item["type"] for item in emitted], ["basic_only", "extended_only"])

    def test_nearest_pending_basic_is_used_for_lbj_extension(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F1",
            "_rx_group": 8,
            "raw": "101 100 1000",
            "basic": {"train_no": "101", "speed_kmh": "100", "km_post": 100.0},
        })
        FakeClock.now = 100
        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F3",
            "_rx_group": 8,
            "raw": "202 120 2000",
            "basic": {"train_no": "202", "speed_kmh": "120", "km_post": 200.0},
        })
        FakeClock.now = 200
        receiver._handle_parsed_msg({
            "type": "extended_only",
            "ric": "1234002-F0",
            "_rx_group": 8,
            "raw": "extended",
            "extended": {"loco_type": "HXD3D-0097"},
        })

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["type"], "train_data_merged")
        self.assertEqual(emitted[0]["basic"]["train_no"], "202")
        self.assertEqual(len(receiver.pending_fragments), 1)
        self.assertEqual(receiver.pending_fragments[0][1]["basic"]["train_no"], "101")

    def test_full_message_flushes_waiting_fragment_without_dropping_it(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)
        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F0",
            "_rx_group": 9,
            "raw": "42 80 100",
            "basic": {"train_no": "42"},
        })
        receiver._handle_parsed_msg({
            "type": "train_data_full",
            "ric": "1234000-F2",
            "_rx_group": 9,
            "raw": "full",
            "basic": {"train_no": "42"},
            "extended": {"loco_type": "DF11-0417"},
        })
        FakeClock.now = 5000
        receiver._flush_pending_fragments(FakeClock.now)

        self.assertEqual([item["type"] for item in emitted], ["basic_only", "train_data_full"])

    def test_pending_fragment_expires_without_being_lost(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)
        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F1",
            "raw": "7 60 500",
            "basic": {"train_no": "7"},
        })
        FakeClock.now = receiver.MERGE_TIMEOUT_MS + 1
        receiver._flush_pending_fragments(FakeClock.now)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["type"], "basic_only")

    def test_pending_fragment_storage_stays_bounded_under_unrelated_traffic(self):
        receiver = self.make_fragment_receiver()
        emitted = []
        receiver.set_callback(emitted.append)
        for ric in range(100):
            FakeClock.now = ric
            receiver._handle_parsed_msg({
                "type": "basic_only",
                "ric": f"1234000-F{ric % 4}",
                "raw": "1 2 3",
                "basic": {"train_no": str(ric + 1)},
            })
        self.assertLessEqual(
            len(receiver.pending_fragments), receiver.MAX_PENDING_FRAGMENTS
        )
        self.assertEqual(
            len(emitted) + len(receiver.pending_fragments), 100
        )

    def test_three_bad_codewords_force_resynchronization_even_without_address(self):
        receiver = self.make_fragment_receiver()
        receiver.synced = True
        receiver.sync_window = 0
        receiver.bit_count = 0
        receiver.current_cw = 0
        receiver.current_address = ""
        receiver.numeric_output = ""
        receiver.pending_fei_hz = None
        receiver.pending_afc_hz = None
        receiver.bad_codeword_streak = 0
        receiver.POCSAG_SYNC = 0x7CD215D8
        receiver.codewords_seen = 0
        receiver.corrected_codewords = 0
        receiver.uncorrectable_codewords = 0
        receiver._correct_bch = lambda _word: (_word, -1)

        receiver._decode_codeword(1, 0)
        receiver._decode_codeword(2, 0)
        self.assertTrue(receiver.synced)
        receiver._decode_codeword(3, 0)
        self.assertFalse(receiver.synced)
        self.assertEqual(receiver.uncorrectable_codewords, 3)

    def test_long_idle_does_not_trigger_blind_radio_restart(self):
        receiver = self.make_fragment_receiver()
        receiver.sm = EmptyStateMachine()
        receiver.synced = True
        receiver.raw_queue = lbj.FixedQueue(2)
        receiver.words_seen = 0
        receiver.last_word_time = 0
        receiver.last_timeout_check = 0
        receiver._service_radio_health = lambda _now: None
        FakeClock.now = 120000

        receiver.tick()

        self.assertTrue(receiver.synced)
        self.assertEqual(receiver.words_seen, 0)

    def test_partial_basic_keeps_valid_train_number(self):
        receiver = self.make_fragment_receiver()
        parsed = receiver._parse_basic("153 XXXXX 1233")
        self.assertEqual(parsed["train_no"], "153")
        self.assertEqual(parsed["speed_kmh"], "---")
        self.assertEqual(parsed["km_post"], 123.3)

    def test_partial_loco_code_matching_uses_micropython_safe_padding(self):
        receiver = self.make_fragment_receiver()
        receiver.loco_types = {"38": "HXD3D", "308": "CRH380"}

        code, name = receiver._resolve_loco_code("0X8")

        self.assertEqual(code, "38")
        self.assertEqual(name, "HXD3D")

    def test_bch_table_corrects_one_and_two_bit_errors(self):
        receiver = self.make_fragment_receiver()
        receiver.BCH_POLY = 0x769
        receiver._init_syndrome_table()
        idle = 0x7A89C197
        fixed, errors = receiver._correct_bch(idle ^ (1 << 7))
        self.assertEqual((fixed, errors), (idle, 1))
        fixed, errors = receiver._correct_bch(idle ^ (1 << 7) ^ (1 << 19))
        self.assertEqual((fixed, errors), (idle, 2))


if __name__ == "__main__":
    unittest.main()
