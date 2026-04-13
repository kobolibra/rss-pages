#!/usr/bin/env python3
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 6:
        print(
            "Usage: python build_with_live_fallback.py <base_url> <site_dir> <feed_name> <restore_item_pages:0|1> <command...>",
            file=sys.stderr,
        )
        return 2

    base_url = sys.argv[1].rstrip("/")
    site_dir = Path(sys.argv[2])
    feed_name = sys.argv[3]
    restore_item_pages = sys.argv[4] == "1"
    command = sys.argv[5:]

    print(f"[build_with_live_fallback] building {feed_name}: {' '.join(shlex.quote(x) for x in command)}")
    proc = subprocess.run(command, text=True)
    if proc.returncode == 0:
        print(f"[build_with_live_fallback] build succeeded: {feed_name}")
        return 0

    print(f"[build_with_live_fallback] build failed for {feed_name} with exit code {proc.returncode}; restoring live copy")
    output_xml = Path("feeds") / "output" / f"{feed_name}.xml"
    if output_xml.exists():
        output_xml.unlink()
        print(f"[build_with_live_fallback] removed stale generated output {output_xml}")
    restore_cmd = [sys.executable, "scripts/restore_live_pages_feed.py", base_url, str(site_dir), feed_name]
    if not restore_item_pages:
        restore_cmd.append("--xml-only")
    restore = subprocess.run(restore_cmd, text=True)
    if restore.returncode != 0:
        print(f"[build_with_live_fallback] restore failed for {feed_name} with exit code {restore.returncode}", file=sys.stderr)
        return restore.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
