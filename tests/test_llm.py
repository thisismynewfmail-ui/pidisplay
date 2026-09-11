"""Model introspection, conversation handling and reasoning separation."""

import os
import struct
import tempfile
import unittest

from aperture.llm import models as M
from aperture.llm.client import Timings, _parse_native, _parse_openai
from aperture.llm.session import (ASSISTANT, Conversation, ReasoningSplitter,
                                  Turn, USER)
from aperture.llm.server import LlamaServer


def make_gguf(path, pairs, version=3):
    body = b"GGUF" + struct.pack("<I", version) + struct.pack("<Q", 0)
    body += struct.pack("<Q", len(pairs)) + b"".join(pairs)
    with open(path, "wb") as handle:
        handle.write(body + b"\0" * 256)


def kv_str(key, value):
    return (struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 8) +
            struct.pack("<Q", len(value)) + value.encode())


def kv_u32(key, value):
    return (struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 4) +
            struct.pack("<I", value))


def kv_str_array(key, items):
    out = (struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 9) +
           struct.pack("<I", 8) + struct.pack("<Q", len(items)))
    for item in items:
        out += struct.pack("<Q", len(item)) + item.encode()
    return out


class TestGGUF(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "test-3b-q4_k_m.gguf")
        make_gguf(self.path, [
            kv_str("general.architecture", "llama"),
            kv_str("general.name", "Test Model"),
            kv_str("general.size_label", "3B"),
            kv_u32("general.file_type", 15),
            kv_u32("llama.context_length", 8192),
            kv_u32("llama.block_count", 28),
            # A tokenizer vocabulary: must be skipped, not materialised.
            kv_str_array("tokenizer.ggml.tokens", [f"t{i}" for i in range(500)]),
            kv_u32("llama.embedding_length", 3072),
        ])

    def tearDown(self):
        self.directory.cleanup()

    def test_reads_metadata(self):
        info = M.inspect_model(self.path)
        self.assertEqual(info.architecture, "llama")
        self.assertEqual(info.train_context, 8192)
        self.assertEqual(info.block_count, 28)
        self.assertEqual(info.quantisation, "Q4_K_M")
        self.assertEqual(info.size_label, "3B")
        self.assertFalse(info.error)

    def test_skips_large_arrays(self):
        info = M.inspect_model(self.path)
        self.assertNotIn("tokenizer.ggml.tokens", info.metadata)

    def test_truncated_file_is_not_fatal(self):
        path = os.path.join(self.directory.name, "broken.gguf")
        with open(path, "wb") as handle:
            handle.write(b"GGUF" + struct.pack("<I", 3) + b"\x00" * 4)
        info = M.inspect_model(path)
        self.assertTrue(info.error)

    def test_non_gguf_is_reported(self):
        path = os.path.join(self.directory.name, "notamodel.gguf")
        with open(path, "wb") as handle:
            handle.write(b"this is not a model")
        self.assertIn("not a GGUF", M.inspect_model(path).error)

    def test_scan_hides_extra_shards(self):
        base = self.directory.name
        for index in (1, 2, 3):
            make_gguf(os.path.join(base, f"big-{index:05d}-of-00003.gguf"),
                      [kv_str("general.architecture", "llama")])
        names = [m.filename for m in M.scan_models(base)]
        self.assertIn("big-00001-of-00003.gguf", names)
        self.assertNotIn("big-00002-of-00003.gguf", names)

    def test_resolve_accepts_several_spellings(self):
        for spelling in ("test-3b-q4_k_m.gguf", "test-3b-q4_k_m", self.path):
            self.assertIsNotNone(M.resolve_model(self.directory.name, spelling),
                                 spelling)

    def test_resolve_falls_back_to_first_model(self):
        self.assertIsNotNone(M.resolve_model(self.directory.name, "absent.gguf"))

    def test_short_name_keeps_the_informative_end(self):
        info = M.inspect_model(self.path)
        info.path = "/x/" + "a" * 40 + "-8B-Q5_K_M.gguf"
        self.assertTrue(info.short_name(18).endswith("Q5_K_M"))


class TestReasoningSplitter(unittest.TestCase):
    def test_separates_channels(self):
        splitter = ReasoningSplitter()
        answer, reasoning = splitter.feed("<think>hmm</think>Result.")
        self.assertEqual(answer, "Result.")
        self.assertEqual(reasoning, "hmm")

    def test_survives_tags_split_across_chunks(self):
        """A chunk boundary inside a tag must not leak markup into the answer."""
        splitter = ReasoningSplitter()
        answer = reasoning = ""
        for chunk in ["Hel", "lo <th", "ink>be brief</thi", "nk> World", "!"]:
            a, r = splitter.feed(chunk)
            answer += a
            reasoning += r
        a, r = splitter.flush()
        self.assertEqual(answer + a, "Hello  World!")
        self.assertEqual(reasoning + r, "be brief")

    def test_unclosed_reasoning_never_reaches_the_answer(self):
        """A reply cut off mid-thought must not print its reasoning as text."""
        splitter = ReasoningSplitter()
        answer, reasoning = splitter.feed("<think>still going")
        tail_answer, tail_reasoning = splitter.flush()
        self.assertEqual(answer + tail_answer, "")
        self.assertEqual(reasoning + tail_reasoning, "still going")

    def test_partial_tag_is_held_back_not_printed(self):
        """Text that might still become a tag must not flash on screen."""
        splitter = ReasoningSplitter()
        answer, _ = splitter.feed("done <thi")
        self.assertEqual(answer, "done ")
        answer, reasoning = splitter.feed("nk>secret")
        self.assertEqual(answer, "")
        self.assertEqual(reasoning, "secret")

    def test_plain_text_passes_through_untouched(self):
        splitter = ReasoningSplitter()
        answer, reasoning = splitter.feed("no tags at all")
        self.assertEqual(answer, "no tags at all")
        self.assertEqual(reasoning, "")


