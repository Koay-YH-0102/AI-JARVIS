"""
tools.py
--------
All of the actual "hands on the computer" logic lives here.

Every function that can be called by the AI is registered in TOOL_SPECS (the
schema sent to the model) and dispatched via run_tool(name, args).

Every tool name also appears in exactly one of:
  - SAFE_ACTIONS   -> executed immediately, no confirmation
  - RISKY_ACTIONS  -> backend pauses and asks the UI to confirm with the user
                      before ever touching the system

If you add a new tool, you MUST add its name to one of those two sets or the
backend will refuse to run it (fail-closed, not fail-open).
"""

import glob
import os
import platform
import subprocess
import time
from datetime import datetime

IS_WINDOWS = platform.system() == "Windows"

# Optional dependencies. The app should still run (with reduced features) if
# these aren't installed, so every import is guarded.
try:
    import psutil
except Exception:
    psutil = None

try:
    from PIL import ImageGrab
except Exception:
    ImageGrab = None

try:
    import screen_brightness_control as sbc
except Exception:
    sbc = None

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_OK = True
except Exception:
    # Catches ImportError as well as internal comtypes failures (e.g. a
    # NameError from comtypes' metaclass code on some Python versions).
    # Either way, volume control just becomes unavailable instead of
    # crashing the whole app.
    PYCAW_OK = False


SCREENSHOT_DIR = os.path.join(os.path.expanduser("~"), "AI_Assistant_Screenshots")


# ---------------------------------------------------------------------------
# Common app name -> launch command. Fuzzy-ish lookup; anything not listed
# falls back to trying the raw text with `start`.
# ---------------------------------------------------------------------------
COMMON_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "settings": "ms-settings:",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "word": "winword",
    "microsoft word": "winword",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
    "spotify": "spotify",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "wordpad": "wordpad.exe",
    "registry editor": "regedit.exe",
    "device manager": "devmgmt.msc",
    "disk management": "diskmgmt.msc",
    "services": "services.msc",
    "event viewer": "eventvwr.msc",
    "snipping tool": "SnippingTool.exe",
}

# ms-settings: URI pages. https://learn.microsoft.com/windows/uwp/launch-resume/launch-settings-app
SETTINGS_PAGES = {
    "display": "display",
    "sound": "sound",
    "bluetooth": "bluetooth",
    "wifi": "network-wifi",
    "network": "network-status",
    "update": "windowsupdate",
    "windows update": "windowsupdate",
    "apps": "appsfeatures",
    "storage": "storagesense",
    "battery": "batterysaver",
    "power": "powersleep",
    "notifications": "notifications",
    "personalization": "personalization",
    "accounts": "yourinfo",
    "privacy": "privacy",
    "date and time": "dateandtime",
    "system": "system",
}


def _not_windows_error():
    return {"ok": False, "error": "This action only works on Windows. The server is not running on Windows."}


def _run(cmd, shell=True):
    try:
        subprocess.Popen(cmd, shell=shell)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# SAFE tools
# ---------------------------------------------------------------------------

def open_application(app_name: str):
    if not IS_WINDOWS:
        return _not_windows_error()
    key = app_name.strip().lower()
    cmd = COMMON_APPS.get(key, app_name)
    if cmd.startswith("ms-settings:"):
        os.startfile(cmd)
        return {"ok": True, "message": f"Opened Settings"}
    result = _run(f'start "" {cmd}')
    if result["ok"]:
        result["message"] = f"Launched {app_name}"
    return result


def open_website(url: str):
    import webbrowser
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return {"ok": True, "message": f"Opened {url} in your default browser"}


def open_windows_setting(page: str):
    if not IS_WINDOWS:
        return _not_windows_error()
    key = page.strip().lower()
    uri_page = SETTINGS_PAGES.get(key, key)
    os.startfile(f"ms-settings:{uri_page}")
    return {"ok": True, "message": f"Opened Settings > {page}"}


def list_running_apps():
    if psutil is None:
        return {"ok": False, "error": "psutil is not installed on the server"}
    apps = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            apps.append(p.info["name"])
        except Exception:
            continue
    unique = sorted(set(a for a in apps if a))
    return {"ok": True, "processes": unique}


def get_system_info():
    info = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if psutil is not None:
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        info["memory_percent"] = vm.percent
        info["memory_used_gb"] = round(vm.used / (1024**3), 1)
        info["memory_total_gb"] = round(vm.total / (1024**3), 1)
        try:
            battery = psutil.sensors_battery()
            if battery:
                info["battery_percent"] = battery.percent
                info["battery_plugged"] = battery.power_plugged
        except Exception:
            pass
    return {"ok": True, "info": info}


def take_screenshot():
    if ImageGrab is None:
        return {"ok": False, "error": "Pillow is not installed on the server"}
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    filename = f"screenshot_{int(time.time())}.png"
    path = os.path.join(SCREENSHOT_DIR, filename)
    img = ImageGrab.grab()
    img.save(path)
    return {"ok": True, "message": f"Screenshot saved to {path}", "path": path}


