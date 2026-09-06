"""Unit tests for mem-sampler's parsers and mem-report's reconstruction.

The parsers exist to read kernel file formats, which change without warning and
fail silently when they do; the reconstruction exists to turn since-boot counters
into rates, which is the step that is easy to get wrong in a way that produces a
plausible-looking but meaningless number.
"""

import importlib.util
import os
import sys
from pathlib import Path

# The build sandbox runs this file from the store, where the repo layout it
# would otherwise walk up to does not exist, so the sources under test are
# located by environment there and by relative path in a working copy.
_APP = Path(
    os.environ.get(
        "MEM_SAMPLER_SRC", Path(__file__).parent.parent / "apps" / "mem-sampler"
    )
)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _APP / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mem_sampler = _load("mem_sampler", "mem_sampler.py")
mem_report = _load("mem_report", "mem_report.py")


# --- parsers -------------------------------------------------------------


def test_meminfo_keeps_numeric_lines_and_drops_units():
    text = "MemTotal:       41014664 kB\nMemFree:         5505516 kB\n"
    assert mem_sampler.parse_colon_kv(text) == {
        "MemTotal": 41014664,
        "MemFree": 5505516,
    }


def test_meminfo_skips_non_numeric_entries():
    # Some meminfo lines carry no value at all on kernels without the feature.
    assert mem_sampler.parse_colon_kv("HugePages_Total:\nDirty: 280 kB\n") == {
        "Dirty": 280
    }


def test_arcstats_skips_the_two_header_lines():
    text = (
        "13 1 0x01 103 28016 4 5\n"
        "name                            type data\n"
        "size                            4    11675997984\n"
        "c_max                           4    21474836480\n"
    )
    assert mem_sampler.parse_arcstats(text) == {
        "size": 11675997984,
        "c_max": 21474836480,
    }


def test_vmstat_parses_bare_pairs():
    assert mem_sampler.parse_space_kv("pgsteal_direct 1172130\nzswpout 8796\n") == {
        "pgsteal_direct": 1172130,
        "zswpout": 8796,
    }


def test_pressure_splits_gauges_from_the_total_counter():
    text = "some avg10=90.15 avg60=44.85 avg300=13.71 total=88559932\nfull avg10=0.00 total=83638412\n"
    parsed = mem_sampler.parse_pressure(text)
    assert parsed["some_avg10"] == 90.15
    # total is a microsecond counter and must stay an exact integer: it is
    # differenced later, where float rounding would show up as phantom stall.
    assert parsed["some_total"] == 88559932
    assert isinstance(parsed["some_total"], int)
    assert parsed["full_total"] == 83638412


def test_reads_return_none_rather_than_raising(tmp_path):
    # A sampler that dies on a missing file records nothing about the incident
    # that removed it.
    assert mem_sampler.read(tmp_path / "absent") is None


def test_sample_survives_a_missing_cgroup(tmp_path):
    record = mem_sampler.sample([tmp_path / "absent"])
    assert "t" in record and "cg" not in record


def _cgroup(path, current, **files):
    path.mkdir(parents=True, exist_ok=True)
    (path / "memory.current").write_text(f"{current}\n")
    for name, text in files.items():
        (path / name.replace("_", ".", 1)).write_text(text)
    return path


def test_sample_reads_a_root_and_its_children(tmp_path):
    root = _cgroup(tmp_path / "zjpanes.slice", 1000)
    _cgroup(root / "pane.scope", 512,
            memory_pressure="some total=42\n",
            memory_stat="anon 400\nfile 100\npgsteal 7\n",
            memory_events="oom_kill 0\n",
            pids_current="784\n")

    groups = mem_sampler.sample([root], floor=0)["cg"]
    assert groups[str(root)]["current"] == 1000
    pane = groups[str(root / "pane.scope")]
    assert pane["current"] == 512
    assert pane["some_total"] == 42
    # anon against file is what says whether a charge is a heap that has to be
    # paged or a cache that can simply be dropped.
    assert pane["anon"] == 400 and pane["file"] == 100
    assert pane["pgsteal"] == 7
    assert pane["pids"] == 784
    assert pane["oom_kill"] == 0


