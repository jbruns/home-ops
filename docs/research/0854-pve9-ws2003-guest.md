# PVE 9 settings for the Windows Server 2003 guest

Findings for #854. Researched 2026-11-03 against primary sources: Proxmox VE docs and `qemu-server` /
`pve-qemu` source, QEMU source and changelogs, Linux KVM docs, virtio-win archives, and archived Microsoft
KB articles.

## Question

VM 105 runs Windows Server 2003 (32-bit). It is an AD DC, DNS, WINS and file server on the retro network.
The Proxmox Host went from PVE 8.2.7 / QEMU 9.0.2 / kernel 6.8 to PVE 9.2 / QEMU 11.0.3 / kernel 7.0.
Since then the guest is CPU-bound under I/O. A 3 GB SMB copy stalls partway, then recovers by itself:

- at about 33% with virtio-net;
- at about 90% with e1000.

During the stall the guest still answers ping. Which VM settings are best for this guest on PVE 9.2, and
what should the Operator test first?

## Verdict

**Treat the stall as a disk write-back problem first and a NIC problem second.** Test the cheap guest-side
cache throttle, then take disk I/O off QEMU's main loop. Leave the machine type, CPU model and ostype alone.

- **The stall point matches a documented Windows Server 2003 cache-manager problem.** By default the
  write-throttle threshold is half of physical RAM ([KB920739]). With 2 GiB that is about 1 GiB, and 1 GiB
  is 33% of a 3 GB copy. Microsoft's fix is the `SystemCacheDirtyPageThreshold` registry value, which SP2
  includes but does not enable (§6). The Operator's earlier symptom fits too: atapi drops an IDE channel to
  PIO after six cumulative time-outs or CRC errors ([KB817472]). That means disk requests on 105 have been
  timing out (§5).
- **Every emulated device 105 uses runs in QEMU's main loop.** IDE cannot use an iothread (only VirtIO SCSI
  single and VirtIO Block can, per [pve-qm]). e1000 and UHCI are also emulated in the main loop. QEMU 10.2
  rebased that main loop onto io_uring ([qemu-cl-10.2]). Proxmox has already patched its IO-wait side
  effects twice ([pve-qemu-log], [pve-fdmon-patch]). This is the largest change in 105's I/O path between
  9.0 and 11.0. The changelogs show no PIIX IDE, e1000 or UHCI device change that would explain it (§1).
- **No-ops.** `ostype: wxp` and `w2k3` produce identical QEMU command lines. Both are Windows version 5,
  with no Hyper-V enlightenments and HPET left on ([qs-helpers], [qs-timer], [qs-cpuconfig]). Keep the
  `pc-i440fx-9.0` pin, which Proxmox applies to Windows guests on purpose ([qs-machine]). Neither bumping it
  nor changing the CPU model has any primary-source evidence of helping.
- **USB tablet.** Its idle cost comes from the UHCI 1 kHz frame timer, plus KVM halt-polling, which turns
  short idle gaps into host CPU time ([qemu-uhci], [kvm-halt-poll]). Leave it off until the stall is fixed.
- **No rebuild yet.** A rebuild cannot fix a host-side regression. If one is wanted later, use a second DC
  and FSMO transfer. Do not restore system state onto new hardware, which Microsoft does not support (§8).

### Ranked tests: one change at a time against the 3 GB copy

Before each test, take a snapshot or note the current `qm config 105` line, so the change can be reverted
exactly. Run the copy in both directions. Note the stall point, the host vCPU thread CPU and the guest Task
Manager CPU.

`qm set --ideN/--virtioN` replaces the whole drive string. Copy the current value from `qm config 105`, then
add or change only the option under test. The `...` below stands for the existing options.

1. **Baseline evidence, no change.** During a stall, read these in the guest:
   - System log events from Srv (2019/2020 pool exhaustion, 2021/2022 work items) and atapi warnings;
   - Performance Monitor: `Cache\Dirty Pages`, `PhysicalDisk(*)\Avg. Disk sec/Write`, `Server Work
     Queues(*)\Queue Length`.

   [KB317249] says the most common cause of 2021 is a disk that cannot keep up. [KB312362] covers 2020.
2. **Guest: cap the dirty-page threshold.**
   - Value: `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management`,
     `SystemCacheDirtyPageThreshold` (REG_DWORD, in MB), `0x40` (64 MB).
   - Allowed range on 2 GiB: 32 to 1024 MB ([KB920739]). Reboot after setting it.
   - Expected result: the 33% cliff turns into a steady, slower copy.
