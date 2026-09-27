"""Journaled Wi-Fi HTTP(S) proxy transaction. CA trust is a separate approved step.

Watchdog restores only endpoints still owned by this transaction. It never
disables an existing VPN, overwrites newer user settings or deletes files.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from catalog_protocol import private_save

KINDS = ('webproxy', 'securewebproxy')
OWNED = {'enabled': True, 'server': '127.0.0.1', 'port': 18089, 'auth': False}


class Network:
    def __init__(self, service='Wi-Fi'):
        self.service = service
    def get(self, kind):
        raw = subprocess.check_output(['/usr/sbin/networksetup', '-get'+kind, self.service], text=True)
        fields = dict(line.split(': ', 1) for line in raw.splitlines() if ': ' in line)
        return {'enabled': fields['Enabled'] == 'Yes', 'server': fields['Server'],
                'port': int(fields['Port']), 'auth': fields['Authenticated Proxy Enabled'] != '0'}

    def set(self, kind, value):
        subprocess.run(['/usr/sbin/networksetup', '-set'+kind, self.service, value['server'], str(value['port'])],
                       check=True, capture_output=True, timeout=20)
        subprocess.run(['/usr/sbin/networksetup', '-set'+kind+'state', self.service, 'on' if value['enabled'] else 'off'],
                       check=True, capture_output=True, timeout=20)


def apply_values(state, network, save):
    owned = state.get('owned', OWNED)
    for kind in KINDS:
        if network.get(kind) != state['original'][kind]:
            raise ValueError('network_changed_before_apply')
    state['phase'] = 'applying'; save(state)
    for kind in KINDS:
        state['attempted'].append(kind); save(state)  # Intent saved before mutation.
        network.set(kind, owned)
        if network.get(kind) != owned:
            raise ValueError('proxy_did_not_stick')
    state['phase'] = 'active'; save(state)


def restore_values(state, network, save):
    owned = state.get('owned', OWNED)
    conflicts = []
    for kind in reversed(state['attempted']):
        current, original = network.get(kind), state['original'][kind]
        if current == original:
            continue
        # A failed setter can leave the same owned endpoint with the old Enabled value.
        ours = current['server'] == owned['server'] and current['port'] == owned['port'] and not current['auth']
        if not ours:
            conflicts.append(kind); continue
        network.set(kind, original)
        if network.get(kind) != original:
            conflicts.append(kind)
    state['conflicts'] = conflicts
    state['phase'] = 'restore_conflict' if conflicts else 'restored'
    save(state)
    return not conflicts


def locked(folder, operation):
    with (folder/'network.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = folder/'network-transaction.private.json'
        state = json.loads(journal.read_text())
        return operation(state, Network(state.get('service', 'Wi-Fi')), lambda value: private_save(journal, value))


def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


def watchdog(folder):
    journal = folder/'network-transaction.private.json'
    while True:
        state = json.loads(journal.read_text())
        if state['phase'] in ('restored', 'restore_conflict'):
            return
        if time.time() >= state['deadline'] or not alive(state['proxy_pid']):
            locked(folder, restore_values)
            return
        time.sleep(2)


def start(folder, pid, minutes=15):
    config = json.loads((folder/'config.private.json').read_text())
    if not 1 <= minutes <= 240: raise ValueError('invalid_approved_duration')
    network = Network(config['service'])
    if not alive(pid): raise ValueError('proxy_process_not_running')
    original = {k: network.get(k) for k in KINDS}
    if original != config['network'] or any(v['auth'] for v in original.values()):
        raise ValueError('baseline_changed_or_auth_proxy')
    journal = folder/'network-transaction.private.json'
    if journal.exists(): raise ValueError('existing_transaction_preserved')
    state = {'phase': 'prepared', 'original': original, 'attempted': [],
             'proxy_pid': pid, 'deadline': time.time()+60*minutes, 'conflicts': [],
             'service': config['service'], 'owned': dict(OWNED, port=config['port'])}
    private_save(journal, state)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watchdog', str(folder)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    try:
        locked(folder, apply_values)
    except Exception:
        locked(folder, restore_values)
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['apply', 'restore', 'watchdog'])
    p.add_argument('folder', type=Path)
    p.add_argument('--proxy-pid', type=int)
    p.add_argument('--confirmed', action='store_true')
    p.add_argument('--minutes', type=int, default=15)
    args = p.parse_args(); folder = args.folder.resolve()
    os.umask(0o077)
    if args.action == 'apply':
        if not args.confirmed or not args.proxy_pid: p.error('explicit approval and proxy PID required')
        start(folder, args.proxy_pid, args.minutes)
    elif args.action == 'restore':
        locked(folder, restore_values)
    else:
        watchdog(folder)
