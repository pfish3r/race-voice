"""Real Tk layout/interaction smoke tests with no audio or network activity."""
import ast
import base64
import json
import uuid
import tkinter as tk
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk
from unittest.mock import Mock, patch
import queue
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modern_widgets import RoundedPanel, RoundedButton
SOURCE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
UI_CODE = ast.Module(body=[node for node in SOURCE.body
    if isinstance(node, ast.ClassDef) and node.name in ("ScrollArea", "App")], type_ignores=[])


class UiTests(unittest.TestCase):
    def setUp(self):
        self.config = ROOT.parent / ("ui-settings-" + uuid.uuid4().hex + ".json")
        self.addCleanup(lambda: self.config.unlink(missing_ok=True))
        self.roots = []
        self.addCleanup(self.close_roots)
        speaker = SimpleNamespace(id="speakers-test", name="Room speakers")
        microphone = SimpleNamespace(id="microphone-test", name="Test microphone", isloopback=False)
        self.devices = SimpleNamespace(all_speakers=lambda: [speaker],
                                       all_microphones=lambda: [microphone])
        self.messages = SimpleNamespace(showerror=Mock(), showwarning=Mock(), showinfo=Mock())
        self.crypto = SimpleNamespace(
            CryptProtectData=lambda data, *args: b"protected:" + data,
            CryptUnprotectData=lambda data, *args: ("test", data.removeprefix(b"protected:")))
        self.namespace = dict(tk=tk, ttk=ttk, json=json, base64=base64,
                              CONFIG=self.config, Engine=lambda: SimpleNamespace(),
                              sc=self.devices, win32crypt=self.crypto,
                              messagebox=self.messages, urlparse=urlparse,
                              RoundedPanel=RoundedPanel, RoundedButton=RoundedButton)
        exec(compile(UI_CODE, "app.py", "exec"), self.namespace)
        self.App = self.namespace["App"]
        self.original_tick = self.App.tick
        self.App.tick = lambda app: None

    def close_roots(self):
        for root in self.roots:
            try:
                for callback in root.tk.call("after", "info"):
                    root.after_cancel(callback)
                root.destroy()
            except tk.TclError:
                pass

    def build(self, settings=None, scale=1.33, width=1080, height=740):
        self.close_roots()
        self.roots.clear()
        if settings is not None:
            self.config.write_text(json.dumps(settings), encoding="utf-8")
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Tk unavailable: " + str(error))
        # Never display a visible test window or access user audio devices.
        root.withdraw()
        # Repeated independent Tcl interpreters can leave an implicit default root.
        # Bind this isolated fixture explicitly before App creates StringVars.
        tk._default_root = root
        self.roots.append(root)
        root.tk.call("tk", "scaling", scale)
        app = self.App(root)
        app.auto_fit = False  # Fit behavior is exercised explicitly in dedicated checks.
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        root.geometry(f"{width}x{height}")
        # Mapping at alpha zero is required for geometry allocation of Notebook tabs.
        root.attributes("-alpha", 0)
        root.deiconify()
        root.update()
        return root, app

    def test_layout_controls_remain_reachable_at_small_sizes_and_scaling(self):
        for scale in (1.33, 2.0):
            root, app = self.build(scale=scale)
            with self.subTest(scale=scale):
                self.assertEqual(len(app.tabs.tabs()), 3)
                self.assertEqual(len(app.slots), 5)
                self.assertEqual(len(app.preview_labels), 5)
                send_buttons = [widget for widget in app.send_widgets if widget is not app.ptt]
                self.assertGreaterEqual(app.ptt.winfo_reqheight(), 30)
                self.assertEqual(len(send_buttons), 6)
                self.assertTrue(all(widget.winfo_reqheight() >= 28 for widget in send_buttons))
            for width, height in ((1080, 740), (800, 600), (640, 480)):
                with self.subTest(scale=scale, width=width, height=height):
                    root.geometry(f"{width}x{height}")
                    root.update()
                    area = app.announcement_area
                    self.assertTrue(area.winfo_ismapped())
                    self.assertGreater(area.canvas.winfo_height(), 80)
                    self.assertLessEqual(area.winfo_rootx() + area.winfo_width(),
                                         root.winfo_rootx() + root.winfo_width() + 2)
                    self.assertGreaterEqual(app.ptt.winfo_width(), app.ptt.winfo_reqwidth())
                    for box in app.counter_boxes:
                        self.assertGreaterEqual(box.winfo_width(), box.winfo_reqwidth())
                    bounds = area.canvas.bbox("all")
                    self.assertIsNotNone(bounds)
                    self.assertGreater(bounds[3], 0)
                    # Large send controls must fit horizontally, including at high DPI.
                    for widget in send_buttons[1:]:
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                             area.canvas.winfo_rootx() + area.canvas.winfo_width() + 2)
                    area.canvas.yview_moveto(1)
                    root.update()
                    # Scrolling must reach the last preset rather than clip it.
                    last = send_buttons[-1]
                    self.assertLessEqual(last.winfo_rooty() + last.winfo_height(),
                                         area.canvas.winfo_rooty() + area.canvas.winfo_height() + 2)

    def test_fit_shows_all_presets_without_scrolling(self):
        for scale, width, height in ((1.33,1080,740),(2.0,800,600),(2.0,640,480)):
            root, app = self.build(scale=scale,width=width,height=height)
            app.fit_announcements()
            root.update_idletasks()
            area = app.announcement_area.canvas
            for button in app.send_widgets:
                self.assertGreaterEqual(button.winfo_rooty(),area.winfo_rooty()-1)
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                     area.winfo_rooty()+area.winfo_height()+1)
            self.assertTrue(app.auto_fit)

    def test_zoom_shortcuts_limits_and_saved_preference(self):
        root, app = self.build()
        app.zoom_key(SimpleNamespace(keysym="minus"))
        self.assertEqual(app.zoom_percent,90)
        self.assertFalse(app.auto_fit)
        old = app.announcement_editors[0].cget("font")
        app.zoom_key(SimpleNamespace(keysym="KP_Add"))
        self.assertEqual(app.zoom_percent,100)
        self.assertNotEqual(app.announcement_editors[0].cget("font"),old)
        for _ in range(20): app.change_zoom(-10)
        self.assertEqual(app.zoom_percent,50)
        for _ in range(20): app.change_zoom(10)
        self.assertEqual(app.zoom_percent,150)
        app.zoom_key(SimpleNamespace(keysym="0"))
        self.assertEqual(app.zoom_percent,100)
        app.auto_fit=True;app.queue_fit()
        app.set_zoom(80)
        self.assertIsNone(app._fit_pending)
        app.save()
        _, restored = self.build()
        self.assertEqual(restored.zoom_percent,80)
        self.assertFalse(restored.auto_fit)
        self.assertTrue(root.bind("<Control-KeyPress>"))

    def test_space_ptt_never_sends_a_focused_button_and_allows_editing(self):
        root, app = self.build()
        button = app.send_widgets[-1]
        self.assertEqual(button.bindtags()[0],"RaceVoicePushToTalk")
        app.running = True
        app.send = Mock()
        event = SimpleNamespace(widget=button,state=0)
        self.assertEqual(app.space_press(event),"break")
        self.assertTrue(app.space_held)
        app.space_press(event)  # Key auto-repeat is harmless.
        app.send.assert_not_called()
        self.assertEqual(app.space_release(event),"break")
        self.assertFalse(app.space_held)
        for editor in [*app.announcement_editors,*app.title_editors,*app.template_editors]:
            self.assertIsNone(app.space_press(SimpleNamespace(widget=editor,state=0)))
            self.assertFalse(app.space_held)
        app.space_press(event)
        app.space_focus(SimpleNamespace(widget=app.announcement_editors[0]))
        self.assertFalse(app.space_held)
        self.assertEqual(app.space_press(SimpleNamespace(widget=button,state=4)),"break")
        self.assertFalse(app.space_held)
        app.role.set("receiver")
        app.space_press(event)
        self.assertFalse(app.space_held)
        app.role.set("sender");app.running=False;app.space_press(event)
        self.assertFalse(app.space_held)

    def test_space_releases_on_focus_loss_and_controls_live_microphone(self):
        root, app = self.build()
        self.namespace["ctypes"] = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(GetAsyncKeyState=lambda _:0)))
        self.namespace["queue"] = queue
        app.engine=SimpleNamespace(connected=True,ready=True,capture_ready=[True,True],
            enabled=False,playback_failed=False,peak=0,events=queue.Queue(),
            speech_diagnostic="No announcement received")
        app.running=True
        event=SimpleNamespace(widget=app.ptt,state=0)
        app.space_press(event)
        self.original_tick(app)
        self.assertTrue(app.engine.mic_on)
        self.assertIn("MIC LIVE",app.ptt.cget("text"))
        app.space_release(event)
        self.original_tick(app)
        self.assertFalse(app.engine.mic_on)
        self.assertIn("HOLD TO TALK",app.ptt.cget("text"))
        app.space_press(event)
        with patch.object(root,"focus_get",return_value=None):
            app.check_focus(None)
            root.update_idletasks()
        self.assertFalse(app.space_held)
        self.original_tick(app)
        self.assertFalse(app.engine.mic_on)

    def test_preview_tracks_round_minutes_and_edited_template(self):
        root, app = self.build()
        app.minutes.set("7")
        app.round.set("3")
        self.assertIn("round 3", app.preview_vars[1].get())
        self.assertIn("7 minutes", app.preview_vars[1].get())
        editor = app.template_editors[1]
        editor.delete(0, "end")
        editor.insert(0, "Round {round} starts in {minutes} minutes.")
        root.update()
        self.assertEqual(app.slots[1][1].get(), "Round {round} starts in {minutes} minutes.")
        self.assertEqual(app.preview_vars[1].get(), "Round 3 starts in 7 minutes.")

    def test_inline_edits_send_sync_both_tabs_and_survive_restart(self):
        root, app = self.build()
        self.assertEqual(len(app.title_editors), 5)
        self.assertEqual(len(app.announcement_editors), 5)
        title = app.title_editors[1]
        title.delete(0, "end")
        title.insert(0, "Next qualifying")
        message = "Qualifying round {round} starts in {minutes} minutes. Bring your cars."
        editor = app.announcement_editors[1]
        self.assertIsInstance(editor, ttk.Entry)
        editor.delete(0, "end")
        editor.insert(0, message)
        app.round.set("2")
        app.minutes.set("6")
        root.update()
        self.assertEqual(app.slots[1][0].get(), "Next qualifying")
        self.assertEqual(app.slots[1][1].get(), message)
        self.assertEqual(app.template_editors[1].get(), message)
        self.assertEqual(app.preview_vars[1].get(),
                         "Qualifying round 2 starts in 6 minutes. Bring your cars.")
        app.send = Mock()
        card = editor.master
        buttons = [widget for widget in app.send_widgets
                   if widget.master is card and widget.cget("text") == "Send"]
        self.assertEqual(len(buttons), 1)
        buttons[0].invoke()
        app.send.assert_called_once_with({
            "type": "say", "text": "Qualifying round 2 starts in 6 minutes. Bring your cars."})
        # Edits from the settings tab must update the main screen too.
        replacement = "Drivers for round {round}, prepare for a start in {minutes} minutes."
        settings_editor = app.template_editors[1]
        settings_editor.delete(0, "end")
        settings_editor.insert(0, replacement)
        root.update()
        self.assertEqual(editor.get(), replacement)
        self.assertEqual(app.preview_vars[1].get(),
                         "Drivers for round 2, prepare for a start in 6 minutes.")
        app.save()
        _, restored = self.build()
        self.assertEqual(restored.title_editors[1].get(), "Next qualifying")
        self.assertEqual(restored.announcement_editors[1].get(), replacement)
        self.assertEqual(restored.template_editors[1].get(), replacement)
        self.assertEqual(restored.preview_vars[1].get(),
                         "Drivers for round 2, prepare for a start in 6 minutes.")
        self.messages.showerror.assert_not_called()

    def test_sidebar_navigation_selects_pages(self):
        root, app = self.build()
        self.assertEqual(len(app.nav_buttons), 3)
        for index in (1, 2, 0):
            app.nav_buttons[index].invoke()
            root.update()
            self.assertEqual(app.tabs.index(app.tabs.select()), index)

    def test_counter_boundaries_and_invalid_entry_recovery(self):
        _, app = self.build()
        for value, delta, expected in (("0", -1, "0"), ("999", 1, "999"),
                                       ("oops", 1, "1"), ("12", -1, "11")):
            app.minutes.set(value)
            app.step_counter(app.minutes, delta)
            self.assertEqual(app.minutes.get(), expected)

    def test_saved_receiver_mode_enables_audio_and_disables_sender_actions(self):
        _, app = self.build({"role": "receiver"})
        self.assertTrue(app.enable.get())
        self.assertTrue(all(widget.instate(["disabled"]) for widget in app.send_widgets))
        self.assertTrue(app.receiver_toggle.instate(["!disabled"]))
        app.role.set("sender")
        self.assertFalse(app.enable.get())
        self.assertTrue(all(widget.instate(["!disabled"]) for widget in app.send_widgets))
        self.assertTrue(app.receiver_toggle.instate(["disabled"]))

    def test_save_restores_autoconnect_counters_templates_and_protected_key(self):
        _, app = self.build()
        app.auto_connect.set(True)
        app.key.set("test-access-key")
        app.url.set("wss://relay.example:8843")
        app.minutes.set("15")
        app.round.set("4")
        app.slots[0][1].set("Practice in {minutes}.")
        app.save()
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(saved["auto_connect"])
        self.assertNotIn("test-access-key", self.config.read_text(encoding="utf-8"))
        _, restored = self.build()
        self.assertTrue(restored.auto_connect.get())
        self.assertEqual(restored.key.get(), "test-access-key")
        self.assertEqual(restored.minutes.get(), "15")
        self.assertEqual(restored.round.get(), "4")
        self.assertEqual(restored.preview_vars[0].get(), "Practice in 15.")
        self.messages.showerror.assert_not_called()

    def test_autoconnect_is_opt_in_and_checks_configuration_without_network(self):
        _, app = self.build()
        app.toggle = Mock()
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        app.auto_connect.set(True)
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        app.settings["url"] = "wss://relay.example:8843"
        app.url.set(app.settings["url"])
        app.key.set("test-key")
        app.auto_connect_saved()
        app.toggle.assert_called_once()
        app.toggle.reset_mock()
        app.running = True
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        app.running = False
        app.url.set("https://relay.example")
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        app.url.set("wss://relay.example:8843")
        app.role.set("receiver")
        app.options[2] = []
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        app.options[2] = [SimpleNamespace(id="different-output")]
        app.settings["device2"] = "missing-saved-output"
        app.key.set("test-receiver-key")
        app.auto_connect_saved()
        app.toggle.assert_not_called()
        self.assertIn("saved audio device", app.status.get())


if __name__ == "__main__":
    unittest.main()
