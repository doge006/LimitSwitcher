"""Measure the running LimitSwitcher: memory and CPU over a stretch of time.

    .venv\\Scripts\\python -m pip install psutil      (once; Windows)
    .venv\\Scripts\\python scripts\\measure.py         (60 s; --seconds N to change)

Leave the app alone while it runs
(or open the panel / full view first to measure those). It reports, for the app's process:
- memory: working set (what Task Manager shows) and private memory (only this app's);
- CPU: the share of one core used over the whole stretch, and the CPU time behind it;
- threads.
Numbers depend on the machine; measure a few times and quote the typical one.
"""
import argparse
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("psutil is needed: python -m pip install psutil")


def app_processes():
    """The app's processes: LimitSwitcher.exe (installed), or Python running LimitSwitcher.pyw or
    account_switcher.tray (from source; a venv's pythonw.exe starts the real Python as its child:
    both are counted)."""
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        line = " ".join(process.info.get("cmdline") or [])
        name = (process.info.get("name") or "").lower()
        if name == "limitswitcher.exe" or ("python" in name and (
                "LimitSwitcher.pyw" in line or "account_switcher.tray" in line or "LimitSwitcher" in line)):
            found.append(process)
    return found


def snapshot(processes):
    memory = private = cpu = threads = 0.0
    for process in processes:
        try:
            with process.oneshot():
                info = process.memory_full_info()
                memory += info.rss
                private += getattr(info, "private", getattr(info, "uss", info.rss))
                times = process.cpu_times()
                cpu += times.user + times.system
                threads += process.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return memory, private, cpu, threads


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    processes = app_processes()
    if not processes:
        sys.exit("LimitSwitcher isn't running.")
    _, _, cpu_before, _ = snapshot(processes)
    start = time.monotonic()
    peak = 0.0
    while time.monotonic() - start < args.seconds:
        time.sleep(1)
        peak = max(peak, snapshot(processes)[0])
    memory, private, cpu_after, threads = snapshot(processes)
    elapsed = time.monotonic() - start
    used = cpu_after - cpu_before
    mb = 1024 * 1024
    print(f"LimitSwitcher over {elapsed:.0f} s ({len(processes)} process{'es' if len(processes) != 1 else ''}):")
    print(f"  memory     {memory / mb:.1f} MB working set, {private / mb:.1f} MB private (peak {peak / mb:.1f} MB)")
    print(f"  CPU        {100 * used / elapsed:.3f}% of one core ({used * 1000:.0f} ms of CPU time)")
    print(f"  threads    {threads:.0f}")


if __name__ == "__main__":
    main()
