"""Speech regression checks using fake SAPI objects, without Windows audio hardware."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech import AnnouncementQueue, run_sapi


ENDPOINT = "{0.0.0.00000000}.{11111111-2222-3333-4444-555555555555}"


class Token:
    def __init__(self, token_id, name):
        self.Id = token_id
        self.name = name

    def GetDescription(self):
        return self.name


class Tokens:
    def __init__(self, tokens):
        self.tokens = tokens
        self.Count = len(tokens)

    def Item(self, index):
        return self.tokens[index]

    def __iter__(self):
        return iter(self.tokens)


class Voice:
    def __init__(self, stop, tokens=None, waits=1, on_wait=None, fail=False):
        self.stop = stop
        self.tokens = tokens or [Token("registry\\MMAudioOut\\" + ENDPOINT, "Room speakers")]
        self.waits = waits
        self.polls = 0
        self.calls = []
        self.on_wait = on_wait
        self.fail = fail
        self.AudioOutput = None
        self.Volume = None

    def GetAudioOutputs(self):
        return Tokens(self.tokens)

    def Speak(self, text, flags):
        self.calls.append((text, flags))
        if text and self.fail:
            self.stop.set()
            raise RuntimeError("simulated speech failure")
        if not text and flags & 2:
            self.stop.set()

    def WaitUntilDone(self, milliseconds):
        self.polls += 1
        if self.on_wait:
            self.on_wait(self)
        if self.polls < self.waits:
            return False
        self.stop.set()
        return True


class QueueTests(unittest.TestCase):
    def test_duplicate_id_rejected_even_after_cancel(self):
        queue = AnnouncementQueue()
        self.assertEqual(queue.enqueue("Announcement", "id-1"), (True, "queued"))
        self.assertEqual(queue.enqueue("Announcement", "id-1"), (False, "duplicate"))
        queue.cancel("disabled")
        self.assertEqual(queue.enqueue("Announcement", "id-1"), (False, "duplicate"))
        self.assertIsNone(queue.take(timeout=0))

    def test_full_queue_preserves_existing_message(self):
        queue = AnnouncementQueue(limit=1)
        self.assertEqual(queue.enqueue("First", "id-1"), (True, "queued"))
        self.assertEqual(queue.enqueue("Second", "id-2"), (False, "full"))
        generation, identifier, text = queue.take(timeout=0)
        self.assertEqual((identifier, text), ("id-1", "First"))
        self.assertTrue(queue.valid(generation))

    def test_cancel_invalidates_current_and_clears_pending(self):
        queue = AnnouncementQueue()
        queue.enqueue("Current", "id-1")
        current = queue.take(timeout=0)
        queue.enqueue("Pending", "id-2")
        queue.cancel("STOP received")
        self.assertFalse(queue.valid(current[0]))
        self.assertEqual(queue.cancel_reason, "STOP received")
        self.assertIsNone(queue.take(timeout=0))

    def test_invalid_inputs_rejected(self):
        queue = AnnouncementQueue()
        for text, identifier in (("", "x"), ("x" * 1001, "x"), (None, "x"), ("text", 3), ("text", ""), ("text", "x" * 129)):
            self.assertEqual(queue.enqueue(text, identifier), (False, "invalid"))


class SapiTests(unittest.TestCase):
    def run_voice(self, queue, voice, output_id=ENDPOINT, output_name="Room speakers"):
        statuses, trace = [], []
        errors = []

        def run():
            try:
                run_sapi(queue, voice.stop, output_id, output_name,
                         lambda: .75, statuses.append,
                         lambda *args, **kwargs: trace.append((args, kwargs)),
                         dispatch=lambda name: voice, pump=lambda: None)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(2)
        voice.stop.set()
        worker.join(1)
        self.assertFalse(worker.is_alive(), "Speech worker did not exit after stop")
        self.assertEqual(errors, [])
        return statuses, trace

    def test_long_speech_waits_to_completion_and_is_submitted_once(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        text = "Qualifying round one will start in ten minutes. Please prepare your cars."
        queue.enqueue(text, "long-id")
        voice = Voice(stop, waits=65)
        self.run_voice(queue, voice)
        self.assertEqual([call[0] for call in voice.calls if call[0]], [text])
        self.assertGreaterEqual(voice.polls, 65)
        self.assertIs(voice.AudioOutput, voice.tokens[0])
        self.assertEqual(voice.Volume, 75)

    def test_stop_purges_active_speech_and_drops_pending(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        queue.enqueue("Current", "one")
        queue.enqueue("Pending", "two")
        voice = Voice(stop, waits=100, on_wait=lambda _: queue.cancel("STOP received"))
        self.run_voice(queue, voice)
        self.assertEqual([text for text, _ in voice.calls if text], ["Current"])
        self.assertTrue(any(not text and flags & 2 for text, flags in voice.calls))
        self.assertIsNone(queue.take(timeout=0))

    def test_missing_selected_device_never_speaks_on_default(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        queue.enqueue("Do not play elsewhere", "one")
        voice = Voice(stop, tokens=[Token("different", "Other speakers")])
        statuses, _ = self.run_voice(queue, voice)
        self.assertFalse(any(text for text, _ in voice.calls))
        self.assertTrue(statuses)

    def test_speech_exception_is_not_replayed(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        queue.enqueue("Submit exactly once", "one")
        voice = Voice(stop, fail=True)
        statuses, _ = self.run_voice(queue, voice)
        self.assertEqual([text for text, _ in voice.calls if text], ["Submit exactly once"])
        self.assertTrue(statuses)

    def test_unique_exact_name_fallback(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        queue.enqueue("Selected name", "one")
        voice = Voice(stop, tokens=[Token("different-id", "Room speakers")])
        self.run_voice(queue, voice)
        self.assertIs(voice.AudioOutput, voice.tokens[0])

    def test_ambiguous_name_does_not_fall_back(self):
        queue, stop = AnnouncementQueue(), threading.Event()
        queue.enqueue("Do not guess", "one")
        voice = Voice(stop, tokens=[Token("one", "Room speakers"), Token("two", "Room speakers")])
        self.run_voice(queue, voice)
        self.assertFalse(any(text for text, _ in voice.calls))


if __name__ == "__main__":
    unittest.main()
