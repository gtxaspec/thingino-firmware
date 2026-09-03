"""
QEMU test harness for thingino firmware images.

Boots a firmware image in QEMU, logs in via serial, and runs assertions
against the running system. Supports three device modalities:

  wifi     - WiFi-only camera (portal mode, provisioning)
  eth      - Ethernet-only camera (wired web UI)
  ethwifi  - Ethernet + WiFi camera (wired takes priority)

and two network backends:

  slirp    - QEMU user networking with host port forwards (default)
  tap      - host tap + dnsmasq lab: real DHCPv4, RA/SLAAC, stateful
             DHCPv6, DNS, NTP, syslog, mDNS, WS-Discovery (needs root)

Usage:
  python3 harness.py --image <path> --soc <t31x|t10n|...> --mode <wifi|eth|ethwifi>

Exit code 0 = all tests pass, 1 = test failure, 2 = boot failure.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from .config import REPO_ROOT, SOC_MACHINES
from .context import Ctx
from .guest import Guest
from .launch import find_qemu, start_qemu
from .plan import OPTIONAL_SUITES, run_suites
from .qmp import Qmp
from .results import TestResult
from .serial import QemuSerial


def collect_artifacts(guest, report_dir):
    """Best-effort dump of guest logs into the report dir."""
    for name, cmd in (("dmesg.txt", "dmesg"),
                      ("logread.txt", "logread 2>/dev/null | tail -n 500"),
                      ("ps.txt", "ps w"),
                      ("netstat.txt", "netstat -tuln 2>/dev/null"),
                      ("ipaddr.txt", "ip addr; ip route; ip -6 route")):
        try:
            rc, out = guest.run(cmd, timeout=25)
            with open(os.path.join(report_dir, name), "w") as f:
                f.write(out)
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", required=True, help="Path to thingino .bin image")
    p.add_argument("--soc", required=True, help="SoC variant (t31x, t10n, ...)")
    p.add_argument("--mode", required=True, choices=["wifi", "eth", "ethwifi"],
                   help="Device modality to test")
    p.add_argument("--net", default="slirp", choices=["slirp", "tap"],
                   help="Network backend (tap enables the full network lab)")
    p.add_argument("--qemu", default=None, help="Path to qemu-system-mipsel")
    p.add_argument("--timeout", type=int, default=240,
                   help="Boot timeout in seconds")
    p.add_argument("--host-tests", action="store_true",
                   help="Also run host-side HTTP tests (slirp mode)")
    p.add_argument("--playwright", action="store_true",
                   help="Run Playwright browser tests")
    p.add_argument("--reboot-test", action="store_true",
                   help="Run reboot persistence / STA connection tests")
    p.add_argument("--report-dir", default=None,
                   help="Report output dir (default: output/<branch>/qemu-test-reports/)")
    p.add_argument("--profile", default=None,
                   help="Profile name for reporting (default qemu_<soc>)")
    p.add_argument("--only", default=None,
                   help="Comma list of optional suites to run: "
                        + ",".join(OPTIONAL_SUITES))
    args = p.parse_args()

    if args.soc not in SOC_MACHINES:
        sys.exit(f"Unknown SoC: {args.soc}")
    if args.net == "tap" and os.geteuid() != 0:
        sys.exit("tap mode requires root (use run.sh, it handles sudo)")

    machine, ram = SOC_MACHINES[args.soc]
    qemu = args.qemu or find_qemu()
    image = os.path.abspath(args.image)
    profile = args.profile or f"qemu_{args.soc}"

    repo_root = REPO_ROOT
    if not args.report_dir:
        branch = "master"
        try:
            branch = subprocess.check_output(
                ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            pass
        args.report_dir = os.path.join(
            repo_root, "output", branch, "qemu-test-reports", profile)

    if not os.path.isfile(image):
        sys.exit(f"Image not found: {image}")

    report_dir = args.report_dir
    os.makedirs(report_dir, exist_ok=True)

    commit = ""
    try:
        commit = subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        pass

    meta = {
        "profile": profile, "soc": args.soc, "mode": args.mode,
        "net": args.net, "machine": machine, "ram_mb": ram,
        "image": os.path.basename(image), "qemu": qemu,
        "commit": commit,
    }

    # Work on a copy so writes don't taint the original
    tmp_image = f"/tmp/qemu-test-{os.getpid()}.bin"
    shutil.copy2(image, tmp_image)

    print(f"Testing {profile} ({args.mode}, {args.net}) "
          f"with {os.path.basename(image)}")
    print(f"QEMU: {qemu}")
    print(f"Machine: {machine}, RAM: {ram}M")
    print()

    lab = None
    if args.net == "tap":
        from .netlab import NetLab
        lab = NetLab(report_dir)
        lab.up()

    t_start = time.time()
    proc, pty, qmp_path = start_qemu(qemu, tmp_image, machine, ram,
                                     args.net, report_dir,
                                     tap_if=lab.tap if lab else "qtap0")
    ser = QemuSerial(proc, pty,
                     log_path=os.path.join(report_dir, "serial.log"))
    guest = Guest(ser)
    res = TestResult()
    qmp = None
    ctx = Ctx(guest, ser, res, args, report_dir, meta)
    ctx.lab = lab
    only = [s.strip() for s in args.only.split(",")] if args.only else None

    def cleanup(sig=None, frame=None):
        try:
            ser.close()
        except Exception:
            pass
        if qmp:
            qmp.close()
        proc.terminate()
        proc.wait()
        try:
            os.unlink(tmp_image)
        except Exception:
            pass
        if lab:
            lab.down()
        if sig:
            sys.exit(128 + sig)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        print("── Boot ──")
        if not ser.login(args.timeout):
            print("  FAIL  boot (no login prompt)")
            # Freeze evidence before teardown: two register snapshots
            # 5 s apart tell a hard CPU freeze (PCs identical) from a
            # slow or wedged-but-running guest.
            stamp = int(time.time())
            try:
                qmp = Qmp(qmp_path)
                for tag in ("t0", "t5"):
                    regs = qmp.cmd("human-monitor-command",
                                   **{"command-line": "info registers -a"})
                    with open(os.path.join(report_dir,
                                           f"bootfail-{stamp}-regs-{tag}.txt"),
                              "w") as f:
                        f.write(str(regs))
                    if tag == "t0":
                        time.sleep(5)
                qmp.close()
            except Exception as e:
                print(f"  (bootfail register dump failed: {e})")
            try:
                shutil.copy2(os.path.join(report_dir, "serial.log"),
                             os.path.join(report_dir,
                                          f"bootfail-{stamp}-serial.log"))
            except OSError:
                pass
            cleanup()
            sys.exit(2)
        boot_s = time.time() - t_start
        meta["boot_seconds"] = round(boot_s, 1)
        res.ok("boot", f"{boot_s:.0f}s to login")

        try:
            qmp = Qmp(qmp_path)
        except RuntimeError as e:
            print(f"  (QMP unavailable: {e})")

        ctx.qmp = qmp
        run_suites(ctx, only)

        print("\n── Artifacts ──")
        collect_artifacts(guest, report_dir)

    finally:
        serial_json = os.path.join(report_dir, "serial-results.json")
        res.save_json(serial_json)
        with open(os.path.join(report_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        from .report import generate_report
        generate_report(res.results, report_dir,
                        profile=profile, mode=args.mode, meta=meta)
        cleanup()

    success = res.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
