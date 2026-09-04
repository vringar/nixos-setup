#!/usr/bin/env python3
"""Sample memory gauges and counters into the journal, one JSON line per tick.

The files this reads are live gauges with no history of their own, so a value
read after an incident describes the present rather than the incident. Writing
them out on a cadence is what makes a past moment reconstructable at all.

Counters (the vmstat and pressure-total fields) are monotonic since boot and are
recorded raw: a rate is the difference between two adjacent samples, which is
also the only honest way to read them. A single reading of a counter says
nothing about any particular moment.
"""

import argparse
import json
import sys
import time
from pathlib import Path

MEMINFO = Path("/proc/meminfo")
ARCSTATS = Path("/proc/spl/kstat/zfs/arcstats")
VMSTAT = Path("/proc/vmstat")
PRESSURE = Path("/proc/pressure/memory")

# Gauges. Enough to split "used" into its real parts: process anonymous memory,
# the ARC's slab footprint, and the page cache.
MEMINFO_KEYS = (
    "MemTotal MemFree MemAvailable Buffers Cached SwapTotal SwapFree SwapCached "
    "AnonPages Mapped Shmem Slab SReclaimable SUnreclaim PageTables Dirty Writeback"
).split()

# The ARC does not appear as cache anywhere in meminfo, so it has to be read
# from its own kstat or it is simply invisible.
ARC_KEYS = (
    "size c c_max c_min data_size metadata_size dnode_size dbuf_size hdr_size "
    "bonus_size abd_chunk_waste_size arc_meta_used memory_throttle_count"
).split()

# Counters. pgsteal_direct against pgsteal_kswapd is the split between reclaim
# done synchronously inside an allocating task and reclaim done in the
# background -- the former is what stall-based accounting actually measures.
VMSTAT_KEYS = (
    "pgscan_direct pgscan_kswapd pgsteal_direct pgsteal_kswapd pswpin pswpout "
    "pgmajfault allocstall_normal workingset_refault_anon workingset_refault_file "
    "zswpin zswpout zswpwb"
).split()


def parse_colon_kv(text):
    """Parse /proc/meminfo: 'MemTotal:  41014664 kB' -> {'MemTotal': 41014664}."""
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].lstrip("-").isdigit():
            out[name.strip()] = int(fields[0])
    return out


def parse_space_kv(text):
    """Parse /proc/vmstat: 'pgsteal_direct 1172130' -> {'pgsteal_direct': 1172130}."""
    out = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("-").isdigit():
            out[fields[0]] = int(fields[1])
    return out


def parse_arcstats(text):
    """Parse arcstats: two header lines, then 'name type data' triples."""
    out = {}
    for line in text.splitlines()[2:]:
        fields = line.split()
        if len(fields) == 3 and fields[2].lstrip("-").isdigit():
            out[fields[0]] = int(fields[2])
    return out


def parse_pressure(text):
    """Parse a PSI file into {'some_avg10': 0.01, 'some_total': 87824382, ...}.

    The avg fields are gauges the kernel has already smoothed; total is a
    monotonic microsecond counter and is the one to difference between samples.
    """
    out = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if not value:
                continue
            try:
                out[f"{kind}_{key}"] = int(value) if key == "total" else float(value)
            except ValueError:
                continue
    return out


def read(path):
    """Read a file, or return None. A sampler that dies mid-incident is useless."""
    try:
        return path.read_text()
    except OSError:
        return None


def select(source, keys):
    return {k: source[k] for k in keys if k in source}


def read_cgroup(path):
    """Current charge, swap, and pressure for one cgroup."""
    out = {}
    for field, name in (("memory.current", "current"), ("memory.swap.current", "swap")):
        text = read(path / field)
        if text and text.strip().isdigit():
            out[name] = int(text.strip())
    text = read(path / "memory.pressure")
    if text:
        pressure = parse_pressure(text)
        out.update({k: v for k, v in pressure.items() if k.endswith("_total")})
    return out


def sample(cgroup_root):
    """One observation of every source, as a plain dict."""
    record = {"t": round(time.time(), 3)}

    for key, path, parser, wanted in (
        ("mem", MEMINFO, parse_colon_kv, MEMINFO_KEYS),
        ("arc", ARCSTATS, parse_arcstats, ARC_KEYS),
        ("vm", VMSTAT, parse_space_kv, VMSTAT_KEYS),
    ):
        text = read(path)
        if text:
            record[key] = select(parser(text), wanted)

    text = read(PRESSURE)
    if text:
        record["psi"] = parse_pressure(text)

    if cgroup_root and cgroup_root.is_dir():
        groups = {"": read_cgroup(cgroup_root)}
        # Direct children are the units a pressure-based killer chooses between,
        # so sampling them is what names the victim after the fact.
        for child in sorted(cgroup_root.iterdir()):
            if child.is_dir():
                groups[child.name] = read_cgroup(child)
        record["cg"] = {k: v for k, v in groups.items() if v}

    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="seconds between samples (default: 10, below the 30s a pressure "
             "rule needs to sustain before it acts)",
    )
    parser.add_argument(
        "--cgroup", type=Path, default=None,
        help="cgroup directory to sample along with its direct children",
    )
    parser.add_argument(
        "--once", action="store_true", help="emit a single sample and exit",
    )
    args = parser.parse_args(argv)

    while True:
        # Deadline before the work, so a slow read shortens the sleep instead of
        # pushing every later sample further out of step.
        deadline = time.monotonic() + args.interval
        record = sample(args.cgroup)
        # Compact, and flushed every line: an unflushed buffer is lost with the
        # process, which is exactly when the last samples matter most.
        print(json.dumps(record, separators=(",", ":")), flush=True)
        if args.once:
            return 0
        time.sleep(max(0.0, deadline - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
