#!/usr/bin/env python3
import argparse
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from jnpr.junos import Device
from jnpr.junos.utils.config import Config
from jnpr.junos.utils.start_shell import StartShell
from jnpr.junos.exception import ConnectError, ConfigLoadError, CommitError, RpcError
import platform
import threading

try:
    import paramiko
except ImportError:
    print("⚠️  Warning: 'paramiko' library not found. Please run 'pip3 install paramiko' to enable sniffer configuration.", file=sys.stderr)
    paramiko = None
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
INCOMPATIBLE_TYPE_GROUPS = [
    (
        {'vscapa'}, {'vbrackla'},
        "their macro headers (common.evovscapa.defs vs. "
        "common.evovptx.defs/common.brackla.defs) redefine the same macros "
        "with conflicting values, which makes 'vmm config' fail"
    ),
    (
        {'valfaromeo'}, {'vptx', 'vscapa', 'vbrackla'},
        "vAlfaRomeo ships its own common.vptx.defs "
        "(/vmm/data/user_disks/dhahm/valfaromeo/) which defines the VPTX_* "
        "chassis macros and IF_ET_CHAN differently from the standard "
        "/vmm/data/vmm-configs/common/vmxc/common.vptx.defs. With both in one "
        "file the wrong IF_ET_CHAN wins and VALFAROMEO_CONNECT emits an "
        "invalid interface name such as 'VALFAROMEO_eth4'"
    ),
]

# -----------------------------
# Topology Validation Function
# -----------------------------

