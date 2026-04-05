from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.topo import Topo
from mininet.log import setLogLevel
import time
import json

# ─────────────────────────────────────────────
#  Topology
# ─────────────────────────────────────────────

class DualPathTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip=None)
        h2 = self.addHost('h2', ip=None)

        s1 = self.addSwitch('s1', cls=OVSBridge)
        s2 = self.addSwitch('s2', cls=OVSBridge)

        self.addLink(h1, s1, bw=10, delay='10ms')
        self.addLink(s1, h2, bw=10, delay='10ms')

        self.addLink(h1, s2, bw=20, delay='50ms')
        self.addLink(s2, h2, bw=20, delay='50ms')


# ─────────────────────────────────────────────
#  Network setup
# ─────────────────────────────────────────────

def setup_ips(h1, h2):
    for iface, ip in [('h1-eth0', '10.0.0.1/24'), ('h1-eth1', '10.0.1.1/24')]:
        h1.cmd(f'ip addr flush dev {iface}')
        h1.cmd(f'ip addr add {ip} dev {iface}')
        h1.cmd(f'ip link set {iface} up')

    for iface, ip in [('h2-eth0', '10.0.0.2/24'), ('h2-eth1', '10.0.1.2/24')]:
        h2.cmd(f'ip addr flush dev {iface}')
        h2.cmd(f'ip addr add {ip} dev {iface}')
        h2.cmd(f'ip link set {iface} up')


def setup_routing(h1, h2):
    for host, src0, src1 in [
        (h1, '10.0.0.1', '10.0.1.1'),
        (h2, '10.0.0.2', '10.0.1.2'),
    ]:
        pfx0 = src0.rsplit('.', 1)[0] + '.0/24'
        pfx1 = src1.rsplit('.', 1)[0] + '.0/24'
        eth0 = f'{host.name}-eth0'
        eth1 = f'{host.name}-eth1'
        peer = '10.0.0.2' if host.name == 'h1' else '10.0.0.1'

        host.cmd('ip route del default 2>/dev/null || true')

        host.cmd(f'ip route add {pfx0} dev {eth0} scope link table 1')
        host.cmd(f'ip route add default dev {eth0} table 1')
        host.cmd(f'ip rule add from {src0} table 1 priority 101')

        host.cmd(f'ip route add {pfx1} dev {eth1} scope link table 2')
        host.cmd(f'ip route add default dev {eth1} table 2')
        host.cmd(f'ip rule add from {src1} table 2 priority 102')

        host.cmd(f'ip route add {peer} dev {eth0} table main')


def setup_mptcp(h1, h2):
    for host in (h1, h2):
        host.cmd('sysctl -w net.mptcp.enabled=1')
        host.cmd('ip mptcp endpoint flush')

    h1.cmd('ip mptcp endpoint add 10.0.0.1 dev h1-eth0 subflow signal')
    h1.cmd('ip mptcp endpoint add 10.0.1.1 dev h1-eth1 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.0.2 dev h2-eth0 subflow signal')
    h2.cmd('ip mptcp endpoint add 10.0.1.2 dev h2-eth1 subflow signal')

    for host in (h1, h2):
        host.cmd('ip mptcp limits set subflows 2 add_addr_accepted 2')


# ─────────────────────────────────────────────
#  Output helpers
# ─────────────────────────────────────────────

W = 62

def banner(title, char='='):
    print(f"\n{char * W}")
    print(f"  {title}")
    print(f"{char * W}")


def section(title):
    banner(title, char='-')


