
import ctypes
import math
import os
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import pymem
from ctypes import wintypes

PROCESS_NAME = "Morrowind.exe"

PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
MEM_COMMIT = 0x1000

READABLE = {
    0x02,  # PAGE_READONLY
    0x04,  # PAGE_READWRITE
    0x08,  # PAGE_WRITECOPY
    0x20,  # PAGE_EXECUTE_READ
    0x40,  # PAGE_EXECUTE_READWRITE
    0x80,  # PAGE_EXECUTE_WRITECOPY
}

# Relative offsets from the verified player structure layout
STRUCT_OFFSETS = {
    "Attributes": {
        "Health_Max": 0x2B8,
        "Health": 0x2BC,
        "Mana_Max": 0x2C4,
        "Mana": 0x2C8,
        "Stamina_Max": 0x2DC,
        "Stamina": 0x2E0,
        "Max_Inventory_Space": 0x2D0,
        "Inventory_Space": 0x2D4,
    },
    "Stats": {
        "Strength_Base": 0x258,
        "Strength_Current": 0x25C,
        "Intelligence_Base": 0x268,
        "Intelligence_Current": 0x264,
        "Willpower_Base": 0x274,
        "Willpower_Current": 0x270,
        "Agility_Base": 0x280,
        "Agility_Current": 0x27C,
        "Speed_Base": 0x28C,
        "Speed_Current": 0x288,
        "Endurance_Base": 0x298,
        "Endurance_Current": 0x294,
        "Personality_Base": 0x2A4,
        "Personality_Current": 0x2A0,
        "Luck_Base": 0x2B0,
        "Luck_Current": 0x2AC,
    },
    "Skills (Misc)": {
        "Block_Base": 0x3B8,
        "Block_Current": 0x3B4,
        "Armorer_Base": 0x3C8,
        "Armorer_Current": 0x3C4,
        "Medium_Armor_Base": 0x3D8,
        "Medium_Armor_Current": 0x3D4,
        "Heavy_Armor_Base": 0x3E8,
        "Heavy_Armor_Current": 0x3E4,
        "Blunt_Weapon_Base": 0x3F8,
        "Blunt_Weapon_Current": 0x3F4,
    },
    "Skills (Minor)": {
        "Athletics_Base": 0x438,
        "Athletics_Current": 0x434,
        "Marksman_Base": 0x528,
        "Marksman_Current": 0x524,
        "Speechcraft_Base": 0x538,
        "Speechcraft_Current": 0x534,
        "Hand_to_Hand_Base": 0x548,
        "Hand_to_Hand_Current": 0x544,
        "Mercantile_Base": 0x558,
        "Mercantile_Current": 0x554,
    },
    "Skills (Major)": {
        "Security_Base": 0x4D8,
        "Security_Current": 0x4D4,
        "Sneak_Base": 0x4E8,
        "Sneak_Current": 0x4E4,
        "Acrobatics_Base": 0x4F8,
        "Acrobatics_Current": 0x4F4,
        "Light_Armor_Base": 0x508,
        "Light_Armor_Current": 0x504,
        "Short_Blade_Base": 0x518,
        "Short_Blade_Current": 0x514,
    },
}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


VirtualQueryEx = kernel32.VirtualQueryEx
VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
VirtualQueryEx.restype = ctypes.c_size_t

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
ReadProcessMemory.restype = wintypes.BOOL


def sane_float(v: float) -> bool:
    return math.isfinite(v) and -1_000_000.0 < v < 1_000_000.0


def read_region(handle, addr: int, size: int):
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return buf.raw[:read.value]