3. **Host: switch the IDE disks from io_uring to thread-pool AIO.**
   - `qm set 105 --ide0 local-lvm:vm-105-disk-0,...,aio=threads`, then the same for `ide3`.
   - Run a cold stop/start (not a guest reboot) so QEMU restarts.
   - Why: LVM-thin defaults to io_uring, and plain `lvm` storage to native or threads ([qs-drive]). The
     main-loop and AIO paths changed most in 10.x.
4. **Host plus guest: move the data disk (ide3) to VirtIO Block with an iothread.**
   - Drivers: the last 2003 viostor is 52.81.104.17600, in [virtio-win 0.1.185] and 0.1.187 (§5).
   - Install the driver first. Add a 1 GB scratch disk, boot, install viostor from `/viostor/2k3/x86`:

     ```sh
     qm set 105 --virtio1 <storage>:1,iothread=1
     ```

   - Then shut down, detach `ide3` and re-attach the same volume:

     ```sh
     qm set 105 --virtio2 <storage>:<volume>,iothread=1,aio=threads
     ```

   - Keep `ide0`, the boot disk, on IDE until the data disk has proven stable.
5. **NIC: virtio-net with the newest 2003 NetKVM, LSO off.**
   - `qm set 105 --net0 virtio=<MAC>,bridge=<bridge>`.
   - Install NetKVM 52.81.104.17600 from `/NetKVM/2k3/x86` on 0.1.185. The guest has 52.81.104.17400.
   - Then set *Offload.Tx.LSO* to Disabled in the adapter's Advanced tab. It defaults to Maximal
     ([netkvm-inf]).
   - If the Operator stays on e1000, also try `MonitorModeEnabled=1` from the Proxmox 2003 wiki ([pve-w2k3]).
6. **Host isolation: boot the previous kernel series.**
   - `proxmox-boot-tool kernel pin <6.17 version> --next-boot` affects every VM, so it is the Operator's call.
   - It tells a kernel 7.0 / io_uring regression apart from a QEMU one. A Proxmox staff member points to the
     10.2 io_uring switch for changed IO accounting ([forum-184026]).
7. **CPU model `kvm64`.** `qm set 105 --cpu kvm64` gives a smaller flag set and adds `+sep`, which Proxmox
   adds for kvm64 "to improve Windows XP+" ([qs-cpuconfig]). Expect no change. Run it only to close the
   question.
8. **Tablet back on, last.** `qm set 105 --tablet 1`, measured idle, only after tests 1–5 (§7).

Not recommended:

- changing `ostype` (a no-op, §2);
- bumping the machine version (no benefit found; Proxmox pins Windows on purpose, §1);
- SATA, because 2003 has no inbox AHCI driver (§5);
- disabling HPET, because 2003 does not use it (§3);
- adding vCPUs, because that needs a HAL change on a DC (§2).

## 1. Machine type and QEMU 9.1–11.0 changes

- **Proxmox pins the machine version for Windows guests.** `Machine.pm` gives Windows VMs a versioned
  machine type, because QEMU 5.2 changed the ACPI layout and that confused Windows ([qs-machine]). For
  machine ≥ 10.0, PVE 9 switches to the "modern blockdev" command line ([pve-roadmap], PVE 9.0). 105 is
  pinned at 9.0, so it still gets the legacy `-drive` path. A bump would change two things at once.
- **QEMU 9.1**
  - Several HPET fixes, including "avoid timer storms on periodic timers" ([qemu-cl-9.1]).
  - Irrelevant to 2003, which does not use HPET (§3).
- **QEMU 10.0**
  - An ACPI workaround for a Windows "PCI Label Id" bug that hangs localized guests.
  - The Linux AIO and io_uring backends use `RWF_DSYNC` for FUA writes ([qemu-cl-10.0]).
- **QEMU 10.1:** removes machine types older than 4.1 ([qemu-cl-10.1]).
- **QEMU 10.2**
  - "On host systems that support io_uring, QEMU's main loop is now based on io_uring" ([qemu-cl-10.2]).
    The upstream commit makes fdmon-io_uring stay active even under the glib main loop, where it used to
    fall back to ppoll ([qemu-ded29e6]).
  - The HPET no longer takes the BQL ([qemu-cl-10.2]).
