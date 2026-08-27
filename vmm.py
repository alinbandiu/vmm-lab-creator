#!/usr/bin/env python3
import argparse
import difflib
import errno
import yaml
from jinja2 import Environment, FileSystemLoader
import subprocess
import time
import telnetlib
import sys
import re 
import os
import logging
import pexpect
import functools
import signal
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from jnpr.junos import Device
from jnpr.junos.utils.config import Config
from jnpr.junos.utils.start_shell import StartShell
from jnpr.junos.exception import ConnectError, ConfigLoadError, CommitError, RpcError
import platform
import threading

# Set up basic logging for better script output and debugging
logging.basicConfig(
    level=logging.CRITICAL + 1,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# -----------------------------
# Deployment constants
# -----------------------------
# Credentials applied to (and used to reach) every Junos device during
# baseline configuration. Override with environment variables so the same
# script works across different pods without editing the source.
DEVICE_ROOT_USER = os.environ.get("VMM_DEVICE_USER", "root")
DEVICE_ROOT_PASSWORD = os.environ.get("VMM_DEVICE_PASSWORD", "Embe1mpls")

# -----------------------------
# Serial console prompt patterns
# -----------------------------
# These MUST require the 'user@host' part. Junos boot output is full of text a
# bare '>' / '#' / '%' pattern happily matches, and a false match makes the
# login state machine think a still-booting device is at a prompt: it stops
# waiting, fires 'edit' into the boot log, and then hangs forever.
#
# Measured against a real 749-line boot log:
#     r">\s"   -> 40 matches, only 4 of them real prompts. The rest were the
#                 '-> ' in banner arrows and the newline after '</output>'.
#     r"#\s"   -> 45 matches, ALL of them '####...#' banner rules.
#     r"%"     ->  4 matches, none of them prompts.
# The patterns below match those same 4 real prompts and nothing else.
#
# The user part stays permissive and the host part may be empty, because a
# device that has not applied its hostname yet prompts as 'root@> '.
PROMPT_OPER = r"[\w.-]+@[\w.-]*>\s"        # Junos operational: user@host>
PROMPT_CONFIG = r"[\w.-]+@[\w.-]*#\s"      # Junos configuration: user@host#
PROMPT_SHELL_PCT = r"[\w.-]+@[\w.-]*[^\r\n]{0,20}%\s"   # FreeBSD shell: root@host:~ %
PROMPT_SHELL_ROOT = r"root@[^\r\n]*#\s"    # Linux/FreeBSD root shell: root@host:~ #
# A bare '#' shell (the vBugatti/MX304 RE drops you here) only counts at the
# start of a line, which is what keeps '####...' banners from matching.
PROMPT_SHELL_BARE = r"(?m)^\r?#\s"


# Filesystem locations of helper assets on the QPOD. These default to the
# original hardcoded paths but can be relocated via VMM_SCRIPTS_DIR (or the
# per-file overrides) to make the tool portable to another user account.
SCRIPTS_DIR = os.environ.get("VMM_SCRIPTS_DIR", "/homes/balinfilipga/scripts")
SNIFFER_BRIDGE_SCRIPT = os.environ.get(
    "VMM_SNIFFER_SCRIPT", os.path.join(SCRIPTS_DIR, "br.sh")
)

# -----------------------------
# Multi-FPC VMX interface catalog
# -----------------------------
# Fixed hardware profile for the multi-FPC 'vmx' VM type: FPC0 (built-in GE/XE),
# FPC1 & FPC2 (channelized 100G->4x25G, XE_CHAN), FPC3 (100G ports - these use the
# XE_CHAN macro internally with a fixed subport of 0, but are exposed to Junos as
# et-3/0/<port> rather than xe-3/0/<port>:0), and FPC5 (12x10GE, XE). FPC4 is not
# present in this profile. This layout is fixed for every 'vmx' VM.
FPC_I2CID_MACRO = {
    0: 'VMX_MPC_I2CID',
    1: 'VMX_EA_MPC_I2CID',
    2: 'VMX_EA_MPC_I2CID',
    3: 'VMX_EA_MPC_I2CID',
    5: 'VMX_XL_MPC_I2CID',
}

# Each vbugatti (VMX304) chassis needs a distinct VMX304_CHASSIS_I2CID. On the
# vmm3 pods valfaromeo (MX10008) takes 21, so vbugatti starts at 22 to coexist.
VBUGATTI_CHASSIS_I2CID_BASE = 22

# --- vmm3 EVO/cosim emission order (LOAD-BEARING) ---------------------------
# common.evovptx.iface.et.defs installs ~250 unprefixed IF_ET_X_* aliases.
# RAW connectors (EVOVPTX_CONNECT, LC304_CONNECT) need the alias ALIVE; PREFIXED
# connectors (VBALERION_/VALFAROMEO_/VBOWMORE_CONNECT) token-paste and
# need it DEAD. So every RAW chassis is emitted before any PREFIXED one, each
# PREFIXED block #undef-ing only the aliases it uses, and vbowmore last (it
# replaces the shared EVOVPTX_* macros via a late include). Non-EVO types are
# rank 0 (order-independent). See the ninenode.cfg reference header comment.
VMM3_EMIT_RANK = {
    'vscapa': 1, 'vardbeg': 1, 'vbrackla': 1, 'vbugatti': 1,   # RAW / self-contained
    'vbalerion': 2, 'valfaromeo': 2,                           # PREFIXED
    'vhamilton': 2, 'vmaserati': 2,                            # PREFIXED (vMX10004)
    'vbowmore': 3,                                             # PREFIXED, must be LAST
}

def _build_vmx_interface_catalog():
    catalog = {}
    # FPC0: 20x GE (pic 0-1) + 4x XE (pic 2-3)
    for pic in (0, 1):
        for port in range(10):
            catalog[f"ge-0/{pic}/{port}"] = {
                'fpc': 0, 'pic': pic, 'port': port, 'subport': None, 'macro': 'GE'
            }
    for pic in (2, 3):
        for port in range(2):
            catalog[f"xe-0/{pic}/{port}"] = {
                'fpc': 0, 'pic': pic, 'port': port, 'subport': None, 'macro': 'XE'
            }
    # FPC1 & FPC2: 6 ports x 4 channelized subports each
    for fpc in (1, 2):
        for port in range(6):
            for sub in range(4):
                catalog[f"xe-{fpc}/0/{port}:{sub}"] = {
                    'fpc': fpc, 'pic': 0, 'port': port, 'subport': sub, 'macro': 'XE_CHAN'
                }
    # FPC3: 6x 100G ports, XE_CHAN macro with fixed subport 0, exposed as et-
    for port in range(6):
        catalog[f"et-3/0/{port}"] = {
            'fpc': 3, 'pic': 0, 'port': port, 'subport': 0, 'macro': 'XE_CHAN'
        }
    # FPC5: 12x XE
    for port in range(12):
        catalog[f"xe-5/0/{port}"] = {
            'fpc': 5, 'pic': 0, 'port': port, 'subport': None, 'macro': 'XE'
        }
    return catalog

VMX_INTERFACE_CATALOG = _build_vmx_interface_catalog()

def _vmx_catalog_hint():
    """Human-readable summary of the valid multi-FPC vmx interface catalog,
    generated from VMX_INTERFACE_CATALOG so it can't drift out of sync with
    the code. Used in validation error messages."""
    return (
        "Valid vmx interfaces: ge-0/0/0-9, ge-0/1/0-9, xe-0/2/0-1, xe-0/3/0-1 (FPC0); "
        "xe-1/0/0-5:0-3 (FPC1, channelized); xe-2/0/0-5:0-3 (FPC2, channelized); "
        "et-3/0/0-5 (FPC3); xe-5/0/0-11 (FPC5). FPC4 is not available."
    )

# -----------------------------
# Mutually exclusive VM types
# -----------------------------
# Each entry is (types_a, types_b, reason): no member of types_a may appear in
# the same topology as any member of types_b, because their VMM macro headers
# collide. Add a new entry here when a new profile brings its own conflicting
# .defs file - the check in validate_topology() picks it up automatically.
# The vmm3 EVO / cosim family. Every one of these is built on the vmm3 headers
# (common.evovptx.defs and friends) and they are proven to coexist in a single
# file - see the 9-platform ninenode reference. Coexistence relies on two
# things the template does automatically: emitting RAW-connect chassis before
# PREFIXED-connect ones (VMM3_EMIT_RANK) and #undef-ing only the unprefixed
# IF_ET_X_* aliases each PREFIXED block actually consumes.
VMM3_EVO_TYPES = {
    'vscapa', 'vardbeg', 'vbrackla', 'vbalerion',
    'vbowmore', 'valfaromeo',
}

# Types lab_template.j2 actually knows how to emit. Kept in sync with the
# {% elif vm.type == ... %} chain in the template - a type missing here (or
# there) is silently dropped from the generated config, so validate_topology()
# rejects anything not in this set.
SUPPORTED_VM_TYPES = {
    'server', 'vswitch', 'vrouter', 'vqfx',
    'vmx', 'vferrari', 'vbugatti', 'vhamilton', 'vmaserati',
    'vscapa', 'vardbeg', 'vbrackla', 'vbalerion',
    'vbowmore', 'valfaromeo',
}

# Types that used to be supported but no longer have a template block.
RETIRED_VM_TYPES = {
    'vptx': (
        "the old pre-vmm3 vPTX profile was replaced by the vmm3 EVO PTX types. "
        "Use 'vardbeg', 'vbowmore', 'vscapa', 'vbrackla' or 'vbalerion' instead "
        "(they interoperate in a single lab; vptx did not)"
    ),
    'vredbull': (
        "removed - the profile did not come up reliably and is no longer "
        "generated. Use another channelized type such as 'valfaromeo', or one "
        "of the vMX10004 linecards ('vhamilton', 'vmaserati')"
    ),
}

# MX10K profiles whose FPCs are DISKLESS. They own no disk of their own: on
# every boot they PXE the linecard image
# 'junos-evo-install-ulc-mx-x86-64-<release>-EVO' out of the RE's own Junos
# package, so the linecard always runs whatever release the RE image is - the
# topology file never names it, which is exactly why a bad one is so hard to
# spot.
ULC_LINECARD_TYPES = {'vhamilton', 'vmaserati', 'valfaromeo'}

# Oldest release observed to survive on the pod. Below this the FPC boots all
# the way up - systemd starts platformd and aft-trio-app, chassisd on the RE
# marks it Online and creates lc-/pfe-/pfh- ifds - and then the RE<->FPC IPC
# drops a few minutes later and the card reboots, forever. What you see is:
#     Slot State                    ...
#      0    Offline ---Chassis connection dropped---
# while 'vmm ls' still shows the FPC VM Running, which looks like anything but
# a software version problem. Confirmed on q-pod26 with 22.4R3-S2.11 (four
# reboot cycles in one console log, ~5 minutes apart, alongside chassisd
# logging 'i2cs_virtual_wait_mpcsd_resp: recv from socket failed: Operation
# timed out'); every ULC boot in the pod's console logs that did NOT loop was
# 23.4 or newer.
ULC_MIN_JUNOS = (23, 4)


def junos_release_from_image(path):
    """
    Pull the (major, minor) release out of a Junos image filename, e.g.
    '.../junos-virtual-x86-64-22.4R3-S2.11.vmdk' -> (22, 4).

    Returns None when the filename carries no release - the pod's blessed
    images are symlinks like 'default_image_vhamilton.img' - so the caller
    skips the check instead of guessing a version it cannot see.
    """
    m = re.search(r'-(\d{2})\.(\d)(?=[R.\-])', os.path.basename(str(path)))
    return (int(m.group(1)), int(m.group(2))) if m else None

# Pairs of type groups that cannot share one generated config.
#
# EMPTY ON PURPOSE - every supported type now mixes freely.
#
# This used to hold {'vhamilton'} x VMM3_EVO_TYPES. vHamilton (and vMaserati)
# build their vNIC name by token-pasting their own prefix onto the IF_ET
# argument, e.g. VHAMILTON_CONNECT(IF_ET(0,0,0), b) -> CATENATE(VHAMILTON_, <arg>).
# cpp fully expands the argument first, so while the vmm3 EVO headers' ~250
# unprefixed IF_ET_X_* aliases are live the argument becomes 'eth4' and the paste
# yields the bogus name 'VHAMILTON_eth4'.
#
# That is now handled structurally instead of by exclusion, and verified on the
# pod with 'cpp -P':
#   * VMM3_EMIT_RANK puts every RAW-connect chassis (rank 1, needs the aliases
#     ALIVE) ahead of every PREFIXED one (rank 2/3, needs them DEAD), and
#   * each PREFIXED block '#undef's exactly the IF_ET_X_<pic>_<port> aliases it
#     is about to consume, immediately before its own *_CONNECT lines.
# Measured result in a single config: EVO -> 'eth13', vHamilton -> 'eth4'/'eth5'.
# _assert_prefixed_aliases_are_undefed() re-checks this on every generate, so a
# regression fails at generation time instead of after a deploy.
INCOMPATIBLE_TYPE_GROUPS = []

# Legal device hostnames.
# ----------------------
# A hostname is not cosmetic - it is reused verbatim in three places that each
# constrain it, so an unconstrained name fails *after* a 20-minute deploy:
#
#   1. 'set system host-name <name>' is pushed to the device over the serial
#      console. Whitespace makes Junos read the tail as extra arguments, the
#      commit fails, and the device is left unconfigured.
#   2. 'vmm ping' output is parsed by whitespace column (get_vmm_ip_map /
#      get_vmm_ping_map). A name containing a space shifts every column, so the
#      wrong IP - or no IP - is read back, silently.
#   3. The template token-pastes it into VMM macro arguments, e.g.
#      VMX_RE_INSTANCE({{ vm.hostname }}_RE, ...).
#
# 'vmm config' itself accepts all of these (measured on q-pod32: dash, dot,
# leading digit and even a space all pass), so VMM will NOT catch it for us.
# Letters/digits/'_'/'-' starting with a letter covers every realistic lab name
# (R1, PE1, CE-2, core_rtr) while keeping all three consumers safe.
HOSTNAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]*')

# WHERE THESE RANGES COME FROM (do not widen them from the alias tables!)
# ---------------------------------------------------------------------
# For the vmm3 EVO platforms the IF_ET_X_0_<port> alias tables are a
# SUPERSET of the ports Junos actually exposes. The real port list is set
# by the platform's CSPP/cosim config, which maps host vNICs to PFE ports:
#     /vmm/data/vmm-configs/common/vptxc/cspp_cfg/<PLATFORM>/*_cspp.conf*
# Its "Interfaces mapping" section lists eth1 (the LCPU host port, not a
# data port) followed by the data vNICs. The generator instantiates ONE
# CSPP/PFE per chassis, so the valid set comes from conf.0, and:
#
#     Junos et-<fpc>/0/<N>   <->   IF_ET_X_<pic>_<N>   <->   eth<N+4>
#
# Verified against live devices: vScapa/vBowmore eth5,7,..,19 -> odd 1-15;
# vBalerion eth13..30 -> 9-26; vArdbeg eth4..15 -> 0-11; vBrackla eth4..8
# -> 0-4. Wiring a port outside this set is NOT a config error - VMM
# happily builds the bridge and the vNIC, but no Junos interface is ever
# bound to it, so the link is silently dead (peer shows link-up, no
# traffic, no LLDP neighbour). That bug cost a full deployment cycle.
#
# Module-level so the web builder (--build) can reuse the *same* rules the
# CLI validator uses. Never duplicate these in JavaScript.
INTERFACE_PATTERNS = {
    'vrouter': re.compile(r'^ge-0/0/\d+$'),
    'vswitch': re.compile(r'^ge-0/0/\d+$'),
    'vqfx': re.compile(r'^xe-0/0/\d+$'),
    'server': re.compile(r'^em\d+$'),
    # vScapa (vmm3 EVOvScapa): RAW EVOVPTX_CONNECT(IF_ET(0,0,<port>)).
    # Junos exposes only the 8 ODD ports et-0/0/1,3,5,7,9,11,13,15
    # (verified on a live vScapa). The shared IF_ET_X_0_<port> alias table
    # runs 0-35, but any port outside that odd set is never bound inside
    # Junos: VMM still builds the bridge and the vNIC, so the peer sees
    # link-up while no traffic passes -> silently dead link.
    'vscapa': re.compile(r'^et-0/0/(1|3|5|7|9|11|13|15)$'),
    # vBrackla (vmm3): FPC1, RAW EVOVPTX_CONNECT(IF_ET(1,0,<port>)) -> et-1/0/<port>
    # (no channel suffix on vmm3; the reference wires et-1/0/0). Only 5 data
    # ports: its CSPP config (EVOvBRACKLA/COSIMPP/vbrackla_cspp.conf, named
    # by BRACKLACHAN in common.brackla.defs) maps eth4..eth8 -> et-1/0/0..4.
    'vbrackla': re.compile(r'^et-1/0/[0-4]$'),
    # vFerrari: 5 fixed 100G ports on FPC0, not channelized. Emitted as
    # VMX_CONNECT(ET(fpc,pic,port,0), ...) - same macro family as vmx.
    'vferrari': re.compile(r'^et-0/0/[0-4]$'),
    # vBugatti (vMX304 + LC304): 16 x 100G ports on FPC0, numbered 0-15.
    # Verified on a live vBugatti, and the LC304_IF_ET_100G_X_0_<port>
    # alias table is likewise 0-15. Emitted as
    # LC304_CONNECT(LC304_IF_ET_100G(0,0,<port>), ...).
    'vbugatti': re.compile(r'^et-0/0/([0-9]|1[0-5])$'),
    # vHamilton (vMX10004 + vHamilton LCs): 14 ports per linecard, numbered
    # from 0 (et-<fpc>/0/0 .. et-<fpc>/0/13), on up to 3 linecards FPC0-FPC2.
    # Emitted as VHAMILTON_CONNECT(IF_ET(<fpc>,0,<port>), ...), one
    # VHAMILTON_FPC block per FPC used (same shape as vAlfaRomeo).
    'vhamilton': re.compile(r'^et-[0-2]/0/([0-9]|1[0-3])$'),
    # vMaserati (vMX10004 + vMaserati LC, "XT"): TWO pics on one linecard -
    # pic0 has 20 ports (et-<fpc>/0/0 .. et-<fpc>/0/19) and pic1 has 16
    # (et-<fpc>/1/0 .. et-<fpc>/1/15). Unlike the EVO/CSPP platforms the full
    # alias table really is wired: the shipped reference topology
    # /vmm/data/user_disks/vmaserati/vmaserati-1router-mx10k4.config connects
    # all 36 of them, so this range is the vendor's own list, not a guess.
    # Alias mapping is VMASERATI_IF_ET_X_0_N -> vio<N+4> and
    # VMASERATI_IF_ET_X_1_N -> vio<N+24> (vio, not eth).
    # Emitted as VMASERATI_CONNECT(IF_ET(<fpc>,<pic>,<port>), ...) with the
    # matching '#undef IF_ET_X_<pic>_<port>' - same PREFIXED handling as
    # vHamilton/vAlfaRomeo.
    'vmaserati': re.compile(r'^et-[0-2]/(0/([0-9]|1[0-9])|1/([0-9]|1[0-5]))$'),
    # vBalerion (vmm3 EVO PTX): FPC0 linecard, ports et-0/0/9 .. et-0/0/26.
    # Emitted as VBALERION_CONNECT(IF_ET(0,0,<port>), ...); the template also
    # emits '#undef IF_ET_X_0_<port>' per port so the prefix-paste doesn't
    # expand the unprefixed alias into junk (VBALERION_eth13).
    'vbalerion': re.compile(r'^et-0/0/(9|1[0-9]|2[0-6])$'),
    # vAlfaRomeo: 4 ports x 4 channelized subports per FPC, on FPC0, FPC1
    # and FPC2 (up to 3 linecards). Emitted as
    # VALFAROMEO_CONNECT(IF_ET_CHAN(fpc,pic,port,subport)), one
    # VALFAROMEO_FPC block per FPC used.
    'valfaromeo': re.compile(r'^et-[0-2]/0/[0-3]:[0-3]$'),
    # vArdbeg (vmm3 EVO PTX): RAW connect - EVOVPTX_CONNECT(IF_ET(0,0,<port>)).
    # The shared unprefixed IF_ET_X_0_<port> table runs to 35, but Junos
    # only exposes et-0/0/0 .. et-0/0/11 (12 contiguous ports, verified on
    # a live vArdbeg). Ports 12-35 exist as aliases only -> dead links.
    'vardbeg': re.compile(r'^et-0/0/([0-9]|1[01])$'),
    # vBowmore (vmm3 EVO PTX): PREFIXED connect - VBOWMORE_CONNECT pastes
    # onto IF_ET_X_0_<port>. Same port layout as vScapa: Junos exposes only
    # the 8 ODD ports et-0/0/1,3,...,15 (verified on a live vBowmore; its
    # alias table is byte-identical to vScapa's). An even port yields a
    # silently dead link - this is exactly what broke a real deployment.
    'vbowmore': re.compile(r'^et-0/0/(1|3|5|7|9|11|13|15)$'),
}


# -----------------------------
# Enumerable port catalog (drives the web builder's port dropdowns)
# -----------------------------
# INTERFACE_PATTERNS above can *validate* an interface name but cannot *list*
# the legal ones, and the GUI needs a list. These two must never disagree: an
# entry offered here but rejected by the validator would be a trap, which is
# precisely the class of bug this project keeps getting bitten by. The
# assertion at the bottom of this block enforces catalog subset-of-regex on
# every import, so a drift is a startup crash, not a dead link found after a
# 20-minute deploy.
#
# For the growable ge-/xe-/em ranges the regex is open-ended; we expose a
# practical slice for the dropdown rather than an infinite list.
def _build_port_catalog():
    cat = {}

    cat['server']   = [f"em{n}" for n in range(1, 9)]
    cat['vswitch']  = [f"ge-0/0/{n}" for n in range(16)]
    cat['vrouter']  = [f"ge-0/0/{n}" for n in range(16)]
    cat['vqfx']     = [f"xe-0/0/{n}" for n in range(12)]

    # Fixed hardware catalogs - these are the verified real port lists.
    cat['vmx']      = list(VMX_INTERFACE_CATALOG.keys())
    cat['vferrari'] = [f"et-0/0/{n}" for n in range(5)]
    cat['vbugatti'] = [f"et-0/0/{n}" for n in range(16)]
    cat['vardbeg']  = [f"et-0/0/{n}" for n in range(12)]
    cat['vbrackla'] = [f"et-1/0/{n}" for n in range(5)]          # FPC1, not FPC0
    cat['vbalerion'] = [f"et-0/0/{n}" for n in range(9, 27)]     # starts at 9
    cat['vscapa']   = [f"et-0/0/{n}" for n in range(1, 16, 2)]   # ODD only
    cat['vbowmore'] = [f"et-0/0/{n}" for n in range(1, 16, 2)]   # ODD only

    # Multi-FPC linecards: FPC0-FPC2.
    cat['vhamilton'] = [f"et-{f}/0/{n}" for f in range(3) for n in range(14)]
    cat['vmaserati'] = ([f"et-{f}/0/{n}" for f in range(3) for n in range(20)] +
                        [f"et-{f}/1/{n}" for f in range(3) for n in range(16)])
    cat['valfaromeo'] = [f"et-{f}/0/{p}:{s}"
                         for f in range(3) for p in range(4) for s in range(4)]
    return cat


PORT_CATALOG = _build_port_catalog()

# Fail fast if the catalog and the validator ever diverge.
for _t, _ports in PORT_CATALOG.items():
    if _t == 'vmx':
        continue  # validated against VMX_INTERFACE_CATALOG membership, not a regex
    _rx = INTERFACE_PATTERNS.get(_t)
    if _rx is None:
        raise RuntimeError(f"PORT_CATALOG has type '{_t}' with no INTERFACE_PATTERNS entry")
    _bad = [p for p in _ports if not _rx.match(p)]
    if _bad:
        raise RuntimeError(
            f"PORT_CATALOG/INTERFACE_PATTERNS drift for '{_t}': "
            f"{_bad[:5]} would be offered by the GUI but rejected by the validator"
        )
if set(PORT_CATALOG) != SUPPORTED_VM_TYPES:
    raise RuntimeError(
        f"PORT_CATALOG does not cover every supported type: "
        f"missing={sorted(SUPPORTED_VM_TYPES - set(PORT_CATALOG))} "
        f"extra={sorted(set(PORT_CATALOG) - SUPPORTED_VM_TYPES)}"
    )



