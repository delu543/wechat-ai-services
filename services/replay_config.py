"""Prepare a fresh replay task using the selected suite runtime; no capture."""
import argparse
import os
import sys
import uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.runtime import REPLAY, home, private_dir
sys.path.insert(0, str(REPLAY))
from catalog_protocol import private_save


def main():
    p = argparse.ArgumentParser()
    for name in ('name', 'seed', 'model', 'service'):
        p.add_argument('--'+name, required=True)
    a = p.parse_args()
    model = Path(a.model).expanduser().resolve()
    if not all((model/n).is_file() for n in ('weights.safetensors', 'config.json')):
        raise ValueError('local_model_required_no_automatic_download')
    from network_profile import inspect_network
    import imageio_ffmpeg
    network = inspect_network(a.service)
    task = private_dir(home()/'replay/tasks'/uuid.uuid4().hex)
    private_save(task/'batch_config.json', {'root': str(task), 'account_name': a.name,
        'seed_url': a.seed, 'proxy': network['upstream'], 'parallel_ranges': True,
        'min_free_bytes': 20*1024**3, 'ffmpeg': imageio_ffmpeg.get_ffmpeg_exe(),
        'asr_python': sys.executable, 'word_python': sys.executable, 'model': str(model)})
    print(str(task))


if __name__ == '__main__':
    os.umask(0o077)
    main()
