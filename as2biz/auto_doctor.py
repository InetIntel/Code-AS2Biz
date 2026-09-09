# file: auto_doctor.py
import argparse
import subprocess
import os
import sys
import re
import shutil
import json
import time
from pathlib import Path

# our slice-rebuild helper
import rebuild_slice_tool

# === dual logger: terminal + file ===
class DualLogger(object):
    """
    Send stdout to both the terminal and a log file.
    """
    def __init__(self, filename):
        self.terminal = sys.stdout
        # line-buffered (buffering=1) for real-time flushing to disk
        self.log = open(filename, "a", encoding="utf-8", buffering=1)

    def write(self, message):
        try:
            self.terminal.write(message)
            self.log.write(message)
        except Exception:
            pass  # never let an encoding error crash the program

    def flush(self):
        try:
            self.terminal.flush()
            self.log.flush()
        except Exception:
            pass

def run_command_streaming(cmd, desc):
    """
    Run a command, stream its output live, and capture it for analysis.
    """
    # force every arg to str (avoids a TypeError)
    cmd = [str(x) for x in cmd]

    print(f"\n[{desc}] Executing: {' '.join(cmd)}")
    print("-" * 60)

    captured_output = []

    # Popen for a live output stream
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout
        text=True,
        bufsize=1  # line-buffered
    )

    # read and print line by line
    # note: these prints are captured by DualLogger and written to the log file
    for line in process.stdout:
        print(line, end='')
        captured_output.append(line)

    process.wait()
    print("-" * 60)

    if process.returncode != 0:
        print(f"⚠️ Warning: Command finished with exit code {process.returncode}")

    return "".join(captured_output)

def run_monitor(archives_dir, version_tag, state_json):
    """Run the monitor script and stream progress."""
    print(f">>> Step 1: Diagnosing health (using state: {os.path.basename(state_json)})...")

    # -u keeps the child unbuffered so it does not look hung
    cmd = [
        sys.executable, "-u", str(Path(__file__).resolve().parent / "warc_health_monitor.py"),
        "--archives-dir", archives_dir,
        "--version-tag", version_tag,
        "--state-json", state_json
    ]

    # run it, streaming
    full_log = run_command_streaming(cmd, "Monitor")

    broken_stores = []
    broken_slices = []

    # parse the log
    for line in full_log.splitlines():
        # [store] store_00062.warc.gz: Error...
        m_store = re.search(r"\[store\]\s+(store_\d+\.warc(?:\.gz)?):", line)
        if m_store:
            broken_stores.append(m_store.group(1))

        # [slice] slice_2025-09-01_00000.warc.gz: Error...
        m_slice = re.search(r"\[slice\]\s+(slice_.*?\.warc(?:\.gz)?):", line)
        if m_slice:
            broken_slices.append(m_slice.group(1))

    # de-dup
    return list(set(broken_stores)), list(set(broken_slices))

def fix_broken_stores(archives_dir, broken_files):
    """Delete broken store WARCs and scrub the store index."""
    if not broken_files:
        return

    print(f"\n>>> Step 2: Fixing {len(broken_files)} Broken Store WARCs...")
    store_dir = Path(archives_dir) / "store"
    warc_dir = store_dir / "warc"
    index_path = store_dir / "index" / "store_index.jsonl"

    # 1. delete the files
    for fname in broken_files:
        p = warc_dir / fname
        if p.exists():
            print(f"  [Delete] Removing broken file: {p.name}")
            p.unlink()
        else:
            print(f"  [Warn] File not found (maybe already deleted): {p.name}")

    # 2. scrub the store index
    if index_path.exists():
        print(f"  [Clean] Scrubbing store index: {index_path.name}")
        tmp_index = index_path.with_suffix(".tmp")
        removed_count = 0
        bad_set = set(broken_files)

        try:
            with open(index_path, "r", encoding="utf-8") as fin, \
                 open(tmp_index, "w", encoding="utf-8") as fout:
                for line in fin:
                    if any(bad in line for bad in bad_set):
                        removed_count += 1
                        continue
                    fout.write(line)

            shutil.move(tmp_index, index_path)
            print(f"  [Clean] Removed {removed_count} lines from store index.")
        except Exception as e:
            print(f"  [Error] Failed to scrub store index: {e}")

