"""Prepare private task configuration and short-lived CA; never trust/install it."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from catalog_protocol import HOSTS, private_save
from network_profile import inspect_network


def prepare(folder, target, title, service, port=18089):
    network = inspect_network(service)
    if not 1024 <= port <= 65535: raise ValueError('invalid_listen_port')
    folder = folder.resolve()
    folder.mkdir(mode=0o700)  # Refuse to overwrite or reuse another task's CA.
    os.chmod(folder, 0o700)
    ca_dir = folder/'ca'
    ca_dir.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Codex Replay Task '+now.strftime('%Y%m%d%H%M'))])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(hours=48))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.NameConstraints([x509.DNSName(h) for h in HOSTS], None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256()))
    pub = cert.public_bytes(serialization.Encoding.PEM)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())+pub
    for filename, content in [('mitmproxy-ca.pem', pem), ('mitmproxy-ca-cert.pem', pub)]:
        with os.fdopen(os.open(ca_dir/filename, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'wb') as f:
            f.write(content)
    config = {'targetName': target, 'seedTitle': title, 'maxPages': 500,
              'token': secrets.token_hex(32), 'port': port, 'upstream': network['upstream'],
              'service': service, 'network': network['proxies'],
              'ca_sha256': cert.fingerprint(hashes.SHA256()).hex(), 'capture_folder': str(folder/'capture')}
    private_save(folder/'config.private.json', config)
    commands = ['-getwebproxy', '-getsecurewebproxy', '-getsocksfirewallproxy',
                '-getautoproxyurl', '-getproxyautodiscovery', '-getproxybypassdomains']
    baseline = {cmd: subprocess.check_output(['/usr/sbin/networksetup', cmd, service], text=True) for cmd in commands}
    baseline['effective'] = subprocess.check_output(['/usr/sbin/scutil', '--proxy'], text=True)
    private_save(folder/'network-baseline.private.json', baseline)
    report = {'ca_sha256': config['ca_sha256'], 'expires_utc': cert.not_valid_after_utc.isoformat(),
              'trusted': False, 'proxy_started': False, 'system_changed': False,
              'hosts': HOSTS, 'listen': '127.0.0.1:'+str(port), 'upstream': config['upstream'],
              'certificate_scope_note': 'NameConstraints is defense in depth, not a proven OS trust sandbox.'}
    private_save(folder/'preparation.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--target', required=True)
    parser.add_argument('--seed-title', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--port', type=int, default=18089)
    args = parser.parse_args()
    print(json.dumps(prepare(args.folder, args.target, args.seed_title, args.service, args.port), ensure_ascii=False))
