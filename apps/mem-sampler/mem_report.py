#!/usr/bin/env python3
"""Reconstruct a memory time series from sampled journal lines and plot it.

Reads the JSON lines mem-sampler writes, differences the counters back into
rates, and renders the result to a PNG.

The differencing is the point. Half of what the sampler records is monotonic
since boot, so a single reading carries no information about any particular
moment -- only the change between two adjacent samples does. Everything the
kernel exposes as a running total is turned into a per-second rate here, and
nothing downstream ever sees a raw counter.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

KIB = 1024
GIB = 1024**3

# Categorical slots in fixed order, never cycled. The ordering is what keeps
# adjacent series distinguishable under colour-vision deficiency, so series are
# assigned from the front and a panel that would need a ninth folds instead.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK = "#0b0b0b"
INK_MUTED = "#52514e"
SURFACE = "#fcfcfb"
GRID = "#e4e3df"
CRITICAL = "#d03b3b"

# What a pressure-based kill rule watches. Drawn as an annotated threshold so a
# stall spike can be read against the line that actually triggers a kill.
PRESSURE_LIMIT_PCT = 60.0


def load(lines):
    """Parse JSON lines, skipping anything unparseable.

    Journal output can carry rotation markers and truncated final lines; a
    report that refuses to open because of one bad line is worse than useless.
    """
    records = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "t" in record:
            records.append(record)
    records.sort(key=lambda r: r["t"])
    return records


def dig(record, *path):
    """Fetch a nested value, or None if any level is absent."""
    for key in path:
        if not isinstance(record, dict) or key not in record:
            return None
        record = record[key]
    return record


def gauge(records, *path, scale=1.0):
    """A gauge series: the recorded value is the answer, as-is."""
    out = []
    for record in records:
        value = dig(record, *path)
        out.append(None if value is None else value / scale)
    return out


def rate(records, *path, scale=1.0):
    """A counter series differenced into a per-second rate.

    The first sample has no predecessor and a counter that moves backwards means
    the source was reset (a service restart, a reboot mid-window); both yield
    None rather than a fabricated number or a negative spike.
    """
    out = [None]
    for previous, current in zip(records, records[1:]):
        before, after = dig(previous, *path), dig(current, *path)
        elapsed = current["t"] - previous["t"]
        if before is None or after is None or elapsed <= 0 or after < before:
            out.append(None)
        else:
            out.append((after - before) / elapsed / scale)
    return out


def build(records):
    """Turn raw samples into named, plot-ready series.

    Kept free of matplotlib so the reconstruction can be tested on its own.
    """
    times = [datetime.fromtimestamp(r["t"]) for r in records]

    # meminfo is in kB; the ARC is in bytes and appears in neither the cache nor
    # the free columns, so it has to be subtracted out of the total by hand for
    # the composition to add up to physical memory.
    total = gauge(records, "mem", "MemTotal", scale=GIB / KIB)
    free = gauge(records, "mem", "MemFree", scale=GIB / KIB)
    anon = gauge(records, "mem", "AnonPages", scale=GIB / KIB)
    cached = gauge(records, "mem", "Cached", scale=GIB / KIB)
    arc = gauge(records, "arc", "size", scale=GIB)

    def other(index):
        parts = (total[index], free[index], anon[index], cached[index], arc[index])
        if any(p is None for p in parts):
            return None
        return max(0.0, parts[0] - parts[1] - parts[2] - parts[3] - parts[4])

    panels = [
        {
            "title": "Memory composition",
            "unit": "GiB",
            "kind": "stack",
            "series": [
                ("ZFS ARC", arc),
                ("Process anon", anon),
                ("Page cache", cached),
                ("Kernel & other", [other(i) for i in range(len(records))]),
                ("Free", free),
            ],
        },
        {
            "title": "Memory stall (share of wall clock spent waiting)",
            "unit": "%",
            "kind": "line",
            "threshold": PRESSURE_LIMIT_PCT,
            # PSI totals are microseconds of stall; per second of wall clock
            # that is a fraction, which is exactly the quantity a pressure rule
            # thresholds on.
            "series": [
                ("System (some)", rate(records, "psi", "some_total", scale=10_000)),
                ("System (full)", rate(records, "psi", "full_total", scale=10_000)),
            ],
        },
        {
            "title": "Page reclaim",
            "unit": "pages/s",
            "kind": "line",
            # Direct reclaim runs inside the allocating task, so it is the half
            # that becomes stall; kswapd's half is free of charge to the caller.
            "series": [
                ("Direct (synchronous)", rate(records, "vm", "pgsteal_direct")),
                ("kswapd (background)", rate(records, "vm", "pgsteal_kswapd")),
            ],
        },
        {
            "title": "Paging",
            "unit": "pages/s",
            "kind": "line",
            # Compressed into RAM, spilled from the pool to disk, and written to
            # the swap device: the three stages a page passes through, so a
            # glance says whether the compressed cache is absorbing the load.
            "series": [
                ("Compressed (zswpout)", rate(records, "vm", "zswpout")),
                ("Spilled to disk (zswpwb)", rate(records, "vm", "zswpwb")),
                ("Swapped out (pswpout)", rate(records, "vm", "pswpout")),
            ],
        },
    ]

    cgroups = cgroup_panel(records)
    if cgroups:
        panels.append(cgroups)
    return times, panels


def cgroup_panel(records, limit=5):
    """Per-child memory, which is what names the victim of a cgroup-scoped kill.

    Children are ranked by peak and the tail folded into one series rather than
    cycling colours past the end of the categorical order.
    """
    names = set()
    for record in records:
        for name in dig(record, "cg") or {}:
            if name:
                names.add(name)
    if not names:
        return None

    tracks = {n: gauge(records, "cg", n, "current", scale=GIB) for n in names}
    # systemd names a transient scope run-p<pid>-i<id>.scope; only the pid
    # distinguishes one from another, and it is what the journal logs.
    short = {n: (re.match(r"run-(p\d+)-i\d+\.scope$", n) or [None, n])[1] for n in names}
    ranked = sorted(names, key=lambda n: max((v or 0) for v in tracks[n]), reverse=True)
    head, tail = ranked[:limit], ranked[limit:]

    series = [(short[n], tracks[n]) for n in head]
    if tail:
        folded = []
        for index in range(len(records)):
            values = [tracks[n][index] for n in tail if tracks[n][index] is not None]
            folded.append(sum(values) if values else None)
        series.append((f"Other ({len(tail)})", folded))
    return {
        "title": "Per-cgroup memory",
        "unit": "GiB",
        "kind": "line",
        "series": series,
    }


def render(times, panels, out_path):
    """Draw the panels to a PNG. matplotlib is imported here so that everything
    above stays importable -- and testable -- without it."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        len(panels), 1, figsize=(13, 2.6 * len(panels)), sharex=True,
        facecolor=SURFACE, constrained_layout=True,
    )
    axes = axes if len(panels) > 1 else [axes]

    for axis, panel in zip(axes, panels):
        axis.set_facecolor(SURFACE)
        drawn = []
        labelled = [(n, v) for n, v in panel["series"] if any(x is not None for x in v)]

        if panel["kind"] == "stack":
            # A stack has to be gapless, so missing samples become zero here
            # rather than breaking the area into disconnected islands.
            values = [[x or 0.0 for x in v] for _, v in labelled]
            axis.stackplot(
                times, *values, labels=[n for n, _ in labelled],
                colors=SERIES[: len(labelled)],
                # A thin surface-coloured seam keeps adjacent bands legible
                # where two similar values meet.
                edgecolor=SURFACE, linewidth=1.2,
            )
        else:
            ends = []
            for slot, (name, values) in enumerate(labelled):
                axis.plot(times, values, label=name, color=SERIES[slot % len(SERIES)],
                          linewidth=1.8, solid_capstyle="round")
                last = next((v for v in reversed(values) if v is not None), None)
                if last is not None:
                    ends.append(last)

        if panel.get("threshold") is not None:
            axis.axhline(panel["threshold"], color=CRITICAL, linewidth=1.2,
                         linestyle=(0, (5, 3)), zorder=1)
            axis.annotate(
                f"kill threshold {panel['threshold']:.0f}%",
                xy=(0.004, panel["threshold"]), xycoords=("axes fraction", "data"),
                xytext=(0, 4), textcoords="offset points",
                fontsize=8, color=CRITICAL,
            )

        columns = min(len(labelled), 5)
        rows = -(-len(labelled) // columns) if labelled else 0
        axis.set_title(panel["title"], loc="left", fontsize=11, color=INK,
                       pad=10 + 15 * rows if len(labelled) > 1 else 8)
        axis.set_ylabel(panel["unit"], fontsize=9, color=INK_MUTED)
        axis.tick_params(colors=INK_MUTED, labelsize=8)
        axis.grid(axis="y", color=GRID, linewidth=0.8)
        axis.set_axisbelow(True)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            axis.spines[side].set_color(GRID)
        axis.set_ylim(bottom=0)
        if panel["kind"] != "stack":
            # One direct label per series at its final value. Labels closer
            # together than a line of text are dropped rather than overprinted,
            # which at idle is most of them.
            span = axis.get_ylim()[1] or 1.0
            for value in sorted(ends, reverse=True):
                if any(abs(value - placed) < span * 0.06 for placed in drawn):
                    continue
                drawn.append(value)
                axis.annotate(
                    f"{value:,.0f}" if value >= 10 else f"{value:,.2f}",
                    xy=(times[-1], value), xytext=(6, 0),
                    textcoords="offset points", va="center",
                    fontsize=8, color=INK_MUTED, annotation_clip=False,
                )
        axis.set_xlim(times[0], times[-1])
        # Identity is never carried by colour alone: every panel with more than
        # one series keeps a legend, and each line is directly labelled too.
        if len(labelled) > 1:
            # Anchored above the axes rather than inside them: at idle every
            # series sits near zero, and a legend placed in the plot area lands
            # squarely on the data it is meant to explain.
            axis.legend(
                loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8,
                frameon=False, ncols=columns, labelcolor=INK_MUTED,
                borderaxespad=0, handlelength=1.6, columnspacing=1.4,
            )

    # A minute of samples and a week of them cannot share a tick format.
    minutes = (times[-1] - times[0]).total_seconds() / 60
    axes[-1].xaxis.set_major_formatter(
        mdates.DateFormatter(
            "%H:%M:%S" if minutes < 15 else "%H:%M" if minutes < 24 * 60 else "%d %H:%M"
        )
    )
    fig.savefig(out_path, dpi=140, facecolor=SURFACE)
    return out_path


def read_journal(unit, since, until):
    argv = ["journalctl", "-u", unit, "-o", "cat", "--no-pager"]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"journalctl failed: {result.stderr.strip()}")
    return result.stdout.splitlines()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", default="mem-sampler.service")
    parser.add_argument("--since", help="passed through to journalctl")
    parser.add_argument("--until", help="passed through to journalctl")
    parser.add_argument(
        "--input", help="read samples from a file, or '-' for stdin, "
                        "instead of querying the journal",
    )
    parser.add_argument("-o", "--output", type=Path, help="PNG path")
    args = parser.parse_args(argv)

    # The journal is the default source whether or not there is a terminal
    # attached. Inferring the source from stdin instead means that every
    # non-interactive caller -- a script, a pipe, a timer -- silently reads an
    # empty stdin and reports having found no samples.
    if args.input == "-":
        lines = sys.stdin.read().splitlines()
    elif args.input:
        lines = Path(args.input).read_text().splitlines()
    else:
        lines = read_journal(args.unit, args.since, args.until)

    records = load(lines)
    if len(records) < 2:
        sys.exit(f"need at least 2 samples to difference counters, got {len(records)}")

    times, panels = build(records)
    out = args.output or Path(
        f"/tmp/mem-report-{times[0]:%Y%m%dT%H%M}-{times[-1]:%H%M}.png"
    )
    render(times, panels, out)
    span = (times[-1] - times[0]).total_seconds() / 60
    print(f"wrote {out}  ({len(records)} samples over {span:.0f} min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
