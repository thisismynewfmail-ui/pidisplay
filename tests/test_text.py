"""Text layout: wrapping, streaming, and the fitting helpers."""

import random
import unittest

from aperture.ui import text as T


class TestWrap(unittest.TestCase):
    def test_never_exceeds_width(self):
        sample = ("Set the context to 8192 tokens; the model was trained for "
                  "8192 so that is the ceiling. i2cdetect -y 1 confirms it.")
        for width in range(6, 24):
            for line in T.wrap(sample, width):
                self.assertLessEqual(len(line), width)

    def test_breaks_words_longer_than_the_column(self):
        lines = T.wrap("supercalifragilisticexpialidocious", 18)
        self.assertTrue(all(len(line) <= 18 for line in lines))
        self.assertEqual("".join(lines), "supercalifragilisticexpialidocious")

    def test_keeps_interior_blank_lines(self):
        self.assertEqual(T.wrap("a\n\nb", 18), ["a", "", "b"])

    def test_drops_trailing_blank_lines(self):
        # Three rows are visible; spending one on a model's trailing newline
        # is a third of the transcript wasted.
        self.assertEqual(T.wrap("a\n\n\n", 18), ["a"])

    def test_sanitises_typography(self):
        self.assertEqual(T.sanitise("don’t — “ok”…"),
                         'don\'t - "ok"...')

    def test_replaces_unrenderable_characters(self):
        self.assertEqual(T.sanitise("hi \U0001f600"), "hi ?")


class TestStreamWrapper(unittest.TestCase):
    SAMPLES = [
        "Checking the bus now. Address 0x27 responded on the first attempt.\n"
        "All four rows are live.",
        "Yes.  Use i2cdetect -y 1 to confirm the backpack address first.",
        "a\n\nb\n", "One.", "   leading", "x" * 40, "\n\n\n", "",
        "- pin 3 SDA\n- pin 5 SCL\n- pin 6 GND",
        "supercalifragilisticexpialidocious is a very long word indeed",
    ]

    def test_streaming_matches_batch_wrapping(self):
        """However the text is chunked, the result must be identical.

        This is the property the transcript depends on: a reply must not
        reflow differently depending on how the network split the stream.
        """
        random.seed(1234)
        for width in (8, 12, 18, 20):
            for sample in self.SAMPLES:
                for _ in range(40):
                    wrapper = T.StreamWrapper(width)
                    index = 0
                    while index < len(sample):
                        size = random.randint(1, 7)
                        wrapper.append(sample[index:index + size])
                        index += size
                    self.assertEqual(wrapper.lines(), T.wrap(sample, width),
                                     f"width={width} sample={sample!r}")

    def test_committed_lines_never_move(self):
        """Once a line is complete it must never re-wrap under the reader."""
        wrapper = T.StreamWrapper(18)
        history = []
        for chunk in ["The panel ", "reports all ", "four rows ", "are live ",
                      "and stable now."]:
            wrapper.append(chunk)
            history.append(wrapper.lines())
        for earlier, later in zip(history, history[1:]):
            # Every line except the last of the earlier snapshot is settled.
            for index in range(len(earlier) - 1):
                self.assertEqual(earlier[index], later[index])

    def test_chunk_boundary_does_not_weld_words(self):
        wrapper = T.StreamWrapper(18)
        wrapper.append("i2cdetect ")
        wrapper.append("-y 1")
        self.assertIn("i2cdetect -y 1", " ".join(wrapper.lines()))

    def test_width_change_rewraps(self):
        wrapper = T.StreamWrapper(18)
        wrapper.append("one two three four five six")
        wrapper.set_width(10)
        self.assertEqual(wrapper.lines(),
                         T.wrap("one two three four five six", 10))


class TestFitting(unittest.TestCase):
    def test_fit_is_exact(self):
        for width in range(1, 21):
            self.assertEqual(len(T.fit("some text here", width)), width)

    def test_pair_is_exact_and_favours_the_value(self):
        for width in range(4, 21):
            row = T.pair("Repeat penalty", "1.15", width)
            self.assertEqual(len(row), width)
            self.assertTrue(row.endswith("1.15"))

    def test_marquee_holds_then_scrolls(self):
        label = "A very long setting label"
        first = T.marquee(label, 12, 0.0)
        self.assertEqual(first, label[:12])
        self.assertNotEqual(T.marquee(label, 12, 4.0), first)

    def test_marquee_is_exact_width(self):
        for elapsed in (0.0, 0.5, 3.0, 7.5, 20.0):
            self.assertEqual(len(T.marquee("A very long label", 12, elapsed)), 12)

    def test_short_text_does_not_scroll(self):
        for elapsed in (0.0, 5.0, 50.0):
            self.assertEqual(T.marquee("short", 12, elapsed), T.fit("short", 12))

    def test_scroll_window_stays_in_range(self):
        for total in range(1, 40):
            for offset in range(0, max(1, total)):
                start, size = T.scroll_window(total, 3, offset)
                self.assertGreaterEqual(start, 0.0)
                self.assertLessEqual(start + size, 1.0 + 1e-9)

    def test_format_count_is_compact(self):
        for value in (0, 512, 4096, 65536, 131072, 2_000_000):
            self.assertLessEqual(len(T.format_count(value)), 5)


if __name__ == "__main__":
    unittest.main()