def parse_iperf_json(raw, label):
    """
    Parse iperf3 --json output.
    Returns (avg_mbps, per_second_list) — always returns valid types,
    never raises, so the summary block never crashes.
    """
    try:
        start = raw.find('{')
        if start == -1:
            raise ValueError("no JSON found — iperf3 may have been killed before finishing")
        data = json.loads(raw[start:])

        per_sec     = [iv['sum']['bits_per_second'] / 1e6
                       for iv in data['intervals']]
        sent        = data['end']['sum_sent']
        received    = data['end']['sum_received']
        avg_mbps    = received['bits_per_second'] / 1e6
        retransmits = sent.get('retransmits', 0)
        duration    = sent['seconds']

        section(label)
        print(f"  Duration      : {duration:.1f} s")
        print(f"  Avg throughput: {avg_mbps:.2f} Mbps")
        print(f"  Peak          : {max(per_sec):.2f} Mbps")
        print(f"  Min           : {min(per_sec):.2f} Mbps")
        print(f"  Data sent     : {sent['bytes'] / 1e6:.2f} MB")
        print(f"  Retransmits   : {retransmits}")

        print(f"\n  Bandwidth per second:")
        print(f"  {'t(s)':>5}  {'Mbps':>7}   bar (relative to peak)")
        print(f"  {'─'*5}  {'─'*7}   {'─'*30}")
        max_bw = max(per_sec) if per_sec else 1
        for i, bw in enumerate(per_sec):
            bar = '█' * int((bw / max_bw) * 30)
            print(f"  {i+1:>5}  {bw:>7.2f}   {bar}")

        return avg_mbps, per_sec

    except Exception as e:
        section(label)
        print(f"  [!] Could not parse result: {e}")
        print(f"      This stream was intentionally killed mid-transfer.")
        print(f"      Its per-second data up to the failure point is shown")
        print(f"      in the combined table below.")
        return 0.0, []


def show_active_subflows(h1):
    """
    Print ESTABLISHED connections to h2.
    Counts only sockets with data in the send queue (actively transferring)
    to avoid double-counting idle control sockets.
    """
    section("Active subflows snapshot")
    out   = h1.cmd('ss -tin dst 10.0.0.2 or dst 10.0.1.2')
    lines = [l for l in out.strip().splitlines() if 'ESTAB' in l]

    active = 0
    for line in lines:
        parts  = line.split()
        send_q = int(parts[2]) if len(parts) > 2 else 0
        # Only count sockets actively sending data
        if send_q > 0:
            active += 1
            print(f"  {line.strip()[:100]}")

    if active == 0:
        print("  (no sockets with data in flight — may have just finished)")

    verdict = ("OK — both paths sending"   if active >= 2 else
               "OK — 1 path sending"       if active == 1 else
               "WARN — no active data flow")
    print(f"\n  Active data sockets : {active}  [{verdict}]")


def wait_for_lte_json(h1, timeout=40):
    """Wait only for the LTE result file (Wi-Fi is killed mid-transfer)."""
    print(f"  Waiting up to {timeout}s for LTE stream to finish...")
    for i in range(timeout):
        time.sleep(1)
        r = h1.cmd('cat /tmp/fail_lte.json 2>/dev/null')
        if r.strip().startswith('{') and r.strip().endswith('}'):
            print(f"  [OK] LTE stream finished after ~{i+1}s")
            return r
    print(f"  [!] Timeout after {timeout}s")
    return h1.cmd('cat /tmp/fail_lte.json 2>/dev/null')


def read_partial_wifi_json(h1):
    """
    Read whatever the Wi-Fi iperf3 wrote before being killed.
    iperf3 writes a complete JSON only at the end, so a killed process
    leaves an incomplete file — we return raw intervals if possible.
    """
    return h1.cmd('cat /tmp/fail_wifi.json 2>/dev/null')


# ─────────────────────────────────────────────
#  Connectivity check
# ─────────────────────────────────────────────

def verify_connectivity(h1):
    banner("CONNECTIVITY CHECK")
    for dst in ('10.0.0.2', '10.0.1.2'):
        out  = h1.cmd(f'ping -c 3 -W 1 {dst}')
        loss = [l for l in out.splitlines() if 'packet loss' in l]
        ok   = loss and '0% packet loss' in loss[0]
        print(f"  ping {dst}  =>  {'[OK]  ' if ok else '[FAIL]'}  "
              f"{loss[0].strip() if loss else 'no response'}")


# ─────────────────────────────────────────────
#  Experiment
# ─────────────────────────────────────────────

