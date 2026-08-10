#!/usr/bin/env python3
"""
vmm_builder.py - browser-based topology builder for the VMM lab generator.

    python3 vmm.py --build              # then open the printed URL

Pick devices, wire them port-to-port by clicking, watch the rules validate as
you go, then hit Deploy.

DESIGN RULE - read this before changing anything here
-----------------------------------------------------
The topology rules (valid ports per platform, disk-alias naming, duplicate and
sequential-numbering checks, retired types) live in ONE place: vmm.py's
collect_topology_errors() and INTERFACE_PATTERNS/PORT_CATALOG. This module must
never restate them, and the JavaScript must never restate them either.

The browser sends the topology to /api/validate and the server runs the exact
same validator the CLI runs. That is deliberate: a second copy of the rules in
JS would drift within a release, and the failure mode of these particular rules
is a silently dead link that costs a 20-minute deploy to discover.

The only thing the browser knows about ports is the list the server handed it
from PORT_CATALOG, which is itself checked against INTERFACE_PATTERNS at import.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vmm


# Default image per type, so a device dropped on the canvas is immediately
# deployable. The alias MUST start with the type name (validator rule 1); the
# two upper-case ones are the literal exceptions the template special-cases.
DEFAULT_DISKS = {
    'server':     ('server_disk',     '/vmm/data/base_disks/ubuntu/ubuntu-22.04.qcow2'),
    'vrouter':    ('VROUTER_DISK',    '/vmm/data/base_disks/junos/vmx/vJunos-router-24.2R2-S1.6.qcow2'),
    'vswitch':    ('VSWITCH_DISK',    '/vmm/data/base_disks/junos/vex/vJunos-switch-24.2R2-S1.6.qcow2'),
    'vqfx':       ('vqfx_disk',       '/vmm/data/base_disks/default_images/default_image_vqfx10.img'),
    'vmx':        ('vmx_disk',        '/vmm/data/base_disks/junos/vmx/junos-virtual-x86-64-23.4R2-S3.9.vmdk'),
    'vferrari':   ('vferrari_disk',   '/homes/balinfilipga/images/junos-virtual-x86-64-22.4R3-S4.5.vmdk'),
    'vbugatti':   ('vbugatti_disk',   '/vmm/data/base_disks/default_images/default_image_vbugatti.img'),
    'vhamilton':  ('vhamilton_disk',  '/vmm/data/base_disks/default_images/default_image_vhamilton.img'),
    'vmaserati':  ('vmaserati_disk',  '/vmm/data/base_disks/default_images/default_image_vmaserati.img'),
    'valfaromeo': ('valfaromeo_disk', '/vmm/data/base_disks/default_images/default_image_valfaromeo.img'),
    'vscapa':     ('vscapa_disk',     '/vmm/data/base_disks/default_images/default_image_vscapa.img'),
    'vardbeg':    ('vardbeg_disk',    '/vmm/data/base_disks/default_images/default_image_vardbeg.img'),
    'vbrackla':   ('vbrackla_disk',   '/vmm/data/base_disks/default_images/default_image_vbrackla.img'),
    'vbalerion':  ('vbalerion_disk',  '/vmm/data/base_disks/default_images/default_image_vbalerion.img'),
    'vbowmore':   ('vbowmore_disk',   '/vmm/data/base_disks/default_images/default_image_vbowmore.img'),
}

SNIFFER_DISK = ('sniffer_disk', '/vmm/data/base_disks/ubuntu/ubuntu-22.04.qcow2')

# Short per-type note shown in the palette. Facts only - anything surprising
# here was learned the hard way on a live device.
TYPE_NOTES = {
    'server':     'Linux host. em1 upward.',
    'vswitch':    'vJunos switch.',
    'vrouter':    'vJunos router.',
    'vqfx':       'vQFX10k.',
    'vmx':        'MX960. Multi-FPC: 0,1,2,3,5 (no FPC4).',
    'vferrari':   'ZT MPC. Needs your own .vmdk - no default image on the pods.',
    'vbugatti':   'MX304 + LC304. 16x100G.',
    'vhamilton':  'MX10004 linecard. 14 ports/FPC, FPC0-2.',
    'vmaserati':  'MX10004 "XT". Two PICs: 20 + 16 ports.',
    'valfaromeo': 'MX10008 + LC9600. Channelized 4x4 per FPC.',
    'vscapa':     'EVO PTX. ODD ports only (1,3..15).',
    'vardbeg':    'EVO PTX. 12 contiguous ports.',
    'vbrackla':   'EVO PTX. Ports live on FPC1, not FPC0.',
    'vbalerion':  'EVO PTX. Numbering starts at 9.',
    'vbowmore':   'EVO PTX. ODD ports only (1,3..15).',
}


def _default_hostname(vm_type, existing):
    """First free <type><N> name, 1-based, matching the project's convention."""
    n = 1
    while f"{vm_type}{n}" in existing:
        n += 1
    return f"{vm_type}{n}"


def _alias_for(vm_type, hostname, custom_path):
    """Disk alias for one device.

    Devices on the stock image all share the type's default alias, which keeps
    the generated 'disks:' block small and familiar. A device with its own image
    gets a private alias instead, because the alias is a cpp macro name: reusing
    the shared one would make the last device of that type silently redefine the
    image for every other device of that type.

    The alias is the DEFAULT alias plus a suffix, never a fresh name, so it keeps
    the prefix both the validator ("must start with '<type>'") and the template
    (which dispatches on startswith('vscapa'), startswith('VROUTER'), ...) rely on.
    """
    default_alias, default_path = DEFAULT_DISKS.get(vm_type, (f"{vm_type}_disk", ""))
    if not custom_path or custom_path == default_path:
        return default_alias, default_path
    # Macro names allow [A-Za-z0-9_] only.
    suffix = re.sub(r'[^A-Za-z0-9_]', '_', str(hostname))
    return f"{default_alias}_{suffix}", custom_path


def topology_from_payload(payload):
    """Turn the browser's {labName, devices, links} into the exact dict shape
    that yaml.safe_load(topo.yml) produces, so the real validator can run on it.
    """
    devices = payload.get('devices', [])
    links = payload.get('links', [])

    disks = {}
    vms = []
    for d in devices:
        vm_type = d.get('type')
        hostname = d.get('hostname')
        alias, path = _alias_for(vm_type, hostname, (d.get('disk_path') or '').strip())
        disks[alias] = path
        vm = {'hostname': hostname, 'type': vm_type, 'disk': alias}
        if d.get('ncpus'):
            vm['ncpus'] = int(d['ncpus'])
        if d.get('memory'):
            vm['memory'] = int(d['memory'])
        vms.append(vm)

    out_links = []
    wants_sniffer = False
    for l in links:
        a, b = l.get('a'), l.get('b')
        if not a or not b:
            continue
        entry = {'endpoints': [f"{a['host']}:{a['port']}", f"{b['host']}:{b['port']}"]}
        if l.get('sniffer'):
            entry['sniffer'] = True
            wants_sniffer = True
        out_links.append(entry)

    # A 'sniffer: true' link needs BOTH the sniffer_disk alias and an actual VM
    # using it - add_sniffers_to_topology() looks up the VM by disk alias and
    # silently does nothing if it is missing ("no VM using 'sniffer_disk' was
    # found ... No sniffers will be added"). The checkbox would appear to work
    # while capturing nothing, so materialise the VM here.
    if wants_sniffer:
        disks[SNIFFER_DISK[0]] = SNIFFER_DISK[1]
        if not any(v['disk'] == SNIFFER_DISK[0] for v in vms):
            taken = {v['hostname'] for v in vms}
            vms.append({
                'hostname': _default_hostname('sniffer', taken),
                'type': 'server',
                'disk': SNIFFER_DISK[0],
            })

    return {
        'lab_name': (payload.get('labName') or '').strip(),
        'disks': disks,
        'vms': vms,
        'links': out_links,
    }


def _yaml_quote(s):
    """Quote only when the value could be misread as non-string YAML."""
    s = str(s)
    if s == '' or re.search(r'[:#\'"\[\]{}]|^\s|\s$', s):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def topology_to_yaml(payload):
    """Emit topo.yml. Hand-rolled rather than yaml.dump so the output keeps the
    project's ordering, grouping and comments instead of alphabetised soup.
    """
    data = topology_from_payload(payload)
    L = []
    L.append("# -----------------------------------------------------------------------------")
    L.append("#  Generated by the VMM topology builder (python3 vmm.py --build)")
    L.append("#  Safe to hand-edit afterwards - it is an ordinary topology file.")
    L.append("# -----------------------------------------------------------------------------")
    L.append("")
    L.append(f"lab_name: {_yaml_quote(data['lab_name'])}")
    L.append("")
    L.append("disks:")
    for alias in sorted(data['disks']):
        L.append(f"  {alias+':':18s} {data['disks'][alias]}")
    L.append("")
    L.append("vms:")
    for vm in data['vms']:
        L.append(f"  - hostname: {vm['hostname']}")
        L.append(f"    type: {vm['type']}")
        L.append(f"    disk: {vm['disk']}")
        if 'ncpus' in vm:
            L.append(f"    ncpus: {vm['ncpus']}")
        if 'memory' in vm:
            L.append(f"    memory: {vm['memory']}")
    L.append("")
    if data['links']:
        L.append("links:")
        for l in data['links']:
            ep = ', '.join('"%s"' % e for e in l['endpoints'])
            L.append(f"  - endpoints: [{ep}]")
            if l.get('sniffer'):
                L.append("    sniffer: true")
    else:
        L.append("links: []")
    L.append("")
    return '\n'.join(L)


