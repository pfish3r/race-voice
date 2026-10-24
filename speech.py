"""Announcement queue and direct Windows SAPI output, independent of live PCM."""
from collections import OrderedDict, deque
import threading
import time
import uuid


class AnnouncementQueue:
    def __init__(self, limit=8):
        self.limit = limit
        self.pending = deque()
        self.seen = OrderedDict()
        self.condition = threading.Condition(threading.RLock())
        self.generation = 0
        self.cancel_reason = "shutdown"

    def enqueue(self, text, message_id=None):
        if (not isinstance(text, str) or not text.strip() or len(text) > 1000
                or (message_id is not None and
                    (not isinstance(message_id, str) or not 1 <= len(message_id) <= 128))):
            return False, "invalid"
        identifier = message_id if message_id is not None else uuid.uuid4().hex
        with self.condition:
            if identifier in self.seen:
                return False, "duplicate"
            if len(self.pending) >= self.limit:
                return False, "full"
            self.seen[identifier] = None
            if len(self.seen) > 256:
                self.seen.popitem(last=False)
            self.pending.append((self.generation, identifier, text))
            self.condition.notify()
            return True, "queued"

    def take(self, timeout=.2):
        with self.condition:
            if not self.pending:
                self.condition.wait(timeout)
            return self.pending.popleft() if self.pending else None

    def cancel(self, reason):
        with self.condition:
            self.generation += 1
            self.cancel_reason = reason
            self.pending.clear()
            self.condition.notify_all()

    def valid(self, generation):
        with self.condition:
            return self.generation == generation


def select_output(voice, output_id, output_name):
    collection = voice.GetAudioOutputs()
    tokens = [collection.Item(i) for i in range(collection.Count)]
    selected = str(output_id).casefold()
    matches = [token for token in tokens
               if str(token.Id).casefold().rsplit("\\", 1)[-1] == selected]
    if not matches:
        matches = [token for token in tokens
                   if token.GetDescription().casefold() == output_name.casefold()]
    if len(matches) != 1:
        raise ValueError("Selected receiver speakers are not uniquely available to Windows speech. "
                         "Choose another output and reconnect.")
    voice.AudioOutput = matches[0]
    return matches[0].GetDescription()


def run_sapi(queue, stop_event, output_id, output_name, volume_fn, status_fn,
             trace_fn, dispatch=None, pump=None):
    if dispatch is None:
        import win32com.client
        dispatch = win32com.client.Dispatch
    if pump is None:
        import pythoncom
        pump = pythoncom.PumpWaitingMessages
    voice = None
    active = False
    try:
        voice = dispatch("SAPI.SpVoice")
        name = select_output(voice, output_id, output_name)
        status_fn("Windows speech ready: " + name)
        trace_fn("speech_output", detail="selected_output")
        while not stop_event.is_set():
            item = queue.take()
            if item is None:
                continue
            generation, identifier, text = item
            with queue.condition:
                if stop_event.is_set() or not queue.valid(generation):
                    continue
                voice.Volume = round(max(0, min(1, volume_fn())) * 100)
                # Async + plain text. No synchronous synthesis timeout, WAV
                # conversion, resampling or application PCM mixer is involved.
                voice.Speak(text, 1 | 16)
                active = True
            started = time.monotonic()
            trace_fn("speech_started", id=identifier, characters=len(text))
            status_fn("Speaking " + identifier[:8])
            while True:
                pump()
                if stop_event.is_set() or not queue.valid(generation):
                    voice.Speak("", 1 | 2)
                    active = False
                    reason = queue.cancel_reason if not queue.valid(generation) else "shutdown"
                    trace_fn("speech_cancelled", id=identifier,
                             seconds=round(time.monotonic()-started, 2), detail=reason)
                    status_fn("Speech cancelled: " + reason)
                    break
                voice.Volume = round(max(0, min(1, volume_fn())) * 100)
                if voice.WaitUntilDone(50):
                    active = False
                    elapsed = round(time.monotonic()-started, 2)
                    trace_fn("speech_completed", id=identifier, seconds=elapsed)
                    status_fn(f"Windows speech completed ({elapsed:.1f}s), " + identifier[:8])
                    break
    except Exception as error:
        status_fn("Windows speech failed: " + str(error))
        trace_fn("speech_error", detail=type(error).__name__)
    finally:
        if voice is not None and active:
            try:
                voice.Speak("", 1 | 2)
            except Exception:
                pass
