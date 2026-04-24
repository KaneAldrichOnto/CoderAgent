#!/usr/bin/env python3
"""
sample_gui.py - Tiny Tkinter app used by test_gui_nav_sample_gui.md.

Window title:   CoderAgent Sample GUI
Controls:
  - Edit (single-line entry) named "InputBox"
  - Button "Submit" — copies the entry text into the StatusLabel
  - StaticText "StatusLabel" — initially "ready", becomes "got: <text>"
  - Button "Quit" — closes the window cleanly

Run with: python sample_gui.py
"""

from __future__ import annotations

import tkinter as tk


def main() -> int:
    root = tk.Tk()
    root.title("CoderAgent Sample GUI")
    root.geometry("360x160")

    entry = tk.Entry(root, name="inputbox", width=30)
    entry.pack(pady=10)

    status = tk.Label(root, text="ready", name="statuslabel")
    status.pack(pady=5)

    def on_submit() -> None:
        status.config(text=f"got: {entry.get()}")

    tk.Button(root, text="Submit", name="submit", command=on_submit).pack(pady=5)
    tk.Button(root, text="Quit", name="quit", command=root.destroy).pack(pady=5)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