def validate_payload(payload):
    """Run the project's real validator over a GUI payload.

    Returns {'errors': [...], 'warnings': [...]}. Errors come straight from
    collect_topology_errors() so the GUI can never be more permissive than the
    CLI. Warnings are builder-only ergonomics (e.g. an unwired device), which
    are not topology errors and must not block a deploy.
    """
    data = topology_from_payload(payload)
    try:
        errors = vmm.collect_topology_errors(data)
    except Exception as e:                       # never let a validator bug kill the UI
        errors = [f"validator raised {type(e).__name__}: {e}"]

    warnings = []
    wired = set()
    for l in data['links']:
        for ep in l['endpoints']:
            wired.add(ep.split(':', 1)[0])
    for vm in data['vms']:
        if vm['hostname'] not in wired:
            warnings.append(f"'{vm['hostname']}' has no links - it will boot but sit isolated.")
    if not data['vms']:
        warnings.append("No devices yet. Add one from the palette on the left.")
    for vm in data['vms']:
        if vm['type'] == 'vferrari':
            warnings.append("vferrari has no default image on the pods - set its disk path to your own .vmdk build.")
    return {'errors': errors, 'warnings': sorted(set(warnings))}


# -----------------------------
# Deploy runner
# -----------------------------
class DeployJob:
    """Runs 'python3 vmm.py -t <file>' and buffers its output for polling."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf = []
        self.proc = None
        self.running = False
        self.rc = None
        self.started = None

    def start(self, topo_path, extra_args=None):
        with self.lock:
            if self.running:
                return False, "a deploy is already running"
            self.buf = []
            self.running = True
            self.rc = None
            self.started = time.time()

        argv = [sys.executable, "-u", "vmm.py", "-t", topo_path] + list(extra_args or [])

        def run():
            try:
                self.proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
                    text=True, bufsize=1,
                )
                for line in self.proc.stdout:
                    with self.lock:
                        self.buf.append(line.rstrip('\n'))
                self.proc.wait()
                rc = self.proc.returncode
            except Exception as e:
                with self.lock:
                    self.buf.append(f"!! failed to start deploy: {e}")
                rc = -1
            with self.lock:
                self.rc = rc
                self.running = False

        threading.Thread(target=run, daemon=True).start()
        return True, "started"

    def tail(self, offset):
        with self.lock:
            return {
                'lines': self.buf[offset:],
                'offset': len(self.buf),
                'running': self.running,
                'rc': self.rc,
            }

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            return True
        return False


DEPLOY = DeployJob()


def _vmm_available():
    """Is the 'vmm' command present? Deploy is meaningless without it."""
    from shutil import which
    return which("vmm") is not None


def build_catalog():
    """Everything the browser needs to render the palette and port grids."""
    types = []
    for t in sorted(vmm.SUPPORTED_VM_TYPES):
        types.append({
            'type': t,
            'ports': vmm.PORT_CATALOG.get(t, []),
            'note': TYPE_NOTES.get(t, ''),
            'disk': DEFAULT_DISKS.get(t, ('', ''))[1],
        })
    return {
        'types': types,
        'retired': {k: v for k, v in vmm.RETIRED_VM_TYPES.items()},
        'canDeploy': _vmm_available(),
        'cwd': os.getcwd(),
    }


# -----------------------------
# Live device status (mgmt IP)
# -----------------------------
# A device's management IP is assigned by DHCP during the deploy, so it only
# exists for a lab that is already running. It is read back from 'vmm ping',
# which is slow (bounded at 20s) and would stall every poll if it ran inside the
# request. So: serve the last snapshot immediately and refresh out of band.
#
# The parsing is NOT reimplemented here - get_vmm_ip_map/get_vmm_ping_map and
# re_ping_name live in vmm.py, and the RE naming they encode is type-specific
# and easy to get subtly wrong (vmx -> '<host>_RE', vbugatti -> '<host>-re0',
# EVO PTX -> '<host>_RE0').
class StatusCache:
    TTL = 25.0

    def __init__(self):
        self._lock = threading.Lock()
        self._ips = {}
        self._states = {}
        self._at = 0.0
        self._busy = False

    def _refresh(self):
        try:
            ips = vmm.get_vmm_ip_map()
            states = vmm.get_vmm_ping_map()
        except Exception:
            ips, states = {}, {}
        with self._lock:
            self._ips, self._states = ips, states
            self._at = time.time()
            self._busy = False

    def snapshot(self):
        """Never blocks. Returns the freshest data available and kicks off a
        refresh when it has gone stale."""
        with self._lock:
            stale = (time.time() - self._at) > self.TTL
            if stale and not self._busy:
                self._busy = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return dict(self._ips), dict(self._states), self._at


STATUS = StatusCache()


def device_status(devices):
    """{hostname: {ip, state}} for the devices currently on the canvas."""
    if not _vmm_available():
        return {'available': False, 'devices': {}, 'at': 0}
    ips, states, at = STATUS.snapshot()
    out = {}
    for d in devices or []:
        host, vtype = d.get('hostname'), d.get('type')
        if not host:
            continue
        key = vmm.re_ping_name(host, vtype)
        out[host] = {'ip': ips.get(key, ''), 'state': states.get(key, ''), 'node': key}
    return {'available': True, 'devices': out, 'at': at}


# -----------------------------
# Draft autosave
# -----------------------------
# The canvas is worthless if a browser refresh throws it away, so the layout is
# autosaved server-side rather than in localStorage. localStorage is keyed by
# origin, and this server is reached by several names (10.51.246.54:8081,
# q-pod08-vmm:8081, 127.0.0.1:8081) -- each would get its OWN private copy, so
# work would vanish just from typing a different URL for the same server.
# Keeping the draft next to the topology file means it also survives closing
# the browser, switching machines, and restarting the server.
def _draft_path(topo_path):
    d, base = os.path.split(os.path.abspath(topo_path))
    return os.path.join(d, '.' + base + '.builder-draft.json')


def read_draft(topo_path):
    try:
        with open(_draft_path(topo_path)) as f:
            draft = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(draft, dict) or not isinstance(draft.get('devices'), list):
        return None
    return draft


def write_draft(topo_path, payload):
    draft = {
        'labName': payload.get('labName') or '',
        'devices': payload.get('devices') or [],
        'links': payload.get('links') or [],
        'shapes': payload.get('shapes') or [],
        'seq': payload.get('seq') or 1,
        'savedAt': time.time(),
    }
    path = _draft_path(topo_path)
    # Write-then-rename: a refresh landing mid-write must never read half a file.
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(draft, f)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {'ok': False, 'error': str(e)}
    return {'ok': True}


def clear_draft(topo_path):
    try:
        os.unlink(_draft_path(topo_path))
    except OSError:
        pass
    return {'ok': True}


# -----------------------------
# HTTP server
# -----------------------------
def _json_response(handler, obj, code=200):
    body = json.dumps(obj).encode('utf-8')
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler):
    n = int(handler.headers.get('Content-Length') or 0)
    if not n:
        return {}
    try:
        return json.loads(handler.rfile.read(n).decode('utf-8'))
    except Exception:
        return {}


def make_handler(topo_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/':
                body = BUILDER_HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                # The whole app is inlined in this page, so a cached copy means
                # an upgraded vmm_builder.py silently keeps serving the old UI.
                self.send_header('Cache-Control', 'no-store, must-revalidate')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == '/api/catalog':
                return _json_response(self, build_catalog())
            if path == '/api/deploy/log':
                q = self.path.split('?', 1)[1] if '?' in self.path else ''
                offset = 0
                for part in q.split('&'):
                    if part.startswith('offset='):
                        try:
                            offset = int(part[7:])
                        except ValueError:
                            offset = 0
                return _json_response(self, DEPLOY.tail(offset))
            if path == '/api/draft':
                draft = read_draft(topo_path)
                return _json_response(self, {'ok': draft is not None, 'draft': draft})
            if path == '/api/load':
                # Re-open an existing topo.yml in the builder.
                try:
                    data = vmm._load_topology(topo_path)
                    return _json_response(self, {'ok': True, 'topology': data})
                except Exception as e:
                    return _json_response(self, {'ok': False, 'error': str(e)})
            self.send_error(404)

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            payload = _read_json(self)

            if path == '/api/draft':
                return _json_response(self, write_draft(topo_path, payload))

            if path == '/api/draft/clear':
                return _json_response(self, clear_draft(topo_path))

            if path == '/api/status':
                return _json_response(self, device_status(payload.get('devices')))

            if path == '/api/validate':
                return _json_response(self, validate_payload(payload))

            if path == '/api/yaml':
                return _json_response(self, {'yaml': topology_to_yaml(payload)})

            if path == '/api/save':
                res = validate_payload(payload)
                if res['errors']:
                    return _json_response(self, {'ok': False, 'errors': res['errors']})
                text = topology_to_yaml(payload)
                try:
                    with open(topo_path, 'w') as f:
                        f.write(text)
                except OSError as e:
                    return _json_response(self, {'ok': False, 'errors': [str(e)]})
                return _json_response(self, {'ok': True, 'path': os.path.abspath(topo_path)})

            if path == '/api/deploy':
                res = validate_payload(payload)
                if res['errors']:
                    return _json_response(self, {'ok': False, 'errors': res['errors']})
                if not _vmm_available():
                    return _json_response(self, {'ok': False, 'errors': [
                        "The 'vmm' command is not on this host, so there is nothing to "
                        "deploy to. Save the file and run it from a -vmm pod host."]})
                try:
                    with open(topo_path, 'w') as f:
                        f.write(topology_to_yaml(payload))
                except OSError as e:
                    return _json_response(self, {'ok': False, 'errors': [str(e)]})
                extra = []
                if payload.get('configFileOnly'):
                    extra.append('--config_file_only')
                if payload.get('force'):
                    extra.append('--force')
                ok, msg = DEPLOY.start(topo_path, extra)
                return _json_response(self, {'ok': ok, 'message': msg,
                                             'path': os.path.abspath(topo_path)})

            if path == '/api/deploy/stop':
                return _json_response(self, {'ok': DEPLOY.stop()})

            self.send_error(404)

        def log_message(self, *a):
            pass

    return Handler


def serve_builder(topo_path="topo.yml", port=8081):
    """Start the builder UI. Blocks until Ctrl+C."""
    handler = make_handler(topo_path)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    except OSError as e:
        print(f"❌ Could not start the builder on port {port}: {e}\n"
              f"   Try a different port with --port <N>.", file=sys.stderr)
        return 1

    try:
        ip = vmm.qpod_ip()
    except Exception:
        ip = "127.0.0.1"

    can = _vmm_available()
    print("\n" + "=" * 64)
    print("  🧩  VMM topology builder — open this in a browser:")
    print(f"          http://{ip}:{port}/")
    print("=" * 64)
    print(f"  Writes to : {os.path.abspath(topo_path)}")
    if can:
        print("  Deploy    : enabled ('vmm' found on this host)")
    else:
        print("  Deploy    : DISABLED — no 'vmm' command here. You can still design")
        print("              and save the file, then deploy from a -vmm pod host.")
    print("  Rules     : validated by vmm.py itself, live as you wire.")
    print("  Ctrl+C to stop.\n", flush=True)   # flush: stdout block-buffers under nohup/redirect
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 builder stopped.")
    return 0


# -----------------------------
# Front end
# -----------------------------
# Self-contained on purpose: no CDN, no build step, no npm. Pod hosts have no
# outbound internet, and the existing --serve diagram already hand-rolls its
# SVG for the same reason.
#
# NOTE: this is a plain string, never an f-string - it is full of CSS and JS
# braces. All server data arrives via fetch('/api/catalog').
BUILDER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VMM topology builder</title>
<style>
  :root{
    --bg:#0f1520; --panel:#151d2b; --panel2:#1b2536; --line:#26334a;
    --fg:#dbe4f0; --dim:#8fa1bb; --accent:#3ea8ff; --ok:#3ddc97;
    --warn:#ffc857; --err:#ff6b6b; --armed:#c792ea;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--bg);color:var(--fg);overflow:hidden}
  button{font:inherit;cursor:pointer;border-radius:6px;border:1px solid var(--line);
    background:var(--panel2);color:var(--fg);padding:6px 10px}
  button:hover:not(:disabled){border-color:var(--accent)}
  button:disabled{opacity:.4;cursor:not-allowed}
  input{font:inherit;background:#0c1119;border:1px solid var(--line);color:var(--fg);
    border-radius:6px;padding:6px 8px}
  input:focus{outline:none;border-color:var(--accent)}

  #app{display:grid;grid-template-columns:212px 1fr var(--rw,330px);
    grid-template-rows:48px 1fr;height:100vh;position:relative}
  header{grid-column:1/4;display:flex;align-items:center;gap:12px;padding:0 14px;
    background:var(--panel);border-bottom:1px solid var(--line)}
  header h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.3px}
  header .sp{flex:1}
  .badge{font-size:11px;padding:3px 8px;border-radius:99px;background:var(--panel2);color:var(--dim)}
  .badge.ok{color:var(--ok)} .badge.err{color:var(--err)}

  aside{background:var(--panel);border-right:1px solid var(--line);overflow-y:auto}
  aside.right{border-right:none;border-left:1px solid var(--line);grid-column:3}
  .sect{padding:9px 12px;font-size:10px;letter-spacing:.9px;text-transform:uppercase;
    color:var(--dim);border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}

  .ptype{padding:7px 12px;border-bottom:1px solid rgba(38,51,74,.5);cursor:pointer}
  .ptype:hover{background:var(--panel2)}
  .ptype b{font-weight:600}
  .ptype small{display:block;color:var(--dim);font-size:10.5px;line-height:1.35;margin-top:1px}

  main{position:relative;overflow:hidden;background:
    radial-gradient(circle at 1px 1px,#1d2738 1px,transparent 0) 0 0/22px 22px,var(--bg)}
  svg{width:100%;height:100%;display:block}
  .node rect{fill:var(--panel2);stroke:var(--line);stroke-width:1.5;rx:8}
  .node.sel rect{stroke:var(--accent);stroke-width:2}
  .node text{fill:var(--fg);font-size:12px;font-weight:600;pointer-events:none}
  .node .ty{fill:var(--dim);font-size:10px;font-weight:400}
  .node .ip{fill:var(--ok);font-size:9.5px;font-weight:500;font-family:ui-monospace,Menlo,monospace}
  .node{cursor:grab}
  .lnk{stroke-width:2;cursor:pointer}
  .lnk.sel{stroke:var(--accent) !important;stroke-width:3}
  .lhit{stroke:transparent;stroke-width:14;fill:none;cursor:pointer}
  .lbl{fill:var(--dim);font-size:9.5px}
  .llbl{fill:#cfe0ff;font-size:11px;font-weight:600}
  .wp{fill:transparent;stroke:transparent;cursor:move}
  .wp:hover{fill:var(--accent);fill-opacity:.35;stroke:var(--accent)}
  .wp.on{fill:var(--panel2);stroke:#4a6180;stroke-width:1.5}
  .wp.on:hover{fill:var(--accent);fill-opacity:.35}

  /* Canvas decorations. Purely visual - they live in the draft and never reach
     the generated topology. */
  .shp{cursor:move}
  .shp .body{stroke-width:2}
  .shp.sel .body{stroke-dasharray:6 3}
  .shp .cap{font-size:12px;font-weight:600;pointer-events:none}
  .rsz{fill:var(--accent);stroke:#0b0f16;stroke-width:1.5;cursor:nwse-resize}
  .shape-tools{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px}
  .stool{flex:1 1 auto;padding:6px 8px;font-size:11px;background:var(--panel2);
    border:1px solid var(--line);border-radius:5px;color:var(--fg);cursor:pointer}
  .stool:hover{border-color:var(--accent)}
  .crow{display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:11px;color:var(--dim)}
  .crow input[type=color]{width:34px;height:22px;padding:0;border:1px solid var(--line);
    background:none;border-radius:4px;cursor:pointer}
  .crow input[type=range]{flex:1;accent-color:var(--accent)}

  .tabs{display:flex;border-bottom:1px solid var(--line)}
  .tab{flex:1;padding:8px;text-align:center;font-size:11.5px;color:var(--dim);cursor:pointer;
    border-bottom:2px solid transparent}
  .tab.on{color:var(--fg);border-bottom-color:var(--accent)}
  .pane{display:none;padding:10px 12px} .pane.on{display:block}

  .port{display:inline-block;margin:2px;padding:3px 6px;font-size:10.5px;border-radius:4px;
    background:#0c1119;border:1px solid var(--line);cursor:pointer;font-family:ui-monospace,Menlo,monospace}
  .port:hover{border-color:var(--accent)}
  .port.used{background:#0a2a1e;border-color:#1f6b4d;color:#79d4ab;cursor:not-allowed}
  .port.armed{background:#3a2b52;border-color:var(--armed);color:#e6d4ff}

  .msg{padding:6px 8px;border-radius:5px;margin-bottom:5px;font-size:11.5px;line-height:1.4}
  .msg.err{background:rgba(255,107,107,.1);border-left:2px solid var(--err)}
  .msg.warn{background:rgba(255,200,87,.09);border-left:2px solid var(--warn)}
  .msg.ok{background:rgba(61,220,151,.09);border-left:2px solid var(--ok);color:var(--ok)}

  .lrow{display:flex;align-items:center;gap:5px;padding:5px 0;border-bottom:1px solid rgba(38,51,74,.5);
    font-size:10.5px;font-family:ui-monospace,Menlo,monospace}
  .lrow .lbl2{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .lrow .x{color:var(--dim);cursor:pointer;padding:0 4px}
  .lrow .x:hover{color:var(--err)}
  .snf{cursor:pointer;color:var(--dim);background:var(--panel2);border:1px solid var(--line);
    border-radius:99px;padding:2px 7px;font-size:9.5px;font-family:inherit;white-space:nowrap}
  .snf:hover{border-color:var(--warn);color:var(--warn)}
  .snf.on{color:#1b1405;background:var(--warn);border-color:var(--warn);font-weight:600}

  .fl{display:block;font-size:9.5px;letter-spacing:.7px;text-transform:uppercase;
    color:var(--dim);margin:0 0 3px}
  .wide{width:100%;box-sizing:border-box}
  .meta{font-size:10px;color:var(--dim);margin:4px 0 8px;line-height:1.5;
    font-family:ui-monospace,Menlo,monospace;word-break:break-all}

  pre{background:#0c1119;border:1px solid var(--line);border-radius:6px;padding:9px;
    font-size:10.5px;line-height:1.45;overflow:auto;max-height:calc(100vh - 190px);
    font-family:ui-monospace,Menlo,monospace;white-space:pre;margin:0}
  #log{max-height:none;height:calc(100vh - 150px);color:#c8d6e8}

  #hint{position:absolute;left:50%;transform:translateX(-50%);bottom:14px;background:var(--panel);
    border:1px solid var(--accent);border-radius:20px;padding:6px 14px;font-size:11.5px;
    box-shadow:0 4px 18px rgba(0,0,0,.45);display:none}
  #empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:var(--dim);font-size:13px;pointer-events:none;text-align:center;line-height:1.7}

  /* Drag the seam to give a long deploy log more room. */
  #rsz{position:absolute;top:48px;bottom:0;right:var(--rw,330px);width:9px;
    margin-right:-4px;cursor:col-resize;z-index:6}
  #rsz i{position:absolute;left:4px;top:0;bottom:0;width:1px;display:block;background:transparent}
  #rsz:hover i,#rsz.on i{background:var(--accent)}
  body.rsz-on{cursor:col-resize;user-select:none}

  #zoom{position:absolute;right:12px;bottom:12px;display:flex;align-items:center;gap:1px;
    background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:3px;
    box-shadow:0 3px 14px rgba(0,0,0,.45);z-index:4}
  #zoom button{border:1px solid transparent;background:transparent;color:var(--dim);
    padding:3px 9px;font-size:13px;line-height:1.15;border-radius:5px}
  #zoom button:hover{background:var(--panel2);color:var(--fg);border-color:transparent}
  #zlvl{min-width:46px;padding:3px 2px;text-align:center;font-size:10.5px;color:var(--dim);
    font-family:ui-monospace,Menlo,monospace;cursor:pointer;user-select:none}
  #zlvl:hover{color:var(--fg)}
  svg#cv.panning{cursor:grabbing}
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>🧩 VMM topology builder</h1>
    <input id="labName" value="MY-LAB" size="16" title="lab_name">
    <span id="stat" class="badge">…</span>
    <span class="sp"></span>
    <span id="where" class="badge"></span>
    <button id="btnClear">Clear</button>
    <button id="btnSave">Save topo.yml</button>
    <button id="btnDeploy" style="background:#12406b;border-color:#2b6ea8">▶ Deploy</button>
  </header>

  <aside class="left">
    <div class="sect">Devices — click to add</div>
    <div id="palette"></div>
    <div class="sect">Drawing — click to add</div>
    <div class="shape-tools">
      <button class="stool" data-shape="rect" title="A grouping box. Drag to move, drag its corner to resize.">▭ Box</button>
      <button class="stool" data-shape="ellipse" title="An ellipse. Drag to move, drag its corner to resize.">◯ Circle</button>
      <button class="stool" data-shape="text" title="A free-standing text label.">T Text</button>
    </div>
    <div style="padding:0 12px 10px;color:var(--dim);font-size:10.5px;line-height:1.4">
      Drawings are decoration only — they are saved with your canvas but never
      appear in the topology file.
    </div>
  </aside>

  <main>
    <svg id="cv"></svg>
    <div id="empty">Click a device on the left to add it.<br>Then click a port, then a port on another device.</div>
    <div id="hint"></div>
    <div id="zoom">
      <button id="zOut" title="Zoom out  ( - )">&minus;</button>
      <div id="zlvl" title="click to reset to 100%  ( 0 )">100%</div>
      <button id="zIn" title="Zoom in  ( + )">+</button>
      <button id="zFit" title="Fit the whole topology  ( f )">&#10530;</button>
    </div>
  </main>

  <aside class="right">
    <div class="tabs">
      <div class="tab on" data-p="insp">Inspect</div>
      <div class="tab" data-p="val">Checks</div>
      <div class="tab" data-p="yaml">YAML</div>
      <div class="tab" data-p="deploy">Deploy</div>
    </div>
    <div class="pane on" id="p-insp"></div>
    <div class="pane" id="p-val"></div>
    <div class="pane" id="p-yaml"><pre id="yaml">…</pre></div>
    <div class="pane" id="p-deploy">
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <button id="btnStop">Stop</button>
        <label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="cfgOnly" style="width:auto"> config only
        </label>
      </div>
      <pre id="log">Not started.</pre>
    </div>
  </aside>

  <div id="rsz" title="drag to resize this panel · double-click to reset"><i></i></div>
</div>

<script>
const $ = s => document.querySelector(s);
let CAT = null;                       // server catalog: types + their legal ports
let S = { labName:'MY-LAB', devices:[], links:[], shapes:[] };
let sel = null, armed = null, seq = 1;
let booted = false;   // guards autosave: the empty boot state must never clobber a saved draft

/* ---------- boot ---------- */
Promise.all([
  fetch('/api/catalog').then(r=>r.json()),
  fetch('/api/draft').then(r=>r.json()).catch(()=>({ok:false}))
]).then(([c, d])=>{
  CAT = c;
  $('#where').textContent = c.cwd;
  if(!c.canDeploy){
    const b=$('#btnDeploy'); b.disabled=true;
    b.title="No 'vmm' command on this host — save the file and deploy from a pod.";
  }
  renderPalette();
  if(d && d.ok && d.draft) restoreDraft(d.draft);
  loadUi();
  booted = true;
  refresh();
  applyView();
  // A saved pan from a previous session could point at empty space, which
  // looks exactly like a lost topology. Snap back to it instead.
  if((S.devices.length || S.shapes.length) && !anythingVisible()) fitView();
  pollStatus();
});

function idNum(s){ const m=/(\d+)$/.exec(s||''); return m?+m[1]:0; }

function restoreDraft(d){
  const known = new Set(CAT.types.map(t=>t.type));
  const devices = (d.devices||[]).filter(x=>x && known.has(x.type));
  const hosts = new Set(devices.map(x=>x.hostname));
  const links = (d.links||[]).filter(l=>l&&l.a&&l.b&&hosts.has(l.a.host)&&hosts.has(l.b.host));
  const shapes = (d.shapes||[]).filter(s=>s && SHAPE_KINDS.has(s.kind));
  // Drawings alone are worth restoring, so only a completely empty draft is skipped.
  if(!devices.length && !shapes.length) return;
  S = { labName: d.labName || 'MY-LAB', devices, links, shapes };
  $('#labName').value = S.labName;
  // Ids handed out from here on must not collide with the restored ones.
  seq = Math.max(d.seq||1, 0, ...devices.map(x=>idNum(x.id)), ...links.map(l=>idNum(l.id)),
                 ...shapes.map(s=>idNum(s.id))) + 1;
  const lost = (d.devices||[]).length - devices.length;
  const bits = [];
  if(devices.length) bits.push(`${devices.length} device${devices.length===1?'':'s'}`);
  if(shapes.length)  bits.push(`${shapes.length} drawing${shapes.length===1?'':'s'}`);
  let msg = `Restored ${bits.join(' and ')} from your last session.`;
  if(lost) msg += ` ${lost} used a device type this server no longer supports and were dropped.`;
  hint(msg); setTimeout(()=>hint(''), lost?9000:4000);
}

function typeInfo(t){ return CAT.types.find(x=>x.type===t); }

function renderPalette(){
  $('#palette').innerHTML = CAT.types.map(t=>
    `<div class="ptype" data-t="${t.type}">
       <b>${t.type}</b> <span style="color:var(--dim);font-size:10px">${t.ports.length}p</span>
       <small>${t.note||''}</small>
     </div>`).join('');
  document.querySelectorAll('.ptype').forEach(el=>
    el.onclick=()=>addDevice(el.dataset.t));
  document.querySelectorAll('.stool').forEach(el=>
    el.onclick=()=>addShape(el.dataset.shape));
}

/* ---------- model ---------- */
const SHAPE_KINDS = new Set(['rect','ellipse','text']);
const SHAPE_DEFAULTS = {
  rect:    {w:260, h:170, text:'Group',  stroke:'#4a6180', fill:'#1b2941', op:35, size:13},
  ellipse: {w:220, h:150, text:'Area',   stroke:'#4a6180', fill:'#1b2941', op:35, size:13},
  text:    {w:0,   h:0,   text:'Label',  stroke:'#cfe0ff', fill:'#000000', op:0,  size:15}
};

function addShape(kind){
  if(!SHAPE_KINDS.has(kind)) return;
  const n = S.shapes.length;
  S.shapes.push(Object.assign({
    id:'s'+(seq++), kind,
    x: view.x + 60 + (n%5)*34, y: view.y + 60 + (n%5)*30
  }, SHAPE_DEFAULTS[kind]));
  sel = S.shapes[S.shapes.length-1].id;
  refresh(); showTab('insp');
  const t=$('#shtext'); if(t){ t.focus(); t.select(); }
}

function selDevice(){ return S.devices.find(x=>x.id===sel) || null; }
function selShape(){  return S.shapes.find(x=>x.id===sel)  || null; }
function selLink(){   return S.links.find(x=>x.id===sel)   || null; }

/* Delete whatever is selected - device, drawing or link. */
function removeSelected(){
  if(!sel) return;
  if(selDevice())      removeDevice(sel);
  else if(selShape()){ S.shapes = S.shapes.filter(x=>x.id!==sel); sel=null; refresh(); }
  else if(selLink()){  S.links  = S.links.filter(x=>x.id!==sel);  sel=null; refresh(); }
}

function addDevice(type){
  const names = new Set(S.devices.map(d=>d.hostname));
  let n=1; while(names.has(type+n)) n++;
  const i = S.devices.length;
  S.devices.push({
    id:'d'+(seq++), type, hostname:type+n,
    x: 90 + (i%4)*210, y: 70 + Math.floor(i/4)*130
  });
  const nd = S.devices[S.devices.length-1];
  sel = nd.id;
  refresh();
  ensureVisible(nd.x, nd.y, W, H);
  // Name it straight away: the field is focused and pre-selected, so typing
  // replaces the default and Enter commits. No dialog to dismiss.
  showTab('insp');
  const hn=$('#hn'); if(hn){ hn.focus(); hn.select(); }
}

function removeDevice(id){
  const d = S.devices.find(x=>x.id===id); if(!d) return;
  S.links = S.links.filter(l=>l.a.host!==d.hostname && l.b.host!==d.hostname);
  S.devices = S.devices.filter(x=>x.id!==id);
  if(sel===id) sel=null;
  armed=null; refresh();
}

function portUser(host, port){
  for(const l of S.links){
    if(l.a.host===host && l.a.port===port) return l.b;
    if(l.b.host===host && l.b.port===port) return l.a;
  }
  return null;
}

function clickPort(host, port){
  if(portUser(host,port)) return;                    // already wired
  if(!armed){ armed={host,port}; hint(`Now click a port on another device (Esc to cancel)`); refresh(); return; }
  if(armed.host===host){ armed={host,port}; refresh(); return; }   // re-arm on same device
  S.links.push({id:'l'+(seq++), a:armed, b:{host,port}, sniffer:false});
  armed=null; hint(''); refresh();
}

function hint(t){ const h=$('#hint'); h.textContent=t; h.style.display = t?'block':'none'; }

/* ---------- render ---------- */
const W=150, H=52;
const FAN=28;          // perpendicular spacing between parallel links

/* ---------- view: pan + zoom ----------------------------------------------
   The SVG has no scrollbars, so panning and zooming are done purely through
   the viewBox. Every pointer handler converts screen coords to user units via
   svgPt(), which keeps this transform the single source of truth - drag a node
   at 40% zoom and it still follows the cursor exactly. */
const ZMIN=0.25, ZMAX=3, ZSTEP=1.2;
let view={x:0,y:0,k:1};
let pdrag=null, rzdrag=false, panned=false;

function applyView(){
  const svg=$('#cv'); if(!svg) return;
  const r=svg.getBoundingClientRect();
  if(!r.width || !r.height) return;
  svg.setAttribute('viewBox',`${view.x} ${view.y} ${r.width/view.k} ${r.height/view.k}`);
  const z=$('#zlvl'); if(z) z.textContent=Math.round(view.k*100)+'%';
}
/* cx,cy are element pixels - the point to keep pinned as the scale changes. */
function setZoom(k,cx,cy){
  const r=$('#cv').getBoundingClientRect();
  k=Math.min(ZMAX,Math.max(ZMIN,k));
  if(cx===undefined){ cx=r.width/2; cy=r.height/2; }
  const ux=view.x+cx/view.k, uy=view.y+cy/view.k;
  view.k=k; view.x=ux-cx/k; view.y=uy-cy/k;
  applyView(); saveUi();
}
function zoomBy(f){ setZoom(view.k*f); }
function resetView(){ view={x:0,y:0,k:1}; applyView(); saveUi(); }
function fitView(){
  const xs=[], ys=[];
  const add=(x,y)=>{ if(isFinite(x)&&isFinite(y)){ xs.push(x); ys.push(y); } };
  for(const d of S.devices){ add(d.x,d.y); add(d.x+W,d.y+H); }
  for(const q of S.shapes){ add(q.x,q.y); add(q.x+(q.w||140), q.y+(q.h||24)); }
  for(const l of S.links) if(l.w) add(l.w.x,l.w.y);
  if(!xs.length){ resetView(); return; }
  const x0=Math.min.apply(null,xs), x1=Math.max.apply(null,xs);
  const y0=Math.min.apply(null,ys), y1=Math.max.apply(null,ys);
  const r=$('#cv').getBoundingClientRect(), pad=70;
  const k=Math.min(ZMAX,Math.max(ZMIN,
        Math.min(r.width/Math.max(1,(x1-x0)+pad*2), r.height/Math.max(1,(y1-y0)+pad*2))));
  view.k=k;
  view.x=(x0+x1)/2 - r.width/(2*k);
  view.y=(y0+y1)/2 - r.height/(2*k);
  applyView(); saveUi();
}
/* True if any device or drawing currently falls inside the viewport. */
function anythingVisible(){
  const r=$('#cv').getBoundingClientRect();
  const vw=r.width/view.k, vh=r.height/view.k;
  const hit=(x,y,w,h)=> x+w>view.x && x<view.x+vw && y+h>view.y && y<view.y+vh;
  for(const d of S.devices) if(hit(d.x,d.y,W,H)) return true;
  for(const q of S.shapes)  if(hit(q.x,q.y,q.w||140,q.h||24)) return true;
  return false;
}

/* Keeps a freshly added item on screen when the canvas is panned or zoomed. */
function ensureVisible(x,y,w,h){
  const r=$('#cv').getBoundingClientRect();
  const vw=r.width/view.k, vh=r.height/view.k, m=40;
  let moved=false;
  if(x-m < view.x){ view.x=x-m; moved=true; }
  if(y-m < view.y){ view.y=y-m; moved=true; }
  if(x+w+m > view.x+vw){ view.x=x+w+m-vw; moved=true; }
  if(y+h+m > view.y+vh){ view.y=y+h+m-vh; moved=true; }
  if(moved){ applyView(); saveUi(); }
}

/* ---------- right panel width ---------- */
const RW_MIN=260, RW_DEF=330;
function rwMax(){ return Math.max(RW_MIN, window.innerWidth-460); }
function getRw(){
  const v=parseInt(document.documentElement.style.getPropertyValue('--rw'),10);
  return isFinite(v)?v:RW_DEF;
}
function setRw(px){
  const w=Math.round(Math.min(rwMax(),Math.max(RW_MIN,px)));
  document.documentElement.style.setProperty('--rw',w+'px');
  applyView();                       // the canvas just changed width
}

/* ---------- UI state -------------------------------------------------------
   Zoom and panel width are per-browser preferences, not part of the lab, so
   they live in localStorage and never travel to the server or the YAML. */
const UIK='vmmb.ui';
function saveUi(){
  try{ localStorage.setItem(UIK,JSON.stringify({view:view,rw:getRw()})); }catch(e){}
}
function loadUi(){
  try{
    const u=JSON.parse(localStorage.getItem(UIK)||'{}');
    if(u.view && isFinite(u.view.k))
      view={x:+u.view.x||0, y:+u.view.y||0,
            k:Math.min(ZMAX,Math.max(ZMIN,+u.view.k||1))};
    if(isFinite(u.rw)) setRw(u.rw);
  }catch(e){}
}

$('#zIn').onclick  = ()=>zoomBy(ZSTEP);
$('#zOut').onclick = ()=>zoomBy(1/ZSTEP);
$('#zFit').onclick = fitView;
$('#zlvl').onclick = resetView;

$('#cv').addEventListener('wheel',e=>{
  e.preventDefault();
  const r=$('#cv').getBoundingClientRect();
  setZoom(view.k*(e.deltaY<0?ZSTEP:1/ZSTEP), e.clientX-r.left, e.clientY-r.top);
},{passive:false});

/* Dragging empty canvas pans. Nodes and shapes stop the event reaching here. */
$('#cv').addEventListener('mousedown',e=>{
  if(e.target.id!=='cv') return;
  pdrag={sx:e.clientX, sy:e.clientY, vx:view.x, vy:view.y};
  panned=false;
  $('#cv').classList.add('panning');
});

$('#rsz').addEventListener('mousedown',e=>{
  e.preventDefault(); rzdrag=true;
  document.body.classList.add('rsz-on'); $('#rsz').classList.add('on');
});
$('#rsz').addEventListener('dblclick',()=>{ setRw(RW_DEF); saveUi(); });
window.addEventListener('resize',()=>{ setRw(getRw()); });

function refresh(){
  $('#empty').style.display = S.devices.length ? 'none' : 'flex';
  drawCanvas(); drawInspector(); drawYaml(); validate(); saveDraft();
}

function drawCanvas(){
  const svg=$('#cv'); let s='';
  // Drawings render first so they sit behind the topology, like a highlight
  // drawn on the whiteboard before the boxes.
  for(const p of S.shapes){
    const on = sel===p.id;
    if(p.kind==='text'){
      s+=`<g class="shp ${on?'sel':''}" data-s="${p.id}">
            <text class="cap" x="${p.x}" y="${p.y}" fill="${p.stroke}"
                  style="font-size:${p.size}px">${esc(p.text||'')}</text>
            ${on?`<rect class="body" x="${p.x-6}" y="${p.y-p.size-2}" width="${(p.text||' ').length*p.size*0.62+12}"
                    height="${p.size+10}" fill="none" stroke="${p.stroke}" stroke-width="1" rx="3"/>`:''}
          </g>`;
    } else {
      const body = p.kind==='ellipse'
        ? `<ellipse class="body" cx="${p.x+p.w/2}" cy="${p.y+p.h/2}" rx="${p.w/2}" ry="${p.h/2}"
              fill="${p.fill}" fill-opacity="${(p.op||0)/100}" stroke="${p.stroke}"/>`
        : `<rect class="body" x="${p.x}" y="${p.y}" width="${p.w}" height="${p.h}" rx="10"
              fill="${p.fill}" fill-opacity="${(p.op||0)/100}" stroke="${p.stroke}"/>`;
      s+=`<g class="shp ${on?'sel':''}" data-s="${p.id}">
            ${body}
            <text class="cap" x="${p.x+p.w/2}" y="${p.y+p.size+8}" text-anchor="middle"
                  fill="${p.stroke}" style="font-size:${p.size}px">${esc(p.text||'')}</text>
          </g>`;
      if(on) s+=`<rect class="rsz" data-r="${p.id}" x="${p.x+p.w-5}" y="${p.y+p.h-5}" width="11" height="11" rx="2"/>`;
    }
  }
  // Several links between the same two devices would otherwise be drawn on
  // exactly the same centre-to-centre line - one visible link hiding the rest,
  // with their port labels stacked on top of each other. Fan them out by
  // bowing each through a midpoint offset perpendicular to the run.
  const pairKey = l => (l.a.host < l.b.host ? l.a.host+'\u0000'+l.b.host
                                            : l.b.host+'\u0000'+l.a.host);
  const pairN={}, pairI={};
  for(const l of S.links){ const k=pairKey(l); pairI[l.id]=(pairN[k]=(pairN[k]||0)+1)-1; }

  for(const l of S.links){
    const A=S.devices.find(d=>d.hostname===l.a.host), B=S.devices.find(d=>d.hostname===l.b.host);
    if(!A||!B) continue;
    const x1=A.x+W/2, y1=A.y+H/2, x2=B.x+W/2, y2=B.y+H/2;
    // Evenly spaced about the centre line: 2 links sit at -14/+14, 3 at -28/0/+28.
    const n=pairN[pairKey(l)]||1, i=pairI[l.id]||0;
    const spread = n>1 ? (i-(n-1)/2)*FAN : 0;
    const vx=x2-x1, vy=y2-y1, vlen=Math.hypot(vx,vy)||1;
    const nx=-vy/vlen, ny=vx/vlen;                 // unit normal to the run
    // A link with a waypoint is drawn as a quadratic curve through it. The
    // control point is placed so the curve actually passes through the handle
    // (a quadratic sits halfway to its control point at t=0.5), which is what
    // makes dragging feel direct rather than sluggish.
    const hasW = l.w && isFinite(l.w.x) && isFinite(l.w.y);
    const mx = hasW ? l.w.x : (x1+x2)/2 + nx*spread;
    const my = hasW ? l.w.y : (y1+y2)/2 + ny*spread;
    const cx = 2*mx-(x1+x2)/2, cy = 2*my-(y1+y2)/2;
    const col = l.sniffer?'#ffc857':'#4a6180';
    const curved = hasW || spread!==0;
    const d = curved ? `M${x1},${y1} Q${cx},${cy} ${x2},${y2}` : `M${x1},${y1} L${x2},${y2}`;
    // A transparent fat path under the visible one makes a 2px link easy to hit.
    s+=`<path class="lhit" data-k="${l.id}" d="${d}"/>`;
    s+=`<path class="lnk ${sel===l.id?'sel':''}" data-k="${l.id}" d="${d}" fill="none"
              stroke="${col}" stroke-width="2"/>`;
    // Port labels ride along the curve so they follow a bent link.
    const at = t => ({x:(1-t)*(1-t)*x1+2*(1-t)*t*cx+t*t*x2, y:(1-t)*(1-t)*y1+2*(1-t)*t*cy+t*t*y2});
    const pa = curved?at(0.26):{x:x1+(x2-x1)*0.26,y:y1+(y2-y1)*0.26};
    const pb = curved?at(0.74):{x:x1+(x2-x1)*0.74,y:y1+(y2-y1)*0.74};
    s+=`<text class="lbl" x="${pa.x}" y="${pa.y-4}" text-anchor="middle">${l.a.port}</text>`;
    s+=`<text class="lbl" x="${pb.x}" y="${pb.y-4}" text-anchor="middle">${l.b.port}</text>`;
    let dy = -12;
    if(l.label){ s+=`<text class="llbl" x="${mx}" y="${my+dy}" text-anchor="middle">${esc(l.label)}</text>`; dy -= 13; }
    if(l.sniffer) s+=`<text class="lbl" x="${mx}" y="${my+dy}" text-anchor="middle" fill="#ffc857">◉ capture</text>`;
    s+=`<circle class="wp ${hasW?'on':''}" data-w="${l.id}" cx="${mx}" cy="${my}" r="6">
          <title>drag to bend this link · double-click to straighten</title></circle>`;
  }
  for(const d of S.devices){
    const n = S.links.filter(l=>l.a.host===d.hostname||l.b.host===d.hostname).length;
    const st = STAT[d.hostname]||{};
    s+=`<g class="node ${sel===d.id?'sel':''}" data-id="${d.id}" transform="translate(${d.x},${d.y})">
          <rect width="${W}" height="${H}"/>
          <text x="10" y="19">${esc(d.hostname)}</text>
          <text class="ty" x="10" y="33">${d.type} · ${n} link${n===1?'':'s'}</text>
          ${st.ip?`<text class="ip" x="10" y="46">${st.ip}</text>`:''}
        </g>`;
  }
  svg.innerHTML=s;
  svg.querySelectorAll('.node').forEach(g=>{
    g.onmousedown = e => startDrag(e, g.dataset.id);
    g.onclick = () => { sel=g.dataset.id; refresh(); };
  });
  svg.querySelectorAll('.shp').forEach(g=>{
    g.onmousedown = e => startShapeDrag(e, g.dataset.s);
    g.onclick = () => { sel=g.dataset.s; refresh(); };
    g.ondblclick = e => { e.stopPropagation(); sel=g.dataset.s; refresh(); showTab('insp');
      const t=$('#shtext'); if(t){ t.focus(); t.select(); } };
  });
  svg.querySelectorAll('.rsz').forEach(r=>{
    r.onmousedown = e => { e.stopPropagation(); rdrag={id:r.dataset.r}; };
  });
  svg.querySelectorAll('[data-k]').forEach(p=>{
    p.onclick = e => { e.stopPropagation(); sel=p.dataset.k; refresh(); showTab('insp'); };
  });
  svg.querySelectorAll('.wp').forEach(c=>{
    c.onmousedown = e => { e.stopPropagation(); startWpDrag(e, c.dataset.w); };
    c.ondblclick  = e => { e.stopPropagation();
      const l=S.links.find(x=>x.id===c.dataset.w); if(l){ delete l.w; refresh(); } };
  });
  svg.onclick = e => {
    if(panned){ panned=false; return; }        // that click was a pan, not a deselect
    if(e.target.id==='cv' && sel){ sel=null; refresh(); }
  };
}

let drag=null, wdrag=null, sdrag=null, rdrag=null;
function svgPt(e){
  // getScreenCTM folds in the viewBox, so this stays correct at any zoom/pan.
  const svg=$('#cv'), m=svg.getScreenCTM();
  if(!m) return {x:e.clientX, y:e.clientY};
  const p=svg.createSVGPoint(); p.x=e.clientX; p.y=e.clientY;
  const q=p.matrixTransform(m.inverse());
  return {x:q.x, y:q.y};
}
function startDrag(e,id){
  const d=S.devices.find(x=>x.id===id); if(!d) return;
  const pt=svgPt(e);
  drag={id, dx:pt.x-d.x, dy:pt.y-d.y, moved:false};
}
function startWpDrag(e,id){ wdrag={id}; }
function startShapeDrag(e,id){
  const p=S.shapes.find(x=>x.id===id); if(!p) return;
  const pt=svgPt(e);
  sdrag={id, dx:pt.x-p.x, dy:pt.y-p.y};
}
document.addEventListener('mousemove',e=>{
  if(rzdrag){ setRw(window.innerWidth-e.clientX); return; }
  if(pdrag){
    panned=true;
    view.x=pdrag.vx-(e.clientX-pdrag.sx)/view.k;
    view.y=pdrag.vy-(e.clientY-pdrag.sy)/view.k;
    applyView(); return;
  }
  if(rdrag){
    const p=S.shapes.find(x=>x.id===rdrag.id); if(!p) return;
    const pt=svgPt(e);
    p.w=Math.max(40, pt.x-p.x); p.h=Math.max(30, pt.y-p.y);
    drawCanvas(); return;
  }
  if(sdrag){
    const p=S.shapes.find(x=>x.id===sdrag.id); if(!p) return;
    const pt=svgPt(e);
    p.x=pt.x-sdrag.dx; p.y=pt.y-sdrag.dy; drawCanvas(); return;
  }
  if(wdrag){
    const l=S.links.find(x=>x.id===wdrag.id); if(!l) return;
    l.w=svgPt(e); drawCanvas(); return;
  }
  if(!drag) return;
  const d=S.devices.find(x=>x.id===drag.id); if(!d) return;
  const pt=svgPt(e);
  d.x=pt.x-drag.dx; d.y=pt.y-drag.dy; drag.moved=true; drawCanvas();
});
document.addEventListener('mouseup',()=>{
  if(drag||wdrag||sdrag||rdrag) saveDraft();      // persist the new layout
  if(pdrag){ $('#cv').classList.remove('panning'); pdrag=null; saveUi(); }
  if(rzdrag){ rzdrag=false; document.body.classList.remove('rsz-on');
              $('#rsz').classList.remove('on'); saveUi(); }
  drag=null; wdrag=null; sdrag=null; rdrag=null;
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){ armed=null; hint(''); refresh(); }
  if((e.key==='Delete'||e.key==='Backspace') && sel && e.target.tagName!=='INPUT') removeSelected();
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
  if(e.key==='+'||e.key==='='){ e.preventDefault(); zoomBy(ZSTEP); }
  else if(e.key==='-'||e.key==='_'){ e.preventDefault(); zoomBy(1/ZSTEP); }
  else if(e.key==='0') resetView();
  else if(e.key==='f'||e.key==='F') fitView();
});

function drawInspector(){
  const p=$('#p-insp');
  const d = S.devices.find(x=>x.id===sel);
  let s='';
  if(!d){
    const p = selShape(), k = selLink();
    if(p){
      s += `<label class="fl">Text</label>
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
              <input id="shtext" value="${esc(p.text||'')}" spellcheck="false" style="flex:1">
              <button id="shdel">Delete</button></div>`;
      s += `<div class="crow"><span style="width:52px">Border</span>
              <input type="color" id="shstroke" value="${esc(p.stroke||'#4a6180')}"></div>`;
      if(p.kind!=='text'){
        s += `<div class="crow"><span style="width:52px">Fill</span>
                <input type="color" id="shfill" value="${esc(p.fill||'#1b2941')}">
                <input type="range" id="shop" min="0" max="100" value="${p.op||0}"
                       title="fill opacity"><span style="width:30px">${p.op||0}%</span></div>`;
      }
      s += `<div class="crow"><span style="width:52px">Size</span>
              <input type="range" id="shsize" min="9" max="34" value="${p.size||13}">
              <span style="width:30px">${p.size||13}px</span></div>`;
      s += `<div class="meta">${p.kind==='text'?'Text label':(p.kind==='ellipse'?'Ellipse':'Box')}
              — drag to move${p.kind!=='text'?', drag the corner square to resize':''}.
              Decoration only; it never reaches the topology file.</div>`;
    } else if(k){
      const A=k.a, B=k.b;
      s += `<label class="fl">Link label</label>
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
              <input id="lktext" value="${esc(k.label||'')}" spellcheck="false"
                     placeholder="e.g. 10G to core" style="flex:1">
              <button id="lkdel">Delete</button></div>`;
      s += `<div class="meta">${esc(A.host)}:${A.port} ↔ ${esc(B.host)}:${B.port}</div>`;
      s += `<div style="display:flex;gap:6px;margin:8px 0">
              <button class="snf ${k.sniffer?'on':''}" id="lksnf" data-s="${k.id}">${k.sniffer?'◉ capturing':'○ capture'}</button>
              <button id="lkstr" ${k.w?'':'disabled'}>Straighten</button></div>`;
      s += `<div class="meta">Drag the handle on the link to bend it, or double-click the
              handle to straighten. The label is decoration only.</div>`;
    } else {
      s = `<div style="color:var(--dim);font-size:11.5px">Select a device, a link or a drawing.</div>`;
    }
  } else {
    const info = typeInfo(d.type);
    const st = STAT[d.hostname] || {};
    s += `<label class="fl">Name</label>
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
            <input id="hn" value="${esc(d.hostname)}" spellcheck="false" style="flex:1">
            <button id="del">Delete</button></div>`;
    s += `<label class="fl">Image <span style="color:var(--dim);text-transform:none">— blank = the default below</span></label>
          <input id="img" class="wide" spellcheck="false" placeholder="${esc(info.disk||'')}"
                 value="${esc(d.disk_path||'')}" title="Full path to a disk image on the pod">`;
    s += `<div class="meta">${d.disk_path ? 'custom image' : 'default: '+esc(info.disk||'—')}</div>`;
    s += `<div class="meta" id="mgmt">${mgmtLine(d, st)}</div>`;
    s += `<div style="color:var(--dim);font-size:10.5px;margin:8px 0">${info.note||''}</div>`;
    s += `<div class="sect" style="margin:0 -12px 8px;position:static">Ports — click to wire (${info.ports.length})</div>`;
    s += info.ports.map(pt=>{
      const u = portUser(d.hostname, pt);
      const cls = u ? 'port used' : (armed && armed.host===d.hostname && armed.port===pt ? 'port armed':'port');
      const ttl = u ? `wired to ${u.host}:${u.port}` : 'click to wire';
      return `<span class="${cls}" data-p="${pt}" title="${ttl}">${pt}</span>`;
    }).join('');
  }
  const nsnf = S.links.filter(l=>l.sniffer).length;
  s += `<div class="sect" style="margin:12px -12px 6px;position:static">Links (${S.links.length})</div>`;
  s += S.links.length ? S.links.map(l=>
      `<div class="lrow">
         <span class="lbl2">${esc(l.a.host)}:${l.a.port} ↔ ${esc(l.b.host)}:${l.b.port}</span>
         <button class="snf ${l.sniffer?'on':''}" data-s="${l.id}"
                 title="Splice a packet-capture VM into this link">${l.sniffer?'◉ capturing':'○ capture'}</button>
         <span class="x" data-l="${l.id}" title="delete link">✕</span></div>`).join('')
    : `<div style="color:var(--dim);font-size:11px">No links yet.</div>`;
  if(nsnf) s += `<div class="meta" style="color:var(--warn)">A <b>sniffer1</b> VM is added automatically and spliced into ${nsnf} link${nsnf===1?'':'s'}. Capture with tcpdump on its eth ports.</div>`;
  p.innerHTML=s;

  if(d){
    $('#hn').onchange = e => {
      const old=d.hostname, nn=e.target.value.trim();
      if(!nn) { e.target.value=old; return; }
      S.links.forEach(l=>{ if(l.a.host===old) l.a.host=nn; if(l.b.host===old) l.b.host=nn; });
      d.hostname=nn; refresh(); pollStatus();
    };
    $('#hn').onkeydown = e => { if(e.key==='Enter') e.target.blur(); };
    $('#img').onchange = e => {
      const v=e.target.value.trim();
      if(v) d.disk_path=v; else delete d.disk_path;
      refresh();
    };
    $('#del').onclick = ()=>removeDevice(d.id);
    p.querySelectorAll('.port:not(.used)').forEach(el=>
      el.onclick=()=>clickPort(d.hostname, el.dataset.p));
  }

  // Drawing properties. Colours and sliders redraw the canvas live but do not
  // rebuild the inspector - that would yank focus out of the control mid-drag.
  const shp = selShape();
  if(shp){
    const live = (id, apply) => {
      const el=$(id); if(!el) return;
      el.oninput = e => {
        apply(e.target.value);
        const out = e.target.parentElement.querySelector('span:last-child');
        if(out && e.target.type==='range') out.textContent = e.target.value + (id==='#shop'?'%':'px');
        drawCanvas();
      };
      el.onchange = () => saveDraft();
    };
    const t=$('#shtext');
    if(t){ t.oninput = e => { shp.text=e.target.value; drawCanvas(); };
           t.onchange = () => saveDraft();
           t.onkeydown = e => { if(e.key==='Enter') e.target.blur(); }; }
    live('#shstroke', v => shp.stroke=v);
    live('#shfill',   v => shp.fill=v);
    live('#shop',     v => shp.op=+v);
    live('#shsize',   v => shp.size=+v);
    $('#shdel').onclick = ()=>removeSelected();
  }

  const lk = selLink();
  if(lk){
    const t=$('#lktext');
    t.oninput  = e => { const v=e.target.value; if(v) lk.label=v; else delete lk.label; drawCanvas(); };
    t.onchange = () => saveDraft();
    t.onkeydown = e => { if(e.key==='Enter') e.target.blur(); };
    $('#lkdel').onclick = ()=>removeSelected();
    $('#lkstr').onclick = ()=>{ delete lk.w; refresh(); };
  }
  p.querySelectorAll('.x').forEach(el=>
    el.onclick=()=>{ S.links=S.links.filter(l=>l.id!==el.dataset.l); refresh(); });
  p.querySelectorAll('.snf').forEach(el=>
    el.onclick=()=>{ const l=S.links.find(x=>x.id===el.dataset.s); l.sniffer=!l.sniffer; refresh(); });
}

/* ---------- server-side rules ---------- */
function payload(){
  return { labName: $('#labName').value, devices: S.devices, links: S.links };
}

/* Autosaved server-side so a refresh, a closed tab or a different URL for the
   same server all come back to the same canvas. */
let dTimer=null, draftWarned=false;
function draftPayload(){
  const p = payload(); p.shapes = S.shapes; p.seq = seq; return p;
}

function saveDraft(){
  if(!booted) return;
  clearTimeout(dTimer);
  dTimer=setTimeout(()=>{
    const p = draftPayload();
    fetch('/api/draft',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(p)}).then(r=>r.json()).then(r=>{
        if(!r.ok && !draftWarned){
          draftWarned=true;
          hint('Autosave failed ('+r.error+') — this layout will not survive a refresh.');
        }
      }).catch(()=>{});
  },400);
}

/* A refresh within the debounce window would otherwise lose the last edit. */
window.addEventListener('pagehide', ()=>{
  if(!booted) return;
  const p = draftPayload();
  try { navigator.sendBeacon('/api/draft', new Blob([JSON.stringify(p)],{type:'application/json'})); }
  catch(e){}
});

let vTimer=null;
function validate(){
  clearTimeout(vTimer);
  vTimer=setTimeout(()=>{
    fetch('/api/validate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload())}).then(r=>r.json()).then(res=>{
        const n=res.errors.length;
        const b=$('#stat');
        b.textContent = n ? `${n} problem${n===1?'':'s'}` : 'valid';
        b.className = 'badge ' + (n?'err':'ok');
        $('#btnSave').disabled = n>0;
        $('#btnDeploy').disabled = n>0 || !CAT.canDeploy;
        let s='';
        if(!n && !res.warnings.length) s=`<div class="msg ok">Topology is valid.</div>`;
        s += res.errors.map(e=>`<div class="msg err">${esc(e)}</div>`).join('');
        s += res.warnings.map(w=>`<div class="msg warn">${esc(w)}</div>`).join('');
        $('#p-val').innerHTML=s;
      });
  },160);
}

function drawYaml(){
  fetch('/api/yaml',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload())}).then(r=>r.json()).then(r=>$('#yaml').textContent=r.yaml);
}

function esc(s){ return String(s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }

/* ---------- live mgmt IPs ---------- */
/* Only exists once a lab is actually running - the address is handed out by
   DHCP during the deploy, so before that every device correctly shows nothing. */
let STAT={}, statAvail=null;
function mgmtLine(d, st){
  if(statAvail===false) return `mgmt IP — needs a pod (no 'vmm' here)`;
  if(st && st.ip) return `mgmt IP <b style="color:var(--ok)">${st.ip}</b>${st.state?' · '+esc(st.state):''}`;
  return `mgmt IP — <span style="color:var(--dim)">not deployed yet</span>`;
}
function pollStatus(){
  if(!S.devices.length){ STAT={}; return; }
  fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({devices:S.devices})}).then(r=>r.json()).then(r=>{
      statAvail = r.available;
      const changed = JSON.stringify(r.devices)!==JSON.stringify(STAT);
      STAT = r.devices||{};
      if(changed){ drawCanvas(); drawInspector(); }
    }).catch(()=>{});
}
setInterval(pollStatus, 12000);

/* ---------- actions ---------- */
$('#labName').oninput = ()=>{ S.labName=$('#labName').value; drawYaml(); validate(); saveDraft(); };
$('#btnClear').onclick = ()=>{ if(confirm('Remove all devices, links and drawings?')){ S={labName:$('#labName').value,devices:[],links:[],shapes:[]}; sel=null;armed=null; fetch('/api/draft/clear',{method:'POST'}); resetView(); refresh(); } };

$('#btnSave').onclick = ()=>{
  fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload())}).then(r=>r.json()).then(r=>{
      if(r.ok){ hint('Saved '+r.path); setTimeout(()=>hint(''),2600); }
      else alert('Not saved:\n\n'+r.errors.join('\n'));
    });
};

$('#btnDeploy').onclick = ()=>{
  if(!confirm('Deploy this lab?\n\nThis writes topo.yml and runs the deploy on this host.')) return;
  const p = payload(); p.configFileOnly = $('#cfgOnly').checked;
  fetch('/api/deploy',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(p)}).then(r=>r.json()).then(r=>{
      if(!r.ok){ alert('Deploy refused:\n\n'+(r.errors||[r.message]).join('\n')); return; }
      showTab('deploy'); $('#log').textContent=''; pollLog(0);
    });
};
$('#btnStop').onclick = ()=>fetch('/api/deploy/stop',{method:'POST'});

function pollLog(off){
  fetch('/api/deploy/log?offset='+off).then(r=>r.json()).then(r=>{
    if(r.lines.length){
      const el=$('#log');
      el.textContent += r.lines.join('\n')+'\n';
      el.scrollTop = el.scrollHeight;
    }
    if(r.running) setTimeout(()=>pollLog(r.offset), 700);
    else if(r.rc!==null) $('#log').textContent += `\n=== finished, exit code ${r.rc} ===\n`;
  });
}

/* ---------- tabs ---------- */
function showTab(p){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.p===p));
  document.querySelectorAll('.pane').forEach(x=>x.classList.remove('on'));
  $('#p-'+p).classList.add('on');
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>showTab(t.dataset.p));
</script>
</body>
</html>
"""
