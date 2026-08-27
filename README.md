# vmm-lab-setup

Build a Juniper virtual lab in your browser: drop devices on a canvas, click
ports to cable them, press **Deploy**. The tool writes the VMM config, brings
the lab up on the pod, waits for every device to boot, and logs into each one
over the serial console to apply a baseline config.

No YAML to hand-write.

---

## 1. Get set up

Run this on a `-vmm` pod host, e.g. `q-pod08-vmm` — that is where the `vmm`
command lives.

```bash
pip3 install pyyaml jinja2 junos-eznc pexpect
git clone <this repo> && cd vmm-lab-creator
```

> Run the commands **from inside the repo directory**. `lab_template.j2` is
> loaded from the current working directory, so `python3 /some/path/vmm.py`
> from elsewhere will fail.

## 2. Start the builder

```bash
python3 vmm.py --build --port 8081
```

It prints a URL — open it in your browser:

```
================================================================
  🧩  VMM topology builder — open this in a browser:
          http://10.51.246.54:8081/
================================================================
```

Any free port works (`--port 5057`, `--port 9000`…). If an old builder of your
own is still holding that port, it is stopped and the port reused automatically.

> The builder runs in the **foreground**, so it dies with the shell that started
> it — a dropped ssh session takes the page down. To leave it running:
>
> ```bash
> nohup python3 vmm.py --build --port 8081 > builder.log 2>&1 &
> ```

## 3. Draw the lab

| do this | to |
|---|---|
| click a device in the left palette | add it to the canvas |
| type a name, press Enter | rename it (the box is already focused) |
| click a port, then a port on another device | cable them together |
| drag a device | move it |
| select a link → **Delete** | remove it |

A few things worth knowing:

- **The port list is real.** Each device only offers the ports it actually has,
  so you cannot wire a port that would come up dead. vScapa offers odd ports
  only, vBrackla's live on FPC1, vBalerion starts at 9.
- **Names** must start with a letter and contain only letters, digits, `-` and
  `_` — `R1`, `PE-2`, `core_rtr`. The name is pushed to the device as
  `set system host-name`.
- **Image** — leave the box empty and the device uses the pod's default image
  for its type. That is the safe choice. Paste a path to override it for that
  one device.
- **Checks** tab goes red the moment something is wrong, and it is the same
  validator the command line uses — the GUI can never let through something the
  deploy would reject.
- Your canvas is **autosaved**, so a refresh or a closed tab brings it back.

## 4. Deploy

Press **Deploy**. The log streams into the **Deploy** tab.

| phase | what happens |
|---|---|
| 1 | validate the topology |
| 2 | generate the config, check the pod has room, unbind the old lab, start |
| 3 | wait for every device to answer `vmm ping` |
| 4 | log in over the serial console and apply the baseline config |

Give it time. Phase 3 alone allows up to 15 minutes for devices to boot
(`--boot_wait`), and phase 4 then walks each console in parallel — most of it is
Junos starting up, not the tool waiting around.

Each device shows its **management IP** on the canvas as soon as it has one, so
you can SSH straight in as `root` with the standard lab password (override it
with `export VMM_DEVICE_PASSWORD=...` before deploying).

Your topology is saved as `topo.yml` next to the script, so the same lab can be
redeployed later with `python3 vmm.py -t topo.yml`.

> **One lab per pod account.** Deploying replaces whatever you had running —
> the old lab is unbound for you first.

## 5. Capture traffic on a link

Right-click any link → **Start capture**. The link pulses red and the
**Capture** tab shows what is being recorded:

```
● Traffic is captured on link ge-0/0/0 ↔ ge-0/0/1 between nodes R1, R2
  1,204 packets · 37s
```

**Stop & download .pcap** gives you a file that opens straight in Wireshark.

Nothing is added to the lab and nothing is redeployed — the frames are copied
out of the running VM itself, so you can start and stop capturing on a lab that
is already up. Both directions are recorded, and you can capture several links
at once.

## 6. Tear it down

```bash
vmm ls        # what is running
vmm unbind    # stop everything and free the pod
```

---

## When something goes wrong

**The page will not load.** Just start it again on the same port — an orphaned
builder is cleared automatically:

```bash
python3 vmm.py --build --port 8081
```

To look before changing anything:

```bash
python3 vmm.py --servers        # what is serving, and does it actually answer?
python3 vmm.py --stop-port 8081 # free a port without starting anything
```

`--servers` reports *listening* and *working* separately, because a wedged
server still owns its port while the browser shows nothing.

**Deploy is greyed out.** There is no `vmm` command on this host, so there is
nothing to deploy to. You can still design the lab and save it — do that, then
deploy from a pod host.

**The lab will not fit.** Check the pod first. Read *Current largest VM
available*, not just free capacity — a 32 GB FPC needs one blade that can hold
it:

```bash
vmm capacity -g vmm-default
```

**A device seems stuck.** Watch its serial console during phase 4:

```bash
python3 vmm.py -t topo.yml --debug
```

---

## Supported devices

| family | types |
|---|---|
| Linux | `server` |
| vJunos | `vswitch`, `vrouter`, `vqfx` |
| classic | `vmx`, `vferrari` |
| MX | `vbugatti`, `vhamilton`, `vmaserati`, `valfaromeo` |
| EVO PTX | `vscapa`, `vardbeg`, `vbrackla`, `vbalerion`, `vbowmore` |

They all mix freely in one lab.

## What is in here

| file | |
|---|---|
| `vmm.py` | the tool |
| `vmm_builder.py` | the browser builder (`--build`) |
| `lab_template.j2` | config template — **required**, loaded from the working directory |
| `vmmcap` | optional standalone packet-capture front end |
| `topo*.yml` | example topologies |
| `REFERENCE.md` | full documentation: YAML syntax, per-platform port rules, every flag |

`lab_topology.conf`, `topology.html` and `.*.builder-draft.json` are generated
and git-ignored.

---

Prefer the command line, or need the YAML format, the exact port ranges, or the
reasoning behind a validation error? See **[REFERENCE.md](REFERENCE.md)**.
