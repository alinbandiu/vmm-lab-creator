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

import errno
import json
import os
import re
import signal
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
    for l in links:
        a, b = l.get('a'), l.get('b')
        if not a or not b:
            continue
        out_links.append(
            {'endpoints': [f"{a['host']}:{a['port']}", f"{b['host']}:{b['port']}"]})

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


# =============================================================================
#  Capture sessions
# =============================================================================
_CAP_LOCK = threading.Lock()
_CAPTURES = {}
_CAP_PENDING = set()

# A capture writes into /vmm/logs as root on a filesystem the whole pod shares,
# and QEMU keeps dumping long after the browser that asked for it has gone. A
# closed tab must not leave a file growing for hours, so every capture is on a
# leash: it stops when nobody is watching, when it has run long enough, or when
# the file gets big.
_CAP_IDLE_STOP = 120              # seconds without a browser poll
_CAP_MAX_SECONDS = 20 * 60
_CAP_MAX_BYTES = 250 * 1024 * 1024
_CAP_WATCHDOG = None


class _Capture:
    """One running capture, owned by the builder process.

    The actual dump lives inside the VM's QEMU process, so it survives a browser
    refresh; this object only tracks what has already been shown.
    """

    def __init__(self, cid, vm, netdev, peer, label):
        self.id = cid
        self.vm = vm
        self.netdev = netdev
        self.peer = peer
        self.label = label
        self.dir = vmm.capture_dir(vm)
        self.path = vmm.capture_start(vm, netdev)
        # Arm the live preview now rather than on the first poll: someone who
        # starts a capture and immediately sends traffic would otherwise have
        # nothing watching for it until a few seconds later.
        _, self.preview_idx = vmm.preview_rotate(vm, netdev, self.dir, 0)
        self.total = 0
        self.running = True
        self.started = time.time()
        self.last_poll = time.time()
        self.stop_reason = ''
        self.lock = threading.Lock()

    def poll(self):
        """Advance the live packet counter.

        The preview dump is still rotated, because that is the only way to have
        a running total at all: QEMU buffers the continuous capture and flushes
        it when it is closed, so its file size says nothing while it records.
        The summaries themselves are counted and dropped - the panel reports how
        many packets crossed the link, not what they were.
        """
        with self.lock:
            self.last_poll = time.time()
            if not self.running:
                return 0
            fresh, self.preview_idx = vmm.preview_rotate(
                self.vm, self.netdev, self.dir, self.preview_idx)
            self.total += len(fresh)
            return len(fresh)

    def size(self):
        try:
            vmm._nfs_revalidate(self.path)
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def stop(self, reason=''):
        """Flush and close the capture. Safe to call twice."""
        with self.lock:
            if not self.running:
                return
            self.running = False
            self.stop_reason = reason
            vmm.preview_stop(self.vm, self.netdev, self.dir, self.preview_idx)
            vmm.capture_stop(self.vm, self.netdev)
            time.sleep(0.6)  # let QEMU's flush reach the shared filesystem

    def info(self):
        return {'id': self.id, 'vm': self.vm, 'netdev': self.netdev,
                'peer': self.peer, 'label': self.label, 'running': self.running,
                'total': self.total, 'bytes': self.size(),
                'file': self.path, 'name': os.path.basename(self.path),
                'stopReason': self.stop_reason,
                'seconds': int(time.time() - self.started)}


def _capture_watchdog():
    """Stop captures nobody is watching any more.

    Without this a closed browser tab leaves QEMU dumping to the shared
    filesystem indefinitely - which is exactly what a 'quick look at a link'
    should never do.
    """
    while True:
        time.sleep(15)
        now = time.time()
        for cap in list(_CAPTURES.values()):
            if not cap.running:
                continue
            reason = ''
            if now - cap.last_poll > _CAP_IDLE_STOP:
                reason = ('stopped automatically - the page stopped polling '
                          f'for {_CAP_IDLE_STOP}s (tab closed?)')
            elif now - cap.started > _CAP_MAX_SECONDS:
                reason = ('stopped automatically after '
                          f'{_CAP_MAX_SECONDS // 60} minutes')
            elif cap.size() > _CAP_MAX_BYTES:
                reason = ('stopped automatically at '
                          f'{_CAP_MAX_BYTES // (1024 * 1024)} MB')
            if reason:
                try:
                    cap.stop(reason)
                except Exception:
                    pass


def _ensure_watchdog():
    global _CAP_WATCHDOG
    if _CAP_WATCHDOG is None:
        _CAP_WATCHDOG = threading.Thread(
            target=_capture_watchdog, name='capture-watchdog', daemon=True)
        _CAP_WATCHDOG.start()


def capture_start_api(payload):
    """Begin capturing the link the browser right-clicked."""
    a, b = payload.get('a') or {}, payload.get('b') or {}
    host_a, host_b = a.get('host'), b.get('host')
    if not host_a or not host_b:
        return {'ok': False, 'error': 'That link has no two endpoints.'}

    cid = f"{host_a}|{a.get('port','')}|{host_b}|{b.get('port','')}"
    with _CAP_LOCK:
        existing = _CAPTURES.get(cid)
        if existing and existing.running:
            return {'ok': True, 'already': True, **existing.info()}
        # Attaching the dump takes a few seconds of monitor chatter. Claim the
        # link inside the lock so an impatient second click cannot start a
        # parallel dump and orphan the first file.
        if cid in _CAP_PENDING:
            return {'ok': False, 'error':
                    'That link is already starting - give it a few seconds.'}
        _CAP_PENDING.add(cid)

    try:
        return _capture_begin(cid, payload, host_a, host_b, a, b)
    finally:
        with _CAP_LOCK:
            _CAP_PENDING.discard(cid)


