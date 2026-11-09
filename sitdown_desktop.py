#!/usr/bin/env python3
"""The Empire State BBS desktop app. Standard-library Tkinter GUI.

One window with a sidebar: Dashboard, Messages, Bulletin, Locals,
Connection, Radio commands, Settings and About. All styling comes from
empire_ui (the design system). Server, radio and message logic is
unchanged from the previous launcher.
"""
import json
import ipaddress
import sqlite3
import runpy
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, filedialog
from sitdown_monitor import ServerSupervisor
from radio_health import radio_health_label
from radio_diagnostics import available_serial_ports, check_serial_target, check_tcp_target
import zipfile
from desktop_session import DesktopStore
from empire_backup import BackupError, inspect_backup, restore_backup
from empire_nodedir import NodeDirectory, count_label
from desktop_views import open_posts, open_nodes, PostsPage, NodesPage, page_header
import empire_ui as ui

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA = Path.home() / ".empire-state-bbs"
CONFIG = DATA / "launcher.json"
APP_NAME = "The Empire State BBS"
APP_VERSION = "0.1.0 beta"
# Legacy colour map kept for callers/tests; screens use empire_ui tokens.
THEME = {"background": ui._DARK["bg"], "panel": ui._DARK["surface"],
         "primary": ui._DARK["accent"], "connected": ui._DARK["success"],
         "text": ui._DARK["text"], "highlight": ui._DARK["accent"]}
COMMANDS = [
    ("/empire", "Main menu"), ("/help", "Quick help"),
    ("/mail", "Check your mail"), ("/boards", "The Bulletin Board"),
    ("/fortune", "New York wisdom"), ("/tools", "Utilities"),
    ("/cancel", "Return to main menu"),
]
ATTITUDES = [("clean", "Clean"), ("spicy", "Spicy"), ("unfiltered", "Uncensored")]
ATTITUDE_HELP = {
    "clean": "Classic New York, family friendly. The default.",
    "spicy": "A little mouthy. Mild language.",
    "unfiltered": "Full New York attitude, including strong language.",
}
# Sidebar: key, label, NYC name, icon, shortcut.
NAV = [
    ("home", "Dashboard", "Empire HQ", "home", "1"),
    ("messages", "Messages", "The Grapevine", "messages", "2"),
    ("bulletin", "Bulletin", "The Bulletin", "bulletin", "3"),
    ("locals", "Locals", "Nodes heard", "locals", "4"),
    ("connection", "Connection", "The Empire Connection", "connection", "5"),
    ("commands", "Radio commands", "The Playbook", "commands", "6"),
]
NAV_FOOTER = [
    ("settings", "Settings", "Control Room", "settings", ","),
    ("about", "About", None, "about", None),
]
SPACE = ui.SPACE


