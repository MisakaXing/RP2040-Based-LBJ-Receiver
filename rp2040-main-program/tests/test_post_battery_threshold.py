"""Execute the actual POST battery method without importing hardware drivers."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class PostBatteryThresholdTests(unittest.TestCase):
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
            'battery_voltage_from_raw': lambda raw: raw,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'check_bat', 'exec'),
             namespace)
        wireless = 'battery_voltage_from_raw(raw)' in ast.unparse(method)
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

