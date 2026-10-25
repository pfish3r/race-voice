"""Race Voice v0.5.1. Windows 11 desktop prototype."""
import asyncio
import base64
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import uuid
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import soundcard as sc
from playback import write_audio
from speech import AnnouncementQueue, run_sapi
from modern_widgets import RoundedButton, RoundedPanel
import pythoncom
import win32crypt
from websockets.asyncio.client import connect

RATE, FRAMES = 48000, 960
CONFIG = Path(os.getenv("APPDATA", str(Path.home()))) / "RaceVoice" / "settings.json"
LOG_LOCK = threading.Lock()


def put_latest(q, value):
    try:
        q.put_nowait(value)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(value)
        except queue.Full:
            pass


class Engine:
    def __init__(self):
        self.events = queue.Queue()
        self.outgoing = queue.Queue(8)
        self.buffers = [queue.Queue(5), queue.Queue(5)]
        self.controls = queue.Queue(16)
        self.diagnostics = queue.Queue(100)
        self.announcements = AnnouncementQueue()
        self.role = "disconnected"
        self.last_command = None
        self.stop = threading.Event()
        self.connected = False
        self.ready = False
        self.enabled = False
        self.system_on = False
        self.mic_on = False
        self.volumes = [0.8, 0.8, 0.8]
        self.playback_failed = False
        self.peak = 0
        self.capture_ready = [False, False]
        self.speech_diagnostic = "No announcement received"

    def report(self, text):
        self.events.put(text)

    def trace(self, event, **fields):
        data = {"type": "diagnostic", "event": event, "version": "0.5.1", **fields}
        put_latest(self.diagnostics, data)
        logger = logging.getLogger("racevoice.client")
        try:
            with LOG_LOCK:
                if not logger.handlers:
                    CONFIG.parent.mkdir(parents=True, exist_ok=True)
                    handler = RotatingFileHandler(CONFIG.parent / "client.log",
                                                  maxBytes=1048576, backupCount=2, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                    logger.addHandler(handler)
                    logger.setLevel(logging.INFO)
                    logger.propagate = False
            logger.info("%s %s", self.role, json.dumps(data))
        except OSError:
            pass

    def clear_live(self):
        # A delivered text announcement belongs to the local speech queue.
        # Reconnecting must not cancel it or cause it to be spoken again.
        for q in [self.outgoing, self.controls, *self.buffers]:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def clear(self, reason="audio_disabled"):
        self.clear_live()
        self.announcements.cancel(reason)

    def queue_announcement(self, text, message_id=None):
        identifier = message_id if message_id is not None else uuid.uuid4().hex
        accepted, reason = self.announcements.enqueue(text, identifier)
        fields = {"id": identifier} if isinstance(identifier, str) else {}
        if accepted:
            self.trace("speech_queued", characters=len(text), **fields)
            self.speech_diagnostic = "Announcement queued: " + identifier[:8]
        else:
            self.trace("speech_duplicate" if reason == "duplicate" else "speech_rejected",
                       detail=reason, **fields)
            self.speech_diagnostic = "Announcement ignored: " + reason
        return accepted

    def send_command(self, command):
        if not self.connected or not self.ready:
            return False
        signature = (command.get("type"), command.get("text"))
        now = time.monotonic()
        if self.last_command and self.last_command[0] == signature and now-self.last_command[1] < .4:
            self.trace("speech_duplicate", detail="duplicate")
            return True
        payload = {**command, "id": uuid.uuid4().hex}
        try:
            self.controls.put_nowait((now, payload))
            self.last_command = (signature, now)
            return True
        except queue.Full:
            return False

    def launch(self, url, key, role, system_id, mic_id, output_id):
        self.stop.clear()
        self.clear()
        self.role = role
        self.threads = []
        def spawn(fn, *args):
            t = threading.Thread(target=fn, args=args, daemon=True)
            self.threads.append(t)
            t.start()
        if role == "sender":
            if system_id:
                spawn(self.capture, system_id, 0)
            if mic_id:
                spawn(self.capture, mic_id, 1)
        else:
            spawn(self.play, output_id)
            spawn(self.speak_worker, output_id)
        spawn(lambda: asyncio.run(self.network(url, key, role)))

    def close(self):
        self.stop.set()
        self.enabled = False
        self.system_on = False
        self.mic_on = False
        self.connected = self.ready = False
        self.clear("manual_disconnect")

    def capture(self, device_id, stream):
        pythoncom.CoInitialize()
        try:
            mic = sc.get_microphone(device_id, include_loopback=True)
            # Record all channels: avoids the WASAPI single-channel capture issue.
            with mic.recorder(samplerate=RATE, blocksize=FRAMES) as recorder:
                self.capture_ready[stream] = True
                while not self.stop.is_set():
                    data = recorder.record(numframes=FRAMES)
                    active = self.system_on if stream == 0 else self.mic_on
                    if active and self.connected and self.ready:
                        mono = np.mean(data, axis=1)
                        self.peak = min(1.0, float(np.max(np.abs(mono))))
                        pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
                        put_latest(self.outgoing, (time.monotonic(), bytes([stream]) + pcm))
        except Exception as e:
            self.capture_ready[stream] = False
            self.report(f"Capture failed ({'system' if stream == 0 else 'mic'}): {e}")
        finally:
            pythoncom.CoUninitialize()

    def play(self, output_id):
        pythoncom.CoInitialize()
        try:
            speaker = sc.get_speaker(output_id)
            with speaker.player(samplerate=RATE, channels=2, blocksize=FRAMES) as player:
                while not self.stop.is_set():
                    mixed = np.zeros(FRAMES, dtype=np.float32)
                    if self.enabled:
                        for i, q in enumerate(self.buffers):
                            try:
                                stamp, samples = q.get_nowait()
                                if time.monotonic() - stamp < .25:
                                    mixed += samples * self.volumes[i]
                            except queue.Empty:
                                pass
                    else:
                        for q in self.buffers:
                            while not q.empty():
                                try:
                                    q.get_nowait()
                                except queue.Empty:
                                    break
                    self.peak = min(1.0, float(np.max(np.abs(mixed))))
                    stereo = np.repeat(np.clip(mixed, -1, 1)[:, None], 2, axis=1)
                    write_audio(player, stereo, self.stop)
        except Exception as e:
            self.enabled = False
            self.playback_failed = True
            self.announcements.cancel("audio_disabled")
            self.report(f"Playback failed: {e}. Disconnect, choose a device and reconnect.")
        finally:
            pythoncom.CoUninitialize()

    def speak_worker(self, output_id):
        pythoncom.CoInitialize()
        try:
            output_name = sc.get_speaker(output_id).name
            def status(message):
                self.speech_diagnostic = message
            run_sapi(self.announcements, self.stop, output_id, output_name,
                     lambda: self.volumes[2], status, self.trace)
        except Exception as error:
            self.speech_diagnostic = "Windows speech failed: " + str(error)
            self.trace("speech_error", detail=type(error).__name__)
        finally:
            pythoncom.CoUninitialize()

    async def network(self, url, key, role):
        while not self.stop.is_set():
            try:
                self.report("Connecting...")
                async with connect(url, compression=None, max_size=4096, max_queue=4,
                                   open_timeout=6, close_timeout=1, ping_interval=10, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"role": role, "key": key}))
                    first = json.loads(await asyncio.wait_for(ws.recv(), 6))
                    if first.get("type") != "status":
                        raise ValueError("Unexpected relay response")
                    self.connected = True
                    self.clear_live()
                    self.trace("connected")
                    self.report("Connected")
                    async def transmit():
                        previous_enabled = None
                        while not self.stop.is_set():
                            if role == "receiver" and self.enabled != previous_enabled:
                                previous_enabled = self.enabled
                                await ws.send(json.dumps({"type": "enabled", "value": previous_enabled}))
                            try:
                                stamp, command = self.controls.get_nowait()
                            except queue.Empty:
                                command = None
                            if command is not None:
                                if time.monotonic()-stamp < 5:
                                    await asyncio.wait_for(ws.send(json.dumps(command)), 1)
                                    fields = {"id": command["id"]}
                                    if command["type"] == "say":
                                        fields["characters"] = len(command["text"])
                                    self.trace("command_sent", **fields)
                                else:
                                    self.trace("speech_rejected", id=command["id"], detail="expired")
                            try:
                                diagnostic = self.diagnostics.get_nowait()
                            except queue.Empty:
                                diagnostic = None
                            if diagnostic is not None:
                                await asyncio.wait_for(ws.send(json.dumps(diagnostic)), 1)
                            try:
                                stamp, payload = self.outgoing.get_nowait()
                                if time.monotonic() - stamp < .25:
                                    await asyncio.wait_for(ws.send(payload), .5)
                            except queue.Empty:
                                await asyncio.sleep(.005)
                        await ws.close()
                    async def receive():
                        self.ready = first.get("enabled", False)
                        if role == "sender":
                            self.report(("Receiver audio ENABLED" if first.get("receiver_enabled", first.get("enabled")) else "Admin portal listening") if self.ready else
                                        "Receiver connected, audio OFF" if first.get("receiver") else "Waiting for receiver")
                        async for msg in ws:
                            if isinstance(msg, bytes):
                                if role == "receiver" and self.enabled and len(msg) == 1921 and msg[0] < 2:
                                    samples = np.frombuffer(msg[1:], dtype="<i2").astype(np.float32) / 32768
                                    put_latest(self.buffers[msg[0]], (time.monotonic(), samples))
                            else:
                                data = json.loads(msg)
                                if data.get("type") == "status":
                                    self.ready = data.get("enabled", False)
                                    if role == "sender":
                                        self.report(("Receiver audio ENABLED" if data.get("receiver_enabled", data.get("enabled")) else "Admin portal listening") if self.ready else
                                                    "Receiver connected, audio OFF" if data.get("receiver") else "Waiting for receiver")
                                elif role == "receiver" and self.enabled and data.get("type") in ("say", "stop"):
                                    if data["type"] == "stop":
                                        self.announcements.cancel("stop_command")
                                    else:
                                        self.queue_announcement(data.get("text"), data.get("id"))
                    tasks = [asyncio.create_task(transmit()), asyncio.create_task(receive())]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            raise result
            except Exception as e:
                self.trace("disconnected", detail=type(e).__name__)
                self.report(f"Disconnected: {e}. Retrying...")
            finally:
                self.connected = self.ready = False
                self.clear_live()
            for _ in range(20):
                if self.stop.is_set():
                    break
                await asyncio.sleep(.1)


