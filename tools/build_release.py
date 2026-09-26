"""Build a single named entry point with embedded, reproducible source identity."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_version import APP_VERSION


def source_fingerprint(root):
    paths = list(root.glob("*.py"))
    for folder in ("vcs", "templates", "assets"):
        paths.extend(p for p in (root / folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts)
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def git_output(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=10)
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    (ROOT / ".tmp").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="release_build_", dir=ROOT / ".tmp"))
    digest = source_fingerprint(ROOT)
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    info = dict(version=APP_VERSION, build_id=digest[:10], built_at=built_at,
                commit=git_output("rev-parse", "HEAD"),
                dirty=bool(git_output("status", "--porcelain")), source_sha256=digest)
    metadata = work / "build_info.json"
    metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--onefile", "--console",
               "--name", "CompareTool", "--noconfirm", "--clean",
               "--specpath", str(work), "--workpath", str(work / "build"),
               "--distpath", str(work / "binary"),
               "--icon", str(ROOT / "assets/icons/app.ico")]
    for source, target in ((ROOT / "templates", "templates"),
                           (ROOT / "assets", "assets"), (metadata, ".")):
        command.extend(["--add-data", str(source) + os.pathsep + target])
    command.append(str(ROOT / "main.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    if source_fingerprint(ROOT) != digest:
        raise RuntimeError("Source changed during build; no release was installed.")
    candidate = work / "binary/CompareTool.exe"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "CompareTool.exe"
    if target.exists():
        archive = output / "archive" / (stamp + "_" + work.name)
        archive.mkdir(parents=True)
        shutil.copy2(target, archive / target.name)
        prior = output / "CompareTool_build.json"
        if prior.exists():
            shutil.copy2(prior, archive / prior.name)
    fd, staged = tempfile.mkstemp(prefix=".CompareTool_", suffix=".exe", dir=output)
    os.close(fd)
    try:
        shutil.copy2(candidate, staged)
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    info["exe_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "CompareTool_build.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(target), **info), ensure_ascii=False), flush=True)
    print("Build finished. Launch the new EXE and verify its window before delivery.")


if __name__ == "__main__":
    main()