# -----------------------------
# Topology Validation Function
# -----------------------------

def collect_topology_errors(data):
    """
    Semantic validation of a parsed topology dict. Returns a sorted list of
    human-readable error strings (empty list == valid).

    This is the single source of truth for topology rules. It is pure: it
    neither prints nor exits, so the web builder (--build) can call it per
    keystroke and render the same errors the CLI reports. validate_topology()
    below is the CLI wrapper that prints and aborts.
    """
    errors = []
    # Use a dictionary comprehension for quick VM lookup
    vms_by_hostname = {vm['hostname']: vm for vm in data.get('vms', [])}

    # 0. Lab name.
    # The template emits TOPOLOGY_START({{ lab_name }}), which becomes
    # 'config "<name>" {'. With the key missing or blank that expands to
    # 'config "" {' and VMM rejects the whole file with an unhelpful
    # "syntax error, unexpected T_number, expecting '{'" pointing at an
    # unrelated line, so catch it here where the cause is obvious.
    lab_name = str(data.get('lab_name') or "").strip()
    if not lab_name:
        errors.append(
            "Missing top-level 'lab_name'. Add e.g. \"lab_name: MY-LAB\" - "
            "it names the VMM config, and without it the generated file is rejected."
        )
    elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", lab_name):
        errors.append(
            f"lab_name '{lab_name}' is not a valid VMM config name. Use letters, "
            f"digits, '-' or '_', starting with a letter."
        )

    # 0. Mutually Exclusive VM Type Check
    # Several profiles ship VMM macro headers that #define the same macro
    # names with conflicting values. Including two of them in one generated
    # config makes 'vmm config' fail its preprocessing step (or, worse, expand
    # to something invalid), so catch the combination here instead of letting
    # it fail later on the VMM host.
    present_types = {vm.get('type') for vm in data.get('vms', [])}

    # 0a. Unknown / retired VM type check.
    # lab_template.j2 dispatches on vm.type through a chain of {% elif %} with no
    # {% else %}, so a type it does not know emits *nothing at all* - the VM block
    # is silently dropped while its links still reference it, producing a config
    # that looks fine but is wired to a device that does not exist. Fail loudly.
    for vm in data.get('vms', []):
        vm_type = vm.get('type')
        if vm_type in RETIRED_VM_TYPES:
            errors.append(
                f"VM '{vm.get('hostname')}' uses retired type '{vm_type}': "
                f"{RETIRED_VM_TYPES[vm_type]}"
            )
        elif vm_type not in SUPPORTED_VM_TYPES:
            errors.append(
                f"VM '{vm.get('hostname')}' has unknown type '{vm_type}'. "
                f"Supported types: {', '.join(sorted(SUPPORTED_VM_TYPES))}."
            )

    # 0a2. Linecard software version for the DISKLESS MX10K profiles.
    # See ULC_LINECARD_TYPES / ULC_MIN_JUNOS above: the FPCs inherit their
    # software from the RE image, and too old a release boot-loops them. The
    # config generates and deploys perfectly happily, so this has to be caught
    # here or it costs a full deploy plus a console-log dig to find.
    disks_map = data.get('disks', {}) or {}
    for vm in data.get('vms', []):
        if vm.get('type') not in ULC_LINECARD_TYPES:
            continue
        image = disks_map.get(vm.get('disk'), vm.get('disk'))
        release = junos_release_from_image(image)
        if release and release < ULC_MIN_JUNOS:
            errors.append(
                f"VM '{vm.get('hostname')}' (type '{vm.get('type')}') uses Junos "
                f"{release[0]}.{release[1]} image '{image}'. Its FPCs are diskless and "
                f"PXE-boot the linecard image out of that package, and releases older "
                f"than {ULC_MIN_JUNOS[0]}.{ULC_MIN_JUNOS[1]} boot-loop: the FPC reaches "
                f"Online, then falls to 'Offline ---Chassis connection dropped---' and "
                f"reboots every few minutes. Point this VM's disk at a newer image - the "
                f"pod's blessed one for this profile is "
                f"/vmm/data/base_disks/default_images/default_image_{vm.get('type')}.img"
            )

    # 0b. Hostname checks.
    # Duplicates were previously invisible: vms_by_hostname above is a dict, so a
    # repeated hostname silently collapses to one entry and every link endpoint
    # resolves to whichever VM happened to be last. The config still generates
    # and still deploys - with a device missing. Now that names are typed by hand
    # in the builder rather than auto-numbered, this is easy to hit.
    seen_hosts = set()
    for vm in data.get('vms', []):
        host = vm.get('hostname')
        if host is None or str(host).strip() == '':
            errors.append(
                f"A VM of type '{vm.get('type')}' has no hostname. Every VM needs a "
                f"unique 'hostname'."
            )
            continue
        host = str(host)
        if host in seen_hosts:
            errors.append(
                f"Duplicate hostname '{host}'. Every VM needs a unique hostname - "
                f"link endpoints are matched by name, so a repeat silently drops a device."
            )
        seen_hosts.add(host)
        if not HOSTNAME_RE.fullmatch(host):
            errors.append(
                f"Hostname '{host}' is not usable. Use letters, digits, '-' or '_', "
                f"starting with a letter (e.g. R1, PE1, CE-2, core_rtr). The name is "
                f"pushed as 'set system host-name' and is parsed back out of "
                f"'vmm ping' by column, so spaces and punctuation break the deploy."
            )

    for group_a, group_b, reason in INCOMPATIBLE_TYPE_GROUPS:
        hit_a = sorted(group_a & present_types)
        hit_b = sorted(group_b & present_types)
        if hit_a and hit_b:
            errors.append(
                f"Topology mixes {hit_a} with {hit_b}. These VM types cannot be "
                f"used in the same lab: {reason}. Put them in separate topology "
                f"files instead."
            )

    # 1. Disk Naming Convention Check
    for vm in data.get('vms', []):
        vm_type = vm.get('type')
        disk_alias = vm.get('disk')
        if vm_type in ('vmx', 'vqfx', 'vferrari', 'valfaromeo', 'vbugatti',
                       'vhamilton', 'vmaserati', 'vbalerion', 'vscapa',
                       'vbrackla', 'vardbeg', 'vbowmore'):
            # Use a single check for all supported types. The template keys off
            # this prefix to decide how the '#define' is emitted, so it is not
            # merely cosmetic.
            if not disk_alias.startswith(vm_type):
                errors.append(f"VM '{vm['hostname']}' (type: {vm_type}) uses disk '{disk_alias}'. Disk alias must start with '{vm_type}'.")

    # Prepare for interface checks
    interface_patterns = INTERFACE_PATTERNS
    vm_interfaces = defaultdict(list)

    # 2. Interface Naming Convention Check
    for link in data.get('links', []):
        endpoints = link.get('endpoints', [])
        for endpoint in endpoints:
            try:
                hostname, iface_name = endpoint.split(':', 1)
                if hostname in vms_by_hostname:
                    vm_type = vms_by_hostname[hostname].get('type')
                    if vm_type == 'vmx':
                        # Multi-FPC vmx: interface must be part of the fixed
                        # FPC0/1/2/3/5 catalog (sparse usage is expected).
                        if iface_name not in VMX_INTERFACE_CATALOG:
                            errors.append(f"Invalid interface '{iface_name}' for VMX '{hostname}'. {_vmx_catalog_hint()}")
                    elif vm_type in interface_patterns:
                        if not interface_patterns[vm_type].match(iface_name):
                            errors.append(f"Invalid interface format '{iface_name}' for VM '{hostname}' (type: {vm_type}). Expected format like: '{interface_patterns[vm_type].pattern}'.")
                    vm_interfaces[hostname].append(iface_name)
                else:
                    errors.append(f"Hostname '{hostname}' used in a link endpoint ('{endpoint}') is not defined in the 'vms' section.")
            except ValueError:
                errors.append(f"Malformed endpoint '{endpoint}' in links section. Expected format 'hostname:interface'.")

    # 3. Interface Ordering and Duplication Check
    for hostname, ifaces in vm_interfaces.items():
        vm_type = vms_by_hostname[hostname].get('type')
        if not ifaces:
            continue

        # Check for duplicate interface usage on the same VM
        if len(ifaces) != len(set(ifaces)):
            errors.append(f"Duplicate interface assignment found on VM '{hostname}'. Each interface can only be used once.")
            continue # Skip sequential check if duplicates found

        # These types draw from a fixed hardware interface catalog rather than
        # a growable range, so the sequential-numbering rule below does not
        # apply - any subset of the valid interfaces may be used, in any order.
        # (Gaps are proven safe: the pod-validated vBalerion reference wires
        # et-0/0/9 and et-0/0/18 with nothing in between.) The exact valid set
        # per type is enforced by interface_patterns above; the ranges below
        # were verified against live devices, not inferred from alias tables.
        #   vmx        - see VMX_INTERFACE_CATALOG (sparse, multi-FPC)
        #   vferrari   - et-0/0/0 .. et-0/0/4
        #   valfaromeo - et-<0-2>/0/0:0 .. et-<0-2>/0/3:3  (FPC0-FPC2)
        #   vbugatti   - et-0/0/0 .. et-0/0/15   (16 ports, 0-based)
        #   vhamilton  - et-<0-2>/0/0 .. et-<0-2>/0/13  (FPC0-FPC2)
        #   vbalerion  - et-0/0/9 .. et-0/0/26   (18 ports, starts at 9)
        #   vardbeg    - et-0/0/0 .. et-0/0/11   (12 ports)
        #   vbowmore   - et-0/0/1,3,5,7,9,11,13,15   (8 ports, ODD only)
        #   vscapa     - et-0/0/1,3,5,7,9,11,13,15   (8 ports, ODD only)
        #   vbrackla   - et-1/0/0 .. et-1/0/4    (5 ports, FPC1)
        if vm_type in ('vmx', 'vferrari', 'valfaromeo', 'vbugatti', 'vhamilton',
                       'vmaserati', 'vbalerion', 'vardbeg', 'vbowmore',
                       'vscapa', 'vbrackla'):
            continue

        numbers = []
        try:
            if vm_type in ['vrouter', 'vswitch', 'vqfx']:
                # Growable ge-/xe- ranges: extract the last number (port index).
                numbers = sorted([int(iface.split('/')[-1]) for iface in ifaces])
            elif vm_type == 'server':
                # Server interfaces: extract number after 'em'
                numbers = sorted([int(iface[2:]) for iface in ifaces if iface.startswith('em')])
        except (ValueError, IndexError):
            # Should only happen if regex validation fails to catch a malformed string
            continue

        # --- Sequential Check (only the growable ge-/xe-/em ranges) ---
        if numbers:
            # Start index is 1 for 'server' (em1), 0 for others (ge-0/0/0, etc.)
            start_index = 1 if vm_type == 'server' else 0
            expected_sequence = list(range(start_index, start_index + len(numbers)))
            if numbers != expected_sequence:
                if vm_type == 'server':
                    errors.append(f"Interface numbering for server '{hostname}' must start at 'em1' and be sequential. Expected port indices {expected_sequence}, but found {numbers}.")
                else:
                    errors.append(f"Interface numbering for device '{hostname}' must start at index {start_index} and be sequential. Expected port indices {expected_sequence}, but found {numbers}.")

    # Deduplicate and sort for stable output; the caller decides how to report.
    return sorted(set(errors))


def validate_topology(data):
    """
    CLI wrapper around collect_topology_errors(): prints the errors in the
    familiar format and aborts. Behaviour is unchanged from before the split.
    """
    errors = collect_topology_errors(data)
    if errors:
        print("\n" + "="*60)
        print(" 🕵️‍♂️ YAML Topology Validation Failed")
        print("="*60)
        print("Please correct the following errors in your topology file:\n")
        for error in errors:
            print(f"  - {error}")
        print("\nScript aborted.")
        sys.exit(1)

# Interface mapping
# -----------------------------
def iface_map(name, vm_type):
    if vm_type == 'vqfx' and name.startswith("xe-0/0/"):
        try:
            idx = int(name.split("/")[-1])
            return f"em{idx+3}"
        except (ValueError, IndexError):
            return name
    return name

# -----------------------------
# Legacy 'sniffer' topologies
# -----------------------------
# Sniffing used to mean splicing a dedicated Linux VM into a link and bridging
# its two interfaces. That is gone: packets are now taken straight out of the
# running VM (see --capture and the builder's right-click menu), which needs no
# extra device, no redeploy and no re-cabling. Old topology files still load -
# the sniffer keys are simply ignored, with one warning so nobody wonders why
# their sniffer VM never appeared.
def warn_if_legacy_sniffer(data, quiet=False):
    """Report, once, that 'sniffer:'/'sniffer_disk' no longer do anything."""
    if quiet:
        return
    sniffed = [l for l in data.get('links', []) if l.get('sniffer')]
    if not sniffed and 'sniffer_disk' not in data.get('disks', {}):
        return
    print("⚠️  This topology still uses the old sniffer VM "
          f"({len(sniffed)} link(s) marked 'sniffer: true').")
    print("   Those keys are ignored now - nothing is spliced into the link.")
    print("   Capture live traffic instead, with no redeploy:")
    print(f"     python3 {os.path.basename(sys.argv[0])} --capture DEVICE --to PEER")
    print("   ...or right-click the link in the builder (--build).")



# -----------------------------
# Post-render safety net: PREFIXED connectors must have their alias killed
# -----------------------------
# Connectors that build the vNIC name by token-pasting their own prefix onto the
# argument, i.e. VBALERION_CONNECT(IF_ET(0,0,9), b) -> CATENATE(VBALERION_, <arg>).
# cpp fully macro-expands the argument first, so if the unprefixed alias
# IF_ET_X_<pic>_<port> is still live (the vmm3 EVO headers define ~250 of them)
# the argument becomes 'eth13' and the paste yields the bogus name
# 'VBALERION_eth13'. VMM accepts that silently: the bridge and the vNIC are still
# created, but no Junos interface is ever bound to them, so the link looks up on
# the peer yet passes no traffic and shows no LLDP neighbour. Each PREFIXED block
# therefore has to '#undef' the exact alias it is about to use.
#
# The alias is keyed by PIC, never FPC - verified on the pod:
#     #define IF_ET(FPC, PIC, PORT)        CATENATE(IF_ET_X_, CATENATE3(PIC, _, PORT))
#     #define IF_ET_CHAN(FPC,PIC,PORT,CH)  CATENATE(IF_ET_X_, CATENATE5(PIC, _, PORT, _, CH))
# IF_ET(1,0,0) and IF_ET(0,0,0) are both eth4; IF_ET(0,1,0) is eth16.
PREFIXED_CONNECTORS = ("VBALERION", "VBOWMORE", "VALFAROMEO", "VHAMILTON", "VMASERATI")

_PREFIXED_CONNECT_RE = re.compile(
    r"^\s*(?P<prefix>" + "|".join(PREFIXED_CONNECTORS) + r")_CONNECT\(\s*"
    r"IF_ET(?P<chan>_CHAN)?\(\s*(?P<args>[^)]*)\)",
    re.MULTILINE,
)


def _assert_prefixed_aliases_are_undefed(rendered):
    """Fail loudly if a PREFIXED *_CONNECT is emitted while its IF_ET_X alias is live.

    Catches the 'VBALERION_eth13' silent-dead-link class at generation time rather
    than after a multi-hour deploy. Only '#undef' lines that appear *before* the
    connector count, since cpp is order sensitive.
    """
    problems = []
    for m in _PREFIXED_CONNECT_RE.finditer(rendered):
        args = [a.strip() for a in m.group("args").split(",")]
        # IF_ET(fpc, pic, port) / IF_ET_CHAN(fpc, pic, port, chan) -> alias drops fpc
        expected_len = 4 if m.group("chan") else 3
        if len(args) != expected_len or not all(a.isdigit() for a in args):
            continue
        alias = "IF_ET_X_" + "_".join(args[1:])
        preceding = rendered[: m.start()]
        if f"#undef {alias}\n" not in preceding:
            line_no = preceding.count("\n") + 1
            problems.append(
                f"  line {line_no}: {m.group('prefix')}_CONNECT would expand to "
                f"'{m.group('prefix')}_<vnic>' because '#undef {alias}' is missing above it"
            )

    if problems:
        raise ValueError(
            "Internal template error - PREFIXED connector emitted without killing its "
            "IF_ET_X alias.\n"
            + "\n".join(problems)
            + "\n\nThis would produce a link that comes up on the peer but carries no "
            "traffic. Fix lab_template.j2 so the block emits "
            "'#undef IF_ET_X_<pic>_<port>[_<subport>]' (keyed by PIC, not FPC) for "
            "every interface before its *_CONNECT lines."
        )
# -----------------------------
# Generate VMM config
# -----------------------------
def generate_config(topology_file, output_file, quiet=False):
    """
    Generates the VMM configuration file from a YAML topology and returns the processed data.
    """
    if not quiet:
        print("\n" + "="*50)
        print(" ⚙️  Phase 1: Generating VMM Configuration")
        print("="*50)
    try:
        with open(topology_file) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"❌ Error: Topology file not found at '{topology_file}'", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"❌ Error: Could not parse YAML file '{topology_file}'. Error: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Save original links for the summary table before any modification
    original_links_list = data.get('links', []).copy()

    # --- VALIDATION STEP ---
    if not quiet:
        print("🕵️‍  Validating topology file rules...")
    validate_topology(data)
    if not quiet:
        print("✅ Topology file passed all validation checks.")
    # ---------------------------

    # Links are now wired exactly as written: nothing is spliced in. Traffic is
    # captured out of the running VM instead (--capture / builder right-click).
    capture_mappings = []
    warn_if_legacy_sniffer(data, quiet=quiet)

    # --- Process links to build VMM config data ---
    vms_by_hostname = {vm['hostname']: vm for vm in data.get('vms', [])}
    for vm in vms_by_hostname.values():
        vm['interfaces'] = []

    # This loop processes the (potentially modified) links to correctly assign interfaces to bridges for the VMM.
    for idx, link in enumerate(data.get("links", []), start=1):
        bridge_name = f"111__{idx}aaeppss"
        endpoints = link.get("endpoints", [])

        if len(endpoints) == 2:
            try:
                host1, iface1_name = endpoints[0].split(":", 1) 
                host2, iface2_name = endpoints[1].split(":", 1)

                def parse_iface(iface_name):
                    """
                    Extracts FPC, PIC, Port, and Subport. Subport is '0' for standard interfaces.
                    """
                    # Pattern 1: Channelized interfaces (e.g., et-0/0/0:3 for vptx)
                    match_channelized = re.match(r'^(et)-(\d+)/(\d+)/(\d+):(\d+)$', iface_name)
                    if match_channelized:
                        return {
                            'fpc': match_channelized.group(2), 
                            'pic': match_channelized.group(3), 
                            'port': match_channelized.group(4),
                            'subport': match_channelized.group(5) # Capture the subport index
                        }
                        
                    # Pattern 2: Standard interfaces (e.g., ge-0/0/0, xe-0/0/1, et-0/0/2)
                    match_standard = re.match(r'^(ge|xe|et)-(\d+)/(\d+)/(\d+)$', iface_name)
                    if match_standard:
                        return {
                            'fpc': match_standard.group(2), 
                            'pic': match_standard.group(3), 
                            'port': match_standard.group(4),
                            'subport': '0' # Default to '0' for consistency
                        }
                        
                    return {}



                if host1 in vms_by_hostname:
                    vm1 = vms_by_hostname[host1]
                    interface_data = {
                        'name': iface1_name,
                        'mapped_name': iface_map(iface1_name, vm1.get('type')),
                        'bridge': bridge_name,
                        'description': f'Connected to {host2} on interface {iface2_name}',
                    }
                    interface_data.update(parse_iface(iface1_name))
                    vm1['interfaces'].append(interface_data)

                if host2 in vms_by_hostname:
                    vm2 = vms_by_hostname[host2]
                    interface_data = {
                        'name': iface2_name,
                        'mapped_name': iface_map(iface2_name, vm2.get('type')),
                        'bridge': bridge_name,
                        'description': f'Connected to {host1} on interface {iface1_name}'
                    }
                    interface_data.update(parse_iface(iface2_name))
                    vm2['interfaces'].append(interface_data)

            except ValueError:
                print(f"⚠️  Warning: Malformed endpoint in link '{endpoints}'. Skipping.", file=sys.stderr)

    # --- Build FPC groupings for multi-FPC vmx VMs (consumed by lab_template.j2) ---
    for vm in vms_by_hostname.values():
        if vm.get('type') != 'vmx':
            continue
        fpc_map = defaultdict(list)
        for iface in vm.get('interfaces', []):
            catalog_entry = VMX_INTERFACE_CATALOG.get(iface['name'])
            if not catalog_entry:
                continue  # already flagged by validate_topology()
            iface['macro'] = catalog_entry['macro']
            iface['fpc'] = catalog_entry['fpc']
            iface['pic'] = catalog_entry['pic']
            iface['port'] = catalog_entry['port']
            iface['subport'] = catalog_entry['subport']
            fpc_map[catalog_entry['fpc']].append(iface)
        vm['fpc_groups'] = [
            {'fpc': fpc, 'i2cid_macro': FPC_I2CID_MACRO[fpc], 'interfaces': ifaces}
            for fpc, ifaces in sorted(fpc_map.items())
        ]

    # --- Assign a unique chassis I2C id to each vbugatti (VMX304) VM ---
    # Every VMX304 chassis in one file needs a distinct VMX304_CHASSIS_I2CID
    # (the reference config uses 21, 22, ... for successive instances), so
    # number them in topology order starting at VBUGATTI_CHASSIS_I2CID_BASE.
    vbugatti_ordinal = 0
    for vm in data.get('vms', []):
        if vm.get('type') == 'vbugatti':
            vm['chassis_i2cid'] = VBUGATTI_CHASSIS_I2CID_BASE + vbugatti_ordinal
            vbugatti_ordinal += 1

    # --- Build the comprehensive summary list for the output table ---
    final_summary_mappings = []
    for link in original_links_list:
        endpoints = link.get("endpoints", [])
        if len(endpoints) == 2:
            final_summary_mappings.append(
                {'link': f"{endpoints[0]} <--> {endpoints[1]}", 'capture_point': ""})

    # --- Emit VMs in vmm3 family order (RAW -> PREFIXED -> vbowmore last) ---
    # Stable sort keeps topology order within each rank. This only affects the
    # order chassis blocks are written to the .conf; links/bridges are unchanged.
    data['vms'] = sorted(data.get('vms', []),
                         key=lambda vm: VMM3_EMIT_RANK.get(vm.get('type'), 0))

    # --- Finalize data and generate the VMM config file ---
    all_vm_types = {vm.get('type') for vm in data.get('vms', [])}
    data['types'] = list(all_vm_types)

    env = Environment(loader=FileSystemLoader("."), trim_blocks=True, lstrip_blocks=True)

    def _unsupported_type(vm_type, hostname):
        # Reached only if lab_template.j2's {% elif vm.type == ... %} chain has
        # no branch for this type. Without this the VM would be silently omitted
        # from the config while its links still point at it.
        raise ValueError(
            f"VM '{hostname}' has type '{vm_type}', which lab_template.j2 has no "
            f"block for. Add a template case (and list the type in "
            f"SUPPORTED_VM_TYPES) before using it."
        )

    env.globals['undefined_vm_type_has_no_template_block'] = _unsupported_type
    template = env.get_template("lab_template.j2")
    output = template.render(data)

    _assert_prefixed_aliases_are_undefed(output)

    with open(output_file, "w") as f:
        f.write(output)
    if not quiet:
        print(f"✅ {output_file} generated successfully!")
    
    # --- Return the processed data and the new comprehensive summary list ---
    return data, final_summary_mappings, capture_mappings
# -----------------------------
# Apply VMM config and start lab
# -----------------------------

# Markers VMM prints on its own stdout/stderr when something went wrong.
# 'vmm config' exits 2 on a missing disk, but it exits 0 for a lab that is far
# too large for the pod (verified: a 900 GB VM is accepted with "write_config
# complete"), so exit codes alone are NOT a reliable success signal.
VMM_FAILURE_MARKERS = (
    "command FAILED",
    "Fatal error",
    "fatal error",
    "syntax error",
    "does not exist",
    "No config active",
    "not enough",
    "Cannot allocate",
)


def _run_vmm(args, timeout=900):
    """Run a 'vmm ...' command capturing output. Returns (returncode, output)."""
    try:
        p = subprocess.run(["vmm"] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "")
    except FileNotFoundError:
        return 127, "'vmm' command not found on this host (are you on the -vmm pod server?)"
    except subprocess.TimeoutExpired:
        return 124, f"'vmm {' '.join(args)}' timed out after {timeout}s"


