# QEMU firmware test suite

Boots a real thingino image under the Ingenic QEMU fork and asserts the
whole network-facing surface of the firmware: boot and services, the WiFi
provisioning portal, DHCPv4/v6, SLAAC, DNS, the web UI, ONVIF, mDNS, NTP,
remote syslog, link flap, and overlay persistence across a reboot. Browser
flows are driven with Playwright and screenshotted.

The image under test is the same `.bin` that gets flashed to a camera. No
firmware code is stubbed or recompiled for testing.

## Quick start

```sh
make GROUP=testing CAMERA=qemu_t31x_eth fast  # build a test profile
scripts/qemu-test/run.sh qemu_t31x_eth        # run its suite
```

`run.sh` derives the SoC and modality from the profile name, finds the
newest matching image under `output/`, picks up the QEMU built alongside
it, and re-execs itself under `sudo` when the run needs a tap device.

```sh
run.sh qemu_t31x                 # wifi portal flow (slirp)
run.sh qemu_t31x_eth             # ethernet + full network lab (tap)
run.sh qemu_t31x_ethwifi         # both interfaces (tap)
run.sh qemu_t31x_eth --net slirp # override the backend
run.sh qemu_t31x_eth --only onvif,ipv6   # just these optional suites
```

Anything after the profile name is passed through to `harness.py`, which
also runs standalone if you need full control (`harness.py --help`).

Requirements: `dnsmasq` and `tcpdump` installed (the lab starts its own
dnsmasq and parks the system one), `npm ci` in this directory plus
`npx playwright install chromium` for the browser tests, and passwordless
`sudo` for tap mode.

## Modality is the profile name

| Profile suffix | Mode | Backend | What it exercises |
| --- | --- | --- | --- |
| *(none)* | `wifi` | slirp | portal, provisioning, reboot into STA |
| `_eth` | `eth` | tap | wired only, full network lab |
| `_ethwifi` | `ethwifi` | tap | wired + WiFi, gateway takeover |

XBurst2 profiles (`t40*`, `t41*`) ship the wired stack even without a
suffix, so a bare name means `ethwifi` there.

## Architecture

```
host                                              QEMU guest
----                                              ----------
run.sh          profile -> soc/mode/image/qemu
  harness.py    serial console (login, commands)  ──► ttyS0
                QMP (link up/down, reset, regs)   ──► monitor
                SSH channel (ephemeral ed25519)   ──► dropbear
  netlab.py     qtap0 + dnsmasq: DHCPv4/RA/SLAAC/ ──► eth0
                DHCPv6/DNS, SNTP + syslog sinks,
                WS-Discovery and mDNS probes
  onvif.py      SOAP + WS-UsernameToken           ──► onvif_simple_server
  playwright    portal + web UI in chromium       ──► uhttpd
  report.py     report.html with screenshots
```

Lab addressing: `192.168.100.1/24` and `fd00:5c1::1/64` on `qtap0`, DHCP
pools `.50-.150` and `fd00:5c1::100-1ff`.

## Adding a suite

Suites live in one ordered table, `SUITES` in `harness.py`. A suite is a
function taking the run context, plus one table row. Nothing in `main()`
changes.

```python
def test_isp(ctx):
    guest, res = ctx.guest, ctx.res
    rc, out = guest.run("cat /proc/jz/isp/info")
    res.check("isp_registered", "sensor" in out, out[:60])
```

```python
SUITES = [
    ...
    Suite("isp", test_isp, WIRED, ("lab", "v4"), header="ISP"),
]
```

Row fields:

| Field | Meaning |
| --- | --- |
| `name` | the `--only` token and label; rows may share one |
| `fn` | callable taking `ctx` |
| `modes` | `ALL_MODES`, `WIRED`, or an explicit tuple |
| `requires` | capabilities that must be present, else the row is skipped silently |
| `header` | section banner printed before the row |
| `optional` | `False` means it always runs and `--only` never filters it out |

Capabilities come from `Ctx.has()`: `lab`, `nolab`, `qmp`, `v4`, `host`,
`pw`, `pw_ok`, `reboot`.

