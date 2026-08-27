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

> **`vhamilton`, `vmaserati` and `valfaromeo` need a reasonably recent image.** Their
> FPCs carry no disk — on every boot they PXE the linecard image out of the RE's own
> Junos package, so the linecard silently runs whatever release you gave the RE.
> Below 23.4 the card boots all the way to Online and then drops to
> `Offline ---Chassis connection dropped---` and reboots every few minutes, while the
> FPC VM still shows as Running. Validation now rejects those images up front. Leaving
> the **Image** box empty picks the pod's blessed default, which is always safe.

**mgmt IP.** Each device shows its management IP under the Image box, and on the
canvas, as soon as the lab is up. It comes from `vmm ping`, refreshed in the
background, so it never freezes the page.

**Capture a link, live.** Right-click any link on the canvas (or hit its `● capture`
button) and pick **Start capture**. The link starts pulsing red, the **Capture**
tab says what is being recorded and counts the packets as they cross the wire,
and **Stop & download .pcap** hands you a file that opens straight in Wireshark.
Nothing is spliced into the link and nothing is redeployed — the frames are
copied out of the running VM itself, so you can start and stop capturing on a lab
that is already up.

**Tidy up the picture.** Drag devices to move them. Every link has a small handle
at its midpoint — drag it to bend the link around a device, double-click it to
snap the link straight again.

**Adjust interface names and link ends.** Each interface name on a link can be
dragged along that link to any point you like, and each link end has a **dot**
sitting on the device — drag it to change where the link meets the box, so
parallel links can enter a device at different points instead of all converging
on its centre. Double-click either to put it back. Select a link and the
**Interface labels** section of the panel sets the font **size** (8–26px) and
**weight** (300–900, i.e. how thick it looks); **Apply to all links** copies that
choice everywhere, and **Reset** puts one link back to defaults. Big bold names
stay readable because they are drawn with a dark outline.

> Parallel links start with their end dots stacked on top of each other, since
> they all attach at the device centre. Click a link first — that lifts its two
> dots above the rest so you can grab the one you want.

**Annotate it.** Click a link to select it and give it a **label** (`10G core
uplink`, `VLAN 200`…). The **Drawing** buttons in the sidebar add a **box**, a
**circle** or a **text label** to the canvas — drag to move, drag the bottom-right
square to resize, and set the text, border colour, fill and font size in the
panel on the right. Use them to group a pod, ring a core, or caption a diagram.
Select anything and press `Delete` to remove it, or use the **Delete** button in
the right-hand panel.

> A text label is clicked anywhere inside the box around its words, not just on
> the letters themselves. Clear its text and it stays put as a dashed outline so
> you can still move or delete it rather than losing track of an invisible label.

Label styling, label positions, link-end positions, bends and drawings are all
**decoration only**. They are saved with your canvas but they never appear in the
topology file, so a lab built with them is byte-for-byte identical to one built
without.

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
back.

**Hide the panels** to give the canvas the whole window, which is what you want
once a lab is wired and you are just arranging the picture. The two buttons in
the header toggle them:

| | |
| --- | --- |
| `◧` or `[` | show/hide the device palette on the left |
| `◨` or `]` | show/hide the Inspect/Checks/YAML/Deploy panel on the right |

Hiding the right panel keeps the width you dragged it to, so bringing it back
does not reset it, and starting a deploy re-opens it by itself — otherwise the
log would be running where you cannot see it.

Zoom, pan, panel width and which panels are hidden are remembered per browser,
and if a saved view would leave you staring at empty canvas the builder snaps
back to your topology rather than looking like it lost your work.

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
| 4 | log in over the serial console and apply a baseline config |

### Capturing traffic on a link

You do not need a sniffer VM, and you do not need to redeploy. Frames are copied
straight out of the running VM's virtual NIC, so capture starts and stops on a
lab that is already up.

**From the builder (easiest).** Right-click the link → **Start capture**. The
link pulses red and the **Capture** tab tells you what is being recorded:

```
● Traffic is captured on link et-0/0/0:0 ↔ et-0/0/0:0 between nodes MX-1, MX-2
  1,204 packets · 37s · the .pcap keeps everything, including anything counted after this
```

**Stop & download .pcap** gives you a file Wireshark opens directly. The panel
deliberately does not list the packets themselves — see below.

**From the command line.**

```bash
python3 vmm.py --capture                        # list the links you can capture
python3 vmm.py --capture R1 --to R2 --seconds 30
python3 vmm.py --capture R1 --to R2 --interface ge-0/0/2   # a specific wire
python3 vmm.py --capture-stop                   # detach anything left running
```

`--to` is optional when the device has only one link. `--interface` is only
needed when the two devices are cabled together more than once — see below. The `.pcap` is written to
the lab's log directory and the path is printed at the end:

