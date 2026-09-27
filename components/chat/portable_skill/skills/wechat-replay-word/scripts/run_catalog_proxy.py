"""Explicit capture launcher; no system proxy/trust mutation, no auto startup."""
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster
from catalog_protocol import HOSTS
from network_profile import inspect_network
from catalog_proxy import CatalogProxy


def checked_options(folder, config):
    ca_dir = folder/'ca'
    pub = ca_dir/'mitmproxy-ca-cert.pem'
    private = ca_dir/'mitmproxy-ca.pem'
    if not pub.is_file() or not private.is_file() or private.stat().st_mode & 0o077:
        raise ValueError('missing_or_unsafe_task_ca')
    cert = x509.load_pem_x509_certificate(pub.read_bytes())
    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    if cert.fingerprint(hashes.SHA256()).hex() != config['ca_sha256'] or key.public_key() != cert.public_key():
        # Public key objects support value equality in the pinned cryptography version.
        raise ValueError('task_ca_mismatch')
    if cert.not_valid_after_utc <= datetime.now(timezone.utc):
        raise ValueError('expired_task_ca')
    current = inspect_network(config['service'])
    if current['proxies'] != config['network'] or current['upstream'] != config['upstream']:
        raise ValueError('network_baseline_changed')
    if not 1024 <= config['port'] <= 65535: raise ValueError('invalid_listen_port')
    return options.Options(listen_host='127.0.0.1', listen_port=config['port'],
        mode=['upstream:'+config['upstream']] if config['upstream'] else ['regular'], confdir=str(ca_dir),
        allow_hosts=['^'+re.escape(h)+':443$' for h in HOSTS],
        ssl_insecure=False, http2=True, http3=False)


async def main(folder):
    config = json.loads((folder/'config.private.json').read_text())
    opts = checked_options(folder, config)
    logging.disable(logging.CRITICAL)  # Never persist connection URLs or headers.
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.options.update(connection_strategy='lazy', onboarding=False, anticache=False, command_history=False,
                          hardump='', save_stream_file='', body_size_limit='20m')
    master.addons.add(CatalogProxy(config, Path(config['capture_folder'])))
    print('Local catalog proxy starting; no system settings changed.', flush=True)
    await master.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--enable-capture', action='store_true', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(main(args.folder.resolve()))