Table position is execution order. The table is the union of both test
plans; mode filtering alone yields the wifi sequence and the wired one, so
put a row where it belongs relative to its neighbours and let the filter
do the rest. `--only` tokens and the `--help` text derive from the table,
so there is no second list to update.

### Rules for suite functions

- Read what you need off `ctx` in one line at the top; leave the body
  plain. Never add positional parameters.
- Report through `res`: `check(name, cond, detail)`, `ok`, `fail`,
  `xfail` for a known gap, `skip`. **Check names are the API** - reports,
  CI logs and regression diffs key on them, so renaming one is a breaking
  change.
- Assert positively. `res.check("x", "No such file" not in out)` passes
  against a U-Boot prompt, a timeout, or an empty read; a sentinel like
  `[ -f /path ] && echo OK` cannot.
- Publish discoveries onto `ctx` (`ctx.guest_v4 = ...`) rather than
  returning them, so later rows can `require` them.
- Poll with a deadline instead of sleeping a fixed amount. Under TCG the
  guest is slow and load-sensitive; every fixed wait in here has flaked at
  least once.

### Adding a SoC or profile

A SoC is one line in `SOC_MACHINES` (machine name, RAM in MB). A profile
needs no harness change at all: name it `qemu_<soc>[_eth|_ethwifi]` under
`configs/cameras-testing/` and `run.sh` works out the rest.

## Reports

Everything lands in `output/<branch>/qemu-test-reports/<profile>/`:
`report.html` (self-contained, screenshots inlined), `serial-results.json`
(the ordered check list; this is what regression diffs compare),
`meta.json` (profile, machine, image, qemu path, commit, boot seconds),
`serial.log`, `qemu-stderr.log`, `dmesg.txt`, `logread.txt`, `ps.txt`,
`netstat.txt`, `ipaddr.txt`, the dnsmasq config/log/leases, ONVIF SOAP
dumps, Playwright screenshots, and on a boot failure two QMP register
snapshots five seconds apart plus a timestamped copy of the serial log
(identical PCs across the two mean a hard CPU freeze rather than a slow
guest).

## CI

`.github/workflows/qemu-test.yaml`, manual dispatch only. It builds the
requested profiles in parallel on arm64 runners, then fans out one test
job per profile on x86_64 against the **released** QEMU pinned by
`package/qemu-ingenic/qemu-ingenic.mk` and verified against
`qemu-ingenic.hash`. Reports upload as run artifacts. Steady state is
about 15 minutes.

## Gotchas

These all cost real debugging time; check them before going deeper.

- **ONVIF credentials do not live in `onvif.json`.** The server reads
  RTSP auth from the streamer's own config, first of `/etc/prudynt.json`,
  `/etc/streamer.d/rtsp.json`, `/etc/timps.conf`, `raptorctl`. Use
  `streamer_auth()`, which mirrors that order; a stale copy means 401
  `NotAuthorized`.
- **After `reboot`, the shell echoes one more prompt before it dies.**
  Matching it reports "logged in" while the machine is still going down,
  and the next command lands in the *next* boot's U-Boot autoboot prompt.
  Use `login(expect_reboot=True)`, which waits for a reset banner first.
- **The console shell answers a cursor-position probe** (`ESC[6n`) after
  login. Unanswered, it eats the next command; the serial reader replies
  automatically.
- **Test images boot with `debug=1`** (`configs/cameras-testing/<profile>/uenv.txt`),
  so the console getty drops straight to a root shell with no login
  prompt. `login()` handles both.
- **Wifi mode has an eth0 that real WiFi-only cameras lack.** An instant
  slirp lease makes the wired-gateway logic kill WiFi, so the STA test
  drops the link over QMP first.
- **Slirp host forwards target `10.0.2.15`.** The guest re-randomises its
  MAC each boot, so after a reboot re-add the address statically and ping
  the gateway once to refresh the stale ARP entry.
- **Build load causes timing flakes.** Do not compile while a suite runs.
