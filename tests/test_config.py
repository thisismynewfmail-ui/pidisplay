"""Settings schema, validation and persistence."""

import json
import os
import tempfile
import unittest

from aperture import config as C
from aperture.config import CONTEXT_MAX, CONTEXT_MIN, CONTEXT_STEP, Config


class TestSchema(unittest.TestCase):
    def test_labels_fit_the_menu_row(self):
        for setting in C.SETTINGS:
            self.assertLessEqual(len(setting.label), C.Setting.LABEL_BUDGET,
                                 setting.key)

    def test_keys_are_two_levels(self):
        for setting in C.SETTINGS:
            self.assertEqual(setting.key.count("."), 1, setting.key)

    def test_every_setting_has_help(self):
        for setting in C.SETTINGS:
            self.assertTrue(setting.help.strip(), setting.key)

    def test_formatted_values_fit_a_row(self):
        config = Config(path="/dev/null")
        for setting in C.SETTINGS:
            for value in filter(None, [setting.default, setting.minimum,
                                       setting.maximum]):
                self.assertLessEqual(len(setting.format(setting.coerce(value))),
                                     14, setting.key)

    def test_enums_default_to_a_valid_choice(self):
        for setting in C.SETTINGS:
            if setting.kind == "enum":
                self.assertIn(setting.default, setting.choices, setting.key)


class TestContextSetting(unittest.TestCase):
    """The context window is the headline control; its behaviour is specified."""

    def setUp(self):
        self.config = Config(path="/dev/null")

    def test_steps_by_512(self):
        start = self.config.get("engine.context")
        self.config.adjust("engine.context", 1)
        self.assertEqual(self.config.get("engine.context"), start + CONTEXT_STEP)
        self.config.adjust("engine.context", -1)
        self.assertEqual(self.config.get("engine.context"), start)

    def test_every_reachable_value_is_a_multiple_of_512(self):
        self.config.set("engine.context", CONTEXT_MIN)
        for _ in range(40):
            self.config.adjust("engine.context", 1)
            self.assertEqual(self.config.get("engine.context") % CONTEXT_STEP, 0)

    def test_clamps_at_both_ends(self):
        for _ in range(200):
            self.config.adjust("engine.context", -1)
        self.assertEqual(self.config.get("engine.context"), CONTEXT_MIN)
        for _ in range(400):
            self.config.adjust("engine.context", 1, big=True)
        self.assertEqual(self.config.get("engine.context"), CONTEXT_MAX)

    def test_big_step_is_a_multiple_of_the_small_one(self):
        setting = self.config.setting("engine.context")
        self.assertEqual(setting.big_step % setting.step, 0)


class TestCoercion(unittest.TestCase):
    def setUp(self):
        self.config = Config(path="/dev/null")

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(self.config.set("sampling.temperature", 99.0), 2.0)
        self.assertEqual(self.config.set("sampling.temperature", -5.0), 0.0)

    def test_garbage_falls_back_to_the_default(self):
        self.assertEqual(self.config.set("endpoint.port", "not-a-port"), 8080)

    def test_unknown_enum_falls_back(self):
        self.assertEqual(self.config.set("chat.persona", "nonsense"), "terminal")

    def test_booleans_accept_strings(self):
        self.assertIs(self.config.set("engine.mlock", "yes"), True)
        self.assertIs(self.config.set("engine.mlock", "off"), False)

    def test_enum_adjust_wraps(self):
        setting = self.config.setting("chat.persona")
        value = setting.choices[-1]
        self.assertEqual(setting.adjust(value, 1), setting.choices[0])


class TestPersistence(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path=path)
            config.set("engine.context", 8192)
            config.set("endpoint.host", "10.0.0.5")
            self.assertTrue(config.save())
            reloaded = Config.load(path)
            self.assertEqual(reloaded.get("engine.context"), 8192)
            self.assertEqual(reloaded.get("endpoint.host"), "10.0.0.5")

    def test_corrupt_file_falls_back_to_defaults(self):
        """A half-written config must not strand the device."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w") as handle:
                handle.write('{"engine": {"context": 40')
            config = Config.load(path)
            self.assertTrue(config.load_error)
            self.assertEqual(config.get("engine.context"),
                             C.SETTINGS_BY_KEY["engine.context"].default)

    def test_out_of_range_stored_values_are_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w") as handle:
                json.dump({"engine": {"context": 99999999},
                           "sampling": {"temperature": 50}}, handle)
            config = Config.load(path)
            self.assertEqual(config.get("engine.context"), CONTEXT_MAX)
            self.assertEqual(config.get("sampling.temperature"), 2.0)

    def test_save_is_atomic(self):
        """No temporary file may survive a successful save."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path=path)
            config.save()
            leftovers = [n for n in os.listdir(directory) if n != "config.json"]
            self.assertEqual(leftovers, [])

    def test_restart_detection(self):
        config = Config(path="/dev/null")
        before = config.snapshot()
        config.set("engine.context", 8192)
        config.set("sampling.temperature", 0.1)
        changed = C.restart_required(before, config.snapshot())
        self.assertIn("engine.context", changed)
        self.assertNotIn("sampling.temperature", changed)


if __name__ == "__main__":
    unittest.main()