def _show_vmm_output(output, limit=15):
    """Echo the tail of VMM's own output so the real error is visible."""
    lines = [l for l in (output or "").splitlines() if l.strip()]
    if not lines:
        return
    print("   ── vmm output " + "─" * 46)
    for line in lines[-limit:]:
        print(f"   │ {line}")
    print("   " + "─" * 60)


def get_vmm_capacity():
    """
    Parse 'vmm capacity -g vmm-default'. Values are in GB (a pod's total
    tracks blades x 64 GB). Returns a dict or None if it cannot be read.

    'largest' is the biggest single VM that still fits on any one blade - a
    32 GB FPC needs largest >= 32 even when total free capacity looks ample.
    """
    rc, out = _run_vmm(["capacity", "-g", "vmm-default"], timeout=120)
    if rc != 0:
        return None
    fields = {"Total capacity": "total", "Utilized capacity": "utilized",
              "Free capacity": "free", "Current largest VM available": "largest"}
    caps = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        name = fields.get(key.strip())
        if name:
            try:
                caps[name] = int(val.strip())
            except ValueError:
                pass
    return caps if "free" in caps and "largest" in caps else None


def _expand_config(config_file):
    """
    Pre-process a VMM config the way VMM does. Most VMs (and their memory
    values) are produced by macros, so the raw file only mentions a handful of
    them literally - anything inspecting the config has to expand it first.
    'common.site.defs' is not a real file on the pod, so an empty stub stands
    in for it (it defines no VMs or memory values).

    Returns the expanded text, or None if it cannot be expanded.
    """
    try:
        with tempfile.TemporaryDirectory() as stub:
            open(os.path.join(stub, "common.site.defs"), "w").close()
            p = subprocess.run(["cpp", "-P", "-I", stub, config_file],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, timeout=120)
            return p.stdout if p.returncode == 0 and p.stdout else None
    except Exception:
        return None


def get_lab_requirements(config_file):
    """
    Measure what the generated lab needs.

    Returns (total_gb, max_vm_gb, vm_count) or None if it cannot be measured.
    """
    expanded = _expand_config(config_file)
    if not expanded:
        return None
    blocks = re.split(r'vm\s+"([^"]+)"\s*\{', expanded)
    sizes = []
    for i in range(1, len(blocks), 2):
        m = re.search(r"memory\s+(\d+)", blocks[i + 1])
        if m:
            sizes.append(int(m.group(1)) // 1024)
    if not sizes:
        return None
    return sum(sizes), max(sizes), len(sizes)


def preflight_capacity_check(config_file, force=False):
    """
    Compare what the lab needs against what the pod can actually host, BEFORE
    unbinding anything. 'vmm config' does not check capacity, so without this
    an oversized lab tears down the running lab and then silently fails to
    bind. Advisory only: if either side cannot be measured, we proceed.
    """
    need = get_lab_requirements(config_file)
    caps = get_vmm_capacity()
    if not need or not caps:
        return
    total_gb, max_vm_gb, vm_count = need
    print(f"Lab needs {total_gb} GB across {vm_count} VMs "
          f"(largest single VM {max_vm_gb} GB); pod has {caps['free']} GB free, "
          f"largest free slot {caps['largest']} GB.")

    problems = []
    if max_vm_gb > caps["largest"]:
        problems.append(
            f"largest VM is {max_vm_gb} GB but the biggest free slot on any "
            f"blade is {caps['largest']} GB")
    if total_gb > caps["free"]:
        problems.append(
            f"lab totals {total_gb} GB but only {caps['free']} GB is free")
    if not problems:
        return

    print("\n❌ This lab cannot fit on this pod:", file=sys.stderr)
    for p in problems:
        print(f"   • {p}", file=sys.stderr)
    print("\n   Nothing has been changed - your current lab is untouched.\n"
          "   Options:\n"
          "     • deploy a smaller subset of the topology\n"
          "     • free capacity, or try another pod\n"
          "     • re-run with --force to attempt it anyway", file=sys.stderr)
    if not force:
        sys.exit(1)
    print("⚠️  --force given: continuing despite the capacity shortfall.",
          file=sys.stderr)


def verify_lab_running(config_file, settle_timeout=180, poll_interval=5):
    """
    Confirm the lab actually materialised. 'vmm start' can exit 0 while nothing
    was bound (e.g. an orphaned lab still holds the capacity), which used to be
    reported as success and then wasted the whole boot wait followed by an
    endless serial-console retry loop against VMs that do not exist.

    'vmm start' also returns *before* VMM has finished registering the VMs, so a
    single 'vmm ls' right after it reports a lab that is coming up fine as
    completely absent. Poll until every expected VM has appeared, and only call
    it a failure once settle_timeout has passed with some still missing.

    Returns a list of problem strings (empty when the lab is really up).
    """
    expanded = _expand_config(config_file)
    if expanded is None:
        # Fall back to the raw file; it names only the literal VMs, so the
        # "absent" check is skipped rather than reporting false positives.
        expected = set()
    else:
        expected = set(re.findall(r'vm\s+"([^"]+)"\s*\{', expanded))

    deadline = time.time() + settle_timeout
    announced = False
    while True:
        rc, out = _run_vmm(["ls"], timeout=180)
        if rc != 0:
            return [f"'vmm ls' failed (exit {rc})"]

        listed = {line.split("\t")[0].strip() for line in out.splitlines() if line.strip()}
        missing = sorted(n for n in expected if n not in listed)
        if not missing or time.time() >= deadline:
            break
        if not announced:
            print(f"   waiting for VMM to register {len(missing)}/{len(expected)} "
                  f"VMs (up to {settle_timeout}s)...")
            announced = True
        time.sleep(poll_interval)

    problems = []
    if "No config active" in out:
        problems.append("VMM reports 'No config active' - the config never took effect")
    if missing:
        shown = ", ".join(missing[:6]) + ("..." if len(missing) > 6 else "")
        problems.append(f"{len(missing)}/{len(expected)} configured VMs are absent "
                        f"after {settle_timeout}s: {shown}")
    elif announced:
        print(f"   all {len(expected)} VMs registered.")

    orphans = [l.split("VM", 1)[-1].split("on server")[0].strip()
               for l in out.splitlines() if "not in current config" in l]
    if orphans:
        shown = ", ".join(orphans[:6]) + ("..." if len(orphans) > 6 else "")
        problems.append(f"{len(orphans)} VM(s) from a previous lab still on the server: {shown}")
    return problems


def _bound_vms(timeout=180):
    """Names still held by VMM, i.e. listed as anything other than Unbound.

    Returns (ok, names). ok is False when 'vmm ls' itself failed, so the caller
    can tell "nothing is bound" apart from "we could not find out".
    """
    rc, out = _run_vmm(["ls"], timeout=timeout)
    if rc != 0:
        return False, []
    held = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, state = parts[0].strip(), parts[1].strip()
        if name and state and state.lower() != "unbound":
            held.append(name)
    return True, sorted(held)


def wait_for_unbind(settle_timeout=180, poll_interval=5):
    """Wait until the previous lab has actually let go of the pod.

    'vmm unbind' is documented as "terminate/cleanup" and it returns before the
    cleanup half has finished - the same asynchrony that already bites
    'vmm start' (see verify_lab_running). Applying the new config into that
    window leaves the pod half in the old lab and half in the new one, and it
    is the genuinely new VMs that fail to bind, because the names carried over
    from the previous lab are still sitting there.

    That failure is invisible at unbind time: 'vmm unbind' exits 0 and the
    deploy only falls over two commands later, which is why it reads as
    "unbind does not work" and gets worked around by running unbind by hand.

    Returns (clear, still_held). Costs nothing when the pod is already clear.
    """
    deadline = time.time() + settle_timeout
    announced = False
    started = time.time()
    while True:
        ok, held = _bound_vms()
        if not ok:
            return True, []            # cannot tell; let the deploy proceed
        if not held:
            if announced:
                print(f"   pod released after {int(time.time() - started)}s.")
            return True, []
        if time.time() >= deadline:
            return False, held
        if not announced:
            print(f"   waiting for VMM to release {len(held)} VM(s) from the "
                  f"previous lab (up to {settle_timeout}s)...")
            announced = True
        time.sleep(poll_interval)


def run_vmm_config(config_file, force=False):
    """Applies the VMM config and starts the lab."""
    print("\n" + "="*50)
    print(" 🚀 Phase 2: Starting the Lab")
    print("="*50)

    preflight_capacity_check(config_file, force=force)

    print("Perfoming VMM unbind")
    rc, out = _run_vmm(["unbind"])
    if rc != 0:
        # Not fatal on its own (there may be nothing bound), but a failed
        # unbind leaves the old lab holding capacity, so never hide it.
        print(f"⚠️  'vmm unbind' returned {rc} - the previous lab may still "
              f"be holding capacity.", file=sys.stderr)
        _show_vmm_output(out)

    # unbind returns before its cleanup finishes; going straight into 'config'
    # is what makes a deploy bind some VMs and silently drop the rest.
    clear, held = wait_for_unbind()
    if not clear:
        # unbind is idempotent ("already unbound - ignoring"), so a second pass
        # costs nothing and clears VMs the first one did not get to.
        print(f"   {len(held)} VM(s) still bound - running 'vmm unbind' again...")
        rc, out = _run_vmm(["unbind"])
        if rc != 0:
            _show_vmm_output(out)
        clear, held = wait_for_unbind()
    if not clear:
        shown = ", ".join(held[:6]) + ("..." if len(held) > 6 else "")
        print(f"⚠️  {len(held)} VM(s) are still bound after two unbind attempts: "
              f"{shown}\n"
              f"   Applying the config now tends to bind only the names that "
              f"carried over from the\n"
              f"   previous lab and silently drop the new ones. If this deploy "
              f"fails, run 'vmm unbind'\n"
              f"   by hand, check 'vmm ls' is clear, then re-run.",
              file=sys.stderr)

    print("Applying vmm config!")
    rc, out = _run_vmm(["config", config_file, "-g", "vmm-default"])
    failed = [m for m in VMM_FAILURE_MARKERS if m in out]
    if rc != 0 or failed:
        print(f"❌ Failed to apply VMM config (exit {rc}).", file=sys.stderr)
        _show_vmm_output(out)
        sys.exit(1)

    rc, out = _run_vmm(["start"])
    if rc != 0:
        print(f"❌ Failed to start VMM lab (exit {rc}).", file=sys.stderr)
        _show_vmm_output(out)
        sys.exit(1)

    problems = verify_lab_running(config_file)
    if problems:
        print("\n❌ VMM reported success but the lab is not actually running:",
              file=sys.stderr)
        for p in problems:
            print(f"   • {p}", file=sys.stderr)
        caps = get_vmm_capacity()
        if caps:
            print(f"\n   Pod capacity: {caps['free']} GB free, largest free "
                  f"slot {caps['largest']} GB.", file=sys.stderr)
        print("\n   This is usually an orphaned lab still holding capacity, or\n"
              "   not enough room to place the VMs. Check with:\n"
              "       vmm ls\n"
              "       vmm capacity -g vmm-default\n"
              "   Stopping here rather than waiting for devices that will "
              "never boot.", file=sys.stderr)
        sys.exit(1)

    print("✅ VMM lab started!")
# -----------------------------
# Monitor devices with 'vmm ping'
# -----------------------------
def monitor_vms(devices, timeout=900, stall_timeout=60, poll_interval=5,
                no_response_timeout=420):
    """
    Wait until the devices we are about to configure report 'alive' in
    'vmm ping', then let Phase 4 proceed. `devices` is a list of
    (hostname, vm_type); each is tracked by its RE / console name (the same
    name that shows up in 'vmm ping' - PE1_RE, R4_RE0, R2, ...), so the
    progress bar reflects the actual routing engines rather than every VM IP.

    This is the boot gate: we only drive a serial console once its device is
    reachable. It is still non-fatal, though - if a device never answers ping
    (some REs never get a management address), the stall/timeout path reports
    it and continues, and the serial login coaxes its own prompt.
    """
    # Map each configurable device to the name it appears under in 'vmm ping'.
    targets = {re_ping_name(host, vtype): host for host, vtype in devices}

    print("\n" + "="*50)
    print(" ⏳ Phase 3: Waiting for devices to become reachable")
    print("="*50)

    if not targets:
        print("No configurable devices to wait for; proceeding.")
        return

    print(f"\U0001f4e1 Waiting for {len(targets)} routing engine(s) to boot: "
          f"{', '.join(sorted(targets))}")

    print("(devices are still configured over serial afterwards, which waits "
          "for boot on its own - press Ctrl+C to skip this wait)")

    total = len(targets)
    start_time = time.time()
    last_alive_count = -1
    last_change = start_time
    last_poll = 0.0
    alive = set()
    bar_length = 40
    spin = "|/-\\"

    def draw(alive_count, elapsed):
        pct = alive_count / total
        filled = int(bar_length * pct)
        bar = "█" * filled + "-" * (bar_length - filled)
        s = spin[int(elapsed) % 4]
        sys.stdout.write(f"\r[{bar}] {alive_count}/{total} booted ({pct:.0%})  "
                         f"{s} {int(elapsed)}s   ")
        sys.stdout.flush()

    def report_pending(header, pending):
        print(header)
        for name in sorted(pending):
            print(f"   - {name}  (device {targets[name]})")
        print("\n➡️  Continuing to the configuration phase; the serial login "
              "waits for boot and retries on its own.")

    try:
        while True:
            now = time.time()
            # Poll 'vmm ping' every poll_interval, but redraw the bar every
            # second so a slow boot shows a live ticking clock, not a frozen bar.
            if last_poll == 0.0 or now - last_poll >= poll_interval:
                ping_map = get_vmm_ping_map()
                alive = {n for n in targets if ping_map.get(n, "").lower() == "alive"}
                last_poll = now
                if len(alive) != last_alive_count:
                    last_alive_count = len(alive)
                    last_change = now

            draw(len(alive), now - start_time)

            if len(alive) == total:
                print("\n\n✅ All devices booted and reachable!")
                return

            pending = set(targets) - alive
            # Stall: nothing has changed for a while. Use a longer grace before
            # ANY device answers (they may still be booting) and a shorter one
            # once some are up (the rest may never get a mgmt address).
            grace = stall_timeout if alive else no_response_timeout
            if now - last_change > grace:
                if alive:
                    report_pending(f"\n\n⏳ No further progress for {stall_timeout}s "
                                   f"({len(alive)}/{total} reachable). Still waiting on:", pending)
                else:
                    report_pending(f"\n\n⏳ No device answered ping in {no_response_timeout}s.", pending)
                return

            if now - start_time > timeout:
                report_pending(f"\n\n⏰ Timeout reached ({timeout}s). Not reachable:", pending)
                return

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n⏭️  Skipping the boot wait; going straight to configuration.")


# -----------------------------
# Resolve hostname to IP
# -----------------------------
def resolve_ip(hostname):
    """Resolves a hostname to an IP address using the 'dig' command."""
    try:
        result = subprocess.run(["dig", "+short", hostname], capture_output=True, text=True, check=True, timeout=5)
        ip = result.stdout.strip()
        return ip if ip else hostname
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print(f"⚠️  Could not resolve '{hostname}', using hostname instead.", file=sys.stderr)
        return hostname
# -----------------------------
# Get VMM nodes info
# -----------------------------
def get_vmx_nodes():
    """
    Runs 'vmm serial', resolves hostnames, and returns a list of (name, ip, port) tuples.
    Filters out FPC and pecosim components.
    """
   # print("⚙️  Fetching node information from 'vmm serial'...")
    try:
        vmm_result = subprocess.run(["vmm", "serial"], capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"❌ Error running 'vmm serial': {e}", file=sys.stderr)
        return []
        
    nodes_info = []
    for line in vmm_result.stdout.strip().splitlines():
        if "_FPC" in line or "pecosim" in line:
           # print(f"   - Ignoring component: {line.split()[0]}")
            continue

        parts = line.split()
        if len(parts) >= 3:
            name, host, port = parts[0], parts[1], int(parts[2])
            ip = resolve_ip(host)
            base_name = name.split("_RE")[0]
            # vBrackla's chassis is named "{hostname}-vBrackla" (see
            # PTX_CHAS_NAME in lab_template.j2), so strip that suffix too to
            # resolve back to the plain topo.yml hostname.
            if base_name.endswith("-vBrackla"):
                base_name = base_name[: -len("-vBrackla")]
            nodes_info.append((base_name, ip, port))
            #print(f"   - Found node: {name} (as {base_name}) at {ip}:{port}")
    
    if not nodes_info:
        print("❌ Error: 'vmm serial' returned no configurable nodes.", file=sys.stderr)
        sys.exit(1)
    return nodes_info
# -----------------------------
# Shared Junos serial-console helpers
# -----------------------------
# Every configure_*_serial() function below drives the same console
# login/boot state machine before applying its type-specific baseline
# commands. These helpers centralise that shared logic so adding a new VM
# template only requires supplying a console name and a command list.

def _spawn_serial_with_retry(cmd, name, debug=False, retries=15, delay=10):
    """Spawn a pexpect serial session, retrying on EOF until the console is
    ready. Returns the live child, or raises after `retries` attempts."""
    for attempt in range(1, retries + 1):
        try:
            child = pexpect.spawn(cmd, encoding='utf-8', timeout=300)
            if debug:
                child.logfile_read = sys.stdout
            return child
        except pexpect.exceptions.EOF:
            if debug:
                print(f"[{name}] Serial not ready, retry {attempt}/{retries}...")
            time.sleep(delay)
    raise Exception(f"Serial console {name} not ready after {retries} attempts")


def _junos_serial_login(child, name, spawn_fn, debug=False,
                        boot_timeout=360, cli_timeout=600, nudge_interval=8):
    """Log in over the serial console and leave the device at the
    configuration-mode ('# ') prompt. Returns the (possibly re-spawned) child.

    Done in two phases so the login handshake is atomic instead of being run
    through a general match loop:

      Phase A - reach a shell or CLI prompt, logging in as root if asked. The
        login is handled explicitly: after sending the username we consume
        forward to the 'Password:' prompt, so a re-printed 'login:' banner
        (the console echoes one every time it receives a stray Enter) can't
        make us send the username again and queue up junk - which previously
        produced 'Login incorrect'.
      Phase B - from that shell/CLI, step into configuration mode.

    No up-front Enter burst: Phase 3 has already confirmed the device answers
    'vmm ping', so it is booted and sitting at a prompt. We nudge with a single
    Enter only when the console has actually gone quiet.

    `cli_timeout` bounds the cli -> edit transition; `boot_timeout` bounds the
    whole login. `nudge_interval` is how long to wait for output before
    pressing Enter to elicit a prompt.
    """
    def dbg(msg):
        if debug:
            print(f"[{name}] {msg}")

    deadline = time.time() + boot_timeout
    child.sendline("")           # one gentle nudge
    state = None                 # which shell/CLI prompt we landed on

    # --- Phase A: log in, reach a shell or CLI prompt ---
    while time.time() < deadline:
        idx = child.expect([
            "login:",              # 0
            "Password:",           # 1
            "Login incorrect",     # 2
            PROMPT_SHELL_PCT,      # 3  FreeBSD shell
            PROMPT_SHELL_ROOT,     # 4  Linux root shell
            PROMPT_OPER,           # 5  Junos operational mode
            PROMPT_SHELL_BARE,     # 6  bare '# ' host/Linux shell (needs 'cli')
            pexpect.TIMEOUT,       # 7
            pexpect.EOF,           # 8
        ], timeout=nudge_interval)
        dbg(f"phaseA idx={idx}")

        if idx == 0:             # login: -> send user, then wait for Password:
            child.sendline(DEVICE_ROOT_USER)
            p = child.expect(["Password:", pexpect.TIMEOUT], timeout=15)
            if p == 0:
                child.sendline(DEVICE_ROOT_PASSWORD)
        elif idx == 1:           # Password: on its own -> answer it
            child.sendline(DEVICE_ROOT_PASSWORD)
        elif idx == 2:           # Login incorrect -> a fresh login: will follow
            dbg("login rejected, retrying")
            time.sleep(1)
        elif idx in (3, 4, 5, 6):
            state = idx
            break
        elif idx == 7:           # quiet -> single Enter to elicit a prompt
            child.sendline("")
        elif idx == 8:           # EOF -> re-spawn and re-nudge
            dbg("EOF, re-spawning console")
            child.close(force=True)
            child = spawn_fn()
            child.sendline("")
    if state is None:
        raise Exception(f"[{name}] could not reach a shell/CLI prompt within {boot_timeout}s "
                        f"(check that the root password matches VMM_DEVICE_PASSWORD)")

    # --- Phase B: shell/CLI -> configuration mode ---
    # A Junos config prompt is ALWAYS 'root@host#'; a bare '# ' (state 6) is a
    # host/Linux shell, not config mode (e.g. the vBugatti/MX304 RE drops you
    # at a bare '#' and you type 'cli' to enter Junos). So a bare '#' is
    # treated like the other shells - run 'cli' - rather than assumed to be
    # config mode, which used to make us fire 'set ...' commands at the shell.
    if state in (3, 4, 6):       # FreeBSD %, Linux root shell, or bare '#' -> cli
        child.sendline("cli")
        child.expect(PROMPT_OPER, timeout=cli_timeout)
        child.sendline("edit")
        child.expect(PROMPT_CONFIG, timeout=cli_timeout)
    elif state == 5:             # Junos operational '>' -> edit
        child.sendline("edit")
        child.expect(PROMPT_CONFIG, timeout=cli_timeout)
    dbg("reached configuration mode")
    return child


def _set_root_password(child, password=None):
    """Handle the interactive 'set system root-authentication
    plain-text-password' prompt sequence, returning to the '# ' prompt."""
    if password is None:
        password = DEVICE_ROOT_PASSWORD
    child.sendline("set system root-authentication plain-text-password")
    child.expect("New password:")
    child.sendline(password)
    child.expect(["Retype new password:", "Re-enter password:"])
    child.sendline(password)
    child.expect(PROMPT_CONFIG)

# -----------------------------
# Configure a vrouter/vswitch
# -----------------------------
def configure_vjunos_serial(name, interfaces, debug=False, retries=15, delay=10, re_name=None):
    """
    Applies the vJunosRouter/vJunosSwitch baseline to a single device via
    serial. `re_name` overrides the console name (default '{name}'); vBugatti
    reuses this exact baseline but its RE console is '{name}_RE'.
    """

    cmd = f"vmm serial -t {re_name or name}"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    try:
        child = spawn()
        child = _junos_serial_login(child, name, spawn, debug=debug)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=PROMPT_CONFIG, timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password
        _set_root_password(child)

        # --- Configure interfaces descriptions ---
        for iface in interfaces:
            send_and_expect(f'set interfaces {iface["name"]} description "{iface["description"]}"')

        # --- Baseline commands ---
        commands = [
            "delete groups",
            "delete apply-groups",
            "delete chassis auto-image-upgrade",
            "set system services ssh root-login allow",
            "set system services ssh sftp-server",
            "set system services netconf ssh",
            "set system management-instance",
            "set routing-instances mgmt_junos", 
            "delete protocols router-advertisement", 
            "set protocols lldp interface all",
            "set protocols lldp interface fxp0 disable",
            "set chassis network-services enhanced-ip", 
            "set chassis aggregated-devices ethernet device-count 10",
            "delete system processes dhcp-service",
            "delete groups member0",
            "set interfaces fxp0.0 family inet dhcp",
        ]
        for c in commands:
            send_and_expect(c)

        # --- Commit & exit ---
        send_and_expect("commit and-quit", prompt=PROMPT_OPER, timeout=120)
        child.sendline("exit")
        child.close(force=True)

        return f"✅ Successfully configured {name}"

    except pexpect.exceptions.TIMEOUT:
        return f"Failure: {name} (Timeout)"
    except pexpect.exceptions.EOF:
        return f"Failure: {name} (Connection Closed)"
    except Exception as e:
        print(f"❌ Failed to configure device {name} via serial. Error: {e}", file=sys.stderr)
        return f"Failure: {name} ({e})"



# Configure a vqfx via Telnet
# -----------------------------
def configure_vqfx(name, ip, port, interfaces):
    """Applies a baseline configuration to a single device via Telnet."""
   # print(f"🚀 Configuring {name} via Telnet...")
    try:
        tn = telnetlib.Telnet(ip, port, timeout=20)
        tn.write(b"\n\n"); time.sleep(1); tn.write(b"\n\n"); time.sleep(1)
        tn.read_until(b"login: ", timeout=10)
        tn.write(DEVICE_ROOT_USER.encode('ascii') + b"\n")
        tn.read_until(b"Password:",timeout=5)
        tn.write(DEVICE_ROOT_PASSWORD.encode('ascii') + b"\n")
        tn.read_until(b"% ", timeout=20)
        tn.write(b"cli\n")
        tn.read_until(b"> ", timeout=10)
        tn.write(b"edit\n")
        tn.read_until(b"# ", timeout=10)

        tn.write(f"set system host-name {name}\n".encode('ascii'))
        tn.read_until(b"# ", timeout=10)
        tn.write(b"set system root-authentication plain-text-password\n")
        tn.read_until(b"New password: ", timeout=10)
        tn.write(DEVICE_ROOT_PASSWORD.encode('ascii') + b"\n")
        tn.read_until(b"Retype new password: ", timeout=10)
        tn.write(DEVICE_ROOT_PASSWORD.encode('ascii') + b"\n")
        tn.read_until(b"# ", timeout=10)
        tn.write(b"set interfaces em1 unit 0 family inet address 169.254.0.2/24\n")
        tn.read_until(b"# ", timeout=1)
        tn.write(b"set interfaces em0 unit 0 family inet dhcp\n")
        tn.read_until(b"# ", timeout=1)
        tn.write(b"delete groups\n")
        tn.read_until(b"# ", timeout=1)
        tn.write(b"delete apply-groups\n")
        tn.read_until(b"# ", timeout=1)



        
        for iface in interfaces:
            desc_command = f"set interfaces {iface['name']} description \"{iface['description']}\"\n".encode('ascii')
            tn.write(desc_command)
            tn.read_until(b"# ", timeout=10)

        commands = [
            b"delete chassis auto-image-upgrade",
            b"set system services ssh root-login allow",
            b"set system services ssh sftp-server",
            b"set system services netconf ssh",
            b"set system management-instance",
            b"delete protocols router-advertisement",
            b"set protocols lldp interface all",
            b"set protocols lldp interface em0 disable",
            b"set chassis aggregated-devices ethernet device-count 10",
            b"delete system processes dhcp-service"
        ]
        for cmd in commands:
            tn.write(cmd + b'\n')
            tn.read_until(b"# ", timeout=10)
        
        tn.write(b"commit and-quit\n")
        tn.read_until(b"> ", timeout=60)
        tn.write(b"exit\n")
        tn.close()
        return f"✅ Successfully configured {name}"
    except Exception as e:
        print(f"❌ Failed to configure device {name} via Telnet. Error: {e}", file=sys.stderr)
        return f"Failure: {name} ({e})"
# -----------------------------
# Configure a VMX via Serial console
# -----------------------------
# Baseline configuration pushed to every vmx after boot. This replaces the
# old vmx-default.cfg that the template used to drop onto the RE with an
# 'install' statement: the device now boots with nothing but the lab's
# inherited groups, and everything below is applied by vmm.py instead.
#
# Applied over the serial console rather than SSH: serial does not depend on
# the management IP, DHCP, NETCONF or the inherited root password, so it stays
# usable even while this very config is tearing the groups (and with them the
# mgmt addressing) out from under the device.
def _vmx_baseline(mgmt_iface="fxp0", include_fpc3_picmode=False):
    """
    Build the vmx-family baseline, parameterised by:
      mgmt_iface          - 'fxp0' on vmx/vFerrari, 'em0' on vAlfaRomeo.
      include_fpc3_picmode - only the multi-FPC 'vmx' profile has an FPC3, so
                            'set chassis fpc 3 pic 0 pic-mode 40G' applies to
                            vmx alone. vFerrari (single FPC0) and vAlfaRomeo
                            (MX10008 FPC0) have no FPC3, and that line fails on
                            commit for them.
    """
    chassis = [
        "set chassis aggregated-devices ethernet device-count 10",
    ]
    if include_fpc3_picmode:
        chassis.append("set chassis fpc 3 pic 0 pic-mode 40G")
    chassis.append("set chassis network-services enhanced-ip")

    return [
        "set system services netconf ssh",
        "set system services ssh root-login allow",
        "set system services ssh sftp-server",
        "set system management-instance",
        *chassis,
        "set protocols lldp interface all",
        f"set protocols lldp interface {mgmt_iface} disable",
        # Management addressing. Required: 'delete groups' removes the mgmt
        # address inherited from member0, so without this the RE ends up with
        # no management config at all - it stays 'no-response' to 'vmm ping'
        # and later SSH/NETCONF runs (--config get/push) cannot reach it.
        f"set interfaces {mgmt_iface} unit 0 family inet dhcp",
    ]


# vmx has an FPC3 (multi-FPC profile); vFerrari and vAlfaRomeo do not.
# vmx and vFerrari manage on fxp0; vAlfaRomeo manages on em0.
VMX_BASELINE_LINES = _vmx_baseline("fxp0", include_fpc3_picmode=True)
VALFAROMEO_BASELINE_LINES = _vmx_baseline("em0", include_fpc3_picmode=False)
# vHamilton (vMX10004) is an MX-family RE with em0 management and a single
# FPC0, so it takes the same baseline shape as vAlfaRomeo.
VHAMILTON_BASELINE_LINES = _vmx_baseline("em0", include_fpc3_picmode=False)
VFERRARI_BASELINE_LINES = _vmx_baseline("fxp0", include_fpc3_picmode=False) + [
    # vFerrari-specific: required forwarding mode for this profile.
    "set forwarding-options hyper-mode",
]
# NOTE: vBugatti (MX304) is NOT configured with a vmx-family baseline - it
# reuses the vJunosRouter init (configure_vjunos_serial), applied on its
# '{host}_RE' console. See the Phase 4 dispatch in main().


def configure_vmx_serial(name, interfaces, baseline=None, debug=False,
                         retries=15, delay=10, re_name=None):
    """
    Configure a vmx-family device (vmx, vFerrari, vAlfaRomeo) over its serial
    console. `baseline` selects the variant; `re_name` overrides the RE console
    name (default '{hostname}_RE').

    The vmx boots with only the lab's inherited groups (no vmx-default.cfg is
    installed any more), so this applies the complete baseline in one commit:

        delete apply-groups
        delete groups
        set system host-name <topo hostname>
        set system root-authentication plain-text-password  (interactive)
        <VMX_BASELINE_LINES>
        set interfaces <ifd> description "..."   (one per link)
        commit and-quit

    Deleting the groups removes the inherited root-authentication and the fxp0
    management address. Over serial neither matters: the root password is set
    back interactively in the same session (so later SSH/NETCONF runs such as
    --config still work), and fxp0 simply falls back to DHCP from
    VMX_BASELINE_LINES without any risk of cutting off our own console.
    """
    if baseline is None:
        baseline = VMX_BASELINE_LINES
    if re_name is None:
        re_name = f"{name}_RE"

    cmd = f"vmm serial -t {re_name}"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    # Tracks the last command sent so a TIMEOUT failure can report where it
    # got stuck; defined before the try block so it's always safe to read.
    last_command = "(spawning serial console)"

    try:
        child = spawn()
        # A multi-FPC vmx can take much longer to become fully interactive
        # (more MPCs to bring up), so allow a large cli_timeout.
        last_command = "(login/boot sequence)"
        child = _junos_serial_login(child, name, spawn, debug=debug, cli_timeout=600)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=PROMPT_CONFIG, timeout=60):
            nonlocal last_command
            last_command = cmd
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Drop the inherited lab groups ---
        # apply-groups first, so nothing references the groups being removed.
        send_and_expect("delete apply-groups")
        send_and_expect("delete groups")

        # --- Identity ---
        send_and_expect(f"set system host-name {name}")

        # root-authentication is mandatory at commit time and was only
        # provided by the deleted 'global' group, so set it back here. This
        # also pins the password to DEVICE_ROOT_PASSWORD so the later
        # SSH/NETCONF paths (--config get/push) can log in.
        last_command = "set system root-authentication plain-text-password"
        _set_root_password(child)

        # --- Baseline ---
        for line in baseline:
            send_and_expect(line)

        # --- Interface descriptions for the links defined in topo.yml ---
        for iface in interfaces:
            send_and_expect(
                f'set interfaces {iface["name"]} description "{iface["description"]}"'
            )

        # --- Commit & detach ---
        send_and_expect("commit and-quit", prompt=PROMPT_OPER, timeout=300)
        child.sendline("exit")
        child.close(force=True)

        return f"✅ Successfully configured {name}"

    except pexpect.exceptions.TIMEOUT:
        return f"Failure: {name} (Timeout while waiting for: '{last_command}')"
    except pexpect.exceptions.EOF:
        return f"Failure: {name} (Connection Closed)"
    except Exception as e:
        return f"Failure: {name} ({e})"