def validate_topology(data):
    """
    Performs semantic validation of the topology data from the YAML file.
    Checks for disk naming conventions, interface naming, and sequential interface usage.
    """
    errors = []
    # Use a dictionary comprehension for quick VM lookup
    vms_by_hostname = {vm['hostname']: vm for vm in data.get('vms', [])}

    # 0. Mutually Exclusive VM Type Check
    # Several profiles ship VMM macro headers that #define the same macro
    # names with conflicting values. Including two of them in one generated
    # config makes 'vmm config' fail its preprocessing step (or, worse, expand
    # to something invalid), so catch the combination here instead of letting
    # it fail later on the VMM host.
    present_types = {vm.get('type') for vm in data.get('vms', [])}
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
        if vm_type in ('vmx', 'vqfx', 'vptx', 'vferrari', 'valfaromeo'):
            # Use a single check for all supported types. The template keys off
            # this prefix to decide how the '#define' is emitted, so it is not
            # merely cosmetic.
            if not disk_alias.startswith(vm_type):
                errors.append(f"VM '{vm['hostname']}' (type: {vm_type}) uses disk '{disk_alias}'. Disk alias must start with '{vm_type}'.")

    # Prepare for interface checks
    interface_patterns = {
        'vrouter': re.compile(r'^ge-0/0/\d+$'),
        'vswitch': re.compile(r'^ge-0/0/\d+$'),
        'vqfx': re.compile(r'^xe-0/0/\d+$'),
        'server': re.compile(r'^em\d+$'),
        'vscapa': re.compile(r'^et-0/0/\d+$'),
        'vptx' : re.compile(r'^et-0/0/\d+:[0-3]$'), # vptx check: enforces et-0/0/PORT:SUB where SUB is 0-3
        # vbrackla's IF_ET macro only takes fpc/pic/port (no subport arg), but
        # the interface is actually exposed to Junos as et-1/0/<port>:0 - the
        # subport is fixed at 0, never channelized further.
        'vbrackla': re.compile(r'^et-1/0/\d+:0$'),
        # vFerrari: 5 fixed 100G ports on FPC0, not channelized. Emitted as
        # VMX_CONNECT(ET(fpc,pic,port,0), ...) - same macro family as vmx.
        'vferrari': re.compile(r'^et-0/0/[0-4]$'),
        # vAlfaRomeo: 4 ports x 4 channelized subports per FPC, on FPC0 and
        # FPC1. Emitted as VALFAROMEO_CONNECT(IF_ET_CHAN(fpc,pic,port,subport)),
        # one VALFAROMEO_FPC block per FPC used.
        'valfaromeo': re.compile(r'^et-[01]/0/[0-3]:[0-3]$'),
    }
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
        #   vmx        - see VMX_INTERFACE_CATALOG (sparse, multi-FPC)
        #   vferrari   - et-0/0/0 .. et-0/0/4
        #   valfaromeo - et-0/0/0:0 .. et-0/0/3:3
        if vm_type in ('vmx', 'vferrari', 'valfaromeo'):
            continue

        numbers = []
        is_vscapa = (vm_type == 'vscapa')
        is_vptx = (vm_type == 'vptx')
        
        try:
            if is_vptx:
                # vptx (et-0/0/PORT:SUB) - Combine PORT and SUB into a single index for sequencing
                # Index = (Port Index * 4) + Sub-Interface Index (since 4 sub-interfaces: 0, 1, 2, 3)
                for iface in ifaces:
                    # Extracts components based on '-', '/' and ':' delimiters.
                    # NB: the '-' must be first (or escaped) in the character
                    # class - '[/-:]' is a *range* (/ 0x2F .. : 0x3A) that
                    # matches every digit, which silently broke this check.
                    parts = re.split(r'[-/:]', iface)
                    port = int(parts[3]) # The PORT number (e.g., 0 in et-0/0/0:0)
                    sub = int(parts[4])  # The SUB number (e.g., 0 in et-0/0/0:0)
                    
                    numbers.append(port * 4 + sub)
                numbers.sort()
            
            elif vm_type == 'vbrackla':
                # vbrackla (et-1/0/PORT:0) - subport is always 0, so only the
                # port index needs to be sequential. Strip the ":0" before
                # converting to int.
                numbers = sorted([int(iface.split('/')[-1].split(':')[0]) for iface in ifaces])

            elif vm_type in ['vrouter', 'vswitch', 'vmx', 'vqfx', 'vscapa']:
                # Standard interfaces: extract the last number (the port index)
                # Split by '/' and take the last element, then convert to int.
                numbers = sorted([int(iface.split('/')[-1]) for iface in ifaces])

            elif vm_type == 'server':
                # Server interfaces: extract number after 'em'
                numbers = sorted([int(iface[2:]) for iface in ifaces if iface.startswith('em')])
        
        except (ValueError, IndexError):
            # Should only happen if regex validation fails to catch a malformed string
            continue 

        # --- Sequential Check ---
        if numbers:
            if is_vscapa:
                # vscapa check: must use sequential odd numbers starting at 1 (1, 3, 5, ...)
                expected_sequence = [1 + 2 * i for i in range(len(numbers))]
                if numbers != expected_sequence:
                    errors.append(f"Interface numbering for vscapa '{hostname}' must start at 'et-0/0/1' and use sequential odd numbers. Expected port indices {expected_sequence}, but found {numbers}.")
            
            elif is_vptx:
                # vptx check: combined index must be sequential starting from 0 (0, 1, 2, 3, ...)
                start_index = 0
                expected_sequence = list(range(start_index, start_index + len(numbers)))
                if numbers != expected_sequence:
                    errors.append(f"Channelized interface numbering for vptx '{hostname}' must start at 'et-0/0/0:0' and be sequential. Expected combined indices {expected_sequence}, but found {numbers}.")
            
            else: # All other standard types (vrouter, vswitch, vmx, vqfx, server)
                # Start index is 1 for 'server' (em1), 0 for others (ge-0/0/0, etc.)
                start_index = 1 if vm_type == 'server' else 0
                expected_sequence = list(range(start_index, start_index + len(numbers)))
                
                if numbers != expected_sequence:
                    if vm_type == 'server':
                         errors.append(f"Interface numbering for server '{hostname}' must start at 'em1' and be sequential. Expected port indices {expected_sequence}, but found {numbers}.")
                    else:
                         errors.append(f"Interface numbering for device '{hostname}' must start at index {start_index} and be sequential. Expected port indices {expected_sequence}, but found {numbers}.")

    # Final error reporting
    if errors:
        print("\n" + "="*60)
        print(" 🕵️‍♂️ YAML Topology Validation Failed")
        print("="*60)
        print("Please correct the following errors in your topology file:\n")
        for error in sorted(list(set(errors))): # Use set to remove duplicate error messages
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
# Add Sniffers Function
# -----------------------------
def add_sniffers_to_topology(data,quiet=False):
    """
    Finds a dedicated sniffer VM and re-plumbs the topology to place it
    in-line on links marked with 'sniffer: true'.
    """
    sniffer_vm_name = None
    if 'sniffer_disk' not in data.get('disks', {}):
        print("⚠️  Warning: A link is marked for sniffing, but 'sniffer_disk' is not defined. No sniffers will be added.")
        return data, None, []

    for vm in data.get('vms', []):
        if vm.get('disk') == 'sniffer_disk':
            sniffer_vm_name = vm.get('hostname')
            break
    
    if not sniffer_vm_name:
        print("⚠️  Warning: A link is marked for sniffing, but no VM using 'sniffer_disk' was found in the topology. No sniffers will be added.")
        return data, None, []
    if not quiet:
        print(f"✅ Sniffer mode enabled")
    
    original_links = data.get("links", [])
    new_links = []
    capture_mappings = []
    sniffer_iface_idx = 1

    for link in original_links:
        endpoints = link.get("endpoints", [])
        if link.get('sniffer') and len(endpoints) == 2:
            ep1, ep2 = endpoints[0], endpoints[1]
            
            sniffer_iface1 = f"eth{sniffer_iface_idx}"
            sniffer_iface2 = f"eth{sniffer_iface_idx + 1}"
            
            new_links.append({'endpoints': [ep1, f"{sniffer_vm_name}:{sniffer_iface1}"]})
            new_links.append({'endpoints': [ep2, f"{sniffer_vm_name}:{sniffer_iface2}"]})
            
            capture_mappings.append({
                "link": f"{ep1} <--> {ep2}",
                "capture_point": f"{sniffer_vm_name} ({sniffer_iface1} <--> {sniffer_iface2})", "ifaces": [sniffer_iface1, sniffer_iface2]
            })
            
            sniffer_iface_idx += 2
        else:
            new_links.append(link)

    data['links'] = new_links
    return data, sniffer_vm_name, capture_mappings
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

    # Process sniffers: this modifies the 'data' dictionary for VMM config generation
    # and returns mappings of original links to sniffer points.
    sniffer_vm_name = None
    capture_mappings = []
    if any(link.get('sniffer') for link in data.get('links', [])):
        data, sniffer_vm_name, capture_mappings = add_sniffers_to_topology(data, quiet=True)

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

    # --- Build the comprehensive summary list for the output table ---
    sniffed_link_map = {m['link']: m['capture_point'] for m in capture_mappings}
    final_summary_mappings = []
    for link in original_links_list:
        endpoints = link.get("endpoints", [])
        if len(endpoints) == 2:
            link_str = f"{endpoints[0]} <--> {endpoints[1]}"
            # Use sniffer info if available, otherwise an empty string as requested
            capture_point = sniffed_link_map.get(link_str, "")
            final_summary_mappings.append({'link': link_str, 'capture_point': capture_point})

    # --- Finalize data and generate the VMM config file ---
    all_vm_types = {vm.get('type') for vm in data.get('vms', [])}
    data['types'] = list(all_vm_types)

    env = Environment(loader=FileSystemLoader("."), trim_blocks=True, lstrip_blocks=True)
    template = env.get_template("lab_template.j2")
    output = template.render(data)

    with open(output_file, "w") as f:
        f.write(output)
    if not quiet:
        print(f"✅ {output_file} generated successfully!")
    
    # --- Return the processed data and the new comprehensive summary list ---
    return data, final_summary_mappings, capture_mappings