- **QEMU 11.0**
  - Removes pc-i440fx 2.6–4.0 ([qemu-cl-11.0]).
  - Includes a fix for completions that waited "for the next ppoll() cycle" in the io_uring fdmon
    ([qemu-961fcc0]).
- **Device files.** From 2024-04 to 2026-05, `hw/ide/piix.c`, `hw/ide/pci.c`, `hw/net/e1000.c`,
  `hw/usb/hcd-uhci.c` and `hw/rtc/mc146818rtc.c` had only trivial or assert-hardening commits
  ([qemu-gh-history]). Separately, `hw/ide/core.c` moved DMA onto the current AioContext (a89c3c9b2c).
- **Proxmox's own io_uring patches.**
  - Proxmox carries a patch so the idle io_uring event loop is not counted as IO wait. It says this
    otherwise showed "nearly 100% IO pressure in PSI on idle VMs" ([pve-fdmon-patch]).
  - It first shipped in pve-qemu 10.2.1-2 ([pve-qemu-log]).
  - pve-qemu 11.1.3-1 is now in git and picks up upstream "USB emulation, IDE emulation" fixes
    ([pve-qemu-log]). If it reaches the Operator's repository, retest after it.
- **Verdict:** keep `pc-i440fx-9.0`. The suspect change (the io_uring main loop and kernel 7.0) applies
  whatever the machine version.

## 2. CPU model, enlightenments and ostype

- **`wxp` and `w2k3` are the same.** `windows_version()` returns 5 for `wxp`, `w2k3` and `w2k`
  ([qs-helpers]). Every later decision keys off that number.
- **No Hyper-V enlightenments.** `get_hyperv_enlightenments` returns early below version 6, so 2003 gets no
  `hv_*` flags ([qs-cpuconfig]). No `hv_*` flag should be added by hand: 2003 predates the Hyper-V
  enlightenment interface.
- **The `kvm_pv_*` flags are Linux-only.** Proxmox adds `+kvm_pv_unhalt` and `+kvm_pv_eoi` to every KVM x86
  guest, with the comment "to improve Linux guest" ([qs-cpuconfig]). Windows ignores them. They are harmless.
- **CPU model.**
  - `x86-64-v2-AES` is the UI default; the backend default is `kvm64` ([pve-qm]).
  - For kvm64/kvm32, Proxmox adds `+sep` ("improve Windows XP+") and, for kvm64, `+lahf_lm`
    ([qs-cpuconfig]).
  - None of the v2 extras (SSE4.x, POPCNT, AES) change how the 2003 kernel schedules or takes interrupts.
  - `host` exposes CPUID leaves that 2003 never saw. It gains nothing and may break live migration.
- **vCPU count.** Keep 1 vCPU. Going from 1 to 2 needs a uniprocessor → multiprocessor ACPI HAL switch.
  Microsoft treats HAL type as a hardware-identity property ([KB249694]).

## 3. Timers

- **What PVE generates for 105.** For Windows version 5, `Timer.pm`:
  - defaults `localtime` on;
  - enables `driftfix=slew` (tdf) when ACPI is on.

  Only version ≥ 6 gets `kvm-pit.lost_tick_policy=discard` and `hpet=off` ([qs-timer]). So 105 runs with
  HPET on and `rtc base=localtime,driftfix=slew`. That logic moved, unchanged, into `Cfg2Cmd/Timer.pm` in 2025.
- **HPET does not matter to 2003.** Microsoft: "HPET support has been in Windows since Windows Vista" ([ms-qpc]).
  2003 uses the ACPI PM timer or the TSC for QPC. Turning HPET off (`qm set 105 --args '-machine hpet=off'`)
  is unlikely to change anything. Do not spend a test slot on it.
- **driftfix and localtime.** Both are correct for an NT 5.x guest. `localtime` keeps the RTC in the
  timezone Windows expects.
- **Halt-polling.**
  - The KVM default `halt_poll_ns` on x86 is 200 µs ([kvm-host-h]).
  - The kernel docs warn that it converts idle time into host kernel time. When wakeups are frequent but
    short, "the host will always poll for the entire block time" ([kvm-halt-poll]).
  - This explains why a guest with a 1 kHz device timer (UHCI) looks busy on the host. It is not a new
    regression: the default has not changed.

## 4. Network

