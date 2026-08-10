# vmm-lab-setup

Build a Juniper VMM virtual lab from a small YAML file.

You describe the devices and the cables. The script generates the VMM config,
deploys it to the pod, waits for boot, and baseline-configures every device over
the serial console.

---

## Quick start

```bash
pip3 install pyyaml jinja2 junos-eznc pexpect

python3 vmm.py --build                           # 🧩 build a topology in your browser
python3 vmm.py -t topo.yml --interfaces          # what ports can I use?
python3 vmm.py -t topo.yml --config_file_only    # just generate the config
python3 vmm.py -t topo.yml                       # deploy the lab
```

Run it from a `-vmm` pod host (e.g. `q-pod08-vmm`), where the `vmm` command lives.
`--build`, `--config_file_only` and `--interfaces` work anywhere.

---

## Build it in a browser (`--build`)

Don't want to hand-write YAML? Let the GUI do it.

```bash
python3 vmm.py --build                    # writes topo.yml
python3 vmm.py -t mylab.yml --build       # writes mylab.yml
```

Open the printed URL, then:

1. **click a device** in the left palette to drop it on the canvas
2. **type a name** — the Name box is already focused, so just type `R1` and press Enter
3. **click a port**, then **a port on another device** — that's a cable
4. watch the **Checks** tab — it goes red the moment something is wrong
5. **Save topo.yml**, or hit **Deploy** to run it right there

The port list per device is the real one, so you can only pick ports that exist:
vScapa offers odd ports only, vBrackla offers FPC1, vBalerion starts at 9.

**Name anything you like.** Call them `R1`, `PE-1`, `core_rtr` — a name must start
with a letter and use only letters, digits, `-` and `_` (no spaces or dots: the
name is pushed to the device as `set system host-name`). Duplicates are rejected.

**Own image per device.** Leave the **Image** box empty and the device uses the
default image for its type. Paste a path and only that device uses it, so you can
run two vScapas on two different builds side by side. You could always do this by
hand in YAML — give the device its own entry in `disks:` whose alias starts with
the type name (`vscapa_disk_R2`) and point that device's `disk:` at it — the GUI
just writes that for you.

**mgmt IP.** Each device shows its management IP under the Image box, and on the
canvas, as soon as the lab is up. It comes from `vmm ping`, refreshed in the
background, so it never freezes the page.

**Capture a link.** Every link has a `○ capture` button. Click it (it turns
`◉ capturing`) and a **sniffer1** VM is spliced into that link automatically —
its two interfaces are bridged for you at deploy time, then tcpdump on its
`eth` ports to see the traffic.

**Tidy up the picture.** Drag devices to move them. Every link has a small handle
at its midpoint — drag it to bend the link around a device, double-click it to
snap the link straight again.

**Annotate it.** Click a link to select it and give it a **label** (`10G core
uplink`, `VLAN 200`…). The **Drawing** buttons in the sidebar add a **box**, a
**circle** or a **text label** to the canvas — drag to move, drag the bottom-right
square to resize, and set the text, border colour, fill and font size in the
panel on the right. Use them to group a pod, ring a core, or caption a diagram.
Select anything and press `Delete` to remove it.

Labels and drawings are **decoration only**. They are saved with your canvas but
they never appear in the topology file, so a lab built with them is byte-for-byte
identical to one built without.

**Move around a big lab.** Scroll (or pinch) to zoom, drag empty canvas to pan,
and use the control in the bottom-right corner:

| | |
| --- | --- |
| `−` / `+` or the `-` / `+` keys | zoom out / in |
| click the `100%` readout, or `0` | back to 1:1 |
| `⤢` or `f` | fit the whole topology in the window |

Zoom is anchored on the pointer, so the spot you are looking at stays put.
Dragging stays exact at any zoom.

**Resize the right panel** by dragging its left edge — handy for watching a
deploy log, which is otherwise a narrow column. Double-click the edge to put it
back. Zoom, pan and panel width are remembered per browser, and if a saved view
would leave you staring at empty canvas the builder snaps back to your topology
rather than looking like it lost your work.

Your canvas is autosaved as you work, so a refresh, a closed tab or a restarted
server all bring it back exactly as you left it. **Clear** throws it away. The
draft is a hidden file next to the topology file (`.mylab.yml.builder-draft.json`)
— delete it and you start from a blank canvas.

Every check is run by `vmm.py` itself, not by the web page — the GUI can never
be more permissive than the CLI. Deploy is disabled automatically on a host with
no `vmm` command, so you can design a lab on your laptop and deploy it later.

---

## Writing a topology

Three sections: `disks`, `vms`, `links`.

```yaml
lab_name: MY-LAB

disks:
  vardbeg_disk:   /vmm/data/base_disks/default_images/default_image_vardbeg.img
  vbalerion_disk: /vmm/data/base_disks/default_images/default_image_vbalerion.img

vms:
  - hostname: r1
    type: vardbeg
    disk: vardbeg_disk

  - hostname: r2
    type: vbalerion
    disk: vbalerion_disk

links:
  - endpoints: ["r1:et-0/0/0", "r2:et-0/0/9"]
```

