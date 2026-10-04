"""Install isolated CPU Korean OCR and download SHA256-pinned official models.

Run once with network: python scripts/setup_ocr.py
For a container with requirements-ocr.txt already installed: add --skip-install.
Subsequent OCR calls are offline and never download while processing a document.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / '.local' / 'ocr'
BASE = 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/'
MODELS = [
    ('det/ch_PP-OCRv5_det_mobile.onnx', '4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae'),
    ('cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx', '54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7'),
    ('rec/korean_PP-OCRv5_rec_mobile.onnx', 'cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b'),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-install', action='store_true')
    args = parser.parse_args()
    if not args.skip_install:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', str(DEST / 'python'),
                        '-r', str(ROOT / 'requirements-ocr.txt'), '--disable-pip-version-check'], check=True)
    folder = DEST / 'models'
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for relative, expected in MODELS:
        target = folder / Path(relative).name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(f'Verified {target.name}')
        else:
            print(f'Downloading {target.name}', flush=True)
            request = urllib.request.Request(BASE + relative, headers={'User-Agent': 'debtoff-ocr-setup/1.0'})
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read(80_000_001)
            if len(data) > 80_000_000 or hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError(f'Model size/hash validation failed: {target.name}')
            target.write_bytes(data)
        manifest.append({'name': target.name, 'url': BASE + relative, 'sha256': expected, 'bytes': target.stat().st_size})
    (folder / 'manifest.json').write_text(json.dumps({'engine': 'rapidocr-3.9.2', 'language': 'ko',
                                                    'models': manifest}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'ready', 'models': len(manifest), 'model_bytes': sum(m['bytes'] for m in manifest)}))


if __name__ == '__main__':
    main()