- **Proxmox's guidance for old guests.**
  - E1000 is the default model; "if your guest does not support this driver, it is usually best to use
    e1000" ([pve-qm]).
  - rtl8139 is "only for OS released before 2002" ([pve-qm]).
  - The community 2003 page recommends e1000 with Intel's newer driver and the `MonitorModeEnabled=1`
    registry fix ([pve-w2k3]). That page is a community wiki, not staff-authored.
- **virtio-net is faster per packet.** The Operator measured virtio-net at about 7500 pps and e1000 at about
  3300 pps, at about 2× host CPU per packet. With `vhost=on`, the data path leaves QEMU's main loop.
  Prefer virtio-net if a driver build behaves.
- **NetKVM builds for 2003.** Listed from the ISOs (`/NetKVM/2k3/x86`, `TargetOS NTx86.5.2`):

  | ISO | 2k3 NetKVM build |
  |---|---|
  | 0.1.173 | 52.80.104.17300 (2019-08-12) |
  | 0.1.185, 0.1.187 | **52.81.104.17600 (2020-01-08), the newest 2k3 build** |
  | 0.1.189–0.1.215 | 52.81.104.17400 (2019-12-12), the guest's current driver |
  | 0.1.229, 0.1.271 | fall back to 2012-era 51.64.x builds |

  Sources: [virtio-win archive], [virtio-win 0.1.185]. The newer ISOs still ship a 2k3 folder, but with older
  builds. Proxmox says the latest ISOs lack 2003 drivers ([pve-w2k3]).
- **Offloads.**
  - The 2k3 NetKVM inf defaults are *Offload.Tx.LSO* Maximal and *Offload.Tx.Checksum* / *Offload.Rx.Checksum*
    Disabled ([netkvm-inf]).
  - LSO hands 64 KB sends to the host and is the only offload on by default, so it is the one to turn off
    first.
  - On the host side nothing needs to change. With vhost the tap offloads are negotiated from the guest's
    features.
- **e1000 stalls.** No primary source documents a 2003-specific e1000 stall. Both NICs stalled, so the NIC is
  unlikely to be the root cause.

## 5. Disk

- **IDE DMA mode.**
  - QEMU's i440fx uses the PIIX3 south bridge. Its IDE function is the 82371SB ([qemu-pc-piix], [qemu-piix]).
    The real 82371SB is a Multi-Word DMA controller; UDMA arrived with the 82371AB (PIIX4).
  - QEMU's IDENTIFY data for a disk still advertises UDMA5 and an 80-wire cable (words 88 and 93 in
    `ide_identify`, [qemu-ide-core]).
  - So Windows settling at *Multi-Word DMA 2* on a PIIX3 is expected, not a fault. In QEMU the transfer mode
    is only a register value: DMA mode does not change emulated throughput. Only PIO vs DMA matters, because
    PIO costs a VM exit per word.
- **Why it fell to PIO.** atapi steps the transfer mode down after six cumulative time-outs or CRC errors
  (10-second time-outs on XP/2003). The KB817472 hotfix plus `ResetErrorCountersOnSuccess=1` makes the
  count consecutive instead ([KB817472]). A demotion on 105 means disk requests have exceeded 10 s. That
  fits the write-back theory in §6.
- **No iothread on IDE.** The iothread option exists only for VirtIO SCSI single and VirtIO Block. Without it,
  I/O runs "in the main event loop or vCPU threads" and can block them ([pve-qm]). Proxmox describes IDE as "a
  great choice if you want to run an OS released before 2003" ([pve-qm]). That is about compatibility, not
  speed.
- **viostor for 2003.**
  - 0.1.185 ships `/viostor/2k3/x86/viostor.inf`, `DriverVer 52.81.104.17600`, `NTx86.5.2`
    ([virtio-win 0.1.185]).
  - The virtio-win changelog froze the WinXP viostor at 0.1.130 ([virtio-win-changelog]), but 2k3 kept
    getting builds until 0.1.187.
  - Use VirtIO Block, not VirtIO SCSI, so `iothread=1` works without the vioscsi driver.
- **SATA.** QEMU's AHCI is on q35 only, and 2003 has no inbox AHCI driver. Skip it.
- **Cache and aio.**
  - The disk default is `cache=none` ([pve-qm]).
  - AIO defaults to io_uring, except on plain `lvm`, CIFS and krbd without cache-direct, which get native
    (cache-direct) or threads ([qs-drive]).
  - 105's `ide0` is on LVM-thin, so it is io_uring. Check `ide3`'s storage type in `storage.cfg`.
  - `aio=threads` is the most conservative choice.

