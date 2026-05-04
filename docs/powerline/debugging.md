# Powerline Network Debugging

Debugging guide for the Fritz!Powerline 1220E (QCA7500 chipset) HomePlug AV2 network.

## Network Topology

```
[FritzBox] <--ethernet--> [Hallway] <--powerline--> [Living Room] <--powerline--> [Office]
  dc:15:c8:28:81:56       74:42:7F:F7:AE:33          74:42:7F:F7:AE:31            98:9B:CB:85:4E:48
  192.168.178.1                                       BDA: 18:C0:4D:29:14:DF       BDA: D8:BB:C1:37:B2:4B
                                                      (PC, enp4s0)                 (Office PC)
```

A fourth adapter is connected to the TV — its MAC can be found via `plctool -m`.

The Office adapter is the CCO (Central Coordinator).

## Tools

All commands require `sudo` (raw sockets) and the `open-plc-utils` package:

```bash
nix-shell -E 'let pkgs = import <nixpkgs> {}; in pkgs.mkShell { packages = [ (pkgs.callPackage /home/vringar/nixos-setup/apps/open-plc-utils/default.nix {}) ]; }'
```

Man pages are available inside the shell (`man plctool`, `man plcrate`, etc.).

## Quick Health Check

The fastest way to check overall network health is the report script:

```bash
sudo python3 docs/powerline/report.py
```

This discovers all adapters, asks you to name them (names are saved for next
time in `docs/powerline/.powerline-names.json`), then prints a full PHY rate
matrix from every adapter's perspective. Pass `--no-prompt` to skip naming.

### Manual commands

Run from the PC connected to the Living Room adapter via `enp4s0`:

```bash
# List all adapters and their link rates (from Living Room's perspective)
sudo plctool -i enp4s0 -m

# Compact rate overview
sudo plcrate -i enp4s0 -n
```

## Querying Remote Adapters

By default, commands talk to the **local** adapter (Living Room, directly connected via Ethernet).
To query a **remote** adapter, pass its powerline MAC as an argument.
The local adapter forwards the management message over powerline.

```bash
# Query Hallway adapter — shows its rates to Living Room and Office
sudo plctool -i enp4s0 -m 74:42:7F:F7:AE:33

# Query Office adapter — shows its rates to Living Room and Hallway
sudo plctool -i enp4s0 -m 98:9B:CB:85:4E:48
```

This is how you get **Office <-> Hallway** throughput: query either the Office or
Hallway adapter and look at the rate to the other.

## Diagnosing an Outage

### Step 1: Check physical link

```bash
ip link show enp4s0
```

- `state UP` = Ethernet link to Living Room adapter is alive.
- `state DOWN` = Cable or adapter Ethernet port issue.

### Step 2: Check if DHCP succeeded

```bash
ip addr show enp4s0 | grep inet
```

- Has a `192.168.178.x` address = full connectivity to FritzBox.
- Only `fe80::` link-local = powerline link to Hallway (and FritzBox) is broken.

### Step 3: Identify which adapter dropped

```bash
sudo plctool -i enp4s0 -m
```

Compare the station list to the known topology above. A missing adapter has
dropped off the powerline network.

### Step 4: Capture traffic for analysis

```bash
nix-shell -p tcpdump --run "sudo tcpdump -i enp4s0 -c 50 -nn -e"
```

Look for:
- **Ethertype 0x8912** = HomePlug AV management frames (adapters trying to find peers).
- **ARP requests for 192.168.178.1 with no reply** = devices stranded from the FritzBox.
- **DHCP requests from 0.0.0.0** = devices (including adapters) trying to get an IP.

### Step 5: Check adapter firmware version

```bash
sudo plctool -i enp4s0 -r              # local adapter
sudo plctool -i enp4s0 -r 74:42:7F:F7:AE:33  # Hallway
sudo plctool -i enp4s0 -r 98:9B:CB:85:4E:48  # Office
```

## Game Streaming Diagnostics

For game streaming (Office -> TV via powerline), the relevant metrics are the
PHY rates between those two adapters. Run:

```bash
sudo python3 docs/powerline/report.py --no-prompt
```

Or manually query the Office adapter:

```bash
sudo plctool -i enp4s0 -m 98:9B:CB:85:4E:48
```

Look at `AvgPHYDR_TX` and `AvgPHYDR_RX` for the TV adapter's station entry.

### Interpreting PHY rates

| Rate | Quality | Streaming viability |
|------|---------|-------------------|
| 400+ mbps | Excellent | No issues |
| 200-400 mbps | Good | Fine for 4K streaming |
| 100-200 mbps | Fair | OK for 1080p, may struggle with 4K |
| 50-100 mbps | Poor | Marginal, expect stutters |
| < 50 mbps | Bad | Unusable for real-time streaming |

Note: PHY rates are **gross** rates. Effective TCP throughput is typically 50-70%
of the reported PHY rate due to protocol overhead.

### Asymmetric rates

Large TX/RX asymmetry (e.g. 45 TX / 197 RX) indicates directional electrical
noise — something on the circuit near the low-TX side is interfering. Common
culprits:
- USB-C / GaN chargers
- LED dimmers
- Appliances with motors (vacuum, washing machine)
- UPS / surge protectors (never plug powerline adapters into power strips)

### Generating test traffic

`plcrate -t` actively pushes traffic to measure real throughput (instead of
reading cached averages):

```bash
# Generate traffic from local adapter to all peers, then show rates
sudo plcrate -i enp4s0 -tni enp4s0

# Generate traffic between ALL pairs (takes longer, scales factorially)
sudo plcrate -i enp4s0 -Tni enp4s0
```

## Network Roles

The CCO (Central Coordinator) manages TDMA scheduling for all stations.
Check which adapter is CCO via `plctool -m` (look for `ROLE = 0x02 (CCO)`).

Ideally the CCO should be the most central / best-connected adapter (typically
the Hallway adapter). The CCO can be changed via PIB modification (`modpib -C`)
but Fritz firmware may override it. See `man modpib` for details.

## Recovery Procedures

### Power cycle (mild)

Power cycle the dropped adapter. If it rejoins automatically, the issue was a
firmware hang.

### Re-pair via Fritz tool (moderate)

If power cycling doesn't help, the adapter lost its network membership key.
Use the Fritz!Powerline Windows tool or FritzBox UI to re-add the adapter by
entering its device password (printed on the device label).

### Factory reset (nuclear)

Press and hold the adapter's reset button for 15+ seconds. Then re-pair all
adapters from scratch. Use this if repeated re-pairing fails.

## Known Issues (as of 2026-05)

- The Hallway adapter has dropped its network membership key twice in one week
  (2026-05). Root cause unknown — possibly firmware bug or NVRAM degradation.
  Monitor for recurrence; if persistent, replace the unit.
- Office <-> Living Room link is weak and asymmetric. Measured 2026-05-04:
  - From Living Room: TX 045 / RX 197 mbps to Office
  - From Office: TX 210 / RX 018 mbps to Living Room
  - Investigate electrical noise sources near both adapters.
- Office <-> Hallway link is healthy (258/235 mbps from Office's perspective).
