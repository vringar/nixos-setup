#!/usr/bin/env python3
"""Sample memory gauges and counters into the journal, one JSON line per tick.

The files this reads are live gauges with no history of their own, so a value
read after an incident describes the present rather than the incident. Writing
them out on a cadence is what makes a past moment reconstructable at all.

Counters (the vmstat, cache-hit and pressure-total fields) are monotonic since
boot and are recorded raw: a rate is the difference between two adjacent
samples, which is also the only honest way to read them. A single reading of a
counter says nothing about any particular moment.
"""

import argparse
import json
import sys
import time
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")
ARCSTATS = Path("/proc/spl/kstat/zfs/arcstats")
VMSTAT = Path("/proc/vmstat")
PRESSURE = Path("/proc/pressure/memory")
SWAPS = Path("/proc/swaps")

# Gauges. Enough to split "used" into its real parts: process anonymous memory,
# the ARC's slab footprint, and the page cache.
MEMINFO_KEYS = (
    "MemTotal MemFree MemAvailable Buffers Cached SwapTotal SwapFree SwapCached "
    "AnonPages Mapped Shmem Slab SReclaimable SUnreclaim PageTables Dirty Writeback"
).split()

# The ARC does not appear as cache anywhere in meminfo, so it has to be read
# from its own kstat or it is simply invisible.
#
# The hit and ghost counters are what make a cap defensible: ghost-list hits are
# the cache's own record of what it would have served had it been larger, so
# they price a size change instead of leaving it to taste. They are counters,
# and a hit rate taken from one reading is a since-boot average that describes
# no particular workload.
ARC_KEYS = (
    "size c c_max c_min data_size metadata_size dnode_size dbuf_size hdr_size "
    "bonus_size abd_chunk_waste_size arc_meta_used memory_throttle_count "
    "hits misses demand_data_hits demand_data_misses demand_metadata_hits "
    "demand_metadata_misses mru_ghost_hits mfu_ghost_hits"
).split()

# Counters. pgsteal_direct against pgsteal_kswapd is the split between reclaim
# done synchronously inside an allocating task and reclaim done in the
# background -- the former is what stall-based accounting actually measures.
#
# The refault counters are the ones that separate paging that is working from
# paging that is not: eviction only frees memory if the page stays evicted, and
# a refault rate approaching the eviction rate means the two have formed a loop
# that consumes reclaim capacity while releasing nothing.
VMSTAT_KEYS = (
    "pgscan_direct pgscan_kswapd pgsteal_direct pgsteal_kswapd pswpin pswpout "
    "pgmajfault allocstall_normal workingset_refault_anon workingset_refault_file "
    "zswpin zswpout zswpwb"
).split()

# Per-cgroup fields, from memory.stat unless noted.
#
# anon against file answers what a charge is actually made of, which decides
# whether a number is a heap that must be paged or a cache that can simply be
# dropped. pgscan and pgsteal localise reclaim to the cgroup causing it. oom_kill
# from memory.events is the decisive one: a high pressure reading beside a zero
# there means something was killed for being slow, not for being out of memory.
CGROUP_STAT_KEYS = (
    "anon file slab shmem pgscan pgsteal workingset_refault_anon"
).split()
CGROUP_EVENT_KEYS = "oom_kill oom_group_kill high max".split()

# Children below this are noise -- a shell sitting idle -- and sampling every one
# of them multiplies the line size without ever explaining anything.
DEFAULT_CHILD_FLOOR = 32 * 1024 * 1024


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


def parse_swaps(text):
    """Parse /proc/swaps into {device: {'size': kB, 'used': kB, 'prio': n}}.

    Per-device rather than the meminfo total: with swap split across disks by
    priority, which device absorbed the writes is the whole question.
    """
    out = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 5 and fields[2].isdigit():
            out[fields[0]] = {
                "size": int(fields[2]),
                "used": int(fields[3]),
                "prio": int(fields[4]),
            }
    return out


def read(path):
    """Read a file, or return None. A sampler that dies mid-incident is useless."""
    try:
        return path.read_text()
    except OSError:
        return None


def read_int(path):
    text = read(path)
    if text and text.strip().lstrip("-").isdigit():
        return int(text.strip())
    return None


def select(source, keys):
    return {k: source[k] for k in keys if k in source}


def read_cgroup(path):
    """Charge, swap, peak, process count, composition, reclaim and kill counts."""
    out = {}
    for field, name in (
        ("memory.current", "current"),
        ("memory.swap.current", "swap"),
        ("memory.peak", "peak"),
        ("pids.current", "pids"),
    ):
        value = read_int(path / field)
        if value is not None:
            out[name] = value

    text = read(path / "memory.stat")
    if text:
        out.update(select(parse_space_kv(text), CGROUP_STAT_KEYS))

    text = read(path / "memory.events")
    if text:
        out.update(select(parse_space_kv(text), CGROUP_EVENT_KEYS))

    text = read(path / "memory.pressure")
    if text:
        out.update({k: v for k, v in parse_pressure(text).items() if k.endswith("_total")})
    return out


def label(path):
    """Name a cgroup by its path below the hierarchy root, which is unambiguous
    across several sampled roots in a way a basename is not."""
    try:
        return str(path.relative_to(CGROUP_ROOT))
    except ValueError:
        return str(path)


def collect_cgroups(roots, floor):
    """Each root plus its direct children.

    Direct children are the units a pressure-based killer chooses between, so
    sampling them is what names a victim afterwards. Children below the floor are
    skipped: an idle shell explains nothing and there are many of them.
    """
    groups = {}
    for root in roots:
        if not root.is_dir():
            continue
        data = read_cgroup(root)
        if data:
            groups[label(root)] = data
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            data = read_cgroup(child)
            if data and data.get("current", 0) >= floor:
                groups[label(child)] = data
    return groups


def sample(roots=(), floor=DEFAULT_CHILD_FLOOR):
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

    text = read(SWAPS)
    if text:
        swaps = parse_swaps(text)
        if swaps:
            record["swap"] = swaps

    groups = collect_cgroups(roots, floor)
    if groups:
        record["cg"] = groups
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="seconds between samples (default: 10, below the 30s a pressure "
             "rule needs to sustain before it acts)",
    )
    parser.add_argument(
        "--cgroup", type=Path, action="append", default=[], metavar="DIR",
        help="cgroup directory to sample along with its direct children; repeat "
             "to watch several, which is what lets a charge be attributed to one "
             "rather than merely observed somewhere",
    )
    parser.add_argument(
        "--child-floor", type=int, default=DEFAULT_CHILD_FLOOR, metavar="BYTES",
        help="skip children charged less than this (default: 32 MiB)",
    )
    parser.add_argument(
        "--once", action="store_true", help="emit a single sample and exit",
    )
    args = parser.parse_args(argv)

    while True:
        # Deadline before the work, so a slow read shortens the sleep instead of
        # pushing every later sample further out of step.
        deadline = time.monotonic() + args.interval
        record = sample(args.cgroup, args.child_floor)
        # Compact, and flushed every line: an unflushed buffer is lost with the
        # process, which is exactly when the last samples matter most.
        print(json.dumps(record, separators=(",", ":")), flush=True)
        if args.once:
            return 0
        time.sleep(max(0.0, deadline - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