## 6. RAM and guest-side SMB tuning

- **SystemCacheDirtyPageThreshold ([KB920739]).**
  - Applies to 2003 SP1/SP2. Large copies (> ~500 MB) make the system unresponsive while the copy continues.
  - Cause: `CcDirtyPageThreshold` defaults to half of physical memory. When it is reached, Cache Manager
    flushes hundreds of MB at once to a slow disk.
  - SP2 includes the fix, but `SystemCacheDirtyPageThreshold` must be added by hand. It is in MB, with a
    minimum of RAM/64 and a maximum of RAM/2; the KB's example is `0x20`.
  - With 2 GiB, half is about 1 GiB, which is where the virtio-net copy stalled (33% of 3 GB). e1000 sends
    more slowly, so the lazy writer kept up longer, which fits the later (~90%) stall.
  - If the copy that stalls is guest → client, this KB fits less well. Check the direction first.
- **Server service (srv) tuning ([KB317249]).**
  - Event 2021 is usually the disk not keeping up, and "modifying the server service will not resolve the
    issue" if so.
  - If tuning is still needed, the KB's values are `MaxWorkItems` 8192 (only with > 1.5 GB RAM and no
    `/3GB`), `MaxFreeConnections`, and so on.
- **Paged pool ([KB312362]).** For event 2020: `PoolUsageMaximum=60` and `PagedPoolSize=0xFFFFFFFF`. Only if
  2020 is actually logged.
- **SizReqBuf ([KB320829]).** Defaults to 16644 on servers with > 512 MB. Raising it helps high-latency links,
  not a LAN. Leave it.
- **IRPStackSize ([KB285089]).** Default 15, range 11–50. It is only for "not enough server storage" errors
  caused by extra filter drivers. Not indicated here.
- **RAM size.** 2 GiB is adequate for a DC/file server of this size, and it keeps `/3GB` out of the picture.
  More RAM raises the default dirty threshold (half of RAM), so it would make the stall later, not go away.
  Set the threshold before resizing.

## 7. USB tablet

- **Proxmox defaults.**
  - The tablet is a `usb-tablet` on `uhci.0` for i440fx. Proxmox keeps UHCI for old VMs "because tablet
    driver was buggy in older qemu" ([qs-usb]).
  - qemu-xhci is used only for machine ≥ 7.1 with Linux or Windows > 7 ([qs-usb]).
  - The `tablet` option says to consider disabling it "to save some context switches" ([pve-qm]).
- **Cost mechanism.**
  - While the guest leaves the controller running, QEMU's UHCI runs a `FRAME_TIMER_FREQ 1000` timer. Each tick
    walks the guest frame list in the main loop ([qemu-uhci]).
  - The full-speed tablet asks to be polled every 10 ms (`bInterval 0x0a`) ([qemu-dev-hid]). Windows' UHCI
    driver therefore keeps the schedule active.
  - KVM halt-polling then turns the gaps between those wakeups into host CPU (§3).
  - With the tablet off, no device is attached and the controller can stop. That fits the drop from about
    90% to 7%.
- **Alternatives, in order.**
  1. Keep `tablet: 0` and use the PS/2 mouse in the console.
  2. Retest `tablet: 1` after the stall is fixed. Some of the 90% may be the same main-loop cost.
  3. Put the tablet on EHCI via `args:`. Untested: QEMU's EHCI timer backs off when the periodic schedule is
     idle ([qemu-ehci]), but a high-speed tablet asks to be polled every 1 ms (`bInterval 4`,
     [qemu-dev-hid]), so it may cost more.
  4. vmmouse with VMware SVGA. Not researched; it needs VMware Tools drivers in the guest.

## 8. Rebuild vs in-place

- **A rebuild does not address the host-side cause.** Retest on a fresh 2003 install only after tests 1–5.
- **Do not restore system state onto a new install or different virtual hardware.** Microsoft: "System
  State Recover is only supported on the same operating system instance," and restoring to a different
  hardware configuration is unsupported ([KB263532]). That article was written for Windows 2000; no 2003
  article reversing the policy was found. [KB249694] lists the HAL constraint.