def iter_regions(handle, max_addr=0x7FFFFFFF):
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    while addr < max_addr:
        result = VirtualQueryEx(
            handle,
            ctypes.c_void_p(addr),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if not result:
            break

        base = mbi.BaseAddress or 0
        size = int(mbi.RegionSize or 0)
        protect = int(mbi.Protect or 0)
        state = int(mbi.State or 0)

        if size > 0:
            if state == MEM_COMMIT and protect in READABLE and not (protect & PAGE_GUARD) and protect != PAGE_NOACCESS:
                yield base, size
            next_addr = base + size
            addr = next_addr if next_addr > addr else addr + 0x1000
        else:
            addr += 0x1000


def find_player_struct(pm, hp, hpmax, mana, manamax, stam, stammax, progress_callback=None):
    handle = pm.process_handle
    hits = []

    current_tol = 2.0
    max_tol = 2.0

    regions = list(iter_regions(handle))
    total_regions = len(regions)
    update_interval = max(1, total_regions // 50)

    OFFSET_HP_MAX = 0x2B8 // 4
    OFFSET_HP_CUR = 0x2BC // 4
    OFFSET_MANA_MAX = 0x2C4 // 4
    OFFSET_MANA_CUR = 0x2C8 // 4
    OFFSET_ST_MAX = 0x2DC // 4
    OFFSET_ST_CUR = 0x2E0 // 4
    MIN_FLOATS = 0x2E4 // 4

    for region_index, (region_base, region_size) in enumerate(regions):
        if progress_callback is not None and (
            region_index % update_interval == 0 or region_index == total_regions - 1
        ):
            progress_callback(region_index, total_regions)

        time.sleep(0)

        data = read_region(handle, region_base, region_size)
        if not data or len(data) <= 0x2E4:
            continue

        arr = np.frombuffer(data, dtype=np.float32)
        n = len(arr) - MIN_FLOATS
        if n <= 0:
            continue

        hp_max_v = arr[OFFSET_HP_MAX:OFFSET_HP_MAX + n]
        hp_cur_v = arr[OFFSET_HP_CUR:OFFSET_HP_CUR + n]
        mana_max_v = arr[OFFSET_MANA_MAX:OFFSET_MANA_MAX + n]
        mana_cur_v = arr[OFFSET_MANA_CUR:OFFSET_MANA_CUR + n]
        st_max_v = arr[OFFSET_ST_MAX:OFFSET_ST_MAX + n]
        st_cur_v = arr[OFFSET_ST_CUR:OFFSET_ST_CUR + n]

        valid = (
            np.isfinite(hp_max_v) & np.isfinite(hp_cur_v)
            & np.isfinite(mana_max_v) & np.isfinite(mana_cur_v)
            & np.isfinite(st_max_v) & np.isfinite(st_cur_v)
        )
        valid &= (hp_max_v > -1e6) & (hp_max_v < 1e6) & (hp_cur_v > -1e6) & (hp_cur_v < 1e6)
        valid &= (mana_max_v > -1e6) & (mana_max_v < 1e6) & (mana_cur_v > -1e6) & (mana_cur_v < 1e6)
        valid &= (st_max_v > -1e6) & (st_max_v < 1e6) & (st_cur_v > -1e6) & (st_cur_v < 1e6)

        valid &= np.abs(hp_cur_v - hp) <= current_tol
        valid &= np.abs(hp_max_v - hpmax) <= max_tol
        valid &= np.abs(mana_cur_v - mana) <= current_tol
        valid &= np.abs(mana_max_v - manamax) <= max_tol
        valid &= np.abs(st_cur_v - stam) <= current_tol
        valid &= np.abs(st_max_v - stammax) <= max_tol

        match_indices = np.where(valid)[0]
        for j in match_indices:
            struct_base = region_base + int(j) * 4
            vals = [
                float(hp_max_v[j]),
                float(hp_cur_v[j]),
                float(mana_max_v[j]),
                float(mana_cur_v[j]),
                float(st_max_v[j]),
                float(st_cur_v[j]),
            ]
            hits.append((struct_base, vals))

    if progress_callback is not None:
        progress_callback(total_regions, total_regions)

    return hits


class StartupDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Find Player Base")
        self.resizable(False, False)
        self.result = None

        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Enter the whole-number values currently shown in the game UI.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        fields = [
            ("Health current", "hp"),
            ("Health max", "hpmax"),
            ("Mana current", "mana"),
            ("Mana max", "manamax"),
            ("Stamina current", "stam"),
            ("Stamina max", "stammax"),
        ]

        self.vars = {}
        vcmd = (self.register(self._validate_int), "%P")

        for row, (label, key) in enumerate(fields, start=1):
            ttk.Label(frame, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            var = tk.StringVar()
            ent = ttk.Entry(frame, textvariable=var, width=14, validate="key", validatecommand=vcmd)
            ent.grid(row=row, column=1, sticky="w", pady=3)
            self.vars[key] = var

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))

        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Scan", command=self._submit).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(50, self._center)

    def _validate_int(self, proposed):
        return proposed == "" or proposed.isdigit()

    def _submit(self):
        try:
            self.result = {
                "hp": int(self.vars["hp"].get()),
                "hpmax": int(self.vars["hpmax"].get()),
                "mana": int(self.vars["mana"].get()),
                "manamax": int(self.vars["manamax"].get()),
                "stam": int(self.vars["stam"].get()),
                "stammax": int(self.vars["stammax"].get()),
            }
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter whole numbers for all six values.", parent=self)
            return

        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def _center(self):
        self.update_idletasks()
        parent = self.master
        x = parent.winfo_rootx() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_rooty() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class TrainerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Morrowind GOTY Memory Editor")
        self.root.withdraw()
        self.root.geometry("600x600")
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 600) // 2
        y = (sh - 600) // 2
        self.root.geometry(f"600x600+{x}+{y}")
        self.root.deiconify()

        self.pm = None
        self.player_base = None
        self.current_scan_values = None
        self.value_widgets = {}
        self.scan_thread = None

        self.status_var = tk.StringVar(value="Not connected")
        self.base_var = tk.StringVar(value="Not found")
        self.scan_status_var = tk.StringVar(value="Click 'Find Player Base' to begin.")
        self.progress_var = tk.StringVar(value="")

        self._build_ui()
        self._connect()

    def _build_ui(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        menubar.add_command(label="Instructions", command=self._show_instructions)
        menubar.add_command(label="About", command=self._show_about)

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=(0, 16))
        ttk.Label(top, text="Player Base:").pack(side="left")
        ttk.Label(top, textvariable=self.base_var).pack(side="left", padx=(4, 16))
        ttk.Button(top, text="Find Player Base", command=self.begin_find_player_base).pack(side="left", padx=4)
        ttk.Button(top, text="Refresh All", command=self.refresh_all).pack(side="left", padx=4)

        scan_frame = ttk.LabelFrame(self.root, text="Scanner", padding=10)
        scan_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(scan_frame, textvariable=self.scan_status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(scan_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 4))
        ttk.Label(scan_frame, textvariable=self.progress_var).pack(anchor="w")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for category, fields in STRUCT_OFFSETS.items():
            frame = ttk.Frame(self.notebook, padding=10)
            self.notebook.add(frame, text=category)

            canvas = tk.Canvas(frame, highlightthickness=0)
            scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas)

            inner.bind(
                "<Configure>",
                lambda e, c=canvas: c.configure(scrollregion=c.bbox("all"))
            )

            canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            header = ttk.Frame(inner)
            header.pack(fill="x", pady=(0, 6))
            ttk.Label(header, text="Field", width=28).grid(row=0, column=0, sticky="w")
            ttk.Label(header, text="Current", width=12).grid(row=0, column=1, sticky="w")
            ttk.Label(header, text="New Value", width=12).grid(row=0, column=2, sticky="w")
            ttk.Label(header, text="", width=10).grid(row=0, column=3, sticky="w")

            self.value_widgets[category] = {}
            vcmd = (self.root.register(self._validate_int), "%P")

            for field_name, offset in fields.items():
                row = ttk.Frame(inner)
                row.pack(fill="x", pady=2)

                ttk.Label(row, text=field_name, width=28).grid(row=0, column=0, sticky="w")
                current_var = tk.StringVar(value="?")
                ttk.Label(row, textvariable=current_var, width=12).grid(row=0, column=1, sticky="w")

                entry_var = tk.StringVar(value="")
                entry = ttk.Entry(row, textvariable=entry_var, width=12, validate="key", validatecommand=vcmd)
                entry.grid(row=0, column=2, sticky="w", padx=(0, 8))

                btn = ttk.Button(row, text="Apply", command=lambda c=category, f=field_name: self.apply_value(c, f))
                btn.grid(row=0, column=3, sticky="w")

                self.value_widgets[category][field_name] = {
                    "current_var": current_var,
                    "entry_var": entry_var,
                    "offset": offset,
                    "entry": entry,
                    "button": btn,
                }

            bottom = ttk.Frame(inner)
            bottom.pack(fill="x", pady=(10, 0))
            ttk.Button(bottom, text=f"Refresh {category}", command=lambda c=category: self.refresh_category(c)).pack(side="left")

        self.set_tabs_enabled(False)

    def _validate_int(self, proposed):
        return proposed == "" or proposed.isdigit()

    def _show_instructions(self):
        instructions_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instructions.txt")
        win = tk.Toplevel(self.root)
        win.title("Instructions")
        win.transient(self.root)
        win.geometry("500x280")
        win.resizable(True, True)
        text = tk.Text(win, wrap="word", padx=12, pady=12, font=("TkDefaultFont", 10))
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        try:
            with open(instructions_path, "r", encoding="utf-8") as f:
                text.insert("1.0", f.read())
        except OSError:
            text.insert("1.0", "Could not load instructions.txt")
        text.configure(state="disabled")
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - win.winfo_width()) // 2
        y = (sh - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    def _show_about(self):
        win = tk.Toplevel(self.root)
        win.title("About")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Morrowind GOTY Memory Editor", font=("TkDefaultFont", 12, "bold")).pack(pady=(0, 10))
        ttk.Label(frame, text="Version agnostic, should work with any Morrowind version.").pack(pady=(0, 8))
        ttk.Label(frame, text="~ coded by non-npc").pack(pady=(0, 0))
        ttk.Button(frame, text="OK", command=win.destroy).pack(pady=(16, 0))
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    def _connect(self):
        try:
            self.pm = pymem.Pymem(PROCESS_NAME)
            self.status_var.set(f"Connected to {PROCESS_NAME}")
        except Exception as e:
            self.pm = None
            self.status_var.set("Not connected")
            messagebox.showerror("Connection Error", f"Could not attach to {PROCESS_NAME}:\n{e}")

    def set_tabs_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for category, fields in self.value_widgets.items():
            for info in fields.values():
                info["entry"].configure(state=state)
                info["button"].configure(state=state)

    def begin_find_player_base(self):
        if not self.pm:
            self._connect()
            if not self.pm:
                return

        dialog = StartupDialog(self.root)
        self.root.wait_window(dialog)

        if not dialog.result:
            return

        self.current_scan_values = dialog.result
        self.scan_status_var.set("Scanning memory for player structure...")
        self.progress_var.set(
            f"Searching with values: HP {dialog.result['hp']}/{dialog.result['hpmax']}, "
            f"Mana {dialog.result['mana']}/{dialog.result['manamax']}, "
            f"Stamina {dialog.result['stam']}/{dialog.result['stammax']}"
        )
        self.progress["value"] = 0
        self.set_tabs_enabled(False)

        self.scan_thread = threading.Thread(target=self._scan_worker, args=(dialog.result,), daemon=True)
        self.scan_thread.start()

    def _update_progress(self, current, total):
        if total > 0:
            pct = int(100 * current / total)
            self.progress["value"] = pct
            region_display = min(current + 1, total)
            self.progress_var.set(
                f"Scanning region {region_display} of {total} ({pct}%)"
            )

    def _scan_worker(self, values):
        def progress_callback(current, total):
            self.root.after(0, lambda: self._update_progress(current, total))

        try:
            hits = find_player_struct(
                self.pm,
                values["hp"],
                values["hpmax"],
                values["mana"],
                values["manamax"],
                values["stam"],
                values["stammax"],
                progress_callback=progress_callback,
            )
            self.root.after(0, lambda: self._scan_complete(hits))
        except Exception as e:
            self.root.after(0, lambda: self._scan_failed(str(e)))

    def _scan_complete(self, hits):
        self.progress["value"] = 100
        if not hits:
            self.player_base = None
            self.base_var.set("Not found")
            self.scan_status_var.set("No player base found.")
            self.progress_var.set("Try again with current on-screen values from the game.")
            self.set_tabs_enabled(False)
            messagebox.showwarning(
                "Not Found",
                "No matching player structure was found.\n\n"
                "Use the whole-number values currently shown in the game and try again.",
            )
            return

        self.player_base = hits[0][0]
        self.base_var.set(f"0x{self.player_base:08X}")
        hp_max_v, hp_cur_v, mana_max_v, mana_cur_v, st_max_v, st_cur_v = hits[0][1]
        self.scan_status_var.set("Player base found.")
        self.progress_var.set(
            f"Matched base 0x{self.player_base:08X}  "
            f"HP={int(round(hp_cur_v))}/{int(round(hp_max_v))}  "
            f"Mana={int(round(mana_cur_v))}/{int(round(mana_max_v))}  "
            f"Stamina={int(round(st_cur_v))}/{int(round(st_max_v))}"
        )
        self.set_tabs_enabled(True)
        self.refresh_all()

    def _scan_failed(self, error_text):
        self.progress["value"] = 0
        self.player_base = None
        self.base_var.set("Not found")
        self.scan_status_var.set("Scan failed.")
        self.progress_var.set("")
        self.set_tabs_enabled(False)
        messagebox.showerror("Scan Error", error_text)

    def _read_value(self, offset: int):
        if not self.pm or self.player_base is None:
            raise RuntimeError("Player base is not available.")
        return self.pm.read_float(self.player_base + offset)

    def _write_value(self, offset: int, new_int_value: int):
        if not self.pm or self.player_base is None:
            raise RuntimeError("Player base is not available.")

        hp_max = self.pm.read_float(self.player_base + 0x2B8)
        hp_cur = self.pm.read_float(self.player_base + 0x2BC)
        mana_max = self.pm.read_float(self.player_base + 0x2C4)
        mana_cur = self.pm.read_float(self.player_base + 0x2C8)

        if not all(sane_float(v) for v in [hp_max, hp_cur, mana_max, mana_cur]):
            raise RuntimeError("Player base appears stale or invalid.")

        addr = self.player_base + offset
        current_bytes = self.pm.read_bytes(addr, 4)
        new_bytes = struct.pack("<f", float(int(new_int_value)))
        if len(new_bytes) != len(current_bytes):
            raise RuntimeError(
                f"Write size mismatch: replacement ({len(new_bytes)} bytes) must equal "
                f"original ({len(current_bytes)} bytes)."
            )
        self.pm.write_bytes(addr, new_bytes, len(new_bytes))

    def refresh_category(self, category: str):
        if not self.pm or self.player_base is None:
            return
        for field_name, info in self.value_widgets[category].items():
            try:
                raw = self._read_value(info["offset"])
                whole = int(round(raw))
                info["current_var"].set(str(whole))
            except Exception:
                info["current_var"].set("ERR")

    def refresh_all(self):
        if not self.pm or self.player_base is None:
            return
        for category in self.value_widgets.keys():
            self.refresh_category(category)

    def apply_value(self, category: str, field_name: str):
        info = self.value_widgets[category][field_name]
        raw_text = info["entry_var"].get().strip()

        if raw_text == "":
            messagebox.showwarning("Missing Value", f"Enter a whole number for {field_name}.")
            return

        try:
            new_value = int(raw_text)
        except ValueError:
            messagebox.showerror("Invalid Value", "Only whole numbers are allowed.")
            return

        try:
            self._write_value(info["offset"], new_value)
            self.refresh_category(category)
            info["entry_var"].set("")
        except Exception as e:
            messagebox.showerror("Write Error", str(e))


def main():
    root = tk.Tk()
    app = TrainerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