def _capture_begin(cid, payload, host_a, host_b, a, b):
    try:
        if not vmm.running_vms():
            return {'ok': False, 'error':
                    'No VMs are running - deploy the lab before capturing.'}
        # The interface names are what make this exact when the two devices are
        # wired together more than once.
        targets = vmm.resolve_capture(host_a, host_b,
                                      port_a=a.get('port'), port_b=b.get('port'))
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

    if not targets:
        return {'ok': False, 'error':
                f"{host_a} {a.get('port','')} and {host_b} {b.get('port','')} "
                f"are not wired together in the running lab. Deploy the "
                f"current topology first."}

    chosen = targets[0]
    if len(targets) > 1 and payload.get('netdev'):
        chosen = next((t for t in targets if t['netdev'] == payload['netdev']),
                      targets[0])

    label = f"{host_a} {a.get('port','')} <-> {host_b} {b.get('port','')}".strip()
    try:
        cap = _Capture(cid, chosen['vm'], chosen['netdev'], chosen['peer'], label)
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

    with _CAP_LOCK:
        _CAPTURES[cid] = cap
    _ensure_watchdog()
    out = {'ok': True, **cap.info()}
    if len(targets) > 1:
        # Only reachable when the interface could not be resolved to one wire,
        # so say which one is being recorded rather than pretending it is the
        # one that was asked for.
        out['note'] = (f"{host_a} and {host_b} have {len(targets)} links and the "
                       f"interface could not be matched to one; capturing "
                       f"{chosen['netdev']}.")
        out['choices'] = targets
    return out


def capture_poll_api(cid):
    cap = _CAPTURES.get(cid)
    if not cap:
        return {'ok': False, 'error': 'No such capture.'}
    try:
        cap.poll()
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    return {'ok': True, **cap.info()}


def capture_stop_api(cid):
    cap = _CAPTURES.get(cid)
    if not cap:
        return {'ok': False, 'error': 'No such capture.'}
    try:
        cap.stop()
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    return {'ok': True, **cap.info()}


def capture_list_api():
    return {'ok': True, 'captures': [c.info() for c in _CAPTURES.values()]}


def capture_stop_all():
    """Leave no dumps attached when the builder exits."""
    for cap in list(_CAPTURES.values()):
        try:
            cap.stop('builder stopped')
        except Exception:
            pass


def _query(handler, key):
    """One query-string value, or ''. Enough for the ids we pass around."""
    from urllib.parse import parse_qs, urlparse
    return (parse_qs(urlparse(handler.path).query).get(key) or [''])[0]


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
            if path == '/api/capture/list':
                return _json_response(self, capture_list_api())
            if path == '/api/capture/poll':
                return _json_response(self, capture_poll_api(_query(self, 'id')))
            if path == '/api/capture/download':
                cap = _CAPTURES.get(_query(self, 'id'))
                if not cap:
                    self.send_error(404)
                    return
                # The file is only complete once QEMU has flushed it, so a
                # download implicitly ends the capture rather than handing over
                # a truncated trace.
                cap.stop()
                try:
                    vmm._nfs_revalidate(cap.path)
                    with open(cap.path, 'rb') as fh:
                        blob = fh.read()
                except OSError as exc:
                    self.send_error(500, str(exc))
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.tcpdump.pcap')
                self.send_header('Content-Disposition',
                                 f'attachment; filename="{os.path.basename(cap.path)}"')
                self.send_header('Content-Length', str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
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

            if path == '/api/capture/start':
                return _json_response(self, capture_start_api(payload))

            if path == '/api/capture/stop':
                return _json_response(self, capture_stop_api(payload.get('id')))

            if path == '/api/deploy/stop':
                return _json_response(self, {'ok': DEPLOY.stop()})

            self.send_error(404)

        def log_message(self, *a):
            pass

    return Handler


# -----------------------------
# Clean shutdown on a kill signal
# -----------------------------
# Ctrl+C already exits cleanly, because KeyboardInterrupt unwinds through
# serve_builder()'s 'finally' and detaches every capture. A signal does not, and
# that difference is expensive here: the dump lives inside the VM's QEMU process
# and outlives the builder, while the watchdog that would have reaped it is a
# thread *in* the builder. Killed abruptly, a capture is left writing to a
# filesystem the whole pod shares with nothing anywhere left to stop it - not
# after 20 minutes, not at 250 MB, not ever.
#
# Both signals are ordinary events, not hypotheticals:
#   SIGHUP  - the builder runs in the foreground, so a dropped ssh session or a
#             closed laptop sends one (the startup banner above says as much).
#   SIGTERM - what 'vmm.py --stop-port' sends, and what reclaim_port() sends by
#             itself when a new builder takes the port from an old one.
#
# Raising KeyboardInterrupt is deliberate: it reuses the exact shutdown path
# Ctrl+C already takes, so there is one cleanup route rather than two. It cannot
# deadlock the way srv.shutdown() would - that blocks until serve_forever()
# returns, and the handler runs on the very thread that is sitting inside it.
def _stop_on_signal(signum, _frame):
    name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
    print(f"\n({name} received)", flush=True)
    raise KeyboardInterrupt


def _install_shutdown_signals():
    """Route SIGTERM/SIGHUP into the same clean exit as Ctrl+C.

    An inherited SIG_IGN is left alone. That matters for SIGHUP specifically:

        nohup python3 vmm.py --build --port 5057 > builder.log 2>&1 &

    is the documented way to keep the builder alive across a dropped ssh
    session, and nohup implements it by ignoring SIGHUP. Installing a handler
    unconditionally would take that back and kill the builder on exactly the
    event the user went out of their way to survive - trading a leaked capture
    for a dead server, which is a worse deal.

    Best effort otherwise: handlers can only be installed from the main thread,
    and serve_builder() is called straight from vmm.py's main(), so that holds.
    If it is ever called off-thread the error is swallowed and the server still
    serves - it just loses the tidy shutdown, which is where it was before.
    """
    for signame in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, signame, None)   # SIGHUP does not exist on Windows
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue
            signal.signal(sig, _stop_on_signal)
        except (ValueError, OSError):
            pass