def clean_version_index_for_broken_stores(archives_dir, version_tag, broken_store_files):
    """Remove version-index rows that reference broken store WARCs."""
    if not broken_store_files:
        return

    version_dir = Path(archives_dir) / "versions" / version_tag
    idx_path = version_dir / "index.jsonl"

    if not idx_path.exists():
        print(f"  [Warn] Version index not found: {idx_path}")
        return

    bad_stores = set(broken_store_files)
    tmp_path = idx_path.with_suffix(".tmp")
    removed = 0

    print("\n>>> Step 2.5: Cleaning Version Index (removing refs to broken stores)...")

    try:
        with idx_path.open("r", encoding="utf-8") as fin, \
             tmp_path.open("w", encoding="utf-8") as fout:
            for line in fin:
                line_strip = line.strip()
                if not line_strip: continue
                try:
                    row = json.loads(line_strip)
                    store_ref = row.get("store_ref") or {}
                    if store_ref.get("warc") in bad_stores:
                        removed += 1
                        continue # Skip this line
                except Exception:
                    pass

                fout.write(line)

        shutil.move(tmp_path, idx_path)
        print(f"  [Clean] Removed {removed} rows from version index.")

    except Exception as e:
        print(f"  [Error] Failed to clean version index: {e}")
        if tmp_path.exists(): tmp_path.unlink()

def fix_broken_slices(archives_dir, version_tag, broken_slices, broken_stores_blacklist):
    """Rebuild broken slice WARCs via rebuild_slice_tool."""
    if not broken_slices:
        return

    print(f"\n>>> Step 3: Rebuilding {len(broken_slices)} Broken Slice WARCs...")
    version_dir = Path(archives_dir) / "versions" / version_tag
    warc_dir = version_dir / "warc"
    index_path = version_dir / "index.jsonl"

    if not index_path.exists():
        print("  [Error] Cannot rebuild slice because index.jsonl is missing!")
        return

    for bad_slice in broken_slices:
        print(f"  [Rebuild] Processing {bad_slice}...")

        # call the rebuild helper
        new_warc, new_index = rebuild_slice_tool.rebuild_slice_warc(
            archives_dir, version_tag, bad_slice, broken_stores_blacklist
        )

        # swap in the new WARC
        bad_slice_path = warc_dir / bad_slice
        if bad_slice_path.exists():
            print(f"  [Replace] Deleting old broken file: {bad_slice_path.name}")
            bad_slice_path.unlink()

        # swap in the new index
        print("  [Replace] Updating main index.jsonl with rebuilt data...")
        shutil.copy(index_path, index_path.with_suffix(f".bak_{bad_slice}"))
        shutil.move(new_index, index_path)

        print(f"  [Success] Rebuilt {bad_slice} -> {os.path.basename(new_warc)}")

def run_scraper(archives_dir, version_tag, input_file, concurrency=12):
    """Run the scraper to backfill missing data."""
    print("\n>>> Step 4: Running Scraper to refill missing data...")
    cmd = [
        sys.executable, "-u", str(Path(__file__).resolve().parent / "scraper.py"),
        "--archives-dir", archives_dir,
        "--version-tag", version_tag,
        "--input", input_file,
        "--concurrency", concurrency
    ]

    # stream this one too
    run_command_streaming(cmd, "Scraper")

def main():
    ap = argparse.ArgumentParser(description="Auto Doctor: Monitor -> Fix -> Scrape (Streaming Output + Logging)")
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--version-tag", required=True)
    ap.add_argument("--input", required=True, help="URL list file for scraper")
    ap.add_argument("--state-json", required=True, help="State file for health monitor")

    args = ap.parse_args()

    # === set up logging ===
    # log file name: doctor_log_<tag>_<timestamp>.log
    # written to the current directory
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_filename = f"doctor_log_{args.version_tag}_{ts}.log"

    # hijack sys.stdout for tee output
    sys.stdout = DualLogger(log_filename)
    # redirect stderr too, to capture tracebacks
    sys.stderr = sys.stdout

    print(f"=== Auto Doctor Log Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"=== Log file saved to: {os.path.abspath(log_filename)} ===\n")

    try:
        # 1. diagnose
        broken_stores, broken_slices = run_monitor(
            args.archives_dir, args.version_tag, args.state_json
        )

        if not broken_stores and not broken_slices:
            print("\n✅ [All Good] No broken files detected. Monitor says everything is healthy.")
            print("Exiting.")
            return

        # 2. Fix Store
        if broken_stores:
            fix_broken_stores(args.archives_dir, broken_stores)

        # 2.5 Clean Version Index
        if broken_stores:
            clean_version_index_for_broken_stores(args.archives_dir, args.version_tag, broken_stores)

        # 3. Fix Slice
        if broken_slices:
            fix_broken_slices(args.archives_dir, args.version_tag, broken_slices, broken_stores)

        # 4. Scraper
        run_scraper(args.archives_dir, args.version_tag, args.input)

        print(f"\n=== Auto Doctor Finished at {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    except Exception as e:
        print(f"\n❌ [CRITICAL ERROR] Auto Doctor crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
