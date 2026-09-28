import ast
from pathlib import Path
import unittest

from history_store import make_history_record


class Screen:
    def __init__(self):
        self.draws = []
        self.fills = []

    def draw_gbk(self, data, x, y, color, background, scale=1):
        self.draws.append((data, x, y))

    def fill_rect(self, x, y, width, height, color):
        self.fills.append((x, y, width, height))


class HistoryGpsTests(unittest.TestCase):
    def test_saved_history_preserves_its_coordinates(self):
        stored = make_history_record('2026-09-27 12:00', {
            'type': 'train_data_full',
            'basic': {'train_no': '12345'},
            'extended': {'lon': "119°02.0000' E", 'lat': "29°02.0000' N"},
        })
        self.assertEqual(stored['d']['extended']['lon'], "119°02.0000' E")
        self.assertEqual(stored['d']['extended']['lat'], "29°02.0000' N")

    def test_history_uses_its_own_coordinates_and_clears_missing_values(self):
        source = Path(__file__).resolve().parents[1] / 'main.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'display_train_data')
        screen = Screen()
        namespace = {
            'tft': screen, 'BLACK': 0, 'WHITE': 1, 'YELLOW': 2,
            'CYAN': 3, 'GREEN': 4, 'GRAY': 5, 'last_screen_layout': None,
            'total_count': 2,
            'format_history_time': lambda value: value,
            'safe_fill_rect': lambda *args: None,
            'draw_km_post': lambda *args, **kwargs: None,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        display = namespace['display_train_data']
        basic = {'train_no': '12345'}
        latest = {'lon': "120°01.0000' E", 'lat': "30°01.0000' N"}
        older = {'lon': "119°02.0000' E", 'lat': "29°02.0000' N"}

        display(basic, latest, False)
        screen.draws.clear()
        display(basic, older, False, True, '2026-09-27 12:00', 0)
        gps_draws = [data for data, x, y in screen.draws if data.startswith(b'GPS:')]
        self.assertEqual(gps_draws, [b"GPS: 119 02.0000' E / 29 02.0000' N"])
        self.assertIn((0, 192, 320, 18), screen.fills)

        screen.draws.clear()
        display(basic, {}, False, True, '2026-09-27 11:00', 1)
        gps_draws = [data for data, x, y in screen.draws if data.startswith(b'GPS:')]
        self.assertEqual(gps_draws, [b'GPS: --- / ---'])


if __name__ == '__main__':
    unittest.main()
