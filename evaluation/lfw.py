"""LFW "View 2" verification protocol: 10 folds of 300 genuine and 300 impostor pairs (pairs.txt).

``python -m evaluation.lfw`` downloads the official files from the scikit-learn figshare mirror (the
original UMass site was unreachable when this was written), verifies their SHA-256 and extracts the
images to ``evaluation/datasets/lfw`` which is ignored by Git. Images are never committed.
"""

from __future__ import annotations

import argparse
import hashlib
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PAIRS_URL = "https://ndownloader.figshare.com/files/5976006"
PAIRS_SHA256 = "ea42330c62c92989f9d7c03237ed5d591365e89b3e649747777b70e692dc1592"
ARCHIVE_URL = "https://ndownloader.figshare.com/files/5976018"
ARCHIVE_SHA256 = "055f7d9c632d7370e6fb4afc7468d40f970c34a80d4c6f50ffec63f5a8d536c0"
DEFAULT_ROOT = Path("evaluation/datasets")


@dataclass(frozen=True)
class Pair:
    fold: int
    first: str
    second: str
    genuine: bool


def image_path(name: str, number: int) -> str:
    return f"{name}/{name}_{number:04d}.jpg"


def parse_pairs(text: str) -> list[Pair]:
    lines = [line.split() for line in text.splitlines() if line.strip()]
    folds, per_fold = int(lines[0][0]), int(lines[0][1])
    rows = lines[1:]
    if len(rows) != folds * per_fold * 2:
        raise ValueError(f"Expected {folds * per_fold * 2} pairs, found {len(rows)}")
    pairs = []
    for index, row in enumerate(rows):
        fold = index // (per_fold * 2)
        if len(row) == 3:
            pairs.append(Pair(fold, image_path(row[0], int(row[1])), image_path(row[0], int(row[2])), True))
        elif len(row) == 4:
            pairs.append(Pair(fold, image_path(row[0], int(row[1])), image_path(row[2], int(row[3])), False))
        else:
            raise ValueError(f"Malformed pairs line {index + 2}: {row}")
    return pairs


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, target: Path, expected: str) -> None:
    if target.is_file() and sha256(target) == expected:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "BioGate-evaluation"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:  # noqa: S310
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    actual = sha256(temporary)
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"SHA-256 mismatch for {url}: {actual}")
    temporary.replace(target)


def prepare(root: Path = DEFAULT_ROOT) -> Path:
    fetch(PAIRS_URL, root / "pairs.txt", PAIRS_SHA256)
    archive = root / "lfw.tgz"
    fetch(ARCHIVE_URL, archive, ARCHIVE_SHA256)
    images = root / "lfw"
    if not images.is_dir():
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter="data")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and verify the LFW pairs protocol and images")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = prepare(args.root)
    pairs = parse_pairs((root / "pairs.txt").read_text(encoding="utf-8"))
    print(f"LFW ready in {root}: {len(pairs)} pairs, {sum(pair.genuine for pair in pairs)} genuine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
