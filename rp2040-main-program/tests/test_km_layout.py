import ast
from pathlib import Path
import unittest


class Screen:
    def __init__(self):
        self.calls = []

    def draw_gbk(self, data, x, y, color, background, scale=1):
        self.calls.append((data, x, y, scale))


class KmLayoutTests(unittest.TestCase):
    def test_values_fit_without_losing_k_or_decimal(self):
        source = Path(__file__).resolve().parents[1] / 'main.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'draw_km_post')
        screen = Screen()
        namespace = {'tft': screen, 'GREEN': 0x07e0}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        draw = namespace['draw_km_post']
        for value, x, scale, height, spaced in (
            ('999.9', 220, 2, 32, False),
            ('100000.0', 220, 2, 32, False),
            ('1000.0', 92, 3, 48, True),
            ('100000.0', 92, 3, 48, True),
            ('100000.0', 68, 2, 32, True),
        ):
            with self.subTest(value=value, x=x):
                draw(value, x, 80, scale, height, 0, spaced=spaced)
                label, actual_x, y, actual_scale = screen.calls[-1]
                self.assertEqual(label.decode(), value + (' K' if spaced else 'K'))
                self.assertLessEqual(actual_x + len(label) * 8 * actual_scale, 320)
                self.assertGreaterEqual(y, 80)
                self.assertLessEqual(y + 16 * actual_scale, 80 + height)
        self.assertEqual(screen.calls[0][3], 2)

    def test_four_digit_value_keeps_large_digits_and_small_k(self):
        source = Path(__file__).resolve().parents[1] / 'main.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'draw_km_post')
        screen = Screen()
        namespace = {'tft': screen, 'GREEN': 0x07e0}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        for value in ('1000.0', '1234.5', '9999.9'):
            screen.calls.clear()
            namespace['draw_km_post'](value, 220, 80, 2, 32, 0)
            self.assertEqual(b''.join(call[0] for call in screen.calls),
                             (value + 'K').encode())
            self.assertEqual([call[3] for call in screen.calls], [2] * 6 + [1])
            self.assertEqual([call[1] for call in screen.calls],
                             [220, 234, 248, 262, 276, 290, 308])
            self.assertEqual(screen.calls[-1][2], 96)
            self.assertLessEqual(screen.calls[-1][1] + 8, 320)


if __name__ == '__main__':
    unittest.main()
