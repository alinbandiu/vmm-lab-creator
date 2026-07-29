# 🧠 VMM Orchestrator

Build, deploy and configure multi-vendor Junos lab topologies on a QPOD from a
single YAML file. You describe the devices and links in `topo.yml`; the
orchestrator generates the VMM config, starts the lab, waits for the devices to
boot, and applies a per-type baseline over the serial console.

---

## What it does

Running `python3 vmm.py` executes four phases:

1. **Generate configuration** – validate `topo.yml` and render `lab_topology.conf` from `lab_template.j2`.
2. **Start the lab** – `vmm unbind` / `vmm config` / `vmm start`.
3. **Wait for boot** – poll `vmm ping` until each device's routing engine is reachable.
4. **Apply baseline** – open each device's serial console in parallel and push its baseline config (hostname, root auth, management, interface descriptions).

At the end it prints a deployment summary with device states and management IPs.

---

## Files

| File | Purpose |
|------|---------|
| `vmm.py` | The orchestrator. |
| `lab_template.j2` | Jinja2 template that renders the VMM `.conf` from the topology. |
| `topo.yml` | Your working topology (edit this). |
| `topo_reference.yml` | **Reference catalogue of every VM type** with the compatibility matrix and per-type interface notes. Copy and trim it to start a new lab. |
| `topo_valfaromeo.yml` | Standalone vAlfaRomeo + vFerrari lab. |
| `topo_mixed.yml` | Example vMX + vFerrari + vAlfaRomeo lab. |
| `lab_topology.conf` | Generated output (overwritten each run). |

---

## Supported VM types

| Type | Role | Mgmt | Disk alias | Interfaces | Ordering rule |
|------|------|------|-----------|-----------|---------------|
| `server` | Linux host | em0 | `server_disk` | `em1`, `em2`, … | sequential from `em1` |
| `sniffer` | Traffic capture | em0 | `sniffer_disk` | (spliced via `sniffer: true`) | — |
| `vswitch` | Switching | fxp0 | `VSWITCH_DISK` | `ge-0/0/N` | sequential from 0 |
| `vrouter` | Routing | fxp0 | `VROUTER_DISK` | `ge-0/0/N` | sequential from 0 |
| `vqfx` | Switching | em0 | `vqfx_disk` | `xe-0/0/N` | sequential from 0 |
| `vmx` | Routing | fxp0 | `vmx_disk` | multi-FPC catalogue (below) | any subset |
| `vferrari` | Routing | fxp0 | `vferrari_disk` | `et-0/0/0` … `et-0/0/4` | any subset |
| `valfaromeo` | Routing | em0 | `valfaromeo_disk` | `et-<0-1>/0/<0-3>:<0-3>` (FPC0 + FPC1) | any subset |
| `vptx` | Routing | em0 | `vptx_disk` | `et-0/0/<port>:<0-3>` | combined index sequential from 0 |
| `vscapa` | EVO | re0:mgmt-0 | `vscapa_disk` | `et-0/0/N` | **odd** N, sequential from 1 |
| `vbrackla` | EVO | re0:mgmt-0 | `vbrackla_disk` | `et-1/0/N:0` | sequential from 0 |

**vmx interface catalogue** (use any subset, in any order):

```
FPC0: ge-0/0/0-9, ge-0/1/0-9, xe-0/2/0-1, xe-0/3/0-1
FPC1: xe-1/0/0-5:0-3      FPC2: xe-2/0/0-5:0-3
FPC3: et-3/0/0-5          FPC5: xe-5/0/0-11        (no FPC4)
```

Disk aliases for `vmx`, `vqfx`, `vptx`, `vferrari` and `valfaromeo` **must start
with the type name** — the template keys off that prefix. The management
interface is set automatically per type; it is not something you edit in the
topology.

---

## Compatibility

Most types mix freely. Only these combinations are forbidden, because the
profiles ship VMM macro headers that redefine the same macros with conflicting
values:

- `valfaromeo` **✗** `vptx` / `vscapa` / `vbrackla` — vAlfaRomeo ships its own `common.vptx.defs`.
- `vscapa` **✗** `vbrackla` — conflicting EVO macro headers.

`vmm.py` rejects an illegal mix during validation (Phase 1), before anything is
deployed, and names the reason. The full matrix lives at the top of
`topo_reference.yml`.

---

## Setup

On the QPOD:

```bash
pip3 install --user virtualenv --index-url https://pypi.org/simple
python3 -m virtualenv venv
source venv/bin/activate
pip3 install pyyaml jinja2 junos-eznc paramiko pexpect --index-url https://pypi.org/simple
```

---

## Define a topology

Start from `topo_reference.yml` — it contains every type with its interfaces
documented inline and the compatibility matrix at the top. Copy it, delete what
you don't need, and edit the rest. A topology has three sections:

