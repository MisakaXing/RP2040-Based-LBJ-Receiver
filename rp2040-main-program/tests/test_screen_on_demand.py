import ast
import pathlib
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


class FakePin:
    def __init__(self):
        self.level = 0
        self.values = []

    def value(self, level=None):
        if level is None:
            return self.level
        self.level = level
        self.values.append(level)


def screen_namespace():
    tree = ast.parse(MAIN_PATH.read_text())
    wanted_assignments = {
        "SCR_OFF_OPTS",
        "SCR_OFF_MS",
        "SCR_OFF_ON_DEMAND_INDEX",
    }
    wanted_functions = {
        "set_screen_power",
        "handle_screen_button_event",
        "wake_screen_for_train",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if names & wanted_assignments:
                body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            body.append(node)

    namespace = {
        "pin_bl": FakePin(),
        "screen_is_on": True,
        "cfg_scr_idx": 3,
        "beep_count": 0,
    }

    def beep():
        namespace["beep_count"] += 1

    namespace["beep"] = beep
    exec(
        compile(ast.Module(body=body, type_ignores=[]), str(MAIN_PATH), "exec"),
        namespace,
    )
    return namespace


class ScreenOnDemandTests(unittest.TestCase):
    def test_menu_has_stable_on_demand_option_and_no_timeout(self):
        namespace = screen_namespace()
        self.assertEqual(
            namespace["SCR_OFF_OPTS"],
            ["30s", "1min", "5min", "never", "on demand"],
        )
        self.assertEqual(namespace["SCR_OFF_ON_DEMAND_INDEX"], 4)
        self.assertEqual(namespace["SCR_OFF_MS"][4], -1)

        source = MAIN_PATH.read_text()
        self.assertIn("% len(SCR_OFF_OPTS)", source)
        self.assertIn("screen_timeout_ms >= 0", source)

    def test_power_key_toggles_screen_in_on_demand_mode(self):
        namespace = screen_namespace()
        namespace["cfg_scr_idx"] = namespace["SCR_OFF_ON_DEMAND_INDEX"]

        consumed = namespace["handle_screen_button_event"](True)
        self.assertTrue(consumed)
        self.assertFalse(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [1])
        self.assertEqual(namespace["beep_count"], 1)

        consumed = namespace["handle_screen_button_event"](True)
        self.assertTrue(consumed)
        self.assertTrue(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [1, 0])
        self.assertEqual(namespace["beep_count"], 2)

    def test_other_keys_do_not_wake_or_operate_while_manually_off(self):
        namespace = screen_namespace()
        namespace["cfg_scr_idx"] = namespace["SCR_OFF_ON_DEMAND_INDEX"]
        namespace["set_screen_power"](False)
        namespace["pin_bl"].values.clear()

        consumed = namespace["handle_screen_button_event"](False)

        self.assertTrue(consumed)
        self.assertFalse(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [])
        self.assertEqual(namespace["beep_count"], 0)

    def test_train_does_not_override_manual_off(self):
        namespace = screen_namespace()
        namespace["cfg_scr_idx"] = namespace["SCR_OFF_ON_DEMAND_INDEX"]
        namespace["set_screen_power"](False)
        namespace["pin_bl"].values.clear()

        namespace["wake_screen_for_train"]()

        self.assertFalse(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [])

    def test_legacy_modes_keep_any_key_and_train_auto_wake(self):
        namespace = screen_namespace()
        namespace["cfg_scr_idx"] = 3
        namespace["set_screen_power"](False)
        namespace["pin_bl"].values.clear()

        consumed = namespace["handle_screen_button_event"](False)

        self.assertTrue(consumed)
        self.assertTrue(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [0])
        self.assertEqual(namespace["beep_count"], 1)

        namespace["set_screen_power"](False)
        namespace["pin_bl"].values.clear()
        namespace["wake_screen_for_train"]()
        self.assertTrue(namespace["screen_is_on"])
        self.assertEqual(namespace["pin_bl"].values, [0])


if __name__ == "__main__":
    unittest.main()
