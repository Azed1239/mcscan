#!/usr/bin/env python3
"""
mcscan GUI - a dark, rounded desktop front end for the random Minecraft server scanner.

Runs the async scanner on a background thread and polls it from the Tk main loop,
so the interface stays responsive while thousands of probes are in flight.
"""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import customtkinter as ctk

from mcscan import (__version__, Scanner, requirements, version_family, version_tuple,
                    version_matches, family_sort_key, loader_of,
                    KNOWN_VERSIONS)

# ------------------------------------------------------------------- appearance

BG        = "#0d1117"
CARD      = "#161b22"
CARD_ALT  = "#1b232d"
BORDER    = "#26303c"
TEXT      = "#e6edf3"
MUTED     = "#7d8894"
ACCENT    = "#4ade80"
ACCENT_HI = "#3bc46b"
ACCENT_DK = "#14532d"
DANGER    = "#f47067"
DANGER_HI = "#d95a51"
WARN      = "#f0b429"

ANY_VERSION = "Any version"
ANY_LOADER  = "Any loader"
NO_LOADER   = "No mods (vanilla / plugins)"
ANY_MODDED  = "Any modded"
LOADER_CHOICES = [ANY_LOADER, NO_LOADER, ANY_MODDED,
                  "Forge", "NeoForge", "Fabric", "Quilt"]

UI     = "Segoe UI"
MONO   = "Consolas"
MAX_ROWS = 300


def app_dir():
    """Where results live: next to the exe when frozen, next to the source otherwise."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource(name):
    """Bundled read-only files live in the PyInstaller temp dir when frozen."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def human(n):
    return format(int(n), ",")


# ----------------------------------------------------------------------- widgets

class StatCard(ctk.CTkFrame):
    """A rounded tile showing one big number with a caption under it."""

    def __init__(self, master, caption, value="0", accent=TEXT):
        super().__init__(master, corner_radius=14, fg_color=CARD,
                         border_width=1, border_color=BORDER)
        self.value = ctk.CTkLabel(self, text=value, text_color=accent,
                                  font=(UI, 26, "bold"), anchor="w")
        self.value.pack(fill="x", padx=16, pady=(13, 0))
        ctk.CTkLabel(self, text=caption.upper(), text_color=MUTED,
                     font=(UI, 10, "bold"), anchor="w").pack(fill="x", padx=16, pady=(0, 12))

    def set(self, text):
        self.value.configure(text=text)


class ResultRow(ctk.CTkFrame):
    """One found server."""

    def __init__(self, master, hit, on_copy):
        super().__init__(master, corner_radius=12, fg_color=CARD_ALT,
                         border_width=1, border_color=BORDER)
        self.hit = hit
        self.on_copy = on_copy
        self.grid_columnconfigure(0, weight=1)
        # Fixed widths so the columns line up down the whole list.
        self.grid_columnconfigure(1, minsize=104)
        self.grid_columnconfigure(2, minsize=124)
        self.grid_columnconfigure(3, minsize=60)

        addr = "%s:%d" % (hit["ip"], hit["port"])
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=10)
        ctk.CTkLabel(left, text=addr, text_color=TEXT, font=(MONO, 14, "bold"),
                     anchor="w").pack(fill="x")
        motd = hit.get("motd") or "(no message of the day)"
        ctk.CTkLabel(left, text=motd[:78], text_color=MUTED, font=(UI, 11),
                     anchor="w").pack(fill="x", pady=(2, 0))

        need, level = requirements(hit)
        colour = {"need": WARN, "maybe": TEXT, "ok": ACCENT,
                  "unknown": MUTED}.get(level, MUTED)
        prefix = {"need": "!  ", "maybe": "~  ", "ok": "+  ",
                  "unknown": "?  "}.get(level, "")
        ctk.CTkLabel(left, text=prefix + need, text_color=colour, font=(UI, 11, "bold"),
                     anchor="w").pack(fill="x", pady=(3, 0))

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=0, column=1, padx=6)
        online, mx = hit.get("online"), hit.get("max")
        players = "%s / %s" % (online if online is not None else "?",
                               mx if mx is not None else "?")
        colour = ACCENT if isinstance(online, int) and online > 0 else MUTED
        ctk.CTkLabel(mid, text=players, text_color=colour,
                     font=(UI, 14, "bold")).pack()
        ctk.CTkLabel(mid, text="PLAYERS", text_color=MUTED, font=(UI, 9, "bold")).pack()

        ver = ctk.CTkLabel(self, text=" %s " % str(hit.get("version", "?"))[:20],
                           text_color=ACCENT, fg_color=ACCENT_DK, corner_radius=8,
                           font=(UI, 11, "bold"), height=24)
        ver.grid(row=0, column=2, padx=6)

        lat = hit.get("latency_ms")
        ctk.CTkLabel(self, text=("%dms" % lat) if lat else "-", text_color=MUTED,
                     font=(UI, 11), width=52).grid(row=0, column=3, padx=(0, 4))

        self.copy_btn = ctk.CTkButton(self, text="Copy", width=62, height=28,
                                      corner_radius=9, fg_color=CARD, hover_color=BORDER,
                                      text_color=TEXT, font=(UI, 11, "bold"),
                                      command=self._copy)
        self.copy_btn.grid(row=0, column=4, padx=(0, 12))

    def _copy(self):
        self.on_copy("%s:%d" % (self.hit["ip"], self.hit["port"]))
        self.copy_btn.configure(text="Copied", text_color=ACCENT)
        self.after(1200, lambda: self.copy_btn.configure(text="Copy", text_color=TEXT))