```yaml
lab_name: DEMO-LAB

disks:
  vmx_disk:     /vmm/data/base_disks/junos/vmx/junos-virtual-x86-64-23.4R2-S3.9.vmdk
  VROUTER_DISK: /vmm/data/base_disks/junos/vmx/vJunos-router-24.2R2-S1.6.qcow2
  server_disk:  /vmm/data/base_disks/ubuntu/ubuntu-22.04.qcow2

vms:
  - hostname: R1
    type: vmx
    disk: vmx_disk
  - hostname: R2
    type: vrouter
    disk: VROUTER_DISK
  - hostname: server
    type: server
    disk: server_disk
    ncpus: 2
    memory: 2048

links:
  - endpoints: ["server:em1", "R1:ge-0/0/0"]
  - endpoints: ["R1:ge-0/0/1", "R2:ge-0/0/0"]
    sniffer: true                 # splice the Sniffer VM inline (P2P links only)
```

Validate your edits without deploying anything:

```bash
python3 vmm.py -t topo_reference.yml --config_file_only
```

---

## Run

```bash
python3 vmm.py                       # default topo.yml
python3 vmm.py -t topo_reference.yml # a specific topology
```

Useful flags:

| Flag | Effect |
|------|--------|
| `-t, --topology FILE` | Topology file (default `topo.yml`). |
| `-o, --output FILE` | Generated VMM config filename (default `lab_topology.conf`). |
| `--config_file_only` | Validate + generate the `.conf` and exit (no deploy). |
| `--lab_detail` | Print the deployment summary + link map and exit. |
| `--config` | Enter config-management mode (get/push device configs). |
| `--skip_boot_wait` | Skip the Phase 3 ping wait and go straight to configuration. |
| `--boot_wait SECONDS` | Cap on the Phase 3 wait (default 900). |
| `--debug` | Stream the full serial dialogue per device (use when a device looks stuck). |

---

## Inspect and manage

Deployment summary at any time:

```bash
python3 vmm.py --lab_detail
```

Get or push device configs (over SSH/NETCONF):

```bash
python3 vmm.py --config
# choose 'get' or 'push', then a folder name
# ✅ Configuration for R1 (10.52.39.148) saved to default-config/R1.conf
```

---

## Packet capture (sniffer)

Mark a **point-to-point** link with `sniffer: true` and the Sniffer VM is
spliced in-line automatically. On the Sniffer:

```bash
root@ubuntu:~# brctl show
bridge name   bridge id           STP enabled  interfaces
br1           8000.fe0ca71458f2   no           eth1
                                                eth2
root@ubuntu:~# tcpdump -i br1
```

> The sniffer does **not** support LACP — only use it on P2P links.

---

## Environment overrides

Credentials and helper-asset paths default to the original values but can be
overridden so the tool is portable across pods and user accounts:

| Variable | Default | Purpose |
|----------|---------|---------|
| `VMM_DEVICE_USER` | `root` | Login user for all Junos devices. |
| `VMM_DEVICE_PASSWORD` | `Embe1mpls` | Root password set on / used to reach devices. |
| `VMM_SCRIPTS_DIR` | `/homes/balinfilipga/scripts` | Base dir for helper scripts. |
| `VMM_SNIFFER_SCRIPT` | `$VMM_SCRIPTS_DIR/br.sh` | Sniffer bridge script uploaded to the Sniffer VM. |

```bash
VMM_DEVICE_PASSWORD='MyLabPass' VMM_SCRIPTS_DIR=/homes/$USER/scripts python3 vmm.py
```

---

## How devices are configured

Every Junos device is configured over its **serial console** (`vmm serial -t
<name>_RE`), not SSH — serial works even before the management interface has an
address, so it never cuts off its own session. Each type's baseline lives in
`vmm.py`:

- `configure_vjunos_serial` — vrouter / vswitch
- `configure_vmx_serial` — vmx, vFerrari, vAlfaRomeo (baseline selected via `VMX_BASELINE_LINES` / `VFERRARI_BASELINE_LINES` / `VALFAROMEO_BASELINE_LINES`)
- `configure_vptx_serial`, `configure_vscapa_serial`, `configure_vbrackla_serial`
- `configure_vqfx` — over telnet

The vmx-family baseline is built by `_vmx_baseline()`. vmx keeps
`set chassis fpc 3 pic 0 pic-mode 40G` (it has an FPC3); vFerrari and
vAlfaRomeo drop it and vFerrari adds `set forwarding-options hyper-mode`.

> If a device stalls in Phase 4, re-run with `--debug`. If you see repeated
> `Login incorrect`, the root password baked into that image doesn't match
> `VMM_DEVICE_PASSWORD` — set the env var accordingly.

---

## Adding a new VM type (developer note)

A new type touches four places in `vmm.py` / `lab_template.j2`:

1. **Validation** – add an interface pattern (and disk-alias / sequential rule) in `validate_topology()`; if it conflicts with another profile's headers, add an entry to `INCOMPATIBLE_TYPE_GROUPS`.
2. **Template** – add a `CASE` block in `lab_template.j2` and gate any type-specific `#include`s on `'<type>' in types`.
3. **Baseline + dispatch** – add a `configure_*` worker (or reuse one) and wire it into the Phase 4 dispatch in `main()`.
4. **Docs** – add a row to the table above and a commented block in `topo_reference.yml`.

Always validate against a **known-good hand-written VMM config** for the new
type — diffing the rendered output against it is how the vFerrari / vAlfaRomeo
templates were verified.

---

© Juniper Networks, Inc. – *For internal use only.*
