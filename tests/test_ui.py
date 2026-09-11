"""
End-to-end interface tests.

These drive the real screens, the real driver and the real streaming client
against the HD44780 emulator and a stand-in llama.cpp server.  Nothing above
the transport is mocked, so a passing run means the panel would show what is
asserted here.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.drive import Harness
from tools.fake_llama import STATE, serve

from aperture.hal import keys as K
from aperture.llm import session as S

_PORT = 18700


def next_port():
    global _PORT
    _PORT += 1
    return _PORT


class UITestCase(unittest.TestCase):
    """Brings up a fake engine and a harness for each test."""

    reply = "Address 0x27 answered. All four rows are live."
    reasoning = ""
    delay = 0.0

    def setUp(self):
        self.port = next_port()
        self.server = serve(self.port)
        STATE.reply = self.reply
        STATE.reasoning = self.reasoning
        STATE.delay = self.delay
        STATE.slots.clear()
        STATE.requests.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.harness = Harness(os.path.join(self.directory.name, "config.json"),
                               os.path.join(self.directory.name, "models"),
                               port=self.port)
        os.makedirs(os.path.join(self.directory.name, "models"), exist_ok=True)
        self.harness.push_chat()
        self.boot_engine()

    def tearDown(self):
        self.harness.close()
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def boot_engine(self):
        from aperture.ui.boot import EngineStage
        self.harness.push(EngineStage(self.harness.app, standalone=True))
        self.wait_for(lambda: self.harness.app.engine_ready, 8.0)

    # -- helpers ------------------------------------------------------------

    @property
    def app(self):
        return self.harness.app

    @property
    def chat(self):
        from aperture.ui.chat import ChatScreen
        return self.app.find_screen(ChatScreen)

    def screen(self):
        return self.harness.screen()

    def wait_for(self, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.harness.step(1, dt=0.05)
            if predicate():
                self.harness.step(2)
                return True
            time.sleep(0.01)
        return False

    def send(self, message, timeout=8.0):
        self.harness.keyboard.type(message)
        self.harness.step(2)
        self.harness.keyboard.press(K.ENTER)
        self.harness.step(2)
        ok = self.wait_for(lambda: self.app.engine.state == S.IDLE, timeout)
        self.assertTrue(ok, "generation did not finish")

    def press(self, *keys):
        for key in keys:
            self.harness.keyboard.press(key)
        self.harness.step(3)


class TestPanelInvariants(UITestCase):
    """Properties that must hold on every frame, whatever is on screen."""

    def assert_frame_is_well_formed(self):
        rows = self.harness.emulator.text_screen()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(len(row), 20, repr(row))

    def test_geometry_holds_across_every_screen(self):
        from aperture.ui.dialogs import DiagnosticsScreen, HelpScreen
        from aperture.ui.pickers import ModelPickerScreen
        from aperture.ui.settings import SettingsScreen

        self.assert_frame_is_well_formed()
        for key in (K.F1, K.F6, K.F7, K.F3):
            self.press(key)
            self.assert_frame_is_well_formed()
            self.press(K.ESC)
            self.assert_frame_is_well_formed()

    def test_no_control_characters_leak_to_the_panel(self):
        """Only CGRAM codes 8-15 are legal below 0x20."""
        self.send("hello")
        for row in self.harness.emulator.text_screen():
            for ch in row:
                code = ord(ch)
                self.assertFalse(0 <= code < 8, f"raw control byte {code}")
                self.assertLess(code, 256)


class TestConversation(UITestCase):
    def test_send_and_receive(self):
        self.send("what responded?")
        turns = self.app.conversation.turns
        self.assertEqual(turns[0].role, S.USER)
        self.assertEqual(turns[1].role, S.ASSISTANT)
        self.assertIn("0x27", turns[1].text)

    def test_reply_appears_on_the_panel(self):
        self.send("what responded?")
        body = " ".join(self.screen())
        self.assertIn("live", body)

    def test_user_turn_is_railed(self):
        self.send("hello there")
        lines = self.chat.all_lines()
        rails = [rail for rail, _ in lines]
        self.assertIn(">", rails)

    def test_prompt_cache_survives_many_turns(self):
        """The point of the whole design: turn N costs the same as turn 1."""
        evaluated = []
        for index in range(4):
            STATE.reply = f"Reply {index}. " + "Padding sentence here. " * 3
            self.send(f"question number {index}")
            evaluated.append(self.app.engine.last_timings.prompt_tokens)
        self.assertLess(max(evaluated), 40,
                        f"prompt was re-read: {evaluated}")
        self.assertLess(max(evaluated) - min(evaluated), 20,
                        f"prompt cost grew with history: {evaluated}")

    def test_cache_reuse_is_reported(self):
        self.send("first")
        self.send("second")
        timings = self.app.engine.last_timings
        self.assertGreater(timings.cached_tokens, 0)
        self.assertGreater(timings.cache_hit_ratio, 0.5)

    def test_every_request_pins_the_same_slot(self):
        self.send("one")
        self.send("two")
        completions = [r for r in STATE.requests if r["path"] == "/completion"]
        self.assertTrue(completions)
        for request in completions:
            self.assertTrue(request["payload"].get("cache_prompt"))
            self.assertEqual(request["payload"].get("id_slot"), 0)

    def test_clear_conversation(self):
        self.send("hello")
        self.press(K.F2)
        self.press(K.ENTER)                 # confirm defaults to YES
        self.assertEqual(len(self.app.conversation.turns), 0)
        self.assertEqual(self.chat.all_lines(), [])

    def test_regenerate_replaces_the_last_reply(self):
        self.send("hello")
        STATE.reply = "A different answer entirely."
        self.press(K.F5)
        self.assertTrue(self.wait_for(
            lambda: self.app.engine.state == S.IDLE and
            "different" in self.app.conversation.turns[-1].text))
        roles = [t.role for t in self.app.conversation.turns]
        self.assertEqual(roles, [S.USER, S.ASSISTANT])


class TestStreamingAndScrolling(UITestCase):
    delay = 0.05
    reply = ("The panel reports that all four rows are live and stable. "
             "Contrast is set correctly and the backlight is on. "
             "No further action is needed at this time.")

    def test_thinking_indicator_precedes_output(self):
        self.harness.keyboard.type("status")
        self.harness.step(2)
        self.harness.keyboard.press(K.ENTER)
        self.assertTrue(self.wait_for(
            lambda: self.app.engine.state in (S.WAITING, S.PREPARING,
                                              S.STREAMING), 3.0))
        self.harness.step(2)
        rendered = " ".join(self.screen())
        self.assertTrue(any(word in rendered for word in
                            ("PREPARING", "READING", "COMPOSING")), rendered)
        self.wait_for(lambda: self.app.engine.state == S.IDLE, 12.0)

    def test_autoscroll_keeps_the_newest_line_visible(self):
        self.send("status", timeout=15.0)
        lines = self.chat.all_lines()
        self.assertGreater(len(lines), 3)
        self.assertTrue(self.chat.follow)
        self.assertEqual(self.chat.scroll, len(lines) - self.chat.view_rows)
        self.assertIn(lines[-1][1].strip(), " ".join(self.screen()))

    def test_scrolling_up_suspends_following(self):
        self.send("status", timeout=15.0)
        self.press(K.UP, K.UP)
        self.assertFalse(self.chat.follow)
        self.press(K.END)
        self.assertTrue(self.chat.follow)

    def test_home_and_end(self):
        self.send("status", timeout=15.0)
        self.press(K.HOME)
        self.assertEqual(self.chat.scroll, 0)
        self.press(K.END)
        self.assertTrue(self.chat.follow)

    def test_scroll_cannot_leave_the_transcript(self):
        self.send("status", timeout=15.0)
        for _ in range(50):
            self.harness.keyboard.press(K.UP)
        self.harness.step(6)
        self.assertEqual(self.chat.scroll, 0)
        for _ in range(50):
            self.harness.keyboard.press(K.DOWN)
        self.harness.step(6)
        total = len(self.chat.all_lines())
        self.assertLessEqual(self.chat.scroll, max(0, total - self.chat.view_rows))

    def test_abort_keeps_partial_text(self):
        self.harness.keyboard.type("status")
        self.harness.step(2)
        self.harness.keyboard.press(K.ENTER)
        self.assertTrue(self.wait_for(
            lambda: self.app.engine.state == S.STREAMING, 6.0))
        self.harness.keyboard.press(K.ESC)
        self.assertTrue(self.wait_for(
            lambda: self.app.engine.state == S.IDLE, 6.0))
        last = self.app.conversation.turns[-1]
        self.assertTrue(last.aborted)
        self.assertTrue(last.text.strip())
        self.assertLess(len(last.text), len(self.reply))


class TestCompose(UITestCase):
    def test_typing_enters_compose_mode(self):
        from aperture.ui.chat import COMPOSE, READ
        self.chat.mode = READ
        self.harness.keyboard.type("x")
        self.harness.step(2)
        self.assertEqual(self.chat.mode, COMPOSE)

    def test_escape_clears_then_leaves_compose(self):
        from aperture.ui.chat import COMPOSE, READ
        self.harness.keyboard.type("draft text")
        self.harness.step(2)
        self.press(K.ESC)
        self.assertEqual(self.chat.field.text, "")
        self.assertEqual(self.chat.mode, COMPOSE)
        self.press(K.ESC)
        self.assertEqual(self.chat.mode, READ)

    def test_caret_is_the_hardware_cursor(self):
        self.harness.keyboard.type("abc")
        self.harness.step(3)
        self.assertTrue(self.harness.emulator.blink_on)

    def test_caret_is_parked_outside_compose(self):
        from aperture.ui.chat import READ
        self.chat.mode = READ
        self.harness.step(3)
        self.assertFalse(self.harness.emulator.blink_on)

    def test_long_input_scrolls_horizontally(self):
        self.harness.keyboard.type("a" * 40)
        self.harness.step(3)
        row = self.screen()[3]
        self.assertEqual(len(row), 20)
        self.assertEqual(self.chat.field.text, "a" * 40)

    def test_control_editing(self):
        self.harness.keyboard.type("hello world")
        self.harness.step(2)
        self.harness.keyboard.press(K.CHAR, "w", ctrl=True)
        self.harness.step(2)
        self.assertEqual(self.chat.field.text, "hello ")
        self.harness.keyboard.press(K.CHAR, "u", ctrl=True)
        self.harness.step(2)
        self.assertEqual(self.chat.field.text, "")

    def test_empty_message_is_not_sent(self):
        before = len(self.app.conversation.turns)
        self.press(K.ENTER)
        self.assertEqual(len(self.app.conversation.turns), before)


class TestSettingsNavigation(UITestCase):
    def open_inference(self):
        self.press(K.F7)
        self.press(K.HOME)
        self.press(K.ENTER)

    def test_f7_opens_settings(self):
        from aperture.ui.settings import SettingsScreen
        self.press(K.F7)
        self.assertIsInstance(self.app.top, SettingsScreen)

    def test_f4_also_opens_settings(self):
        """Compact keyboards put F4 where full-size ones put F7."""
        from aperture.ui.settings import SettingsScreen
        self.press(K.F4)
        self.assertIsInstance(self.app.top, SettingsScreen)

    def test_context_steps_by_512(self):
        self.open_inference()
        start = self.app.config.get("engine.context")
        self.press(K.ENTER)                 # enter adjust mode
        self.press(K.RIGHT)
        self.assertEqual(self.app.config.get("engine.context"), start + 512)
        self.press(K.LEFT, K.LEFT)
        self.assertEqual(self.app.config.get("engine.context"), start - 512)

    def test_context_plus_minus_without_adjust_mode(self):
        self.open_inference()
        start = self.app.config.get("engine.context")
        self.press(K.PLUS)
        self.assertEqual(self.app.config.get("engine.context"), start + 512)
        self.press(K.MINUS)
        self.assertEqual(self.app.config.get("engine.context"), start)

    def test_context_clamps_at_the_floor(self):
        self.open_inference()
        for _ in range(60):
            self.harness.keyboard.press(K.MINUS)
        self.harness.step(10)
        self.assertEqual(self.app.config.get("engine.context"), 512)

    def test_escape_unwinds_to_the_chat(self):
        from aperture.ui.chat import ChatScreen
        self.open_inference()
        self.press(K.ESC)
        self.press(K.ESC)
        self.harness.step(3)
        self.assertIsInstance(self.app.top, ChatScreen)

    def test_boolean_toggles_on_enter(self):
        self.press(K.F7)
        self.press(K.HOME)
        self.press(K.ENTER)
        # Walk to a boolean row.
        for _ in range(7):
            self.harness.keyboard.press(K.DOWN)
        self.harness.step(3)
        before = self.app.config.get("engine.flash_attn")
        self.press(K.ENTER)
        self.assertNotEqual(self.app.config.get("engine.flash_attn"), before)

    def test_help_for_the_selected_setting(self):
        from aperture.ui.dialogs import MessageScreen
        self.open_inference()
        self.press(K.F1)
        self.assertIsInstance(self.app.top, MessageScreen)

    def test_f7_from_a_section_closes_settings(self):
        """F7 toggles; it must not stack a second menu over the first."""
        from aperture.ui.chat import ChatScreen
        from aperture.ui.settings import SettingsScreen
        self.open_inference()
        self.press(K.F7)
        self.assertIsInstance(self.app.top, ChatScreen)
        self.assertIsNone(self.app.find_screen(SettingsScreen))

    def test_restart_prompt_survives_leaving_settings(self):
        """A setting needing a restart must raise its prompt, not lose it."""
        from aperture.ui.dialogs import ConfirmScreen
        self.open_inference()
        self.press(K.PLUS)                  # context: restart-required
        self.press(K.ESC)                   # leave the section
        self.press(K.ESC)                   # leave settings
        self.harness.step(3)
        self.assertIsInstance(self.app.top, ConfirmScreen)

    def test_settings_survive_a_round_trip(self):
        self.open_inference()
        self.press(K.PLUS)
        expected = self.app.config.get("engine.context")
        self.press(K.ESC)
        self.press(K.ESC)
        self.harness.step(3)
        from aperture.config import Config
        reloaded = Config.load(self.app.config.path)
        self.assertEqual(reloaded.get("engine.context"), expected)


class TestReasoning(UITestCase):
    reasoning = "The operator wants the address. Keep the answer short."
    reply = "0x27."

    def test_reasoning_hidden_by_default(self):
        self.app.config.set("chat.show_reasoning", False)
        self.send("address?")
        bodies = " ".join(body for _, body in self.chat.all_lines())
        self.assertNotIn("operator wants", bodies)
        self.assertIn("0x27", bodies)

    def test_reasoning_never_leaks_tags(self):
        self.app.config.set("chat.show_reasoning", True)
        self.chat.invalidate_lines()
        self.send("address?")
        bodies = " ".join(body for _, body in self.chat.all_lines())
        self.assertNotIn("<think>", bodies)
        self.assertNotIn("</think>", bodies)

    def test_f8_reveals_reasoning(self):
        self.app.config.set("chat.show_reasoning", False)
        self.send("address?")
        self.press(K.F8)
        self.assertTrue(self.app.config.get("chat.show_reasoning"))
        rails = [rail for rail, _ in self.chat.all_lines()]
        self.assertIn("~", rails)

    def test_reply_is_stored_without_reasoning(self):
        self.send("address?")
        turn = self.app.conversation.turns[-1]
        self.assertEqual(turn.text.strip(), "0x27.")
        self.assertIn("operator wants", turn.reasoning)


class TestContextTrimming(UITestCase):
    reply = "Acknowledged. " * 12

    def test_trims_and_says_so(self):
        """When the window fills, history is dropped and reported."""
        self.app.engine.configure(window=768, reserve=192,
                                  params=self.app.engine.params)
        for index in range(8):
            self.send(f"message number {index} with some padding words")
            if self.app.conversation.dropped_turns:
                break
        self.assertGreater(self.app.conversation.dropped_turns, 0,
                           "context never trimmed")
        notes = [t.text for t in self.app.conversation.turns if t.role == S.NOTE]
        self.assertTrue(any("DROPPED" in n for n in notes), notes)

    def test_context_gauge_tracks_usage(self):
        self.send("hello")
        self.assertGreater(self.app.engine.usage.percent, 0)
        self.assertLessEqual(self.app.engine.usage.percent, 100)


class TestWiredSettings(UITestCase):
    """Every setting must actually do something."""

    def test_autoscroll_off_pins_the_view(self):
        self.send("first message")
        self.app.config.set("chat.autoscroll", False)
        self.chat.scroll = 0
        self.chat.follow = True
        STATE.reply = "A much longer reply. " * 12
        self.send("second message")
        self.assertEqual(self.chat.scroll, 0,
                         "view followed output with autoscroll off")

    def test_autoscroll_on_follows(self):
        self.app.config.set("chat.autoscroll", True)
        STATE.reply = "A much longer reply. " * 12
        self.send("hello")
        total = len(self.chat.all_lines())
        self.assertEqual(self.chat.scroll, max(0, total - self.chat.view_rows))

    def test_end_still_reaches_the_bottom_with_autoscroll_off(self):
        self.app.config.set("chat.autoscroll", False)
        STATE.reply = "A much longer reply. " * 12
        self.send("hello")
        self.press(K.END)
        self.press(K.DOWN)
        total = len(self.chat.all_lines())
        self.assertEqual(self.chat.scroll, max(0, total - self.chat.view_rows))

    def test_history_is_logged(self):
        self.app.config.set("chat.save_history", True)
        self.send("what responded?")
        path = self.chat._session_log_path()
        self.assertTrue(os.path.exists(path), path)
        with open(path) as handle:
            body = handle.read()
        self.assertIn("what responded?", body)
        self.assertIn("0x27", body)

    def test_history_can_be_switched_off(self):
        self.app.config.set("chat.save_history", False)
        self.send("what responded?")
        self.assertFalse(os.path.exists(self.chat._session_log_path()))

    def test_history_appends_each_exchange(self):
        self.app.config.set("chat.save_history", True)
        self.send("first")
        self.send("second")
        with open(self.chat._session_log_path()) as handle:
            body = handle.read()
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_stream_rate_caps_redraws_while_busy(self):
        self.app.config.set("display.fps", 30)
        self.app.config.set("chat.stream_rate", 5)
        self.app.apply_engine_config()
        idle = self.app.frame_budget()
        self.app.engine.state = S.STREAMING
        try:
            busy = self.app.frame_budget()
        finally:
            self.app.engine.state = S.IDLE
        self.assertGreater(busy, idle)
        self.assertAlmostEqual(busy, 1 / 5, places=3)

    def test_key_repeat_can_be_disabled(self):
        from aperture.hal.keys import KeyEvent
        self.app.config.set("input.repeat_nav", False)
        STATE.reply = "A much longer reply. " * 12
        self.send("hello")
        self.press(K.HOME)
        before = self.chat.scroll
        for _ in range(4):
            self.harness.keyboard.events.put(
                KeyEvent(key=K.DOWN, repeat=True, source="script"))
        self.harness.step(4)
        self.assertEqual(self.chat.scroll, before)

    def test_key_repeat_is_honoured_when_enabled(self):
        from aperture.hal.keys import KeyEvent
        self.app.config.set("input.repeat_nav", True)
        STATE.reply = "A much longer reply. " * 12
        self.send("hello")
        self.press(K.HOME)
        before = self.chat.scroll
        for _ in range(3):
            self.harness.keyboard.events.put(
                KeyEvent(key=K.DOWN, repeat=True, source="script"))
        self.harness.step(4)
        self.assertGreater(self.chat.scroll, before)

    def test_typing_repeat_is_never_suppressed(self):
        """Holding backspace must still delete, whatever the nav setting is."""
        from aperture.hal.keys import KeyEvent
        self.app.config.set("input.repeat_nav", False)
        self.harness.keyboard.type("abcdef")
        self.harness.step(2)
        for _ in range(3):
            self.harness.keyboard.events.put(
                KeyEvent(key=K.BACKSPACE, repeat=True, source="script"))
        self.harness.step(4)
        self.assertEqual(self.chat.field.text, "abc")


class TestSettingsLayout(UITestCase):
    """Every menu row must fit the panel, at every value it can hold.

    A settings screen that truncates is not a cosmetic problem on a display
    this size: the value is the thing being changed, and a clipped one is
    actively misleading. This walks the whole tree at each setting's default,
    minimum and maximum, in both normal and adjust mode.
    """

    def test_every_row_fits_at_every_extreme(self):
        from aperture.ui.settings import (SECTION_ORDER, SECTIONS, SETTING,
                                          SectionScreen)
        problems = []
        checked = 0
        for section in SECTION_ORDER:
            entries = SECTIONS[section]
            self.app.push(SectionScreen(self.app, section))
            screen = self.app.top
            for index, entry in enumerate(entries):
                screen.list.select(index)
                for adjusting in (False, True):
                    if adjusting and entry.kind != SETTING:
                        continue
                    screen.adjusting = adjusting
                    for extreme in ("default", "min", "max"):
                        if entry.kind == SETTING:
                            setting = self.app.config.setting(entry.key)
                            bound = {"min": setting.minimum,
                                     "max": setting.maximum}.get(extreme)
                            self.app.config.set(
                                entry.key,
                                setting.default if bound is None else bound)
                        self.app.toast.clear()
                        self.harness.step(1)
                        checked += 1
                        rows = self.harness.emulator.text_screen()
                        for number, line in enumerate(rows):
                            if len(line) != 20:
                                problems.append(
                                    f"{section}[{index}] row {number}: {line!r}")
                        if entry.kind == SETTING and not adjusting:
                            value = self.app.config.format(entry.key)
                            row = rows[1 + screen.list.index - screen.list.offset]
                            if value and value not in row:
                                problems.append(
                                    f"{section}.{entry.key}: value {value!r} "
                                    f"not visible in {row!r}")
                screen.adjusting = False
            self.app.pop()
            self.harness.step(1)
        self.assertGreater(checked, 100)
        self.assertEqual(problems, [], "\n".join(problems[:10]))

    def test_every_section_is_reachable(self):
        from aperture.ui.settings import SECTION_ORDER, SectionScreen
        self.press(K.F7)
        self.press(K.HOME)
        for expected in SECTION_ORDER:
            self.press(K.ENTER)
            self.assertIsInstance(self.app.top, SectionScreen)
            self.assertEqual(self.app.top.section, expected)
            self.press(K.ESC)
            self.press(K.DOWN)


if __name__ == "__main__":
    unittest.main()