```
🎥 Capturing vrouter1 netdev1 <-> vrouter2 for 30s...
✅ 134 packets -> /vmm/logs/uuids/<uuid>/vrouter1_netdev1_20260825-123643.pcap
     01:41:37.239864 IP 10.1.1.1 > 10.1.1.2: ICMP echo request, id 35996, seq 0, length 64
     ...
```

Both directions of the link are recorded, and protocols are dissected by name
(OSPF, LDP, BGP, LLDP) rather than shown as hex.

#### Devices wired together more than once

Two devices are often cabled together several times, and "the link between A and
B" then means nothing on its own — recording the first one found is a coin flip,
and a capture on the wrong wire looks exactly like a link with no traffic on it.

The interface you clicked is therefore resolved to a specific NIC, with no login
to the device:

```
config_print   ge-0/0/2  ->  bridge 111__3aaeppss
vmm vde        bridge    ->  /vmm/data/vde_switches/9723
QEMU cmdline   switch    ->  netdev3
```

Right-clicking a link in the builder always carries its interface, so it is
always exact. From the command line, add `--interface`; without it, a pair with
several links reports the ambiguity and lists the alternatives rather than
guessing quietly.

This goes through the switch id rather than assuming netdevs are numbered in
interface order — that assumption is false on any multi-FPC chassis, where the
netdevs are non-contiguous and include internal RE-to-FPC links.

#### Several links at once

Captures are independent, so you can run as many as you need side by side —
including two on the *same* device. Right-click a second link and start it; the
Capture tab grows a chip per capture showing its live packet count, and you
click between them to see each one. Every recording link pulses on the canvas,
each has its own `.pcap`, and **Stop all** ends them together. **Remove** drops a
finished capture from the list once you have its file.

```
● MX-BXL2-2↔MX-LVL-3-1 74   ● MX-BXL2-2↔MX-NAM-1 110   ● MX-LVL-3-2↔MX-NAM-1 147
```

Starting several at once means several threads asking the `vmm` wrapper what is
in the lab, and it does not survive that — measured, three of six concurrent
`vmm ls` calls came back empty, which reads as "the lab is down". The lab
inventory is therefore answered once and shared for a few seconds, and the
shell-outs behind it retry rather than believing a blank first answer.

#### Why not tap the bridge?

The obvious approach — attach a listener to the VDE switch carrying the link —
does not work. `vde_switch` does MAC learning, so a port that joins late only
ever receives broadcast traffic. Measured on a busy link: **0 frames** that way
versus **2,070 frames** via the VM's own NIC over the same 15 seconds.

#### The panel counts packets; the file has them

QEMU buffers a capture and only flushes it when the capture is closed, so while
a recording runs its file says nothing at all — a long capture would sit at
`0.0 KB` no matter how much traffic crossed the link. To give you live feedback
anyway, a second short-lived capture is rotated every few seconds and the frames
it catches are counted, while the real one records without interruption.

That count is what the panel shows. The packets themselves are deliberately not
listed: the rotating preview can miss a frame that arrives mid-rotation, so a
list built from it is never quite the truth, and putting it on screen invited
people to read the approximate thing instead of the exact one. The **downloaded
`.pcap` is complete** — that is the artefact to open in Wireshark, and it is
what the panel points you at.

#### It stops on its own

A capture writes as root onto a filesystem the whole pod shares, so it is never
left running by accident. It stops when you stop it, when the browser tab goes
away, after 20 minutes, or at 250 MB — whichever comes first, and the panel says
which. Closing the builder closes any capture it still had open, whether you
pressed Ctrl+C or the builder was stopped from elsewhere (`--stop-port`, or the
port being reclaimed by a new builder). That matters because the recording lives
inside the VM's QEMU process, not in the builder: a builder killed outright would
leave it writing with nothing left anywhere to stop it.

The one deliberate exception is `nohup`. If you started the builder with it —
the documented way to survive a dropped ssh session — then SIGHUP is ignored, as
you asked: the builder keeps running and keeps looking after its captures.

#### Upgrading from the old sniffer VM

Older topologies spliced a `sniffer1` VM into links marked `sniffer: true`.
That is gone. Those files still load — the `sniffer:` and `sniffer_disk` keys
are ignored, with one warning — and the link is now wired straight through
instead of being cut in half by a VM. Delete the keys when convenient; capture
the link with the commands above instead.

Most devices are a multi-VM chassis, not one VM — an EVO PTX is RE + FPC + CSPP.
Check the pod has room before a big lab:

```bash
vmm capacity -g vmm-default     # '-g' is required
```

Read **`Current largest VM available`**, not just free capacity: a 32 GB FPC
needs one blade that can hold it.

### Tearing down a lab

You get **one lab per pod account**, so an old lab keeps holding capacity until
you remove it. `vmm unbind` is the teardown:

```bash
vmm ls           # what is currently bound
vmm unbind       # stop and release every VM in your lab
vmm ls           # confirm it is gone
```