def configure_vscapa_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Connects to a vscapa/vPTX device via serial console,
    logs in, configures baseline system + mgmt DHCP + optional default route,
    and commits config.
    """

    cmd = f"vmm serial -t {name}_RE0"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    try:
        child = spawn()
        child = _junos_serial_login(child, name, spawn, debug=debug)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=PROMPT_CONFIG, timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password (interactive)
        _set_root_password(child)

        # --- Apply base config including DHCP on mgmt interface ---
        commands = [
            "delete groups",
            "delete apply-groups",
            "set system services ssh root-login allow",
            "set system services ssh sftp-server",
            "set system services netconf ssh",
            "set system management-instance",
            "set protocols lldp interface all",
            "set protocols lldp interface re0:mgmt-0 disable",
            "set chassis aggregated-devices ethernet device-count 10",
            "set interfaces re0:mgmt-0.0 family inet dhcp",
        ]
        for c in commands:
            send_and_expect(c)

        for iface in interfaces:
            desc_cmd = f"set interfaces {iface['name']} description \"{iface['description']}\""
            send_and_expect(desc_cmd) 

        # --- First commit to apply base config ---
        send_and_expect("commit", timeout=120)
        time.sleep(35)  # adjust if needed based on lab speed
        if debug:
            print(f"[{name}] base config committed, waiting for mgmt interface to come up...")

        # --- Wait for DHCP to populate ---
     

        # Avoiding PR :1726785
        gw_ip = None
        max_gw_attempts = 25
        for attempt in range(1, max_gw_attempts + 1):
            time.sleep(5)
            child.sendline('run show dhcp client binding detail | match "Name: routers, Value: "')
            child.expect(PROMPT_CONFIG, timeout=30)
            output = child.before
            if debug:
                print(f"[{name}] DHCP binding output (attempt {attempt}):\n{output}")

            gw_match = re.search(r"Name: routers, Value: (\d+\.\d+\.\d+\.\d+)", output)
            if gw_match:
                gw_ip = gw_match.group(1)
                if debug:
                    print(f"[{name}] detected gateway: {gw_ip}")
                break

        # --- Configure static route if gateway found ---
        if gw_ip:
            send_and_expect(f"set routing-instances mgmt_junos routing-options static route 0/0 next-hop {gw_ip}")
            send_and_expect("commit", timeout=60)
        else:
            if debug:
                print(f"[{name}] DHCP gateway info not found, skipping static route configuration")

        # --- Exit config mode ---
        child.sendline("exit")
        child.close(force=True)

        return f"✅ Successfully configured {name}"

    except pexpect.exceptions.TIMEOUT:
        return f"Failure: {name} (Timeout)"
    except pexpect.exceptions.EOF:
        return f"Failure: {name} (Connection Closed)"
    except Exception as e:
        return f"Failure: {name} ({e})"


def configure_vbrackla_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Connects to a vBrackla device via serial console and applies the same
    baseline configuration as configure_vscapa_serial() (login, mgmt DHCP,
    default route detection, interface descriptions, commit).

    NOTE: the vBrackla chassis is named "{hostname}-vBrackla" in
    lab_template.j2 (see PTX_CHAS_NAME), so unlike vscapa's "{name}_RE0",
    the serial console here is assumed to be "{name}-vBrackla_RE0". If this
    doesn't match reality, run 'vmm serial | grep -i <hostname>' to get the
    exact console name and this line is the only one that needs adjusting.
    """

    cmd = f"vmm serial -t {name}-vBrackla_RE0"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    try:
        child = spawn()
        child = _junos_serial_login(child, name, spawn, debug=debug)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=PROMPT_CONFIG, timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password (interactive)
        _set_root_password(child)

        # --- Apply base config including DHCP on mgmt interface ---
        commands = [
            "delete groups",
            "delete apply-groups",
            "set system services ssh root-login allow",
            "set system services ssh sftp-server",
            "set system services netconf ssh",
            "set system management-instance",
            "set protocols lldp interface all",
            "set protocols lldp interface re0:mgmt-0 disable",
            "set chassis aggregated-devices ethernet device-count 10",
            "set interfaces re0:mgmt-0.0 family inet dhcp",
        ]
        for c in commands:
            send_and_expect(c)

        for iface in interfaces:
            desc_cmd = f"set interfaces {iface['name']} description \"{iface['description']}\""
            send_and_expect(desc_cmd)

        # --- First commit to apply base config ---
        send_and_expect("commit", timeout=120)
        time.sleep(35)  # adjust if needed based on lab speed
        if debug:
            print(f"[{name}] base config committed, waiting for mgmt interface to come up...")

        # --- Wait for DHCP to populate ---

        # Avoiding PR :1726785
        gw_ip = None
        max_gw_attempts = 25
        for attempt in range(1, max_gw_attempts + 1):
            time.sleep(5)
            child.sendline('run show dhcp client binding detail | match "Name: routers, Value: "')
            child.expect(PROMPT_CONFIG, timeout=30)
            output = child.before
            if debug:
                print(f"[{name}] DHCP binding output (attempt {attempt}):\n{output}")

            gw_match = re.search(r"Name: routers, Value: (\d+\.\d+\.\d+\.\d+)", output)
            if gw_match:
                gw_ip = gw_match.group(1)
                if debug:
                    print(f"[{name}] detected gateway: {gw_ip}")
                break

        # --- Configure static route if gateway found ---
        if gw_ip:
            send_and_expect(f"set routing-instances mgmt_junos routing-options static route 0/0 next-hop {gw_ip}")
            send_and_expect("commit", timeout=60)
        else:
            if debug:
                print(f"[{name}] DHCP gateway info not found, skipping static route configuration")

        # --- Exit config mode ---
        child.sendline("exit")
        child.close(force=True)

        return f"✅ Successfully configured {name}"

    except pexpect.exceptions.TIMEOUT:
        return f"Failure: {name} (Timeout)"
    except pexpect.exceptions.EOF:
        return f"Failure: {name} (Connection Closed)"
    except Exception as e:
        return f"Failure: {name} ({e})"




# -----------------------------
# Print Summary Table
# -----------------------------
def print_summary_table(topology_data):
    """Gathers final state of all VMs and prints a summary table."""
    try:
        result = subprocess.run(["vmm", "ping"], capture_output=True, text=True, check=False)
        ping_output = result.stdout.strip().splitlines()
    except FileNotFoundError:
        print("❌ 'vmm' command not found. Cannot generate summary.", file=sys.stderr)
        return

    ping_data = {}
    for line in ping_output:
        parts = line.split()
        if len(parts) >= 3:
            vm_name, ip, state = parts[0], parts[1], parts[2]
            ping_data[vm_name] = {"ip": ip, "state": state}

    columns = {
        "Name": 15,
        "Type": 10,
        "Image Path": 40,
        "State": 15,
        "IPv4 Address": 15
    }
    
    header_format = "| " + " | ".join([f"{{{key}:<{width}}}" for key, width in columns.items()]) + " |"
    row_format = "| " + " | ".join([f"{{{key}:<{width}}}" for key, width in columns.items()]) + " |"
    separator = "+" + "+".join(["-" * (width + 2) for width in columns.values()]) + "+"
    table_width = len(separator)

    print("\n" + "="*table_width)
    print(f" {topology_data.get('lab_name', 'Lab')} Deployment Summary ".center(table_width))
    print("="*table_width)
    print(separator)
    print(header_format.format(**{key: key for key in columns.keys()}))
    print(separator)

    for vm in topology_data.get('vms', []):
        name = vm.get('hostname', 'N/A')
        vm_type = vm.get('type', 'N/A')
        disk_alias = vm.get('disk', 'N/A')
        disks_map = topology_data.get('disks', {})
        image_path = disks_map.get(disk_alias, disk_alias)

        if len(image_path) > 40:
            image_display = "..." + image_path[-37:]
        else:
            image_display = image_path
        
        # The mgmt IP is reported against the RE component, whose name is
        # type-specific: '{name}_RE' for the vmx family, '{name}-re0' for the
        # vmm3 MX cosim (vbugatti/valfaromeo), '{name}_RE0' for the vmm3 EVO PTX
        # vmm3 EVO PTX types (including vbrackla). Everything else is keyed on
        # the plain hostname. Mirrors re_ping_name().
        lookup_name = (
        f"{name}_RE" if vm_type in ["vmx", "vferrari", "vhamilton", "vmaserati"]
        else f"{name}-re0" if vm_type in ["vbugatti", "valfaromeo"]
        else f"{name}_RE0" if vm_type in ["vscapa", "vbalerion", "vardbeg", "vbowmore", "vbrackla"]
        else name)
        status = ping_data.get(lookup_name, {})
        state = status.get('state', 'unknown')
        ip = status.get('ip', 'N/A')

        row_data = { "Name": name, "Type": vm_type, "Image Path": image_display, "State": state, "IPv4 Address": ip }
        print(row_format.format(**row_data))
           
    
    print(separator)
# -----------------------------
# Print Link Table
# -----------------------------
def print_capture_info_table(link_mappings):
    """List the lab's links and how to capture traffic on one of them."""
    if not link_mappings:
        return

    width = 64
    separator = "+" + "-" * (width + 2) + "+"
    table_width = len(separator)

    print("\n" + "=" * table_width)
    print(" Links ".center(table_width))
    print("=" * table_width)
    print(separator)
    print(f"| {'Link':<{width}} |")
    print(separator)
    for mapping in link_mappings:
        print(f"| {mapping['link'][:width]:<{width}} |")
    print(separator)
    # Every link is capturable now, so there is no per-link capture point to
    # print - the recording is done on the running VM, on demand.
    print(f"\n Capture any of them live, without redeploying:")
    print(f"   python3 {os.path.basename(sys.argv[0])} --capture DEVICE --to PEER")
    print(f"   ...or right-click the link in the builder (--build).\n")
# -----------------------------
# Configuration Management Functions
# -----------------------------
# Which 'vmm list' image strings mean "this is a Junos device". Two different
# shapes turn up on the pod and only the first was ever recognised:
#
#   1. a versioned filename someone pointed at by hand
#      /homes/<user>/images/junos-virtual-x86-64-22.4R3-S2.11.vmdk
#   2. the pod's blessed symlink for a profile
#      /vmm/data/base_disks/default_images/default_image_vhamilton.img
#
# Shape 2 carries no 'junos' anywhere in the path, so every device sitting on
# its default image was silently skipped by --config - and that is the common
# case: it is what the builder writes when the Image box is left empty, and
# what this script recommends. A lab with four REs would report two.
JUNOS_IMAGE_RE = re.compile(
    r'vJunos-router|vJunos-switch|junos-virtual-install|junos-x86|vqfx|vmx'
    r'|junos-evo-install|junos-virtual',
    re.IGNORECASE,
)

# Profiles that are not Junos and must never be picked up for config work.
NON_JUNOS_VM_TYPES = {'server', 'vswitch', 'vrouter'}

# Built from SUPPORTED_VM_TYPES rather than hard-coded, so a profile added
# there is recognised here for free instead of quietly going missing.
# 'default_image_vqfx10.img' is matched by the 'vqfx' stem.
JUNOS_DEFAULT_IMAGE_RE = re.compile(
    r'default_image_(?:'
    + '|'.join(sorted((re.escape(t) for t in SUPPORTED_VM_TYPES - NON_JUNOS_VM_TYPES),
                      key=len, reverse=True))
    + r')',
    re.IGNORECASE,
)


def get_junos_ips_from_vmm():
    """Retrieve active Junos device IPs from VMM by checking their image type."""
    print("⚙️  Finding active Junos devices in VMM...")
    try:
        list_result = subprocess.run(["vmm", "list"], capture_output=True, text=True, check=True)
        nodes = {}
        for line in list_result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                name = parts[0]
                image = " ".join(parts[3:])
                if "_FPC" in name or "pecosim" in name:
                    continue
                if JUNOS_IMAGE_RE.search(image) or JUNOS_DEFAULT_IMAGE_RE.search(image):
                    nodes[name] = {}

        if not nodes:
            print("⚠️  No Junos devices found based on 'vmm list'.")
            return []

        ip_result = subprocess.run(["vmm", "ip"], capture_output=True, text=True, check=True)
        for line in ip_result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                name, ip = parts
                if name in nodes:
                    nodes[name]["ip"] = ip

        ips = [info["ip"] for info in nodes.values() if "ip" in info]

        if not ips:
            print("⚠️  No IPs found for the Junos devices in the running lab.")
            return []

        # Name the devices. When this picked the wrong set the old message gave
        # a bare count, so there was no way to tell which ones were missed.
        named = ", ".join(f"{n} ({i['ip']})" for n, i in nodes.items() if "ip" in i)
        print(f"✅ Found {len(ips)} active devices: {named}")

        no_ip = [n for n, i in nodes.items() if "ip" not in i]
        if no_ip:
            print(f"⚠️  Skipping {', '.join(no_ip)} - Junos, but 'vmm ip' reports no address yet.")
        return ips

    except FileNotFoundError:
        print("❌ Error: 'vmm' command not found. Is it in your PATH?", file=sys.stderr)
        return []
    except subprocess.CalledProcessError:
        print("⚠️  No active lab found. Please start a lab first using 'python3 vmm.py -t <topology-file.yml>'.")
        return []

def display_folders():
    """Displays subdirectories for config selection."""
    print("\nAvailable configuration folders:")
    folders = [f for f in os.listdir() if os.path.isdir(f)]
    for folder in folders:
        print(f" - {folder}")

def get_config(ip, folder_name):
    """Fetches configuration from a device and saves it."""
    try:
        print(f"   - Connecting to {ip} to get config...")
        dev = Device(host=ip, user=DEVICE_ROOT_USER, passwd=DEVICE_ROOT_PASSWORD)
        dev.open()
        hostname = dev.facts['hostname']
        config = dev.rpc.get_config(options={'format': 'text'})
        file_path = os.path.join(folder_name, f"{hostname}.conf")
        with open(file_path, 'w') as f:
            f.write(config.text)
        dev.close()
        return f"✅ Configuration for {hostname} ({ip}) saved to {file_path}"
    except Exception as e:
        return f"❌ Failed to retrieve configuration from {ip}: {e}"

def push_config(ip, folder_name):
    """Pushes a configuration file to a device."""
    try:
        print(f"   - Connecting to {ip} to push config...")
        dev = Device(host=ip, user=DEVICE_ROOT_USER, passwd=DEVICE_ROOT_PASSWORD)
        dev.open()
        hostname = dev.facts['hostname']
        config_file = os.path.join(folder_name, f"{hostname}.conf")

        if not os.path.exists(config_file):
            return f"⚠️  Configuration file {config_file} not found for {hostname}."

        with Config(dev) as cu:
            cu.load(path=config_file, format="text", overwrite=True)
            cu.commit()
        dev.close()
        return f"✅ Configuration for {hostname} ({ip}) has been pushed and committed."
    except Exception as e:
        return f"❌ Failed to push configuration to {ip}: {e}"

def handle_config_management(topology_file):
    """Main logic for the --config flag."""
    device_ips = get_junos_ips_from_vmm()
    if not device_ips:
        return

    action = input("Do you want to 'get' or 'push' configurations? ").strip().lower()

    if action not in ['get', 'push']:
        print("Invalid action. Please choose 'get' or 'push'.")
        return

    if action == 'push':
        display_folders()

    folder_name = input("Enter the folder name for configurations: ").strip()

    if not folder_name:
        print("Folder name cannot be empty.")
        return

    if action == 'get' and not os.path.exists(folder_name):
        os.makedirs(folder_name)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        if action == 'get':
            for ip in device_ips:
                futures.append(executor.submit(get_config, ip, folder_name))
        elif action == 'push':
            for ip in device_ips:
                futures.append(executor.submit(push_config, ip, folder_name))

        for future in as_completed(futures):
            try:
                result = future.result()
                print(result)
            except Exception as exc:
                print(f"A task generated an exception: {exc}")

