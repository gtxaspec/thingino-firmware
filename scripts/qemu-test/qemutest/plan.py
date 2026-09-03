"""The ordered suite table that drives a run."""

import time
from .config import PORTAL_PORT, SSH_FWD_PORT, WEBUI_PORT
from .playwright import run_playwright
from .suites.common import setup_ssh, test_boot, test_health, test_services
from .suites.net import test_dns_and_pref, test_dual_stack_listeners, test_ethernet, test_ethwifi_behavior, test_ipv4_dhcp, test_ipv6, test_link_flap, test_mdns, test_ntp, test_persist_reboot, test_syslog_remote
from .suites.onvif import test_onvif
from .suites.webui import test_host_http, test_host_webui_access, test_webui
from .suites.wifi import test_host_portal_access, test_provision_reboot_sta, test_wifi_bridge_setup, test_wifi_modules, test_wifi_portal


#
# One ordered table drives the whole run. A suite is declared once, with
# the modes it applies to and the capabilities it needs, and everything
# else (execution order, --only filtering, the --only help text) derives
# from it. Adding a suite is one entry; nothing in main() changes.
#
# The table is the union of both test plans: mode filtering alone yields
# the wifi sequence and the eth/ethwifi sequence, so the rows stay in one
# list rather than diverging into per-mode schedules.
def suite_ssh_lab(ctx):
    setup_ssh(ctx, ctx.guest_v4)


def suite_ssh_forward(ctx):
    time.sleep(2)                      # let the bridge settle before keying
    setup_ssh(ctx, "127.0.0.1", SSH_FWD_PORT)


def suite_dns(ctx):
    test_dns_and_pref(ctx)
    test_dual_stack_listeners(ctx)


def suite_net_services(ctx):
    test_mdns(ctx)
    test_ntp(ctx)
    test_syslog_remote(ctx)


def suite_playwright_wifi(ctx):
    ctx.playwright_ok = run_playwright(ctx.res, ctx.report_dir, ctx.mode, {
        "PORTAL_URL": f"http://localhost:{PORTAL_PORT}",
        "WEBUI_URL": f"http://localhost:{WEBUI_PORT + 1}",
        "WEBUI_PORT": str(WEBUI_PORT + 1),
        "SKIP_WEBUI": "1",
    })


def suite_playwright_eth(ctx):
    ctx.playwright_ok = run_playwright(ctx.res, ctx.report_dir, ctx.mode, {
        "PORTAL_URL": f"http://{ctx.guest_v4}",
        "WEBUI_URL": f"http://{ctx.guest_v4}",
        "WEBUI_PORT": "80",
        "SKIP_WEBUI": "0",
        "SKIP_PORTAL": "1",
        "SKIP_PROVISION": "1",
    })


ALL_MODES = ("wifi", "eth", "ethwifi")


WIRED = ("eth", "ethwifi")


class Suite:
    """One row of the run plan.

    name      --only token, and the label; rows may share one (a token
              like "webui" can pull in more than one row)
    fn        callable taking the run context
    modes     modes this row applies to
    requires  capability names from Ctx.has(); an unmet requirement skips
              the row silently, exactly as the old inline guards did
    header    section banner printed before the row, if any
    optional  False = always runs, never filtered out by --only
    """

    def __init__(self, name, fn, modes=ALL_MODES, requires=(), header=None,
                 optional=True):
        self.name = name
        self.fn = fn
        self.modes = modes
        self.requires = requires
        self.header = header
        self.optional = optional

    def wanted(self, ctx, only):
        if ctx.mode not in self.modes:
            return False
        if self.optional and only and self.name not in only:
            return False
        return all(ctx.has(c) for c in self.requires)


SUITES = [
    Suite("boot", test_boot, optional=False),
    Suite("procs", test_services, optional=False),

    Suite("wifi", test_wifi_modules, ("wifi",), header="WiFi",
          optional=False),
    Suite("wifi", test_wifi_portal, ("wifi",), optional=False),

    Suite("ethernet", test_ethernet, WIRED, header="Ethernet",
          optional=False),
    Suite("ethwifi", test_ethwifi_behavior, ("ethwifi",),
          header="Ethernet + WiFi behavior", optional=False),

    Suite("ipv4", test_ipv4_dhcp, WIRED, ("lab",), header="IPv4 DHCP",
          optional=False),
    Suite("ssh", suite_ssh_lab, WIRED, ("lab", "v4"), optional=False),
    Suite("ipv6", test_ipv6, WIRED, ("lab",), header="IPv6"),
    Suite("dns", suite_dns, WIRED, ("lab",), header="DNS / dual-stack"),

    Suite("health", test_health, header="Health"),

    Suite("webui", test_webui, WIRED, header="Web UI"),
    Suite("webui", test_host_http, WIRED, ("lab", "v4")),
    Suite("onvif", test_onvif, WIRED, ("lab", "v4"), header="ONVIF"),
    Suite("services", suite_net_services, WIRED, ("lab", "v4"),
          header="mDNS / NTP / syslog"),

    Suite("bridge", test_wifi_bridge_setup, ("wifi",), ("host",),
          header="Bridge setup", optional=False),
    Suite("ssh", suite_ssh_forward, ("wifi",), ("host",), optional=False),
    Suite("portal", test_host_portal_access, ("wifi",), ("host",),
          header="Host access", optional=False),

    Suite("playwright", suite_playwright_wifi, ("wifi",), ("pw", "host")),
    # Only meaningful once the browser has actually submitted the portal
    # form, so it requires the playwright row above to have passed.
    Suite("persist", test_provision_reboot_sta, ("wifi",),
          ("pw", "host", "reboot", "pw_ok"), optional=False),

    Suite("playwright", suite_playwright_eth, WIRED, ("pw", "lab", "v4")),
    Suite("webui", test_host_webui_access, WIRED, ("host", "nolab"),
          header="Host access"),
    Suite("linkflap", test_link_flap, WIRED, ("lab", "qmp", "v4"),
          header="Link flap"),
    Suite("persist", test_persist_reboot, WIRED, ("reboot",)),
]


OPTIONAL_SUITES = sorted({s.name for s in SUITES if s.optional})


def run_suites(ctx, only):
    for suite in SUITES:
        if not suite.wanted(ctx, only):
            continue
        if suite.header:
            print(f"\n── {suite.header} ──")
        suite.fn(ctx)
