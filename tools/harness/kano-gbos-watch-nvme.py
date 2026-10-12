#!/usr/bin/env python3
"""Watch the KANO AP UART during a Googlebook boot from the internal NVMe; cut power through the EC on any danger line.

usage: kano-gbos-watch-nvme.py <ap-uart.log> <seconds> <frames-dir> [options of kano-gbos-watch.py]

This is kano-gbos-watch.py --profile nvme: the NVMe controller appearing is expected, and the kernel command line
must carry initcall_blacklist=mei_me_driver_init (nvme_init allowed). Same limits as kano-gbos-watch.py: serial-debug
entry only, a supplementary detector, "unobservable" (exit 4) when this boot cannot be seen.
"""
import os, sys
from pathlib import Path

watch = Path(__file__).resolve().parent / "kano-gbos-watch.py"
os.execv(sys.executable, [sys.executable, str(watch), "--profile", "nvme", *sys.argv[1:]])
