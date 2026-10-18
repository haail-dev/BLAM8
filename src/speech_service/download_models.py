"""Explicit provisioning step; the running service never downloads models."""
import argparse
import hashlib
from pathlib import Path
import urllib.request


ASSETS = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="models")
    parser.add_argument("--whisper-model", default="base.en")
    args = parser.parse_args()
    directory = Path(args.model_dir)
    (directory / "kokoro").mkdir(parents=True, exist_ok=True)
    from faster_whisper.utils import download_model
    download_model(args.whisper_model, cache_dir=str(directory))
    for name, expected in ASSETS.items():
        destination = directory / "kokoro" / name
        if destination.exists():
            if digest(destination) != expected:
                raise RuntimeError(f"Existing {name} has an unexpected checksum; preserved without overwriting")
            continue
        temporary = destination.with_suffix(destination.suffix + ".download")
        with urllib.request.urlopen(BASE + name, timeout=120) as response, temporary.open("wb") as file:
            while chunk := response.read(1024 * 1024):
                file.write(chunk)
        if digest(temporary) != expected:
            raise RuntimeError(f"Downloaded {name} has an unexpected checksum")
        temporary.replace(destination)
    print("Speech models provisioned and Kokoro checksums verified.")


if __name__ == "__main__":
    main()
