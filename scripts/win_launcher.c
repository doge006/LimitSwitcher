/* LimitSwitcher.exe: the installed app. It runs the bundled Python inside this process
   (runtime\python3XX.dll) on LimitSwitcher.pyw next to this exe, passing its arguments on, so
   Task Manager shows LimitSwitcher, with its name and icon, rather than Python. The Python
   runtime finds its standard library through runtime\python3XX._pth, next to the DLL. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

#ifdef LINKED_PYTHON
/* Linked against python3XX.lib and delay-loaded (/DELAYLOAD): the DLL is loaded at the first call,
   once SetDllDirectoryW points at runtime\. */
__declspec(dllimport) int __cdecl Py_Main(int, wchar_t **);
#else
typedef int (__cdecl *PyMain)(int, wchar_t **);
#endif

static void fail(const wchar_t *text) {
    MessageBoxW(NULL, text, L"LimitSwitcher", MB_ICONERROR);
}

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
    swprintf(script, MAX_PATH, L"%ls\\LimitSwitcher.pyw", folder);
    if (!find_python(runtime, dll)) {
        fail(L"LimitSwitcher couldn't start: its Python runtime is missing. Install LimitSwitcher again.");
        return 1;
    }
    SetDllDirectoryW(runtime); /* Python's own DLLs (vcruntime, _ctypes, ...) */
#ifdef LINKED_PYTHON
    (void)dll;
#else
    HMODULE python = LoadLibraryExW(dll, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    PyMain py_main = python ? (PyMain)GetProcAddress(python, "Py_Main") : NULL;
    if (!py_main) {
        fail(L"LimitSwitcher couldn't start: its Python runtime could not be loaded. Install LimitSwitcher again.");
        return 1;
    }
#endif
    /* argv: this exe, the script, then this exe's own arguments. */
    int count = 0;
    wchar_t **given = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!given) return 1;
    wchar_t **argv = (wchar_t **)LocalAlloc(LPTR, sizeof(wchar_t *) * (count + 2));
    if (!argv) return 1;
    argv[0] = given[0];
    argv[1] = script;
    for (int i = 1; i < count; i++) argv[i + 1] = given[i];
    AllowSetForegroundWindow(ASFW_ANY); /* a second launch may bring the running copy's window up */
#ifdef LINKED_PYTHON
    return Py_Main(count + 1, argv);
#else
    return py_main(count + 1, argv);
#endif
}
