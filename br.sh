#!/bin/bash
#
# br.sh: Creates a permanent, transparent bridge for packet capture.
# Designed for an add-only workflow on Ubuntu/Netplan.
#
# Usage: sudo ./br.sh <bridge-name> <iface1> <iface2>
#
# "Transparent" means the two ends must behave as though the capture VM were
# not there at all. A plain Linux bridge is NOT transparent: the kernel eats
# every frame addressed to the IEEE reserved group 01:80:C2:00:00:0X, which
# is exactly LLDP, LACP and the spanning-tree BPDUs. This script fixes that
# (see /usr/local/sbin/br-transparent) and also disables STP, IPv6 and the
# offloads, so the bridge neither swallows traffic nor injects its own.
#
# Re-running it is safe: an existing bridge keeps its config untouched and
# only has the transparency settings re-applied.
#

# --- Configuration & Colors ---
set -o pipefail
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Jumbo-capable MTU, so the tap is never what drops a large frame.
BR_MTU="${BR_MTU:-9500}"

# --- Initial Checks ---
if [ "$EUID" -ne 0 ]; then
	echo -e "${RED}Error: This script must be run as root or with sudo.${NC}"
	exit 1
fi

if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
	echo -e "${RED}Usage: $0 <bridge-name> <iface1> <iface2>${NC}"
	echo "Example: $0 br0 eth1 eth2"
	exit 1
fi

BR=$1
IF1=$2
IF2=$3
CONFIG_FILE="/etc/netplan/90-bridge-${BR}.yaml"