def test_sample_skips_children_below_the_floor(tmp_path):
    root = _cgroup(tmp_path / "slice", 10_000_000_000)
    _cgroup(root / "idle.scope", 1024)
    _cgroup(root / "busy.scope", 5_000_000_000)

    groups = mem_sampler.sample([root])["cg"]
    # An idle shell explains nothing and there are many of them; sampling every
    # one multiplies the line size without ever answering a question.
    assert str(root / "idle.scope") not in groups
    assert str(root / "busy.scope") in groups


def test_sample_reads_several_roots(tmp_path):
    # Watching more than one root is what lets a charge be attributed to a
    # particular slice rather than merely observed somewhere on the machine.
    a = _cgroup(tmp_path / "a.slice", 1)
    b = _cgroup(tmp_path / "b.slice", 2)
    groups = mem_sampler.sample([a, b], floor=0)["cg"]
    assert {str(a), str(b)} <= set(groups)


def test_parse_swaps_keeps_each_device_separate():
    text = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
            "/dev/sde2                               partition\t50331644\t5662720\t-2\n"
            "/dev/sdd6                               partition\t104857596\t0\t100\n")
    swaps = mem_sampler.parse_swaps(text)
    # With swap split across disks by priority, which device absorbed the writes
    # is the whole question, and the meminfo total cannot answer it.
    assert swaps["/dev/sde2"] == {"size": 50331644, "used": 5662720, "prio": -2}
    assert swaps["/dev/sdd6"]["prio"] == 100


# --- reconstruction ------------------------------------------------------


def _records(*samples):
    return [{"t": t, "vm": {"c": c}} for t, c in samples]


def test_rate_differences_a_counter_into_per_second():
    series = mem_report.rate(_records((0.0, 100), (10.0, 300)), "vm", "c")
    # The first sample has no predecessor, so it cannot yield a rate.
    assert series == [None, 20.0]


def test_rate_rejects_a_counter_that_went_backwards():
    # A reboot or service restart resets the counter; the difference would
    # otherwise render as a large negative spike that never happened.
    assert mem_report.rate(_records((0.0, 500), (10.0, 5)), "vm", "c") == [None, None]


def test_rate_handles_a_duplicated_timestamp():
    assert mem_report.rate(_records((5.0, 1), (5.0, 9)), "vm", "c") == [None, None]


def test_rate_returns_none_where_the_field_is_absent():
    records = [{"t": 0.0, "vm": {}}, {"t": 10.0, "vm": {"c": 10}}]
    assert mem_report.rate(records, "vm", "c") == [None, None]


def test_gauge_passes_the_value_through_scaled():
    records = [{"t": 0.0, "mem": {"MemFree": 2048}}]
    assert mem_report.gauge(records, "mem", "MemFree", scale=1024) == [2.0]


def test_load_skips_unparseable_lines_and_sorts_by_time():
    lines = [
        '{"t": 20.0}',
        "-- Journal begins at Fri 2026-09-04 --",
        '{"t": 10.0}',
        '{"t": 15.0',  # truncated final line, as a killed process leaves
        "",
    ]
    assert [r["t"] for r in mem_report.load(lines)] == [10.0, 20.0]


def test_load_rejects_json_without_a_timestamp():
    assert mem_report.load(['{"mem": {}}', '{"t": 1.0}']) == [{"t": 1.0}]


def test_build_produces_panels_without_importing_matplotlib():
    records = [
        {
            "t": float(t),
            "mem": {"MemTotal": 8 << 20, "MemFree": 1 << 20, "AnonPages": 1 << 20,
                    "Cached": 1 << 20},
            "arc": {"size": 2 * 1024**3},
            "vm": {"pgsteal_direct": t * 10, "pgsteal_kswapd": t * 5,
                   "zswpout": t, "zswpwb": 0, "pswpout": 0},
            "psi": {"some_total": t * 1000, "full_total": 0},
        }
        for t in (0, 10, 20)
    ]
    _, panels = mem_report.build(records)
    titles = [p["title"] for p in panels]
    assert "Memory composition" in titles[0]
    assert "matplotlib" not in sys.modules