# -----------------------------
# Apply VMM config and start lab
# -----------------------------

def run_vmm_config(config_file):
    """Applies the VMM config and starts the lab."""
    print("\n" + "="*50)
    print(" 🚀 Phase 2: Starting the Lab")
    print("="*50)
    try:
        print("Perfoming VMM unbind")
        subprocess.run(["vmm", "unbind"],stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Applying vmm config!")
        subprocess.run(["vmm", "config", config_file, "-g", "vmm-default"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"❌ Failed to apply VMM config: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(["vmm", "start"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ VMM lab started!")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"❌ Failed to start VMM lab: {e}", file=sys.stderr)
        sys.exit(1)
# -----------------------------
# Monitor devices with 'vmm ping'
# -----------------------------
def monitor_vms(devices, timeout=900, stall_timeout=60, poll_interval=5):
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

    total = len(targets)
    start_time = time.time()
    last_alive_count = -1
    last_progress_time = start_time
    bar_length = 40

    def draw(alive_count):
        pct = alive_count / total
        filled = int(bar_length * pct)
        bar = "█" * filled + "-" * (bar_length - filled)
        sys.stdout.write(f"\r[{bar}] {alive_count}/{total} Booted ({pct:.0%})   ")
        sys.stdout.flush()

    def report_pending(header, pending):
        print(header)
        for name in sorted(pending):
            print(f"   - {name}  (device {targets[name]})")
        print("\n➡️  Continuing to the configuration phase for the devices that "
              "are up; serial login will keep retrying the rest.")

    try:
        while True:
            ping_map = get_vmm_ping_map()
            alive = {name for name in targets if ping_map.get(name, "").lower() == "alive"}
            draw(len(alive))

            if len(alive) == total:
                print("\n\n✅ All devices booted and reachable!")
                return

            if len(alive) != last_alive_count:
                last_alive_count = len(alive)
                last_progress_time = time.time()

            pending = set(targets) - alive
            if alive and (time.time() - last_progress_time) > stall_timeout:
                report_pending(
                    f"\n\n⏳ No further progress for {stall_timeout}s "
                    f"({len(alive)}/{total} reachable). Still waiting on:", pending)
                return

            if time.time() - start_time > timeout:
                report_pending(
                    f"\n\n⏰ Timeout reached ({timeout}s). Not reachable:", pending)
                return

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n\n\U0001f6d1 Monitoring stopped by user.", file=sys.stderr)
        sys.exit(1)


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
            "login:",            # 0
            "Password:",         # 1
            "Login incorrect",   # 2
            r"%",                # 3  FreeBSD shell
            r"root@[^\r\n]*# ",  # 4  Linux root shell
            r">\s",              # 5  Junos operational mode
            r"#\s",              # 6  already in configuration mode
            pexpect.TIMEOUT,     # 7
            pexpect.EOF,         # 8
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
    if state in (3, 4):          # FreeBSD % or Linux root shell -> enter cli
        child.sendline("cli")
        child.expect(r">\s", timeout=cli_timeout)
        child.sendline("edit")
        child.expect(r"#\s", timeout=cli_timeout)
    elif state == 5:             # Junos operational '>' -> edit
        child.sendline("edit")
        child.expect(r"#\s", timeout=cli_timeout)
    # state == 6: already at '# '
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
    child.expect(r"# ")

# -----------------------------
# Configure a vrouter/vswitch
# -----------------------------
def configure_vjunos_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Applies a baseline configuration to a single device via serial (vmm serial).
    """

    cmd = f"vmm serial -t {name}"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    try:
        child = spawn()
        child = _junos_serial_login(child, name, spawn, debug=debug)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
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
        send_and_expect("commit and-quit", prompt=r"> ", timeout=120)
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



# -----------------------------
# Configure a vtpx 
# -----------------------------

def configure_vptx_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Applies a baseline configuration to a single device via serial (vmm serial).
    """

    cmd = f"vmm serial -t {name}_RE"

    def spawn():
        return _spawn_serial_with_retry(cmd, name, debug=debug, retries=retries, delay=delay)

    try:
        child = spawn()
        child = _junos_serial_login(child, name, spawn, debug=debug)

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
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
            "set system services ssh root-login allow",
            "set system services ssh sftp-server",
            "set system services netconf ssh",
            "set system management-instance",
            "set protocols lldp interface all",
            "set protocols lldp interface em0 disable",
            "set chassis aggregated-devices ethernet device-count 10",
            "delete groups member0",
            "set interfaces em0.0 family inet dhcp",
            "delete groups member0",
        ]
        for c in commands:
            send_and_expect(c)

        # --- Commit & exit ---
        send_and_expect("commit and-quit", prompt=r"> ", timeout=120)
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
VFERRARI_BASELINE_LINES = _vmx_baseline("fxp0", include_fpc3_picmode=False) + [
    # vFerrari-specific: required forwarding mode for this profile.
    "set forwarding-options hyper-mode",
]