# -----------------------------------------------------------------------------
# The transparency helper. Netplan cannot express group_fwd_mask or
# multicast_snooping, so a tiny oneshot unit re-applies them on every boot.
# -----------------------------------------------------------------------------
install_transparency_helper() {
	tee /usr/local/sbin/br-transparent > /dev/null <<'HELPER'
#!/bin/bash
# Make a capture bridge behave like a piece of wire.
# Usage: br-transparent <bridge>   (idempotent; safe to re-run)
BR="${1:?usage: br-transparent <bridge>}"
MTU="${BR_MTU:-9500}"

# At boot this can run before networkd has finished building the bridge.
for _ in $(seq 1 30); do
	[ -d "/sys/class/net/$BR/bridge" ] && break
	sleep 1
done
[ -d "/sys/class/net/$BR/bridge" ] || exit 0

# Forward the IEEE reserved 01:80:C2:00:00:0X group addresses instead of
# swallowing them. Bit N enables address 01:80:C2:00:00:0N, so LLDP is bit
# 0x0E and LACP is bit 0x02.
#
# The bridge-wide knob rejects bits 0-2 (STP / MAC pause / LACP) with EINVAL,
# but the per-port knob only rejects MAC pause. LACP can therefore only be
# enabled per port, which is why both masks are set here.
ip link set dev "$BR" type bridge group_fwd_mask 0xfff8 2>/dev/null

for path in /sys/class/net/"$BR"/brif/*; do
	[ -e "$path" ] || continue
	IF=$(basename "$path")
	ip link set dev "$IF" type bridge_slave group_fwd_mask 0xfffd 2>/dev/null
	# Offloads coalesce frames, which changes what gets forwarded and makes a
	# capture lie about the real packet boundaries.
	ethtool -K "$IF" gro off gso off tso off lro off 2>/dev/null
	# Only touch the MTU when it differs, so re-running never flaps a live link.
	if [ "$(cat "/sys/class/net/$IF/mtu" 2>/dev/null)" != "$MTU" ]; then
		ip link set dev "$IF" mtu "$MTU" 2>/dev/null
	fi
done

if [ "$(cat "/sys/class/net/$BR/mtu" 2>/dev/null)" != "$MTU" ]; then
	ip link set dev "$BR" mtu "$MTU" 2>/dev/null
fi

# Flood multicast rather than pruning it with IGMP snooping.
echo 0 > "/sys/class/net/$BR/bridge/multicast_snooping" 2>/dev/null

# A transparent tap must not speak. Without this the bridge keeps an IPv6
# link-local address and emits MLD reports and router solicitations into a
# link that should only carry the two devices' traffic.
sysctl -qw "net.ipv6.conf.$BR.disable_ipv6=1" 2>/dev/null
exit 0
HELPER
	chmod +x /usr/local/sbin/br-transparent

	tee /etc/systemd/system/br-transparent@.service > /dev/null <<'UNIT'
[Unit]
Description=Make capture bridge %i transparent (LLDP/LACP/BPDU pass-through)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/br-transparent %i

[Install]
WantedBy=multi-user.target
UNIT

	systemctl daemon-reload 2>/dev/null
	systemctl enable "br-transparent@${BR}.service" >/dev/null 2>&1
	BR_MTU="$BR_MTU" /usr/local/sbin/br-transparent "$BR"
}

report_bridge() {
	local members
	members=$(ls "/sys/class/net/$BR/brif" 2>/dev/null | tr '\n' ' ')
	echo "    members           : ${members}"
	echo "    group_fwd_mask    : $(cat "/sys/class/net/$BR/bridge/group_fwd_mask" 2>/dev/null) (bridge), $(cat "/sys/class/net/$IF1/brport/group_fwd_mask" 2>/dev/null) (ports)"
	echo "    stp / mcast snoop : $(cat "/sys/class/net/$BR/bridge/stp_state" 2>/dev/null) / $(cat "/sys/class/net/$BR/bridge/multicast_snooping" 2>/dev/null)"
	echo "    LLDP, LACP and BPDUs cross this bridge."
}

# -----------------------------------------------------------------------------
# Already configured? Leave the config alone, but make sure the runtime
# transparency settings are in place - they live in sysfs, not in netplan.
# -----------------------------------------------------------------------------
if [ -f "$CONFIG_FILE" ]; then
	MEMBERS=$(ls "/sys/class/net/$BR/brif" 2>/dev/null | sort | tr '\n' ' ')
	WANTED=$(printf '%s\n%s\n' "$IF1" "$IF2" | sort | tr '\n' ' ')
	if [ "$MEMBERS" != "$WANTED" ]; then
		echo -e "${RED}Error: bridge '$BR' already exists with different members.${NC}"
		echo "  has:    ${MEMBERS:-<none>}"
		echo "  wanted: ${WANTED}"
		echo -e "To make changes, remove: ${YELLOW}${CONFIG_FILE}${NC}"
		exit 1
	fi
	echo -e "${YELLOW}[*] Bridge '$BR' already configured; re-applying transparency only.${NC}"
	install_transparency_helper
	echo -e "${GREEN}[+] Bridge '$BR' is ready for packet capture. 🕵️‍♂️${NC}"
	report_bridge
	exit 0
fi

for IF in "$IF1" "$IF2"; do
	if [ ! -e "/sys/class/net/$IF" ]; then
		echo -e "${RED}Error: interface '$IF' does not exist on this host.${NC}"
		echo "Available: $(ls /sys/class/net | tr '\n' ' ')"
		exit 1
	fi
done

# --- Dependency & System Configuration ---
echo -e "${YELLOW}[*] Preparing system...${NC}"

# bridge-utils only provides the legacy 'brctl' tool - netplan/networkd build
# the bridge without it - so a pod with no route to the archive must not stop
# us from bridging.
if ! dpkg -s bridge-utils &>/dev/null; then
	echo "-> Installing bridge-utils (optional)..."
	if ! { apt-get update -qq && \
	       DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Lock::Timeout=30 install bridge-utils; }; then
		echo -e "${YELLOW}[!] Could not install bridge-utils; continuing without it.${NC}"
	fi
fi

# Keep iptables out of the bridge path. These keys only exist once
# br_netfilter is loaded; if the module is absent then nothing filters bridged
# frames anyway, which is exactly what we want.
SYSCTL_CONF="/etc/sysctl.d/99-bridge-nf.conf"
if [ ! -f "$SYSCTL_CONF" ]; then
	echo "-> Configuring kernel for bridge transparency..."
	tee "$SYSCTL_CONF" > /dev/null <<EOF
net.bridge.bridge-nf-call-iptables=0
net.bridge.bridge-nf-call-ip6tables=0
net.bridge.bridge-nf-call-arptables=0
EOF
fi
if [ -d /proc/sys/net/bridge ]; then
	sysctl -p "$SYSCTL_CONF" > /dev/null 2>&1
fi

# --- Create and Apply Configuration ---
echo -e "${YELLOW}[*] Creating Netplan config for bridge '$BR'...${NC}"
tee "$CONFIG_FILE" > /dev/null <<EOF
network:
  version: 2
  ethernets:
    $IF1:
      dhcp4: no
      dhcp6: no
      accept-ra: no
      link-local: []
    $IF2:
      dhcp4: no
      dhcp6: no
      accept-ra: no
      link-local: []
  bridges:
    $BR:
      interfaces: [$IF1, $IF2]
      dhcp4: no
      dhcp6: no
      accept-ra: no
      link-local: []
      parameters:
        stp: false
        forward-delay: 0
EOF
chmod 600 "$CONFIG_FILE"

echo -e "${YELLOW}[*] Applying Netplan configuration...${NC}"
if ! netplan apply; then
	echo -e "${RED}[!] Failed to apply Netplan config. Removing invalid file.${NC}"
	rm "$CONFIG_FILE"
	exit 1
fi

echo -e "${YELLOW}[*] Making bridge '$BR' transparent...${NC}"
install_transparency_helper

if [ ! -d "/sys/class/net/$BR/bridge" ]; then
	echo -e "${RED}[!] Bridge '$BR' was not created.${NC}"
	exit 1
fi

echo -e "${GREEN}\n[+] Success! Bridge '$BR' is ready for packet capture. 🕵️‍♂️${NC}"
report_bridge
