#!/usr/bin/env python3
"""
cisco_config_generator.py
-------------------------
Reads a CSV file with network parameters and generates a text file of
Cisco IOS router CLI commands that can be pasted directly into a terminal.

Expected CSV header:
    network,interface,description,vlan,ipaddress,subnetmask,defaultgateway

Supported scenarios:
    1. DHCP on WAN, basic LAN
    2. Static WAN with default gateway, basic LAN
    3. Static WAN, LAN with additional static routes (empty interface column)
    4. Router-on-a-stick (VLAN subinterfaces with 802.1Q encapsulation)

Usage:
    python cisco_config_generator.py samples/scenario1.csv
    python cisco_config_generator.py samples/scenario4.csv -o router4.txt
"""

import argparse
import csv
import ipaddress
import sys
from pathlib import Path

EXPECTED_HEADER = [
    "network", "interface", "description",
    "vlan", "ipaddress", "subnetmask", "defaultgateway",
]


class RowError(ValueError):
    """Raised when a single CSV row contains invalid data."""


# --------------------------------------------------------------------------- #
# CSV reading
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> list[dict]:
    """Read the CSV, validate the header and return cleaned rows.

    Every value is stripped of whitespace; 'network' and 'ipaddress' are
    lower-cased so that 'WAN' / 'DHCP' are handled the same as 'wan' / 'dhcp'.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:  # utf-8-sig strips Excel BOM
        reader = csv.DictReader(fh)

        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [col for col in EXPECTED_HEADER if col not in header]
        if missing:
            raise SystemExit(f"Error: CSV is missing column(s): {', '.join(missing)}")

        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):        # skip completely blank lines
                continue
            row["network"] = row["network"].lower()
            row["ipaddress"] = row["ipaddress"].lower()
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def parse_vlan(value: str) -> int:
    """Return the VLAN ID as int (empty -> 0). Valid range is 0-4094."""
    if value == "":
        return 0
    try:
        vlan = int(value)
    except ValueError:
        raise RowError(f"VLAN '{value}' is not a number")
    if not 0 <= vlan <= 4094:
        raise RowError(f"VLAN {vlan} is outside the range 0-4094")
    return vlan


def check_ip(address: str, mask: str) -> None:
    """Raise RowError if the address / subnet mask pair is not valid IPv4."""
    try:
        ipaddress.IPv4Address(address)
        ipaddress.IPv4Network(f"0.0.0.0/{mask}")   # validates the mask itself
    except ValueError as exc:
        raise RowError(f"invalid IP/mask '{address} {mask}' ({exc})")


def check_gateway(gateway: str) -> None:
    try:
        ipaddress.IPv4Address(gateway)
    except ValueError:
        raise RowError(f"invalid gateway '{gateway}'")


# --------------------------------------------------------------------------- #
# Command generation
# --------------------------------------------------------------------------- #
def physical_interface_block(name: str) -> list[str]:
    """Bare physical interface that only needs to be brought up
    (used as the parent of VLAN subinterfaces)."""
    return [f"interface {name}", " no shutdown", " exit"]


def interface_block(name: str, description: str, ip: str, mask: str,
                    vlan: int = 0) -> list[str]:
    """Build the CLI block for a physical interface or a dot1Q subinterface."""
    cmds = [f"interface {name}"]
    if description:
        cmds.append(f" description {description}")
    if vlan > 0:
        cmds.append(f" encapsulation dot1Q {vlan}")

    if ip == "dhcp":
        cmds.append(" ip address dhcp")
    else:
        check_ip(ip, mask)
        cmds.append(f" ip address {ip} {mask}")

    if vlan == 0:                      # subinterfaces inherit state from parent
        cmds.append(" no shutdown")
    cmds.append(" exit")
    return cmds


def generate_commands(rows: list[dict]) -> list[str]:
    """Translate the CSV rows into an ordered list of IOS commands."""
    interface_cmds: list[str] = []     # 'interface ...' blocks
    route_cmds: list[str] = []         # 'ip route ...' lines (emitted last)
    parents_done: set[str] = set()     # physical ports already brought up

    for line_no, row in enumerate(rows, start=2):   # line 1 is the header
        try:
            network = row["network"]
            iface = row["interface"]
            vlan = parse_vlan(row["vlan"])
            ip, mask, gw = row["ipaddress"], row["subnetmask"], row["defaultgateway"]

            # ---- Static route: no interface given (Scenario 3) -------------
            if not iface:
                if not (ip and mask and gw):
                    raise RowError("static route needs ipaddress, subnetmask and defaultgateway")
                check_ip(ip, mask)
                check_gateway(gw)
                route_cmds.append(f"ip route {ip} {mask} {gw}")
                continue

            if not ip:
                raise RowError("interface row has no ipaddress")

            # ---- Subinterface (Scenario 4) ---------------------------------
            if vlan > 0:
                if iface not in parents_done:          # 'no shut' the parent once
                    interface_cmds += physical_interface_block(iface)
                    parents_done.add(iface)
                interface_cmds += interface_block(f"{iface}.{vlan}",
                                                  row["description"], ip, mask, vlan)
            # ---- Regular physical interface --------------------------------
            else:
                interface_cmds += interface_block(iface, row["description"], ip, mask)
                parents_done.add(iface)

            # ---- Default route (WAN interface with a gateway) --------------
            if gw:
                if network == "wan":
                    check_gateway(gw)
                    route_cmds.append(f"ip route 0.0.0.0 0.0.0.0 {gw}")
                else:
                    print(f"Warning (line {line_no}): gateway ignored on non-WAN "
                          f"interface '{iface}'", file=sys.stderr)

        except RowError as exc:
            print(f"Warning (line {line_no}): skipped row - {exc}", file=sys.stderr)

    # Final assembly: interfaces first, then routes, then save.
    commands = ["enable", "configure terminal", "!"]
    commands += interface_cmds
    if route_cmds:
        commands.append("!")
        commands += route_cmds
    commands += ["!", "end", "write memory"]
    return commands


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Cisco IOS CLI commands from a network CSV file.")
    parser.add_argument("csv_file", type=Path, help="input CSV file")
    parser.add_argument("-o", "--output", type=Path,
                        help="output .txt file (default: <csv name>_config.txt)")
    args = parser.parse_args()

    if not args.csv_file.is_file():
        sys.exit(f"Error: file not found: {args.csv_file}")

    output = args.output or args.csv_file.with_name(f"{args.csv_file.stem}_config.txt")

    commands = generate_commands(read_csv(args.csv_file))
    output.write_text("\n".join(commands) + "\n", encoding="utf-8")
    print(f"Wrote {len(commands)} lines to {output}")


if __name__ == "__main__":
    main()