- **The safe AD-preserving path:**
  1. Build a fresh 2003 VM on the chosen hardware (IDE boot, VirtIO Block data disk, NIC as tested).
  2. Join it to `<domain>` and `dcpromo` it to an additional DC with DNS (AD-integrated zones replicate) and
     WINS (set up push/pull replication).
  3. Let replication settle.
  4. Transfer all five FSMO roles with `ntdsutil` ([KB255504], [KB324801]) and make it a GC.
  5. Move the file shares and WDS/RIS content.
  6. `dcpromo` the old DC down.
  7. If the demotion fails, clean up its metadata ([KB216498]).

  This keeps the domain's identity, SIDs and trusts, with no unsupported restore.

## Open questions

- Which direction stalls, client → guest or guest → client? §6 fits writes into the guest best.
- Is `ide3` on `lvm` (native/threads) or `lvmthin` (io_uring)? That decides whether test 3 changes it.
- Was `tablet: 1` set before the upgrade, at the old 7% idle? If so, the tablet's cost is new, which points
  at the main loop rather than UHCI.
- Do pve-qemu 11.1.3's "USB emulation, IDE emulation" stable fixes include anything for this path? Retest
  after upgrading.
- Not verified from primary sources:
  - the last Intel PRO/1000 driver version for 2003;
  - whether 2003 SP2's inbox `usbehci.sys` handles QEMU's high-speed tablet;
  - the 82371SB MWDMA-only limit (from Intel datasheet knowledge; the datasheet itself was not fetched).

## Sources

- [pve-qm]: <https://pve.proxmox.com/pve-docs/chapter-qm.html>: IDE "before 2003"; iothread only for VirtIO
  SCSI single/Block, otherwise main loop; e1000 default; rtl8139 pre-2002; cache none default; `tablet`
  context-switch note; CPU defaults.
- [pve-roadmap]: <https://pve.proxmox.com/wiki/Roadmap>: 9.0 = QEMU 10.0.2, kernel 6.14, blockdev for machine
  ≥ 10.0; 9.1 = QEMU 10.1.2, kernel 6.17; 9.2 = QEMU 11.0, kernel 7.0.
- [pve-w2k3]: <https://pve.proxmox.com/wiki/Windows_2003_guest_best_practices>: community wiki. e1000 with
  Intel driver plus `MonitorModeEnabled`; latest VirtIO ISOs lack 2003 drivers.
- [qs-helpers]:
  <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Helpers.pm;hb=HEAD>:
  `windows_version` maps `wxp`/`w2k3`/`w2k` to 5.
- [qs-timer]:
  <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Cfg2Cmd/Timer.pm;hb=HEAD>:
  localtime/tdf for Windows ≥ 5; `hpet=off` and PIT discard for ≥ 6.
- [qs-cpuconfig]:
  <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/CPUConfig.pm;hb=HEAD>:
  no `hv_*` below 6; `kvm_pv_*` "to improve Linux guest"; `+sep` for kvm64/kvm32.
- [qs-machine]:
  <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Machine.pm;hb=HEAD>:
  Windows machine-version pinning (5.2 ACPI change).
- [qs-usb]: <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/USB.pm;hb=HEAD>:
  UHCI for i440fx, tablet bus, xHCI conditions.
- [qs-drive]:
  <https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Drive.pm;hb=HEAD>:
  aio defaults per storage type.
- [pve-qemu-log]: <https://git.proxmox.com/?p=pve-qemu.git;a=log;hb=HEAD>: 10.2.1-2 IO-wait fix; 11.0.3-x
  releases; 11.1.3-1 stable fixes.
- [pve-fdmon-patch]:
  <https://git.proxmox.com/?p=pve-qemu.git;a=blob_plain;f=debian/patches/extra/0002-fdmon-io_uring-avoid-idle-event-loop-being-accounted.patch;hb=HEAD>:
  "nearly 100% IO pressure in PSI on idle VMs" since 10.2.
- [forum-184026]: <https://forum.proxmox.com/threads/increase-in-io-pressure-after-9-2-3-update.184026/>: a
  Proxmox staff member: QEMU 10.2 switched the event loops to io_uring, which changes IO accounting.
