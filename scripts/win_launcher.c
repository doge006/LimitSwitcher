/* LimitSwitcher.exe: the installed app. It runs the bundled Python inside this process
   (runtime\python3XX.dll) on LimitSwitcher.pyw next to this exe, passing its arguments on, so
   Task Manager shows LimitSwitcher, with its name and icon, rather than Python. The Python
   runtime finds its standard library through runtime\python3XX._pth, next to the DLL.

   Built with /DSTATUS it is LimitSwitcherStatus.exe instead: a console program (Claude Code reads
   its output) that runs account_switcher\statusline.py, which Claude Code starts every second
   or so per open session. Task Manager shows "LimitSwitcher Status" for it, it starts Python
   without site-packages (-S) and ignoring PYTHON* variables (-E), and it never shows a dialog. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

typedef int (__cdecl *PyMain)(int, wchar_t **);

#ifdef STATUS
#define SCRIPT_FILE L"account_switcher\\statusline.py"
#define EXTRA_ARGS 2 /* -S -E */
static void fail(const wchar_t *text) { (void)text; } /* run every second: never a dialog */
#else
#define SCRIPT_FILE L"LimitSwitcher.pyw"
#define EXTRA_ARGS 0
static void fail(const wchar_t *text) {
    MessageBoxW(NULL, text, L"LimitSwitcher", MB_ICONERROR);
}
#endif

/* runtime\python3XX.dll (the versioned one, not the stable-ABI python3.dll). */
static int find_python(const wchar_t *runtime, wchar_t *dll) {
    wchar_t pattern[MAX_PATH];
    WIN32_FIND_DATAW found;
    swprintf(pattern, MAX_PATH, L"%ls\\python3*.dll", runtime);
    HANDLE search = FindFirstFileW(pattern, &found);
    if (search == INVALID_HANDLE_VALUE) return 0;
    int ok = 0;
    do {
        if (_wcsicmp(found.cFileName, L"python3.dll") != 0) {
            swprintf(dll, MAX_PATH, L"%ls\\%ls", runtime, found.cFileName);
            ok = 1;
        }
    } while (!ok && FindNextFileW(search, &found));
    FindClose(search);
    return ok;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command, int show) {
    wchar_t folder[MAX_PATH], runtime[MAX_PATH], dll[MAX_PATH], script[MAX_PATH];
    DWORD length = GetModuleFileNameW(NULL, folder, MAX_PATH);
    if (!length || length >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(folder, L'\\');
    if (!slash) return 1;
    *slash = 0;
    swprintf(runtime, MAX_PATH, L"%ls\\runtime", folder);
    swprintf(script, MAX_PATH, L"%ls\\%ls", folder, SCRIPT_FILE);
    if (!find_python(runtime, dll)) {
        fail(L"LimitSwitcher couldn't start: its Python runtime is missing. Install LimitSwitcher again.");
        return 1;
    }
    SetDllDirectoryW(runtime); /* Python's own DLLs (vcruntime, _ctypes, ...) */
    HMODULE python = LoadLibraryExW(dll, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    PyMain py_main = python ? (PyMain)GetProcAddress(python, "Py_Main") : NULL;
    if (!py_main) {
        fail(L"LimitSwitcher couldn't start: its Python runtime could not be loaded. Install LimitSwitcher again.");
        return 1;
    }
    /* argv: this exe, the script, then this exe's own arguments. */
    int count = 0;
    wchar_t **given = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!given) return 1;
    wchar_t **argv = (wchar_t **)LocalAlloc(LPTR, sizeof(wchar_t *) * (count + 2 + EXTRA_ARGS));
    if (!argv) return 1;
    int n = 0;
    argv[n++] = given[0];
#ifdef STATUS
    argv[n++] = L"-S";
    argv[n++] = L"-E";
#endif
    argv[n++] = script;
    for (int i = 1; i < count; i++) argv[n++] = given[i];
#ifndef STATUS
    AllowSetForegroundWindow(ASFW_ANY); /* a second launch may bring the running copy's window up */
#endif
    return py_main(n, argv);
}