TRANSFER_DURATION = 30
FAILURE_AT        = 10   # second at which Wi-Fi is killed

def run_failure_experiment(net, h1, h2):
    banner("EXPERIMENT — PATH FAILURE DURING MPTCP TRANSFER")
    print(f"  Transfer duration : {TRANSFER_DURATION}s")
    print(f"  Failure injected  : t = {FAILURE_AT}s")
    print(f"  Method            : bring h2-eth0 DOWN on h2 directly")
    print(f"  Expected          : LTE path survives, Wi-Fi stream drops to 0")

    # ── Start servers ──
    h2.cmd('pkill -f iperf3 2>/dev/null; sleep 0.5')
    h2.cmd('iperf3 -s -p 5201 &')
    h2.cmd('iperf3 -s -p 5202 &')
    time.sleep(2)

    # ── Start both streams ──
    h1.cmd('rm -f /tmp/fail_wifi.json /tmp/fail_lte.json')

    # Wi-Fi stream — will be killed at FAILURE_AT
    h1.cmd(f'iperf3 -c 10.0.0.2 -B 10.0.0.1 -p 5201 '
           f'-t {TRANSFER_DURATION} -i 1 --json '
           f'> /tmp/fail_wifi.json 2>&1 &')

    # LTE stream — runs the full duration
    h1.cmd(f'iperf3 -c 10.0.1.2 -B 10.0.1.1 -p 5202 '
           f'-t {TRANSFER_DURATION} -i 1 --json '
           f'> /tmp/fail_lte.json 2>&1 &')

    print(f"\n  [t= 0]  Both streams running.")
    time.sleep(FAILURE_AT)

    show_active_subflows(h1)

    # configLinkStatus targets OVS switch ports and often silently fails.
    # Bringing h2-eth0 down at the host level is immediate and reliable.
    print(f"\n  [t={FAILURE_AT:>2}]  Taking h2-eth0 DOWN (Wi-Fi path killed)...")
    h2.cmd('ip link set h2-eth0 down')

    # Without this, the client hangs trying to reconnect and never writes JSON.
    time.sleep(1)
    h1.cmd('pkill -f "iperf3 -c 10.0.0.2" 2>/dev/null')
    print(f"  [t={FAILURE_AT+1:>2}]  Wi-Fi iperf3 client killed — JSON will be partial.")
    print(f"          LTE stream continues alone.")

    time.sleep(2)
    show_active_subflows(h1)

    # ── Wait for LTE stream to finish ──
    remaining = TRANSFER_DURATION - FAILURE_AT - 3
    print(f"\n  [t=~{FAILURE_AT+3}]  Waiting {remaining}s for LTE stream to complete...")
    time.sleep(remaining)

    r_lte  = wait_for_lte_json(h1)
    r_wifi = read_partial_wifi_json(h1)

    # Wi-Fi: parse whatever intervals were written before kill
    _, bw_wifi = parse_iperf_json(r_wifi, f"Stream 1 — Wi-Fi path (killed at t={FAILURE_AT})")
    avg_lte, bw_lte = parse_iperf_json(r_lte, "Stream 2 — LTE path (full 30s)")

    combined = []

    if bw_lte:
        # Pad Wi-Fi bandwidth with zeros from the failure point onwards
        wifi_padded = bw_wifi[:] + [0.0] * (len(bw_lte) - len(bw_wifi))
        combined    = [w + l for w, l in zip(wifi_padded, bw_lte)]

        banner("PER-SECOND THROUGHPUT — WITH FAILURE EVENT")
        print(f"  {'t(s)':>5}  {'Wi-Fi':>7}  {'LTE':>7}  {'Total':>7}   "
              f"{'bar':<24}  annotation")
        print(f"  {'─'*5}  {'─'*7}  {'─'*7}  {'─'*7}   "
              f"{'─'*24}  {'─'*24}")

        max_c = max(combined) if combined else 1
        for i, (bw, bl, bc) in enumerate(zip(wifi_padded, bw_lte, combined)):
            bar = '█' * int((bc / max_c) * 24)

            if i == FAILURE_AT - 1:
                note = "<-- last second before failure"
            elif i == FAILURE_AT:
                note = "<-- FAILURE: h2-eth0 DOWN"
            elif i == FAILURE_AT + 1:
                note = "<-- Wi-Fi client killed"
            elif i == FAILURE_AT + 2:
                note = "<-- LTE stabilising"
            else:
                note = ""

            print(f"  {i+1:>5}  {bw:>7.2f}  {bl:>7.2f}  {bc:>7.2f}   "
                  f"{bar:<24}  {note}")

        try:
            from plot_helpers import save_throughput_timeseries

            # Printed table uses 1-based second index; link goes down after ~FAILURE_AT s.
            save_throughput_timeseries(
                {
                    "Wi-Fi subflow (lost at failure)": wifi_padded,
                    "LTE subflow (continues)": bw_lte,
                    "Combined (session total)": combined,
                },
                "Path failure during dual-path transfer: throughput over time",
                "fig03_path_failure_throughput.png",
                vlines=[
                    (
                        float(FAILURE_AT + 1),
                        "Wi-Fi path failure",
                    )
                ],
            )
        except ImportError:
            print("  [!] plot_helpers / matplotlib not available — skip PNG figures")

    # ── Summary ──
    banner("FAILURE EXPERIMENT SUMMARY")

    if combined:
        pre  = combined[:FAILURE_AT]
        post = combined[FAILURE_AT + 2:]
        print(f"  Avg throughput BEFORE failure   : "
              f"{sum(pre)/len(pre):.2f} Mbps"  if pre  else "  (no pre-failure data)")
        print(f"  Avg throughput AFTER  recovery  : "
              f"{sum(post)/len(post):.2f} Mbps" if post else "  (no post-failure data)")
        zero_gap = sum(1 for c in combined[FAILURE_AT:FAILURE_AT+5] if c < 0.5)
        print(f"  Near-zero intervals at failure  : {zero_gap}s")
    else:
        print("  (combined data unavailable — check parse errors above)")

    print(f"  LTE avg (whole 30s transfer)    : {avg_lte:.2f} Mbps")
    print(f"  Wi-Fi stream intervals captured : {len(bw_wifi)}s")

    if combined and len(combined) > FAILURE_AT + 2:
        post_avg = sum(combined[FAILURE_AT+2:]) / len(combined[FAILURE_AT+2:])
        verdict  = ("OK  — LTE maintained session continuity" if post_avg > 5
                    else "WARN — throughput did not recover")
    else:
        verdict = "WARN — insufficient data for verdict"

    print(f"\n  Verdict : [{verdict}]")
    print(f"\n  What to look for in the table:")
    print(f"    Wi-Fi column should drop to 0 at t={FAILURE_AT+1} and stay 0.")
    print(f"    LTE column should stay ~20 Mbps throughout.")
    print(f"    Total should dip briefly then recover to ~20 Mbps.")

    # ── Restore for clean teardown ──
    print(f"\n  Restoring h2-eth0...")
    h2.cmd('ip link set h2-eth0 up')
    h2.cmd('pkill -f iperf3 2>/dev/null')
    time.sleep(2)

def run():
    setLogLevel('warning')

    net = Mininet(
        topo=DualPathTopo(),
        link=TCLink,
        switch=OVSBridge,
        controller=None,
        autoSetMacs=True
    )

    net.start()
    h1, h2 = net.get('h1', 'h2')

    banner("MPTCP PATH FAILURE EXPERIMENT")
    print("  Topology : h1 <--[s1  Wi-Fi  10 Mbps / 20ms RTT ]--> h2")
    print("             h1 <--[s2  LTE   20 Mbps / 100ms RTT]--> h2")

    setup_ips(h1, h2)
    setup_routing(h1, h2)
    setup_mptcp(h1, h2)
    verify_connectivity(h1)

    run_failure_experiment(net, h1, h2)

    banner("EXPERIMENT COMPLETE")
    net.stop()


if __name__ == '__main__':
    run()