- [qemu-cl-9.1]: <https://wiki.qemu.org/ChangeLog/9.1>: HPET fixes.
- [qemu-cl-10.0]: <https://wiki.qemu.org/ChangeLog/10.0>: Windows PCI Label Id workaround; RWF_DSYNC.
- [qemu-cl-10.1]: <https://wiki.qemu.org/ChangeLog/10.1>: machine types < 4.1 removed.
- [qemu-cl-10.2]: <https://wiki.qemu.org/ChangeLog/10.2>: io_uring main loop; HPET without BQL.
- [qemu-cl-11.0]: <https://wiki.qemu.org/ChangeLog/11.0>: pc-i440fx 2.6–4.0 removed.
- [qemu-ded29e6]: <https://gitlab.com/qemu-project/qemu/-/commit/ded29e64c6f3>: fdmon-io_uring integrated into
  the glib main loop.
- [qemu-961fcc0]: <https://gitlab.com/qemu-project/qemu/-/commit/961fcc0f22>: delayed-completion fix,
  included in v11.0.0.
- [qemu-gh-history]: <https://github.com/qemu/qemu/commits/master/hw/ide/core.c> (and the same for
  `hw/ide/piix.c`, `hw/net/e1000.c`, `hw/usb/hcd-uhci.c`): commit history 2024–2026.
- [qemu-ide-core]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/ide/core.c>: `ide_identify` words 63,
  88 and 93.
- [qemu-piix]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/ide/piix.c>: PIIX3 IDE =
  `PCI_DEVICE_ID_INTEL_82371SB_1`.
- [qemu-pc-piix]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/i386/pc_piix.c>: PIIX3 south bridge.
- [qemu-uhci]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/hcd-uhci.c>: `FRAME_TIMER_FREQ
  1000`, `uhci_frame_timer`.
- [qemu-ehci]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/hcd-ehci.c>: `async_stepdown`
  back-off.
- [qemu-dev-hid]: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/dev-hid.c>: tablet `bInterval`
  10 ms full-speed, 1 ms high-speed.
- [kvm-halt-poll]: <https://docs.kernel.org/virt/kvm/halt-polling.html>: polling converts idle time into host
  CPU time.
- [kvm-host-h]: <https://github.com/torvalds/linux/blob/master/arch/x86/include/asm/kvm_host.h>:
  `KVM_HALT_POLL_NS_DEFAULT 200000`.
- [ms-qpc]: <https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps>:
  HPET used since Vista.
- [virtio-win archive]: <https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/>.
- [virtio-win 0.1.185]:
  <https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.185-2/virtio-win-0.1.185.iso>:
  `/NetKVM/2k3/x86` and `/viostor/2k3/x86` at 52.81.104.17600.
- [virtio-win-changelog]: <https://fedorapeople.org/groups/virt/virtio-win/CHANGELOG>: 0.1.132 locks the WinXP
  viostor at 0.1.130.
- [netkvm-inf]: `NetKVM/2k3/x86/netkvm.inf` on the 0.1.185 ISO above: offload defaults.
- [KB920739]: <https://web.archive.org/web/20110101033201/http://support.microsoft.com/kb/920739>:
  `SystemCacheDirtyPageThreshold`.
- [KB817472]: <https://web.archive.org/web/20101225004419/http://support.microsoft.com/kb/817472/>: atapi DMA →
  PIO after six errors; `ResetErrorCountersOnSuccess`.
- [KB317249]: <https://web.archive.org/web/20101210083758/http://support.microsoft.com/kb/317249>: Srv events
  2021/2022.
- [KB312362]: <https://web.archive.org/web/20101228182541/http://support.microsoft.com/kb/312362>: Srv 2020,
  `PoolUsageMaximum`, `PagedPoolSize`.
- [KB320829]: <https://web.archive.org/web/20110109131346/http://support.microsoft.com/kb/320829>: SizReqBuf.
- [KB285089]: <https://web.archive.org/web/20101223194531/http://support.microsoft.com/kb/285089>: IRPStackSize.
- [KB263532]: <https://web.archive.org/web/20101222170257/http://support.microsoft.com/kb/263532>: system-state
  restore to different hardware unsupported.
- [KB249694]: <https://web.archive.org/web/20101230184219/http://support.microsoft.com/kb/249694>: moving
  Windows to different hardware; HAL rules.
- [KB255504]: <https://web.archive.org/web/20110104044114/http://support.microsoft.com/kb/255504>: ntdsutil
  FSMO transfer/seize.
- [KB324801]: <https://web.archive.org/web/20101230100857/http://support.microsoft.com/kb/324801>: view and
  transfer FSMO roles in 2003.
