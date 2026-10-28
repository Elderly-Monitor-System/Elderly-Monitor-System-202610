"""下载固定版本 SisFall 镜像并校验 SHA-256"""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/datasets/Algo-rythmic/Sisfall_Dataset/resolve/c2956ad/SisFall_dataset.zip?download=true"
SHA256 = "7f0dd1583e39fee0f402a6855976eb12517f7eaecdacce871d5b3421793ad665"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", default=".build/ml-data/SisFall_dataset.zip")
    args = parser.parse_args()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        partial = path.with_suffix(".zip.part")
        with urllib.request.urlopen(URL, timeout=60) as response, partial.open("xb") as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        with partial.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != SHA256:
            raise ValueError("download checksum mismatch; partial file retained for inspection")
        partial.rename(path)
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    if digest != SHA256:
        raise ValueError("existing dataset checksum mismatch")
    print(json.dumps({"path": str(path), "sha256": digest, "source": URL}))


if __name__ == "__main__":
    main()