# Junos VM types that get a baseline pushed in Phase 4 (i.e. everything except
# 'server'). Used both to decide who to wait for in Phase 3 and who
# to configure in Phase 4.
CONFIGURABLE_TYPES = (
    'vrouter', 'vswitch', 'vmx', 'vferrari', 'valfaromeo',
    'vbugatti', 'vhamilton', 'vmaserati', 'vscapa', 'vbrackla', 'vqfx',
    'vbalerion', 'vardbeg', 'vbowmore',
)


def configurable_devices(topology_data):
    """Return [(hostname, vm_type), ...] for the VMs configured in Phase 4."""
    return [
        (vm['hostname'], vm['type'])
        for vm in topology_data.get('vms', [])
        if vm.get('type') in CONFIGURABLE_TYPES
    ]


# -----------------------------
# Per-device boot gate (vmm ping)
# -----------------------------
def get_vmm_ping_map():
    """Run 'vmm ping' and return {node_name: state} (e.g. {'PE1_RE': 'alive'}).
    Always returns quickly - a 'vmm ping' that hangs (common while devices are
    still booting) is bounded by a timeout so it can't freeze the caller."""
    try:
        result = subprocess.run(["vmm", "ping"], capture_output=True, text=True,
                                 check=False, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    ping_map = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            ping_map[parts[0]] = parts[2]
    return ping_map


def get_vmm_ip_map():
    """Run 'vmm ping' and return {node_name: ipv4}. Empty if 'vmm' is absent
    (e.g. generating a diagram off-pod before deployment) or the call times out."""
    try:
        result = subprocess.run(["vmm", "ping"], capture_output=True, text=True,
                                 check=False, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    ip_re = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
    ip_map = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and ip_re.match(parts[1]):
            ip_map[parts[0]] = parts[1]
    return ip_map


def get_lo0_address(ip):
    """
    SSH/NETCONF into a Junos device and return its lo0.0 primary inet address
    (the loopback), or '' if lo0.0 has no user address yet, the device is
    unreachable, or credentials don't match. Best-effort and fast-failing
    (auto_probe) so it never blocks diagram generation for long.
    """
    if not ip:
        return ''
    dev = None
    try:
        dev = Device(host=ip, user=DEVICE_ROOT_USER, passwd=DEVICE_ROOT_PASSWORD,
                     timeout=10, auto_probe=5)
        dev.open()
        rsp = dev.rpc.get_interface_information(interface_name='lo0.0', terse=True)
        for af in rsp.findall('.//address-family'):
            if (af.findtext('address-family-name') or '').strip() != 'inet':
                continue
            for ifa in af.findall('interface-address/ifa-local'):
                a = (ifa.text or '').strip().split('/')[0]
                # skip Junos-internal loopback addresses (127.x, 128.0.0.x)
                if a and not a.startswith('127.') and not a.startswith('128.0.0.'):
                    return a
        return ''
    except Exception:
        return ''
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass


def annotate_lo0(nodes):
    """Fill each Junos node's 'lo0' from the live device (parallel, best effort)."""
    junos = [n for n in nodes if n.get('ip') and n['type'] in CONFIGURABLE_TYPES]
    results = {}
    if junos:
        with ThreadPoolExecutor(max_workers=min(8, len(junos))) as ex:
            futs = {ex.submit(get_lo0_address, n['ip']): n['id'] for n in junos}
            for f in as_completed(futs):
                try:
                    results[futs[f]] = f.result()
                except Exception:
                    results[futs[f]] = ''
    for n in nodes:
        n['lo0'] = results.get(n['id'], '')


def re_ping_name(host, vtype):
    """The name a device's RE appears under in 'vmm ping' (see the RE naming in
    lab_template.j2 and print_summary_table)."""
    if vtype in ('vmx', 'vferrari', 'vhamilton', 'vmaserati'):
        return f"{host}_RE"
    if vtype in ('vbugatti', 'valfaromeo'):
        # template: VMX304_RE_START(<hostname>-re0, 0) /
        #           VMX10008_RE_START(<hostname>-re0, 0)
        return f"{host}-re0"
    if vtype in ('vscapa', 'vbalerion', 'vardbeg', 'vbowmore', 'vbrackla'):
        # vmm3 EVO REs appear as '<hostname>_RE0' (e.g. vbrackla_RE0).
        return f"{host}_RE0"
    return host


# -----------------------------
# Interface cheat sheet (--interfaces)
# -----------------------------
# Human-readable valid-interface summary per VM type. Mirrors the rules
# enforced in validate_topology() so an engineer never has to remember which
# prefix (et-/ge-/xe-) a given device uses.
INTERFACE_HELP = {
    'server':     "em1, em2, ...            (data ports; em0 is management)",
    'vswitch':    "ge-0/0/0, ge-0/0/1, ...  (sequential from 0)",
    'vrouter':    "ge-0/0/0, ge-0/0/1, ...  (sequential from 0)",
    'vqfx':       "xe-0/0/0, xe-0/0/1, ...  (sequential from 0)",
    'vmx':        "ge-0/0/0-9, ge-0/1/0-9, xe-0/2/0-1, xe-0/3/0-1, "
                  "xe-1/0/0-5:0-3, xe-2/0/0-5:0-3, et-3/0/0-5, xe-5/0/0-11   (any subset)",
    'vferrari':   "et-0/0/0 .. et-0/0/4      (any subset)",
    'vbugatti':   "et-0/0/0 .. et-0/0/15     (any subset)",
    'vhamilton':  "et-<0-2>/0/0 .. et-<0-2>/0/13  (any subset; et-1/... and et-2/... add FPC1/FPC2)",
    'vmaserati':  "et-<0-2>/0/0 .. et-<0-2>/0/19 and et-<0-2>/1/0 .. et-<0-2>/1/15  (2 pics, 36 ports; et-1/... and et-2/... add FPC1/FPC2)",
    'valfaromeo': "et-<0-2>/0/<0-3>:<0-3>    (any subset; et-1/... and et-2/... ports add a 2nd/3rd linecard)",
    'vscapa':     "et-0/0/1,3,5,7,9,11,13,15  (ODD ports only; vmm3 EVO PTX)",
    'vbrackla':   "et-1/0/0 .. et-1/0/4     (any subset; vmm3 EVO PTX, FPC1)",
    'vbalerion':  "et-0/0/9 .. et-0/0/26     (any subset; vmm3 EVO PTX)",
    'vardbeg':    "et-0/0/0 .. et-0/0/11     (any subset; vmm3 EVO PTX)",
    'vbowmore':   "et-0/0/1,3,5,7,9,11,13,15  (ODD ports only; vmm3 EVO PTX)",
}


def _load_topology(topology_file):
    """Load and return the raw topology dict, exiting cleanly on error."""
    try:
        with open(topology_file) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"❌ Error: Topology file not found at '{topology_file}'", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"❌ Error: Could not parse YAML file '{topology_file}'. Error: {e}", file=sys.stderr)
        sys.exit(1)


def print_interfaces(topology_file):
    """Print the valid interfaces for every device in the topology."""
    data = _load_topology(topology_file)
    vms = data.get('vms', [])
    print(f"\nInterfaces for lab '{data.get('lab_name', '?')}' ({topology_file}):\n")
    width = max((len(vm.get('hostname', '')) for vm in vms), default=4)
    for vm in vms:
        host = vm.get('hostname', '?')
        vtype = vm.get('type', '?')
        help_text = INTERFACE_HELP.get(vtype, "(unknown type)")
        print(f"  {host:<{width}}  {vtype:<11} {help_text}")
    print("\nWrite links as 'hostname:interface', e.g. "
          f"[\"{vms[0]['hostname']}:...\", \"...\"]" if vms else "")


def print_devices(topology_file):
    """Print every Junos device as a junos-mcp-server style devices.json map:

        {
          "<hostname>": {
            "ip": "<mgmt ip>",
            "port": 22,
            "username": "root",
            "auth": {"type": "password", "password": "..."}
          },
          ...
        }

    IPs are the live management addresses from 'vmm ping' (empty if the device
    is not up yet). Only Junos devices are emitted - Linux 'server' VMs
    are not managed over SSH with these credentials. The JSON is written to
    stdout so it can be redirected straight into a file:

        python3 vmm.py -t topo.yml --print_devices > devices.json
    """
    import json
    data = _load_topology(topology_file)
    ip_map = get_vmm_ip_map()
    devices = {}
    for vm in data.get('vms', []):
        vtype = vm.get('type')
        if vtype not in CONFIGURABLE_TYPES:
            continue  # 'server' is a Linux host, not Junos over SSH
        host = vm['hostname']
        devices[host] = {
            "ip": ip_map.get(re_ping_name(host, vtype), ""),
            "port": 22,
            "username": DEVICE_ROOT_USER,
            "auth": {
                "type": "password",
                "password": DEVICE_ROOT_PASSWORD,
            },
        }
    print(json.dumps(devices, indent=2))


# -----------------------------
# Interactive topology diagram (--diagram)
# -----------------------------
def _build_topology_html(topology_file):
    """Build the interactive diagram HTML from the topology, embedding current
    mgmt IPs (best effort from 'vmm ping'). Returns (html, n_nodes, n_edges,
    n_with_ip)."""
    import json
    data = _load_topology(topology_file)

    vms = data.get('vms', [])
    known = {vm['hostname'] for vm in vms}

    ip_map = get_vmm_ip_map()
    nodes = []
    for vm in vms:
        host = vm['hostname']
        ip = ip_map.get(re_ping_name(host, vm.get('type')), '')
        nodes.append({'id': host, 'type': vm.get('type', '?'), 'ip': ip})

    # Best-effort: pull each reachable Junos device's lo0.0 loopback address
    # over SSH so it can be shown above the icon (blank if not configured).
    annotate_lo0(nodes)

    edges = []
    for link in data.get('links', []):
        eps = link.get('endpoints', [])
        if len(eps) != 2:
            continue
        try:
            a, ai = eps[0].split(':', 1)
            b, bi = eps[1].split(':', 1)
        except ValueError:
            continue
        if a in known and b in known:
            edges.append({'a': a, 'ai': ai, 'b': b, 'bi': bi})

    html = (_TOPOLOGY_HTML
            .replace("__LAB__", json.dumps(data.get('lab_name', 'lab')))
            .replace("__NODES__", json.dumps(nodes))
            .replace("__EDGES__", json.dumps(edges)))
    return (html, len(nodes), len(edges),
            sum(1 for n in nodes if n['ip']),
            sum(1 for n in nodes if n.get('lo0')))


def generate_topology_diagram(topology_file, out_path):
    """
    Render a self-contained, interactive HTML diagram of the topology (one
    draggable box per device, every link labelled with the interface at both
    ends, mgmt IPs shown, add text/zone annotations, export PNG/SVG). No
    internet/CDN needed. Layout + annotations persist in the browser per lab.
    """
    html, nn, ne, hi, lo = _build_topology_html(topology_file)
    with open(out_path, "w") as f:
        f.write(html)
    ip_note = f", {hi} with mgmt IPs" if hi else " (no IPs yet - run after deploy to show them)"
    lo_note = f", {lo} with lo0 loopbacks" if lo else ""
    print(f"✅ Topology diagram written to '{out_path}' "
          f"({nn} devices, {ne} links{ip_note}{lo_note}). Open it in a browser: "
          f"drag devices, add text/zones, export PNG/SVG.")


def qpod_ip():
    """Best-effort primary IPv4 of this QPOD (the address a browser would use
    to reach it)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # no packets sent; just resolves the route's source IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# -----------------------------
# Port housekeeping
# -----------------------------
# 'Address already in use' on a pod host is nearly always a server the user
# forgot to Ctrl+C - a builder in a closed tab, or one orphaned when an ssh
# session dropped. The old advice was to go find it by hand with ss/lsof and
# kill it, which is three commands and assumes the PID is even visible: 'ss
# -ltnp' only reveals pid/cmd for YOUR OWN processes, so another user's server
# on the same port appears as a listener with no owner at all. That is the case
# that looks like "the port is stuck and there is nothing to kill".
#
# Our own servers are identified by their command line rather than by a pid
# file: --serve writes one, but --build never did, and a pid file is stale the
# moment a process dies in a way that skips its cleanup.
_OUR_SERVER_MARKERS = ("vmm.py", "vmm_builder")


def _proc_cmdline(pid):
    """Full command line of <pid>, or '' when it cannot be read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def port_listeners(port):
    """
    Who is listening on <port>?

    Returns a list of {'pid', 'user', 'cmd', 'ours'} dicts - empty when the
    port is free. 'pid' is None when the owner is another user, because the
    kernel hides it; 'ours' means the process is one of this project's own
    servers and is therefore safe to stop.

    lsof and ss are both consulted: lsof names the user, ss sees listeners lsof
    may miss, and on a host where only one of the two is installed the other
    still answers.
    """
    found = {}

    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.splitlines()[1:]:            # first line is the header
            parts = line.split()
            if len(parts) >= 3 and parts[1].isdigit():
                pid = int(parts[1])
                found[pid] = {"pid": pid, "user": parts[2], "cmd": _proc_cmdline(pid) or parts[0]}
    except Exception:
        pass

    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            # Local Address:Port is the 4th column: 0.0.0.0:5057, [::]:5057, *:5057
            cols = line.split()
            if len(cols) < 4 or not re.search(rf":{port}$", cols[3]):
                continue
            m = re.search(r"pid=(\d+)", line)
            if m:
                pid = int(m.group(1))
                found.setdefault(pid, {"pid": pid, "user": None,
                                       "cmd": _proc_cmdline(pid) or line})
            elif not found:
                # A listener whose owner the kernel will not reveal: another
                # user. Recorded so the caller can say so instead of insisting
                # the port is free.
                found.setdefault(None, {"pid": None, "user": None, "cmd": None})
    except Exception:
        pass

    result = []
    for info in found.values():
        cmd = info.get("cmd") or ""
        info["ours"] = any(mark in cmd for mark in _OUR_SERVER_MARKERS)
        result.append(info)
    return result


def port_is_free(port, host="0.0.0.0"):
    """True when <port> can actually be bound right now - the only test that
    matters, since it is what the server itself is about to attempt."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_probe(port, host="127.0.0.1", timeout=4.0):
    """Does something actually answer HTTP on <port>?

    Returns (status, detail). A socket can be listening while the server behind
    it is wedged, half-dead, or not an HTTP server at all - which looks exactly
    like 'the page will not load' from a browser, so the two are reported apart
    rather than assuming a listener means a working page.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            # GET, not HEAD: plenty of small servers (this project's builder
            # included) answer HEAD with 501, which reads like a fault when the
            # server is in fact perfectly healthy. Only the status line is read.
            s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            s.settimeout(timeout)
            data = b""
            while b"\r\n" not in data and len(data) < 256:
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
    except socket.timeout:
        return "hung", "connected, but no HTTP reply - the server is wedged"
    except ConnectionRefusedError:
        return "closed", "nothing accepted the connection"
    except OSError as exc:
        return "closed", str(exc)

    first = data.split(b"\r\n", 1)[0].decode("utf-8", "replace").strip()
    if first.startswith("HTTP/"):
        code = first.split()[1] if len(first.split()) > 1 else "?"
        return "ok", f"answering (HTTP {code})"
    if not first:
        return "hung", "connected, but the server said nothing"
    return "notHttp", f"answered, but not with HTTP: {first[:60]}"


def server_status(ports=None):
    """Report every web server of ours that is up, and whether it really works.

    Answers 'is a server running, and why is the page not loading?' without
    stopping anything - --stop-port reports the same facts but kills as it goes,
    which is no use when all you wanted was to look.
    """
    seen = {}
    for port in (ports or []):
        seen[port] = port_listeners(port)

    if not ports:
        # No port named: find our own servers wherever they happen to be, since
        # the whole point is usually 'I forgot which port I used'.
        for pid, cmd in _our_server_processes():
            for port in _ports_of_pid(pid) or _ports_in_cmdline(cmd):
                seen.setdefault(port, port_listeners(port))
        for port in (8080, 8081):
            seen.setdefault(port, port_listeners(port))

    rows = []
    for port in sorted(seen):
        holders = seen[port]
        if not holders:
            continue
        state, detail = http_probe(port)
        for h in holders:
            rows.append({"port": port, "state": state, "detail": detail, **h})
    return rows


def _our_server_processes():
    """[(pid, cmdline)] for this project's servers running as this user."""
    out = []
    try:
        listing = subprocess.run(["ps", "-eo", "pid=,args="],
                                 capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return out
    for line in listing.splitlines():
        line = line.strip()
        pid, _, cmd = line.partition(" ")
        if not pid.isdigit() or "ps -eo" in cmd:
            continue
        if not any(mark in cmd for mark in _OUR_SERVER_MARKERS):
            continue
        if not any(flag in cmd for flag in ("--build", "--serve", "serve_builder",
                                            "vmm_builder")):
            continue
        out.append((int(pid), cmd))
    return out


def _ports_of_pid(pid):
    """TCP ports <pid> is listening on, read from ss."""
    ports = set()
    try:
        listing = subprocess.run(["ss", "-ltnp"], capture_output=True,
                                 text=True, timeout=15).stdout
    except Exception:
        return ports
    for line in listing.splitlines():
        if f"pid={pid}," not in line and f"pid={pid})" not in line:
            continue
        cols = line.split()
        if len(cols) >= 4:
            m = re.search(r":(\d+)$", cols[3])
            if m:
                ports.add(int(m.group(1)))
    return ports


def _ports_in_cmdline(cmd):
    """Ports named on a server's own command line - the fallback when the
    kernel will not tell us what it bound (it happens under some containers)."""
    ports = set()
    for m in re.finditer(r"--(?:build-)?port[= ](\d+)", cmd or ""):
        ports.add(int(m.group(1)))
    return ports


def print_server_status(ports=None):
    """Human-readable 'what is serving right now'. Returns a shell exit code."""
    rows = server_status(ports)
    if not rows:
        if ports:
            print(f"\nNothing is listening on {', '.join(str(p) for p in ports)}.\n")
        else:
            print("\nNo web server of this script's is running.\n")
            print("  Start one with:")
            print(f"    python3 {os.path.basename(__file__)} --build      "
                  f"# topology builder  (8081)")
            print(f"    python3 {os.path.basename(__file__)} --serve      "
                  f"# topology diagram  (8080)\n")
        return 1

    host = ""
    try:
        import socket as _socket
        host = _socket.gethostname()
    except Exception:
        pass

    print("\nWeb servers:\n")
    for r in rows:
        mark = {"ok": "✅", "hung": "⚠️ ", "notHttp": "⚠️ ",
                "closed": "❌"}.get(r["state"], "•")
        who = ("your own server" if r["ours"]
               else f"another process ({r['user'] or 'unknown user'})"
               if r["pid"] else "another user (the kernel hides the pid)")
        print(f"  {mark} port {r['port']} - {who}")
        if r["pid"]:
            print(f"       pid {r['pid']}: {(r['cmd'] or '')[:90]}")
        print(f"       {r['detail']}")
        if r["state"] == "ok":
            print(f"       open: http://{host or 'localhost'}:{r['port']}/")
        elif r["ours"] and r["pid"]:
            # A listening socket that will not serve is the confusing case: the
            # port looks taken, so a restart refuses to bind, but nothing loads.
            print(f"       free it with: python3 "
                  f"{os.path.basename(__file__)} --stop-port {r['port']}")
        else:
            # --stop-port deliberately refuses to kill anything that is not
            # ours, so pointing at it here would just waste a command.
            print(f"       not this script's, so --stop-port will not touch it "
                  f"- use another port (--port N)")
        print()
    return 0


def describe_port_conflict(port, indent="   "):
    """Explain who is holding <port>, and what can be done about it.

    Every caller reaches this on an error path - a refused bind, or a
    --stop-port that found nothing of ours - so this is the message that has to
    tell the three cases apart: an orphan of our own (stoppable), another
    process of this user's (named, but left alone), and another user's (the
    kernel hides the pid, so there is nothing to kill and the answer is simply
    a different port).
    """
    holders = port_listeners(port)
    if not holders:
        return (f"{indent}Nothing is listening on {port} now - the port may have been\n"
                f"{indent}released as you looked, or is held in another network namespace.\n"
                f"{indent}Try again, or use another port with --port <N>.")

    lines = []
    ours = [h for h in holders if h["ours"]]
    mine = [h for h in holders if h["pid"] and not h["ours"]]
    theirs = [h for h in holders if not h["pid"]]

    for h in ours:
        lines.append(f"{indent}Port {port} is held by your own server, pid {h['pid']}:")
        lines.append(f"{indent}    {h['cmd'][:100]}")
        lines.append(f"{indent}Close it and reuse the port with:")
        lines.append(f"{indent}    python3 {os.path.basename(__file__)} --stop-port {port}")
    for h in mine:
        lines.append(f"{indent}Port {port} is held by pid {h['pid']}, which is not one of this")
        lines.append(f"{indent}script's servers, so it will not be touched automatically:")
        lines.append(f"{indent}    {h['cmd'][:100]}")
        lines.append(f"{indent}Stop it yourself with 'kill {h['pid']}', or use another port.")
    for _ in theirs:
        lines.append(f"{indent}Port {port} is held by ANOTHER USER on this pod - the kernel hides")
        lines.append(f"{indent}their pid, which is why nothing shows up to kill. Pick another")
        lines.append(f"{indent}port with --port <N>; pod hosts are shared.")
    return "\n".join(lines)


def _terminate_pids(targets, port, timeout=8.0):
    """SIGTERM each target, escalating to SIGKILL only if it is ignored."""
    import signal

    for h in targets:
        pid = h["pid"]
        print(f"⏳ Stopping pid {pid} on port {port}: {h['cmd'][:90]}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f"❌ Not allowed to stop pid {pid} - it belongs to another user.", file=sys.stderr)
            continue

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            # Still there: a wedged server would otherwise hold the port forever.
            print(f"   pid {pid} ignored SIGTERM after {timeout:.0f}s - sending SIGKILL.")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.5)

    # Confirm against a real bind rather than trusting the kill: that is the
    # only thing that proves the next server will actually start.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if port_is_free(port):
            return True
        time.sleep(0.25)
    return False


def reclaim_port(port):
    """
    Take <port> back from an earlier server of ours so a new one can start.

    This is what makes 'resume the lab on the same port' a single command: an
    orphaned builder - one whose ssh session dropped, or that was left running
    in a closed tab - is stopped automatically instead of turning into an
    'Address already in use' the user has to go and clear by hand.

    Only our own servers, owned by this user, are ever taken; anything else
    returns False so the caller can explain and leave it alone. Returns True
    when the port is free to bind.
    """
    targets = [h for h in port_listeners(port) if h["ours"] and h["pid"]]
    if not targets:
        return False

    print(f"♻️  Port {port} was still held by an earlier builder of yours - reclaiming it.")
    if _terminate_pids(targets, port):
        print(f"✅ Port {port} reclaimed.")
        return True
    return False


def stop_port(port, timeout=8.0):
    """
    Close this project's server on <port> so the port can be reused.

    Only our own servers are stopped, and only ones this user owns: anything
    else is reported and left alone rather than killed on a shared host. SIGTERM
    first so the server can shut its socket down cleanly, SIGKILL only if it
    ignores that. Returns True when the port ends up free.
    """
    if port_is_free(port) and not port_listeners(port):
        print(f"✅ Port {port} is already free.")
        return True

    holders = port_listeners(port)
    targets = [h for h in holders if h["ours"] and h["pid"]]

    if not targets:
        print(f"❌ Nothing of this script's is listening on port {port}.", file=sys.stderr)
        print(describe_port_conflict(port), file=sys.stderr)
        return False

    if _terminate_pids(targets, port, timeout):
        print(f"✅ Port {port} is free again - you can start on it now.")
        return True

    print(f"❌ Port {port} is still busy.", file=sys.stderr)
    print(describe_port_conflict(port), file=sys.stderr)
    return False


# =============================================================================
#  Packet capture
# =============================================================================
# Traffic is captured by asking each VM's own QEMU process to copy every frame
# crossing one of its virtual NICs into a .pcap ('filter-dump'). It is added and
# removed live over QEMU's monitor, so a running lab needs no redeploy and no
# sniffer VM.
#
# The obvious alternative - attaching a listener to the VDE switch that carries
# the link - does not work: vde_switch does MAC learning, so a late-joining port
# only ever receives broadcast. Measured on a busy link: 0 frames that way,
# 2,070 frames via filter-dump over the same 15 seconds.

_CAP_PREFIX = "vmmcap_"
# QEMU buffers a filter-dump and only flushes it when the object is deleted, so
# a single long capture shows nothing at all until it is stopped. A second,
# short-lived dump on the same NIC is therefore rotated every few seconds purely
# to feed the live packet list; the continuous one stays untouched so the file
# offered for download has no gaps in it.
_PRE_PREFIX = "vmmcapv_"

# The builder serves every request on its own thread and runs a capture watchdog
# alongside them, so several of them can want the same VM's monitor at once. A
# QEMU monitor is a single conversational session - two overlapping talkers get
# each other's replies - so calls are serialised per VM.
_MON_LOCKS = {}
_MON_LOCKS_GUARD = threading.Lock()
# 'vmm monitor' shells out and costs seconds, but a running VM's monitor
# endpoint never moves. Caching it keeps the rotate gap short; a VM that
# restarts gets a different port, so a failed connect drops the entry and retries.
_MON_ENDPOINTS = {}


def _monitor_lock(vm):
    with _MON_LOCKS_GUARD:
        lock = _MON_LOCKS.get(vm)
        if lock is None:
            lock = _MON_LOCKS[vm] = threading.Lock()
        return lock


# Starting several captures at once means several threads asking "what is in
# this lab?" at the same moment, and the vmm wrapper does not survive that -
# measured, three of six concurrent 'vmm ls' calls came back empty, which reads
# as "the lab is down". One caller does the work under the lock and the rest
# take its answer, so the shell-outs are never stampeded.
_LAB_CACHE = {}
_LAB_CACHE_LOCK = threading.Lock()
_LAB_CACHE_TTL = 8.0


def _lab_cached(key, produce, ttl=_LAB_CACHE_TTL):
    with _LAB_CACHE_LOCK:
        hit = _LAB_CACHE.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        value = produce()
        # An empty inventory is more likely a hiccup than the truth, and
        # remembering it would keep the wrong answer alive for the whole TTL.
        if value:
            _LAB_CACHE[key] = (time.time(), value)
        return value


def lab_cache_clear():
    """Forget the lab inventory - call after anything binds or unbinds VMs."""
    with _LAB_CACHE_LOCK:
        _LAB_CACHE.clear()


def _monitor_endpoint(vm, refresh=False):
    if not refresh:
        cached = _MON_ENDPOINTS.get(vm)
        if cached:
            return cached
    # 'vmm monitor' is another shell-out that comes back blank when several
    # captures start at once. Believing it the first time reports a running VM
    # as "not in this lab", so a blank answer is retried before it is trusted.
    for attempt in (0, 1, 2):
        _, out = _run_vmm(["monitor", vm], timeout=30)
        parts = (out or "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            endpoint = (parts[0], int(parts[1]))
            _MON_ENDPOINTS[vm] = endpoint
            return endpoint
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    _MON_ENDPOINTS.pop(vm, None)
    raise RuntimeError(f"'{vm}' is not a running VM in this lab.")


def _qemu_monitor(vm, *cmds, timeout=15):
    """Run HMP commands on a VM's QEMU monitor and return the combined output.

    Raises RuntimeError if the VM is not running or the monitor is unreachable.
    """
    import socket

    with _monitor_lock(vm):
        sock = None
        for attempt in (0, 1):
            host, port = _monitor_endpoint(vm, refresh=bool(attempt))
            try:
                sock = socket.create_connection((host, port), timeout=timeout)
                break
            except OSError as exc:
                _MON_ENDPOINTS.pop(vm, None)
                if attempt:
                    raise RuntimeError(f"cannot reach {vm}'s QEMU monitor "
                                       f"at {host}:{port} ({exc})") from exc
        sock.settimeout(4)

        def drain(first_wait=2.5, idle=0.35, overall=8.0):
            """Read until the monitor goes quiet.

            There is no end-of-reply marker, so the only signal is a pause. Wait
            a while for the first byte, then treat a short gap as 'done' - a flat
            multi-second timeout per read made every capture call take ~9s, most
            of it spent waiting on a socket that had already said everything.
            """
            buf = b""
            deadline = time.time() + overall
            while time.time() < deadline:
                sock.settimeout(first_wait if not buf else idle)
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
            return buf.decode("utf-8", "replace")

        try:
            drain()  # banner
            chunks = []
            for cmd in cmds:
                sock.sendall((cmd + "\n").encode())
                # The monitor echoes every keystroke, so the reply arrives with
                # the command smeared through it ("iininfinfo..."). Everything
                # after the last echo of the command is the real answer.
                text = drain()
                idx = text.rfind(cmd)
                chunks.append(text[idx + len(cmd):] if idx >= 0 else text)
            return "\n".join(chunks)
        finally:
            sock.close()


def running_vms():
    """Hostnames of the VMs currently running in this lab.

    'vmm ls' prints tab-separated '<name>\tRunning\t<disk>\t...'. Anything that
    does not look like that is a warning or an error the wrapper decided to
    print, and treating it as a VM name turns a stray line into a phantom
    device we would then try to open a QEMU monitor for.
    """
    return _lab_cached("running_vms", _running_vms_uncached)


def _running_vms_uncached():
    # Measured: six concurrent 'vmm ls' calls, three of them came back empty.
    # An empty answer is indistinguishable from "the lab is down", which is a
    # very confusing thing to tell someone whose lab is plainly up, so a blank
    # result is retried once before it is believed.
    for attempt in (0, 1):
        _, out = _run_vmm(["ls"], timeout=60)
        vms = []
        for line in (out or "").splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            name, state = fields[0].strip(), fields[1].strip()
            if not name or "/" in name or " " in name:
                continue
            if state.lower() not in ("running", "paused"):
                continue
            vms.append(name)
        if vms or attempt:
            return vms
        time.sleep(0.6)
    return []


def vm_netdevs(vm):
    """[(netdev, switch_id)] for every VDE link on this VM, in QEMU order."""
    text = _qemu_monitor(vm, "info network")
    return [(m.group(1), m.group(2)) for m in
            re.finditer(r"(netdev\d+):.*?/vde_switches/(\d+)", text)]


def lab_switch_owners(vms=None):
    """{switch_id: [vm, ...]} for the whole lab, read from the QEMU cmdlines.

    Cheaper and more reliable than opening a monitor session per VM, and it is
    what turns an anonymous switch id into 'the link between A and B'.

    Cached on the VM set, not just on "no argument given": callers routinely
    pass the list they already have, and that used to skip the cache and fire
    one 'vmm cmdline' per VM per caller. Three captures starting together made
    that dozens of concurrent shell-outs, some of which came back empty - which
    surfaced as "A and B are not wired together" on a lab that plainly was.
    """
    vms = vms if vms is not None else running_vms()
    key = "switch_owners:" + ",".join(sorted(vms))
    return _lab_cached(key, lambda: _switch_owners_uncached(vms))


def _switch_owners_uncached(vms):
    owners = {}
    for vm in vms:
        _, text = _run_vmm(["cmdline", vm], timeout=60)
        for sw in re.findall(r"vde_switches/(\d+)", text or ""):
            owners.setdefault(sw, [])
            if vm not in owners[sw]:
                owners[sw].append(vm)
    return owners


# A pair of devices is often wired together more than once, and "capture the
# link between A and B" is then ambiguous - picking the first one silently
# records the wrong wire. The topology names the interface, so the interface is
# what has to be resolved, and these two lookups make that exact:
#
#   'vmm config_print'  interface -> bridge name   (VJUNOS_CONNECT(GE(0,0,2), br))
#   'vmm vde'           bridge name -> switch id   ("br": /vde_switches/9723/mgt)
#   QEMU cmdline        switch id   -> netdev      (already in lab_switch_owners)
#
# It needs no login to the device and does not assume netdevs are numbered in
# interface order, which is false on any multi-FPC chassis.
def lab_bridge_switches():
    """{bridge_name: switch_id} for the bound lab."""
    return _lab_cached("bridge_switches", _bridge_switches_uncached)


def _bridge_switches_uncached():
    _, out = _run_vmm(["vde"], timeout=60)
    found = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*\S*/vde_switches/(\d+)', out or ""):
        found[m.group(1)] = m.group(2)
    return found


_IFACE_KIND = {"GE": "ge", "XE": "xe", "ET": "et"}


def _iface_from_macro(macro, args):
    """'VJUNOS_GE', '0,0,2' -> 'ge-0/0/2'; a 4th number is a channel (':0')."""
    kind = ""
    for token in macro.split("_"):
        if token in _IFACE_KIND:
            kind = _IFACE_KIND[token]
    if not kind:
        return ""
    nums = [n.strip() for n in args.split(",") if n.strip()]
    if len(nums) == 3:
        return f"{kind}-{nums[0]}/{nums[1]}/{nums[2]}"
    if len(nums) == 4:
        return f"{kind}-{nums[0]}/{nums[1]}/{nums[2]}:{nums[3]}"
    return ""


def lab_interface_bridges():
    """{(hostname, interface): bridge_name} for the bound lab."""
    return _lab_cached("iface_bridges", _interface_bridges_uncached)


def _interface_bridges_uncached():
    _, text = _run_vmm(["config_print"], timeout=60)
    mapping = {}
    host = ""
    for line in (text or "").splitlines():
        chassis = re.search(r"#define\s+\w*CHASSIS_NAME\s+(\S+)", line)
        if chassis:
            host = chassis.group(1)
            continue
        conn = re.search(r"\w*CONNECT\(\s*([A-Za-z0-9_]+)\(([^)]*)\)\s*,\s*"
                         r"([A-Za-z0-9_]+)\s*\)", line)
        if conn and host:
            iface = _iface_from_macro(conn.group(1), conn.group(2))
            if iface:
                mapping[(host, iface)] = conn.group(3)
    return mapping


def switch_for_interface(host, iface):
    """The VDE switch id carrying <host>'s <iface>, or '' if not known."""
    if not host or not iface:
        return ""
    try:
        bridge = lab_interface_bridges().get((host, iface))
        if not bridge:
            return ""
        return lab_bridge_switches().get(bridge, "")
    except Exception:
        return ""


def vm_links(vm, owners=None):
    """[{'netdev','switch','peers'}] - who is on the far end of each link."""
    if owners is None:
        owners = lab_switch_owners()
    rows = []
    for netdev, switch in vm_netdevs(vm):
        rows.append({'netdev': netdev, 'switch': switch,
                     'peers': [p for p in owners.get(switch, []) if p != vm]})
    return rows


def vms_for_host(host, running=None):
    """Every running VM belonging to a topology hostname.

    An MX is several VMs - 'R1_RE', 'R1_FPC0' - and the revenue ports live on
    the FPCs, so a topology name can map to more than one QEMU process.
    """
    running = running if running is not None else running_vms()
    exact = [v for v in running if v == host]
    return exact or [v for v in running
                     if v.startswith(host + "_") or v.startswith(host + "-")]


def resolve_capture(host_a, host_b, running=None, owners=None,
                    port_a=None, port_b=None):
    """Find the NIC(s) to capture on for the link between two topology hosts.

    Returns [{'vm','netdev','switch','peer'}]. When the interface names are
    known the answer is exactly one entry - the wire the user actually pointed
    at. Without them the best that can be done is "a link between these two",
    which is ambiguous as soon as the pair is cabled together twice.
    """
    running = running if running is not None else running_vms()
    a_vms = vms_for_host(host_a, running)
    b_vms = vms_for_host(host_b, running)
    if not a_vms or not b_vms:
        return []
    owners = owners if owners is not None else lab_switch_owners(running)

    # Prefer the interface the topology named. Either end identifies the wire,
    # so B's interface is tried too - and A is still the side recorded, since
    # that is the host the caller named first.
    want = (switch_for_interface(host_a, port_a) or
            switch_for_interface(host_b, port_b))

    found = []
    failed = []
    for vm in a_vms:
        try:
            netdevs = vm_netdevs(vm)
        except RuntimeError as exc:
            # Swallowing this silently reports a wiring problem that does not
            # exist - the link is there, we just could not read the VM.
            failed.append(f"{vm} ({exc})")
            continue
        for netdev, switch in netdevs:
            if want and switch != want:
                continue
            peers = [p for p in owners.get(switch, []) if p != vm]
            hit = [p for p in peers if p in b_vms]
            if hit:
                found.append({'vm': vm, 'netdev': netdev,
                              'switch': switch, 'peer': hit[0]})
    if found:
        return found
    if failed:
        raise RuntimeError("could not read the interfaces of " +
                           ", ".join(failed))
    if want:
        # The interface resolved to a switch but no NIC of A's sits on it, so
        # falling back to "any link between these two" would quietly record a
        # different wire than the one asked for.
        return []
    return found


def capture_dir(vm):
    """A directory QEMU can actually write a .pcap into.

    QEMU runs as root on the compute node and NFS squashes root, so anything
    under a home directory fails with EPERM. Each VM's log directory is
    world-writable and reachable from every pod host, which is what is needed.

    Cached: it never moves while the VM is up, and 'vmm debuglog' is another
    shell-out that comes back blank when two captures start at the same moment.
    """
    return _lab_cached("capture_dir:" + vm, lambda: _capture_dir_uncached(vm))


def _capture_dir_uncached(vm):
    for attempt in (0, 1):
        _, log = _run_vmm(["debuglog", vm], timeout=30)
        for line in (log or "").splitlines():
            line = line.strip()
            if line.startswith("/") and "/uuids/" in line:
                directory = os.path.dirname(line)
                if os.path.isdir(directory):
                    return directory
        if not attempt:
            time.sleep(0.6)
    raise RuntimeError(f"no writable log directory for '{vm}'")


def capture_active(vm):
    """{netdev: pcap_path} for captures currently running on this VM."""
    try:
        text = _qemu_monitor(vm, "info qom-tree /objects")
    except RuntimeError:
        return {}
    live = {}
    for name in re.findall(_CAP_PREFIX + r"(netdev\d+)", text):
        live[name] = ""
    if not live:
        return {}
    # qom-tree lists the objects but not their properties; ask for the filename
    # so callers can offer the file straight away after a builder restart.
    for netdev in list(live):
        try:
            info = _qemu_monitor(vm, f"qom-get {_CAP_PREFIX}{netdev} file")
        except RuntimeError:
            continue
        match = re.search(r'"?(/\S+\.pcap)"?', info)
        if match:
            live[netdev] = match.group(1)
    return live


def capture_start(vm, netdev, path=None, snaplen=65536):
    """Start copying every frame on <vm> <netdev> into a .pcap. Returns the path."""
    if path is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(capture_dir(vm), f"{vm}_{netdev}_{stamp}.pcap")
    obj = _CAP_PREFIX + netdev
    # A capture left behind by an earlier run would keep its file open and make
    # object_add fail on the duplicate id.
    _qemu_monitor(vm, f"object_del {obj}")
    out = _qemu_monitor(
        vm, f"object_add filter-dump,id={obj},netdev={netdev},"
            f"file={path},maxlen={snaplen}")
    if re.search(r"error|Error|Parameter|not found", out):
        raise RuntimeError(_clean_monitor_error(out) or
                           f"QEMU refused to capture on {vm} {netdev}")
    return path


def _preview_path(directory, netdev, index):
    return os.path.join(directory, f".preview_{netdev}_{index}.pcap")


def preview_rotate(vm, netdev, directory, index):
    """Flush the live-preview chunk and start the next one.

    Returns (lines, next_index). The swap is done in a single monitor session:
    splitting it across two sessions leaves the NIC unwatched for as long as it
    takes to spawn 'vmm monitor' twice, which was enough to miss most of a burst.
    Frames arriving inside the remaining sub-second gap are still missed by the
    preview - the continuous capture running alongside records them either way,
    so only the on-screen list is approximate, never the downloaded file.
    """
    obj = _PRE_PREFIX + netdev
    nxt = index + 1
    cmds = []
    if index:
        cmds.append(f"object_del {obj}")
    cmds.append(f"object_add filter-dump,id={obj},netdev={netdev},"
                f"file={_preview_path(directory, netdev, nxt)},maxlen=4096")
    try:
        _qemu_monitor(vm, *cmds)
    except RuntimeError:
        return [], 0

    lines = []
    if index:
        # The delete above already flushed and closed this chunk, and the
        # object_add that followed it gave the write time to land.
        current = _preview_path(directory, netdev, index)
        lines = [l for l in _tcpdump(current).splitlines() if l.strip()]
        try:
            os.remove(current)
        except OSError:
            pass
    return lines, nxt


def preview_stop(vm, netdev, directory, index):
    """Detach the preview dump and delete its leftover chunk."""
    try:
        _qemu_monitor(vm, f"object_del {_PRE_PREFIX}{netdev}")
    except RuntimeError:
        pass
    if index:
        try:
            os.remove(_preview_path(directory, netdev, index))
        except OSError:
            pass


def capture_stop(vm, netdev=None):
    """Stop one capture, or every capture on this VM. Returns the netdevs stopped."""
    targets = [netdev] if netdev else list(capture_active(vm))
    for nd in targets:
        try:
            _qemu_monitor(vm, f"object_del {_CAP_PREFIX}{nd}")
        except RuntimeError:
            pass
    return targets


def _clean_monitor_error(text):
    """Pull a human sentence out of the monitor's echo-laden reply."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Error") or "Permission denied" in line:
            return line
    return ""


def _tcpdump(path):
    """One-line summaries of a .pcap, or '' if tcpdump cannot read it yet."""
    _nfs_revalidate(path)
    try:
        p = subprocess.run(["tcpdump", "-nr", path], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=60)
        return p.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _nfs_revalidate(path):
    """Force the NFS client to re-read the directory holding <path>.

    The pcap is created and grown by QEMU on a *different* host, so this host's
    cached directory listing and file attributes can lag badly - long enough for
    a live capture to look empty. Re-reading the directory drops that cache.
    """
    try:
        os.listdir(os.path.dirname(path) or ".")
    except OSError:
        pass


def capture_packets(path, offset=0, limit=400):
    """Decode a .pcap into one-line summaries, skipping the first <offset>.

    tcpdump does the dissection, so protocols show up by name (OSPF, LDP, BGP)
    instead of as hex. Reading a file that is still being written is fine - a
    half-written trailing packet is simply not reported yet.
    """
    if not os.path.exists(path):
        _nfs_revalidate(path)
        if not os.path.exists(path):
            return [], offset
    out = _tcpdump(path)
    lines = [l for l in out.splitlines() if l.strip()]
    fresh = lines[offset:offset + limit]
    return fresh, offset + len(fresh)


def capture_count(path):
    """How many packets are in the file so far."""
    if not os.path.exists(path):
        return 0
    return len([l for l in _tcpdump(path).splitlines() if l.strip()])


# -----------------------------------------------------------------------------
#  --capture / --capture-stop
# -----------------------------------------------------------------------------
def _capture_link_table(running=None):
    """Every capturable link in the running lab, as (device, peer, netdev)."""
    running = running or running_vms()
    owners = lab_switch_owners(running)
    rows = []
    for vm in running:
        for link in vm_links(vm, owners):
            for peer in link['peers']:
                rows.append((vm, peer, link['netdev']))
    return rows


def cli_capture_list():
    """Print the links --capture can record."""
    running = running_vms()
    if not running:
        print("❌ No VMs are running. Deploy the lab first.")
        return 1
    rows = _capture_link_table(running)
    if not rows:
        print("❌ The running VMs have no links between them.")
        return 1
    print("\nLinks you can capture:\n")
    print(f"  {'device':<22} {'far end':<22} netdev")
    print(f"  {'-'*22} {'-'*22} ------")
    for vm, peer, netdev in rows:
        print(f"  {vm:<22} {peer:<22} {netdev}")
    first = rows[0]
    print(f"\nCapture one with:\n"
          f"  python3 {os.path.basename(sys.argv[0])} --capture {first[0]} "
          f"--to {first[1]} --seconds 30\n")
    return 0


def cli_capture(device, peer, seconds, interface=None):
    """Record one link for <seconds>, then report where the .pcap landed."""
    if not device:
        return cli_capture_list()

    try:
        running = running_vms()
    except Exception as exc:
        print(f"❌ Could not talk to VMM: {exc}")
        return 1
    if not running:
        print("❌ No VMs are running. Deploy the lab first.")
        return 1

    hosts = {vm.split('_')[0] for vm in running} | set(running)
    if device not in hosts:
        close = difflib.get_close_matches(device, sorted(hosts), n=1, cutoff=0.6)
        print(f"❌ '{device}' is not a device in the running lab.")
        if close:
            print(f"   Did you mean '{close[0]}'?")
        print(f"   Running: {', '.join(sorted(hosts))}")
        return 1

    if not peer:
        # One link needs no --to; several do, and guessing would be wrong.
        mine = [r for r in _capture_link_table(running)
                if r[0] == device or r[0].split('_')[0] == device]
        peers = sorted({r[1].split('_')[0] for r in mine})
        if len(peers) == 1:
            peer = peers[0]
            print(f"ℹ️  {device} has one link, to {peer}.")
        elif not peers:
            print(f"❌ {device} has no links to another device.")
            return 1
        else:
            print(f"❌ {device} has several links - say which one with --to:")
            for p in peers:
                print(f"     --to {p}")
            return 1

    print(f"🔎 Finding the {device} ↔ {peer} link…", flush=True)
    try:
        targets = resolve_capture(device, peer, running, port_a=interface)
    except Exception as exc:
        print(f"❌ {exc}")
        return 1
    if not targets:
        if interface:
            print(f"❌ {device} {interface} is not wired to {peer} in the "
                  f"running lab.")
        else:
            print(f"❌ No link between {device} and {peer} in the running lab.")
        return 1

    chosen = targets[0]
    if len(targets) > 1:
        # Only happens without --interface: the pair is cabled together more
        # than once and nothing said which wire was meant.
        print(f"ℹ️  {device} and {peer} have {len(targets)} links; "
              f"capturing {chosen['netdev']}. Name the port with "
              f"--interface to pick a specific one:")
        for t in targets:
            print(f"     {t['netdev']} -> {t['peer']}")

    vm, netdev = chosen['vm'], chosen['netdev']
    try:
        path = capture_start(vm, netdev)
    except Exception as exc:
        print(f"❌ Could not start the capture: {exc}")
        return 1

    print(f"\n🎥 Capturing {vm} {netdev} ↔ {chosen['peer']} for {seconds}s…")
    try:
        for left in range(seconds, 0, -1):
            print(f"\r   {left:>4}s remaining ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n   interrupted - closing the file.")
    finally:
        print("\r" + " " * 30 + "\r", end="")
        capture_stop(vm, netdev)
        time.sleep(0.8)   # QEMU flushes on delete; let it reach the filesystem

    total = capture_count(path)
    print(f"✅ {total} packets → {path}")
    if total:
        preview, _ = capture_packets(path, 0, 10)
        for line in preview:
            print(f"     {line}")
        if total > len(preview):
            print(f"     … {total - len(preview)} more")
    else:
        print("   The link was idle. Send some traffic (a ping between the two "
              "devices is enough) and try again.")
    print(f"\n   Open it with Wireshark, or read it here:\n"
          f"     tcpdump -nr {path}\n")
    return 0


def cli_capture_stop(device):
    """Detach captures left attached to a VM, or to the whole lab."""
    try:
        running = running_vms()
    except Exception as exc:
        print(f"❌ Could not talk to VMM: {exc}")
        return 1
    if device:
        vms = vms_for_host(device, running)
        if not vms:
            print(f"❌ '{device}' is not a device in the running lab.")
            return 1
    else:
        vms = running

    stopped = 0
    for vm in vms:
        try:
            active = capture_active(vm)
        except Exception:
            continue
        for netdev in active:
            capture_stop(vm, netdev)
            print(f"   stopped {vm} {netdev}")
            stopped += 1
    print(f"✅ {stopped} capture(s) stopped." if stopped
          else "ℹ️  No captures were running.")
    return 0


def scan_live(topology_file):
    """Return {hostname: {'ip': .., 'lo0': ..}} scanned live from the running
    lab ('vmm ping' for mgmt IPs, SSH for lo0). Used by the server's periodic
    refresh so loopbacks configured/changed after boot show up in the browser."""
    data = _load_topology(topology_file)
    ip_map = get_vmm_ip_map()
    nodes = []
    for vm in data.get('vms', []):
        t = vm.get('type')
        host = vm['hostname']
        nodes.append({'id': host, 'type': t,
                      'ip': ip_map.get(re_ping_name(host, t), '')})
    annotate_lo0(nodes)
    return {n['id']: {'ip': n['ip'], 'lo0': n.get('lo0', '')} for n in nodes}


def serve_topology_diagram(topology_file, port=8080, out_path="topology.html",
                           scan_interval=10):
    """
    Host the interactive topology diagram over HTTP on this QPOD at
    http://<qpod-ip>:<port>/ . A background thread rescans the running lab
    every `scan_interval`s (mgmt IPs + lo0 loopbacks); the page polls GET /data
    and updates the labels live, so loopbacks configured/changed after the lab
    is up appear without regenerating. Blocks until Ctrl+C / SIGTERM.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    html, nn, ne, hi, lo = _build_topology_html(topology_file)
    try:
        with open(out_path, "w") as f:
            f.write(html)
    except OSError:
        pass
    payload = html.encode("utf-8")

    lock = threading.Lock()
    live = {"state": {}}

    def scanner():
        while True:
            try:
                s = scan_live(topology_file)
                with lock:
                    live["state"] = s
            except Exception:
                pass
            time.sleep(scan_interval)

    threading.Thread(target=scanner, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/data"):
                with lock:
                    body = json.dumps(live["state"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        srv = None
        if getattr(e, "errno", None) == errno.EADDRINUSE and reclaim_port(port):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            except OSError as e2:
                e = e2
        if srv is None:
            print(f"❌ Could not start the web server on port {port}: {e}", file=sys.stderr)
            if getattr(e, "errno", None) == errno.EADDRINUSE:
                print(describe_port_conflict(port), file=sys.stderr)
            else:
                print(f"   Try a different port with --port <N>.", file=sys.stderr)
            return

    ip = qpod_ip()
    ipnote = f"{hi}/{nn} devices show a mgmt IP" if hi else "no mgmt IPs yet (deploy first to show them)"
    lonote = f", {lo} with lo0 loopbacks" if lo else ""
    print("\n" + "=" * 60)
    print("  🌐  Topology is live — open it in a browser:")
    print(f"          http://{ip}:{port}/")
    print("=" * 60)
    print(f"  {nn} devices, {ne} links, {ipnote}{lonote}.")
    print(f"  Live: mgmt IPs + lo0 loopbacks refresh every {scan_interval}s.")
    print("  Editable: drag devices, add text/zones, export PNG/SVG.")
    print("  Press Ctrl+C to stop serving.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Stopped serving the topology.")
    finally:
        srv.server_close()


# PID file for the detached background web server (kept in the cwd so it lives
# next to the topology/lab files it serves).
_SERVER_PIDFILE = "topology_server.pid"
_SERVER_LOGFILE = "topology_server.log"


def _port_answers(port, host="127.0.0.1", timeout=0.4):
    """True if something is accepting TCP connections on this port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_pid_exit(pid, timeout=5.0):
    """Block until `pid` is gone (or timeout). Returns True if it exited."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def start_topology_server_bg(topology_file, port=8080):
    """
    Launch the topology web server as an independent, detached background
    process (a fresh 'python3 vmm.py ... --serve') so it runs in parallel and
    keeps serving after this command returns. Records its PID for --serve-stop.

    The success banner is only printed once the new process has actually bound
    the port. Previously it was printed unconditionally: if a stale server still
    held the port, the child died with 'Address already in use' into the log file
    while the console reported success - so you would browse the PREVIOUS lab's
    diagram and see a topology that no longer matched the YAML.
    """
    # If one is already running, stop it first and wait for the port to be
    # released - SIGTERM is asynchronous, so spawning immediately races it.
    if os.path.exists(_SERVER_PIDFILE):
        stop_topology_server(quiet=True)

    if _port_answers(port):
        # Something else still holds the port (pid file lost, or a server was
        # started from a different directory - the pid file lives in the cwd).
        holder = ""
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            if out:
                holder = f" (held by pid {', '.join(out)})"
        except Exception:
            pass
        print(f"\n❌ Port {port} is already in use{holder}.", file=sys.stderr)
        print("   That is almost certainly an older topology server still running -\n"
              "   if it is left alone you would be shown the PREVIOUS lab's diagram.\n"
              f"   Stop it with:  python3 {os.path.basename(__file__)} --serve-stop\n"
              f"   ...or serve this lab elsewhere with:  --serve --port {port + 1}",
              file=sys.stderr)
        return

    cmd = [sys.executable, os.path.abspath(__file__),
           "-t", topology_file, "--serve-fg", "--port", str(port)]
    try:
        logf = open(_SERVER_LOGFILE, "ab")
        proc = subprocess.Popen(
            cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True,   # detach from this terminal/process group
        )
    except Exception as e:
        print(f"❌ Could not start the background web server: {e}", file=sys.stderr)
        return

    # Confirm it really came up before claiming success.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _port_answers(port):
            break
        if proc.poll() is not None:      # child exited early
            break
        time.sleep(0.2)

    if not _port_answers(port):
        print(f"\n❌ The topology web server did not come up on port {port}.", file=sys.stderr)
        try:
            with open(_SERVER_LOGFILE) as f:
                tail = f.read().strip().splitlines()[-12:]
            if tail:
                print("   Last lines of " + _SERVER_LOGFILE + ":", file=sys.stderr)
                for line in tail:
                    print("     " + line, file=sys.stderr)
        except OSError:
            pass
        return

    with open(_SERVER_PIDFILE, "w") as f:
        f.write(str(proc.pid))

    ip = qpod_ip()
    print("\n" + "=" * 60)
    print("  🌐  Topology web server running in the background:")
    print(f"          http://{ip}:{port}/")
    print("=" * 60)
    print(f"  pid {proc.pid}  ·  logs: {_SERVER_LOGFILE}")
    print(f"  Serving: {topology_file}")
    print("  If the diagram looks stale, hard-refresh the browser (Ctrl-Shift-R).")
    print(f"  Stop it with:  python3 {os.path.basename(__file__)} --serve-stop\n")


def stop_topology_server(quiet=False):
    """Stop the background web server started with --serve-bg (via its PID file)."""
    if not os.path.exists(_SERVER_PIDFILE):
        if not quiet:
            print("No background topology web server is recorded (no pid file).")
        return
    try:
        pid = int(open(_SERVER_PIDFILE).read().strip())
        os.kill(pid, signal.SIGTERM)
        # Wait for it to actually go away, otherwise a server started straight
        # afterwards races it for the port and loses.
        if not _wait_for_pid_exit(pid):
            os.kill(pid, signal.SIGKILL)
            _wait_for_pid_exit(pid, timeout=3.0)
        if not quiet:
            print(f"🛑 Stopped topology web server (pid {pid}).")
    except (ProcessLookupError, ValueError):
        if not quiet:
            print("Topology web server was not running.")
    except Exception as e:
        if not quiet:
            print(f"Could not stop topology web server: {e}", file=sys.stderr)
    finally:
        try:
            os.remove(_SERVER_PIDFILE)
        except OSError:
            pass


# The diagram is a single self-contained HTML page (SVG + vanilla JS, no CDN).
# It is also a light editor: add text / zone annotations, show mgmt IPs, and
# export to PNG/SVG. __LAB__ / __NODES__ / __EDGES__ are replaced with JSON at
# generation time (each node carries an optional 'ip' from 'vmm ping').
_TOPOLOGY_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>VMM topology</title>
<style>
  html,body{margin:0;height:100%;background:#0f1720;color:#e6edf3;font-family:Arial,Helvetica,sans-serif}
  #bar{position:fixed;top:0;left:0;right:0;height:46px;display:flex;align-items:center;gap:7px;
       padding:0 12px;background:#16212e;border-bottom:1px solid #24313f;z-index:10}
  #bar b{font-size:14px;margin-right:4px}
  #bar .hint{color:#8aa0b2;font-size:12px;margin:0 6px}
  #bar button{background:#24313f;color:#e6edf3;border:1px solid #33475a;border-radius:6px;
              padding:6px 10px;cursor:pointer;font-size:13px}
  #bar button:hover{background:#2c3d4e}
  #bar button.on{background:#2f6feb;border-color:#2f6feb;color:#fff}
  .pal{display:inline-flex;gap:5px;margin:0 4px}
  .sw{width:18px;height:18px;padding:0;border-radius:50%;border:1px solid #0007;cursor:pointer}
  .grow{flex:1}
  svg{position:fixed;inset:46px 0 0 0;width:100%;height:calc(100% - 46px);cursor:grab;touch-action:none}
  .rsz{cursor:nwse-resize}
</style></head>
<body>
<div id="bar">
  <b id="lab"></b>
  <button class="tool on" data-tool="select">Select</button>
  <button class="tool" data-tool="text">+ Text</button>
  <button class="tool" data-tool="zone">+ Zone</button>
  <button id="szdn" title="smaller icons">Icon &minus;</button>
  <button id="szup" title="bigger icons">Icon +</button>
  <span id="palette" class="pal"></span>
  <button id="del">Delete</button>
  <span class="hint">drag a port dot to re-attach a link &middot; drag a label to move it (double-click to rename) &middot; grab a link to bend &middot; scroll = zoom</span>
  <span class="grow"></span>
  <button id="pngb">PNG</button>
  <button id="svgb">SVG</button>
  <button id="reset">Reset</button>
</div>
<svg id="svg">
  <style>
    .edge{stroke:#6b8296;stroke-width:1.8;fill:none}
    .iflabel{font-size:10.5px;fill:#e6edf3;paint-order:stroke;stroke:#0f1720;stroke-width:3px;
             stroke-linejoin:round;text-anchor:middle}
    text{font-family:Arial,Helvetica,sans-serif}
    .nh{font-size:13px;font-weight:600;fill:#eaf1f8;text-anchor:middle;paint-order:stroke;stroke:#0f1720;stroke-width:2.6px;stroke-linejoin:round}
    .nip{font-size:10.5px;fill:#9db4c8;text-anchor:middle;font-family:monospace;paint-order:stroke;stroke:#0f1720;stroke-width:2.6px;stroke-linejoin:round}
    .nlo{font-size:11px;font-weight:600;fill:#8fe3c8;text-anchor:middle;font-family:monospace;paint-order:stroke;stroke:#0f1720;stroke-width:2.8px;stroke-linejoin:round}
    .rsz{cursor:nwse-resize}
    .port{fill:#cfe3f5;stroke:#0f1720;stroke-width:1;cursor:grab}
  </style>
  <defs><pattern id="grid" width="26" height="26" patternUnits="userSpaceOnUse">
    <rect width="26" height="26" fill="#0f1720"/><circle cx="2" cy="2" r="1" fill="#1c2b3b"/>
  </pattern></defs>
  <g id="vp"><rect x="-4000" y="-4000" width="8000" height="8000" fill="url(#grid)"/><g id="zones"></g><g id="edges"></g><g id="labels"></g><g id="nodes"></g><g id="ports"></g><g id="texts"></g></g>
</svg>
<script>
const LAB=__LAB__, NODES=__NODES__, EDGES=__EDGES__;
const KEY='vmm_topo_'+LAB;
document.getElementById('lab').textContent=LAB+" — topology";
const COLORS={server:'#c9d1d9',vswitch:'#6cb6ff',vrouter:'#6cb6ff',
 vmx:'#7ee787',vferrari:'#7ee787',vbugatti:'#7ee787',vhamilton:'#7ee787',vmaserati:'#7ee787',
 vscapa:'#f0883e',vardbeg:'#f0883e',vbowmore:'#f0883e',vbrackla:'#f0883e',valfaromeo:'#f0883e',vbalerion:'#f0883e',vqfx:'#d2a8ff'};
function cat(t){if(t==='server')return'server';if(t==='vswitch'||t==='vqfx')return'switch';return'router';}
const ICON={
 router:'<circle r="8" fill="none" stroke="#0c141c" stroke-width="1.3"/><g stroke="#0c141c" stroke-width="1.2" fill="none"><path d="M0 -6V-11 M-2.2 -8.6L0 -11L2.2 -8.6"/><path d="M0 6V11 M-2.2 8.6L0 11L2.2 8.6"/><path d="M-6 0H-11 M-8.6 -2.2L-11 0L-8.6 2.2"/><path d="M6 0H11 M8.6 -2.2L11 0L8.6 2.2"/></g>',
 switch:'<rect x="-10" y="-7" width="20" height="14" rx="3" fill="none" stroke="#0c141c" stroke-width="1.3"/><g stroke="#0c141c" stroke-width="1.2" fill="none"><path d="M-6 -2H6 M3 -5L6 -2L3 1"/><path d="M6 3H-6 M-3 0L-6 3L-3 6"/></g>',
 server:'<rect x="-7" y="-10" width="14" height="20" rx="2" fill="none" stroke="#0c141c" stroke-width="1.3"/><g stroke="#0c141c" stroke-width="1.2"><path d="M-4 -6H4"/><path d="M-4 -1H4"/><path d="M-4 4H2"/></g>'
};
const PALETTE=['#ffd24a','#ff6b6b','#4ade80','#5ac8fa','#e6edf3'];
const NS='http://www.w3.org/2000/svg';
const svg=document.getElementById('svg'),vp=document.getElementById('vp');
const gZ=document.getElementById('zones'),gE=document.getElementById('edges'),gL=document.getElementById('labels'),gN=document.getElementById('nodes'),gP=document.getElementById('ports'),gT=document.getElementById('texts');
let HAS_IP=NODES.some(n=>n.ip);
// Fan parallel links between the same pair of devices: index each edge within
// its pair so update() can bow them apart into separate curves.
const _pk=e=>[e.a,e.b].slice().sort().join('|');
const _tot={};EDGES.forEach(e=>{_tot[_pk(e)]=(_tot[_pk(e)]||0)+1;});
const _seen={};EDGES.forEach(e=>{const k=_pk(e);e._i=(k in _seen)?_seen[k]+1:0;_seen[k]=e._i;e._n=_tot[k];});
EDGES.forEach(e=>{e._ai=e.ai;e._bi=e.bi;});   // keep originals for Reset
let view={x:60,y:60,k:1},pos={},annos=[],tool='select',sel=null,curColor=PALETTE[0],uid=1,active=null,nodeScale=1;
// Parallel links leave the icon at slightly different angles so their dots and
// labels don't stack. FAN is the angle between adjacent links; FAN_MAX caps the
// total spread so a device with many parallel links keeps them on one face.
const FAN=0.40, FAN_MAX=1.2;

function el(t,a){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;}
function txt(x,y,s,a){const e=el('text',Object.assign({x:x,y:y},a||{}));e.textContent=s;return e;}
// Saved browser state is keyed by lab name, so editing a topology (or reusing a
// lab_name for a different one) must not resurrect the previous lab's data.
// Every saved item is therefore matched by IDENTITY, never by array index:
//   - node positions by hostname (unknown hostnames are dropped)
//   - edge state by endpoint+original-interface signature
// Restoring edges by index was the old behaviour and it silently pasted the
// previous topology's interface labels onto the new links.
const _eid=e=>e.a+'|'+e._ai+'|'+e.b+'|'+e._bi;
function persist(){try{localStorage.setItem(KEY,JSON.stringify({pos,annos,view,uid,nodeScale,
 edges:EDGES.map(e=>({id:_eid(e),bend:e.bend,ai:e.ai,bi:e.bi,aOff:e.aOff,bOff:e.bOff,laOff:e.laOff,lbOff:e.lbOff}))}));}catch(e){}}
function restore(){try{const s=JSON.parse(localStorage.getItem(KEY));if(s){
 const ids=new Set(NODES.map(n=>n.id));
 pos={};for(const k in (s.pos||{}))if(ids.has(k))pos[k]=s.pos[k];
 annos=s.annos||[];if(s.view)view=s.view;uid=s.uid||1;if(s.nodeScale)nodeScale=s.nodeScale;
 if(s.edges){const m={};s.edges.forEach(se=>{if(se&&se.id)m[se.id]=se;});
  EDGES.forEach(e=>{const se=m[_eid(e)];if(!se)return;
   if(se.bend!=null)e.bend=se.bend;if(se.ai)e.ai=se.ai;if(se.bi)e.bi=se.bi;
   if(se.aOff)e.aOff=se.aOff;if(se.bOff)e.bOff=se.bOff;if(se.laOff)e.laOff=se.laOff;if(se.lbOff)e.lbOff=se.lbOff;});}}}catch(e){}}
function layout(){const w=svg.clientWidth||1000,h=svg.clientHeight||600,cx=w/2-60,cy=h/2-40,R=Math.max(170,72*NODES.length/Math.PI);
 NODES.forEach((n,i)=>{if(!pos[n.id]){const a=2*Math.PI*i/NODES.length-Math.PI/2;pos[n.id]={x:cx+R*Math.cos(a),y:cy+R*Math.sin(a)};}});}

const nodeEls={},edgeEls=[];
// EVE-NG style node: a coloured device-icon chip (scaled by nodeScale) with
// the hostname and mgmt IP labelled beneath it.
function nodeSvg(n){const g=el('g',{class:'node'});g.style.cursor='grab';const CH=44*nodeScale;
  if(n.lo0)g.appendChild(txt(0,-CH/2-9,n.lo0,{class:'nlo'}));   // loopback0 above the icon
  g.appendChild(el('rect',{x:-CH/2,y:-CH/2,width:CH,height:CH,rx:10*nodeScale,fill:COLORS[n.type]||'#c9d1d9',stroke:'#0c141c','stroke-opacity':.35}));
  const ic=el('g',{transform:'scale('+nodeScale+')'});ic.innerHTML=ICON[cat(n.type)]||'';g.appendChild(ic);
  g.appendChild(txt(0,CH/2+15,n.id,{class:'nh'}));
  if(HAS_IP)g.appendChild(txt(0,CH/2+29,n.ip||'—',{class:'nip'}));
  g.addEventListener('pointerdown',ev=>nodeDown(ev,n.id));return g;}
function rebuildNodes(){gN.innerHTML='';NODES.forEach(n=>{const g=nodeSvg(n);gN.appendChild(g);nodeEls[n.id]=g;});update();}
function build(){
 EDGES.forEach(e=>{
  const hit=el('path',{fill:'none',stroke:'transparent','stroke-width':16});hit.style.cursor='grab';gE.appendChild(hit);
  const path=el('path',{class:'edge',fill:'none'});gE.appendChild(path);
  const dotA=el('circle',{r:3.6,class:'port'}),dotB=el('circle',{r:3.6,class:'port'});gP.appendChild(dotA);gP.appendChild(dotB);
  const la=txt(0,0,e.ai,{class:'iflabel'});la.style.cursor='move';gL.appendChild(la);
  const lb=txt(0,0,e.bi,{class:'iflabel'});lb.style.cursor='move';gL.appendChild(lb);
  hit.addEventListener('pointerdown',ev=>bendDown(ev,e));
  dotA.addEventListener('pointerdown',ev=>{ev.stopPropagation();active={type:'endpt',e:e,which:'a'};});
  dotB.addEventListener('pointerdown',ev=>{ev.stopPropagation();active={type:'endpt',e:e,which:'b'};});
  la.addEventListener('pointerdown',ev=>{ev.stopPropagation();active={type:'label',e:e,which:'a'};});
  lb.addEventListener('pointerdown',ev=>{ev.stopPropagation();active={type:'label',e:e,which:'b'};});
  la.addEventListener('dblclick',ev=>{ev.stopPropagation();const v=prompt('Interface label:',e.ai);if(v!==null){e.ai=v;la.textContent=v;persist();}});
  lb.addEventListener('dblclick',ev=>{ev.stopPropagation();const v=prompt('Interface label:',e.bi);if(v!==null){e.bi=v;lb.textContent=v;persist();}});
  edgeEls.push({e,hit,path,dotA,dotB,la,lb});});
 NODES.forEach(n=>{const g=nodeSvg(n);gN.appendChild(g);nodeEls[n.id]=g;});}

// Curved link: quadratic bezier whose control point bows perpendicular to the
// straight A-B line. `off` is the bow - auto-fanned for parallel links so they
// separate, or set manually when the user grabs and bends a link.
function bz(A,C,B,t){const u=1-t;return{x:u*u*A.x+2*u*t*C.x+t*t*B.x,y:u*u*A.y+2*u*t*C.y+t*t*B.y};}
// Each link attaches to a device ON THE BORDER OF ITS ICON - never at the centre
// (where the icon would cover it) and never floating free in the canvas. The
// anchor is the point at which the line toward the peer device crosses the
// icon's bounding square, plus a small gap. Parallel links between the same pair
// are fanned perpendicular so they land on different points of that border.
//
// Dragging an endpoint chooses WHICH SIDE of the icon the link leaves from; it
// is stored as an angle, so the dot stays welded to the icon however the device
// is moved or scaled. (It used to store a free dx/dy from the cursor, which let
// the dot be dragged off the icon and stranded in empty space.)
function iconHalf(){return 22*nodeScale+3;}   // chip is 44*nodeScale square
function clipIcon(cx,cy,dx,dy){const h=iconHalf(),m=Math.max(Math.abs(dx),Math.abs(dy));
 if(!m)return{x:cx,y:cy-h};
 const t=h/m;return{x:cx+dx*t,y:cy+dy*t};}
function anchor(e,which){
 const c=pos[which==='a'?e.a:e.b],p=pos[which==='a'?e.b:e.a];
 const o=(which==='a')?e.aOff:e.bOff;
 // Only an explicit angle is honoured - a legacy free-floating {dx,dy} offset
 // saved by an older version is ignored, so old diagrams heal themselves.
 if(o&&o.ang!=null)return clipIcon(c.x,c.y,Math.cos(o.ang),Math.sin(o.ang));
 if(!p)return clipIcon(c.x,c.y,0,-1);
 // Fan parallel links by ANGLE around the icon, not by nudging the direction
 // vector: the nudge is proportional to link length and the clip normalises it
 // away, so on a long link the dots ended up ~1px apart and stacked. The total
 // fan is capped so a device with many parallel links doesn't wrap around.
 const base=Math.atan2(p.y-c.y,p.x-c.x);
 const step=Math.min(FAN,FAN_MAX/Math.max(1,e._n-1));
 const ang=base+(e._i-(e._n-1)/2)*step;
 return clipIcon(c.x,c.y,Math.cos(ang),Math.sin(ang));}
function endptA(e){return anchor(e,'a');}
function endptB(e){return anchor(e,'b');}
function endpoints(e){return[endptA(e),endptB(e)];}

function applyView(){vp.setAttribute('transform','translate('+view.x+','+view.y+') scale('+view.k+')');}
function update(){applyView();
 const BOW=16;
 edgeEls.forEach(function(o){const e=o.e;if(!pos[e.a]||!pos[e.b])return;
  const A=endptA(e),B=endptB(e);
  const dx=B.x-A.x,dy=B.y-A.y,L=Math.hypot(dx,dy)||1,px=-dy/L,py=dx/L;
  const off=(e.bend!==undefined && e.bend!==null)?e.bend:(e._i-(e._n-1)/2)*BOW;
  const C={x:(A.x+B.x)/2+px*off*2,y:(A.y+B.y)/2+py*off*2};
  const d='M'+A.x+' '+A.y+' Q'+C.x+' '+C.y+' '+B.x+' '+B.y;
  o.path.setAttribute('d',d);o.hit.setAttribute('d',d);
  o.dotA.setAttribute('cx',A.x);o.dotA.setAttribute('cy',A.y);o.dotB.setAttribute('cx',B.x);o.dotB.setAttribute('cy',B.y);
  // labels: custom position (relative to their end, so they follow the node) or default along the curve
  const la=e.laOff?{x:A.x+e.laOff.dx,y:A.y+e.laOff.dy}:(function(){const p=bz(A,C,B,0.25);return{x:p.x,y:p.y-5};})();
  const lb=e.lbOff?{x:B.x+e.lbOff.dx,y:B.y+e.lbOff.dy}:(function(){const p=bz(A,C,B,0.75);return{x:p.x,y:p.y-5};})();
  o.la.setAttribute('x',la.x);o.la.setAttribute('y',la.y);o.lb.setAttribute('x',lb.x);o.lb.setAttribute('y',lb.y);});
 NODES.forEach(n=>{nodeEls[n.id].setAttribute('transform','translate('+pos[n.id].x+','+pos[n.id].y+')');});}

function drawAnnos(){gZ.innerHTML='';gT.innerHTML='';
 annos.forEach(a=>{
  if(a.kind==='zone'){const g=el('g',{transform:'translate('+a.x+','+a.y+')'});g.dataset.id=a.id;
   g.appendChild(el('rect',{x:0,y:0,width:a.w,height:a.h,rx:10,fill:a.color,'fill-opacity':.10,stroke:a.color,'stroke-opacity':.7,'stroke-dasharray':'7 5','stroke-width':sel===a.id?2:1.4}));
   g.appendChild(txt(11,21,a.label||'zone',{fill:a.color,'font-size':13,'font-weight':500}));
   const hl=el('rect',{x:a.w-14,y:a.h-14,width:11,height:11,rx:2,fill:a.color,class:'rsz'});g.appendChild(hl);
   g.addEventListener('pointerdown',ev=>annoDown(ev,a));
   hl.addEventListener('pointerdown',ev=>zoneResize(ev,a));
   g.addEventListener('dblclick',ev=>{ev.stopPropagation();const v=prompt('Zone label:',a.label||'');if(v!==null){a.label=v;drawAnnos();persist();}});
   gZ.appendChild(g);
  }else{const g=el('g',{transform:'translate('+a.x+','+a.y+')'});g.dataset.id=a.id;
   const t=txt(0,0,a.text,{fill:a.color,'font-size':a.size||15,'font-weight':500});
   if(sel===a.id){t.setAttribute('stroke','#5ac8fa');t.setAttribute('stroke-width',.5);}
   g.appendChild(t);
   g.addEventListener('pointerdown',ev=>annoDown(ev,a));
   g.addEventListener('dblclick',ev=>{ev.stopPropagation();const v=prompt('Text:',a.text);if(v!==null){a.text=v;drawAnnos();persist();}});
   gT.appendChild(g);}});}

function worldOf(ev){const r=svg.getBoundingClientRect();return{x:(ev.clientX-r.left-view.x)/view.k,y:(ev.clientY-r.top-view.y)/view.k};}
function moveAnnoEl(a){const g=(a.kind==='zone'?gZ:gT).querySelector('[data-id="'+a.id+'"]');if(g)g.setAttribute('transform','translate('+a.x+','+a.y+')');}
function nodeDown(ev,id){ev.stopPropagation();if(sel){sel=null;drawAnnos();}const w=worldOf(ev);active={type:'node',id:id,dx:pos[id].x-w.x,dy:pos[id].y-w.y};}
function annoDown(ev,a){if(tool!=='select')return;ev.stopPropagation();sel=a.id;drawAnnos();const w=worldOf(ev);active={type:'anno',a:a,dx:a.x-w.x,dy:a.y-w.y};}
function zoneResize(ev,a){ev.stopPropagation();sel=a.id;active={type:'resize',a:a};}
function bendDown(ev,e){if(tool!=='select')return;ev.stopPropagation();if(sel){sel=null;drawAnnos();}active={type:'bend',e:e};}

svg.addEventListener('pointerdown',ev=>{
 if(ev.target.closest('[data-id]')||ev.target.closest('.node'))return;
 if(tool==='text'||tool==='zone'){placeAnno(worldOf(ev));return;}
 if(sel){sel=null;drawAnnos();}
 active={type:'pan',sx:ev.clientX,sy:ev.clientY,vx:view.x,vy:view.y};});
svg.addEventListener('pointermove',ev=>{if(!active)return;
 if(active.type==='pan'){view.x=active.vx+(ev.clientX-active.sx);view.y=active.vy+(ev.clientY-active.sy);applyView();return;}
 const w=worldOf(ev);
 if(active.type==='node'){pos[active.id]={x:w.x+active.dx,y:w.y+active.dy};update();}
 else if(active.type==='anno'){active.a.x=w.x+active.dx;active.a.y=w.y+active.dy;moveAnnoEl(active.a);}
 else if(active.type==='resize'){active.a.w=Math.max(70,w.x-active.a.x);active.a.h=Math.max(46,w.y-active.a.y);drawAnnos();}
 else if(active.type==='bend'){const e=active.e,ep=endpoints(e),A=ep[0],B=ep[1];const dx=B.x-A.x,dy=B.y-A.y,L=Math.hypot(dx,dy)||1,px=-dy/L,py=dx/L,mx=(A.x+B.x)/2,my=(A.y+B.y)/2;
   e.bend=(w.x-mx)*px+(w.y-my)*py;update();}
 else if(active.type==='endpt'){const e=active.e,c=pos[active.which==='a'?e.a:e.b],o={ang:Math.atan2(w.y-c.y,w.x-c.x)};if(active.which==='a')e.aOff=o;else e.bOff=o;update();}
 else if(active.type==='label'){const e=active.e,A=(active.which==='a')?endptA(e):endptB(e),o={dx:w.x-A.x,dy:w.y-A.y};if(active.which==='a')e.laOff=o;else e.lbOff=o;update();}});
window.addEventListener('pointerup',()=>{if(active){if(active.type!=='pan')persist();active=null;}});
svg.addEventListener('wheel',ev=>{ev.preventDefault();const r=svg.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top,f=ev.deltaY<0?1.1:1/1.1,nk=Math.min(3,Math.max(.3,view.k*f));view.x=mx-(mx-view.x)*(nk/view.k);view.y=my-(my-view.y)*(nk/view.k);view.k=nk;applyView();persist();},{passive:false});

function placeAnno(w){const id='a'+(uid++);
 if(tool==='text'){const a={id:id,kind:'text',x:w.x,y:w.y,text:'text',color:curColor,size:15};annos.push(a);sel=id;drawAnnos();
  const v=prompt('Text:',a.text);if(v===null){annos=annos.filter(x=>x.id!==id);sel=null;}else a.text=v||'text';drawAnnos();}
 else{annos.push({id:id,kind:'zone',x:w.x,y:w.y,w:240,h:150,color:curColor,label:'zone'});sel=id;drawAnnos();}
 setTool('select');persist();}
function delSel(){if(!sel)return;annos=annos.filter(a=>a.id!==sel);sel=null;drawAnnos();persist();}
window.addEventListener('keydown',ev=>{if((ev.key==='Delete'||ev.key==='Backspace')&&sel){ev.preventDefault();delSel();}});

function setTool(t){tool=t;document.querySelectorAll('.tool').forEach(b=>b.classList.toggle('on',b.dataset.tool===t));svg.style.cursor=(t==='select')?'grab':'crosshair';}
document.querySelectorAll('.tool').forEach(b=>b.addEventListener('click',()=>setTool(b.dataset.tool)));
const pal=document.getElementById('palette');
PALETTE.forEach(c=>{const s=document.createElement('button');s.className='sw';s.style.background=c;s.title=c;
 s.addEventListener('click',()=>{curColor=c;if(sel){const a=annos.find(x=>x.id===sel);if(a){a.color=c;drawAnnos();persist();}}});pal.appendChild(s);});
document.getElementById('szup').addEventListener('click',()=>{nodeScale=Math.min(2.4,nodeScale+0.2);rebuildNodes();persist();});
document.getElementById('szdn').addEventListener('click',()=>{nodeScale=Math.max(0.6,Math.round((nodeScale-0.2)*10)/10);rebuildNodes();persist();});
document.getElementById('del').addEventListener('click',delSel);
document.getElementById('reset').addEventListener('click',()=>{try{localStorage.removeItem(KEY);}catch(e){}
 EDGES.forEach(e=>{delete e.bend;delete e.aOff;delete e.bOff;delete e.laOff;delete e.lbOff;e.ai=e._ai;e.bi=e._bi;});
 edgeEls.forEach(o=>{o.la.textContent=o.e.ai;o.lb.textContent=o.e.bi;});
 pos={};annos=[];view={x:60,y:60,k:1};uid=1;nodeScale=1;layout();rebuildNodes();update();drawAnnos();});

function exportSVG(){const W=svg.clientWidth,H=svg.clientHeight,clone=svg.cloneNode(true);
 clone.setAttribute('width',W);clone.setAttribute('height',H);clone.setAttribute('viewBox','0 0 '+W+' '+H);
 clone.insertBefore(el('rect',{x:0,y:0,width:W,height:H,fill:'#0f1720'}),clone.firstChild);
 return new XMLSerializer().serializeToString(clone);}
function dl(name,blob){const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);}
document.getElementById('svgb').addEventListener('click',()=>dl('topology.svg',new Blob([exportSVG()],{type:'image/svg+xml'})));
document.getElementById('pngb').addEventListener('click',()=>{const W=svg.clientWidth,H=svg.clientHeight,xml=exportSVG(),img=new Image();
 img.onload=()=>{const c=document.createElement('canvas');c.width=W*2;c.height=H*2;const x=c.getContext('2d');x.scale(2,2);x.drawImage(img,0,0);c.toBlob(b=>dl('topology.png',b));};
 img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(xml);});

restore();layout();build();update();drawAnnos();setTool('select');

// Live refresh: when served, poll /data and update mgmt IP + lo0 labels in
// place so loopbacks configured/changed after boot appear automatically.
function poll(){
  fetch('data').then(r=>r.json()).then(d=>{
    let changed=false;
    NODES.forEach(n=>{const s=d[n.id]; if(!s)return;
      if((s.ip||'')!==(n.ip||'')){n.ip=s.ip||'';changed=true;}
      if((s.lo0||'')!==(n.lo0||'')){n.lo0=s.lo0||'';changed=true;}});
    if(changed){ HAS_IP=NODES.some(n=>n.ip); rebuildNodes(); }
  }).catch(()=>{});
}
if(location.protocol!=='file:'){ setTimeout(poll,1500); setInterval(poll,10000); }
</script>
</body></html>
"""


# -----------------------------
# Main workflow
# -----------------------------
def main():
    # To stderr so data-producing modes (e.g. --print_devices) can be piped or
    # redirected to a file without this banner polluting stdout.
    print("Install the required libraries if not already done:please use --> pip3 install pyyaml jinja2  junos-eznc paramiko", file=sys.stderr)
    parser = argparse.ArgumentParser(description="A tool to deploy and configure a VMM lab from a YAML topology.")
    parser.add_argument("-t", "--topology", default="topo.yml", help="Path to the YAML topology file (default: topo.yml)")
    parser.add_argument("-o", "--output", default="lab_topology.conf", help="Name of the output configuration file")
    parser.add_argument("--lab_detail", action="store_true", help="Display lab summary table and exit.")
    parser.add_argument("--config", action="store_true", help="Enter configuration management mode.")
    parser.add_argument("--config_file_only", action="store_true", help="Generate the VMM config file only and exit.")
    parser.add_argument("--force", action="store_true", help="Deploy even if the lab looks too big for the pod's free capacity.")
    parser.add_argument("--resume", action="store_true",
                        help="The lab is already deployed and running: skip Phase 2 "
                             "(unbind/config/start) and go straight to waiting for boot "
                             "and applying the baseline configuration.")
    parser.add_argument("--skip_boot_wait", action="store_true",
                        help="Skip the Phase 3 ping wait entirely and go straight to configuration. "
                             "Safe because devices are configured over the serial console, which "
                             "has its own boot/login retry handling.")
    parser.add_argument("--boot_wait", type=int, default=900, metavar="SECONDS",
                        help="Hard cap on the Phase 3 ping wait (default: 900).")
    parser.add_argument("--ping_grace", type=int, default=420, metavar="SECONDS",
                        help="How long Phase 3 waits for the FIRST device to answer 'vmm ping' "
                             "before giving up and handing off to the serial login (default: 420). "
                             "Slow TVP images (vHamilton/vMX10004, vFerrari) take ~5-6 min to boot "
                             "and pull a mgmt IP; a value below that makes Phase 3 hand off early, "
                             "so Phase 4 then sits through the boot on the serial console.")
    parser.add_argument("--debug", action="store_true",
                        help="Stream the full serial console dialogue for each device during "
                             "Phase 4. Use this when a device appears stuck to see exactly where "
                             "its login/commit is waiting.")
    parser.add_argument("--interfaces", action="store_true",
                        help="Print the valid interfaces for every device in the topology and exit "
                             "(a per-device cheat sheet - no need to remember et-/ge-/xe-).")
    parser.add_argument("--print_devices", dest="print_devices", action="store_true",
                        help="Print every Junos device as a junos-mcp-server devices.json map "
                             "(hostname -> ip/port/username/password) and exit. IPs come from "
                             "'vmm ping'. Redirect to a file: --print_devices > devices.json.")
    parser.add_argument("--diagram", nargs="?", const="topology.html", metavar="FILE",
                        help="Generate an interactive, draggable topology diagram (self-contained "
                             "HTML, default 'topology.html') from the links and exit. Every link is "
                             "labelled with the interface at each end.")
    parser.add_argument("--serve", action="store_true",
                        help="Start the topology diagram web server in the BACKGROUND (detached; prints "
                             "the URL and returns immediately, keeps running in parallel). Serve-only - "
                             "does not deploy. Stop it with --serve-stop.")
    parser.add_argument("--serve-bg", dest="serve_bg", action="store_true",
                        help="Alias for --serve (background web server).")
    parser.add_argument("--serve-fg", dest="serve_fg", action="store_true",
                        help=argparse.SUPPRESS)   # internal: the foreground worker spawned by --serve
    parser.add_argument("--serve-stop", dest="serve_stop", action="store_true",
                        help="Stop the background topology web server.")
    parser.add_argument("--port", type=int, default=None, metavar="PORT",
                        help="Port for --serve (default 8080) or --build (default 8081).")
    parser.add_argument("--servers", nargs="*", type=int, default=None, metavar="PORT",
                        help="Is a web server running? Lists this script's builder/diagram "
                             "servers, the port each is on, and whether it actually answers "
                             "HTTP - a wedged server still holds its port, so 'listening' and "
                             "'working' are reported separately. Name PORTs to check specific "
                             "ones. Read-only: unlike --stop-port it never stops anything.")
    parser.add_argument("--stop-port", dest="stop_port", type=int, default=None, metavar="PORT",
                        help="Close whatever server of this script's is listening on PORT, so the "
                             "port can be reused. Use it when --build or --serve says 'Address "
                             "already in use' - usually a builder left running in a closed shell. "
                             "Reports the owner and stops rather than killing anything that is not "
                             "ours.")
    parser.add_argument("--build", action="store_true",
                        help="Start the browser-based topology BUILDER: pick devices, wire them "
                             "port-to-port by clicking, validate live against the same rules this "
                             "script enforces, then save topo.yml or deploy. Runs in the foreground.")
    parser.add_argument("--build-port", dest="build_port", type=int, default=None, metavar="PORT",
                        help="Port for --build only, so a builder and a --serve diagram can run "
                             "side by side. Overrides --port.")
    parser.add_argument("--capture", nargs="?", const="", metavar="DEVICE",
                        help="Capture packets on a live link without redeploying: "
                             "--capture R1 --to R2. The frames are copied straight out of "
                             "the running VM, so no sniffer device is needed. Writes a .pcap "
                             "you can open in Wireshark. With no DEVICE, lists the links that "
                             "can be captured.")
    parser.add_argument("--to", dest="capture_to", default=None, metavar="DEVICE",
                        help="The device at the far end of the link to capture "
                             "(only needed with --capture when the device has several links).")
    parser.add_argument("--seconds", dest="capture_seconds", type=int, default=30,
                        metavar="N", help="How long --capture records for (default 30).")
    parser.add_argument("--interface", dest="capture_iface", default=None,
                        metavar="IFACE",
                        help="Which port on the --capture device to record, e.g. "
                             "ge-0/0/2. Only needed when the two devices are "
                             "wired together more than once.")
    parser.add_argument("--capture-stop", dest="capture_stop", nargs="?", const="",
                        metavar="DEVICE",
                        help="Detach any capture left running on DEVICE, or on every device "
                             "in the lab if none is given.")
    args = parser.parse_args()

    # --port applies to whichever server you asked for. --build-port is the
    # explicit override for the builder, so the two servers can coexist.
    serve_port = args.port if args.port is not None else 8080
    build_port = (args.build_port if args.build_port is not None
                  else args.port if args.port is not None else 8081)

    # --- Read-only: what is serving right now (never stops anything) ---
    if args.servers is not None:
        sys.exit(print_server_status(args.servers or None))

    # --- Free a port held by an earlier server (runs before anything binds) ---
    if args.stop_port is not None:
        sys.exit(0 if stop_port(args.stop_port) else 1)

    # --- Packet capture on a running lab (no redeploy, no sniffer device) ---
    if args.capture_stop is not None:
        sys.exit(cli_capture_stop(args.capture_stop))
    if args.capture is not None:
        sys.exit(cli_capture(args.capture, args.capture_to,
                             args.capture_seconds, args.capture_iface))

    # --- Topology builder UI (foreground; writes args.topology) ---
    if args.build:
        try:
            import vmm_builder
        except ImportError as e:
            print(f"❌ Could not load the builder UI: {e}\n"
                  f"   vmm_builder.py must sit next to vmm.py.", file=sys.stderr)
            sys.exit(1)
        sys.exit(vmm_builder.serve_builder(args.topology, build_port))

    # --- Web server: decoupled from deploy, runs in the background ---
    if args.serve_stop:
        stop_topology_server()
        sys.exit(0)
    if args.serve_fg:                        # detached child process: block and serve
        serve_topology_diagram(args.topology, serve_port)
        sys.exit(0)
    if args.serve or args.serve_bg:          # user-facing: launch the background server, return
        start_topology_server_bg(args.topology, serve_port)
        sys.exit(0)

    if args.interfaces:
        print_interfaces(args.topology)
        sys.exit(0)

    if args.print_devices:
        print_devices(args.topology)
        sys.exit(0)

    if args.diagram:
        generate_topology_diagram(args.topology, args.diagram)
        sys.exit(0)

    if args.config_file_only:
        print("NOTE: --config_file_only flag detected.")
        generate_config(args.topology, args.output)
        print(f"✅ VMM configuration file '{args.output}' created successfully. Exiting now.")
        sys.exit(0)


    if args.config:
        handle_config_management(args.topology)
        sys.exit(0)

    if args.lab_detail:
        try:
            # This line was fixed to accept all 3 return values
            topology_data, final_summary_mappings, capture_mappings = generate_config(args.topology, args.output, quiet=True)
            print_summary_table(topology_data)
            # This was updated to pass the correct variable to the function
            print_capture_info_table(final_summary_mappings)
            sys.exit(0)
        except FileNotFoundError:
            print(f"❌ Error: Topology file not found at '{args.topology}'", file=sys.stderr)
            sys.exit(1)
        except yaml.YAMLError as e:
            print(f"❌ Error: Could not parse YAML file '{args.topology}'. Error: {e}", file=sys.stderr)
            sys.exit(1)

    # --- Normal execution flow ---
    topology_data, final_summary_mappings, capture_mappings = generate_config(args.topology, args.output)
    if args.resume:
        print("\n⏭️  --resume: the lab is already deployed, skipping Phase 2 "
              "(unbind/config/start) and going straight to the boot wait.")
    else:
        run_vmm_config(args.output, force=args.force)
    
    if args.skip_boot_wait:
        print("\n⏭️  Skipping the boot wait (--skip_boot_wait); serial consoles "
              "retry on their own until each device is ready.")
    else:
        monitor_vms(configurable_devices(topology_data), timeout=args.boot_wait,
                    no_response_timeout=args.ping_grace)


    time.sleep(5)
    print("\n" + "="*50)
    print(" 🔧 Phase 4: Applying Baseline Configuration")
    print("="*50) 
    print("**NOTE** This may take longer if using vSCAPA version that do not have fix for PR :1726785.\n")
    # Serial-configured types build their own 'vmm serial -t <name>_RE'
    # command, so they are dispatched straight from the topology - NOT from
    # 'vmm serial' discovery. (Keying dispatch off discovery used to silently
    # drop any device whose console name didn't split cleanly to its topology
    # hostname, so it got no task and no error.)
    SERIAL_WORKERS = {
        'vrouter':  configure_vjunos_serial,
        'vswitch':  configure_vjunos_serial,
        'vscapa':   configure_vscapa_serial,
        # vmm3 vbrackla's RE is '{host}_RE0' (not the old '-vBrackla_RE0'), and it
        # takes the same re0:mgmt-0 baseline as vscapa, so it reuses that worker.
        'vbrackla': configure_vscapa_serial,
        # vbalerion / vardbeg / vbowmore are all EVO PTX REs with re0:mgmt-0
        # mgmt and the same '{name}_RE0' console convention as vscapa, so they
        # reuse that worker. (vardbeg comes up via EVOvArdbegRE, vbowmore via
        # the stock EVOVPTX_RE0_* macros - both land on the same console.)
        'vbalerion': configure_vscapa_serial,
        'vardbeg':   configure_vscapa_serial,
        'vbowmore':  configure_vscapa_serial,
    }

    # The vmx-family types all use configure_vmx_serial but each takes a
    # slightly different baseline (fxp0 vs em0, FPC3 pic-mode only on vmx).
    VMX_FAMILY_BASELINE = {
        'vmx':        VMX_BASELINE_LINES,
        'vferrari':   VFERRARI_BASELINE_LINES,
        'valfaromeo': VALFAROMEO_BASELINE_LINES,
        'vhamilton':  VHAMILTON_BASELINE_LINES,
        # vMaserati is the same vMX10004 RE as vHamilton (RE-TVP, em0 mgmt),
        # only the linecard differs, so it reuses the vHamilton baseline.
        'vmaserati':  VHAMILTON_BASELINE_LINES,
    }

    # vqfx is the only type configured over telnet, so it is the only one that
    # needs its console host/port from 'vmm serial'. Only run that discovery
    # (which aborts if it returns nothing) when the lab actually has a vqfx.
    telnet_endpoints = {}
    if any(vm.get('type') == 'vqfx' for vm in topology_data.get('vms', [])):
        telnet_endpoints = {n: (ip, port) for n, ip, port in get_vmx_nodes()}

    # Build the per-device plan up front so it can be shown before work starts.
    # Each entry carries a zero-arg thunk with debug already bound; the serial
    # workers all accept a `debug` kwarg (streams the console dialogue), vqfx
    # does not.
    debug = args.debug
    plan = []          # (hostname, vtype, thunk)
    skipped = []       # (hostname, vtype, reason)
    for vm in topology_data.get('vms', []):
        host = vm['hostname']
        vtype = vm.get('type')
        interfaces = vm.get('interfaces', [])
        if vtype == 'vbugatti':
            # vBugatti (MX304) gets the vJunosRouter init config. The template
            # emits VMX304_RE_START(<hostname>-re0, 0), so its console is
            # '{host}-re0' - NOT the '{host}_RE' the rest of the vmx family
            # uses. Getting this wrong makes the serial login sit until it
            # times out and the RE never resolves in 'vmm ping'.
            plan.append((host, vtype, functools.partial(
                configure_vjunos_serial, host, interfaces, debug=debug,
                re_name=f"{host}-re0")))
        elif vtype in VMX_FAMILY_BASELINE:
            # valfaromeo's template emits VMX10008_RE_START(<hostname>-re0, 0),
            # so it needs the same '-re0' console override as vbugatti. vmx /
            # vferrari / vhamilton all use the default '{host}_RE'.
            plan.append((host, vtype, functools.partial(
                configure_vmx_serial, host, interfaces, VMX_FAMILY_BASELINE[vtype],
                debug=debug,
                re_name=f"{host}-re0" if vtype == 'valfaromeo' else None)))
        elif vtype in SERIAL_WORKERS:
            plan.append((host, vtype, functools.partial(
                SERIAL_WORKERS[vtype], host, interfaces, debug=debug)))
        elif vtype == 'vqfx':
            ep = telnet_endpoints.get(host)
            if ep:
                plan.append((host, vtype, functools.partial(
                    configure_vqfx, host, ep[0], ep[1], interfaces)))
            else:
                skipped.append((host, vtype, "no telnet console found via 'vmm serial'"))
        else:
            # 'server' has no Junos baseline applied in this phase.
            skipped.append((host, vtype, "no serial baseline for this type"))

    print(f"{len(plan)} device(s) to configure:")
    for host, vtype, _ in plan:
        print(f"   • {host} ({vtype})")
    for host, vtype, reason in skipped:
        print(f"   – {host} ({vtype}) skipped: {reason}")
    if not debug:
        print("(a device can take several minutes to boot; re-run with --debug to "
              "watch the serial dialogue if one appears stuck)")

    def _run(host, vtype, thunk):
        # Phase 3 already waited for these devices to answer 'vmm ping', so
        # just note the start (a slow console then shows as in-progress rather
        # than looking like the whole run has hung).
        print(f"🔧 [{host}] ({vtype}) connecting to serial console...")
        return thunk()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_run, host, vtype, thunk): host
            for host, vtype, thunk in plan
        }
        print(f"\nStarting {len(futures)} parallel configuration tasks...")
        for future in as_completed(futures):
            host = futures[future]
            try:
                print(future.result())
            except Exception as e:
                print(f"❌ [{host}] unexpected error in worker thread: {e}", file=sys.stderr)

    print("\n🎉 All configuration tasks complete.")
    print_summary_table(topology_data)
    print_capture_info_table(final_summary_mappings)
    print("\nℹ️  To view/edit the topology in a browser, start the web server (it "
          "runs in the background): python3 vmm.py -t " + args.topology + " --serve")

if __name__ == "__main__":
    main()