def configure_vmx_serial(name, interfaces, baseline=None, debug=False, retries=15, delay=10):
    """
    Configure a vmx-family device (vmx, vFerrari, vAlfaRomeo) over its serial
    console. All three expose their RE as '{hostname}_RE' and take the same
    baseline; `baseline` selects the variant (vAlfaRomeo manages on em0 rather
    than fxp0, so it passes VALFAROMEO_BASELINE_LINES).

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

    cmd = f"vmm serial -t {name}_RE"

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
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
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
        send_and_expect("commit and-quit", prompt=r"> ", timeout=300)
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
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
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
            child.expect(r"# ", timeout=30)
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
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
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
            child.expect(r"# ", timeout=30)
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


def upload_sniffer_script(capture_mappings):
    """
    Finds the sniffer VM's IP, uploads the bridging script, and executes it
    for each sniffed link to create the necessary bridges.
    """
    if not paramiko:
        return "⚠️  Skipping sniffer configuration: 'paramiko' library is not installed."

    # Filter for only the mappings that have sniffer interfaces defined
    sniffed_links = [m for m in capture_mappings if 'ifaces' in m and m['ifaces']]
    if not sniffed_links:
        logging.info("No links marked for sniffing. Skipping sniffer configuration.")
        return "✅ No sniffed links to configure."

    logging.info("🚀 Locating and configuring sniffer VM via SSH...")
    
    # --- Step 1 & 2: Get Sniffer IP ---
    result = subprocess.run(["vmm", "ping"], capture_output=True, text=True, check=False)
    sniffer_ip = None
    if result.stdout:
        for line in result.stdout.splitlines():
            if "Sniffer" in line and "alive" in line:
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    sniffer_ip = match.group(1)
                    logging.info(f"✅ Sniffer VM found at IP: {sniffer_ip}")
                    break
    
    # --- Step 3: If sniffer IP wasn't found, fail with a clear error ---
    if not sniffer_ip:
        error_message = "❌ Failed to find an 'alive' Sniffer VM from 'vmm ping' output."
        logging.error(error_message)
        if result.stderr:
            logging.error(f"   'vmm ping' error: {result.stderr.strip()}")
        return f"Failure: {error_message}"

    # --- Step 4: Proceed with SSH, now that we have a valid IP ---
    local_script_path = SNIFFER_BRIDGE_SCRIPT
    remote_script_path = "/root/br.sh"

    if not os.path.exists(local_script_path):
        return f"❌ Error: Sniffer script not found at {local_script_path}"

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(sniffer_ip, username=DEVICE_ROOT_USER, password=DEVICE_ROOT_PASSWORD, timeout=20)
        
        sftp = ssh.open_sftp()
        sftp.put(local_script_path, remote_script_path)
        sftp.close()
        
        # Make script executable
        stdin, stdout, stderr = ssh.exec_command(f"chmod +x {remote_script_path}")
        if stdout.channel.recv_exit_status() != 0:
            ssh.close()
            return f"❌ Failed to make sniffer script executable. Error: {stderr.read().decode().strip()}"
        
        # Loop through sniffed links and create a bridge for each
        bridge_idx = 1
        bridge_errors = []
        for link_info in sniffed_links:
            iface1, iface2 = link_info['ifaces']
            bridge_name = f"br{bridge_idx}"
            command = f"{remote_script_path} {bridge_name} {iface1} {iface2}"
            logging.info(f"   - Executing on sniffer: '{command}'")
            
            stdin, stdout, stderr = ssh.exec_command(command)
            if stdout.channel.recv_exit_status() != 0:
                error_output = stderr.read().decode().strip()
                bridge_errors.append(f"   - Failed on bridge {bridge_name}: {error_output}")
            bridge_idx += 1
        
        ssh.close()

        if not bridge_errors:
            return f"✅ Successfully configured {bridge_idx - 1} sniffer bridge(s) on {sniffer_ip}"
        else:
            return f"❌ Failed to configure some sniffer bridges:\n" + "\n".join(bridge_errors)

    except Exception as e:
        logging.error(f"❌ An SSH or Paramiko error occurred: {e}", exc_info=True)
        return f"Failure: Could not configure sniffer due to SSH error ({e})"
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
        # type-specific: '{name}_RE' for vmx/vptx, '{name}_RE0' for vscapa,
        # and '{name}-vBrackla_RE0' for vbrackla (see PTX_CHAS_NAME in the
        # template). Everything else is keyed on the plain hostname.
        lookup_name = (
        f"{name}_RE" if vm_type in ["vmx", "vptx", "vferrari", "valfaromeo"]
        else f"{name}_RE0" if vm_type == "vscapa"
        else f"{name}-vBrackla_RE0" if vm_type == "vbrackla"
        else name)
        status = ping_data.get(lookup_name, {})
        state = status.get('state', 'unknown')
        ip = status.get('ip', 'N/A')

        row_data = { "Name": name, "Type": vm_type, "Image Path": image_display, "State": state, "IPv4 Address": ip }
        print(row_format.format(**row_data))
           
    
    print(separator)
# -----------------------------
# Print Capture Info Table
# -----------------------------
def print_capture_info_table(capture_mappings):
    """Prints a table mapping all links to their capture points (sniffers or bridges)."""
    if not capture_mappings:
        return

    columns = { "Link": 60, "Capture Point": 30 }
    header_format = "| " + " | ".join([f"{{{key}:<{width}}}" for key, width in columns.items()]) + " |"
    row_format = "| " + " | ".join([f"{{{key}:<{width}}}" for key, width in columns.items()]) + " |"
    separator = "+" + "+".join(["-" * (width + 2) for width in columns.values()]) + "+"
    table_width = len(separator)

    print("\n" + "="*table_width)
    print(" Link & Capture Point Summary ".center(table_width))
    print("="*table_width)
    print(separator)
    print(header_format.format(**{key: key for key in columns.keys()}))
    print(separator)

    for mapping in capture_mappings:
        row_data = { "Link": mapping['link'], "Capture Point": mapping['capture_point'] }
        print(row_format.format(**row_data))
    
    print(separator)
# -----------------------------
# Configuration Management Functions
# -----------------------------
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
                if not "_FPC" in name and not "pecosim" in name and re.search(r'vJunos-router|vJunos-switch|junos-virtual-install|junos-x86|vqfx|vmx|junos-evo-install-ptx|junos-virtual', image, re.IGNORECASE):
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

        print(f"✅ Found {len(ips)} active devices.")
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
# 'server'/'sniffer'). Used both to decide who to wait for in Phase 3 and who
# to configure in Phase 4.
CONFIGURABLE_TYPES = (
    'vrouter', 'vswitch', 'vptx', 'vmx', 'vferrari', 'valfaromeo',
    'vscapa', 'vbrackla', 'vqfx',
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
    """Run 'vmm ping' and return {node_name: state} (e.g. {'PE1_RE': 'alive'})."""
    try:
        result = subprocess.run(["vmm", "ping"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return {}
    ping_map = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            ping_map[parts[0]] = parts[2]
    return ping_map


def re_ping_name(host, vtype):
    """The name a device's RE appears under in 'vmm ping' (see the RE naming in
    lab_template.j2 and print_summary_table)."""
    if vtype in ('vmx', 'vptx', 'vferrari', 'valfaromeo'):
        return f"{host}_RE"
    if vtype == 'vscapa':
        return f"{host}_RE0"
    if vtype == 'vbrackla':
        return f"{host}-vBrackla_RE0"
    return host


# -----------------------------
# Main workflow
# -----------------------------
def main():
    print("Install the required libraries if not already done:please use --> pip3 install pyyaml jinja2  junos-eznc paramiko")
    parser = argparse.ArgumentParser(description="A tool to deploy and configure a VMM lab from a YAML topology.")
    parser.add_argument("-t", "--topology", default="topo.yml", help="Path to the YAML topology file (default: topo.yml)")
    parser.add_argument("-o", "--output", default="lab_topology.conf", help="Name of the output configuration file")
    parser.add_argument("--lab_detail", action="store_true", help="Display lab summary table and exit.")
    parser.add_argument("--config", action="store_true", help="Enter configuration management mode.")
    parser.add_argument("--config_file_only", action="store_true", help="Generate the VMM config file only and exit.")
    parser.add_argument("--skip_boot_wait", action="store_true",
                        help="Skip the Phase 3 ping wait entirely and go straight to configuration. "
                             "Safe because devices are configured over the serial console, which "
                             "has its own boot/login retry handling.")
    parser.add_argument("--boot_wait", type=int, default=900, metavar="SECONDS",
                        help="Hard cap on the Phase 3 ping wait (default: 900).")
    parser.add_argument("--debug", action="store_true",
                        help="Stream the full serial console dialogue for each device during "
                             "Phase 4. Use this when a device appears stuck to see exactly where "
                             "its login/commit is waiting.")
    args = parser.parse_args()

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
    run_vmm_config(args.output)
    
    if args.skip_boot_wait:
        print("\n⏭️  Skipping the boot wait (--skip_boot_wait); serial consoles "
              "retry on their own until each device is ready.")
    else:
        monitor_vms(configurable_devices(topology_data), timeout=args.boot_wait)


    time.sleep(5)
    result = upload_sniffer_script(capture_mappings)
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
        'vptx':     configure_vptx_serial,
        'vscapa':   configure_vscapa_serial,
        'vbrackla': configure_vbrackla_serial,
    }

    # The vmx-family types all use configure_vmx_serial but each takes a
    # slightly different baseline (fxp0 vs em0, FPC3 pic-mode only on vmx).
    VMX_FAMILY_BASELINE = {
        'vmx':        VMX_BASELINE_LINES,
        'vferrari':   VFERRARI_BASELINE_LINES,
        'valfaromeo': VALFAROMEO_BASELINE_LINES,
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
        if vtype in VMX_FAMILY_BASELINE:
            plan.append((host, vtype, functools.partial(
                configure_vmx_serial, host, interfaces, VMX_FAMILY_BASELINE[vtype], debug=debug)))
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
            # server / sniffer have no Junos baseline applied in this phase.
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

if __name__ == "__main__":
    main()