"""Execute the actual POST battery method without importing hardware drivers."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import unittest


class PostBatteryThresholdTests(unittest.TestCase):
    def test_usb_selects_external_battery_and_restores_gate(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'boot_post.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'SystemPOST')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'check_bat')
        convert = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == 'battery_voltage_from_raw')
        ns = {'BATTERY_EMPTY_V': 3.45, 'BATTERY_ADC_OFFSET_V': .10,
              'time': SimpleNamespace(sleep_ms=Mock())}
        exec(compile(ast.Module(body=[convert, method], type_ignores=[]),
                     'boot_post.py', 'exec'), ns)
        post = SimpleNamespace(_check_start=Mock(), _check_end=Mock())
        battery = SimpleNamespace(read_u16=Mock(return_value=int((4.1 - .10) / 6.6 * 65535)))
        vsys = SimpleNamespace(read_u16=Mock(return_value=int(3.7 / 9.9 * 65535)))
        gate = SimpleNamespace(value=Mock())
        ns['check_bat'](post, battery, gate, vsys, True)
        post._check_start.assert_called_with('BATTERY')
        self.assertIn('4.10V', post._check_end.call_args.args[1])
        vsys.read_u16.assert_not_called()
        self.assertEqual([call.args for call in gate.value.call_args_list], [(0,), (1,)])
        gate.value.reset_mock()
        battery.read_u16.reset_mock()
        ns['check_bat'](post, battery, gate, vsys, False)
        post._check_start.assert_called_with('VSYS')
        self.assertIn('3.70V', post._check_end.call_args.args[1])
        battery.read_u16.assert_not_called()
        gate.value.assert_not_called()
        battery.read_u16.side_effect = OSError('ADC failed')
        with self.assertRaises(OSError):
            ns['check_bat'](post, battery, gate, vsys, True)
        self.assertEqual([call.args for call in gate.value.call_args_list], [(0,), (1,)])

    def test_empty_low_good_boundaries(self):
        source = (Path(__file__).resolve().parents[1] / 'boot_post.py').read_text()
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'SystemPOST')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'check_bat')
        namespace = {
            'BATTERY_EMPTY_V': 3.45,
            'time': SimpleNamespace(sleep_ms=lambda _: None),
            'battery_voltage_from_raw': lambda raw, usb_power=False: raw,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'check_bat', 'exec'),
             namespace)
        wireless = 'battery_voltage_from_raw' in ast.unparse(method)
        for voltage, expected in (
            (3.44, ('WARN_RED', 'EMPTY')),
            (3.45, ('WARN_RED', 'EMPTY')),
            (3.451, ('WARN', 'LOW')),
            (3.50, ('WARN', 'LOW')),
            (3.599, ('WARN', 'LOW')),
            (3.60, ('OK', 'Good')),
            (3.61, ('OK', 'Good')),
            (3.70, ('OK', 'Good')),
            (3.90, ('OK', 'Good')),
            (4.20, ('OK', 'Good')),
        ):
            with self.subTest(voltage=voltage):
                results, enable_values = [], []
                post = SimpleNamespace(
                    _check_start=lambda _: None,
                    _check_end=lambda status, text: results.append((status, text)),
                )
                raw = voltage if wireless else (voltage - .174) / (3.3 * 2) * 65535
                namespace['check_bat'](
                    post, SimpleNamespace(read_u16=lambda: raw),
                    SimpleNamespace(value=enable_values.append),
                )
                self.assertEqual(results[0][0], expected[0])
                self.assertIn('(' + expected[1] + ')', results[0][1])
                self.assertEqual(post.low_battery, voltage <= 3.45)
                self.assertEqual(enable_values, [] if wireless else [0, 1])


if __name__ == '__main__':
    unittest.main()
