"""Read the selected macOS network service without changing VPN or proxy state."""
import ipaddress
import subprocess


def proxy_fields(raw):
    fields = dict(line.split(': ', 1) for line in raw.splitlines() if ': ' in line)
    return {'enabled': fields['Enabled'] == 'Yes', 'server': fields['Server'],
            'port': int(fields['Port']), 'auth': fields['Authenticated Proxy Enabled'] != '0'}


def inspect_network(service):
    if not service or service.startswith('-') or any(ord(c) < 32 for c in service):
        raise ValueError('invalid_network_service')
    def read(option):
        return subprocess.check_output(['/usr/sbin/networksetup', option, service], text=True, timeout=10)
    if 'Yes' in read('-getautoproxyurl') or 'On' in read('-getproxyautodiscovery'):
        raise ValueError('pac_or_autodiscovery_needs_manual_review')
    proxies = {kind: proxy_fields(read('-get' + kind)) for kind in ('webproxy', 'securewebproxy')}
    values = list(proxies.values())
    if any(v['auth'] for v in values):
        raise ValueError('authenticated_proxy_not_supported')
    if values[0] != values[1]:
        raise ValueError('different_http_https_proxy_needs_review')
    active = values[0]
    upstream = ''
    if active['enabled']:
        if not ipaddress.ip_address(active['server']).is_loopback or not 1 <= active['port'] <= 65535:
            raise ValueError('non_loopback_upstream_needs_review')
        host = '[' + active['server'] + ']' if ':' in active['server'] else active['server']
        upstream = 'http://' + host + ':' + str(active['port'])
    return {'proxies': proxies, 'upstream': upstream}
