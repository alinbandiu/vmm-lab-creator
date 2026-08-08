# vmm-lab-setup

Build a Juniper VMM virtual lab from a small YAML file.

You describe the devices and the cables. The script generates the VMM config,
deploys it to the pod, waits for boot, and baseline-configures every device over
the serial console.

---

## Quick start

```bash
pip3 install pyyaml jinja2 junos-eznc pexpect

python3 vmm.py -t topo.yml --interfaces          # what ports can I use?
python3 vmm.py -t topo.yml --config_file_only    # just generate the config
python3 vmm.py -t topo.yml                       # deploy the lab
```

Run it from a `-vmm` pod host (e.g. `q-pod08-vmm`), where the `vmm` command lives.
`--config_file_only` and `--interfaces` work anywhere.

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
| `--interfaces` | per-device list of valid ports |
| `--config_file_only` | generate the config, don't touch the pod |
| `--lab_detail` | summary table of the lab |
| `--serve` | interactive topology diagram in your browser |
| `--debug` | stream the serial console dialogue during phase 4 |
| `--skip_boot_wait` | skip the phase 3 ping wait |
| `--print_devices` | emit a `devices.json` for junos-mcp-server |

---

## Notes

- The root password used for serial login defaults to the standard lab
  password. Override it with `export VMM_DEVICE_PASSWORD=...`.
- `lab_topology.conf` is generated on every run and is git-ignored.
- Retired types `vptx` and `vredbull` are rejected at validation, with a message
  naming the replacement.