def set_volume(level: int):
    level = max(0, min(100, int(level)))
    if not PYCAW_OK:
        return {"ok": False, "error": "pycaw is not installed on the server (pip install pycaw comtypes)"}
    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    return {"ok": True, "message": f"Volume set to {level}%"}


def mute_volume(mute: bool = True):
    if not PYCAW_OK:
        return {"ok": False, "error": "pycaw is not installed on the server (pip install pycaw comtypes)"}
    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    volume.SetMute(1 if mute else 0, None)
    return {"ok": True, "message": "Muted" if mute else "Unmuted"}


def set_brightness(level: int):
    if sbc is None:
        return {"ok": False, "error": "screen_brightness_control is not installed on the server"}
    level = max(0, min(100, int(level)))
    sbc.set_brightness(level)
    return {"ok": True, "message": f"Brightness set to {level}%"}


def lock_computer():
    if not IS_WINDOWS:
        return _not_windows_error()
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
    return {"ok": True, "message": "Locked the computer"}


def search_files(query: str, search_path: str = None):
    base = search_path or os.path.expanduser("~")
    pattern = os.path.join(base, "**", f"*{query}*")
    matches = glob.glob(pattern, recursive=True)[:50]
    return {"ok": True, "matches": matches, "count": len(matches)}


def create_folder(path: str):
    try:
        os.makedirs(path, exist_ok=True)
        return {"ok": True, "message": f"Created folder {path}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_task_manager():
    return open_application("task manager")


def read_file(path: str, max_chars: int = 8000):
    try:
        if not os.path.isfile(path):
            return {"ok": False, "error": f"Not a file: {path}"}
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return {"ok": True, "path": path, "size_bytes": size, "truncated": truncated, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_directory(path: str = None):
    target = path or os.path.expanduser("~")
    try:
        if not os.path.isdir(target):
            return {"ok": False, "error": f"Not a directory: {target}"}
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": None if entry.is_dir() else entry.stat().st_size,
                    })
                except Exception:
                    continue
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"ok": True, "path": target, "entries": entries[:200], "count": len(entries)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# RISKY tools — never executed without explicit user confirmation
# ---------------------------------------------------------------------------

def close_application(app_name: str):
    if not IS_WINDOWS:
        return _not_windows_error()
    name = app_name if app_name.lower().endswith(".exe") else app_name + ".exe"
    result = subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, text=True)
    ok = result.returncode == 0
    return {"ok": ok, "message": result.stdout.strip() or result.stderr.strip()}


def shutdown_computer(delay_seconds: int = 30):
    if not IS_WINDOWS:
        return _not_windows_error()
    subprocess.run(["shutdown", "/s", "/t", str(delay_seconds)])
    return {"ok": True, "message": f"Shutdown scheduled in {delay_seconds}s. Run 'shutdown /a' to cancel."}


def restart_computer(delay_seconds: int = 30):
    if not IS_WINDOWS:
        return _not_windows_error()
    subprocess.run(["shutdown", "/r", "/t", str(delay_seconds)])
    return {"ok": True, "message": f"Restart scheduled in {delay_seconds}s. Run 'shutdown /a' to cancel."}


def sleep_computer():
    if not IS_WINDOWS:
        return _not_windows_error()
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return {"ok": True, "message": "Sleeping the computer"}


def cancel_shutdown():
    if not IS_WINDOWS:
        return _not_windows_error()
    subprocess.run(["shutdown", "/a"])
    return {"ok": True, "message": "Cancelled pending shutdown/restart"}


