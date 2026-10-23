"""The processes of this user, with when each started and its command line, and a way to end a
process with everything it started. Used to find Codex processes that began before the router did
(they keep the settings they started with, so they skip it). One snapshot of the process list, plus
a command-line read only for processes whose name matches: a few milliseconds, once a minute."""
import os
import subprocess
import sys
import time


class Process:
    def __init__(self, pid, parent, name, started, command):
        self.pid, self.parent, self.name, self.started, self.command = pid, parent, name, started, command

    def __repr__(self):
        return f"Process({self.pid}, {self.name!r}, started={self.started:.0f})"


def listing(names):
    """[Process] whose executable name (lower case, without .exe) is in `names`, with the parent
    ids of every process (for trees). Empty when the list can't be read."""
    try:
        return _windows(names) if sys.platform == "win32" else _posix(names)
    except Exception:  # never let a process listing break the caller
        return []


def descendants(pid):
    """Ids of every process started (directly or not) by `pid`."""
    try:
        parents = _windows_parents() if sys.platform == "win32" else _posix_parents()
    except Exception:
        return []
    found, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        for child, parent in parents.items():
            if parent == current and child not in found and child != pid:
                found.append(child)
                frontier.append(child)
    return found


def family():
    """{pid: (parent pid, executable name in lower case without .exe)} for every process."""
    try:
        if sys.platform == "win32":
            rows = {pid: (parent, exe.lower()) for pid, parent, exe in _snapshot()}
        else:
            rows = {pid: (parent, os.path.basename(command.split()[0]).lower() if command.split() else "")
                    for pid, parent, _, command in _ps()}
    except Exception:
        return {}
    return {pid: (parent, name[:-4] if name.endswith(".exe") else name) for pid, (parent, name) in rows.items()}


def end_tree(pid):
    """End a process and everything it started (children first)."""
    for target in reversed([pid] + descendants(pid)):
        try:
            if sys.platform == "win32":
                _terminate(target)
            else:
                import signal
                os.kill(target, signal.SIGTERM)
        except OSError:
            pass


# ---------- Windows ----------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    ntdll.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG))
    QUERY_LIMITED, TERMINATE = 0x1000, 0x0001

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    def _snapshot():
        handle = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if handle in (None, wintypes.HANDLE(-1).value):
            return []
        entries = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                entries.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(handle)
        return entries

    def _windows_parents():
        return {pid: parent for pid, parent, _ in _snapshot()}

    def _details(pid):
        """(started as a Unix time, command line) or None."""
        handle = kernel32.OpenProcess(QUERY_LIMITED, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            created = (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) / 1e7 - 11644473600
            size = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
            command = ""
            if size.value:
                buffer = ctypes.create_string_buffer(size.value)
                if ntdll.NtQueryInformationProcess(handle, 60, buffer, size, ctypes.byref(size)) == 0:
                    text = UNICODE_STRING.from_buffer(buffer)
                    command = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
            return created, command
        finally:
            kernel32.CloseHandle(handle)

    def _windows(names):
        found = []
        for pid, parent, exe in _snapshot():
            name = exe.lower()
            name = name[:-4] if name.endswith(".exe") else name
            if name in names:
                details = _details(pid)
                if details:
                    found.append(Process(pid, parent, name, *details))
        return found

    def _terminate(pid):
        handle = kernel32.OpenProcess(TERMINATE, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)


# ---------- Linux ----------
def _ps():
    """[(pid, parent, seconds running, command)] from ps."""
    done = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,command="], capture_output=True, text=True, timeout=10)
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), _etime(parts[2]), parts[3]))
        except ValueError:
            continue
    return rows


def _etime(text):
    """ps's elapsed time, [[dd-]hh:]mm:ss, in seconds."""
    days, _, rest = text.rpartition("-")
    seconds = 0
    for part in rest.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + (int(days) * 86400 if days else 0)


def _posix(names):
    now = time.time()
    found = []
    for pid, parent, running, command in _ps():
        first = command.split()[0] if command.split() else ""
        name = os.path.basename(first).lower()
        if name in names:
            found.append(Process(pid, parent, name, now - running, command))
    return found


def _posix_parents():
    return {pid: parent for pid, parent, _, _ in _ps()}
