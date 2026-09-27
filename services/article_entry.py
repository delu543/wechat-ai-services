"""Article compatibility adapter with suite-owned config and disabled scheduling."""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.runtime import home, private_dir
sys.path.insert(0, str(ROOT/'components/articles'))


def main():
    import yaml
    from wechat_crawler.main import main as upstream
    args = sys.argv[1:]
    if '--config' in args or any(x.startswith('--config=') for x in args):
        raise ValueError('suite_config_cannot_be_overridden_use_explicit_migration')
    if any(x in args for x in ('automation-install-launch-agent', 'automation-uninstall-launch-agent')):
        raise ValueError('background_service_requires_separate_installation_review_see_MIGRATION')
    if '--help' in args or '-h' in args:
        return upstream(args)
    base = private_dir(home()/'articles')
    config = base/'config.yaml'
    if not config.exists():
        value = {'database': {'path': str(base/'data/archive.sqlite')},
                 'output': {'path': str(base/'output')},
                 'web': {'host': '127.0.0.1', 'port': 8876},
                 'automation': {'enabled': False, 'prepare_wechat_session': False},
                 'discoverers': {'search': {'enabled': False}}}
        with config.open('x', encoding='utf-8') as f:
            yaml.safe_dump(value, f, allow_unicode=True)
    # Source-preserving default: automatic cleanup becomes preview only.
    from wechat_crawler.cleanup import ArchiveCleanup
    original = ArchiveCleanup.prune
    def preview(self, *args, **kwargs):
        kwargs['execute'] = False
        return original(self, *args, **kwargs)
    ArchiveCleanup.prune = preview
    os.chdir(base)
    return upstream(['--config', str(config), *args])


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
