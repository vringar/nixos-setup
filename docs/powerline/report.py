#!/usr/bin/env python3
"""
Powerline network report tool.

Discovers all HomePlug AV adapters on the network, asks the user to name them,
then prints a PHY rate matrix showing TX/RX speeds between all pairs as
reported by each adapter.

Requires: open-plc-utils (plctool, plcrate) and root privileges.

Usage:
    sudo python3 report.py [-i INTERFACE]
    sudo python3 report.py [-i INTERFACE] --names '{"74:42:7F:F7:AE:31": "Living Room", ...}'
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAMES_FILE = Path(__file__).parent / ".powerline-names.json"


def check_root():
    if os.geteuid() != 0:
        sys.exit("Error: this script requires root (raw sockets). Run with sudo.")


def find_tool(name: str) -> str:
    """Find a tool in PATH or common nix store locations."""
    result = subprocess.run(["which", name], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    sys.exit(f"Error: '{name}' not found in PATH. Enter a nix-shell with open-plc-utils first.")


def run(cmd: list[str], timeout: int = 10) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout + result.stderr


def resolve_ips(interface: str) -> dict[str, str]:
    """
    Build a MAC->IP mapping by pinging the broadcast address to populate
    the ARP table, then reading it back.
    Returns: {mac_upper: ip}
    """
    # Get our broadcast address from the interface
    addr_output = run(["ip", "-4", "addr", "show", "dev", interface])
    m = re.search(r"brd (\d+\.\d+\.\d+\.\d+)", addr_output)
    if not m:
        return {}

    brd = m.group(1)
    # Broadcast ping to populate ARP table (best-effort, ignore errors)
    subprocess.run(
        ["ping", "-b", "-c", "2", "-W", "1", "-I", interface, brd],
        capture_output=True, timeout=5,
    )

    mac_to_ip: dict[str, str] = {}

    # Read ARP table from ip neigh
    arp_output = run(["ip", "neigh", "show", "dev", interface])
    for line in arp_output.splitlines():
        # "192.168.178.35 lladdr 74:42:7f:f7:ae:31 REACHABLE"
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "lladdr":
            ip, mac = parts[0], parts[3].upper()
            if mac != "00:00:00:00:00:00" and ":" in ip:
                continue  # skip IPv6
            if mac != "00:00:00:00:00:00":
                mac_to_ip[mac] = ip

    # Also read /proc/net/arp as fallback for STALE entries
    try:
        with open("/proc/net/arp") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6 and parts[5] == interface:
                    ip, mac = parts[0], parts[3].upper()
                    if mac != "00:00:00:00:00:00" and mac not in mac_to_ip:
                        mac_to_ip[mac] = ip
    except OSError:
        pass

    return mac_to_ip


def discover_network(interface: str) -> dict:
    """
    Run plctool -m on the local adapter to discover the network.
    Returns dict: {source_mac: {"stations": {remote_mac: {"tx": int, "rx": int, "bda": str}}, "role": str}}
    """
    output = run(["plctool", "-i", interface, "-m"])
    return parse_network_info(output)


def parse_network_info(output: str) -> dict:
    """Parse plctool -m output into structured data."""
    devices = {}
    current_source = None
    current_station = None

    for line in output.splitlines():
        # "source address = 74:42:7F:F7:AE:31"
        m = re.match(r"\s*source address = ([0-9A-Fa-f:]{17})", line)
        if m:
            current_source = m.group(1).upper()
            devices[current_source] = {"stations": {}, "role": "STA"}
            continue

        if current_source is None:
            continue

        # "network->ROLE = 0x02 (CCO)"
        m = re.match(r"\s*network->ROLE = 0x\d+ \((\w+)\)", line)
        if m:
            devices[current_source]["role"] = m.group(1)
            continue

        # "station->MAC = 74:42:7F:F7:AE:33"
        m = re.match(r"\s*station->MAC = ([0-9A-Fa-f:]{17})", line)
        if m:
            current_station = m.group(1).upper()
            devices[current_source]["stations"][current_station] = {"tx": 0, "rx": 0, "bda": ""}
            continue

        if current_station is None:
            continue

        # "station->BDA = DC:15:C8:28:81:56"
        m = re.match(r"\s*station->BDA = ([0-9A-Fa-f:]{17})", line)
        if m:
            devices[current_source]["stations"][current_station]["bda"] = m.group(1).upper()
            continue

        # "station->AvgPHYDR_TX = 506 mbps Alternate"
        m = re.match(r"\s*station->AvgPHYDR_TX = (\d+) mbps", line)
        if m:
            devices[current_source]["stations"][current_station]["tx"] = int(m.group(1))
            continue

        m = re.match(r"\s*station->AvgPHYDR_RX = (\d+) mbps", line)
        if m:
            devices[current_source]["stations"][current_station]["rx"] = int(m.group(1))
            continue

    return devices


def query_remote_adapter(interface: str, mac: str) -> dict:
    """Query a specific remote adapter for its view of the network."""
    output = run(["plctool", "-i", interface, "-m", mac])
    return parse_network_info(output)


def get_full_rate_matrix(interface: str, all_macs: list[str]) -> dict:
    """
    Query each adapter for its PHY rates to all peers.
    Returns: {source_mac: {dest_mac: {"tx": int, "rx": int}}}
    """
    matrix = {}
    for mac in all_macs:
        info = query_remote_adapter(interface, mac)
        if mac in info:
            matrix[mac] = {
                peer: {"tx": data["tx"], "rx": data["rx"]}
                for peer, data in info[mac]["stations"].items()
            }
        else:
            # Adapter didn't respond — might be unreachable
            matrix[mac] = {}
    return matrix


def load_names() -> dict:
    """Load saved adapter names from disk."""
    if NAMES_FILE.exists():
        return json.loads(NAMES_FILE.read_text())
    return {}


def save_names(names: dict):
    """Persist adapter names to disk."""
    NAMES_FILE.write_text(json.dumps(names, indent=2) + "\n")


def prompt_names(
    all_macs: list[str],
    roles: dict,
    bdas: dict,
    ips: dict,
    existing_names: dict,
) -> dict:
    """Interactively ask user to name each adapter."""
    names = dict(existing_names)
    print("\n--- Adapter Naming ---")
    print("Press Enter to keep existing name, or type a new one.\n")
    for mac in sorted(all_macs):
        role = roles.get(mac, "?")
        bda = bdas.get(mac, "")
        ip = ips.get(mac, "")
        parts = [f"role={role}"]
        if ip:
            parts.append(f"ip={ip}")
        if bda:
            parts.append(f"bridged={bda}")
        hint = "  [" + ", ".join(parts) + "]"
        current = names.get(mac, "")
        if current:
            new = input(f"  {mac}{hint}\n    Name [{current}]: ").strip()
        else:
            new = input(f"  {mac}{hint}\n    Name: ").strip()
        if new:
            names[mac] = new
        elif not current:
            names[mac] = mac  # fallback to MAC if user skips
    return names


def format_rate(tx: int, rx: int) -> str:
    """Format a TX/RX pair for table display."""
    if tx == 0 and rx == 0:
        return "  --  "
    return f"{tx:>3}/{rx:<3}"


def print_matrix(matrix: dict, names: dict, all_macs: list[str]):
    """Print the rate matrix as a table."""
    # Determine column width based on longest name
    col_width = max(len(names.get(mac, mac)) for mac in all_macs)
    col_width = max(col_width, 7)  # minimum for "TX/RX" header

    # Header
    print("\n--- PHY Rate Matrix (TX/RX in mbps, as seen from row adapter) ---\n")
    header = " " * (col_width + 2) + "| " + " | ".join(
        names.get(mac, mac).center(col_width) for mac in all_macs
    ) + " |"
    print(header)
    print("-" * len(header))

    # Rows
    for src in all_macs:
        row_label = names.get(src, src).ljust(col_width)
        cells = []
        for dst in all_macs:
            if src == dst:
                cells.append("  --  ".center(col_width))
            elif dst in matrix.get(src, {}):
                rate = matrix[src][dst]
                cells.append(format_rate(rate["tx"], rate["rx"]).center(col_width))
            else:
                cells.append("  ??  ".center(col_width))
        print(f"{row_label}  | " + " | ".join(cells) + " |")

    print()
    print("Each cell shows TX/RX mbps from the row adapter to the column adapter.")
    print("Note: PHY rates are gross; effective throughput is ~50-70% of reported.")


def main():
    parser = argparse.ArgumentParser(description="Powerline network PHY rate report")
    parser.add_argument("-i", "--interface", default="enp4s0",
                        help="Network interface connected to a powerline adapter (default: enp4s0)")
    parser.add_argument("--names", type=str, default=None,
                        help='JSON dict of MAC->name mappings, e.g. \'{"AA:BB:...": "Office"}\'')
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip interactive naming (use saved or MAC addresses)")
    args = parser.parse_args()

    check_root()
    find_tool("plctool")

    # Step 1: Discover network from local adapter
    print(f"Querying local adapter on {args.interface}...")
    local_info = discover_network(args.interface)

    if not local_info:
        sys.exit("Error: no adapters responded. Check interface and cable.")

    # Collect all MACs (local + all stations)
    all_macs = list(local_info.keys())
    roles = {}
    bdas = {}
    for src, data in local_info.items():
        roles[src] = data["role"]
        for peer_mac, peer_data in data["stations"].items():
            if peer_mac not in all_macs:
                all_macs.append(peer_mac)
            bdas[peer_mac] = peer_data.get("bda", "?")

    print(f"Found {len(all_macs)} adapters on the network.")

    # Step 2: Resolve adapter IPs via ARP
    print("Resolving adapter IPs...")
    mac_to_ip = resolve_ips(args.interface)

    # Step 3: Name adapters
    existing_names = load_names()
    if args.names:
        existing_names.update(json.loads(args.names))

    if args.no_prompt:
        names = existing_names
    else:
        names = prompt_names(all_macs, roles, bdas, mac_to_ip, existing_names)
        save_names(names)

    # Step 4: Query each adapter for the full rate matrix
    print("\nQuerying all adapters for PHY rates...")
    matrix = get_full_rate_matrix(args.interface, all_macs)

    # Step 5: Print the matrix
    print_matrix(matrix, names, all_macs)

    # Step 6: Flag potential issues
    print("\n--- Health Notes ---")
    issues = []
    for src in all_macs:
        for dst, rates in matrix.get(src, {}).items():
            if rates["tx"] > 0 and rates["rx"] > 0:
                ratio = max(rates["tx"], rates["rx"]) / max(min(rates["tx"], rates["rx"]), 1)
                if ratio > 3:
                    issues.append(
                        f"  ! {names.get(src, src)} -> {names.get(dst, dst)}: "
                        f"high asymmetry ({rates['tx']}/{rates['rx']} mbps, ratio {ratio:.1f}x)"
                    )
                if min(rates["tx"], rates["rx"]) < 50:
                    issues.append(
                        f"  ! {names.get(src, src)} -> {names.get(dst, dst)}: "
                        f"low rate ({rates['tx']}/{rates['rx']} mbps) — may affect streaming"
                    )

    if issues:
        for issue in issues:
            print(issue)
    else:
        print("  All links look healthy.")


if __name__ == "__main__":
    main()