# --------------------------------------------------------------------- main app

class App(ctk.CTk):
    PRESETS = {"Gentle": (400, 2.0), "Balanced": (1000, 2.0),
               "Fast": (2000, 2.0), "Max": (4000, 2.0)}

    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("mcscan %s - Minecraft server finder" % __version__)
        self.geometry("1080x720")
        self.minsize(960, 620)
        try:
            self.iconbitmap(resource("mcscan.ico"))
        except Exception:
            pass

        self.out_path = os.path.join(app_dir(), "servers.jsonl")
        self.queue = queue.Queue()
        self.scanner = None
        self.thread = None
        self.rows = []
        self.seen_versions = set()
        self._bulk = False
        self.last_tried = 0
        self.last_time = time.time()
        self.started_at = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_sidebar()
        self._build_main()
        self._load_previous()
        self._sync_rate_label()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)

    # ------------------------------------------------------------------ layout

    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=64)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 6))
        ctk.CTkLabel(bar, text="mcscan", text_color=TEXT,
                     font=(UI, 22, "bold")).pack(side="left")
        ctk.CTkLabel(bar, text="  finds Minecraft servers on random public addresses",
                     text_color=MUTED, font=(UI, 12)).pack(side="left", pady=(4, 0))

        self.status_pill = ctk.CTkLabel(bar, text="  IDLE  ", text_color=MUTED,
                                        fg_color=CARD, corner_radius=10,
                                        font=(UI, 11, "bold"), height=26)
        self.status_pill.pack(side="right")

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD,
                            border_width=1, border_color=BORDER, width=300)
        side.grid(row=1, column=0, sticky="nsw", padx=(18, 9), pady=(6, 18))
        side.grid_propagate(False)

        def heading(text, pady=(18, 6)):
            ctk.CTkLabel(side, text=text.upper(), text_color=MUTED,
                         font=(UI, 10, "bold"), anchor="w").pack(
                fill="x", padx=18, pady=pady)

        heading("speed preset", (18, 8))
        self.preset = ctk.CTkSegmentedButton(
            side, values=list(self.PRESETS), corner_radius=10,
            fg_color=BG, selected_color=ACCENT_DK, selected_hover_color=ACCENT_DK,
            unselected_color=BG, unselected_hover_color=BORDER,
            text_color=TEXT, font=(UI, 11, "bold"), command=self._apply_preset)
        self.preset.pack(fill="x", padx=16)
        self.preset.set("Gentle")

        heading("concurrency")
        self.conc = ctk.CTkSlider(side, from_=100, to=5000, number_of_steps=49,
                                  button_color=ACCENT, button_hover_color=ACCENT_HI,
                                  progress_color=ACCENT, fg_color=BG,
                                  command=self._on_slider)
        self.conc.set(400)
        self.conc.pack(fill="x", padx=16)
        self.conc_value = ctk.CTkLabel(side, text="400 workers", text_color=MUTED,
                                       font=(UI, 11), anchor="w")
        self.conc_value.pack(fill="x", padx=18, pady=(4, 0))

        heading("timeout")
        self.timeout = ctk.CTkSlider(side, from_=1.0, to=5.0, number_of_steps=16,
                                     button_color=ACCENT, button_hover_color=ACCENT_HI,
                                     progress_color=ACCENT, fg_color=BG,
                                     command=self._on_slider)
        self.timeout.set(2.0)
        self.timeout.pack(fill="x", padx=16)
        self.timeout_value = ctk.CTkLabel(side, text="2.0 s per probe", text_color=MUTED,
                                          font=(UI, 11), anchor="w")
        self.timeout_value.pack(fill="x", padx=18, pady=(4, 0))

        self.rate_label = ctk.CTkLabel(side, text="", text_color=ACCENT,
                                       font=(UI, 12, "bold"), anchor="w")
        self.rate_label.pack(fill="x", padx=18, pady=(14, 0))
        self.warn_label = ctk.CTkLabel(side, text="", text_color=WARN, font=(UI, 10),
                                       anchor="w", justify="left", wraplength=262)
        self.warn_label.pack(fill="x", padx=18, pady=(2, 0))

        heading("ports")
        self.ports = ctk.CTkEntry(side, corner_radius=10, fg_color=BG,
                                  border_color=BORDER, text_color=TEXT,
                                  font=(MONO, 12), height=34)
        self.ports.insert(0, "25565")
        self.ports.pack(fill="x", padx=16)

        self.legacy = ctk.CTkSwitch(side, text="Also catch pre-1.7 servers",
                                    text_color=MUTED, font=(UI, 11),
                                    progress_color=ACCENT, button_color=TEXT)
        self.legacy.pack(fill="x", padx=18, pady=(18, 0))

        ctk.CTkLabel(side, text="Sends only the status ping your client sends on the\n"
                               "multiplayer screen. It never tries to log in.",
                     text_color="#5a636e", font=(UI, 10), anchor="w",
                     justify="left").pack(fill="x", padx=18, pady=(22, 0))

        self.start_btn = ctk.CTkButton(side, text="START SCAN", height=46,
                                       corner_radius=12, fg_color=ACCENT,
                                       hover_color=ACCENT_HI, text_color="#08130c",
                                       font=(UI, 14, "bold"), command=self._toggle)
        self.start_btn.pack(fill="x", padx=16, pady=(20, 16), side="bottom")

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=1, sticky="nsew", padx=(9, 18), pady=(6, 18))
        main.grid_columnconfigure((0, 1, 2, 3), weight=1)
        main.grid_rowconfigure(1, weight=1)

        stats = ctk.CTkFrame(main, fg_color="transparent")
        stats.grid(row=0, column=0, columnspan=4, sticky="ew")
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.card_found = StatCard(stats, "servers found", "0", ACCENT)
        self.card_probed = StatCard(stats, "addresses probed")
        self.card_rate = StatCard(stats, "addresses / sec")
        self.card_open = StatCard(stats, "ports open")
        for i, c in enumerate((self.card_found, self.card_probed,
                               self.card_rate, self.card_open)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))

        panel = ctk.CTkFrame(main, corner_radius=16, fg_color=CARD,
                             border_width=1, border_color=BORDER)
        panel.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(14, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        ctk.CTkLabel(head, text="RESULTS", text_color=MUTED,
                     font=(UI, 11, "bold")).pack(side="left")
        self.filter_count = ctk.CTkLabel(head, text="", text_color=MUTED, font=(UI, 11))
        self.filter_count.pack(side="left", padx=12)


        ctk.CTkButton(head, text="Open folder", width=100, height=28, corner_radius=9,
                      fg_color=CARD_ALT, hover_color=BORDER, text_color=TEXT,
                      font=(UI, 11), command=self._open_folder).pack(side="right")
        self.elapsed_label = ctk.CTkLabel(head, text="", text_color=MUTED, font=(UI, 11))
        self.elapsed_label.pack(side="right", padx=12)

        self._build_filters(panel)

        self.results = ctk.CTkScrollableFrame(panel, fg_color="transparent",
                                              scrollbar_button_color=BORDER,
                                              scrollbar_button_hover_color=MUTED)
        self.results.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 6))
        self.results.grid_columnconfigure(0, weight=1)

        self.empty_default = ("\n\nNothing yet.\n\nAbout 1 in 150,000 public addresses "
                              "runs a\nMinecraft server, so give it a few minutes.")
        self.empty = ctk.CTkLabel(self.results, text=self.empty_default,
                                  text_color=MUTED, font=(UI, 12), justify="center")
        self.empty.pack(pady=40)

        self.footer = ctk.CTkLabel(panel, text="saving to  %s" % self.out_path,
                                   text_color=MUTED, font=(UI, 10), anchor="w")
        self.footer.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))

    def _build_filters(self, panel):
        """Filters hide rows; they never change what gets scanned or saved, so
        loosening one brings servers straight back without rescanning."""
        bar = ctk.CTkFrame(panel, corner_radius=12, fg_color=BG,
                           border_width=1, border_color=BORDER)
        bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 10))

        ctk.CTkLabel(bar, text="FILTER", text_color=MUTED,
                     font=(UI, 10, "bold")).pack(side="left", padx=(14, 10), pady=10)

        def menu(values, command, width=150):
            widget = ctk.CTkOptionMenu(
                bar, values=values, width=width, height=30, corner_radius=9,
                fg_color=CARD_ALT, button_color=CARD_ALT, button_hover_color=BORDER,
                text_color=TEXT, font=(UI, 11), dropdown_fg_color=CARD_ALT,
                dropdown_text_color=TEXT, dropdown_hover_color=BORDER,
                command=command)
            widget.pack(side="left", padx=(0, 8))
            return widget

        # Editable: pick an exact version from the list, or just type one.
        self.version_menu = ctk.CTkComboBox(
            bar, values=[ANY_VERSION], width=168, height=30, corner_radius=9,
            fg_color=CARD_ALT, border_color=BORDER, button_color=CARD_ALT,
            button_hover_color=BORDER, text_color=TEXT, font=(UI, 11),
            dropdown_fg_color=CARD_ALT, dropdown_text_color=TEXT,
            dropdown_hover_color=BORDER, dropdown_font=(MONO, 11),
            command=lambda _v: self._apply_filters())
        self.version_menu.set(ANY_VERSION)
        self.version_menu.pack(side="left", padx=(0, 8))
        for event in ("<Return>", "<KP_Enter>", "<FocusOut>", "<KeyRelease>"):
            self.version_menu.bind(event, lambda _e: self._apply_filters())
        self._refresh_versions()

        self.loader_menu = menu(LOADER_CHOICES, lambda _v: self._apply_filters(), 190)

        self.players_switch = ctk.CTkSwitch(
            bar, text="has players online", text_color=TEXT, font=(UI, 11),
            progress_color=ACCENT, button_color=TEXT,
            command=self._apply_filters)
        self.players_switch.pack(side="left", padx=(6, 10))

        self.reset_btn = ctk.CTkButton(bar, text="Reset", width=64, height=28,
                                       corner_radius=9, fg_color=CARD_ALT,
                                       hover_color=BORDER, text_color=TEXT,
                                       font=(UI, 11), command=self._reset_filters)
        self.reset_btn.pack(side="right", padx=(0, 12))

    # ----------------------------------------------------------------- filtering

    def _note_version(self, hit):
        """Remember exactly what turned up so the picker covers it."""
        exact = hit.get("version")
        before = len(self.seen_versions)
        if version_tuple(exact):
            self.seen_versions.add(".".join(str(n) for n in version_tuple(exact)))
        if len(self.seen_versions) != before and not self._bulk:
            self._refresh_versions()

    def _refresh_versions(self):
        """Known versions plus everything found, grouped under their family.

        Families come first in each group so '1.21.x' is one click away, with the
        individual releases listed under it.
        """
        versions = set(KNOWN_VERSIONS) | self.seen_versions
        families = {}
        for version in versions:
            families.setdefault(version_family(version), []).append(version)

        values = [ANY_VERSION]
        for family in sorted((f for f in families if f), key=family_sort_key, reverse=True):
            values.append(family)
            for version in sorted(families[family], key=family_sort_key, reverse=True):
                values.append("   " + version)

        current = self.version_menu.get()
        self.version_menu.configure(values=values)
        self.version_menu.set(current)

    def _matches(self, hit):
        wanted = self.version_menu.get().strip()
        if wanted and wanted != ANY_VERSION:
            if not version_matches(wanted, hit.get("version")):
                return False

        choice = self.loader_menu.get()
        if choice != ANY_LOADER:
            loader = loader_of(hit)
            if choice == NO_LOADER:
                if loader is not None:
                    return False
            elif choice == ANY_MODDED:
                if loader is None:
                    return False
            elif (loader or "").lower() != choice.lower():
                return False

        if self.players_switch.get():
            online = hit.get("online")
            if not isinstance(online, int) or online < 1:
                return False
        return True

    def _apply_filters(self):
        shown = 0
        for row in self.rows:
            if self._matches(row.hit):
                row.pack_forget()          # repack in order so newest stays on top
                row.pack(fill="x", padx=4, pady=4)
                shown += 1
            else:
                row.pack_forget()
        total = len(self.rows)
        if shown == total:
            self.filter_count.configure(text="%d shown" % total, text_color=MUTED)
        else:
            self.filter_count.configure(text="%d of %d shown" % (shown, total),
                                        text_color=WARN)
        if total and not shown:
            self.empty.configure(text="\n\nNo results match these filters.\n\n"
                                      "They are only hidden, not lost -\nReset brings "
                                      "them back.")
            self.empty.pack(pady=40)
        elif shown:
            self.empty.pack_forget()
        else:
            self.empty.configure(text=self.empty_default)
            self.empty.pack(pady=40)
        return shown

    def _reset_filters(self):
        self.version_menu.set(ANY_VERSION)
        self.loader_menu.set(ANY_LOADER)
        self.players_switch.deselect()
        self._apply_filters()

    # ----------------------------------------------------------------- controls

    def _apply_preset(self, name):
        conc, timeout = self.PRESETS[name]
        self.conc.set(conc)
        self.timeout.set(timeout)
        self._sync_rate_label()

    def _on_slider(self, _value):
        try:
            self.preset.set("")
        except Exception:
            pass
        self._sync_rate_label()

    def _sync_rate_label(self):
        conc = int(self.conc.get())
        timeout = round(self.timeout.get(), 1)
        rate = conc / max(timeout, 0.1)
        self.conc_value.configure(text="%d workers" % conc)
        self.timeout_value.configure(text="%.1f s per probe" % timeout)
        minutes = 150000 / rate / 60
        self.rate_label.configure(
            text="~%d addr/sec  -  a server every ~%s" % (
                rate, ("%.0f min" % minutes) if minutes >= 1 else "40 s"))
        if conc > 1200:
            self.warn_label.configure(
                text="High concurrency fills your router's connection table and can "
                     "make the rest of your network stutter. Drop to Gentle if so.")
        else:
            self.warn_label.configure(text="")

    def _toggle(self):
        if self.scanner and self.thread and self.thread.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        try:
            from mcscan import parse_ports
            ports = parse_ports(self.ports.get())
        except Exception:
            self.footer.configure(text="bad port list - try something like 25565",
                                  text_color=DANGER)
            return
        self.footer.configure(text="saving to  %s" % self.out_path, text_color=MUTED)

        args = SimpleNamespace(
            concurrency=int(self.conc.get()), timeout=round(self.timeout.get(), 1),
            ports=ports, out=self.out_path, overwrite=False, limit=0, duration=0,
            max_ips=0, protocol=767, legacy=bool(self.legacy.get()), seed=None)

        self.scanner = Scanner(args, on_hit=self.queue.put, quiet=True)
        # Servers already on screen are in the scanner's dedupe set via the file.
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started_at = time.time()
        self.last_tried, self.last_time = 0, time.time()
        for card in (self.card_found, self.card_probed, self.card_rate, self.card_open):
            card.set("0")
        self.thread.start()

        self.start_btn.configure(text="STOP SCAN", fg_color=DANGER,
                                 hover_color=DANGER_HI, text_color="#1a0a08")
        self.status_pill.configure(text="  SCANNING  ", text_color="#08130c",
                                   fg_color=ACCENT)
        self._set_controls("disabled")

    def _run(self):
        try:
            asyncio.run(self.scanner.run())
        except Exception as exc:
            self.queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))
        finally:
            self.queue.put(("done", None))

    def _stop(self):
        if self.scanner:
            self.scanner.abort = True
        self.start_btn.configure(text="STOPPING...", state="disabled")
        self.status_pill.configure(text="  STOPPING  ", text_color=BG, fg_color=WARN)

    def _finish(self):
        self.start_btn.configure(text="START SCAN", fg_color=ACCENT,
                                 hover_color=ACCENT_HI, text_color="#08130c",
                                 state="normal")
        self.status_pill.configure(text="  IDLE  ", text_color=MUTED, fg_color=CARD)
        self._set_controls("normal")

    def _set_controls(self, state):
        for w in (self.conc, self.timeout, self.ports, self.legacy, self.preset):
            try:
                w.configure(state=state)
            except Exception:
                pass

    # -------------------------------------------------------------- result feed

    def _add_row(self, hit, at_top=True):
        self._note_version(hit)
        row = ResultRow(self.results, hit, self._copy)
        if at_top:
            self.rows.insert(0, row)
        else:
            self.rows.append(row)
        while len(self.rows) > MAX_ROWS:      # drop the oldest so the list stays smooth
            self.rows.pop().destroy()
        # _apply_filters does the packing, so ordering and hiding live in one place.
        if not self._bulk:
            self._apply_filters()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)


    def _load_previous(self):
        if not os.path.exists(self.out_path):
            return
        try:
            with open(self.out_path, "r", encoding="utf-8") as fh:
                hits = [json.loads(line) for line in fh if line.strip()]
        except Exception:
            return
        self._bulk = True
        for hit in hits[-40:]:
            try:
                self._add_row(hit, at_top=True)
            except Exception:
                pass
        self._bulk = False
        self._refresh_versions()
        self._apply_filters()
        if hits:
            self.card_found.set(human(len(hits)))
            self.footer.configure(
                text="%s server%s already saved in  %s" % (
                    human(len(hits)), "" if len(hits) == 1 else "s", self.out_path))

    def _open_folder(self):
        """Reveal the results folder in the OS file manager (Win/Mac/Linux)."""
        folder = app_dir()
        try:
            if sys.platform == "win32":
                if os.path.exists(self.out_path):
                    subprocess.Popen(["explorer", "/select,",
                                      os.path.normpath(self.out_path)])
                else:
                    os.startfile(folder)                          # noqa: cross-platform guard above
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            pass

    # --------------------------------------------------------------- main loop

    def _poll(self):
        running = bool(self.thread and self.thread.is_alive())
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                kind, payload = item
                if kind == "done":
                    self._finish()
                elif kind == "error":
                    self.footer.configure(text="scan stopped: %s" % payload,
                                          text_color=DANGER)
            else:
                self._add_row(item)

        if self.scanner and running:
            now = time.time()
            tried = self.scanner.tried
            dt = now - self.last_time
            if dt >= 0.9:
                rate = (tried - self.last_tried) / dt
                self.card_rate.set(human(rate))
                self.last_tried, self.last_time = tried, now
            self.card_probed.set(human(tried))
            self.card_open.set(human(self.scanner.open_ports))
            self.card_found.set(human(self.scanner.hits))   # new this run
            elapsed = int(now - self.started_at)
            self.elapsed_label.configure(
                text="%d:%02d elapsed" % (elapsed // 60, elapsed % 60))
        elif not running:
            self.card_rate.set("0")

        self.after(200, self._poll)

    def _on_close(self):
        if self.scanner:
            self.scanner.abort = True
        self.destroy()


def main():
    ctk.set_appearance_mode("dark")
    App().mainloop()


if __name__ == "__main__":
    main()