class SitdownApp:
    def __init__(self, root):
        self.root = root
        self.store = DesktopStore(DATA / "desktop-state.db",
                                  (DATA if getattr(sys, "frozen", False) else ROOT) / "bulletins.db")
        self.proc = None
        self.supervisor = None
        self.restart_at = None
        self.retry_count = 0
        self.auto_restart = tk.BooleanVar(value=False)
        self.node_directory = self.open_node_directory()
        self.radio_node_count = None
        self.last_node_count = 0.0
        self.node_count = tk.StringVar(value=count_label(self.saved_node_count(), None))
        self.uptime = tk.StringVar(value='Uptime: 0s')
        self.demo = tk.BooleanVar(value=False)
        self.attitude = tk.StringVar(value="clean")
        self.connection = tk.StringVar(value="serial")
        self.address = tk.StringVar()
        self.community = tk.StringVar(value="The Empire State BBS")
        self.welcome = tk.StringVar(value="AYO, welcome to the Empire State. One state. One network.")
        self.status = tk.StringVar(value="Stopped")
        self.radio_health = tk.StringVar(value="Radio health: unverified")
        self.appearance = tk.StringVar(value="system")
        self.text_scale = tk.StringVar(value="default")
        self.last_serial_scan = 0.0
        self.last_serial_ports = None
        self.waiting_for_usb = False
        self.current_page = None
        self._probe = None
        self.root.title(APP_NAME)
        self.root.geometry("1280x840")
        self.root.minsize(1000, 680)
        self.load()
        self.theme = ui.Theme(root, self.appearance.get(), self.text_scale.get())
        self.colors = dict(THEME, highlight=self.theme["accent"])
        self._toast = ui.Toast(root, self.theme)
        self.status.trace_add("write", self.update_status_color)
        for var in (self.uptime, self.node_count, self.radio_health, self.connection,
                    self.address, self.demo, self.attitude):
            var.trace_add("write", lambda *_a: self.refresh_summary())
        self.connection.trace_add("write", lambda *_a: self.on_connection_mode())
        self.address.trace_add("write", lambda *_a: self.validate_address_live())
        self.build()
        self.install_menu()
        # Command shortcuts navigate only; they never transmit or start/stop the server.
        for key, *_rest, shortcut in NAV + NAV_FOOTER:
            if shortcut:
                sequence = "<Command-comma>" if shortcut == "," else f"<Command-Key-{shortcut}>"
                self.root.bind(sequence, lambda _event, k=key: self.open_section(k))
        self.root.bind("<Command-n>", lambda _e: self.new_item_shortcut())
        self.root.bind("<Command-f>", lambda _e: self.find_shortcut())
        self.root.bind_all("<MouseWheel>", self.on_wheel, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.root.createcommand("tk::mac::Quit", self.close)
            self.root.createcommand("tk::mac::ShowPreferences", self.settings)
        except tk.TclError:
            pass
        self.root.after(1000, self.poll)

    # ----------------------------------------------------------- settings
    def load(self):
        if CONFIG.exists():
            try:
                data = json.loads(CONFIG.read_text(encoding="utf-8"))
                self.connection.set(data.get("connection", "serial"))
                self.address.set(data.get("address", ""))
                self.community.set(data.get("community", "The Empire State BBS"))
                self.welcome.set(data.get("welcome", self.welcome.get()))
                self.attitude.set(data.get("attitude", "clean") if data.get("attitude") in ("clean", "spicy", "unfiltered") else "clean")
                if data.get("appearance") in dict(ui.APPEARANCES):
                    self.appearance.set(data["appearance"])
                if data.get("text_scale") in ui.TEXT_SCALES:
                    self.text_scale.set(data["text_scale"])
            except (ValueError, OSError, AttributeError):
                pass

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({
            "connection": self.connection.get(), "address": self.address.get(),
            "community": self.community.get(), "welcome": self.welcome.get(),
            "attitude": self.attitude.get(), "appearance": self.appearance.get(),
            "text_scale": self.text_scale.get(),
        }, indent=2), encoding="utf-8")
        self.log("Settings saved.")

    # -------------------------------------------------------------- shell
    def build(self):
        t = self.theme
        self.root.configure(bg=t["bg"])
        self.shell = ui.frame(self.root, t)
        self.shell.pack(fill="both", expand=True)
        self.build_sidebar()
        tk.Frame(self.shell, bg=t["border"], width=1).pack(side="left", fill="y")
        self.content = ui.frame(self.shell, t)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.pages = {}
        self.page_objects = {}
        builders = {
            "home": self.build_dashboard, "connection": self.build_connection,
            "commands": self.build_commands, "settings": self.build_settings,
            "about": self.build_about,
        }
        for key, *_rest in NAV + NAV_FOOTER:
            if key == "messages":
                page = PostsPage(self, self.content, "mail")
            elif key == "bulletin":
                page = PostsPage(self, self.content, "bulletin")
            elif key == "locals":
                page = NodesPage(self, self.content)
            else:
                page = None
            if page is not None:
                self.page_objects[key] = page
                frame = page.frame
            else:
                frame = ui.frame(self.content, t)
                builders[key](frame)
            frame.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = frame
        self.log(self.attitude_message())
        self.log("Demo mode is clearly labeled and never connects to a radio.")
        self.log("Shortcuts: ⌘1 Dashboard · ⌘2 Messages · ⌘3 Bulletin · ⌘4 Locals · "
                 "⌘5 Connection · ⌘6 Radio commands · ⌘, Settings")
        self.update_status_color()
        self.on_connection_mode()
        self.refresh_summary()
        self.navigate("home")

    def build_sidebar(self):
        t = self.theme
        side = ui.frame(self.shell, t, "surface", width=232)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = ui.frame(side, t, "surface")
        brand.pack(fill="x", padx=SPACE["lg"], pady=(SPACE["xl"], SPACE["lg"]))
        mark = tk.Canvas(brand, width=34, height=34, bg=t["surface"], highlightthickness=0)
        mark.pack(side="left")
        self.draw_mark(mark)
        words = ui.frame(brand, t, "surface")
        words.pack(side="left", padx=(SPACE["sm"], 0))
        ui.text(words, t, "Empire State", "body_strong", bg="surface").pack(anchor="w")
        ui.text(words, t, "BBS · New York mesh", "caption", "text_muted", "surface").pack(anchor="w")
        self.nav_items = {}
        nav = ui.frame(side, t, "surface")
        nav.pack(fill="x", padx=SPACE["sm"])
        for item in NAV:
            self.nav_row(nav, *item)
        footer = ui.frame(side, t, "surface")
        footer.pack(side="bottom", fill="x", padx=SPACE["sm"], pady=(0, SPACE["md"]))
        status_box = ui.Card(footer, t, padding="md", bg="surface_alt")
        status_box.pack(fill="x", padx=SPACE["xs"], pady=(0, SPACE["md"]))
        ui.text(status_box.body, t, "SERVER", "overline", "text_faint", "surface_alt").pack(anchor="w")
        self.side_status = ui.StatusPill(status_box.body, t, "Stopped", "neutral", bg="surface_alt")
        self.side_status.pack(anchor="w", pady=(SPACE["xxs"], 0))
        for widget in (status_box, status_box.body):
            widget.bind("<Button-1>", lambda _e: self.navigate("connection"))
        for item in NAV_FOOTER:
            self.nav_row(footer, *item)

    def nav_row(self, parent, key, label, nyc, icon, shortcut):
        t = self.theme
        row = tk.Frame(parent, bg=t["surface"], cursor="hand2", takefocus=True,
                       highlightthickness=2, highlightbackground=t["surface"],
                       highlightcolor=t["focus"])
        row.pack(fill="x", pady=1)
        glyph = tk.Label(row, text=ui.ICON[icon], width=2, font=t.font("headline"),
                         bg=t["surface"], fg=t["text_muted"])
        glyph.pack(side="left", padx=(SPACE["sm"], SPACE["xs"]), pady=SPACE["sm"])
        name = tk.Label(row, text=label, font=t.font("body"), bg=t["surface"],
                        fg=t["text"], anchor="w")
        name.pack(side="left", fill="x", expand=True)
        hint = tk.Label(row, text=("⌘" + shortcut) if shortcut else "", font=t.font("caption"),
                        bg=t["surface"], fg=t["text_faint"])
        hint.pack(side="right", padx=SPACE["sm"])
        parts = (row, glyph, name, hint)
        for widget in parts:
            widget.bind("<Button-1>", lambda _e, k=key: self.open_section(k))
            widget.bind("<Enter>", lambda _e, k=key: self.paint_nav(k, hover=True))
            widget.bind("<Leave>", lambda _e, k=key: self.paint_nav(k))
        row.bind("<Return>", lambda _e, k=key: self.open_section(k))
        row.bind("<space>", lambda _e, k=key: self.open_section(k))
        row.bind("<Up>", lambda _e: self.step_nav(-1))
        row.bind("<Down>", lambda _e: self.step_nav(1))
        row.bind("<FocusIn>", lambda _e: row.configure(highlightbackground=t["focus"]))
        row.bind("<FocusOut>", lambda _e, k=key: self.paint_nav(k))
        if nyc:
            ui.Tooltip(row, t, nyc)
        self.nav_items[key] = parts

    def paint_nav(self, key, hover=False):
        t = self.theme
        parts = self.nav_items.get(key)
        if not parts or not parts[0].winfo_exists():
            return
        active = key == self.current_page
        bg = t["accent_soft"] if active else (t["surface_alt"] if hover else t["surface"])
        for widget in parts:
            widget.configure(bg=bg)
        parts[1].configure(fg=t["accent"] if active else t["text_muted"])
        parts[2].configure(fg=t["text"], font=t.font("body_strong" if active else "body"))
        if self.root.focus_get() is not parts[0]:
            parts[0].configure(highlightbackground=bg)

    def step_nav(self, delta):
        keys = [k for k, *_r in NAV + NAV_FOOTER]
        index = keys.index(self.current_page) if self.current_page in keys else 0
        target = keys[(index + delta) % len(keys)]
        self.navigate(target)
        self.nav_items[target][0].focus_set()
        return "break"

    def open_section(self, key):
        """Sidebar/shortcut entry: Connection also focuses the address field."""
        if key == "connection":
            self.show_radio()
        else:
            self.navigate(key)

    def navigate(self, key):
        pages = getattr(self, "pages", None)
        if not pages or key not in pages:
            return
        previous = self.current_page
        self.current_page = key
        pages[key].tkraise()
        for name in self.nav_items:
            self.paint_nav(name)
        page = self.page_objects.get(key)
        if page is not None and previous != key:
            page.activate()
        if key == "home":
            self.refresh_summary()

    def new_item_shortcut(self):
        page = self.page_objects.get(self.current_page)
        if isinstance(page, PostsPage):
            page.compose()

    def find_shortcut(self):
        if self.current_page == "locals":
            self.page_objects["locals"].search_entry.focus_set()

    def install_menu(self):
        menubar = tk.Menu(self.root)
        app_menu = tk.Menu(menubar, name="apple", tearoff=0)
        app_menu.add_command(label="About " + APP_NAME, command=lambda: self.navigate("about"))
        app_menu.add_separator()
        menubar.add_cascade(menu=app_menu)
        view = tk.Menu(menubar, tearoff=0)
        for key, label, _nyc, _icon, shortcut in NAV:
            view.add_command(label=label, accelerator=f"Command-{shortcut}",
                             command=lambda k=key: self.navigate(k))
        menubar.add_cascade(label="View", menu=view)
        server_box = tk.Menu(menubar, tearoff=0)
        server_box.add_command(label="Start BBS server", command=self.start)
        server_box.add_command(label="Stop BBS server", command=self.stop)
        server_box.add_separator()
        server_box.add_command(label="Check radio connection", command=self.check_radio_target)
        server_box.add_command(label="Find USB radios", command=self.list_ports)
        server_box.add_separator()
        server_box.add_command(label="Back up data…", command=self.backup)
        server_box.add_command(label="Restore from backup…", command=self.restore)
        menubar.add_cascade(label="Server", menu=server_box)
        window_menu = tk.Menu(menubar, name="window", tearoff=0)
        menubar.add_cascade(label="Window", menu=window_menu)
        help_menu = tk.Menu(menubar, name="help", tearoff=0)
        help_menu.add_command(label="Radio commands", command=lambda: self.navigate("commands"))
        help_menu.add_command(label="Connection troubleshooting", command=self.show_radio)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)
        self._menubar = menubar

    def rebuild(self):
        """Apply a new appearance or text size without touching the server."""
        log_text = self.output.get("1.0", "end-1c") if hasattr(self, "output") else ""
        page = self.current_page or "home"
        for child in self.root.winfo_children():
            if isinstance(child, tk.Menu):
                continue
            child.destroy()
        self.theme.configure(self.appearance.get(), self.text_scale.get())
        self.colors = dict(THEME, highlight=self.theme["accent"])
        self._toast = ui.Toast(self.root, self.theme)
        self.current_page = None
        self.build()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", log_text + ("\n" if log_text else ""))
        self.output.see("end")
        self.output.configure(state="disabled")
        self.navigate(page)

    def toast(self, message, tone="success"):
        if getattr(self, "_toast", None):
            self._toast.show(message, tone)

    def scroll_page(self, parent):
        """A vertically scrollable page body that fills the window width."""
        t = self.theme
        canvas = tk.Canvas(parent, bg=t["bg"], highlightthickness=0)
        bar = ui.ttk.Scrollbar(parent, orient="vertical", command=canvas.yview,
                               style="Empire.Vertical.TScrollbar")
        inner = ui.frame(canvas, t)
        window = canvas.create_window(0, 0, window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)

        def sync(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            needed = inner.winfo_reqheight() > canvas.winfo_height()
            if needed and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y")
            elif not needed and bar.winfo_ismapped():
                bar.pack_forget()
                canvas.yview_moveto(0)
        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(window, width=e.width), sync()))

        if not hasattr(self, "_scrollers"):
            self._scrollers = {}
        self._scrollers[str(parent)] = (canvas, inner)
        canvas.pack(side="left", fill="both", expand=True)
        return inner

    def on_wheel(self, event):
        """Scroll the visible page, unless the pointer is over a scrollable widget."""
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Listbox)) or isinstance(widget, ui.ttk.Treeview):
            return
        page = getattr(self, "pages", {}).get(self.current_page)
        entry = getattr(self, "_scrollers", {}).get(str(page)) if page is not None else None
        if not entry:
            return
        canvas, inner = entry
        try:
            if inner.winfo_reqheight() <= canvas.winfo_height():
                return
        except tk.TclError:
            return
        delta = event.delta if sys.platform == "darwin" else (event.delta // 120 or (1 if event.delta > 0 else -1))
        canvas.yview_scroll(-delta, "units")

    # ---------------------------------------------------------- dashboard
    def build_dashboard(self, page):
        t = self.theme
        self.header_canvas = tk.Canvas(page, height=132, bg=t["sky_top"], highlightthickness=0)
        self.header_canvas.pack(fill="x")
        self.header_canvas.bind("<Configure>", self.draw_skyline_header)
        body = ui.frame(page, t)
        body.pack(fill="both", expand=True, padx=SPACE["xl"], pady=SPACE["xl"])
        body.columnconfigure(0, weight=3, uniform="dash")
        body.columnconfigure(1, weight=2, uniform="dash")
        body.rowconfigure(2, weight=1)

        server_box = ui.Card(body, t, padding="xl")
        server_box.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        top = ui.frame(server_box.body, t, "surface")
        top.pack(fill="x")
        ui.text(top, t, "BBS SERVER", "overline", "text_faint", "surface").pack(anchor="w")
        self.status_label = ui.text(top, t, "", "title", "text", "surface", textvariable=self.status)
        self.status_label.pack(anchor="w", pady=(SPACE["xxs"], 0), fill="x")
        ui.wrap_to_width(self.status_label)
        self.dash_health = ui.text(server_box.body, t, "", "body", "text_muted", "surface")
        self.dash_health.pack(anchor="w", fill="x", pady=(SPACE["xs"], SPACE["lg"]))
        ui.wrap_to_width(self.dash_health)
        actions = ui.frame(server_box.body, t, "surface")
        actions.pack(fill="x")
        self.dash_power = ui.Button(actions, t, "Start server", self.toggle_server, "primary",
                                    "start", size="lg")
        self.dash_power.pack(side="left")
        ui.Button(actions, t, "Connection settings", self.show_radio, "ghost", "chevron").pack(
            side="left", padx=(SPACE["sm"], 0))
        self.demo_badge = ui.StatusPill(actions, t, "Demo mode is on", "accent")

        quick = ui.Card(body, t, padding="xl")
        quick.grid(row=0, column=1, sticky="nsew")
        ui.text(quick.body, t, "JUMP TO", "overline", "text_faint", "surface").pack(anchor="w")
        for key, label, desc in [
            ("messages", "Messages", "Read and draft direct messages"),
            ("bulletin", "Bulletin", "Local board posts and replies"),
            ("locals", "Locals", "Nodes your radio has heard"),
            ("commands", "Radio commands", "What mesh users can DM the BBS"),
        ]:
            self.quick_link(quick.body, key, label, desc)

        metrics = ui.frame(body, t)
        metrics.grid(row=1, column=0, columnspan=2, sticky="ew", pady=SPACE["md"])
        self.metric_values = {}
        for index, (key, label) in enumerate([("radio", "Radio"), ("link", "Connection"),
                                              ("nodes", "Known nodes"), ("uptime", "Uptime")]):
            metrics.columnconfigure(index, weight=1, uniform="metric")
            card = ui.Card(metrics, t, padding="lg")
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0 if index == 0 else SPACE["xs"], 0 if index == 3 else SPACE["xs"]))
            ui.text(card.body, t, label.upper(), "overline", "text_faint", "surface").pack(anchor="w")
            value = ui.text(card.body, t, "—", "headline", "text", "surface")
            value.pack(anchor="w", fill="x", pady=(SPACE["xs"], 0))
            sub = ui.text(card.body, t, "", "caption", "text_muted", "surface")
            sub.pack(anchor="w", fill="x")
            ui.wrap_to_width(sub)
            self.metric_values[key] = (value, sub)

        logcard = ui.Card(body, t, padding="lg")
        logcard.grid(row=2, column=0, columnspan=2, sticky="nsew")
        loghead = ui.frame(logcard.body, t, "surface")
        loghead.pack(fill="x", pady=(0, SPACE["sm"]))
        ui.text(loghead, t, "Activity", "headline", bg="surface").pack(side="left")
        ui.text(loghead, t, "  Server output and app events", "caption", "text_muted",
                "surface").pack(side="left")
        ui.Button(loghead, t, "Clear", self.clear_log, "ghost", size="sm").pack(side="right")
        ui.Button(loghead, t, "Copy", self.copy_log, "ghost", "copy", size="sm").pack(
            side="right", padx=(0, SPACE["xs"]))
        border, self.output = ui.text_area(logcard.body, t, height=8, bg_slot="surface_sunken",
                                           role="mono_small", state="disabled")
        self.output.configure(fg=t["log_text"])
        self.output.tag_configure("time", foreground=t["text_faint"])
        border.pack(fill="both", expand=True)

    def quick_link(self, parent, key, label, desc):
        t = self.theme
        row = tk.Frame(parent, bg=t["surface"], cursor="hand2", takefocus=True,
                       highlightthickness=2, highlightbackground=t["surface"],
                       highlightcolor=t["focus"])
        row.pack(fill="x", pady=(SPACE["sm"], 0))
        icon = dict((k, i) for k, _l, _n, i, _s in NAV)[key]
        glyph = tk.Label(row, text=ui.ICON[icon], font=t.font("headline"), width=2,
                         bg=t["surface_alt"], fg=t["accent"], padx=SPACE["xs"], pady=SPACE["xs"])
        glyph.pack(side="left")
        words = tk.Frame(row, bg=t["surface"])
        words.pack(side="left", fill="x", expand=True, padx=SPACE["md"])
        name = tk.Label(words, text=label, font=t.font("body_strong"), bg=t["surface"],
                        fg=t["text"], anchor="w")
        name.pack(anchor="w")
        sub = tk.Label(words, text=desc, font=t.font("caption"), bg=t["surface"],
                       fg=t["text_muted"], anchor="w")
        sub.pack(anchor="w")
        arrow = tk.Label(row, text=ui.ICON["chevron"], font=t.font("headline"),
                         bg=t["surface"], fg=t["text_faint"])
        arrow.pack(side="right", padx=SPACE["xs"])
        hoverables = (row, words, name, sub, arrow)
        for widget in hoverables + (glyph,):
            widget.bind("<Button-1>", lambda _e: self.navigate(key))
            widget.bind("<Enter>", lambda _e: [w.configure(bg=t["surface_alt"]) for w in hoverables])
            widget.bind("<Leave>", lambda _e: [w.configure(bg=t["surface"]) for w in hoverables])
        row.bind("<Return>", lambda _e: self.navigate(key))
        row.bind("<space>", lambda _e: self.navigate(key))
        row.bind("<FocusIn>", lambda _e: row.configure(highlightbackground=t["focus"]))
        row.bind("<FocusOut>", lambda _e: row.configure(highlightbackground=t["surface"]))

    def draw_mark(self, canvas):
        """Small Empire State Building app mark for the sidebar."""
        t = self.theme
        canvas.create_rectangle(0, 0, 34, 34, fill=t["sky_top"], outline="")
        canvas.create_rectangle(11, 14, 23, 34, fill=t["building"], outline="")
        canvas.create_polygon(9, 15, 25, 15, 21, 10, 13, 10, fill=t["accent"], outline="")
        canvas.create_rectangle(15, 4, 19, 10, fill=t["accent"], outline="")
        canvas.create_line(17, 0, 17, 4, fill=t["accent"], width=2)
        for x in (3, 26):
            canvas.create_rectangle(x, 22, x + 6, 34, fill=t["building"], outline="")

    def draw_skyline_header(self, event=None):
        """New York after dark: vector-only, resize-safe masthead."""
        canvas = self.header_canvas
        t = self.theme
        canvas.delete("all")
        width = max(canvas.winfo_width(), 600)
        height = 132
        bands = 6
        top, bottom = t["sky_top"], t["sky_bottom"]
        for i in range(bands):
            mix = i / (bands - 1)
            color = "#%02x%02x%02x" % tuple(
                int(int(top[j:j + 2], 16) * (1 - mix) + int(bottom[j:j + 2], 16) * mix)
                for j in (1, 3, 5))
            canvas.create_rectangle(0, i * height / bands, width, (i + 1) * height / bands + 1,
                                    fill=color, outline="")
        for n in range(int(width / 36)):
            x = (n * 89 + 17) % max(width, 1)
            peak = 62 + (n * 37) % 46
            if x < width * 0.55:
                peak = max(peak, 112)  # keep the title area clear and readable
            bw = 18 + (n * 11) % 22
            canvas.create_rectangle(x, peak, x + bw, height, fill=t["building"], outline="")
            for wy in range(peak + 8, height - 6, 14):
                if (n + wy) % 4 == 0:
                    canvas.create_rectangle(x + 6, wy, x + 9, wy + 4, fill=t["window_light"], outline="")
        tower = int(width * 0.8)
        accent = "#FF8A3D"
        canvas.create_rectangle(tower - 20, 46, tower + 20, height, fill=t["building"], outline="")
        canvas.create_polygon(tower - 25, 48, tower + 25, 48, tower + 13, 34, tower - 13, 34, fill=accent)
        canvas.create_rectangle(tower - 6, 18, tower + 6, 35, fill=accent, outline="")
        canvas.create_line(tower, 4, tower, 18, fill="#FFD49C", width=2)
        community = self.community.get().strip() or APP_NAME
        canvas.create_text(SPACE["xl"], 30, anchor="nw", text=community,
                           fill="#F7F3ED", font=t.font("display"))
        canvas.create_text(SPACE["xl"] + 1, 72, anchor="nw",
                           text="From the Five Boroughs to the North Country",
                           fill="#F4D7BF", font=t.font("body"))
        canvas.create_text(SPACE["xl"] + 1, 96, anchor="nw",
                           text="62 COUNTIES  ·  ONE NETWORK", fill=accent, font=t.font("overline"))
        canvas.create_rectangle(0, height - 3, width, height, fill=accent, outline="")

    def refresh_summary(self):
        """Derive dashboard and sidebar displays from the live state variables."""
        if not getattr(self, "metric_values", None):
            return
        try:
            if not self.dash_health.winfo_exists():
                return
            self._refresh_summary()
        except tk.TclError:
            pass  # widgets are being rebuilt

    def _refresh_summary(self):
        health = self.radio_health.get().replace("Radio health: ", "", 1)
        self.dash_health.configure(text="Radio: " + health)
        tone = self.status_tone()[0]
        first = health.split(" — ")[0]
        names = {"BBS STOPPED": "Stopped", "BBS READY": "Ready", "USB MISSING": "USB missing",
                 "DEMO": "Demo", "STARTING": "Starting", "unverified": "Not verified"}
        self.metric_values["radio"][0].configure(text=names.get(first, first.capitalize()),
                                                 fg=self.theme[ui.TONES[tone][0]] if tone != "neutral" else self.theme["text"])
        self.metric_values["radio"][1].configure(text=" — ".join(health.split(" — ")[1:]) or "Not verified")
        mode = "Network (TCP)" if self.connection.get() == "tcp" else "USB / serial"
        self.metric_values["link"][0].configure(text=mode)
        self.metric_values["link"][1].configure(text=self.address.get().strip() or
                                                ("Automatic port discovery" if self.connection.get() == "serial"
                                                 else "No host set"))
        self.metric_values["nodes"][0].configure(text=self.node_count.get().split(":")[-1].split("(")[0].strip() or "0")
        self.metric_values["nodes"][1].configure(
            text=(f"Radio holds {self.radio_node_count} · all saved on this Mac" if self.radio_node_count is not None
                  else "Saved on this Mac · see Locals"))
        seconds = self.uptime.get().split(":")[-1].strip().rstrip("s")
        self.metric_values["uptime"][0].configure(text=ui.humanize_seconds(seconds))
        self.metric_values["uptime"][1].configure(text="Since the server started")
        running = bool(self.supervisor and self.supervisor.running) or self.status.get().lower().startswith("demo running")
        self.dash_power.set_label("Stop server" if running else "Start server",
                                  "stop" if running else "start", "danger" if running else "primary")
        if hasattr(self, "conn_power"):
            self.conn_power.set_label("Stop server" if running else "Start server",
                                      "stop" if running else "start", "danger" if running else "primary")
        if self.demo.get() and not self.status.get().lower().startswith("demo"):
            if not self.demo_badge.winfo_ismapped():
                self.demo_badge.pack(side="right")
        elif self.demo_badge.winfo_ismapped():
            self.demo_badge.pack_forget()
        if hasattr(self, "preview"):
            self.preview.configure(text=self.attitude_message())
            self.attitude_help.configure(text=ATTITUDE_HELP.get(self.attitude.get(), ""))

    def toggle_server(self):
        running = bool(self.supervisor and self.supervisor.running) or self.status.get().lower().startswith("demo running")
        if running:
            self.stop()
        else:
            self.start()
        self.refresh_summary()

    def status_tone(self):
        state = self.status.get().lower()
        if state.startswith("demo"):
            return "accent", False
        if state.startswith("running") or state.startswith("ready") or state.startswith("connected"):
            return "success", False
        if state.startswith("starting") or "recovery" in state:
            return "warning", True
        if state.startswith("stopped"):
            return "neutral", False
        return "danger", False

    def update_status_color(self, *_args):
        """Green only when running; stopped is neutral, problems are red."""
        tone, pulse = self.status_tone()
        fg = self.theme[ui.TONES[tone][0]] if tone != "neutral" else self.theme["text"]
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.configure(fg=fg)
        if hasattr(self, "side_status") and self.side_status.winfo_exists():
            self.side_status.set(self.short_status(), tone, pulse)
        if hasattr(self, "conn_status") and self.conn_status.winfo_exists():
            self.conn_status.set(self.status.get(), tone, pulse)
        self.refresh_summary()

    def short_status(self):
        value = self.status.get()
        short = value.split(" — ")[0]
        return short if len(short) <= 26 else short[:25] + "…"

    def clear_log(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.output.get("1.0", "end-1c"))
        self.toast("Activity log copied to the clipboard")

    def show_home(self):
        self.navigate("home")

    # --------------------------------------------------------- connection
    def build_connection(self, page):
        t = self.theme
        inner = self.scroll_page(page)
        head, _actions = page_header(inner, t, "Connection",
                                     "Choose how this Mac talks to your Meshtastic radio, then start the BBS server.",
                                     "The Empire Connection")
        head.pack(fill="x", padx=SPACE["xl"], pady=(SPACE["xl"], SPACE["lg"]))
        grid = ui.frame(inner, t)
        grid.pack(fill="both", expand=True, padx=SPACE["xl"], pady=(0, SPACE["xl"]))
        grid.columnconfigure(0, weight=3, uniform="conn")
        grid.columnconfigure(1, weight=2, uniform="conn")

        radio = ui.Card(grid, t, padding="xl")
        radio.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        self.connection_panel = radio
        ui.section_header(radio.body, t, "Radio", "How the BBS reaches your radio. Changes apply the next time "
                          "the server starts.").pack(fill="x", anchor="w")
        ui.Segmented(radio.body, t, self.connection,
                     [("serial", "USB cable"), ("tcp", "Wi-Fi / network")]).pack(
            anchor="w", pady=(SPACE["lg"], SPACE["lg"]))
        self.address_field = ui.Field(radio.body, t, "Serial port", self.address, " ")
        self.address_field.pack(fill="x")
        self.connection_address = self.address_field.entry
        tools = ui.frame(radio.body, t, "surface")
        tools.pack(fill="x", pady=(SPACE["lg"], 0))
        self.find_ports_btn = ui.Button(tools, t, "Find USB radios", self.list_ports, "secondary", "search")
        self.find_ports_btn.pack(side="left", padx=(0, SPACE["sm"]))
        self.check_btn = ui.Button(tools, t, "Test connection", self.check_radio_target, "secondary", "check",
                                   tooltip="Checks the port or network address only. Nothing is transmitted.")
        self.check_btn.pack(side="left")
        self.port_choices = ui.frame(radio.body, t, "surface")
        self.port_choices.pack(fill="x")
        self.connection_banner = ui.Banner(radio.body, t, bg="surface")
        self.connection_banner.place_with(fill="x", pady=(SPACE["md"], 0))

        side = ui.frame(grid, t)
        side.grid(row=0, column=1, rowspan=2, sticky="nsew")
        server_box = ui.Card(side, t, padding="xl")
        server_box.pack(fill="x")
        ui.section_header(server_box.body, t, "Server").pack(fill="x", anchor="w")
        self.conn_status = ui.StatusPill(server_box.body, t, self.status.get(), "neutral")
        self.conn_status.pack(anchor="w", pady=(SPACE["sm"], SPACE["xs"]))
        health = ui.text(server_box.body, t, "", "caption", "text_muted", "surface",
                         textvariable=self.radio_health)
        health.pack(anchor="w", fill="x")
        ui.wrap_to_width(health)
        self.conn_power = ui.Button(server_box.body, t, "Start server", self.toggle_server, "primary",
                                    "start", size="lg")
        self.conn_power.pack(fill="x", pady=(SPACE["lg"], 0))
        options = ui.Card(side, t, padding="xl")
        options.pack(fill="x", pady=(SPACE["md"], 0))
        ui.section_header(options.body, t, "Options").pack(fill="x", anchor="w", pady=(0, SPACE["sm"]))
        ui.Toggle(options.body, t, "Demo mode", self.demo,
                  "Explore the app without a radio. Nothing connects or transmits.").pack(fill="x")
        tk.Frame(options.body, bg=t["border"], height=1).pack(fill="x", pady=SPACE["md"])
        ui.Toggle(options.body, t, "Automatic recovery", self.auto_restart,
                  "Restart the server after a crash or lost radio, with increasing delays "
                  "up to one minute. If the USB radio is unplugged, waits for it to return.").pack(fill="x")

        trouble = ui.Card(grid, t, padding="xl")
        trouble.grid(row=1, column=0, sticky="nsew", padx=(0, SPACE["md"]), pady=(SPACE["md"], 0))
        ui.section_header(trouble.body, t, "Troubleshooting", "Common fixes when the radio won't connect.").pack(
            fill="x", anchor="w")
        for title, lines in [
            ("USB radio not found", [
                "Use a data-capable USB cable (some cables only charge).",
                "Unplug and reconnect the radio, then choose Find USB radios.",
                "Quit other apps that may hold the port, such as the Meshtastic web client or a serial monitor.",
            ]),
            ("Network radio unreachable", [
                "Enable Wi-Fi on the radio and confirm it joined the same network as this Mac.",
                "Use the radio's IP address if its .local name doesn't resolve.",
                "The radio accepts one network client at a time — close the Meshtastic app on other devices.",
            ]),
            ("What “Test connection” proves", [
                "It checks that the port exists or that the address accepts a network connection on port 4403.",
                "It does not prove the Meshtastic protocol works or that messages are delivered over the air.",
                "Stop the server before testing — a second network connection can disrupt the radio.",
            ]),
        ]:
            section = ui.Collapsible(trouble.body, t, title)
            section.pack(fill="x", pady=(SPACE["sm"], 0))
            for line in lines:
                ui.wrap_to_width(ui.text(section.body, t, "•  " + line, "caption", "text_muted", "surface")).pack(
                    anchor="w", fill="x", pady=1)

    def on_connection_mode(self):
        if not hasattr(self, "address_field") or not self.address_field.winfo_exists():
            return
        if self.connection.get() == "tcp":
            self.address_field.winfo_children()[0].configure(text="Radio address")
            self.address_field.set_help("Hostname or IP address, for example meshtastic.local or 192.168.1.50. "
                                        "Port 4403 is used automatically.")
            self.find_ports_btn.pack_forget()
            for child in self.port_choices.winfo_children():
                child.destroy()
        else:
            self.address_field.winfo_children()[0].configure(text="Serial port")
            self.address_field.set_help("For example /dev/cu.usbserial-0001. Leave empty to let Meshtastic "
                                        "find the radio automatically.")
            if not self.find_ports_btn.winfo_ismapped():
                self.find_ports_btn.pack(side="left", padx=(0, SPACE["sm"]), before=self.check_btn)
        self.validate_address_live()

    def validate_address_live(self):
        if not hasattr(self, "address_field") or not self.address_field.winfo_exists():
            return
        if self.connection.get() == "tcp" and not self.address.get().strip():
            self.address_field.set_error("")
            return
        self.address_field.set_error(self.validate_radio_target() or "")

    def _notify_connection(self, message, tone="info", title="Radio connection"):
        banner = getattr(self, "connection_banner", None)
        try:
            if banner is not None and banner.winfo_exists():
                banner.show(message, tone)
                return
        except tk.TclError:
            pass
        (messagebox.showerror if tone == "danger" else messagebox.showinfo)(title, message)

    def show_port_choices(self, ports):
        if not hasattr(self, "port_choices"):
            return
        for child in self.port_choices.winfo_children():
            child.destroy()
        if not ports:
            return
        t = self.theme
        ui.text(self.port_choices, t, "Detected ports — click to use:", "caption", "text_muted",
                "surface").pack(anchor="w", pady=(SPACE["md"], SPACE["xs"]))
        for port in ports:
            ui.Button(self.port_choices, t, port, lambda p=port: (self.address.set(p), self.toast("Using " + p)),
                      "secondary", "connection", size="sm").pack(anchor="w", pady=1)

    # ------------------------------------------------------- radio command
    def build_commands(self, page):
        t = self.theme
        inner = self.scroll_page(page)
        head, _a = page_header(inner, t, "Radio commands",
                               "What people on the mesh can send to your BBS node in a direct message. "
                               "Copy a command to share it.", "The Playbook")
        head.pack(fill="x", padx=SPACE["xl"], pady=(SPACE["xl"], SPACE["lg"]))
        card = ui.Card(inner, t, padding="md")
        card.pack(fill="x", padx=SPACE["xl"], pady=(0, SPACE["xl"]))
        for index, (command, description) in enumerate(COMMANDS):
            if index:
                tk.Frame(card.body, bg=t["border"], height=1).pack(fill="x")
            line = ui.frame(card.body, t, "surface")
            line.pack(fill="x", pady=SPACE["xs"])
            tk.Label(line, text=command, font=t.font("mono"), bg=t["surface_alt"], fg=t["accent"],
                     padx=SPACE["sm"], pady=SPACE["xxs"], width=10, anchor="w").pack(
                side="left", padx=(SPACE["xs"], SPACE["lg"]), pady=SPACE["xs"])
            ui.text(line, t, description, "body", bg="surface").pack(side="left", fill="x", expand=True)

            def copy(value=command):
                self.root.clipboard_clear()
                self.root.clipboard_append(value)
                self.log("Copied radio command: " + value)
                self.toast(f"Copied {value}")
            ui.Button(line, t, "Copy", copy, "ghost", "copy", size="sm").pack(side="right")
        ui.wrap_to_width(ui.text(inner, t, "These are typed on a phone or radio, not in this app. "
                                 "The BBS answers over the mesh while the server is running.",
                                 "caption", "text_muted")).pack(anchor="w", fill="x", padx=SPACE["xl"])

    def commands(self):
        self.navigate("commands")

    def command_panel(self, title, subtitle, commands):
        """Compatibility: the command browser is now the Radio commands page."""
        self.navigate("commands")

    # ----------------------------------------------------------- settings
    def build_settings(self, page):
        t = self.theme
        inner = self.scroll_page(page)
        head, _a = page_header(inner, t, "Settings",
                               "Preferences stored on this Mac. Changing them never transmits anything.",
                               "Control Room")
        head.pack(fill="x", padx=SPACE["xl"], pady=(SPACE["xl"], SPACE["lg"]))
        col = ui.frame(inner, t)
        col.pack(fill="x", padx=SPACE["xl"], pady=(0, SPACE["xl"]))

        community = ui.Card(col, t, padding="xl")
        community.pack(fill="x")
        ui.section_header(community.body, t, "Community",
                          "How your BBS introduces itself in this app.").pack(fill="x", anchor="w")
        name_field = ui.Field(community.body, t, "Community name", self.community)
        name_field.pack(fill="x", pady=(SPACE["lg"], 0))
        welcome_field = ui.Field(community.body, t, "Welcome message", self.welcome,
                                 "Saved locally. Sending it over the radio is not available yet.")
        welcome_field.pack(fill="x", pady=(SPACE["md"], 0))
        save_row = ui.frame(community.body, t, "surface")
        save_row.pack(fill="x", pady=(SPACE["lg"], 0))
        self.settings_state = ui.text(save_row, t, "", "caption", "text_muted", "surface")

        def save_community():
            if not self.community.get().strip():
                name_field.set_error("Enter a community name.")
                return
            name_field.set_error("")
            try:
                self.save()
            except OSError as exc:
                messagebox.showerror("Couldn't save settings", str(exc), parent=self.root)
                return
            self.settings_state.configure(text=ui.ICON["check"] + "  Saved", fg=t["success"])
            self.draw_skyline_header()
            self.toast("Settings saved")
        ui.Button(save_row, t, "Save changes", save_community, "primary", "check").pack(side="left")
        self.settings_state.pack(side="left", padx=SPACE["md"])
        if not getattr(self, "_settings_traced", False):
            self._settings_traced = True
            for var in (self.community, self.welcome):
                var.trace_add("write", lambda *_a: self.mark_settings_dirty())

        personality = ui.Card(col, t, padding="xl")
        personality.pack(fill="x", pady=(SPACE["md"], 0))
        ui.section_header(personality.body, t, "Personality",
                          "The New York attitude the BBS uses in replies. Saved as soon as you choose.").pack(
            fill="x", anchor="w")
        ui.Segmented(personality.body, t, self.attitude, ATTITUDES, command=self.set_attitude).pack(
            anchor="w", pady=(SPACE["lg"], SPACE["xs"]))
        self.attitude_help = ui.text(personality.body, t, ATTITUDE_HELP.get(self.attitude.get(), ""),
                                     "caption", "text_muted", "surface")
        self.attitude_help.pack(anchor="w")
        ui.text(personality.body, t, "PREVIEW · NOT TRANSMITTED", "overline", "text_faint", "surface").pack(
            anchor="w", pady=(SPACE["lg"], SPACE["xs"]))
        self.preview = ui.text(personality.body, t, self.attitude_message(), "mono", "text",
                               "surface_sunken", padx=SPACE["md"], pady=SPACE["md"])
        self.preview.pack(fill="x")
        ui.wrap_to_width(self.preview, margin=SPACE["xl"])

        appearance = ui.Card(col, t, padding="xl")
        appearance.pack(fill="x", pady=(SPACE["md"], 0))
        ui.section_header(appearance.body, t, "Appearance").pack(fill="x", anchor="w")
        for label, var, options in [("Theme", self.appearance, ui.APPEARANCES),
                                    ("Text size", self.text_scale, ui.TEXT_SCALE_LABELS)]:
            row = ui.frame(appearance.body, t, "surface")
            row.pack(fill="x", pady=(SPACE["lg"], 0))
            ui.text(row, t, label, "body_strong", bg="surface", width=12).pack(side="left")
            ui.Segmented(row, t, var, options, command=lambda _v: self.apply_appearance()).pack(side="left")

        data = ui.Card(col, t, padding="xl")
        data.pack(fill="x", pady=(SPACE["md"], 0))
        ui.section_header(data.body, t, "Data & backups",
                          "Backups can contain private messages and radio keys. Store them safely.").pack(
            fill="x", anchor="w")
        row = ui.frame(data.body, t, "surface")
        row.pack(fill="x", pady=(SPACE["lg"], 0))
        ui.Button(row, t, "Back up data…", self.backup, "secondary", "archive").pack(side="left")
        ui.Button(row, t, "Restore…", self.restore, "secondary").pack(side="left", padx=(SPACE["sm"], 0))
        ui.Button(row, t, "Show data folder", self.reveal_data, "ghost", "folder").pack(
            side="left", padx=(SPACE["sm"], 0))
        ui.Button(row, t, "Legacy drafts…", self.list_local_mail_drafts, "ghost").pack(
            side="left", padx=(SPACE["sm"], 0))
        ui.text(data.body, t, "Data folder: " + str(DATA), "caption", "text_faint", "surface").pack(
            anchor="w", pady=(SPACE["sm"], 0))

    def mark_settings_dirty(self):
        label = getattr(self, "settings_state", None)
        try:
            if label is not None and label.winfo_exists():
                label.configure(text="Unsaved changes", fg=self.theme["warning"])
        except tk.TclError:
            pass

    def apply_appearance(self):
        try:
            self.save()
        except OSError as exc:
            self.log(f"Could not save appearance: {exc}")
        self.rebuild()
        self.toast("Appearance updated")

    def settings(self):
        self.navigate("settings")

    def reveal_data(self):
        DATA.mkdir(parents=True, exist_ok=True)
        opener = ["open", str(DATA)] if sys.platform == "darwin" else ["xdg-open", str(DATA)]
        try:
            subprocess.Popen(opener)
        except OSError as exc:
            messagebox.showerror("Couldn't open folder", str(exc), parent=self.root)

    # -------------------------------------------------------------- about
    def build_about(self, page):
        t = self.theme
        inner = self.scroll_page(page)
        card = ui.Card(inner, t, padding="xl")
        card.pack(fill="x", padx=SPACE["xl"], pady=SPACE["xl"])
        top = ui.frame(card.body, t, "surface")
        top.pack(fill="x")
        mark = tk.Canvas(top, width=34, height=34, bg=t["surface"], highlightthickness=0)
        mark.pack(side="left", anchor="n")
        self.draw_mark(mark)
        words = ui.frame(top, t, "surface")
        words.pack(side="left", padx=SPACE["md"])
        ui.text(words, t, APP_NAME, "title", bg="surface").pack(anchor="w")
        ui.text(words, t, f"Version {APP_VERSION} · Intel Mac", "caption", "text_muted", "surface").pack(anchor="w")
        for heading, body in [
            ("What it is", "A New York statewide Meshtastic bulletin board system — from the Five Boroughs "
                           "to the North Country. Mail, bulletins and radio commands for your local mesh."),
            ("Safety", "Demo mode never connects or transmits. Saving a draft never transmits. Each send "
                       "happens once, only after you confirm, and is never retried automatically."),
            ("Credits", "Based on TC²-BBS by TheCommsChannel, with adaptations through The Sitdown BBS. "
                        "Original contributors retain their credits."),
            ("License", "GNU General Public License v3. See README and LICENSE."),
        ]:
            ui.text(card.body, t, heading, "body_strong", bg="surface").pack(anchor="w", pady=(SPACE["lg"], 0))
            ui.wrap_to_width(ui.text(card.body, t, body, "body", "text_muted", "surface")).pack(
                anchor="w", fill="x", pady=(SPACE["xxs"], 0))

    def show_about(self):
        self.navigate("about")

    # ---------------------------------------------------- local records
    def local_database(self):
        """Locate the BBS SQLite store without creating an empty database."""
        for path in (DATA / "bulletins.db", ROOT / "bulletins.db"):
            if path.is_file():
                return path
        return None

    def browse_local_records(self, title, table, columns):
        """Read-only browser of the local BBS database, never a radio inbox."""
        t = self.theme
        win = ui.sheet(self.root, t, title + " | " + APP_NAME, 820, 600)
        pad = ui.frame(win, t)
        pad.pack(fill="both", expand=True, padx=SPACE["xl"], pady=SPACE["xl"])
        ui.text(pad, t, title, "title").pack(anchor="w")
        ui.wrap_to_width(ui.text(pad, t, "Read-only view of the BBS database on this Mac — not a live mesh feed. "
                                 "Mail may contain private messages. Nothing is transmitted.",
                                 "body", "text_muted")).pack(anchor="w", fill="x", pady=(SPACE["xxs"], SPACE["lg"]))
        search = tk.StringVar()
        row = ui.frame(pad, t)
        row.pack(fill="x", pady=(0, SPACE["md"]))
        field = ui.Field(row, t, "Search the latest 100 records", search, bg="bg")
        field.pack(side="left", fill="x", expand=True)
        border, content = ui.text_area(pad, t, height=14, bg_slot="surface", role="mono_small")
        border.pack(fill="both", expand=True)
        content.tag_configure("key", foreground=t["text_faint"])
        content.tag_configure("rule", foreground=t["border_strong"])

        def refresh(_event=None):
            content.configure(state="normal")
            content.delete("1.0", "end")
            path = self.local_database()
            if not path:
                content.insert("end", "No local BBS database yet. Start the BBS to create records, then refresh.\n")
            else:
                try:
                    # Read-only URI prevents accidental writes and creation.
                    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=2) as conn:
                        names = {r[0] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        if table not in names:
                            content.insert("end", "The database has no " + table + " table yet.\n")
                        else:
                            selected = ", ".join('"' + col + '"' for col in columns)
                            query = "SELECT " + selected + " FROM " + table + " ORDER BY id DESC LIMIT 100"
                            records = conn.execute(query).fetchall()
                            needle = search.get().strip().casefold()
                            if needle:
                                records = [record for record in records if any(
                                    needle in str(value).casefold() for value in record)]
                            if not records:
                                content.insert("end", "No matching records among the latest 100.\n")
                            for record in records:
                                for col, value in zip(columns, record):
                                    content.insert("end", f"{col}: ", "key")
                                    content.insert("end", f"{value}\n")
                                content.insert("end", "─" * 55 + "\n", "rule")
                except sqlite3.Error as exc:
                    content.insert("end", "Could not read local BBS data: " + str(exc))
            content.configure(state="disabled")
        field.entry.bind("<Return>", refresh)
        ui.Button(row, t, "Search", refresh, "primary", "search", bg="bg").pack(
            side="left", padx=(SPACE["sm"], 0), anchor="s")
        refresh()
        field.entry.focus_set()

    def compose_local_mail_draft(self, draft_path=None):
        """Edit a legacy operator draft saved locally, never to the BBS mail database."""
        t = self.theme
        saved = {}
        if draft_path is not None:
            try:
                saved = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                messagebox.showerror("Couldn't open draft", str(exc), parent=self.root)
                return
        win = ui.sheet(self.root, t, "Legacy draft | " + APP_NAME, 640, 560)
        pad = ui.frame(win, t)
        pad.pack(fill="both", expand=True, padx=SPACE["xl"], pady=SPACE["xl"])
        ui.text(pad, t, "Edit legacy draft" if draft_path else "New legacy draft", "title").pack(anchor="w")
        ui.text(pad, t, "Saved only on this Mac. Not sent over the mesh.", "body", "text_muted").pack(
            anchor="w", pady=(SPACE["xxs"], SPACE["lg"]))
        recipient = ui.Field(pad, t, "Recipient node ID", tk.StringVar(value=str(saved.get("recipient", ""))),
                             "A Meshtastic node ID like !a1b2c3d4.", bg="bg")
        recipient.pack(fill="x")
        subject = ui.Field(pad, t, "Subject", tk.StringVar(value=str(saved.get("subject", ""))), bg="bg")
        subject.pack(fill="x", pady=(SPACE["md"], 0))
        ui.text(pad, t, "Message", "label").pack(anchor="w", pady=(SPACE["md"], SPACE["xs"]))
        border, body = ui.text_area(pad, t, height=8)
        border.pack(fill="both", expand=True)
        body.insert("1.0", str(saved.get("message", "")))

        def save_draft(_event=None):
            rid = recipient.value().strip()
            subj = subject.value().strip()
            message = body.get("1.0", "end-1c").strip()
            recipient.set_error("" if rid else "Required.")
            subject.set_error("" if subj else "Required.")
            if not rid or not subj or not message:
                if not message:
                    messagebox.showerror("Draft incomplete", "Recipient, subject and message are required.",
                                         parent=win)
                return "break"
            if not (rid.startswith("!") and len(rid) == 9
                    and all(c in "0123456789abcdefABCDEF" for c in rid[1:])):
                recipient.set_error("Use a Meshtastic node ID such as !a1b2c3d4.")
                return "break"
            drafts = DATA / "drafts"
            try:
                drafts.mkdir(parents=True, exist_ok=True)
                import uuid
                draft = draft_path if draft_path is not None else drafts / (uuid.uuid4().hex + ".json")
                draft.write_text(json.dumps({"recipient": rid, "subject": subj, "message": message},
                                            indent=2), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Couldn't save draft", str(exc), parent=win)
                return "break"
            self.log("Saved a local mail draft; nothing was transmitted.")
            self.toast("Draft saved — nothing was transmitted")
            win.destroy()
            if getattr(self, "_drafts_refresh", None):
                self._drafts_refresh()
            return "break"
        buttons = ui.frame(pad, t)
        buttons.pack(fill="x", pady=(SPACE["lg"], 0))
        ui.Button(buttons, t, "Save draft", save_draft, "primary", "check", bg="bg").pack(side="right")
        ui.Button(buttons, t, "Cancel", win.destroy, "ghost", bg="bg").pack(side="right", padx=(0, SPACE["sm"]))
        win.bind("<Command-s>", save_draft)
        recipient.entry.focus_set()

    def list_local_mail_drafts(self):
        """Browse and reopen legacy drafts without sending over the mesh."""
        existing = getattr(self, "_drafts_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            return
        t = self.theme
        folder = DATA / "drafts"
        win = ui.sheet(self.root, t, "Legacy drafts | " + APP_NAME, 680, 480)
        self._drafts_window = win
        pad = ui.frame(win, t)
        pad.pack(fill="both", expand=True, padx=SPACE["xl"], pady=SPACE["xl"])
        ui.text(pad, t, "Legacy drafts", "title").pack(anchor="w")
        ui.wrap_to_width(ui.text(pad, t, "Drafts saved by earlier versions of the app. They stay on this Mac "
                                 "and are never sent.", "body", "text_muted")).pack(
            anchor="w", fill="x", pady=(SPACE["xxs"], SPACE["lg"]))
        buttons = ui.frame(pad, t)
        buttons.pack(side="bottom", fill="x", pady=(SPACE["lg"], 0))
        table_box = ui.frame(pad, t)
        table_box.pack(fill="both", expand=True)
        box, items = ui.treeview(table_box, t, [("recipient", "Recipient", 140, False),
                                                ("subject", "Subject", 380, True)], height=8)
        box.pack(fill="both", expand=True)
        empty = ui.EmptyState(table_box, t, "messages", "No legacy drafts",
                              "New drafts are written from Messages.", bg="surface")
        paths = {}

        def refresh():
            if not win.winfo_exists():
                return
            items.delete(*items.get_children())
            paths.clear()
            for index, path in enumerate(sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                iid = str(index)
                paths[iid] = path
                items.insert("", "end", iid=iid, values=(record.get("recipient", "?"),
                                                         record.get("subject", "(no subject)")),
                             tags=("even" if index % 2 else "odd",))
            if paths:
                empty.place_forget()
            else:
                empty.place(in_=table_box, relx=0, rely=0, relwidth=1, relheight=1)
        self._drafts_refresh = refresh

        def chosen():
            selected = items.selection()
            if not selected:
                self.toast("Select a draft first", "info")
                return None
            return paths.get(selected[0])

        def edit_selected():
            path = chosen()
            if path:
                self.compose_local_mail_draft(path)

        def delete_selected():
            path = chosen()
            if not path:
                return
            if not messagebox.askyesno("Delete this draft?",
                                       "The draft will be permanently deleted from this Mac.\n"
                                       "No mesh or BBS messages are affected.", parent=win, icon="warning"):
                return
            try:
                path.unlink()
            except OSError as exc:
                messagebox.showerror("Couldn't delete draft", str(exc), parent=win)
                return
            self.log("Deleted a local draft; no mesh message was changed.")
            self.toast("Draft deleted")
            refresh()

        def copy_selected():
            path = chosen()
            if not path:
                return
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                payload = "Recipient: {}\nSubject: {}\n\n{}".format(
                    record.get("recipient", ""), record.get("subject", ""),
                    record.get("message", ""))
                win.clipboard_clear()
                win.clipboard_append(payload)
            except (OSError, ValueError, tk.TclError) as exc:
                messagebox.showerror("Couldn't copy draft", str(exc), parent=win)
                return
            self.toast("Draft copied to the clipboard — nothing was sent")
        ui.Button(buttons, t, "Edit", edit_selected, "primary", bg="bg").pack(side="right")
        ui.Button(buttons, t, "Copy", copy_selected, "secondary", "copy", bg="bg").pack(
            side="right", padx=(0, SPACE["sm"]))
        ui.Button(buttons, t, "Delete…", delete_selected, "subtle_danger", bg="bg").pack(side="left")
        items.bind("<Double-1>", lambda _e: edit_selected())
        items.bind("<BackSpace>", lambda _e: delete_selected())
        refresh()

    # ------------------------------------------------------- navigation
    def show_messages(self):
        open_posts(self, "mail")

    def show_bulletin(self):
        open_posts(self, "bulletin")

    def show_locals(self):
        open_nodes(self)

    def show_radio(self):
        """Navigate to existing controls without touching the active radio session."""
        self.navigate("connection")
        self.root.deiconify()
        self.root.lift()
        self.connection_panel.configure(highlightbackground=self.colors["highlight"])
        self.connection_address.focus_set()
        self.refresh_radio_health()
        try:
            self.root.after(1600, self._unhighlight_connection)
        except (tk.TclError, AttributeError):
            pass

    def _unhighlight_connection(self):
        panel = getattr(self, "connection_panel", None)
        try:
            if isinstance(panel, tk.Widget) and panel.winfo_exists():
                panel.configure(highlightbackground=self.theme["border"])
        except tk.TclError:
            pass

    def refresh_radio_health(self):
        """Display only the state already observed by the running supervisor."""
        monitor = self.supervisor
        present = None
        if self.connection.get() == "serial" and self.address.get().strip() and self.last_serial_ports is not None:
            present = self.address.get().strip() in self.last_serial_ports
        label = radio_health_label(self.demo.get(),
            bool(monitor and monitor.running), bool(monitor and monitor.ready),
            present, self.waiting_for_usb and self.connection.get() == "serial",
            monitor.radio_state if monitor else None)
        # Skip identical writes so traces don't redraw the dashboard every poll.
        try:
            unchanged = isinstance(self.radio_health, tk.Variable) and self.radio_health.get() == label
        except tk.TclError:
            unchanged = False
        if not unchanged:
            self.radio_health.set(label)

    def attitude_message(self):
        return {
            "clean": "AYO! Welcome to the Empire State. Whaddya wanna do, boss?",
            "spicy": "AYO! Welcome to New York. Pick a damn command already, we're busy over here!",
            "unfiltered": "AYO! Welcome to the Empire State. Pick a fuckin' command, boss!",
        }.get(self.attitude.get(), "AYO! Welcome to the Empire State.")

    def set_attitude(self, value):
        self.attitude.set(value)
        self.save()
        if hasattr(self, "preview"):
            self.preview.configure(text=self.attitude_message())
        self.log(self.attitude_message())

    def styled_button(self, parent, text, command, color=None):
        """Compatibility wrapper around the design-system button."""
        return ui.Button(parent, self.theme, text, command, "secondary")

    def log(self, message):
        self.output.configure(state="normal")
        self.output.insert("end", time.strftime("%H:%M:%S  "), "time")
        self.output.insert("end", message + "\n")
        if int(self.output.index("end-1c").split(".")[0]) > 2000:
            self.output.delete("1.0", "300.0")
        self.output.see("end")
        self.output.configure(state="disabled")

    # ------------------------------------------------------- radio logic
    def validate_radio_target(self):
        """Validate connection input before launching the BBS process."""
        mode = self.connection.get()
        addr = self.address.get().strip()
        if mode not in ("serial", "tcp"):
            return "Choose USB/Serial or TCP."
        if mode == "tcp":
            if not addr:
                return "Enter a radio hostname or IP address."
            if any(c.isspace() for c in addr) or "/" in addr or "://" in addr:
                return "Enter a hostname or IP address, not a URL."
            try:
                ipaddress.ip_address(addr)
            except ValueError:
                labels = addr.split(".")
                if len(addr) > 253 or not all(
                    label and len(label) <= 63 and label[0].isalnum()
                    and label[-1].isalnum()
                    and all(c.isalnum() or c == "-" for c in label)
                    for label in labels
                ):
                    return "Invalid TCP hostname or IP address."
        elif any(ord(c) < 32 or ord(c) == 127 for c in addr):
            return "Serial port contains invalid characters."
        return None

    def list_ports(self):
        try:
            ports = available_serial_ports()
            self.log("USB ports: " + (", ".join(ports) if ports else "None detected"))
            if len(ports) == 1 and self.connection.get() == "serial" and not self.address.get().strip():
                self.address.set(ports[0])
                self.log("Automatically selected the only available USB port.")
            if hasattr(self, "connection_banner"):
                if ports:
                    self._notify_connection(f"Found {len(ports)} USB device{'s' if len(ports) != 1 else ''}. "
                                            "Choose one below or type a port.", "success")
                else:
                    self._notify_connection("No USB serial devices found. Check the cable, radio power and "
                                            "that no other app is using the radio.", "warning")
                self.show_port_choices(ports)
        except ImportError:
            self.log("pyserial is missing. Install requirements.txt.")
            self._notify_connection("USB support (pyserial) is missing from this installation.", "danger")
        except OSError as exc:
            self.log(f"USB enumeration failed: {exc}")
            self._notify_connection(f"Couldn't list USB devices: {exc}", "danger")

    def watch_serial_ports(self):
        """Observe USB changes without opening a port or interrupting a session."""
        if self.demo.get() or self.connection.get() != "serial":
            return
        try:
            ports = available_serial_ports()
        except (ImportError, OSError) as exc:
            if self.last_serial_ports is not None:
                self.log(f"USB scan unavailable: {exc}")
                self.last_serial_ports = None
            return
        previous = self.last_serial_ports
        self.last_serial_ports = tuple(ports)
        if previous is None:
            return
        added = sorted(set(ports) - set(previous))
        removed = sorted(set(previous) - set(ports))
        if added:
            self.log("USB attached: " + ", ".join(added))
        if removed:
            self.log("USB disconnected: " + ", ".join(removed))
        selected = self.address.get().strip()
        if selected and selected in removed:
            self.status.set("USB device removed — radio connection unverified")
            self.log("Selected USB radio disappeared; waiting for reconnect.")
            if self.auto_restart.get() and self.supervisor and self.supervisor.running:
                self.supervisor.stop()
                self.supervisor = None
                self.waiting_for_usb = True
                self.restart_at = None
                self.log("Stopped BBS; waiting for the same USB port to return.")
        if (self.waiting_for_usb and selected in added and
                self.auto_restart.get() and not self.demo.get() and
                not (self.supervisor and self.supervisor.running)):
            self.waiting_for_usb = False
            self.restart_at = time.monotonic() + 2
            self.log("USB port returned; scheduling restart in 2 seconds.")
        if (not selected and len(ports) == 1 and
                not (self.supervisor and self.supervisor.running)):
            self.address.set(ports[0])
            self.log("Auto-selected newly detected USB port: " + ports[0])

    def check_radio_target(self):
        """Non-transmitting preflight; does not prove a Meshtastic session."""
        if self.demo.get():
            self._notify_connection("Demo mode is on, so no radio checks run and nothing is transmitted.",
                                    "info", "Radio preflight")
            return
        if self.supervisor and self.supervisor.running:
            self._notify_connection("Stop the server before testing — a second network connection can "
                                    "disrupt the running radio session.", "warning", "Radio preflight")
            return
        error = self.validate_radio_target()
        if error:
            self._notify_connection(error, "danger", "Radio preflight")
            return
        if self._probe is not None and self._probe.is_alive():
            return
        mode, address = self.connection.get(), self.address.get().strip()
        result = {}

        def probe():
            try:
                if mode == "serial":
                    result["text"] = check_serial_target(address)
                else:
                    result["text"] = check_tcp_target(address)
            except (OSError, ImportError) as exc:
                result["text"] = f"Radio preflight failed: {exc}"

        if not hasattr(self, "connection_banner"):
            probe()  # headless/test callers: run synchronously
            self.log(result["text"])
            messagebox.showinfo("Radio preflight", result["text"])
            return
        # Run off the UI thread so a slow network never freezes the window.
        self._notify_connection("Testing " + (address or "USB ports") + "…", "info")
        if hasattr(self, "check_btn"):
            self.check_btn.set_enabled(False)
        self._probe = threading.Thread(target=probe, daemon=True)
        self._probe.start()

        def finish():
            if self._probe.is_alive():
                self.root.after(100, finish)
                return
            self._probe = None
            text_value = result.get("text", "Radio preflight did not complete.")
            self.log(text_value)
            lowered = text_value.lower()
            tone = ("danger" if any(w in lowered for w in ("unreachable", "failed", "not currently", "no usb"))
                    else "success" if any(w in lowered for w in ("accepted", "is present", "available usb"))
                    else "info")
            self._notify_connection(text_value, tone)
            if hasattr(self, "check_btn") and self.check_btn.winfo_exists():
                self.check_btn.set_enabled(True)
        self.root.after(100, finish)

    def start(self):
        if self.supervisor and self.supervisor.running:
            return
        if self.demo.get():
            self.status.set("Demo running — no radio")
            self.log("DEMO: No radio messages are sent.")
            self.log(self.attitude_message())
            self.retry_count = 0
            return
        DATA.mkdir(parents=True, exist_ok=True)
        config = DATA / "config.ini" if getattr(sys, "frozen", False) else ROOT / "config.ini"
        if not config.exists() and getattr(sys, "frozen", False) and (ROOT / "example_config.ini").exists():
            shutil.copy2(ROOT / "example_config.ini", config)
            self.log("Created config.ini in ~/.empire-state-bbs. Review it before connecting a radio.")
        if not config.exists():
            messagebox.showerror("BBS configuration missing",
                                 "config.ini wasn't found. Copy example_config.ini to config.ini, "
                                 "edit it for your BBS, then start again.")
            return
        addr = self.address.get().strip()
        error = self.validate_radio_target()
        if error:
            self.status.set("Connection settings invalid")
            self.log("Connection validation: " + error)
            messagebox.showerror("Check the radio connection", error)
            return
        if self.connection.get() == "serial" and not addr:
            self.log("No USB port selected; Meshtastic will try automatic discovery.")
        self.save()
        cmd = ([sys.executable, "--sitdown-server"] if getattr(sys, "frozen", False)
               else [sys.executable, "-u", str(ROOT / "server.py")]) + ["--config", str(config),
               "--interface-type", self.connection.get()]
        if addr:
            cmd += ["--host" if self.connection.get() == "tcp" else "--port", addr]
        try:
            self.supervisor = ServerSupervisor(cmd, DATA if getattr(sys, "frozen", False) else ROOT,
                                               env_overrides={"EMPIRE_ATTITUDE": self.attitude.get(),
                                                              "EMPIRE_DESKTOP_STORE": str(DATA / "desktop-state.db"),
                                                              "EMPIRE_NODE_DIRECTORY": str(DATA / "node-directory.db")})
            self.supervisor.start()
            self.status.set("Starting...")
            self.log("Starting The Empire State BBS...")
        except OSError as exc:
            self.supervisor = None
            self.status.set("Start failed")
            self.log(f"Start failed: {exc}")

    def poll(self):
        now = time.monotonic()
        if now - self.last_serial_scan >= 5.0:
            self.last_serial_scan = now
            self.watch_serial_ports()
        monitor = self.supervisor
        self.refresh_radio_health()
        if hasattr(self, "node_count") and now - getattr(self, "last_node_count", 0.0) >= 5.0:
            self.refresh_node_count()
        if monitor:
            for kind, value in monitor.poll_events():
                if kind == "log":
                    self.log(value)
                elif kind == "state":
                    self.status.set(value)
                elif kind == "radio_nodes":
                    self.radio_node_count = value
                    if hasattr(self, "node_count"):
                        self.refresh_node_count()
            if monitor.running:
                self.uptime.set(f"Uptime: {monitor.uptime_seconds()}s")
            elif not monitor.stopping and self.restart_at is None:
                code = monitor.process.poll() if monitor.process else "unknown"
                self.log(f"BBS process exited ({code}).")
                self.status.set("Disconnected / process stopped")
                if self.auto_restart.get() and not self.demo.get() and not self.waiting_for_usb:
                    self.retry_count += 1
                    delay = min(60, 2 ** min(self.retry_count, 6))
                    self.restart_at = time.monotonic() + delay
                    self.status.set(f"Radio unavailable — recovery in {delay}s (attempt {self.retry_count})")
                    self.log(f"Retrying process in {delay}s (attempt {self.retry_count}).")
                self.supervisor = None
        if self.restart_at is not None:
            if not self.auto_restart.get() or self.demo.get():
                self.restart_at = None
            elif time.monotonic() >= self.restart_at and not self.waiting_for_usb:
                self.restart_at = None
                self.start()
        if self.root.winfo_exists():
            self.root.after(500, self.poll)

    def stop(self):
        self.waiting_for_usb = False
        self.restart_at = None
        self.retry_count = 0
        if self.supervisor:
            self.supervisor.stop()
            self.supervisor = None
        self.status.set("Stopped")
        self.uptime.set("Uptime: 0s")
        self.log("Server stopped.")

    # ---------------------------------------------------- node directory
    def open_node_directory(self):
        """The app's own node list beside its other data; None if the file cannot be opened."""
        try:
            directory = NodeDirectory(DATA / "node-directory.db")
        except (OSError, sqlite3.Error):
            return None
        try:
            # Nodes heard before the directory existed lived in desktop-state.db; copy them once.
            directory.import_legacy(self.store.records("nodes"))
        except (OSError, sqlite3.Error, AttributeError):
            pass
        return directory

    def saved_node_count(self):
        directory = getattr(self, "node_directory", None)
        if directory is None:
            return 0
        try:
            return directory.count()
        except (OSError, sqlite3.Error):
            return 0

    def refresh_node_count(self):
        """'Known nodes: N (radio holds M)': the suffix appears only when this Mac knows more than the radio."""
        self.last_node_count = time.monotonic()
        self.node_count.set(count_label(self.saved_node_count(), getattr(self, "radio_node_count", None)))

    def backup(self):
        DATA.mkdir(parents=True, exist_ok=True)
        destination = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".zip",
            initialfile=time.strftime("empire-state-backup-%Y-%m-%d.zip"),
            filetypes=[("Zip archive", "*.zip")])
        if not destination:
            return
        # Include the packaged app's data folder as well as a source checkout.
        candidates = [CONFIG, DATA / "config.ini", ROOT / "config.ini",
                      *sorted(DATA.glob("*.db")), *sorted(ROOT.glob("*.db"))]
        written = set()
        try:
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in candidates:
                    if path.exists() and path.is_file() and path.name not in written:
                        archive.write(path, path.name)
                        written.add(path.name)
        except (OSError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Backup failed", str(exc), parent=self.root)
            return
        self.log(f"Backup saved ({len(written)} files). Treat backups as sensitive; "
                 "they may contain messages and keys.")
        self.toast(f"Backup saved — {len(written)} file{'s' if len(written) != 1 else ''}")

    def restore_destination(self, name):
        """A source checkout keeps its server files beside the code; the packaged app uses DATA."""
        beside_code = ROOT / name
        if not getattr(sys, "frozen", False) and name != CONFIG.name and beside_code.exists():
            return beside_code
        return DATA / name

    def server_running(self):
        return bool(self.supervisor and self.supervisor.running)

    def restore(self):
        stop_first = "Stop the BBS server before restoring, so it isn't using the files being replaced."
        if self.server_running():
            messagebox.showerror("Stop the server first", stop_first, parent=self.root)
            return
        source = filedialog.askopenfilename(parent=self.root, filetypes=[("Zip archive", "*.zip")])
        if not source:
            return
        try:
            names = inspect_backup(source)
        except BackupError as exc:
            messagebox.showerror("Can't restore this backup", str(exc), parent=self.root)
            return
        if not messagebox.askyesno(
                "Replace current data?",
                "These files will be replaced:\n\n" + "\n".join(names) +
                "\n\nA safety copy of your current versions is saved in the data folder first. "
                "Files not listed are left alone.", parent=self.root, icon="warning"):
            return
        if self.server_running():
            messagebox.showerror("Stop the server first", stop_first, parent=self.root)
            return
        try:
            result = restore_backup(source, self.restore_destination, DATA)
        except BackupError as exc:
            messagebox.showerror("Restore failed", str(exc), parent=self.root)
            return
        self.load()
        self.refresh_node_count()
        saved = f" Safety copy: {result.safety_copy.name}." if result.safety_copy else ""
        self.log(f"Restored {len(result.names)} files from backup.{saved}")
        messagebox.showinfo("Backup restored",
                            f"Restored {len(result.names)} files.{saved}\n\n"
                            "Quit and reopen the app to finish loading the restored data.",
                            parent=self.root)

    def close(self):
        running = bool(self.supervisor and self.supervisor.running)
        if running and not messagebox.askyesno(
                "Quit and stop the BBS?",
                "The BBS server is running. Quitting stops it, and mesh users won't "
                "reach the BBS until you start it again.", parent=self.root, icon="warning"):
            return
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        # Launch the *packaged* GUI in CI, not just the source Python module.
        root = tk.Tk()
        root.withdraw()
        app = SitdownApp(root)
        app.show_messages()
        app.show_bulletin()
        app.show_locals()
        for key, *_rest in NAV + NAV_FOOTER:
            app.navigate(key)
        root.update_idletasks()
        if not app.output.winfo_exists():
            raise RuntimeError("Dashboard failed to initialize")
        print("EMPIRE_GUI_SMOKE_OK", flush=True)
        root.destroy()
        sys.exit(0)
    if "--sitdown-server" in sys.argv:
        sys.argv.remove("--sitdown-server")
        import server  # Ensures PyInstaller includes the server and its dependencies.
        runpy.run_module("server", run_name="__main__")
        sys.exit(0)
    root = tk.Tk()
    SitdownApp(root)
    root.mainloop()