def test_composition_never_goes_negative():
    # The ARC is counted in neither the cache nor the free column, so the
    # remainder is computed by subtraction and could underflow on a sample where
    # the sources were read a moment apart.
    records = [
        {"t": float(t), "mem": {"MemTotal": 1 << 20, "MemFree": 1 << 20,
                                "AnonPages": 1 << 20, "Cached": 1 << 20},
         "arc": {"size": 4 * 1024**3}}
        for t in (0, 10)
    ]
    _, panels = mem_report.build(records)
    other = dict(panels[0]["series"])["Kernel & other"]
    assert all(v >= 0 for v in other)


def test_cgroup_panel_folds_the_tail_into_other():
    records = [
        {"t": float(t), "cg": {name: {"current": size} for name, size in
                               (("a", 90), ("b", 80), ("c", 70), ("d", 5), ("e", 4))}}
        for t in (0, 10)
    ]
    panel = mem_report.cgroup_panel(records, limit=3)
    names = [n for n, _ in panel["series"]]
    # Colours are assigned from a fixed order and never cycled, so a panel that
    # would run past the end folds its tail rather than reusing a hue.
    assert names[:3] == ["a", "b", "c"]
    assert names[3] == "Other (2)"


def test_cgroup_panel_shortens_transient_scope_names_to_the_pid():
    records = [
        {"t": float(t), "cg": {"run-p15906-i15907.scope": {"current": 9}}}
        for t in (0, 10)
    ]
    panel = mem_report.cgroup_panel(records)
    assert [n for n, _ in panel["series"]] == ["p15906"]


def test_shorten_strips_the_path_and_the_unit_suffix():
    assert mem_report.shorten("user.slice/zjpanes.slice/run-p15906-i15907.scope") == "p15906"
    assert mem_report.shorten("system.slice/open-webui.service") == "open-webui"
    assert mem_report.shorten("system.slice") == "system"


def test_leaves_drops_a_slice_that_has_sampled_children():
    # Plotting a slice beside its own children double counts every charge and
    # makes the largest line meaningless.
    names = ["a.slice", "a.slice/one.scope", "a.slice/two.scope", "b.slice"]
    assert set(mem_report.leaves(names)) == {"a.slice/one.scope", "a.slice/two.scope",
                                             "b.slice"}


def test_leaves_keeps_a_root_with_no_sampled_children():
    assert mem_report.leaves(["only.slice"]) == ["only.slice"]


def test_ratio_is_computed_per_interval_not_since_boot():
    # A hit rate from one reading is a since-boot average that flatters every
    # cache; only the difference describes the workload that was running.
    records = [
        {"t": 0.0, "arc": {"h": 900, "m": 100}},    # 90% up to here
        {"t": 10.0, "arc": {"h": 900, "m": 1100}},  # but 0% during this interval
    ]
    assert mem_report.ratio(records, ("arc", "h"), ("arc", "m")) == [None, 0.0]


def test_ratio_yields_none_when_nothing_was_looked_up():
    records = [{"t": 0.0, "arc": {"h": 5, "m": 1}}, {"t": 10.0, "arc": {"h": 5, "m": 1}}]
    assert mem_report.ratio(records, ("arc", "h"), ("arc", "m")) == [None, None]


def test_vanished_reports_a_large_cgroup_that_stopped_existing():
    # A scoped kill leaves no trace in the cgroup it destroys, so absence is the
    # only evidence in the record.
    records = [
        {"t": 0.0, "cg": {"z/victim.scope": {"current": 12 * 1024**3}}},
        {"t": 10.0, "cg": {"z/victim.scope": {"current": 12 * 1024**3}}},
        {"t": 20.0, "cg": {}},
    ]
    assert [n for _, n, _ in mem_report.vanished(records)] == ["z/victim.scope"]


def test_vanished_ignores_a_cgroup_that_merely_shrank_below_the_floor():
    records = [
        {"t": 0.0, "cg": {"z/small.scope": {"current": 1024}}},
        {"t": 10.0, "cg": {}},
    ]
    assert mem_report.vanished(records) == []