`Warning: <vm> unbound - ignoring` just means that VM was already down — it is
not an error, and `unbind` is safe to run twice.

You do not normally need to do this by hand: a deploy runs `vmm unbind` for you
before applying the new config (the `Perfoming VMM unbind` line in phase 2).
Run it yourself when you want to free the pod without deploying anything, or
when a previous lab is wedged and a fresh deploy will not fit.

#### Why the deploy waits after unbinding

`vmm unbind` is documented as *"terminate/cleanup"*, and it returns as soon as
the terminate half is done — the cleanup runs on afterwards. That is the same
asynchrony that already affects `vmm start`.

Applying the new config inside that window produces a confusing failure: the VM
names that carry over from the previous lab bind fine, and the ones that are
genuinely new are silently dropped. A lab that only added a device would come up
missing exactly that device, while `vmm unbind` had reported success and the
pod had plenty of free capacity. Running `vmm unbind` by hand appeared to fix
it, only because typing the next command gave the cleanup the seconds it needed.

So phase 2 now polls `vmm ls` until nothing is bound before it applies the
config, retries the unbind once if anything is still held, and says so:

```
Perfoming VMM unbind
   waiting for VMM to release 9 VM(s) from the previous lab (up to 180s)...
   pod released after 10s.
Applying vmm config!
```

When the pod is already clear this costs nothing — a single `vmm ls` — and
prints nothing.

---

## Useful flags

| flag | does |
|---|---|
| `--build` | browser topology builder (pick devices, wire, deploy) |
| `--capture A --to B` | record a live link to a `.pcap` (`--capture` alone lists them) |
| `--capture-stop` | detach any capture left running |
| `--interfaces` | per-device list of valid ports |
| `--config_file_only` | generate the config, don't touch the pod |
| `--lab_detail` | summary table of the lab |
| `--serve` | interactive topology diagram in your browser |
| `--debug` | stream the serial console dialogue during phase 4 |
| `--skip_boot_wait` | skip the phase 3 ping wait |
| `--print_devices` | emit a `devices.json` for junos-mcp-server |
| `--servers [N...]` | is a web server running? read-only; says whether it really answers HTTP |
| `--stop-port N` | free port N without starting anything (`--build` reclaims it by itself) |

`--build` (8081) and `--serve` (8080) use different default ports, so both can
run at once. `--port` changes whichever one you asked for:

```bash
python3 vmm.py --build --port 8082       # builder on 8082
python3 vmm.py --serve --port 8085       # diagram on 8085
python3 vmm.py --build --build-port 9000 --serve --port 8085   # both, explicitly
```

### When the page doesn't come up

Just start it again on the same port:

```bash
python3 vmm.py --build --port 5057
```

That is all it takes. If an earlier builder of yours was still holding the
port, it is stopped automatically and the port reclaimed, so you land straight
back on your lab instead of an `Address already in use`.

To see what is actually running before you change anything:

```bash
python3 vmm.py --servers            # find this script's servers, wherever they are
python3 vmm.py --servers 5057 8081  # or check specific ports
```

```
Web servers:

  ✅ port 8081 - your own server
       pid 1802598: python3 vmm.py --build --build-port 8081
       answering (HTTP 200)
       open: http://q-pod08-vmm:8081/

  ⚠️  port 5057 - your own server
       pid 1802444: python3 vmm.py --build --port 5057
       connected, but no HTTP reply - the server is wedged
       free it with: python3 vmm.py --stop-port 5057
```

It reports **listening** and **working** separately, because they are not the
same thing: a wedged server still owns its port, so a restart refuses to bind
while the browser shows nothing. That combination is the confusing one, and it
is what the second line above is telling you. `--servers` never stops anything —
use `--stop-port` for that.

Two different faults produce that same blank page, and this covers both:

- **The server exited.** `--build` runs in the **foreground**, so it dies with
  the shell that started it: a dropped ssh session, a closed laptop, or a closed
  terminal takes the page down without warning. To make it survive that:

  ```bash
  nohup python3 vmm.py --build --port 5057 > builder.log 2>&1 &
  ```

- **An orphan still holds the port.** Reclaimed for you on the next start. To
  free a port without starting anything, use `--stop-port 5057`.

Only *this script's* servers that *you* own are ever stopped. Anything else is
named and left running:

- another program of yours → it prints the pid and command so you can decide;
- **another user's server** → pod hosts are shared, and the kernel hides other
  users' pids, so nothing shows up to kill. That is not a stuck port — pick a
  different one with `--port <N>`.

---

## Notes

- The root password used for serial login defaults to the standard lab
  password. Override it with `export VMM_DEVICE_PASSWORD=...`.
- `lab_topology.conf` is generated on every run and is git-ignored.
- Retired types `vptx` and `vredbull` are rejected at validation, with a message
  naming the replacement.