def serve_builder(topo_path="topo.yml", port=8081):
    """Start the builder UI. Blocks until Ctrl+C."""
    handler = make_handler(topo_path)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    except OSError as e:
        # Almost always our own orphan: a builder whose ssh session dropped, or
        # one left behind in a closed tab. Take the port back and carry on, so
        # resuming a lab on the same port stays a single command.
        srv = None
        if getattr(e, "errno", None) == errno.EADDRINUSE and vmm.reclaim_port(port):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
            except OSError as e2:
                e = e2
        if srv is None:
            print(f"❌ Could not start the builder on port {port}: {e}", file=sys.stderr)
            if getattr(e, "errno", None) == errno.EADDRINUSE:
                print(vmm.describe_port_conflict(port), file=sys.stderr)
                print(f"   Or run the builder elsewhere:  --build --port {port + 1}",
                      file=sys.stderr)
            else:
                print(f"   Try a different port with --port <N>.", file=sys.stderr)
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
    print("  Ctrl+C to stop.", flush=True)
    if sys.stdout.isatty():
        # This server runs in the foreground, so it dies with the shell that
        # started it. On a pod that means a dropped ssh session (or a closed
        # laptop) silently takes the page down, which looks like "the port
        # stopped working" rather than "my server exited".
        print(f"  Note: it stops when this shell does. To survive an ssh drop, run:")
        print(f"        nohup python3 vmm.py --build --port {port} > builder.log 2>&1 &")
        print(f"        ...and later free the port with: python3 vmm.py --stop-port {port}")
    print("", flush=True)
    _install_shutdown_signals()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 builder stopped.")
    finally:
        # QEMU keeps dumping to the shared filesystem until the object is
        # removed, so exiting must not leave one attached.
        running = [c for c in _CAPTURES.values() if c.running]
        if running:
            print(f"   closing {len(running)} running capture(s)…", flush=True)
        capture_stop_all()
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
  /* Collapsing a panel keeps --rw intact, so unhiding restores the width you
     had dragged it to rather than snapping back to the default. */
  #app.no-left{grid-template-columns:0 1fr var(--rw,330px)}
  #app.no-right{grid-template-columns:212px 1fr 0}
  #app.no-left.no-right{grid-template-columns:0 1fr 0}
  #app.no-left aside.left{display:none}
  #app.no-right aside.right, #app.no-right #rsz{display:none}
  .pin{padding:4px 7px;font-size:12px;line-height:1}
  .pin.off{color:var(--dim);background:transparent}
  header{grid-column:1/4;display:flex;align-items:center;gap:12px;padding:0 14px;
    background:var(--panel);border-bottom:1px solid var(--line)}
  header h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.3px}
  header .sp{flex:1}
  .badge{font-size:11px;padding:3px 8px;border-radius:99px;background:var(--panel2);color:var(--dim)}
  .badge.ok{color:var(--ok)} .badge.err{color:var(--err)}

  aside{background:var(--panel);border-right:1px solid var(--line);overflow-y:auto}
  /* Explicit columns: hiding a panel with display:none takes it out of grid
     auto-placement, and main would otherwise slide into the collapsed column
     and vanish along with it. */
  aside.left{grid-column:1}
  main{grid-column:2}
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
  /* A link being captured is drawn as a travelling dashed red line, so it is
     obvious at a glance which one is recording - the tab may not be open. */
  .lnk.rec{stroke:#ff5c5c !important;stroke-width:3;stroke-dasharray:7 5;
           animation:recflow 1s linear infinite}
  @keyframes recflow{to{stroke-dashoffset:-12}}
  .lhit{stroke:transparent;stroke-width:14;fill:none;cursor:pointer}
  /* Right-click menu for links. */
  #lmenu{position:fixed;z-index:60;display:none;min-width:190px;padding:4px;
         background:#141c26;border:1px solid #2b3a4d;border-radius:8px;
         box-shadow:0 10px 30px rgba(0,0,0,.55);font-size:12px}
  #lmenu .mi{padding:7px 10px;border-radius:5px;cursor:pointer;white-space:nowrap}
  #lmenu .mi:hover{background:#1e2a38}
  #lmenu .mi.off{opacity:.4;cursor:default}
  #lmenu .mi.off:hover{background:none}
  #lmenu .mh{padding:6px 10px 7px;color:var(--dim);font-size:10.5px;
             border-bottom:1px solid #223044;margin-bottom:3px}
  /* The capture panel says what is being recorded, in one sentence, instead of
     streaming the packets themselves. The .pcap is the artefact worth having -
     a scrolling dump next to it was noise, and it could never be the whole
     truth anyway (QEMU only flushes the real file when the capture closes). */
  #capmsg{font-size:12px;line-height:1.65;color:var(--dim);background:#0c1119;
          border:1px solid var(--line);border-radius:6px;padding:10px 11px}
  #capmsg b{color:var(--fg);font-weight:600}
  #capmsg .err{color:var(--err)}
  #capmsg .note{display:block;margin-top:7px;color:var(--warn);font-size:11px;line-height:1.45}
  #capstat{font-size:11px;color:var(--dim);margin:7px 0 0}
  /* One chip per running capture, so several at once stay visible at a glance. */
  #captabs{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
  #captabs:empty{display:none}
  .capchip{cursor:pointer;background:var(--panel2);border:1px solid var(--line);
           color:var(--dim);border-radius:99px;padding:3px 9px;font-size:10px;
           font-family:inherit;white-space:nowrap}
  .capchip b{color:var(--fg);font-weight:600;margin-left:2px}
  .capchip:hover{border-color:var(--accent)}
  .capchip.on{border-color:var(--accent);color:var(--fg);background:var(--panel)}
  .capchip.live{border-color:#ff5c5c}
  .capchip .recdot{width:6px;height:6px;margin-right:4px}
  .recdot{display:inline-block;width:8px;height:8px;border-radius:50%;
          background:#ff5c5c;margin-right:6px;animation:pulse 1.1s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.25}}
  .lbl{fill:var(--dim);font-size:9.5px}
  /* Port labels are draggable along the link, so they need a grab cursor and a
     dark halo - once you make them big and bold they otherwise become unreadable
     wherever they cross the link they belong to. */
  .plbl{cursor:ew-resize;paint-order:stroke;stroke:var(--bg);stroke-width:3px;
        stroke-linejoin:round}
  .plbl:hover{fill:var(--accent)}
  .llbl{fill:#cfe0ff;font-size:11px;font-weight:600}
  .ep{fill:var(--panel2);stroke:#4a6180;stroke-width:1.5;cursor:move;r:4}
  .ep:hover{fill:var(--accent);stroke:var(--accent);r:6}
  .ep.sel{stroke:var(--accent);fill:var(--bg)}
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
  /* Click target for a text drawing. pointer-events:all makes it catch clicks
     even though it paints nothing. */
  .shp .hit{fill:none;stroke:none;pointer-events:all}
  /* A label with no text would otherwise be invisible but still eat clicks. */
  .shp .hit.ghost{stroke-width:1;stroke-dasharray:4 3;stroke-opacity:.45}
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
  .cap{cursor:pointer;color:var(--dim);background:var(--panel2);border:1px solid var(--line);
    border-radius:99px;padding:2px 7px;font-size:9.5px;font-family:inherit;white-space:nowrap}
  .cap:hover{border-color:var(--err);color:var(--err)}
  .cap.on{color:#fff;background:var(--err);border-color:var(--err);font-weight:600}

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
    <button class="pin" id="pinL" title="Hide the device palette  ( [ )">◧</button>
    <button class="pin" id="pinR" title="Hide the side panel  ( ] )">◨</button>
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
      <div class="tab" data-p="cap">Capture</div>
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
    <div class="pane" id="p-cap">
      <div id="capnone">
        Right-click any link on the canvas and choose <b>Start capture</b>.
        <div style="margin-top:8px;color:var(--dim);line-height:1.5">
          Packets are recorded inside the running VM itself, so there is no
          sniffer device to add and nothing to redeploy. Both directions of the
          link are captured, and you can record several links at the same time.
          Download the <b>.pcap</b> when you are done and open it in Wireshark.
        </div>
      </div>
      <div id="capbox" style="display:none">
        <div id="captabs"></div>
        <div id="capmsg"></div>
        <div id="capstat"></div>
        <div style="display:flex;gap:6px;margin:10px 0 0;flex-wrap:wrap">
          <button id="capStop">Stop</button>
          <button id="capDl">Download .pcap</button>
          <button id="capStopAll" class="ghost" style="display:none">Stop all</button>
          <button id="capRm" class="ghost"
                  title="Remove this finished capture from the list">Remove</button>
        </div>
      </div>
    </div>
  </aside>

  <div id="lmenu"></div>

  <div id="rsz" title="drag to resize this panel · double-click to reset"><i></i></div>
</div>

<script>
const $ = s => document.querySelector(s);
let CAT = null;                       // server catalog: types + their legal ports
let S = { labName:'MY-LAB', devices:[], links:[], shapes:[] };
let sel = null, armed = null, seq = 1;
/* Live packet capture state. Declared with the rest of the canvas state because
   drawCanvas() reads it to mark the link that is recording. */
// Several links can record at once, so captures live in a map keyed by link
// id; CAP.sel is only which one the Capture panel is showing.
const CAP = {by:{}, sel:null, timer:null};
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
  S.links.push({id:'l'+(seq++), a:armed, b:{host,port}});
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

/* ---------- collapsible side panels ----------------------------------------
   Both panels can be folded away to give the canvas the whole window, which is
   what you want once a topology is wired and you are just arranging it. The
   width the right panel was dragged to is kept, so unhiding restores it. */
let hideL=false, hideR=false;
function applyPanels(){
  const app=$('#app');
  app.classList.toggle('no-left', hideL);
  app.classList.toggle('no-right', hideR);
  const bl=$('#pinL'), br=$('#pinR');
  bl.classList.toggle('off', hideL);
  br.classList.toggle('off', hideR);
  bl.title = (hideL?'Show':'Hide')+' the device palette  ( [ )';
  br.title = (hideR?'Show':'Hide')+' the side panel  ( ] )';
  // The empty-state tells you to click the palette, which is unhelpful advice
  // while the palette is hidden.
  const em=$('#empty');
  if(em) em.innerHTML = hideL
    ? 'Press <b>[</b> or the ◧ button to bring back the device palette.'
    : 'Click a device on the left to add it.<br>Then click a port, then a port on another device.';
  applyView();                       // the canvas just changed width
}
function togglePanel(side){
  if(side==='l') hideL=!hideL; else hideR=!hideR;
  applyPanels(); saveUi();
}
$('#pinL').onclick = ()=>togglePanel('l');
$('#pinR').onclick = ()=>togglePanel('r');

/* ---------- UI state -------------------------------------------------------
   Zoom, panel width and which panels are folded away are per-browser
   preferences, not part of the lab, so they live in localStorage and never
   travel to the server or the YAML. */
const UIK='vmmb.ui';
function saveUi(){
  try{ localStorage.setItem(UIK,JSON.stringify({view:view,rw:getRw(),hl:hideL,hr:hideR})); }catch(e){}
}
function loadUi(){
  try{
    const u=JSON.parse(localStorage.getItem(UIK)||'{}');
    if(u.view && isFinite(u.view.k))
      view={x:+u.view.x||0, y:+u.view.y||0,
            k:Math.min(ZMAX,Math.max(ZMIN,+u.view.k||1))};
    if(isFinite(u.rw)) setRw(u.rw);
    hideL=!!u.hl; hideR=!!u.hr;
  }catch(e){}
  applyPanels();
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
      // The glyphs themselves are not clickable (.cap is pointer-events:none, so
      // a caption can never steal clicks from the shape under it). A text drawing
      // has nothing else in it, so without a hit area it could be neither
      // selected nor deleted. The rect is sized from the real glyph box just
      // after render - see sizeTextBoxes().
      const empty = !(p.text||'').trim();
      s+=`<g class="shp ${on?'sel':''}" data-s="${p.id}">
            <rect class="hit${empty?' ghost':''}" data-hit="${p.id}" rx="3"
                  ${empty?`stroke="${p.stroke}"`:''}/>
            <text class="cap" x="${p.x}" y="${p.y}" fill="${p.stroke}"
                  style="font-size:${p.size}px">${esc(p.text||'')}</text>
            ${on?`<rect class="body" data-hit="${p.id}" fill="none"
                    stroke="${p.stroke}" stroke-width="1" rx="3"/>`:''}
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

  let dots='', topDots='';         // drawn after the nodes, see below
  for(const l of S.links){
    const A=S.devices.find(d=>d.hostname===l.a.host), B=S.devices.find(d=>d.hostname===l.b.host);
    if(!A||!B) continue;
    // Where the link meets each device. Stored relative to the node box so the
    // anchor travels with the node when it is dragged; centre by default.
    const ea=anchor(l.ea), eb=anchor(l.eb);
    const x1=A.x+ea.x, y1=A.y+ea.y, x2=B.x+eb.x, y2=B.y+eb.y;
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
    const rec = capIsRecording(l.id);
    const col = rec?'#ff6b6b':'#4a6180';
    const curved = hasW || spread!==0;
    const d = curved ? `M${x1},${y1} Q${cx},${cy} ${x2},${y2}` : `M${x1},${y1} L${x2},${y2}`;
    // A transparent fat path under the visible one makes a 2px link easy to hit.
    s+=`<path class="lhit" data-k="${l.id}" d="${d}"/>`;
    s+=`<path class="lnk ${sel===l.id?'sel':''} ${rec?'rec':''}" data-k="${l.id}" d="${d}" fill="none"
              stroke="${col}" stroke-width="2"/>`;
    // Port labels ride along the curve so they follow a bent link, at whatever
    // point along it you dragged them to. With the control point at the midpoint
    // a quadratic is exactly the straight segment, so one formula covers both.
    const at = t => ({x:(1-t)*(1-t)*x1+2*(1-t)*t*cx+t*t*x2, y:(1-t)*(1-t)*y1+2*(1-t)*t*cy+t*t*y2});
    const pa = at(labT(l,'a')), pb = at(labT(l,'b'));
    const fs = labSize(l), fw = labWeight(l);
    const pstyle = `font-size="${fs}" font-weight="${fw}"`;
    s+=`<text class="lbl plbl" data-pl="${l.id}" data-e="a" ${pstyle}
              x="${pa.x}" y="${pa.y-4}" text-anchor="middle">${l.a.port}
          <title>drag to slide along the link · double-click to recentre</title></text>`;
    s+=`<text class="lbl plbl" data-pl="${l.id}" data-e="b" ${pstyle}
              x="${pb.x}" y="${pb.y-4}" text-anchor="middle">${l.b.port}
          <title>drag to slide along the link · double-click to recentre</title></text>`;
    let dy = -12;
    if(l.label){ s+=`<text class="llbl" x="${mx}" y="${my+dy}" text-anchor="middle">${esc(l.label)}</text>`; dy -= 13; }
    if(rec) s+=`<text class="lbl" x="${mx}" y="${my+dy}" text-anchor="middle" fill="#ff6b6b">● recording</text>`;
    s+=`<circle class="wp ${hasW?'on':''}" data-w="${l.id}" cx="${mx}" cy="${my}" r="6">
          <title>drag to bend this link · double-click to straighten</title></circle>`;
    // Endpoint handles go above the nodes - they sit on the device box, so drawn
    // in link order they would be buried under it and impossible to grab.
    // Parallel links all default to the same anchor, so their handles stack; the
    // selected link's go last, which is what makes a specific one reachable.
    const dot = (e,x,y) => `<circle class="ep ${sel===l.id?'sel':''}" data-ep="${l.id}"
          data-e="${e}" cx="${x}" cy="${y}" r="4">
          <title>drag to move where this link meets ${e==='a'?esc(l.a.host):esc(l.b.host)}`
          + ` · double-click to recentre</title></circle>`;
    const pair = dot('a',x1,y1) + dot('b',x2,y2);
    if(sel===l.id) topDots += pair; else dots += pair;
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
  s += dots + topDots;             // endpoint handles sit on top of the devices
  svg.innerHTML=s;
  sizeTextBoxes(svg);
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
    p.oncontextmenu = e => { e.preventDefault(); e.stopPropagation();
                             sel=p.dataset.k; refresh(); openLinkMenu(e, p.dataset.k); };
  });
  svg.querySelectorAll('.wp').forEach(c=>{
    c.onmousedown = e => { e.stopPropagation(); startWpDrag(e, c.dataset.w); };
    c.ondblclick  = e => { e.stopPropagation();
      const l=S.links.find(x=>x.id===c.dataset.w); if(l){ delete l.w; refresh(); } };
  });
  svg.querySelectorAll('.plbl').forEach(t=>{
    t.onmousedown = e => { e.stopPropagation(); ldrag={id:t.dataset.pl, end:t.dataset.e}; };
    t.onclick     = e => { e.stopPropagation(); sel=t.dataset.pl; refresh(); showTab('insp'); };
    t.ondblclick  = e => { e.stopPropagation();
      const l=S.links.find(x=>x.id===t.dataset.pl);
      if(l){ delete l[t.dataset.e==='a'?'ta':'tb']; refresh(); } };
  });
  svg.querySelectorAll('.ep').forEach(c=>{
    c.onmousedown = e => { e.stopPropagation(); edrag={id:c.dataset.ep, end:c.dataset.e}; };
    c.onclick     = e => { e.stopPropagation(); sel=c.dataset.ep; refresh(); showTab('insp'); };
    c.ondblclick  = e => { e.stopPropagation();
      const l=S.links.find(x=>x.id===c.dataset.ep);
      if(l){ delete l[c.dataset.e==='a'?'ea':'eb']; refresh(); } };
  });
  svg.onclick = e => {
    if(panned){ panned=false; return; }        // that click was a pan, not a deselect
    if(e.target.id==='cv' && sel){ sel=null; refresh(); }
  };
}

let drag=null, wdrag=null, sdrag=null, rdrag=null, ldrag=null, edrag=null;
// Port-label defaults. Kept here rather than inline so a link that has never
// been touched and one that was explicitly reset render identically.
const PLS=9.5, PLW=400, PLA=0.26, PLB=0.74;
const labSize   = l => isFinite(l.ps) ? l.ps : PLS;
const labWeight = l => isFinite(l.pw) ? l.pw : PLW;
const labT      = (l,e) => { const v = e==='a' ? l.ta : l.tb;
                             return isFinite(v) ? v : (e==='a'?PLA:PLB); };
const anchor    = a => (a && isFinite(a.x) && isFinite(a.y)) ? a : {x:W/2, y:H/2};
// Nearest point on the drawn curve to the cursor. Sampling beats solving the
// cubic for dp/dt: it is exact enough at any zoom and cannot pick a root that
// lies off the segment.
function nearestT(pt, x1, y1, cx, cy, x2, y2){
  let best=0.5, bd=Infinity;
  for(let i=0;i<=120;i++){
    const t=i/120, u=1-t;
    const x=u*u*x1+2*u*t*cx+t*t*x2, y=u*u*y1+2*u*t*cy+t*t*y2;
    const d=(x-pt.x)**2+(y-pt.y)**2;
    if(d<bd){ bd=d; best=t; }
  }
  return Math.min(0.95, Math.max(0.05, best));
}
// Geometry of one link, shared by the renderer and the drag handlers so a label
// dragged to a spot lands exactly where the cursor is.
function linkGeom(l){
  const A=S.devices.find(d=>d.hostname===l.a.host), B=S.devices.find(d=>d.hostname===l.b.host);
  if(!A||!B) return null;
  const ea=anchor(l.ea), eb=anchor(l.eb);
  const x1=A.x+ea.x, y1=A.y+ea.y, x2=B.x+eb.x, y2=B.y+eb.y;
  const same = m => (m.a.host<m.b.host? m.a.host+'\u0000'+m.b.host : m.b.host+'\u0000'+m.a.host);
  const peers = S.links.filter(m=>same(m)===same(l));
  const n=peers.length, i=peers.findIndex(m=>m.id===l.id);
  const spread = n>1 ? (i-(n-1)/2)*FAN : 0;
  const vlen=Math.hypot(x2-x1,y2-y1)||1;
  const nx=-(y2-y1)/vlen, ny=(x2-x1)/vlen;
  const hasW = l.w && isFinite(l.w.x) && isFinite(l.w.y);
  const mx = hasW ? l.w.x : (x1+x2)/2 + nx*spread;
  const my = hasW ? l.w.y : (y1+y2)/2 + ny*spread;
  return {A,B,x1,y1,x2,y2, cx:2*mx-(x1+x2)/2, cy:2*my-(y1+y2)/2};
}
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
  if(ldrag){
    const l=S.links.find(x=>x.id===ldrag.id); if(!l) return;
    const g=linkGeom(l); if(!g) return;
    const t=nearestT(svgPt(e), g.x1,g.y1,g.cx,g.cy,g.x2,g.y2);
    l[ldrag.end==='a'?'ta':'tb']=t; drawCanvas(); return;
  }
  if(edrag){
    const l=S.links.find(x=>x.id===edrag.id); if(!l) return;
    const dev=S.devices.find(d=>d.hostname===(edrag.end==='a'?l.a.host:l.b.host));
    if(!dev) return;
    // Clamped to the device box: an endpoint that could roam free would leave
    // the link visibly detached from the device it is wired to.
    const pt=svgPt(e);
    l[edrag.end==='a'?'ea':'eb'] = {x: Math.min(W, Math.max(0, pt.x-dev.x)),
                                    y: Math.min(H, Math.max(0, pt.y-dev.y))};
    drawCanvas(); return;
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
  if(drag||wdrag||sdrag||rdrag||ldrag||edrag) saveDraft();   // persist the new layout
  if(pdrag){ $('#cv').classList.remove('panning'); pdrag=null; saveUi(); }
  if(rzdrag){ rzdrag=false; document.body.classList.remove('rsz-on');
              $('#rsz').classList.remove('on'); saveUi(); }
  drag=null; wdrag=null; sdrag=null; rdrag=null; ldrag=null; edrag=null;
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){ armed=null; hint(''); refresh(); }
  if((e.key==='Delete'||e.key==='Backspace') && sel && e.target.tagName!=='INPUT') removeSelected();
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
  if(e.key==='+'||e.key==='='){ e.preventDefault(); zoomBy(ZSTEP); }
  else if(e.key==='-'||e.key==='_'){ e.preventDefault(); zoomBy(1/ZSTEP); }
  else if(e.key==='0') resetView();
  else if(e.key==='f'||e.key==='F') fitView();
  else if(e.key==='[') togglePanel('l');
  else if(e.key===']') togglePanel('r');
});

/* A text drawing is clickable through a transparent rect rather than through its
   glyphs, so the gaps between letters count too and an empty label still has
   something to grab. The box can only be measured once the text is in the DOM. */
function sizeTextBoxes(svg){
  svg.querySelectorAll('.shp [data-hit]').forEach(r=>{
    const g = r.parentNode, t = g.querySelector('text');
    const p = S.shapes.find(x=>x.id===r.dataset.hit);
    if(!t || !p) return;
    let bb;
    try { bb = t.getBBox(); } catch(_) { bb = null; }
    // An empty <text> measures as nothing, so fall back to a box big enough to
    // aim at. Same for a shape that has not been laid out yet.
    const box = (bb && bb.width > 1)
      ? {x:bb.x-6, y:bb.y-4, w:bb.width+12, h:bb.height+8}
      : {x:p.x-6, y:p.y-p.size-2, w:64, h:p.size+10};
    r.setAttribute('x', box.x);       r.setAttribute('y', box.y);
    r.setAttribute('width',  Math.max(box.w, 18));
    r.setAttribute('height', Math.max(box.h, 14));
  });
}

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
              <button id="shdel" title="Delete this drawing">Delete</button></div>`;
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
              <button id="lkdel" title="Delete the whole link, not just its label">Delete</button></div>`;
      s += `<div class="meta">${esc(A.host)}:${A.port} ↔ ${esc(B.host)}:${B.port}</div>`;
      const recK = capIsRecording(k.id);
      s += `<div style="display:flex;gap:6px;margin:8px 0">
              <button class="cap ${recK?'on':''}" data-cap="${k.id}"
                      title="Record live traffic on this link">${recK?'■ stop capture':'● capture'}</button>
              <button id="lkstr" ${k.w?'':'disabled'}>Straighten</button></div>`;
      s += `<div class="sect" style="margin:10px -12px 6px;position:static">Interface labels</div>`;
      s += `<div class="crow"><span style="width:52px">Size</span>
              <input type="range" id="lkps" min="8" max="26" step="0.5" value="${labSize(k)}">
              <span style="width:34px">${labSize(k)}px</span></div>`;
      s += `<div class="crow"><span style="width:52px">Weight</span>
              <input type="range" id="lkpw" min="300" max="900" step="100" value="${labWeight(k)}">
              <span style="width:34px">${labWeight(k)}</span></div>`;
      s += `<div style="display:flex;gap:6px;margin:6px 0">
              <button id="lkpall" title="Use this size and weight on every link">Apply to all links</button>
              <button id="lkprst" title="Back to the default size, weight and positions">Reset</button></div>`;
      s += `<div class="meta">Drag an interface name to slide it along the link, or the
              dot at either end to move where the link meets the device. Double-click
              either to recentre it. Drag the handle mid-link to bend it. All of this is
              decoration - it never reaches the topology file.</div>`;
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
  s += `<div class="sect" style="margin:12px -12px 6px;position:static">Links (${S.links.length})</div>`;
  s += S.links.length ? S.links.map(l=>{
      const r = capIsRecording(l.id);
      return `<div class="lrow">
         <span class="lbl2">${esc(l.a.host)}:${l.a.port} ↔ ${esc(l.b.host)}:${l.b.port}</span>
         <button class="cap ${r?'on':''}" data-cap="${l.id}"
                 title="Record live traffic on this link">${r?'■ stop':'● capture'}</button>
         <span class="x" data-l="${l.id}" title="delete link">✕</span></div>`; }).join('')
    : `<div style="color:var(--dim);font-size:11px">No links yet.</div>`;
  if(S.links.length) s += `<div class="meta">Capture records inside the running VM - deploy the lab first, then hit ● capture (or right-click the link).</div>`;
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
    // Sliders redraw live but deliberately do not rebuild the inspector, which
    // would yank focus out of the slider mid-drag.
    const slider = (id, prop, suffix) => {
      const el=$(id); if(!el) return;
      el.oninput = e => {
        lk[prop] = parseFloat(e.target.value);
        const out = e.target.parentElement.querySelector('span:last-child');
        if(out) out.textContent = e.target.value + suffix;
        drawCanvas();
      };
      el.onchange = () => saveDraft();
    };
    slider('#lkps', 'ps', 'px');
    slider('#lkpw', 'pw', '');
    $('#lkpall').onclick = ()=>{
      const ps=labSize(lk), pw=labWeight(lk);
      S.links.forEach(l=>{ l.ps=ps; l.pw=pw; });
      refresh();
    };
    $('#lkprst').onclick = ()=>{
      ['ps','pw','ta','tb','ea','eb'].forEach(k=>delete lk[k]);
      refresh();
    };
  }
  p.querySelectorAll('.x').forEach(el=>
    el.onclick=()=>{ S.links=S.links.filter(l=>l.id!==el.dataset.l); refresh(); });
  p.querySelectorAll('.cap').forEach(el=>
    el.onclick=()=>{
      const id = el.dataset.cap;
      if(capIsRecording(id)) capStop(id); else capStart(id);
    });
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
      // The log lives in the right panel, so a hidden panel would make a deploy
      // look like it did nothing at all.
      if(hideR) togglePanel('r');
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

/* ---------- packet capture ---------- */
/* The dump runs inside the VM's own QEMU process, and QEMU only flushes it when
   it is closed - so while a capture runs its file size and contents tell you
   nothing. The server therefore rotates a second, short-lived dump on the same
   NIC purely to keep a packet counter moving. The frames it decodes are counted
   and thrown away: the panel says what is being recorded and how much of it has
   gone by, and the .pcap you download is the uninterrupted one. */

function linkById(id){ return S.links.find(l=>l.id===id) || null; }

function openLinkMenu(e, id){
  const l = linkById(id); if(!l) return;
  const busy = capIsRecording(id);
  const done = CAP.by[id] && CAP.by[id].id;   // finished, file still downloadable
  const m = $('#lmenu');
  m.innerHTML =
    `<div class="mh">${esc(l.a.host)} ${esc(l.a.port)} &harr; ${esc(l.b.host)} ${esc(l.b.port)}</div>` +
    (busy
      ? `<div class="mi" data-a="stop">■ Stop capture</div>
         <div class="mi" data-a="dl">⭳ Stop &amp; download .pcap</div>
         <div class="mi" data-a="show">☰ Show capture</div>`
      : `<div class="mi" data-a="start">● Start capture</div>` +
        (done ? `<div class="mi" data-a="dl">⭳ Download last .pcap</div>` : '')) +
    `<div class="mi" data-a="del">✕ Delete link</div>`;
  m.style.display='block';
  // Keep the menu on screen when the click lands near an edge.
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(e.clientX, innerWidth  - r.width  - 8) + 'px';
  m.style.top  = Math.min(e.clientY, innerHeight - r.height - 8) + 'px';
  m.querySelectorAll('.mi').forEach(mi=>mi.onclick=()=>{
    if(mi.classList.contains('off')) return;
    closeLinkMenu();
    const a = mi.dataset.a;
    if(a==='start') capStart(id);
    else if(a==='stop') capStop(id);
    else if(a==='dl') capDownload(id);
    else if(a==='show') showTab('cap');
    else if(a==='del'){ S.links=S.links.filter(x=>x.id!==id); sel=null; refresh(); }
  });
}
function closeLinkMenu(){ $('#lmenu').style.display='none'; }
document.addEventListener('mousedown', e=>{ if(!e.target.closest('#lmenu')) closeLinkMenu(); });
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeLinkMenu(); });
window.addEventListener('blur', closeLinkMenu);

// The canvas shows the recording animation and the panels show the stop
// buttons, so both have to follow the capture state - but without saveDraft(),
// which has no business running every few seconds behind a poll.
function capRedraw(){ drawCanvas(); drawInspector(); }

function capStart(id){
  const l = linkById(id); if(!l) return;
  if(CAP.by[id] && CAP.by[id].running) { CAP.sel = id; showTab('cap'); capRenderAll(); return; }
  showTab('cap');
  // The two ends are copied in rather than looked up from the link on every
  // render, so the sentence still names them if the link is deleted from the
  // canvas while the capture is running.
  CAP.by[id] = {id:null, link:id, running:false, starting:true, name:'', note:'',
                a:{host:l.a.host, port:l.a.port}, b:{host:l.b.host, port:l.b.port},
                label:`${l.a.host} ${l.a.port} \u2194 ${l.b.host} ${l.b.port}`,
                total:0, bytes:0, seconds:0, stopReason:''};
  CAP.sel = id;
  capRenderAll();
  fetch('/api/capture/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({a:l.a,b:l.b})}).then(r=>r.json()).then(r=>{
      const c = CAP.by[id]; if(!c) return;
      c.starting = false;
      if(!r.ok){ c.error = r.error; c.running = false; capRenderAll(); capRedraw(); return; }
      c.id=r.id; c.running=true; c.name=r.name;
      if(r.note) c.note = r.note;
      capRedraw(); capRenderAll();
      capSchedule();
    }).catch(err=>{ const c=CAP.by[id]; if(c){ c.starting=false; c.error=String(err); }
                    capRenderAll(); });
}

/* One timer drives every running capture. Polling is what rotates the preview
   dump server-side, which is what keeps each packet counter moving, so they all
   have to be kept turning - not just the one on screen. It is also the browser's
   only "still here" signal: the server stops a capture nobody has polled for
   two minutes, so a poll that covers only the selected capture would let the
   others be reaped as abandoned. */
function capSchedule(){
  clearTimeout(CAP.timer);
  CAP.timer = setTimeout(capPollAll, 3000);
}

function capPollAll(){
  const live = Object.values(CAP.by).filter(c=>c.running && c.id);
  if(!live.length) return;
  Promise.all(live.map(c =>
    fetch('/api/capture/poll?id='+encodeURIComponent(c.id))
      .then(r=>r.json())
      .then(r=>{
        if(!r.ok){ c.error = r.error||''; c.running = false; return; }
        c.running=r.running; c.total=r.total; c.bytes=r.bytes;
        c.seconds=r.seconds; c.stopReason=r.stopReason||'';
      })
      .catch(()=>{})
  )).then(()=>{
    capRenderAll(); capRedraw();
    if(Object.values(CAP.by).some(c=>c.running)) capSchedule();
  });
}

/* ---- rendering ---- */
function capRenderAll(){
  const all = Object.values(CAP.by);
  $('#capnone').style.display = all.length ? 'none' : '';
  $('#capbox').style.display  = all.length ? 'block' : 'none';
  if(!all.length) return;
  if(!CAP.by[CAP.sel]) CAP.sel = all[all.length-1].link;

  // One chip per capture, so several running at once stay visible and
  // switchable instead of the panel only ever knowing about the last one.
  $('#captabs').innerHTML = all.map(c=>{
    const on = c.link===CAP.sel;
    const dot = c.running ? '<span class="recdot"></span>' : (c.error?'✕ ':'■ ');
    return `<button class="capchip ${on?'on':''} ${c.running?'live':''}" data-ct="${c.link}"
              title="${esc(c.label)}">${dot}${esc(shortLabel(c))} <b>${c.total}</b></button>`;
  }).join('');
  $('#captabs').querySelectorAll('[data-ct]').forEach(b=>
    b.onclick = ()=>{ CAP.sel = b.dataset.ct; capRenderAll(); });

  const nlive = all.filter(c=>c.running).length;
  $('#capStopAll').style.display = nlive > 1 ? '' : 'none';
  $('#capStopAll').textContent = `Stop all (${nlive})`;

  const c = CAP.by[CAP.sel];
  $('#capStop').disabled = !c.running;
  $('#capDl').disabled   = !c.id;
  // Removing a capture that is still recording would strand the dump inside
  // QEMU with nothing left on screen to stop it.
  $('#capRm').disabled   = !!c.running;

  $('#capmsg').innerHTML = capSentence(c)
    + (c.note ? `<span class="note">${esc(c.note)}</span>` : '');

  if(c.starting){
    $('#capstat').textContent = 'attaching to the running VM, this takes a few seconds…';
  } else if(c.error){
    $('#capstat').textContent = '';
  } else {
    // QEMU buffers the continuous dump, so its file size lags far behind what
    // has actually been recorded - reporting it live would read '0.0 KB' beside
    // a rising packet count. The size is only meaningful once the file is closed.
    const kb = (c.bytes/1024).toFixed(1);
    $('#capstat').innerHTML = `${c.total} packet${c.total===1?'':'s'} · ${c.seconds}s`
      + (c.running
          ? ' · the .pcap keeps everything, including anything counted after this'
          : ` · ${kb} KB`)
      + (!c.running && c.stopReason?` · ${esc(c.stopReason)}`:'');
  }
}

/* One sentence naming what is being recorded and where. This replaced a live
   packet dump: the list could never be complete (QEMU flushes the real file
   only when the capture closes), so it invited people to trust the wrong
   artefact. The .pcap is the answer; this just says it is being written. */
function capSentence(c){
  const where = `link <b>${esc(c.a.port)} \u2194 ${esc(c.b.port)}</b> between nodes `
              + `<b>${esc(c.a.host)}</b>, <b>${esc(c.b.host)}</b>`;
  if(c.error)    return `<span class="err">\u2715 ${esc(c.error)}</span>`;
  if(c.starting) return `Starting capture on ${where}\u2026`;
  if(c.running)  return `<span class="recdot"></span>Traffic is captured on ${where}`;
  return `Capture stopped on ${where}. Download the .pcap to open it in Wireshark.`;
}

/* Chips have to stay narrow, so name the far ends rather than the full link.
   Read from the capture's own copy, so a chip still names its devices after the
   link has been deleted from the canvas. */
function shortLabel(c){
  return `${c.a.host}\u2194${c.b.host}`;
}

function capStop(id){
  const c = CAP.by[id || CAP.sel]; if(!c || !c.id || !c.running) return;
  c.running = false;
  $('#capstat').textContent='stopping…';
  fetch('/api/capture/stop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:c.id})}).then(r=>r.json()).then(r=>{
      if(r.ok){ c.total=r.total; c.bytes=r.bytes; c.seconds=r.seconds;
                c.stopReason=r.stopReason||''; }
      capRenderAll(); capRedraw();
    });
}

function capStopAll(){
  Object.values(CAP.by).filter(c=>c.running).forEach(c=>capStop(c.link));
}

function capDownload(id){
  const c = CAP.by[id || CAP.sel]; if(!c || !c.id) return;
  c.running = false; capRedraw();
  // Downloading closes the capture first, so the file is complete rather than
  // whatever QEMU happened to have flushed.
  location.href = '/api/capture/download?id='+encodeURIComponent(c.id);
  setTimeout(()=>{ fetch('/api/capture/poll?id='+encodeURIComponent(c.id))
    .then(r=>r.json()).then(r=>{ if(r.ok){ c.total=r.total; c.bytes=r.bytes;
      c.seconds=r.seconds; c.running=r.running; c.stopReason=r.stopReason||''; }
      capRenderAll(); capRedraw(); }); }, 1200);
}

function capIsRecording(id){ const c=CAP.by[id]; return !!(c && c.running); }

$('#capStop').onclick   = ()=>capStop();
$('#capDl').onclick     = ()=>capDownload();
$('#capStopAll').onclick= capStopAll;
/* Drop a finished capture from the panel. Without this the chip list only ever
   grows: one entry per link you have ever recorded, for the life of the page.
   The .pcap on the pod is untouched - this only forgets it here. */
$('#capRm').onclick     = ()=>{
  const c = CAP.by[CAP.sel];
  if(!c || c.running) return;
  delete CAP.by[c.link];
  CAP.sel = null;
  capRenderAll(); capRedraw();
};
// Closing the tab would otherwise leave QEMU dumping until the server's
// watchdog notices. sendBeacon still goes out during unload.
window.addEventListener('pagehide', ()=>{
  if(!navigator.sendBeacon) return;
  Object.values(CAP.by).filter(c=>c.running && c.id).forEach(c=>
    navigator.sendBeacon('/api/capture/stop',
      new Blob([JSON.stringify({id:c.id})], {type:'application/json'})));
});

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
