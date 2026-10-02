#!/usr/bin/env python3
"""vpn_gate_setup.py — Tìm và tải VPN Gate config cho server Việt Nam."""
import csv
import os
import sys
import urllib.request

VPN_LIST_URL = "https://www.vpngate.net/api/iphone/"
VPN_CONFIG_PATH = "/tmp/vpn_config.ovpn"
VPN_STATUS_PATH = "/tmp/vpn_status.txt"


def find_vietnam_vpn():
    try:
        urllib.request.urlretrieve(VPN_LIST_URL, "/tmp/vpn_list.csv")
    except Exception as e:
        print(f"Error downloading VPN list: {e}", file=sys.stderr)
        with open(VPN_STATUS_PATH, "w") as f:
            f.write("NOT_FOUND")
        return False

    try:
        with open("/tmp/vpn_list.csv", "r", errors="ignore") as f:
            lines = f.readlines()

        for line in lines[2:]:
            parts = line.strip().split(",")
            if len(parts) < 15:
                continue
            country = parts[6] if len(parts) > 6 else ""
            if "Viet" in country or "Viet Nam" in country:
                ovpn_url = parts[14] if len(parts) > 14 else ""
                if ovpn_url and ovpn_url.startswith("http"):
                    print(f"Found Vietnam VPN: {ovpn_url}")
                    try:
                        urllib.request.urlretrieve(ovpn_url, VPN_CONFIG_PATH)
                        with open(VPN_STATUS_PATH, "w") as f:
                            f.write("FOUND")
                        return True
                    except Exception as e:
                        print(f"Download failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error parsing VPN list: {e}", file=sys.stderr)

    with open(VPN_STATUS_PATH, "w") as f:
        f.write("NOT_FOUND")
    print("No Vietnam VPN found.")
    return False


if __name__ == "__main__":
    find_vietnam_vpn()
