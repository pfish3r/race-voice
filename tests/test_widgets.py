"""Native Tk widget interaction/layout checks without audio or network."""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modern_widgets import RoundedButton, RoundedPanel


class WidgetTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.root.attributes("-alpha", 0)
        self.root.geometry("640x480")
        self.addCleanup(self.root.destroy)

    def show(self):
        self.root.deiconify()
        self.root.update()

    def test_button_mouse_keyboard_and_disabled_invoke_once(self):
        calls = []
        button = RoundedButton(self.root, text="Send", command=lambda: calls.append("send"))
        button.pack()
        self.show()
        event = SimpleNamespace(x=10, y=10)
        button._press(event)
        button._release(event)
        button._release(event)
        self.assertEqual(calls, ["send"])
        button._key_press(event)
        button._key_press(event)
        button._key_release(event)
        button._key_release(event)
        self.assertEqual(calls, ["send", "send"])
        button.configure(state="disabled")
        button._press(event)
        button._release(event)
        button.invoke()
        self.assertEqual(len(calls), 2)
        self.assertTrue(button.instate(["disabled"]))
        self.assertFalse(button.instate(["!disabled"]))
        button.configure(state="normal", text="Send announcement")
        self.root.update()
        self.assertTrue(button.instate(["!disabled"]))
        self.assertEqual(button.cget("text"), "Send announcement")
        self.assertGreater(button.winfo_reqwidth(), 180)

    def test_panel_follows_wrapped_content_and_width(self):
        panel = RoundedPanel(self.root, padding=(15, 10))
        panel.pack(fill="x")
        text = tk.Label(panel.body, text="A wrapped editable announcement", wraplength=250)
        text.pack(fill="x")
        self.show()
        self.assertEqual(panel.winfo_height(), panel.body.winfo_reqheight()+20)
        self.assertEqual(panel.body.winfo_width(), panel.winfo_width()-30)
        old_height = panel.winfo_reqheight()
        text.configure(text="Long announcement. "*20)
        self.root.update()
        self.assertGreater(panel.winfo_reqheight(), old_height)
        self.root.geometry("800x480")
        self.root.update()
        self.assertEqual(panel.body.winfo_width(), panel.winfo_width()-30)

    def test_button_size_uses_font_metrics_at_larger_scaling(self):
        self.root.tk.call("tk", "scaling", 2.0)
        button = RoundedButton(self.root, text="HOLD TO TALK", font=("Segoe UI", 18, "bold"))
        button.pack(fill="x")
        self.show()
        self.assertGreaterEqual(button.winfo_reqheight(), 65)
        self.assertGreaterEqual(button.winfo_reqwidth(), 250)


if __name__ == "__main__":
    unittest.main()
