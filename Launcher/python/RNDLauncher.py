# -*- coding: utf-8 -*-
import os, json, glob, shlex, subprocess, threading, sys, traceback
import customtkinter as ctk
import tkinter as tk  # <-- ใช้ตั้ง iconphoto/iconbitmap
from tkinter import filedialog, messagebox
from queue import Queue

# --- crash logger ---
def _excepthook(exctype, value, tb):
    with open("launcher_error.log", "w", encoding="utf-8") as f:
        traceback.print_exception(exctype, value, tb, file=f)
sys.excepthook = _excepthook

# ===================== Config =====================
CONFIG_FILE = "ue_gitaware_launcher_config.json"
CHECK_INTERVAL_MS = 60000  # auto-check Git interval (ms)

# ---- UE-like palette ----
UE_BG          = "#1b1b1c"
UE_PANEL       = "#202225"
UE_BTN         = "#2a2e33"
UE_BTN_HOVER   = "#32363c"
UE_BORDER      = "#3b4046"
UE_TEXT        = "#e6e6e6"
UE_TEXT_MUTED  = "#a4a9ae"
UE_ACCENT      = "#2ea3ff"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")
ctk.set_widget_scaling(1.0)

# ===================== Utils =====================
def resource_path(relative):
    """รองรับทั้งรันจากโค้ดและจากไฟล์ .exe (PyInstaller)"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.abspath("."), relative)

def set_window_icon(win, ico_rel="assets/logo.ico", png_rel="assets/logo.png"):
    """ตั้งโลโก้กลมสำหรับ Titlebar/Taskbar"""
    ico = resource_path(ico_rel)
    png = resource_path(png_rel)
    try:
        if os.name == "nt" and os.path.isfile(ico):
            win.iconbitmap(ico)  # Taskbar (Windows)
        if os.path.isfile(png):
            win.iconphoto(True, tk.PhotoImage(file=png))  # Titlebar (ทุกแพลตฟอร์ม)
    except Exception:
        pass

def run_cmd(cmd, cwd=None):
    try:
        if isinstance(cmd, str):
            cmd_list = cmd if os.name == "nt" else shlex.split(cmd)
        else:
            cmd_list = cmd
        p = subprocess.Popen(cmd_list, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        return p.returncode, (out or "").strip(), (err or "").strip()
    except Exception as e:
        return 1, "", str(e)

def log_append(textbox: ctk.CTkTextbox, text: str):
    """Thread-safe UI update."""
    def _do():
        try:
            textbox.configure(state="normal")
            textbox.insert("end", text + "\n")
            textbox.see("end")
            textbox.configure(state="disabled")
        except Exception:
            pass
    try:
        textbox.after(0, _do)
    except Exception:
        _do()

def save_settings(state_like):
    data = {
        "engine_dir": state_like["engine_dir"].get(),
        "uproject": state_like["uproject"].get(),
        "autobuild": state_like["autobuild"].get(),
        "autogen": state_like["autogen"].get(),
        "auto_check": state_like["auto_check"].get(),
        "ubt": state_like["ubt"].get(),
        "editor": state_like["editor"].get()
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {
        "engine_dir": "",
        "uproject": "",
        "autobuild": False,
        "autogen": True,
        "auto_check": False,
        "ubt": "",
        "editor": ""
    }

def find_file_case_insensitive(root, patterns):
    for pat in patterns:
        hits = glob.glob(os.path.join(root, pat), recursive=True)
        if hits:
            hits.sort(key=len)
            return hits[0]
    return None

def autodetect_tools(engine_dir):
    ubt = editor = None
    if not engine_dir or not os.path.isdir(engine_dir):
        return None, None
    ubt = find_file_case_insensitive(engine_dir, [
        "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.exe",
        "Engine/Binaries/DotNET/UnrealBuildTool.exe",
        "Engine/Binaries/DotNET/**/UnrealBuildTool.exe",
    ])
    editor = find_file_case_insensitive(engine_dir, [
        "Engine/Binaries/Win64/UnrealEditor.exe",
        "Engine/**/Win64/UnrealEditor.exe",
    ])
    return ubt, editor

def get_repo_root_from_uproject(uproject_path):
    if not uproject_path:
        return None
    d = os.path.abspath(os.path.dirname(uproject_path))
    for _ in range(10):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.abspath(os.path.join(d, ".."))
        if parent == d: break
        d = parent
    return None

# ===================== Git Helpers =====================
def git_fetch_all(repo, logbox):
    rc, out, err = run_cmd(["git", "fetch", "--all", "--prune"], cwd=repo)
    if out: log_append(logbox, out)
    if rc != 0: log_append(logbox, err or "git fetch failed")
    return rc == 0

def git_current_branch(repo):
    rc, out, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return out if rc == 0 else None

def git_has_remote(repo, name):
    rc, out, _ = run_cmd(["git", "remote"], cwd=repo)
    return (rc == 0) and (name in out.splitlines())

def git_default_remote_branch(repo, remote):
    rc, out, _ = run_cmd(["git", "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD"], cwd=repo)
    if rc == 0 and out: return out
    rc, out, _ = run_cmd(["git", "remote", "show", remote], cwd=repo)
    if rc == 0:
        for line in out.splitlines():
            s = line.strip().lower()
            if s.startswith("head branch:"):
                head = line.split(":",1)[1].strip()
                return f"{remote}/{head}"
    return None

def git_ahead_behind(repo, left, right):
    rc, out, _ = run_cmd(["git", "rev-list", "--left-right", "--count", f"{left}...{right}"], cwd=repo)
    if rc != 0 or not out: return None, None
    try:
        behind, ahead = out.split()
        return int(behind), int(ahead)
    except: return None, None

def git_list_commits(repo, rev_range, limit=5):
    rc, out, _ = run_cmd(["git", "log", "--oneline", rev_range, f"-n{limit}"], cwd=repo)
    return out.splitlines() if rc == 0 and out else []

def do_pull(repo, remote, branch, logbox):
    rc, out, err = run_cmd(["git", "pull", "--rebase", remote, branch], cwd=repo)
    if out: log_append(logbox, out)
    if rc != 0: log_append(logbox, err or "git pull failed")
    return rc == 0

def do_push(repo, remote, branch, logbox):
    rc, out, err = run_cmd(["git", "push", remote, branch], cwd=repo)
    if out: log_append(logbox, out)
    if rc != 0: log_append(logbox, err or "git push failed")
    return rc == 0

# ===================== Build / Open =====================
def generate_project_files(ubt_path, uproject, logbox):
    cmd = [ubt_path, "-ProjectFiles", f"-Project={uproject}", "-game", "-engine"]
    log_append(logbox, " ".join(cmd))
    rc, out, err = run_cmd(cmd, cwd=os.path.dirname(uproject))
    if out: log_append(logbox, out)
    if rc != 0: log_append(logbox, err or "Generate Project Files failed")
    return rc == 0

def build_editor(ubt_path, uproject, logbox):
    proj_name = os.path.splitext(os.path.basename(uproject))[0]
    cmd = [ubt_path, f"{proj_name}Editor", "Win64", "Development",
           f"-Project={uproject}", "-WaitMutex", "-NoHotReloadFromIDE"]
    log_append(logbox, " ".join(cmd))
    rc, out, err = run_cmd(cmd, cwd=os.path.dirname(uproject))
    if out: log_append(logbox, out)
    if rc != 0: log_append(logbox, err or "Build Editor failed")
    return rc == 0

def open_project(editor_exe, uproject, logbox):
    if not os.path.isfile(editor_exe):
        log_append(logbox, "UnrealEditor.exe not found")
        return False
    cmd = [editor_exe, uproject]
    log_append(logbox, " ".join(cmd))
    try:
        subprocess.Popen(cmd, cwd=os.path.dirname(uproject))
        return True
    except Exception as e:
        log_append(logbox, f"Failed to launch editor: {e}")
        return False

# ===================== UI Components =====================
def big_button(master, text, cmd):
    return ctk.CTkButton(
        master, text=text, command=cmd,
        fg_color=UE_BTN, hover_color=UE_BTN_HOVER, text_color=UE_TEXT,
        corner_radius=10, border_width=1, border_color=UE_BORDER,
        height=40, font=ctk.CTkFont(size=13, weight="bold")
    )

def small_button(master, text, cmd):
    return ctk.CTkButton(
        master, text=text, command=cmd,
        fg_color=UE_BTN, hover_color=UE_BTN_HOVER, text_color=UE_TEXT,
        corner_radius=10, border_width=1, border_color=UE_BORDER,
        height=34, font=ctk.CTkFont(size=12)
    )

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, state_like, on_saved):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("860x420")
        self.configure(fg_color=UE_PANEL)
        set_window_icon(self)  # << ไอคอนกลม
        self.grab_set()
        self.state_like = state_like
        self.on_saved = on_saved

        self.vars = {
            "engine_dir": ctk.StringVar(value=state_like["engine_dir"].get()),
            "uproject":   ctk.StringVar(value=state_like["uproject"].get()),
            "ubt":        ctk.StringVar(value=state_like["ubt"].get()),
            "editor":     ctk.StringVar(value=state_like["editor"].get()),
            "autogen":    ctk.BooleanVar(value=state_like["autogen"].get()),
            "autobuild":  ctk.BooleanVar(value=state_like["autobuild"].get()),
            "auto_check": ctk.BooleanVar(value=state_like["auto_check"].get()),
        }

        frm = ctk.CTkFrame(self, fg_color=UE_PANEL, border_color=UE_BORDER, border_width=1, corner_radius=12)
        frm.pack(fill="both", expand=True, padx=12, pady=12)
        frm.grid_columnconfigure(1, weight=1)

        row = 0
        def add_row(label, var, browse=None, placeholder=""):
            nonlocal row
            ctk.CTkLabel(frm, text=label, text_color=UE_TEXT_MUTED).grid(row=row, column=0, sticky="w", padx=12, pady=(10,0))
            entry = ctk.CTkEntry(frm, textvariable=var, placeholder_text=placeholder,
                                 fg_color=UE_BG, border_color=UE_BORDER, text_color=UE_TEXT, corner_radius=8)
            entry.grid(row=row, column=1, sticky="we", padx=8, pady=(10,0))
            if browse:
                small_button(frm, "Browse", browse).grid(row=row, column=2, padx=8, pady=(10,0))
            row += 1
            return entry

        add_row("Engine Folder (UE_5.4.x):", self.vars["engine_dir"],
                browse=self.browse_engine, placeholder="E:/UE_5.4")

        add_row(".uproject:", self.vars["uproject"],
                browse=self.browse_uproject, placeholder="E:/Project/MyProj.uproject")

        add_row("UnrealBuildTool.exe:", self.vars["ubt"], placeholder=".../UnrealBuildTool.exe")
        small_button(frm, "Detect", self.detect_tools).grid(row=row-1, column=2, padx=8, pady=(10,0))

        add_row("UnrealEditor.exe:", self.vars["editor"], placeholder=".../UnrealEditor.exe")

        toggles = ctk.CTkFrame(frm, fg_color=UE_PANEL)
        toggles.grid(row=row, column=0, columnspan=3, sticky="we", padx=8, pady=(10,0))
        row += 1
        for tvar, text in [
            (self.vars["autogen"],   "Generate Project Files ก่อนเปิด"),
            (self.vars["autobuild"], "Build Editor ก่อนเปิด"),
            (self.vars["auto_check"], f"Auto-check Git ทุก {CHECK_INTERVAL_MS//1000} วิ"),
        ]:
            sw = ctk.CTkSwitch(toggles, text=text, variable=tvar,
                               fg_color=UE_BORDER, progress_color=UE_ACCENT,
                               button_color=UE_BTN, button_hover_color=UE_BTN_HOVER,
                               text_color=UE_TEXT)
            sw.pack(side="left", padx=10)

        footer = ctk.CTkFrame(self, fg_color=UE_PANEL)
        footer.pack(fill="x", pady=(0,12))
        small_button(footer, "Save", self.save).pack(side="right", padx=(0,10))
        small_button(footer, "Cancel", self.destroy).pack(side="right", padx=10)

    # ---- actions ----
    def browse_engine(self):
        d = filedialog.askdirectory(title="Select UE Engine Folder")
        if d: self.vars["engine_dir"].set(d)

    def browse_uproject(self):
        f = filedialog.askopenfilename(title="Select .uproject", filetypes=[("Unreal Project", "*.uproject")])
        if f: self.vars["uproject"].set(f)

    def detect_tools(self):
        ubt, editor = autodetect_tools(self.vars["engine_dir"].get())
        if ubt: self.vars["ubt"].set(ubt)
        if editor: self.vars["editor"].set(editor)
        messagebox.showinfo("Detect", "ตรวจพบเครื่องมือแล้ว (ถ้ามี)")

    def save(self):
        for k, v in self.vars.items():
            self.state_like[k].set(v.get())
        save_settings(self.state_like)
        if self.on_saved: self.on_saved()
        self.destroy()

# ===================== App =====================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Rewind and Desires Launcher")  # << เปลี่ยนชื่อโปรแกรมที่ Titlebar
        self.geometry("900x560")
        self.configure(fg_color=UE_BG)
        set_window_icon(self)  # << โลโก้กลม

        # ---- shared ctx (อย่าใช้ชื่อ 'state') ----
        self.ctx = {
            "engine_dir": ctk.StringVar(),
            "uproject":   ctk.StringVar(),
            "autobuild":  ctk.BooleanVar(),
            "autogen":    ctk.BooleanVar(),
            "auto_check": ctk.BooleanVar(),
            "ubt":        ctk.StringVar(),
            "editor":     ctk.StringVar(),
            "bg_job":     None,
        }
        for k, v in load_settings().items():
            if k in self.ctx:
                if isinstance(self.ctx[k], ctk.BooleanVar):
                    self.ctx[k].set(bool(v))
                else:
                    self.ctx[k].set(v)

        # ---- top bar ----
        top = ctk.CTkFrame(self, fg_color=UE_PANEL, border_color=UE_BORDER, border_width=1, corner_radius=12)
        top.pack(fill="x", padx=12, pady=12)

        big_button(top, "Open Project", self.do_open_sequence).pack(side="left", padx=8, pady=8)
        big_button(top, "Check Git Now", self.do_check_now).pack(side="left", padx=8, pady=8)
        big_button(top, "Settings", self.open_settings).pack(side="right", padx=8, pady=8)

        # ---- log box ----
        self.log = ctk.CTkTextbox(self, fg_color=UE_PANEL, border_color=UE_BORDER,
                                  border_width=1, corner_radius=12, text_color=UE_TEXT)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0,12))
        self.log.configure(state="disabled")

        if not self.ctx["uproject"].get() or not self.ctx["editor"].get():
            self.after(300, self.open_settings)

        self.after(1000, self.schedule_auto_check)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- helpers ----------
    def call_in_main(self, func, *args, **kwargs):
        q = Queue(maxsize=1)
        def _wrap():
            try:
                q.put(func(*args, **kwargs))
            except Exception as e:
                q.put(e)
        self.after(0, _wrap)
        res = q.get()
        if isinstance(res, Exception):
            raise res
        return res

    def schedule_auto_check(self):
        if self.ctx["bg_job"]:
            try: self.after_cancel(self.ctx["bg_job"])
            except Exception: pass
            self.ctx["bg_job"] = None

        if self.ctx["auto_check"].get():
            def tick():
                threading.Thread(target=self._auto_check_task, daemon=True).start()
                self.ctx["bg_job"] = self.after(CHECK_INTERVAL_MS, tick)
            self.ctx["bg_job"] = self.after(CHECK_INTERVAL_MS, tick)

    def _auto_check_task(self):
        res = self.do_git_check(silent=True)
        if res.get("ok"):
            msgs = []
            if res["behind_upstream"] > 0:
                msgs.append(f"[Auto] ตามหลัง {res['upstream_target']} {res['behind_upstream']} commit")
            if res["ahead_origin"] > 0:
                msgs.append(f"[Auto] ยังไม่ได้ push {res['ahead_origin']} commit ไป {res['origin_target']}")
            if msgs:
                log_append(self.log, " / ".join(msgs))

    def open_settings(self):
        SettingsDialog(self, self.ctx, self.schedule_auto_check)

    # ---------- git check + open ----------
    def do_check_now(self):
        def _task():
            log_append(self.log, "=== Git Check ===")
            res = self.do_git_check()
            if not res.get("ok"):
                log_append(self.log, "Git check ไม่สำเร็จ")
                return
            msg = []
            if res["behind_upstream"] > 0:
                msg.append(f"- คุณตามหลัง {res['upstream_target']} อยู่ {res['behind_upstream']} commit")
                for l in res["behind_list"]: msg.append(f"    {l}")
            else:
                msg.append("- ไม่มี commit ใหม่จาก upstream")

            if res["ahead_origin"] > 0:
                msg.append(f"- คุณนำหน้า {res['origin_target']} อยู่ {res['ahead_origin']} commit (ยังไม่ได้ push)")
                for l in res["ahead_list"]: msg.append(f"    {l}")
            else:
                msg.append("- ไม่มี commit ค้างที่ยังไม่ได้ push")

            self.call_in_main(messagebox.showinfo, "Git Status", "\n".join(msg))
        threading.Thread(target=_task, daemon=True).start()

    def do_open_sequence(self):
        def _task():
            res = self.do_git_check()
            if not res.get("ok"):
                self.call_in_main(messagebox.showwarning, "Git", "เช็ค Git ไม่ได้ จะเปิดโปรเจกต์ต่อไป")
                self._continue_open()
                return

            need_warn = (res["behind_upstream"] > 0) or (res["ahead_origin"] > 0)
            if not need_warn:
                self._continue_open()
                return

            lines = [f"Branch: {res['branch']}"]
            if res["behind_upstream"] > 0:
                lines.append(f"\nตามหลัง {res['upstream_target']} {res['behind_upstream']} commit:")
                lines += [f"  {l}" for l in res["behind_list"]]
            if res["ahead_origin"] > 0:
                lines.append(f"\nยังไม่ได้ push ไป {res['origin_target']} {res['ahead_origin']} commit:")
                lines += [f"  {l}" for l in res["ahead_list"]]

            ans = self.call_in_main(
                messagebox.askyesnocancel,
                "Git Sync Warning",
                "\n".join(lines) + "\n\nYes=Pull, No=Continue, Cancel=หยุด"
            )
            if ans is True:
                target = res["upstream_target"] or res["origin_target"]
                remote = "upstream" if res["upstream_target"] else "origin"
                branch = target.split("/",1)[1] if "/" in target else target
                log_append(self.log, f"Pull --rebase จาก {remote} {branch}")
                do_pull(res["repo"], remote, branch, self.log)
                self._continue_open()
            elif ans is False:
                self._continue_open()
            else:
                log_append(self.log, "ยกเลิกการเปิดโปรเจกต์")
        threading.Thread(target=_task, daemon=True).start()

    def _continue_open(self):
        ubt = self.ctx["ubt"].get().strip()
        editor_exe = self.ctx["editor"].get().strip()
        uproject = self.ctx["uproject"].get().strip()

        if not uproject or not os.path.isfile(uproject):
            self.call_in_main(messagebox.showerror, "Error", "โปรดตั้งค่า .uproject ใน Settings")
            return

        if self.ctx["autogen"].get():
            if not ubt or not os.path.isfile(ubt):
                self.call_in_main(messagebox.showerror, "Error", "โปรดตั้งค่า UnrealBuildTool.exe ใน Settings")
                return
            log_append(self.log, "=== Generate Project Files ===")
            if not generate_project_files(ubt, uproject, self.log):
                self.call_in_main(messagebox.showerror, "Error", "Generate Project Files ล้มเหลว")
                return

        if self.ctx["autobuild"].get():
            if not ubt or not os.path.isfile(ubt):
                self.call_in_main(messagebox.showerror, "Error", "โปรดตั้งค่า UnrealBuildTool.exe ใน Settings")
                return
            log_append(self.log, "=== Build Editor ===")
            if not build_editor(ubt, uproject, self.log):
                self.call_in_main(messagebox.showerror, "Error", "Build Editor ล้มเหลว")
                return

        if not editor_exe or not os.path.isfile(editor_exe):
            self.call_in_main(messagebox.showerror, "Error", "โปรดตั้งค่า UnrealEditor.exe ใน Settings")
            return

        log_append(self.log, "=== เปิดโปรเจกต์ ===")
        open_project(editor_exe, uproject, self.log)

    def do_git_check(self, silent=False):
        uproject = self.ctx["uproject"].get()
        logbox = self.log
        res = {"ok": False, "repo": None, "branch": None,
               "behind_upstream": 0, "ahead_origin": 0,
               "upstream_target": None, "origin_target": None,
               "behind_list": [], "ahead_list": []}

        repo = get_repo_root_from_uproject(uproject)
        res["repo"] = repo
        if not repo:
            if not silent: log_append(logbox, "ไม่พบ .git ใกล้ไฟล์ .uproject")
            return res
        if not git_fetch_all(repo, logbox): return res

        branch = git_current_branch(repo)
        if not branch:
            log_append(logbox, "ตรวจ branch ไม่ได้")
            return res
        res["branch"] = branch

        if git_has_remote(repo, "upstream"):
            upstream_target = git_default_remote_branch(repo, "upstream") or f"upstream/{branch}"
            res["upstream_target"] = upstream_target
            behind, _ = git_ahead_behind(repo, upstream_target, "HEAD")
            if behind is not None:
                res["behind_upstream"] = behind
                if behind > 0:
                    res["behind_list"] = git_list_commits(repo, f"HEAD..{upstream_target}", 5)
        else:
            if not silent: log_append(logbox, "ไม่พบ remote 'upstream' (จะเทียบเฉพาะ origin)")

        origin_target = f"origin/{branch}"
        res["origin_target"] = origin_target
        _, ahead = git_ahead_behind(repo, origin_target, "HEAD")
        if ahead is not None:
            res["ahead_origin"] = ahead
            if ahead > 0:
                res["ahead_list"] = git_list_commits(repo, f"{origin_target}..HEAD", 5)

        res["ok"] = True
        return res

    def on_close(self):
        save_settings(self.ctx)
        if self.ctx["bg_job"]:
            try: self.after_cancel(self.ctx["bg_job"])
            except Exception: pass
        self.destroy()

def main():
    App().mainloop()

if __name__ == "__main__":
    main()
