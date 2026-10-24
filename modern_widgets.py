"""Small rounded Tk controls. No image assets or third-party UI dependency."""
import tkinter as tk
from tkinter import ttk
from tkinter.font import Font


def _rounded_polygon(canvas, x1, y1, x2, y2, radius, **options):
    radius = max(0, min(radius, (x2-x1)/2, (y2-y1)/2))
    points = (
        x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
        x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
        x1, y2, x1, y2-radius, x1, y1+radius, x1, y1)
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class RoundedButton(tk.Canvas):
    """A keyboard-operable rounded action with ttk-like state helpers."""
    def __init__(self, parent, text="", command=None, font=("Segoe UI", 14, "bold"),
                 fill="#7653d6", foreground="#ffffff", hover_fill="#6846c2",
                 pressed_fill=None, disabled_fill="#e4e7ef",
                 disabled_foreground="#9299ad", padding=(18, 14), width=0,
                 state="normal", radius=12, **kwargs):
        self._text = str(text)
        self._command = command
        self._font = Font(root=parent, font=font)
        self._fill = fill
        self._foreground = foreground
        self._hover_fill = hover_fill
        self._pressed_fill = pressed_fill or hover_fill
        self._disabled_fill = disabled_fill
        self._disabled_foreground = disabled_foreground
        self._padding = padding if isinstance(padding, (tuple, list)) else (padding, padding)
        self._minimum_chars = width
        self._state = state
        self._radius = radius
        self._hover = self._pressed = self._focused = False
        self._ready = False
        kwargs.setdefault("background", "#f4f5fa")
        kwargs.setdefault("takefocus", 1)
        super().__init__(parent, highlightthickness=0, borderwidth=0,
                         cursor="hand2", **kwargs)
        self._ready = True
        self._resize_request()
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<KeyPress-space>", self._key_press)
        self.bind("<KeyRelease-space>", self._key_release)
        self.bind("<KeyPress-Return>", self._key_press)
        self.bind("<KeyRelease-Return>", self._key_release)
        self._redraw()

    def _resize_request(self):
        width = max(self._font.measure(self._text),
                    self._font.measure("0") * max(0, self._minimum_chars))
        super().configure(width=width + self._padding[0]*2 + 6,
                          height=self._font.metrics("linespace") + self._padding[1]*2 + 6)

    def _redraw(self, event=None):
        if not self._ready:
            return
        width = self.winfo_width() if self.winfo_width() > 1 else self.winfo_reqwidth()
        height = self.winfo_height() if self.winfo_height() > 1 else self.winfo_reqheight()
        if self._state == "disabled":
            fill, foreground = self._disabled_fill, self._disabled_foreground
        else:
            fill = self._pressed_fill if self._pressed else self._hover_fill if self._hover else self._fill
            foreground = self._foreground
        self.delete("button")
        _rounded_polygon(self, 3, 3, width-3, height-3, self._radius,
                         fill=fill, outline="", tags="button")
        if self._focused and self._state != "disabled":
            _rounded_polygon(self, 1, 1, width-1, height-1, self._radius+2,
                             fill="", outline="#9d85d7", width=2, tags="button")
        self.create_text(width/2, height/2, text=self._text, font=self._font,
                         fill=foreground, tags="button")
        super().configure(cursor="arrow" if self._state == "disabled" else "hand2")

    def _enter(self, event):
        self._hover = True
        self._redraw()

    def _leave(self, event):
        self._hover = False
        self._redraw()

    def _focus_in(self, event):
        self._focused = True
        self._redraw()

    def _focus_out(self, event):
        self._focused = self._pressed = False
        self._redraw()

    def _press(self, event):
        if self._state == "disabled":
            return
        self.focus_set()
        self._pressed = True
        self._redraw()

    def _release(self, event):
        pressed = self._pressed
        self._pressed = False
        self._redraw()
        if pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            return self.invoke()

    def _key_press(self, event):
        if self._state != "disabled" and not self._pressed:
            self._pressed = True
            self._redraw()

    def _key_release(self, event):
        pressed = self._pressed
        self._pressed = False
        self._redraw()
        if pressed:
            return self.invoke()

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            return self.cget(cnf)
        if cnf:
            kwargs = {**cnf, **kwargs}
        if not kwargs:
            return super().configure()
        custom = {
            "text": "_text", "command": "_command", "state": "_state",
            "fill": "_fill", "foreground": "_foreground", "hover_fill": "_hover_fill",
            "pressed_fill": "_pressed_fill", "disabled_fill": "_disabled_fill",
            "disabled_foreground": "_disabled_foreground", "radius": "_radius",
            "width": "_minimum_chars"}
        resize = any(key in kwargs for key in ("text", "font", "width", "padding"))
        for key, attribute in custom.items():
            if key in kwargs:
                setattr(self, attribute, kwargs.pop(key))
        if "font" in kwargs:
            self._font = Font(root=self, font=kwargs.pop("font"))
        if "padding" in kwargs:
            value = kwargs.pop("padding")
            self._padding = value if isinstance(value, (tuple, list)) else (value, value)
        if self._state == "disabled":
            self._pressed = False
        if kwargs:
            super().configure(**kwargs)
        if resize:
            self._resize_request()
        if self._ready:
            self._redraw()

    config = configure

    def cget(self, key):
        values = {"state": self._state, "text": self._text, "command": self._command,
                  "fill": self._fill, "foreground": self._foreground,
                  "hover_fill": self._hover_fill, "pressed_fill": self._pressed_fill,
                  "disabled_fill": self._disabled_fill, "disabled_foreground": self._disabled_foreground,
                  "font": str(self._font), "padding": self._padding}
        return values[key] if key in values else super().cget(key)

    def invoke(self):
        if self._state != "disabled" and callable(self._command):
            return self._command()

    def instate(self, statespec, callback=None, *args):
        matches = all((self._state != state[1:] if state.startswith("!") else self._state == state)
                      for state in statespec)
        if matches and callback is not None:
            return callback(*args)
        return matches


class RoundedPanel(tk.Canvas):
    """Rounded card with a ttk body whose requested height grows with content."""
    def __init__(self, parent, fill="#ffffff", background="#f4f5fa",
                 radius=16, padding=12, stroke="", **kwargs):
        self._fill = fill
        self._radius = radius
        self._stroke = stroke
        self._padding = padding if isinstance(padding, (tuple, list)) else (padding, padding)
        self._last_width = self._last_height = None
        super().__init__(parent, background=background, highlightthickness=0,
                         borderwidth=0, **kwargs)
        # Per-color style keeps nested body frames consistent with the rounded surface.
        style_name = "Panel" + fill.lstrip("#") + ".TFrame"
        ttk.Style(self).configure(style_name, background=fill, relief="flat", borderwidth=0)
        self.body = ttk.Frame(self, style=style_name)
        self._window = self.create_window(self._padding[0], self._padding[1],
                                         anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._body_changed)
        self.bind("<Configure>", self._canvas_changed)
        self.after_idle(self._body_changed)

    def _body_changed(self, event=None):
        if not self.winfo_exists():
            return
        height = self.body.winfo_reqheight() + 2*self._padding[1]
        if self._last_height != height:
            self._last_height = height
            self.configure(height=height)

    def _canvas_changed(self, event):
        width = max(1, event.width-2*self._padding[0])
        if self._last_width != width:
            self._last_width = width
            self.itemconfigure(self._window, width=width)
        self.delete("surface")
        _rounded_polygon(self, 0, 0, event.width, event.height, self._radius,
                         fill=self._fill, outline=self._stroke, tags="surface")
        self.tag_lower("surface", self._window)