- [KB216498]: <https://web.archive.org/web/20101230023203/http://support.microsoft.com/kb/216498>: metadata
  cleanup after failed demotion.

[pve-qm]: https://pve.proxmox.com/pve-docs/chapter-qm.html
[pve-roadmap]: https://pve.proxmox.com/wiki/Roadmap
[pve-w2k3]: https://pve.proxmox.com/wiki/Windows_2003_guest_best_practices
[qs-helpers]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Helpers.pm;hb=HEAD
[qs-timer]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Cfg2Cmd/Timer.pm;hb=HEAD
[qs-cpuconfig]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/CPUConfig.pm;hb=HEAD
[qs-machine]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Machine.pm;hb=HEAD
[qs-usb]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/USB.pm;hb=HEAD
[qs-drive]: https://git.proxmox.com/?p=qemu-server.git;a=blob_plain;f=src/PVE/QemuServer/Drive.pm;hb=HEAD
[pve-qemu-log]: https://git.proxmox.com/?p=pve-qemu.git;a=log;hb=HEAD
[pve-fdmon-patch]: https://git.proxmox.com/?p=pve-qemu.git;a=blob_plain;f=debian/patches/extra/0002-fdmon-io_uring-avoid-idle-event-loop-being-accounted.patch;hb=HEAD
[forum-184026]: https://forum.proxmox.com/threads/increase-in-io-pressure-after-9-2-3-update.184026/
[qemu-cl-9.1]: https://wiki.qemu.org/ChangeLog/9.1
[qemu-cl-10.0]: https://wiki.qemu.org/ChangeLog/10.0
[qemu-cl-10.1]: https://wiki.qemu.org/ChangeLog/10.1
[qemu-cl-10.2]: https://wiki.qemu.org/ChangeLog/10.2
[qemu-cl-11.0]: https://wiki.qemu.org/ChangeLog/11.0
[qemu-ded29e6]: https://gitlab.com/qemu-project/qemu/-/commit/ded29e64c6f3
[qemu-961fcc0]: https://gitlab.com/qemu-project/qemu/-/commit/961fcc0f22
[qemu-gh-history]: https://github.com/qemu/qemu/commits/master/hw/ide/core.c
[qemu-ide-core]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/ide/core.c
[qemu-piix]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/ide/piix.c
[qemu-pc-piix]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/i386/pc_piix.c
[qemu-uhci]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/hcd-uhci.c
[qemu-ehci]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/hcd-ehci.c
[qemu-dev-hid]: https://gitlab.com/qemu-project/qemu/-/blob/master/hw/usb/dev-hid.c
[kvm-halt-poll]: https://docs.kernel.org/virt/kvm/halt-polling.html
[kvm-host-h]: https://github.com/torvalds/linux/blob/master/arch/x86/include/asm/kvm_host.h
[ms-qpc]: https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps
[virtio-win archive]: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/
[virtio-win 0.1.185]: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.185-2/virtio-win-0.1.185.iso
[virtio-win-changelog]: https://fedorapeople.org/groups/virt/virtio-win/CHANGELOG
[netkvm-inf]: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.185-2/virtio-win-0.1.185.iso
[KB920739]: https://web.archive.org/web/20110101033201/http://support.microsoft.com/kb/920739
[KB817472]: https://web.archive.org/web/20101225004419/http://support.microsoft.com/kb/817472/
[KB317249]: https://web.archive.org/web/20101210083758/http://support.microsoft.com/kb/317249
[KB312362]: https://web.archive.org/web/20101228182541/http://support.microsoft.com/kb/312362
[KB320829]: https://web.archive.org/web/20110109131346/http://support.microsoft.com/kb/320829
[KB285089]: https://web.archive.org/web/20101223194531/http://support.microsoft.com/kb/285089
[KB263532]: https://web.archive.org/web/20101222170257/http://support.microsoft.com/kb/263532
[KB249694]: https://web.archive.org/web/20101230184219/http://support.microsoft.com/kb/249694
[KB255504]: https://web.archive.org/web/20110104044114/http://support.microsoft.com/kb/255504
[KB324801]: https://web.archive.org/web/20101230100857/http://support.microsoft.com/kb/324801
[KB216498]: https://web.archive.org/web/20101230023203/http://support.microsoft.com/kb/216498