class TestConversation(unittest.TestCase):
    def test_system_prompt_leads(self):
        conversation = Conversation("SYSTEM")
        conversation.add(Turn(USER, "hi"))
        messages = conversation.messages()
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "SYSTEM")

    def test_display_only_turns_are_not_sent(self):
        conversation = Conversation("S")
        conversation.add(Turn(USER, "hi"))
        conversation.add(Turn("note", "CONTEXT TRIMMED"))
        self.assertEqual(len(conversation.messages()), 2)

    def test_drop_oldest_removes_a_whole_exchange(self):
        conversation = Conversation("S")
        for index in range(3):
            conversation.add(Turn(USER, f"q{index}"))
            conversation.add(Turn(ASSISTANT, f"a{index}"))
        self.assertTrue(conversation.drop_oldest_exchange())
        contents = [m["content"] for m in conversation.messages()]
        self.assertNotIn("q0", contents)
        self.assertNotIn("a0", contents)
        self.assertIn("q1", contents)
        self.assertEqual(conversation.exchanges, 2)

    def test_drop_on_empty_is_safe(self):
        self.assertFalse(Conversation("S").drop_oldest_exchange())


class TestStreamParsing(unittest.TestCase):
    def test_native_final_chunk_carries_timings(self):
        events = _parse_native({
            "content": "", "stop": True, "tokens_cached": 300,
            "timings": {"prompt_n": 12, "prompt_ms": 400,
                        "predicted_n": 40, "predicted_ms": 4000}})
        done = events[-1]
        self.assertEqual(done.kind, "done")
        self.assertEqual(done.timings.cached_tokens, 300)
        self.assertAlmostEqual(done.timings.tokens_per_second, 10.0)

    def test_openai_delta(self):
        events = _parse_openai({"choices": [{"delta": {"content": "hi"}}]})
        self.assertEqual((events[0].kind, events[0].text), ("text", "hi"))

    def test_stop_reasons(self):
        self.assertEqual(_parse_native({"stop": True, "stopped_limit": True})[-1]
                         .stop_reason, "length")
        self.assertEqual(_parse_native({"stop": True, "stopped_word": True})[-1]
                         .stop_reason, "stop-word")

    def test_cache_hit_ratio(self):
        self.assertAlmostEqual(
            Timings(prompt_tokens=10, cached_tokens=90).cache_hit_ratio, 0.9)


class TestLaunchPlan(unittest.TestCase):
    """The command line must adapt to whichever llama.cpp build is installed."""

    def plan(self, flags, **kwargs):
        server = LlamaServer("/bin/true")
        server._flags = set(flags)
        defaults = dict(model_path="/m.gguf", host="127.0.0.1", port=8080,
                        n_ctx=4096, threads=4, gpu_layers=0, batch=256,
                        cache_reuse=256, mlock=False, flash_attn=False)
        defaults.update(kwargs)
        return server.build_plan(**defaults).args

    def test_omits_flags_the_binary_does_not_advertise(self):
        args = self.plan(set())
        self.assertNotIn("--cache-reuse", args)
        self.assertNotIn("--slots", args)
        self.assertNotIn("--parallel", args)

    def test_includes_supported_flags(self):
        args = self.plan({"--cache-reuse", "--slots", "--parallel", "-ngl"})
        self.assertIn("--cache-reuse", args)
        self.assertIn("--slots", args)

    def test_pins_a_single_slot(self):
        """Extra slots would silently divide the operator's context window."""
        args = self.plan({"--parallel"})
        self.assertEqual(args[args.index("--parallel") + 1], "1")

    def test_context_is_always_passed(self):
        args = self.plan(set(), n_ctx=8192)
        self.assertEqual(args[args.index("-c") + 1], "8192")

    def test_mlock_only_when_asked_and_supported(self):
        self.assertNotIn("--mlock", self.plan({"--mlock"}, mlock=False))
        self.assertIn("--mlock", self.plan({"--mlock"}, mlock=True))
        self.assertNotIn("--mlock", self.plan(set(), mlock=True))


class TestTranscriptExport(unittest.TestCase):
    def test_export_never_overwrites(self):
        """Two exports in the same second must produce two files."""
        from aperture.llm.client import LlamaClient
        from aperture.llm.session import ChatEngine

        conversation = Conversation("S")
        conversation.add(Turn(USER, "hello"))
        conversation.add(Turn(ASSISTANT, "hi"))
        engine = ChatEngine(LlamaClient("http://127.0.0.1:1"), conversation)
        with tempfile.TemporaryDirectory() as directory:
            first = engine.export(directory)
            second = engine.export(directory)
            self.assertNotEqual(first, second)
            self.assertTrue(os.path.exists(first))
            self.assertTrue(os.path.exists(second))
            with open(first) as handle:
                self.assertIn("hello", handle.read())


if __name__ == "__main__":
    unittest.main()