Two rules the generator enforces:

- a disk alias must start with its type name (`VROUTER_DISK` and `VSWITCH_DISK`
  are the two literal exceptions)
- a port must be inside that type's real range — an invalid port would otherwise
  build a link that comes up and silently passes no traffic

**`topo.yml` is the reference lab.** It contains every supported type with its
valid ports, FPCs, management interface and console name. Start there: copy the
blocks you want into your own file.

---

## Supported types

| family | types |
|---|---|
| Linux | `server` |
| vJunos | `vswitch`, `vrouter`, `vqfx` |
| classic | `vmx`, `vferrari` |
| MX | `vbugatti`, `vhamilton`, `vmaserati`, `valfaromeo` |
| EVO PTX | `vscapa`, `vardbeg`, `vbrackla`, `vbalerion`, `vbowmore` |

All types mix freely in one lab. Port ranges and gotchas are documented in
`topo.yml`, or run `--interfaces` against your own file.

---

## What a deploy does

| phase | what happens |
|---|---|
| 1 | validate the YAML |
| 2 | render `lab_topology.conf`, check pod capacity, `vmm config` |
| 3 | bind, start, wait for devices to answer `vmm ping` |
| 3.5 | bridge the capture VM's interfaces transparently (only if a link is sniffed) |
| 4 | log in over the serial console and apply a baseline config |

### Capturing traffic on a link

Mark a link with `sniffer: true` (or click `○ capture` in the builder) and the
link is re-plumbed to run *through* a **sniffer1** VM:

```
PE1 ──── eth1 [ sniffer1 ] eth2 ──── PE2
```

Because the sniffer sits inside the link, its two interfaces have to be bridged
or the link is simply cut in half. Phase 3.5 does that for you: it finds the
sniffer's IP, uploads `br.sh` and runs it once per sniffed link, building a
transparent bridge (`br1`, `br2`, …). It checks the interfaces really exist on
the VM and verifies each bridge came up with both members in it. `br.sh` is
idempotent — an existing bridge keeps its netplan file and only has the
transparency settings re-applied — so `--resume` is safe to re-run.

The bridge is a netplan file, so it survives a reboot. Then just capture:

```bash
ssh root@<sniffer-ip>
tcpdump -i eth1 -w /tmp/capture.pcap
```

#### How transparent is it?

A stock Linux bridge is *not* transparent. It swallows every frame sent to the
IEEE reserved group addresses `01:80:C2:00:00:00`–`0F`, which is exactly where
**LLDP** (`…:0E`) and **LACP** (`…:02`) live — so neighbours never see each
other and aggregated links never come up. `br.sh` fixes that and a few other
things that would otherwise distort a capture:

| Setting | Why |
| --- | --- |
| `group_fwd_mask 0xfff8` on the bridge, `0xfffd` on each port | Forward the reserved group addresses, so LLDP, LACP and friends cross the tap. The kernel refuses bits 0–2 bridge-wide, so the per-port mask is the only way to pass LACP. |
| STP off | The tap must never take part in the topology it is watching. |
| No IPv6 address, `accept-ra: no` | Stops the bridge injecting its own MLD/router-solicitation traffic into your capture. |
| `multicast_snooping 0` | No IGMP snooping means no multicast gets pruned. |
| GRO/GSO/TSO/LRO off | You capture the frames that were really on the wire, not coalesced super-frames. |
| MTU 9500 | Jumbo frames pass instead of being dropped. Only changed when it differs, because setting MTU briefly bounces the link. |

Those last settings are runtime-only, so `br.sh` also installs a
`br-transparent@<bridge>` systemd unit that re-applies them on every boot.

`br.sh` ships next to `vmm.py`. Set `VMM_SNIFFER_SCRIPT=/path/to/br.sh` to use
your own copy instead.

Most devices are a multi-VM chassis, not one VM — an EVO PTX is RE + FPC + CSPP.
Check the pod has room before a big lab:

```bash
vmm capacity -g vmm-default     # '-g' is required
```

Read **`Current largest VM available`**, not just free capacity: a 32 GB FPC
needs one blade that can hold it.

---

## Useful flags

| flag | does |
|---|---|
| `--build` | browser topology builder (pick devices, wire, deploy) |
| `--interfaces` | per-device list of valid ports |
| `--config_file_only` | generate the config, don't touch the pod |
| `--lab_detail` | summary table of the lab |
| `--serve` | interactive topology diagram in your browser |
| `--debug` | stream the serial console dialogue during phase 4 |
| `--skip_boot_wait` | skip the phase 3 ping wait |
| `--print_devices` | emit a `devices.json` for junos-mcp-server |

`--build` (8081) and `--serve` (8080) use different default ports, so both can
run at once. Change them with `--build-port` / `--port`.

---

## Notes

- The root password used for serial login defaults to the standard lab
  password. Override it with `export VMM_DEVICE_PASSWORD=...`.
- `lab_topology.conf` is generated on every run and is git-ignored.
- Retired types `vptx` and `vredbull` are rejected at validation, with a message
  naming the replacement.
