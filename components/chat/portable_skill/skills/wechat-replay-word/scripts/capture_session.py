"""Explicit bounded macOS capture lifecycle. No certificate trust during installation.

Run as the user, never sudo. The independent process owns its proxy child and
always attempts network restoration and exact-certificate revocation separately.
Power loss/SIGKILL require the documented restore command on the next run.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from catalog_protocol import private_save
from proxy_transaction import start, locked, restore_values


def certificate(folder):
    cfg = json.loads((folder / 'config.private.json').read_text())
    certpath = folder / 'ca/mitmproxy-ca-cert.pem'
    if certpath.is_symlink() or folder.is_symlink():
        raise ValueError('unsafe_certificate_path')
    cert = x509.load_pem_x509_certificate(certpath.read_bytes())
    if cert.fingerprint(hashes.SHA256()).hex() != cfg['ca_sha256']:
        raise ValueError('certificate_identity_changed')
    return cfg, certpath, cert


def cleanup(folder):
    cfg, certpath, cert = certificate(folder)
    errors = []
    journal = folder / 'network-transaction.private.json'
    if journal.exists():
        try:
            if not locked(folder, restore_values): errors.append('network_restore_conflict')
        except Exception:
            errors.append('network_restore_failed')
    # Revoke even when restoring the network failed. Do not remove any other CA.
    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
    for command in (
        ['security', 'remove-trusted-cert', str(certpath)],
        ['security', 'delete-certificate', '-Z', cfg['ca_sha256'], str(keychain)],
    ):
        try:
            subprocess.run(command, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            errors.append('certificate_cleanup_command_failed')
    absent, untrusted = False, False
    try:
        listing = subprocess.run(['security', 'find-certificate', '-a', '-Z', str(keychain)],
                                 capture_output=True, timeout=20)
        trusted = subprocess.run(['security', 'verify-cert', '-c', str(certpath)], capture_output=True, timeout=20)
        absent = listing.returncode == 0 and cfg['ca_sha256'].lower() not in listing.stdout.decode().replace(' ', '').lower()
        untrusted = trusted.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        errors.append('certificate_verification_failed')
    if not absent or not untrusted:
        errors.append('certificate_revocation_unverified')
    report = {'phase': 'restored' if not errors else 'needs_manual_rollback', 'errors': errors,
              'certificate_absent': absent, 'certificate_untrusted': untrusted,
              'updated': time.time()}
    private_save(folder / 'lifecycle.json', report)
    return not errors


def capture(folder, minutes):
    cfg, certpath, cert = certificate(folder)
    if cert.not_valid_after_utc <= datetime.now(timezone.utc):
        raise ValueError('expired_task_certificate')
    if not 1 <= minutes <= 240: raise ValueError('invalid_duration')
    import socket
    with socket.socket() as check:
        try: check.bind(('127.0.0.1', cfg['port']))
        except OSError: raise ValueError('capture_port_already_in_use') from None
    proxy = None
    complete = False
    restored = False
    def interrupted(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, interrupted)
    private_save(folder / 'lifecycle.json', {'phase': 'trust_pending', 'pid': os.getpid(), 'updated': time.time()})
    try:
        # Only this explicit --confirmed-capture entry point can install user trust.
        result = subprocess.run(['security', 'add-trusted-cert', '-r', 'trustRoot', '-k',
            str(Path.home() / 'Library/Keychains/login.keychain-db'), str(certpath)], capture_output=True, timeout=60)
        if result.returncode: raise ValueError('user_certificate_trust_failed')
        proxy = subprocess.Popen([sys.executable, str(Path(__file__).with_name('run_catalog_proxy.py')),
            str(folder), '--enable-capture'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        import socket
        deadline = time.monotonic() + 15
        while True:
            if proxy.poll() is not None: raise ValueError('proxy_start_failed')
            try:
                with socket.create_connection(('127.0.0.1', cfg['port']), timeout=.5): break
            except OSError:
                if time.monotonic() >= deadline: raise ValueError('proxy_start_timeout')
                time.sleep(.25)
        start(folder, proxy.pid, minutes)
        private_save(folder / 'lifecycle.json', {'phase': 'capture_active', 'pid': os.getpid(),
                     'deadline': time.time()+60*minutes, 'updated': time.time()})
        deadline = time.monotonic()+60*minutes
        while time.monotonic() < deadline and proxy.poll() is None:
            catalog = Path(cfg['capture_folder'])/'catalog.private.json'
            if catalog.exists() and json.loads(catalog.read_text()).get('complete'):
                complete = True
                break
            time.sleep(2)
    finally:
        try:
            restored = cleanup(folder)
        finally:
            if proxy and proxy.poll() is None:
                proxy.terminate()
                try: proxy.wait(timeout=10)
                except subprocess.TimeoutExpired: proxy.kill(); proxy.wait()
    if not restored:
        raise ValueError('capture_rollback_requires_attention')
    if not complete:
        raise ValueError('catalog_capture_incomplete')


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['start', 'restore'])
    p.add_argument('folder', type=Path); p.add_argument('--minutes', type=int, default=15)
    p.add_argument('--confirmed-capture', action='store_true')
    args = p.parse_args()
    if sys.platform != 'darwin': p.error('macOS capture only; no Windows capture acceptance')
    if args.action == 'start':
        if not args.confirmed_capture: p.error('current-scope explicit capture approval required')
        capture(args.folder.resolve(), args.minutes)
    else:
        raise SystemExit(0 if cleanup(args.folder.resolve()) else 2)
