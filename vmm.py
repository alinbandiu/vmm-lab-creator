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

    # 1. Disk Naming Convention Check
    for vm in data.get('vms', []):
        vm_type = vm.get('type')
        disk_alias = vm.get('disk')
        if vm_type in ('vmx', 'vqfx', 'vptx'):
            # Use a single check for all supported types
            if not disk_alias.startswith(vm_type):
                errors.append(f"VM '{vm['hostname']}' (type: {vm_type}) uses disk '{disk_alias}'. Disk alias must start with '{vm_type}'.")

    # Prepare for interface checks
    interface_patterns = {
        'vrouter': re.compile(r'^ge-0/0/\d+$'),
        'vswitch': re.compile(r'^ge-0/0/\d+$'),
        'vmx': re.compile(r'^ge-0/0/\d+$'),
        'vqfx': re.compile(r'^xe-0/0/\d+$'),
        'server': re.compile(r'^em\d+$'),
        'vscapa': re.compile(r'^et-0/0/\d+$'),
        'vptx' : re.compile(r'^et-0/0/\d+:[0-3]$') # vptx check: enforces et-0/0/PORT:SUB where SUB is 0-3
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
                    if vm_type in interface_patterns:
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

        numbers = []
        is_vscapa = (vm_type == 'vscapa')
        is_vptx = (vm_type == 'vptx')
        
        try:
            if is_vptx:
                # vptx (et-0/0/PORT:SUB) - Combine PORT and SUB into a single index for sequencing
                # Index = (Port Index * 4) + Sub-Interface Index (since 4 sub-interfaces: 0, 1, 2, 3)
                for iface in ifaces:
                    # Extracts components based on '/' and ':' delimiters
                    parts = re.split(r'[/-:]', iface) 
                    port = int(parts[3]) # The PORT number (e.g., 0 in et-0/0/0:0)
                    sub = int(parts[4])  # The SUB number (e.g., 0 in et-0/0/0:0)
                    
                    numbers.append(port * 4 + sub)
                numbers.sort()
            
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
        subprocess.run(["vmm", "config", config_file, "-g", "vmm-default"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Applying vmm config!")
   # except (subprocess.CalledProcessError, FileNotFoundError) as e:
     #   print(f"❌ Failed to apply VMM config: {e}", file=sys.stderr)
      #  sys.exit(1)

        subprocess.run(["vmm", "start"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ VMM lab started!")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"❌ Failed to start VMM lab: {e}", file=sys.stderr)
        sys.exit(1)
# -----------------------------
# Monitor VMs with vmm-ping
# -----------------------------
def monitor_vms(required_pings=25, timeout=900):
    """
    Discovers VMs using 'vmm ip', then concurrently pings them until they are stable.
    A VM is declared 'stable' after it has been reported as 'alive' for a set
    number of consecutive checks. Displays a single-line progress bar.
    
    Args:
        required_pings (int): Number of successful, consecutive pings required.
        timeout (int): Time in seconds before the function gives up and exits.
    """

    # --- Nested Helper Function for Threading ---
    def ping_worker(ip, progress_dict, lock):
        """
        Continuously pings a single IP and updates a shared dictionary with the
        count of consecutive successful pings. This function is designed to be
        run in a separate thread.
        """
        # Ping command for Linux: -c 1 (send 1 packet), -W 2 (wait 2s for reply)
        command = ["ping", "-c", "1", "-W", "2",ip]

        while progress_dict.get(ip, 0) < required_pings:
            try:
                # Run the ping command, hiding its output
                result = subprocess.run(command, capture_output=True, check=False)
                
                # Use a lock to safely update the shared dictionary
                with lock:
                    if result.returncode == 0:
                        # Successful ping: increment the streak
                        progress_dict[ip] += 1
                    else:
                        # Failed ping: reset the streak
                        progress_dict[ip] = 0
            except Exception:
                # In case of any other error, reset the streak
                with lock:
                    progress_dict[ip] = 0
            
            time.sleep(1) # Wait 1 second before the next ping

    # --- Main Function Logic ---
    print("\n" + "="*50)
    print(" ⏳ Phase 3: Waiting for VMs to become reachable")
    print("="*50)

    # 1. Discover Target IPs using 'vmm ip'
    print("📡 Waiting for VMs to boot'...")
    target_ips = []
    try:
        result = subprocess.run(
            ["vmm", "ip"],
            capture_output=True,
            text=True,
            check=True
        )
        
        ip_pattern = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and ip_pattern.match(parts[1]):
                target_ips.append(parts[1])
                
        if not target_ips:
            print("✅ 'vmm ip' ran successfully but returned no IPs. Proceeding...")
            return
        
        print(f"Lab consist of {len(target_ips)} VMs in total")

    except FileNotFoundError:
        print("❌ Error: 'vmm' command not found. Is it installed and in your PATH?", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: 'vmm ip' command failed with return code {e.returncode}:", file=sys.stderr)
        print(e.stderr, file=sys.stderr)
        sys.exit(1)

    # 2. Set up and start pinging threads
    progress_lock = threading.Lock()
    progress_data = {ip: 0 for ip in target_ips}
    
    for ip in target_ips:
        # The worker thread is given the target function, ip, and shared resources
        thread = threading.Thread(
            target=ping_worker,
            args=(ip, progress_data, progress_lock),
            daemon=True  # Allows main program to exit even if threads are running
        )
        thread.start()

    # 3. Display progress and check for completion or timeout
    start_time = time.time()
    total_ips = len(target_ips)

    try:
        while True:
            # Safely read the current progress from the shared dictionary
            with progress_lock:
                stable_count = sum(1 for count in progress_data.values() if count >= required_pings)

            # Draw the progress bar
            percentage = stable_count / total_ips
            bar_length = 40
            filled_length = int(bar_length * percentage)
            bar = "█" * filled_length + "-" * (bar_length - filled_length)
            
            sys.stdout.write(f"\r[{bar}] {stable_count}/{total_ips} Booted ({percentage:.0%})   ")
            sys.stdout.flush()

            # Check for success condition
            if stable_count == total_ips:
                print("\n\n✅ Devices booted up, checking reachabilty....!")
                return
            
            # Check for timeout condition
            if time.time() - start_time > timeout:
                print(f"\n\n⏰ Timeout reached ({timeout}s). The following IPs did not stabilize:")
                with progress_lock:
                    for ip, count in sorted(progress_data.items()):
                        if count < required_pings:
                            print(f"   - {ip} (Ping Streak: {count}/{required_pings})")
                sys.exit(1)

            time.sleep(1) # Update the progress bar every second

    except KeyboardInterrupt:
        print("\n\n🛑 Monitoring stopped by user.", file=sys.stderr)
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
            nodes_info.append((base_name, ip, port))
            #print(f"   - Found node: {name} (as {base_name}) at {ip}:{port}")
    
    if not nodes_info:
        print("❌ Error: 'vmm serial' returned no configurable nodes.", file=sys.stderr)
        sys.exit(1)
    return nodes_info
# -----------------------------
# Configure a vrouter/vswitch
# -----------------------------
def configure_vjunos_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Applies a baseline configuration to a single device via serial (vmm serial).
    """

    cmd = f"vmm serial -t {name}"

    # --- Spawn helper with retries ---
    def spawn_with_retry():
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

    try:
        child = spawn_with_retry()
        child.sendline("")
        time.sleep(1)

        # --- Login / boot handling ---
        max_boot_retries = 30
        attempts = 0
        while attempts < max_boot_retries:
            attempts += 1
            idx = child.expect([
                "login:",
                "Password:",
                r"%",
                r"root@.*# ",
                r"> ",
                r"# ",
                pexpect.TIMEOUT,
                pexpect.EOF
            ], timeout=30)

            if debug:
                print(f"[{name}] idx={idx} matched={child.after.strip()!r}")

            if idx == 0:  # login
                child.sendline("root")
            elif idx == 1:  # password
                child.sendline("Embe1mpls")
            elif idx == 2:  # FreeBSD shell %
                child.sendline("cli")
            elif idx == 3:  # Linux root shell
                child.sendline("cli")
                child.expect(r"> ")
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 4:  # Junos operational >
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 5:  # already in config mode
                break
            elif idx == 6:  # timeout
                child.sendline("")
            elif idx == 7:  # EOF
                if debug:
                    print(f"[{name}] EOF before config detected, retrying...")
                child.close(force=True)
                child = spawn_with_retry()
        else:
            raise Exception(f"[{name}] Could not reach config mode after {max_boot_retries} attempts")

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password
        child.sendline("set system root-authentication plain-text-password")
        child.expect("New password:")
        child.sendline("Embe1mpls")
        child.expect(["Retype new password:", "Re-enter password:"])
        child.sendline("Embe1mpls")
        child.expect(r"# ")

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

    # --- Spawn helper with retries ---
    def spawn_with_retry():
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

    try:
        child = spawn_with_retry()
        child.sendline("")
        time.sleep(1)

        # --- Login / boot handling ---
        max_boot_retries = 30
        attempts = 0
        while attempts < max_boot_retries:
            attempts += 1
            idx = child.expect([
                "login:",
                "Password:",
                r"%",
                r"root@.*# ",
                r"> ",
                r"# ",
                pexpect.TIMEOUT,
                pexpect.EOF
            ], timeout=30)

            if debug:
                print(f"[{name}] idx={idx} matched={child.after.strip()!r}")

            if idx == 0:  # login
                child.sendline("root")
            elif idx == 1:  # password
                child.sendline("Embe1mpls")
            elif idx == 2:  # FreeBSD shell %
                child.sendline("cli")
            elif idx == 3:  # Linux root shell
                child.sendline("cli")
                child.expect(r"> ")
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 4:  # Junos operational >
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 5:  # already in config mode
                break
            elif idx == 6:  # timeout
                child.sendline("")
            elif idx == 7:  # EOF
                if debug:
                    print(f"[{name}] EOF before config detected, retrying...")
                child.close(force=True)
                child = spawn_with_retry()
        else:
            raise Exception(f"[{name}] Could not reach config mode after {max_boot_retries} attempts")

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password
        child.sendline("set system root-authentication plain-text-password")
        child.expect("New password:")
        child.sendline("Embe1mpls")
        child.expect(["Retype new password:", "Re-enter password:"])
        child.sendline("Embe1mpls")
        child.expect(r"# ")

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
        tn.write(b"root\n")
        tn.read_until(b"Password:",timeout=5)
        tn.write(b"Embe1mpls\n")
        tn.read_until(b"% ", timeout=20)
        tn.write(b"cli\n")
        tn.read_until(b"> ", timeout=10)
        tn.write(b"edit\n")
        tn.read_until(b"# ", timeout=10)
        
        tn.write(f"set system host-name {name}\n".encode('ascii'))
        tn.read_until(b"# ", timeout=10)
        tn.write(b"set system root-authentication plain-text-password\n")
        tn.read_until(b"New password: ", timeout=10)
        tn.write(b"Embe1mpls\n")
        tn.read_until(b"Retype new password: ", timeout=10)
        tn.write(b"Embe1mpls\n")
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
# Configure a VMX via Serial
# -----------------------------

def configure_vmx_serial(name, interfaces, debug=False, retries=15, delay=10):
    """
    Configures a vMX RE via serial console in serial mode.
    Returns only success/failure.
    Automatically retries if the serial console is not ready (EOF before config).
    
    Args:
        name (str): Name of the RE (e.g., P1)
        debug (bool): If True, prints all serial output and matched prompts
        retries (int): Number of times to retry if the console is not ready
        delay (int): Seconds to wait between retries
    """
    cmd = f"vmm serial -t {name}_RE"

    # --- Helper to spawn with retry ---
    def spawn_with_retry():
        for attempt in range(1, retries+1):
            try:
                child = pexpect.spawn(cmd, encoding='utf-8', timeout=300)
                if debug:
                    child.logfile_read = None  # replace None with sys.stdout to see full output
                return child
            except pexpect.exceptions.EOF:
                if debug:
                    print(f"[{name}] Serial not ready, retry {attempt}/{retries}...")
                time.sleep(delay)
        raise Exception(f"Serial console {name} not ready after {retries} attempts")

    try:
        child = spawn_with_retry()
        # Wake up the console
        child.sendline("")
        time.sleep(1)

        # --- Boot/Login Handling ---
        while True:
            idx = child.expect([
                "login:",
                "Password:",
                r"%",
                r"root@.*# ",
                r"> ",
                r"# ",
                pexpect.TIMEOUT,
                pexpect.EOF
            ], timeout=30)

            if debug:
                print(f"[{name}] idx={idx} matched={child.after.strip()!r}")

            if idx == 0:  # login
                child.sendline("root")
            elif idx == 1:  # password
                child.sendline("Embe1mpls")
            elif idx == 2:  # FreeBSD shell %
                child.sendline("cli")
            elif idx == 3:  # root shell
                child.sendline("cli")
                child.expect(r"> ")
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 4:  # Junos operational >
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 5:  # already in config mode
                break
            elif idx == 6:  # timeout
                child.sendline("")
            elif idx == 7:  # EOF
                if debug:
                    print(f"[{name}] EOF before config detected, retrying...")
                child.close(force=True)
                child = spawn_with_retry()

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# "):
            child.sendline(cmd)
            child.expect(prompt, timeout=60)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Interactive root password
        child.sendline("set system root-authentication plain-text-password")
        child.expect("New password:")
        if debug: print(f"[{name}] matched: New password:")
        child.sendline("Embe1mpls")
        child.expect("Retype new password:")
        if debug: print(f"[{name}] matched: Retype new password:")
        child.sendline("Embe1mpls")
        child.expect(r"# ")

        # Other configuration commands
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
        for iface in interfaces:
            desc_cmd = f"set interfaces {iface['name']} description \"{iface['description']}\""
            send_and_expect(desc_cmd)   


        # --- Commit & detach ---
        send_and_expect("commit and-quit", prompt=r"> ")
        child.close(force=True)

        return f"✅ Successfully configured {name}"

    except pexpect.exceptions.TIMEOUT:
        return f"Failure: {name} (Timeout)"
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

    # --- Spawn helper with retries ---
    def spawn_with_retry():
        for attempt in range(1, retries + 1):
            try:
                child = pexpect.spawn(cmd, encoding="utf-8", timeout=300)
                if debug:
                    child.logfile_read = sys.stdout
                return child
            except pexpect.exceptions.EOF:
                if debug:
                    print(f"[{name}] Serial not ready, retry {attempt}/{retries}...")
                time.sleep(delay)
        raise Exception(f"Serial console {name} not ready after {retries} attempts")

    try:
        child = spawn_with_retry()
        child.sendline("")
        time.sleep(1)

        # --- Boot/Login Handling ---
        max_boot_retries = 30
        attempts = 0
        while attempts < max_boot_retries:
            attempts += 1
            idx = child.expect(
                [
                    "login:",
                    "Password:",
                    r"%",
                    r"root@.*# ",
                    r"> ",
                    r"# ",
                    pexpect.TIMEOUT,
                    pexpect.EOF,
                ],
                timeout=30,
            )

            if debug:
                print(f"[{name}] idx={idx} matched={child.after.strip()!r}")

            if idx == 0:  # login
                child.sendline("root")
            elif idx == 1:  # password
                child.sendline("Embe1mpls")
            elif idx == 2:  # FreeBSD shell %
                child.sendline("cli")
            elif idx == 3:  # Linux root shell
                child.sendline("cli")
                child.expect(r"> ")
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 4:  # Junos operational >
                child.sendline("edit")
                child.expect(r"# ")
                break
            elif idx == 5:  # Already in config mode
                break
            elif idx == 6:  # timeout
                child.sendline("")
            elif idx == 7:  # EOF
                if debug:
                    print(f"[{name}] EOF before config detected, retrying...")
                child.close(force=True)
                child = spawn_with_retry()
        else:
            raise Exception(f"[{name}] Could not reach config mode after {max_boot_retries} attempts")

        # --- Helper to send commands ---
        def send_and_expect(cmd, prompt=r"# ", timeout=60):
            child.sendline(cmd)
            child.expect(prompt, timeout=timeout)
            if debug:
                print(f"[{name}] executed: {cmd}, matched: {child.after.strip()!r}")

        # --- Configure system ---
        send_and_expect(f"set system host-name {name}")

        # Root password (interactive)
        child.sendline("set system root-authentication plain-text-password")
        child.expect("New password:")
        if debug:
            print(f"[{name}] matched: New password:")
        child.sendline("Embe1mpls")
        child.expect(["Retype new password:", "Re-enter password:"])
        if debug:
            print(f"[{name}] matched: Retype/Re-enter password:")
        child.sendline("Embe1mpls")
        child.expect(r"# ")

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
    local_script_path = "/homes/balinfilipga/scripts/br.sh"
    remote_script_path = "/root/br.sh"

    if not os.path.exists(local_script_path):
        return f"❌ Error: Sniffer script not found at {local_script_path}"

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(sniffer_ip, username='root', password='Embe1mpls', timeout=20)
        
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
        
        # vscapa, like vmx, has a _RE component that gets the IP
        lookup_name = (
        f"{name}_RE" if vm_type in ["vmx", "vptx"]
        else f"{name}_RE0" if vm_type == "vscapa"
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
        dev = Device(host=ip, user='root', passwd='Embe1mpls')
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
        dev = Device(host=ip, user='root', passwd='Embe1mpls')
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
    
    monitor_vms()


    time.sleep(5)
    result = upload_sniffer_script(capture_mappings)
    print("\n" + "="*50)
    print(" 🔧 Phase 4: Applying Baseline Configuration")
    print("="*50) 
    print("**NOTE** This may take longer if using vSCAPA version that do not have fix for PR :1726785.\n")
    telnet_nodes_info = get_vmx_nodes()
    vms_by_hostname = {vm['hostname']: vm for vm in topology_data.get('vms', [])}   
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = []

        for name, ip, port in telnet_nodes_info:
            vm = vms_by_hostname.get(name)
            if vm and vm.get('type') in ['vrouter', 'vswitch']:
                interfaces = vm.get('interfaces', [])
                futures.append(executor.submit(configure_vjunos_serial, vm['hostname'], interfaces))
            elif vm and vm.get('type') == 'vptx':
                interfaces = vm.get('interfaces', [])
                futures.append(executor.submit(configure_vptx_serial, vm['hostname'], interfaces))
            elif vm and vm.get('type') == 'vmx':
                interfaces = vm.get('interfaces', [])
                futures.append(executor.submit(configure_vmx_serial, vm['hostname'], interfaces))
            elif vm and vm.get('type') == 'vscapa':
                interfaces = vm.get('interfaces', [])
                futures.append(executor.submit(configure_vscapa_serial, vm['hostname'], interfaces))
            elif vm and vm.get('type') == 'vqfx':
                interfaces = vm.get('interfaces', [])
                futures.append(executor.submit(configure_vqfx, name, ip, port, interfaces))                    

        print(f"Starting {len(futures)} parallel configuration tasks...")
        for future in as_completed(futures):
            try:
                result = future.result()
                print(result)
            except Exception as e:
                print(f"An unexpected error occurred in a worker thread: {e}", file=sys.stderr)

    print("\n🎉 All configuration tasks complete.")
    print_summary_table(topology_data)
    print_capture_info_table(final_summary_mappings)

if __name__ == "__main__":
    main()