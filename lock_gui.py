#!/usr/bin/env python3
"""
lock_gui.py — Tkinter GUI wrapper for lock.py

Launches lock.py as a subprocess (lock.py is NOT modified in any way).
If the GUI crashes or is closed mid-run, the subprocess keeps running
until you stop it manually via Ctrl+C in the terminal.

Live plots read the lock_*.csv written by lock.py every 2 s.  The lock
loop itself is not involved in plotting — zero performance impact.

Usage:
    python lock_gui.py
"""

import os
import sys
import glob
import queue
import signal
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

SCRIPT_DIR = Path(__file__).parent


class LockGUI:
    # ── Defaults (edit here to change what the GUI opens with) ──────────────
    DEFAULTS = dict(
        host     = "192.168.100.2",
        setpoint = "298.002634",
        vcenter  = "73.95",
        vrange   = "2.0",
        kp       = "0.001",
        ki       = "0.002",
        rate     = "1.0",
    )
    PLOT_REFRESH_MS   = 2000   # ms between plot redraws (does NOT affect lock)
    CONSOLE_POLL_MS   = 100    # ms between stdout polls

    def __init__(self, root: tk.Tk):
        self.root      = root
        self.proc        = None      # subprocess.Popen handle
        self.log_path    = None      # path to current lock_*.csv
        self._start_time = None      # time.time() when lock was started
        self._stop_gen   = 0         # incremented on start/stop to invalidate stale callbacks
        self._q          = queue.Queue()
        self._plot_job = None

        root.title("Laser Frequency Lock")
        root.resizable(True, True)
        self._build_ui()
        self._schedule_console_poll()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ─── Parameter frame ─────────────────────────────────────────────────
        pf = ttk.LabelFrame(self.root, text="Lock parameters", padding=8)
        pf.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        param_defs = [
            ("Host / IP",      "host"),
            ("Setpoint (THz)", "setpoint"),
            ("vCenter (V)",    "vcenter"),
            ("vRange (V)",     "vrange"),
            ("Kp  V/MHz",      "kp"),
            ("Ki  V/(MHz·s)",  "ki"),
            ("Rate  Hz",       "rate"),
        ]
        self._vars = {}
        for col, (lbl, key) in enumerate(param_defs):
            ttk.Label(pf, text=lbl).grid(row=0, column=col, padx=5, sticky="w")
            v = tk.StringVar(value=self.DEFAULTS[key])
            self._vars[key] = v
            ttk.Entry(pf, textvariable=v, width=14).grid(row=1, column=col, padx=5)

        self._dry_run = tk.BooleanVar(value=False)
        self._do_log  = tk.BooleanVar(value=True)
        n = len(param_defs)
        ttk.Checkbutton(pf, text="Dry run",  variable=self._dry_run).grid(
            row=0, column=n, padx=10, sticky="w")
        ttk.Checkbutton(pf, text="Log CSV",  variable=self._do_log).grid(
            row=1, column=n, padx=10, sticky="w")

        # ─── Control row ─────────────────────────────────────────────────────
        cf = ttk.Frame(self.root, padding=4)
        cf.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8)

        self._btn_start = ttk.Button(cf, text="▶  Start lock", command=self._start)
        self._btn_stop  = ttk.Button(cf, text="■  Stop",       command=self._stop,
                                     state="disabled")
        self._btn_start.pack(side="left", padx=4)
        self._btn_stop .pack(side="left", padx=4)

        self._status_var = tk.StringVar(value="Stopped")
        self._status_lbl = ttk.Label(cf, textvariable=self._status_var,
                                     font=("Helvetica", 11, "bold"), foreground="gray")
        self._status_lbl.pack(side="left", padx=16)

        self._rms_var = tk.StringVar(value="")
        ttk.Label(cf, textvariable=self._rms_var,
                  font=("Helvetica", 10)).pack(side="right", padx=8)

        # ─── Time window selector ─────────────────────────────────────────────
        self._window_var = tk.StringVar(value="All")
        window_cb = ttk.Combobox(
            cf, textvariable=self._window_var,
            values=["1 min", "15 min", "30 min", "1 hr", "3 hr", "6 hr", "All"],
            state="readonly", width=8)
        window_cb.pack(side="right", padx=2)
        ttk.Label(cf, text="Show last:").pack(side="right", padx=(8, 0))

        # ─── Matplotlib figure ────────────────────────────────────────────────
        self._fig = Figure(figsize=(11, 5.5), dpi=100)
        self._fig.subplots_adjust(hspace=0.50, left=0.08, right=0.97,
                                  top=0.93, bottom=0.09)
        self._ax_freq  = self._fig.add_subplot(3, 1, 1)
        self._ax_err   = self._fig.add_subplot(3, 1, 2)
        self._ax_piezo = self._fig.add_subplot(3, 1, 3)
        self._draw_placeholder()

        canvas = FigureCanvasTkAgg(self._fig, master=self.root)
        canvas.get_tk_widget().grid(row=2, column=0, columnspan=2,
                                    sticky="nsew", padx=8, pady=4)
        self._canvas = canvas

        # ─── Console ─────────────────────────────────────────────────────────
        self._console = scrolledtext.ScrolledText(
            self.root, height=8, state="disabled",
            font=("Courier", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white")
        self._console.grid(row=3, column=0, columnspan=2,
                           sticky="ew", padx=8, pady=(0, 6))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

    def _draw_placeholder(self):
        for ax, ylabel, title in [
            (self._ax_freq,  "Freq. dev.\n(MHz)",  "Frequency deviation from mean"),
            (self._ax_err,   "Error\n(MHz)",        "Error signal"),
            (self._ax_piezo, "Piezo (V)",           "Piezo correction voltage"),
        ]:
            ax.cla()
            ax.set_title(title, fontsize=9, loc="left")
            ax.set_ylabel(ylabel, fontsize=8)
            ax.set_xlabel("Time (s)", fontsize=8)
            ax.tick_params(labelsize=8)
            ax.grid(True, alpha=0.3)
            ax.text(0.5, 0.5, "Waiting for data …",
                    transform=ax.transAxes, ha="center", va="center",
                    color="gray", fontsize=10)

    # ── Lock start / stop ────────────────────────────────────────────────────

    def _build_cmd(self):
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "lock.py"),
            "--host",     self._vars["host"].get(),
            "--setpoint", self._vars["setpoint"].get(),
            "--vcenter",  self._vars["vcenter"].get(),
            "--vrange",   self._vars["vrange"].get(),
            "--kp",       self._vars["kp"].get(),
            "--ki",       self._vars["ki"].get(),
            "--rate",     self._vars["rate"].get(),
        ]
        if self._dry_run.get():
            cmd.append("--dry-run")
        if self._do_log.get():
            cmd.append("--log")
        return cmd

    def _start(self):
        self._stop_gen  += 1          # invalidate any pending _on_stopped callback
        cmd = self._build_cmd()
        self._log_console("$ " + " ".join(cmd) + "\n")
        self.log_path    = None
        self._start_time = time.time()
        # Cancel any existing plot loop before starting a new one
        if self._plot_job:
            self.root.after_cancel(self._plot_job)
            self._plot_job = None
        self._draw_placeholder()
        self._canvas.draw_idle()

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(SCRIPT_DIR),
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()

        mode = "DRY RUN" if self._dry_run.get() else "LIVE"
        color = "darkorange" if self._dry_run.get() else "green"
        self._set_status(f"Running  ({mode})", color)
        self._btn_start.config(state="disabled")
        self._btn_stop .config(state="normal")
        self._schedule_plot_refresh()

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                pass
        self._stop_gen += 1
        my_gen = self._stop_gen
        self._set_status("Stopping ...", "darkorange")
        self.root.after(1500, lambda: self._on_stopped(my_gen))

    def _on_stopped(self, gen: int):
        if gen != self._stop_gen:
            return   # a new run was started before this callback fired; ignore it
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._set_status("Stopped", "gray")
        self._rms_var.set("")
        self._btn_start.config(state="normal")
        self._btn_stop .config(state="disabled")
        if self._plot_job:
            self.root.after_cancel(self._plot_job)
            self._plot_job = None

    # ── Stdout draining (background thread) ──────────────────────────────────

    def _drain_stdout(self):
        for line in self.proc.stdout:
            self._q.put(line)
            # Extract log file path from lock.py startup output
            if self.log_path is None and "Lock log" in line and ":" in line:
                candidate = line.split(":", 1)[1].strip()
                if os.path.isfile(candidate):
                    self.log_path = candidate
        self._q.put(None)   # sentinel: process ended

    def _schedule_console_poll(self):
        self._drain_queue()
        self.root.after(self.CONSOLE_POLL_MS, self._schedule_console_poll)

    def _drain_queue(self):
        try:
            while True:
                line = self._q.get_nowait()
                if line is None:
                    self.root.after(300, self._on_stopped)
                    return
                self._log_console(line)
        except queue.Empty:
            pass

    def _log_console(self, text: str):
        self._console.config(state="normal")
        self._console.insert("end", text)
        self._console.see("end")
        # Keep last 500 lines to avoid unbounded growth
        nlines = int(self._console.index("end-1c").split(".")[0])
        if nlines > 500:
            self._console.delete("1.0", f"{nlines - 500}.0")
        self._console.config(state="disabled")

    # ── Live plot ────────────────────────────────────────────────────────────

    def _schedule_plot_refresh(self):
        self._refresh_plot()
        self._plot_job = self.root.after(self.PLOT_REFRESH_MS, self._schedule_plot_refresh)

    def _refresh_plot(self):
        # Fallback: find most recent lock CSV if subprocess hasn't announced it yet.
        # Only pick files created after this session started to avoid loading old logs.
        if self.log_path is None:
            candidates = sorted(
                glob.glob(str(SCRIPT_DIR / "logs" / "**" / "lock_*.csv"), recursive=True))
            if candidates:
                if self._start_time is not None:
                    candidates = [c for c in candidates
                                  if os.path.getmtime(c) >= self._start_time - 10]
                if candidates:
                    self.log_path = candidates[-1]

        if not self.log_path or not os.path.isfile(self.log_path):
            return

        try:
            df = pd.read_csv(self.log_path)
        except Exception:
            return
        if len(df) < 2:
            return

        t_all = df["time_s"].to_numpy()
        f_all = df["frequency_THz"].to_numpy()
        e_all = df["error_MHz"].to_numpy()
        V_all = df["output_V"].to_numpy()
        s_all = df["saturated"].to_numpy().astype(bool)
        n_total = len(df)

        # ─── Apply time-window filter ─────────────────────────────────────────
        window_map = {
            "1 min":  60,
            "15 min": 900,
            "30 min": 1800,
            "1 hr":   3600,
            "3 hr":   10800,
            "6 hr":   21600,
            "All":    None,
        }
        win_s = window_map.get(self._window_var.get())
        if win_s is not None and len(t_all) > 0:
            cutoff = t_all[-1] - win_s
            mask = t_all >= cutoff
            t, f, e, V, sat = t_all[mask], f_all[mask], e_all[mask], V_all[mask], s_all[mask]
        else:
            t, f, e, V, sat = t_all, f_all, e_all, V_all, s_all

        if len(t) < 2:
            return

        f_dev = (f - f.mean()) * 1e6
        rms = float(np.std(e))

        win_label = self._window_var.get()
        self._rms_var.set(
            f"RMS error: {rms:.3f} MHz   ({win_label}, n={len(t)}/{n_total})")

        # Simple unlock detector: mean of last 5 error samples > 8 MHz
        if len(e) >= 5 and abs(float(e[-5:].mean())) > 8.0:
            self._set_status("Lock lost?", "red")

        # ─── Frequency deviation ──────────────────────────────────────────────
        ax = self._ax_freq
        ax.cla()
        ax.plot(t, f_dev, color="steelblue", lw=0.8, rasterized=True)
        ax.axhline(0, color="k", lw=0.6, ls="--")
        ax.set_ylabel("Freq. dev.\n(MHz)", fontsize=8)
        ax.set_title(f"Frequency deviation from mean   σ = {f_dev.std():.2f} MHz",
                     fontsize=9, loc="left")
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3)

        # ─── Error signal ─────────────────────────────────────────────────────
        ax = self._ax_err
        ax.cla()
        ax.plot(t, e, color="tomato", lw=0.8, rasterized=True)
        ax.axhline(0, color="k", lw=0.6, ls="--")
        ax.set_ylabel("Error\n(MHz)", fontsize=8)
        ax.set_title(f"Error signal   RMS = {rms:.3f} MHz", fontsize=9, loc="left")
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3)

        # ─── Piezo voltage ────────────────────────────────────────────────────
        ax = self._ax_piezo
        ax.cla()
        ax.plot(t, V, color="seagreen", lw=0.8, rasterized=True)
        if sat.any():
            ax.scatter(t[sat], V[sat], color="red", s=14, zorder=5, label="saturated")
            ax.legend(fontsize=7)
        ax.set_ylabel("Piezo (V)", fontsize=8)
        ax.set_xlabel("Time (s)", fontsize=8)
        ax.set_title("Piezo correction voltage", fontsize=9, loc="left")
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3)

        self._fig.suptitle(os.path.basename(self.log_path), fontsize=9, color="gray")
        self._canvas.draw_idle()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str = "gray"):
        self._status_var.set(text)
        self._status_lbl.config(foreground=color)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app  = LockGUI(root)

    def _on_close():
        if app.proc and app.proc.poll() is None:
            app._log_console("\n[GUI closed — lock subprocess still running. "
                             "Stop it manually with Ctrl+C in its terminal.]\n")
            try:
                app.proc.send_signal(signal.SIGINT)
                app.proc.wait(timeout=3)
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