def delete_file(path: str):
    try:
        if os.path.isdir(path):
            return {"ok": False, "error": "Refusing to delete a directory through this tool. Delete files one at a time."}
        os.remove(path)
        return {"ok": True, "message": f"Deleted {path}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def empty_recycle_bin():
    if not IS_WINDOWS:
        return _not_windows_error()
    try:
        import ctypes
        SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x00000001, 0x00000002, 0x00000004
        ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        )
        return {"ok": True, "message": "Recycle bin emptied"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_shell_command(command: str):
    """Generic escape hatch for anything not covered by a dedicated tool.
    ALWAYS risky, no exceptions, regardless of what the command looks like."""
    if not IS_WINDOWS:
        return _not_windows_error()
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {"ok": result.returncode == 0, "message": output.strip()[:4000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def uninstall_hint(app_name: str):
    # We deliberately do NOT programmatically uninstall software. We open the
    # native Windows "Apps & Features" panel and let the human do it there.
    open_windows_setting("apps")
    return {"ok": True, "message": f"Opened Apps & Features so you can uninstall '{app_name}' yourself"}


# ---------------------------------------------------------------------------
# Registry: every tool the model is allowed to ask for, its JSON schema, and
# which bucket (safe / risky) it belongs to.
# ---------------------------------------------------------------------------

SAFE_ACTIONS = {
    "open_application", "open_website", "open_windows_setting", "list_running_apps",
    "get_system_info", "take_screenshot", "set_volume", "mute_volume",
    "set_brightness", "lock_computer", "search_files", "create_folder",
    "open_task_manager", "read_file", "list_directory",
}

RISKY_ACTIONS = {
    "close_application", "shutdown_computer", "restart_computer", "sleep_computer",
    "cancel_shutdown", "delete_file", "empty_recycle_bin", "run_shell_command",
    "uninstall_hint",
}

DISPATCH = {
    "open_application": open_application,
    "open_website": open_website,
    "open_windows_setting": open_windows_setting,
    "list_running_apps": list_running_apps,
    "get_system_info": get_system_info,
    "take_screenshot": take_screenshot,
    "set_volume": set_volume,
    "mute_volume": mute_volume,
    "set_brightness": set_brightness,
    "lock_computer": lock_computer,
    "search_files": search_files,
    "create_folder": create_folder,
    "open_task_manager": open_task_manager,
    "read_file": read_file,
    "list_directory": list_directory,
    "close_application": close_application,
    "shutdown_computer": shutdown_computer,
    "restart_computer": restart_computer,
    "sleep_computer": sleep_computer,
    "cancel_shutdown": cancel_shutdown,
    "delete_file": delete_file,
    "empty_recycle_bin": empty_recycle_bin,
    "run_shell_command": run_shell_command,
    "uninstall_hint": uninstall_hint,
}

assert set(DISPATCH) == SAFE_ACTIONS | RISKY_ACTIONS, "Every tool must be classified safe or risky"


def is_risky(tool_name: str) -> bool:
    # Fail closed: unknown tool names are treated as risky.
    return tool_name in RISKY_ACTIONS or tool_name not in SAFE_ACTIONS


def run_tool(name: str, args: dict):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# OpenAI-style tool schemas (OpenRouter uses the same "tools" format)
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "open_application", "description": "Open/launch a desktop application by name (e.g. notepad, chrome, spotify, calculator, task manager, control panel).",
        "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
    }},
    {"type": "function", "function": {
        "name": "open_website", "description": "Open a URL in the default web browser.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "open_windows_setting", "description": "Open a specific page of the Windows Settings app (e.g. display, sound, bluetooth, wifi, network, update, apps, storage, battery, power, notifications, personalization, accounts, privacy, system).",
        "parameters": {"type": "object", "properties": {"page": {"type": "string"}}, "required": ["page"]},
    }},
    {"type": "function", "function": {
        "name": "list_running_apps", "description": "List currently running processes/applications.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_system_info", "description": "Get CPU, memory, battery, and OS info for the machine.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "take_screenshot", "description": "Take a screenshot of the current screen and save it to disk.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "set_volume", "description": "Set the system output volume, 0-100.",
        "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]},
    }},
    {"type": "function", "function": {
        "name": "mute_volume", "description": "Mute or unmute system audio.",
        "parameters": {"type": "object", "properties": {"mute": {"type": "boolean"}}, "required": ["mute"]},
    }},
    {"type": "function", "function": {
        "name": "set_brightness", "description": "Set screen brightness, 0-100.",
        "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]},
    }},
    {"type": "function", "function": {
        "name": "lock_computer", "description": "Lock the Windows session immediately (does not log out or close programs).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "search_files", "description": "Search for files by (partial) name under a given folder, defaults to the user's home folder.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "search_path": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "create_folder", "description": "Create a new folder at the given path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "open_task_manager", "description": "Open the Windows Task Manager.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read the text contents of a file (truncated to a safe length if very large).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_directory", "description": "List the files and folders inside a directory, defaults to the user's home folder.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "close_application", "description": "RISKY: Force-close a running application by name. May cause loss of unsaved work.",
        "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
    }},
    {"type": "function", "function": {
        "name": "shutdown_computer", "description": "RISKY: Shut down the computer after a delay (seconds).",
        "parameters": {"type": "object", "properties": {"delay_seconds": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "restart_computer", "description": "RISKY: Restart the computer after a delay (seconds).",
        "parameters": {"type": "object", "properties": {"delay_seconds": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "sleep_computer", "description": "RISKY: Put the computer to sleep immediately.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "cancel_shutdown", "description": "Cancel a previously scheduled shutdown/restart.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "delete_file", "description": "RISKY: Permanently delete a single file at the given path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "empty_recycle_bin", "description": "RISKY: Permanently empty the Recycle Bin.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "run_shell_command", "description": "RISKY: Run an arbitrary PowerShell command. Use only when no other tool covers the task.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "uninstall_hint", "description": "RISKY: Open the native 'Apps & Features' panel so the user can uninstall an app themselves. This tool never uninstalls anything programmatically.",
        "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
    }},
]

_tool_spec_names = {t["function"]["name"] for t in TOOL_SPECS}
assert _tool_spec_names == set(DISPATCH), (
    "TOOL_SPECS must list exactly the same tools as DISPATCH — "
    f"missing from TOOL_SPECS: {set(DISPATCH) - _tool_spec_names}, "
    f"missing from DISPATCH: {_tool_spec_names - set(DISPATCH)}"
)