def test_vanished_ignores_a_cgroup_still_present_in_the_last_sample():
    records = [
        {"t": 0.0, "cg": {"z/alive.scope": {"current": 12 * 1024**3}}},
        {"t": 10.0, "cg": {"z/alive.scope": {"current": 12 * 1024**3}}},
    ]
    assert mem_report.vanished(records) == []


def test_cgroup_panel_can_plot_process_counts():
    records = [
        {"t": float(t), "cg": {"z/a.scope": {"current": 1, "pids": 784}}}
        for t in (0, 10)
    ]
    panel = mem_report.cgroup_panel(records, field="pids", unit="processes", scale=1)
    assert panel["series"][0] == ("a", [784.0, 784.0])


def test_cgroup_panel_is_absent_without_data():
    assert mem_report.cgroup_panel([{"t": 0.0}]) is None


def test_report_reads_a_named_file_without_touching_the_journal(tmp_path, monkeypatch):
    # The source must follow the arguments, not the presence of a terminal: a
    # non-interactive caller that fell back to stdin would silently report zero
    # samples rather than reading the journal.
    def fail(*args, **kwargs):
        raise AssertionError("journal must not be queried when --input is given")

    monkeypatch.setattr(mem_report, "read_journal", fail)
    monkeypatch.setattr(mem_report, "render", lambda *a: a[-1])
    samples = tmp_path / "s.jsonl"
    samples.write_text(
        '{"t": 0.0, "mem": {"MemTotal": 8, "MemFree": 1, "AnonPages": 1, "Cached": 1}}\n'
        '{"t": 10.0, "mem": {"MemTotal": 8, "MemFree": 1, "AnonPages": 1, "Cached": 1}}\n'
    )
    out = tmp_path / "out.png"
    assert mem_report.main(["--input", str(samples), "-o", str(out)]) == 0


def test_summary_only_calls_a_death_a_stall_kill_when_something_died(capsys):
    # The verdict is about how to read a death. Printed with nothing to explain,
    # it reads as a finding on an idle machine.
    quiet = [{"t": float(t), "cg": {}} for t in (0, 10)]
    mem_report.summarise(quiet, marks=[])
    assert "stall kill" not in capsys.readouterr().out

    dead = [
        {"t": 0.0, "cg": {"z/v.scope": {"current": 12 * 1024**3, "oom_kill": 0}}},
        {"t": 10.0, "cg": {}},
    ]
    mem_report.summarise(dead, mem_report.vanished(dead))
    out = capsys.readouterr().out
    assert "stall kill" in out and "v (last seen 12.00 GiB)" in out


def test_summary_names_a_real_kernel_oom_kill(capsys):
    records = [
        {"t": 0.0, "cg": {"z/v.scope": {"current": 1, "oom_kill": 0}}},
        {"t": 10.0, "cg": {"z/v.scope": {"current": 1, "oom_kill": 1}}},
    ]
    mem_report.summarise(records, marks=[])
    assert "genuinely ran out of memory" in capsys.readouterr().out


def test_render_writes_html_unless_a_raster_is_asked_for(tmp_path, monkeypatch):
    # Format follows the requested filename rather than a flag, so a caller that
    # asks for one and silently receives the other cannot happen.
    chosen = []
    monkeypatch.setattr(mem_report, "render_png", lambda *a, **k: chosen.append("png"))
    monkeypatch.setattr(mem_report, "render_html", lambda *a, **k: chosen.append("html"))

    mem_report.render([], [], tmp_path / "r.png")
    mem_report.render([], [], tmp_path / "r.html")
    mem_report.render([], [], tmp_path / "r")
    assert chosen == ["png", "html", "html"]


def test_html_escapes_values_that_come_from_cgroup_names(tmp_path):
    # Names reach the page from the cgroup tree, so they are data rather than
    # markup and must not be able to close a tag.
    from datetime import datetime

    out = tmp_path / "r.html"
    stamps = [datetime(2026, 9, 6, 11, 0), datetime(2026, 9, 6, 12, 0)]
    mem_report.render_html(stamps, [], out, marks=[(0, "z/<script>x", 1)],
                           rows=[("note", "a & b")])
    page = out.read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page and "a &amp; b" in page