class ScrollArea(ttk.Frame):
    """A tab-sized viewport with a scrollbar and wheel handling for this tab."""
    def __init__(self, parent, padding=12):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f4f5fa")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = ttk.Frame(self.canvas, padding=padding, style="Page.TFrame")
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda event: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(
            self.window, width=event.width))
        self.bind_all("<MouseWheel>", self.wheel, add="+")

    def wheel(self, event):
        if not self.winfo_ismapped() or isinstance(event.widget, (tk.Text, ttk.Combobox)):
            return
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        bounds = self.canvas.bbox("all")
        if widget is self and bounds and bounds[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.running = self.held = False
        self.space_held = False
        self._fitting = False
        self._fit_pending = None
        self._closed = False
        try:
            self.settings = json.loads(CONFIG.read_text(encoding="utf-8"))
            if not isinstance(self.settings, dict):
                self.settings = {}
        except (OSError, ValueError):
            self.settings = {}
        root.title("Race Voice | v0.5.1")
        root.geometry(f"{min(1080, root.winfo_screenwidth()-80)}x{min(860, root.winfo_screenheight()-120)}")
        root.minsize(640, 480)
        root.option_add("*Font", ("Segoe UI", 11))
        try:
            self.zoom_percent = max(50, min(150, int(self.settings.get("zoom", 100))))
        except (ValueError, TypeError):
            self.zoom_percent = 100
        self.auto_fit = bool(self.settings.get("auto_fit", True))
        self.zoom_text = tk.StringVar(value=f"{self.zoom_percent}%")
        root.configure(background="#f4f5fa")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background="#ffffff", foreground="#24283e", font=("Segoe UI", 11))
        style.configure("TFrame", background="#ffffff")
        style.configure("Page.TFrame", background="#f4f5fa")
        style.configure("TLabel", background="#ffffff", foreground="#24283e")
        style.configure("TButton", padding=(12, 9), borderwidth=0,
                        background="#ede8fa", foreground="#6443ba")
        style.map("TButton", background=[("active", "#e1d7f8"), ("pressed", "#d5c6f4")])
        style.configure("TEntry", padding=7, borderwidth=1, relief="flat",
                        fieldbackground="#f8f9fc", bordercolor="#e2e5ef",
                        lightcolor="#e2e5ef", darkcolor="#e2e5ef", foreground="#24283e")
        style.map("TEntry", bordercolor=[("focus", "#9274dc")])
        style.configure("TCombobox", padding=7, fieldbackground="#f8f9fc", bordercolor="#e2e5ef",
                        arrowcolor="#7954c6", arrowsize=16)
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("CardTitle.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Status.TLabel", foreground="#646d84", font=("Segoe UI", 10))
        style.configure("Hint.TLabel", background="#f4f5fa", foreground="#777f96", font=("Segoe UI", 10))
        style.configure("PanelHint.TLabel", background="#ffffff", foreground="#777f96", font=("Segoe UI", 10))
        style.configure("HeaderHint.TLabel", background="#ffffff", foreground="#8b91a5", font=("Segoe UI", 10))
        style.configure("Step.TButton", font=("Segoe UI", 18, "bold"),
                        foreground="#67519f", background="#ffffff", padding=(8, 5))
        style.configure("Vertical.TScrollbar", background="#e0dced", troughcolor="#f4f5fa",
                        borderwidth=0, arrowsize=12)
        style.configure("Horizontal.TProgressbar", background="#9270df", troughcolor="#e9e5f4",
                        borderwidth=0, thickness=5)
        style.configure("Page.TNotebook", background="#f4f5fa", borderwidth=0, tabmargins=0)
        style.layout("Page.TNotebook.Tab", [])

        role = self.settings.get("role", "sender")
        if role not in ("sender", "receiver"):
            role = "sender"
        self.role = tk.StringVar(value=role)
        self.key_role = role
        self.saved_keys = {}
        key_error = False
        for saved_role, encrypted in self.settings.get("protected_keys", {}).items():
            if saved_role not in ("sender", "receiver"):
                continue
            try:
                self.saved_keys[saved_role] = win32crypt.CryptUnprotectData(
                    base64.b64decode(encrypted, validate=True), None, None, None, 1)[1].decode("utf-8")
            except Exception:
                key_error = True
        self.url = tk.StringVar(value=self.settings.get("url", "wss://audio.your-domain.example:8843"))
        self.key = tk.StringVar(value=self.saved_keys.get(role, ""))
        self.auto_connect = tk.BooleanVar(value=bool(self.settings.get("auto_connect", False)))
        self.system = tk.BooleanVar()
        self.always = tk.BooleanVar()
        self.enable = tk.BooleanVar(value=role == "receiver")
        self.minutes = tk.StringVar(value=str(self.settings.get("minutes", "10")))
        self.round = tk.StringVar(value=str(self.settings.get("round", "1")))
        self.oneoff = tk.StringVar()
        self.status = tk.StringVar(value=("A saved key could not be unlocked. Enter it in Connection."
                                         if key_error else "Disconnected. Click Connect to begin."))
        self.mic_status = tk.StringVar(value="MIC OFF")
        self.speech_diagnostic = tk.StringVar(value="No announcement received")
        self.locked = []
        self.send_widgets = []
        self.preview_vars = []
        self.preview_labels = []
        defaults = [
            ("Practice", "Practice will start in {minutes} minutes."),
            ("Qualifying", "Qualifying round {round} will start in {minutes} minutes. Please prepare your cars."),
            ("Finals", "The finals will start in {minutes} minutes."),
            ("Marshals", "Could all marshals please take their positions."),
            ("Custom", "Please make your way to race control.")]
        saved_slots = self.settings.get("slots", [])
        self.slots = []
        for i, default in enumerate(defaults):
            slot = saved_slots[i] if i < len(saved_slots) else default
            if not isinstance(slot, (list, tuple)) or len(slot) != 2:
                slot = default
            self.slots.append((tk.StringVar(value=slot[0]), tk.StringVar(value=slot[1])))

        self.shell = ttk.Frame(root, style="Page.TFrame")
        self.shell.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(self.shell, background="#ffffff", width=180)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar_brand = tk.Label(self.sidebar, text="RV", font=("Segoe UI", 23, "bold"),
                                      background="#ffffff", foreground="#7954c6")
        self.sidebar_brand.pack(anchor="w", padx=22, pady=(24, 2))
        self.sidebar_name = tk.Label(self.sidebar, text="RACE VOICE", font=("Segoe UI", 13, "bold"),
                                     background="#ffffff", foreground="#272c43")
        self.sidebar_name.pack(anchor="w", padx=22, pady=(0, 3))
        self.sidebar_caption = tk.Label(self.sidebar, text="Club audio control", font=("Segoe UI", 10),
                                        background="#ffffff", foreground="#949bad")
        self.sidebar_caption.pack(anchor="w", padx=22, pady=(0, 28))
        self.nav_buttons = []
        for index, text in enumerate(("Announcements", "Connection", "Audio setup")):
            button = RoundedButton(self.sidebar, text=text, font=("Segoe UI", 11, "bold"),
                                   fill="#ffffff", foreground="#7c8397", hover_fill="#f2eefb",
                                   padding=(12, 12), background="#ffffff",
                                   command=lambda i=index: self.select_page(i))
            button.pack(fill="x", padx=12, pady=5)
            self.nav_buttons.append(button)
        self.sidebar_version = tk.Label(self.sidebar, text="Race Voice\nv0.5.1", font=("Segoe UI", 9),
                                        justify="left", background="#ffffff", foreground="#9ca2b3")
        self.sidebar_version.pack(side="bottom", anchor="w", padx=22, pady=20)
        self.workspace = ttk.Frame(self.shell, style="Page.TFrame")
        self.workspace.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(self.workspace, padding=(14, 8))
        header.pack(fill="x", padx=16, pady=(8, 0))
        header.columnconfigure(0, weight=1)
        self.page_title = ttk.Label(header, text="Announcements", style="Title.TLabel")
        self.page_title.grid(row=0, column=0, sticky="w")
        self.connect_button = RoundedButton(header, text="Connect", font=("Segoe UI", 11, "bold"),
                                            padding=(20, 11), background="#ffffff", command=self.toggle)
        self.connect_button.grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.mode_label = ttk.Label(header, style="HeaderHint.TLabel")
        self.mode_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        zoom_controls = ttk.Frame(header)
        zoom_controls.grid(row=1, column=1, sticky="e", pady=(3, 0))
        style.configure("Zoom.TButton", padding=(4, 2), font=("Segoe UI", 9))
        for text, command, width in (("-", lambda: self.change_zoom(-10), 2),
                                     (None, lambda: self.set_zoom(100), 5),
                                     ("+", lambda: self.change_zoom(10), 2),
                                     ("Fit", self.fit_announcements, 3)):
            options = {"textvariable":self.zoom_text} if text is None else {"text":text}
            ttk.Button(zoom_controls, command=command, width=width,
                       style="Zoom.TButton", **options).pack(side="left", padx=1)
        root.bind("<Control-KeyPress>", self.zoom_key, add="+")
        status_label = ttk.Label(header, textvariable=self.status, style="Status.TLabel", wraplength=700)
        status_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        header.bind("<Configure>", lambda event: status_label.configure(wraplength=max(220, event.width-44)))

        self.tabs = ttk.Notebook(self.workspace, style="Page.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(6, 8))
        operation_area = ScrollArea(self.tabs, padding=0)
        operation = operation_area.body
        connection_area = ScrollArea(self.tabs, padding=0)
        audio_area = ScrollArea(self.tabs, padding=0)
        self.tabs.add(operation_area, text="Announcements")
        self.tabs.add(connection_area, text="Connection")
        self.tabs.add(audio_area, text="Audio setup")
        self.tabs.bind("<<NotebookTabChanged>>", self.navigation_changed)
        root.bind("<Configure>", self.window_resized, add="+")
        root.bind("<Destroy>", self.window_destroyed, add="+")

        summary = ttk.Frame(operation, style="Page.TFrame")
        summary.pack(fill="x", pady=(0, 6))
        self.summary_frame = summary
        summary.columnconfigure((0, 1, 2), weight=1)
        self.summary_vars = []
        for col, (label, color) in enumerate((("RELAY", "#e9f7fa"), ("AUDIO", "#eee7fb"), ("MICROPHONE", "#fff2df"))):
            panel = RoundedPanel(summary, fill=color, padding=(10, 4))
            panel.grid(row=0, column=col, sticky="ew", padx=(0, 8 if col < 2 else 0))
            ttk.Label(panel.body, text=label, background=color, foreground="#9293a8",
                      font=("Segoe UI", 9, "bold")).pack(side="left")
            variable = tk.StringVar(value="Offline" if col == 0 else "Waiting" if col == 1 else "Off")
            self.summary_vars.append(variable)
            ttk.Label(panel.body, textvariable=variable, background=color, foreground="#38304f",
                      font=("Segoe UI", 11, "bold")).pack(side="left", padx=(8, 0))

        toolbar = ttk.Frame(operation, style="Page.TFrame")
        self.operation_toolbar = toolbar
        self.counter_boxes = []
        self.counter_panels = []
        toolbar.pack(fill="x", pady=(0, 8))
        toolbar.columnconfigure((0, 1), weight=1)
        for col, (label, variable) in enumerate((("Minutes", self.minutes), ("Round", self.round))):
            color = "#e9f7fa" if col == 0 else "#eee7fb"
            panel = RoundedPanel(toolbar, fill=color, padding=(10, 5))
            panel.grid(row=0, column=col, sticky="ew", padx=(0, 8))
            self.counter_panels.append(panel)
            box = panel.body
            self.counter_boxes.append(box)
            ttk.Label(box, text=label, background=color, foreground="#746b8b",
                      font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0,6))
            step = ttk.Frame(box, style=box.cget("style"))
            step.pack(side="left")
            ttk.Button(step, text="-", style="Step.TButton", width=2,
                       command=lambda var=variable: self.step_counter(var, -1)).pack(side="left")
            ttk.Entry(step, textvariable=variable, width=3, justify="center",
                      font=("Segoe UI", 17, "bold")).pack(side="left", padx=5, ipady=3)
            ttk.Button(step, text="+", style="Step.TButton", width=2,
                       command=lambda var=variable: self.step_counter(var, 1)).pack(side="left")
        self.stop_button = RoundedButton(toolbar, text="STOP", font=("Segoe UI", 12, "bold"),
                                         fill="#fff0d8", foreground="#8b632d", hover_fill="#ffe3b6",
                                         padding=(16, 16), command=lambda: self.send({"type": "stop"}))
        self.stop_button.grid(row=0, column=2, sticky="se", padx=(8, 0))
        toolbar.bind("<Configure>", self.reflow_toolbar)

        talk_row = ttk.Frame(operation, style="Page.TFrame")
        talk_row.pack(fill="x", pady=(0, 6))
        talk_row.columnconfigure(0, weight=1)
        self.ptt = RoundedButton(talk_row, text="HOLD TO TALK  |  SPACE / F8", font=("Segoe UI", 16, "bold"),
                                 fill="#e7def9", foreground="#6b46b3", hover_fill="#dacef4",
                                 pressed_fill="#ffe0aa", padding=(14, 12))
        self.ptt.grid(row=0, column=0, sticky="ew")
        self.ptt.bind("<ButtonPress-1>", self.press_talk, add="+")
        self.send_widgets.append(self.ptt)
        root.bind_all("<ButtonRelease-1>", lambda event: setattr(self, "held", False), add="+")
        root.bind("<FocusOut>", self.check_focus)

        self.meter = ttk.Progressbar(operation, maximum=100)
        self.meter.pack(fill="x", pady=(0, 6))
        guide = ttk.Frame(operation, style="Page.TFrame")
        guide.pack(fill="x", pady=(0, 6))
        self.guide_frame = guide
        ttk.Label(guide, text="Ctrl +/-: zoom   |   Fit: show all",
                  style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        guide.columnconfigure(0, weight=1)
        ttk.Button(guide, text="Save announcements", command=self.save).grid(row=0, column=1, sticky="e", padx=(8, 0))

        # The entire operations page scrolls, including the one-off message.
        quick_panel = RoundedPanel(operation, padding=(10, 6))
        quick_panel.pack(side="bottom", fill="x", pady=(8, 0))
        quick = quick_panel.body

        quick_row = ttk.Frame(quick)
        quick_row.pack(fill="x")
        ttk.Label(quick_row, text="One-off", font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 8))
        ttk.Entry(quick_row, textvariable=self.oneoff, font=("Segoe UI", 13), width=1).pack(
            side="left", fill="x", expand=True)
        quick_send = RoundedButton(quick_row, text="Send", font=("Segoe UI", 15, "bold"),
                                   padding=(14, 11), background="#ffffff",
                                   command=lambda: self.say(self.oneoff.get()))
        quick_send.pack(side="right", padx=(8, 0))
        self.send_widgets.append(quick_send)
        self.announcement_area = operation_area
        self.announcement_body = ttk.Frame(operation, style="Page.TFrame")
        self.announcement_body.pack(fill="x")
        self.announcement_editors = []
        self.title_editors = []
        self.announcement_panels = []
        for title, message in self.slots:
            panel = RoundedPanel(self.announcement_body, padding=(10, 5), stroke="#eaecf3")
            panel.pack(fill="x", pady=(0, 5))
            self.announcement_panels.append(panel)
            card = panel.body
            card.columnconfigure(1, weight=1)
            title_editor = ttk.Entry(card, textvariable=title, font=("Segoe UI", 11, "bold"), width=10)
            title_editor.grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.title_editors.append(title_editor)
            editor = ttk.Entry(card, textvariable=message, font=("Segoe UI", 13), width=1)
            editor.grid(row=0, column=1, sticky="ew")
            self.announcement_editors.append(editor)
            preview = tk.StringVar()
            self.preview_vars.append(preview)
            label = ttk.Label(card, textvariable=preview, font=("Segoe UI", 9), foreground="#8c91a4", width=1, anchor="w")
            label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            self.preview_labels.append(label)
            button = RoundedButton(card, text="Send", font=("Segoe UI", 14, "bold"), width=5,
                                   padding=(12, 11), background="#ffffff",
                                   command=lambda var=message: self.say(var.get()))
            button.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=(10, 0))
            self.send_widgets.append(button)

        connection_panel = RoundedPanel(connection_area.body, padding=18)
        connection_panel.pack(fill="x")
        connection = connection_panel.body
        ttk.Label(connection, text="Connection", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(connection, text="Choose sender in the main hall or receiver in the other room. Change these while disconnected.",
                  style="PanelHint.TLabel", wraplength=480).pack(anchor="w", pady=(4, 10))
        fields = ttk.Frame(connection)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for row, (label, var) in enumerate((("Mode", self.role), ("Relay URL", self.url), ("Access key", self.key))):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
            widget = (ttk.Combobox(fields, textvariable=var, values=["sender", "receiver"], state="readonly")
                      if row == 0 else ttk.Entry(fields, textvariable=var, show="*" if row == 2 else ""))
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self.locked.append(widget)
        ttk.Checkbutton(connection, text="Connect automatically when Race Voice opens",
                        variable=self.auto_connect, command=self.save).pack(anchor="w", pady=(12, 3))
        ttk.Label(connection, text="Uses the saved mode, address, protected key and devices. Receiver audio defaults on.",
                  style="PanelHint.TLabel", wraplength=480).pack(anchor="w")
        ttk.Button(connection, text="Save settings & templates", command=self.save).pack(anchor="w", pady=12)
        ttk.Separator(connection).pack(fill="x", pady=8)
        ttk.Label(connection, text="Announcement templates", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(connection, text="Use {minutes} and {round}. The main screen shows the words that will be sent.",
                  style="PanelHint.TLabel", wraplength=480).pack(anchor="w", pady=(4, 10))
        self.template_editors = []
        for title, message in self.slots:
            box = ttk.Frame(connection)
            box.pack(fill="x", pady=(0, 12))
            ttk.Entry(box, textvariable=title, font=("Segoe UI", 12, "bold"), width=20).pack(anchor="w", pady=(0, 4))
            editor = ttk.Entry(box, textvariable=message, font=("Segoe UI", 12))
            editor.pack(fill="x")
            self.template_editors.append(editor)
        ttk.Button(connection, text="Save settings & templates", command=self.save).pack(anchor="w")

        audio_panel = RoundedPanel(audio_area.body, padding=18)
        audio_panel.pack(fill="x")
        audio = audio_panel.body
        ttk.Label(audio, text="Audio devices", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(audio, text="Select devices before connecting. System capture sends every sound from the chosen output.",
                  style="PanelHint.TLabel", wraplength=480).pack(anchor="w", pady=(4, 10))
        self.device_vars, self.device_lists = [], []
        try:
            self.speakers = sc.all_speakers()
            self.mics = [mic for mic in sc.all_microphones() if not mic.isloopback]
        except Exception as error:
            self.speakers, self.mics = [], []
            self.status.set(f"Device enumeration failed: {error}")
        self.options = [self.speakers, self.mics, self.speakers]
        for i, label in enumerate(("Capture system output", "Microphone", "Receiver speakers")):
            ttk.Label(audio, text=label).pack(anchor="w", pady=(6, 2))
            variable = tk.StringVar()
            widget = ttk.Combobox(audio, textvariable=variable, state="readonly")
            widget.pack(fill="x", pady=(0, 5))
            options = self.options[i]
            widget["values"] = [f"{j+1}. {device.name}" for j, device in enumerate(options)]
            if options:
                selected = next((j for j, device in enumerate(options)
                                 if device.id == self.settings.get(f"device{i}")), 0)
                widget.current(selected)
            self.device_vars.append(variable)
            self.device_lists.append(widget)
            self.locked.append(widget)
        toggles = ttk.LabelFrame(audio, text="Live audio options", padding=12)
        toggles.pack(fill="x", pady=12)
        self.sender_audio_controls = []
        for text, variable in (("Send system audio", self.system), ("Always-on microphone", self.always)):
            button = ttk.Checkbutton(toggles, text=text, variable=variable)
            button.pack(anchor="w", pady=4)
            self.sender_audio_controls.append(button)
        self.receiver_toggle = ttk.Checkbutton(toggles, text="Enable receiver audio", variable=self.enable)
        self.receiver_toggle.pack(anchor="w", pady=4)
        ttk.Label(audio, text="Receiver volumes", style="CardTitle.TLabel").pack(anchor="w", pady=(8, 0))
        self.volume_vars = []
        for i, label in enumerate(("Timing / system", "Microphone", "Announcements")):
            ttk.Label(audio, text=label).pack(anchor="w", pady=(8, 0))
            variable = tk.DoubleVar(value=self.settings.get(f"volume{i}", .8))
            self.volume_vars.append(variable)
            ttk.Scale(audio, from_=0, to=1, variable=variable).pack(fill="x")
        ttk.Label(audio, text="These sliders control playback on this PC when it is the receiver.",
                  style="PanelHint.TLabel", wraplength=480).pack(anchor="w", pady=(5, 12))
        diagnostic = ttk.LabelFrame(audio, text="Receiver diagnostics", padding=12)
        diagnostic.pack(fill="x", pady=8)
        ttk.Label(diagnostic, textvariable=self.speech_diagnostic, wraplength=520).pack(anchor="w")
        ttk.Button(diagnostic, text="Test receiver locally", command=self.test_receiver).pack(anchor="w", pady=(10, 5))
        ttk.Button(diagnostic, text="Open local logs", command=self.open_speech_log).pack(anchor="w")
        ttk.Button(audio, text="Save settings", command=self.save).pack(anchor="w", pady=10)

        self.role.trace_add("write", self.change_role)
        for variable in [self.minutes, self.round, *(message for _, message in self.slots)]:
            variable.trace_add("write", self.refresh_previews)
        self.refresh_previews()
        self.sync_mode()
        self.prepare_zoom()
        self.install_space_shortcut()
        self.apply_zoom()
        self.navigation_changed()
        self.queue_fit()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tick()
        root.after(300, self.auto_connect_saved)

    def all_widgets(self):
        pending = [self.root]
        while pending:
            widget = pending.pop()
            yield widget
            pending.extend(widget.winfo_children())

    def prepare_zoom(self):
        self._zoom_widgets = []
        self._zoom_geometry = []
        self._zoom_surfaces = []
        for widget in self.all_widgets():
            if isinstance(widget, (RoundedButton, RoundedPanel)):
                self._zoom_surfaces.append(widget)
            elif "font" in widget.keys() and widget.cget("font"):
                spec = widget.cget("font")
                actual = dict(zip(*(iter(self.root.tk.splitlist(self.root.tk.call("font", "actual", spec))),)*2))
                self._zoom_widgets.append((widget, actual))
            manager = widget.winfo_manager()
            if manager in ("pack", "grid"):
                info = widget.pack_info() if manager == "pack" else widget.grid_info()
                pads = {key:tuple(int(n) for n in (info[key] if isinstance(info[key], (tuple,list)) else self.root.tk.splitlist(str(info[key]))))
                        for key in ("padx", "pady", "ipadx", "ipady") if key in info}
                self._zoom_geometry.append((widget, manager, pads))
        self._zoom_style_fonts = {}
        style = ttk.Style(self.root)
        for name in (".", "Title.TLabel", "CardTitle.TLabel", "Status.TLabel", "Hint.TLabel",
                     "PanelHint.TLabel", "HeaderHint.TLabel", "Step.TButton", "Zoom.TButton"):
            spec = style.lookup(name, "font")
            if spec:
                actual = dict(zip(*(iter(self.root.tk.splitlist(self.root.tk.call("font", "actual", spec))),)*2))
                self._zoom_style_fonts[name] = actual

    def apply_zoom(self):
        factor = self.zoom_percent/100
        def font(actual):
            return (actual["-family"], max(7, round(int(actual["-size"])*factor)),
                    actual["-weight"], actual["-slant"])
        for widget, actual in self._zoom_widgets:
            widget.configure(font=font(actual))
        for widget in self._zoom_surfaces:
            widget.set_zoom(factor)
        style = ttk.Style(self.root)
        for name, actual in self._zoom_style_fonts.items():
            style.configure(name, font=font(actual))
        for name, padding in (("TButton",(12,9)),("TEntry",(7,)),("TCombobox",(7,)),
                              ("Step.TButton",(8,5)),("Zoom.TButton",(4,2))):
            style.configure(name, padding=tuple(round(n*factor) for n in padding))
        for widget, manager, pads in self._zoom_geometry:
            if widget.winfo_manager() != manager:
                continue
            config = {key:tuple(round(n*factor) for n in value) for key,value in pads.items()}
            if manager == "pack":
                widget.pack_configure(**config)
            else:
                widget.grid_configure(**config)
        self.zoom_text.set(f"{self.zoom_percent}%")
        self.sidebar.configure(width=round((76 if getattr(self,"_sidebar_compact",False) else 180)*factor))
        self.announcement_area.canvas.yview_moveto(0)

    def set_zoom(self, percent):
        self.auto_fit = False
        if self._fit_pending is not None:
            self.root.after_cancel(self._fit_pending)
            self._fit_pending = None
        self.zoom_percent = max(50, min(150, percent))
        self.apply_zoom()
        return "break"

    def change_zoom(self, delta):
        return self.set_zoom(self.zoom_percent+delta)

    def zoom_key(self, event):
        key = event.keysym
        if key in ("plus", "equal", "KP_Add"):
            return self.change_zoom(10)
        if key in ("minus", "underscore", "KP_Subtract"):
            return self.change_zoom(-10)
        if key in ("0", "KP_0"):
            return self.set_zoom(100)

    def queue_fit(self):
        if self._closed or not hasattr(self, "_zoom_widgets") or not self.auto_fit or self._fitting:
            return
        if self._fit_pending is not None:
            self.root.after_cancel(self._fit_pending)
        self._fit_pending = self.root.after(80, self.fit_announcements)

    def fit_announcements(self):
        if self._closed or not self.root.winfo_exists() or self._fitting:
            return
        if self._fit_pending is not None:
            self.root.after_cancel(self._fit_pending)
        self._fit_pending = None
        self.auto_fit = True
        self._fitting = True
        try:
            for label in self.preview_labels:
                label.grid()
            if not self.summary_frame.winfo_manager():
                self.summary_frame.pack(fill="x", pady=(0,6), before=self.operation_toolbar)
            if not self.guide_frame.winfo_manager():
                self.guide_frame.pack(fill="x", pady=(0,6), before=self.announcement_body)
            self.zoom_percent = 100
            self.apply_zoom()
            self.root.update_idletasks()
            canvas = self.announcement_area.canvas
            bounds = canvas.bbox("all")
            if bounds and bounds[3] > canvas.winfo_height()+1:
                estimate = max(50, min(95, int(100*canvas.winfo_height()/bounds[3])//5*5))
                for percent in range(estimate, 49, -5):
                    self.zoom_percent = percent
                    self.apply_zoom()
                    self.root.update_idletasks()
                    bounds = canvas.bbox("all")
                    if bounds and bounds[3] <= canvas.winfo_height()+1:
                        break
            if bounds and bounds[3] > canvas.winfo_height()+1:
                for label in self.preview_labels:
                    label.grid_remove()
                self.root.update_idletasks()
                bounds = canvas.bbox("all")
                if bounds and bounds[3] > canvas.winfo_height()+1:
                    self.summary_frame.pack_forget()
                    self.root.update_idletasks()
                    bounds = canvas.bbox("all")
                    if bounds and bounds[3] > canvas.winfo_height()+1:
                        self.guide_frame.pack_forget()
                        self.root.update_idletasks()
                        bounds = canvas.bbox("all")
                        if bounds and bounds[3] > canvas.winfo_height()+1:
                            for panel in self.announcement_panels:
                                panel.pack_configure(pady=0)
                            self.root.update_idletasks()
        finally:
            self._fitting = False

    def is_text_editor(self, widget):
        return isinstance(widget, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry,
                                   ttk.Combobox, ttk.Spinbox))

    def install_space_shortcut(self):
        tag = "RaceVoicePushToTalk"
        self.root.bind_class(tag, "<KeyPress-space>", self.space_press)
        self.root.bind_class(tag, "<KeyRelease-space>", self.space_release)
        self.root.bind_class(tag, "<FocusIn>", self.space_focus)
        self.root.bind("<Button-1>", self.background_focus, add="+")
        for widget in self.all_widgets():
            widget.bindtags((tag, *widget.bindtags()))

    def space_press(self, event):
        if self.is_text_editor(event.widget) or event.widget.winfo_toplevel() is not self.root:
            return
        if event.state & 0xC:
            return "break"
        if self.running and self.role.get() == "sender":
            self.space_held = True
            return "break"
        # Space must not activate a focused Send/STOP/connect button.
        return "break"

    def space_release(self, event):
        if self.space_held:
            self.space_held = False
            return "break"
        if not self.is_text_editor(event.widget):
            return "break"

    def background_focus(self, event):
        if not self.is_text_editor(event.widget) and self.is_text_editor(self.root.focus_get()):
            self.root.focus_set()

    def space_focus(self, event):
        if self.is_text_editor(event.widget):
            self.space_held = False

    def select_page(self, index):
        self.held = False
        self.tabs.select(index)
        self.navigation_changed()

    def navigation_changed(self, event=None):
        self.held = self.space_held = False
        selected = self.tabs.index(self.tabs.select())
        self.page_title.configure(text=("Announcements", "Connection", "Audio setup")[selected])
        for index, button in enumerate(self.nav_buttons):
            active = selected == index
            button.configure(fill="#eee7fb" if active else "#ffffff",
                             foreground="#7954c6" if active else "#7c8397")

    def window_resized(self, event):
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if getattr(self, "_window_size", None) == size:
            return
        self._window_size = size
        self.reflow_sidebar(event)
        self.queue_fit()

    def window_destroyed(self, event):
        if event.widget is self.root:
            self._closed = True
            if self._fit_pending is not None:
                self.root.after_cancel(self._fit_pending)
                self._fit_pending = None

    def reflow_sidebar(self, event):
        if event.widget is not self.root:
            return
        compact = event.width < 1150
        if getattr(self, "_sidebar_compact", None) == compact:
            return
        self._sidebar_compact = compact
        self.sidebar.configure(width=round((76 if compact else 180)*self.zoom_percent/100))
        labels = ("Send", "Link", "Audio") if compact else ("Announcements", "Connection", "Audio setup")
        for button, text in zip(self.nav_buttons, labels):
            button.configure(text=text, font=("Segoe UI", 9 if compact else 11, "bold"),
                             padding=(3 if compact else 12, 8))
        for label in (self.sidebar_name, self.sidebar_caption):
            if compact:
                label.pack_forget()
            else:
                label.pack(anchor="w", padx=22, pady=(0, 3) if label is self.sidebar_name else (0, 28),
                           before=self.nav_buttons[0])
        self.sidebar_brand.pack_configure(padx=16 if compact else 22, pady=(24, 24 if compact else 2))
        self.sidebar_version.configure(text="v0.5" if compact else "Race Voice\nv0.5.1")
        self.sidebar_version.pack_configure(padx=12 if compact else 22)
        self.navigation_changed()
        self.queue_fit()

    def update_summary(self):
        values = (
            "Connected" if self.engine.connected else "Offline",
            ("Enabled" if self.engine.enabled else "Muted") if self.role.get() == "receiver"
                else ("Ready" if self.engine.ready else "Waiting"),
            "Live" if self.mic_status.get() == "MIC LIVE" else "Off")
        for variable, value in zip(self.summary_vars, values):
            if variable.get() != value:
                variable.set(value)

    def reflow_toolbar(self, event):
        counters_width = sum(box.winfo_reqwidth()+32 for box in self.counter_boxes)
        stacked = event.width < counters_width
        compact = stacked or event.width < counters_width+self.stop_button.winfo_reqwidth()+14
        layout = (compact, stacked)
        if getattr(self, "_toolbar_compact", None) == layout:
            return
        self._toolbar_compact = layout
        for index, panel in enumerate(self.counter_panels):
            panel.grid(row=index if stacked else 0, column=0 if stacked else index,
                       columnspan=2 if stacked else 1, sticky="ew",
                       padx=0 if stacked else (0, 8), pady=(0, 6) if stacked else 0)
        if compact:
            self.stop_button.grid(row=2 if stacked else 1, column=0, columnspan=2,
                                  sticky="ew", padx=0, pady=(4, 0))
        else:
            self.stop_button.grid(row=0, column=2, columnspan=1,
                                  sticky="se", padx=(8, 0), pady=0)

    def refresh_previews(self, *_):
        for preview, (_, message) in zip(self.preview_vars, self.slots):
            preview.set(message.get().replace("{minutes}", self.minutes.get()).replace("{round}", self.round.get()))

    def step_counter(self, variable, delta):
        try:
            number = int(variable.get())
        except ValueError:
            number = 0
        variable.set(str(max(0, min(999, number+delta))))

    def check_focus(self, event):
        def release_if_outside():
            try:
                if self.root.focus_get() is None:
                    self.held = self.space_held = False
            except tk.TclError:
                self.held = self.space_held = False
        self.root.after_idle(release_if_outside)

    def press_talk(self, event):
        if self.running and self.role.get() == "sender":
            self.held = True

    def sync_mode(self):
        sender = self.role.get() == "sender"
        self.mode_label.configure(text="SENDER  /  Main hall" if sender else "RECEIVER  /  Other room")
        for widget in [*self.send_widgets, *self.sender_audio_controls]:
            widget.configure(state="normal" if sender else "disabled")
        self.receiver_toggle.configure(state="disabled" if sender else "normal")

    def auto_connect_saved(self):
        if not self.auto_connect.get() or self.running:
            return
        if not self.settings.get("url") or not self.key.get():
            self.status.set("Auto-connect skipped. Enter and save your connection address and access key.")
            return
        parsed = urlparse(self.url.get().strip())
        if not parsed.hostname or (parsed.scheme != "wss" and not (
                parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1"))):
            self.status.set("Auto-connect skipped. Check the saved relay URL in Connection.")
            return
        if self.role.get() == "receiver" and not self.options[2]:
            self.status.set("Auto-connect skipped. No receiver speakers are available.")
            return
        for index in ([0, 1] if self.role.get() == "sender" else [2]):
            saved_id = self.settings.get(f"device{index}")
            if saved_id and not any(device.id == saved_id for device in self.options[index]):
                self.status.set("Auto-connect skipped. A saved audio device is unavailable; check Audio setup.")
                return
        self.toggle()

    def test_receiver(self):
        if not (self.running and self.role.get() == "receiver" and self.engine.enabled):
            messagebox.showinfo("Local test", "Connect in receiver mode and enable receiver audio first.")
            return
        self.engine.queue_announcement(
            "Testing one two three, four, five. This is a longer local announcement. "
            "You should hear this final sentence completely. End of test.")

    def open_speech_log(self):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(str(CONFIG.parent))

    def change_role(self, *_):
        self.space_held = False
        self.saved_keys[self.key_role] = self.key.get()
        self.key_role = self.role.get()
        self.key.set(self.saved_keys.get(self.key_role, ""))
        self.enable.set(self.key_role == "receiver")
        self.held = False
        self.sync_mode()

    def save(self):
        settings = {"role": self.role.get(), "url": self.url.get(),
                    "auto_connect": self.auto_connect.get(),
                    "zoom": self.zoom_percent, "auto_fit": self.auto_fit,
                    "minutes": self.minutes.get(), "round": self.round.get(),
                    "slots": [[a.get(), b.get()] for a, b in self.slots]}
        for i, widget in enumerate(self.device_lists):
            if widget.current() >= 0:
                settings[f"device{i}"] = self.options[i][widget.current()].id
        for i, var in enumerate(self.volume_vars):
            settings[f"volume{i}"] = var.get()
        try:
            self.saved_keys[self.role.get()] = self.key.get()
            settings["protected_keys"] = {
                role: base64.b64encode(win32crypt.CryptProtectData(
                    key.encode("utf-8"), "Race Voice access key", None, None, None, 1
                )).decode("ascii")
                for role, key in self.saved_keys.items() if key
            }
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except Exception:
            messagebox.showerror("Cannot save settings", "Could not protect or save settings. Your changes may not be remembered.")

    def send(self, data):
        if data.get("type") == "stop" and self.role.get() == "receiver":
            self.engine.announcements.cancel("stop_command")
            return
        if self.role.get() != "sender" or not self.engine.send_command(data):
            messagebox.showwarning("Not sent", "Connect as sender and enable audio on the receiver first.")

    def say(self, text):
        text = text.replace("{minutes}", self.minutes.get()).replace("{round}", self.round.get()).strip()
        if not text or len(text) > 1000:
            messagebox.showwarning("Announcement", "Enter 1 to 1000 characters.")
            return
        self.send({"type": "say", "text": text})

    def toggle(self):
        if self.running:
            self.space_held = self.held = False
            self.engine.close()
            self.running = False
            self.system.set(False)
            self.always.set(False)
            self.connect_button.configure(state="disabled", text="Disconnecting...")
            self.wait_closed()
            return
        parsed = urlparse(self.url.get().strip())
        if parsed.scheme != "wss" and not (parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1")):
            messagebox.showerror("Relay URL", "Use wss:// for your cloud server. ws:// is allowed only for localhost tests.")
            return
        if not self.key.get():
            messagebox.showerror("Access key", "Enter the key for the selected mode.")
            return
        ids = [options[widget.current()].id if widget.current() >= 0 else None
               for options, widget in zip(self.options, self.device_lists)]
        if self.role.get() == "receiver" and not ids[2]:
            messagebox.showerror("Speakers", "No playback device available.")
            return
        self.save()
        self.engine = Engine()
        self.engine.launch(self.url.get().strip(), self.key.get(), self.role.get(), *ids)
        self.running = True
        for widget in self.locked:
            widget.configure(state="disabled")
        self.connect_button.configure(text="Disconnect")

    def wait_closed(self):
        if any(t.is_alive() for t in self.engine.threads):
            self.root.after(100, self.wait_closed)
            return
        for widget in self.locked:
            widget.configure(state="readonly" if isinstance(widget, ttk.Combobox) else "normal")
        self.connect_button.configure(state="normal", text="Connect")
        self.status.set("Disconnected")

    def tick(self):
        sender = self.running and self.role.get() == "sender"
        pressed = bool(ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000)
        self.engine.system_on = sender and self.system.get()
        self.engine.mic_on = sender and (self.always.get() or self.held or self.space_held or pressed)
        enabled = self.running and self.role.get() == "receiver" and self.enable.get() and not self.engine.playback_failed
        if self.engine.enabled and not enabled:
            self.engine.clear("audio_disabled")
        self.engine.enabled = enabled
        self.engine.volumes = [v.get() for v in self.volume_vars]
        self.mic_status.set("MIC LIVE" if self.engine.mic_on and self.engine.ready and self.engine.capture_ready[1] else "MIC OFF")
        talk_text = "MIC LIVE  |  SPACE / F8" if self.mic_status.get() == "MIC LIVE" else "HOLD TO TALK  |  SPACE / F8"
        if self.ptt.cget("text") != talk_text:
            self.ptt.configure(text=talk_text)
        self.meter["value"] = self.engine.peak * 100
        self.engine.peak *= .8
        self.update_summary()
        self.speech_diagnostic.set(self.engine.speech_diagnostic)
        while True:
            try:
                self.status.set(self.engine.events.get_nowait())
            except queue.Empty:
                break
        self.root.after(30, self.tick)

    def quit(self):
        self.save()
        self.engine.close()
        self.root.destroy()


if __name__ == "__main__":
    App(tk.Tk())
    tk.mainloop()
