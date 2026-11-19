"""Build the Windows x64 executable and copy distribution notices from the build environment."""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def copy_notices(destination):
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", destination / "CPython-LICENSE.txt")
    for package in metadata.distributions():
        name = package.metadata["Name"]
        for file in package.files or ():
            path = file.as_posix()
            if ".dist-info/" not in path:
                continue
            relative = path.split(".dist-info/", 1)[1]
            if not any(word in relative.lower() for word in ("license", "notice", "copying", "authors")):
                continue
            source = Path(package.locate_file(file))
            if source.is_file():
                target = destination / (name + "-" + package.version) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/windows"))
    parser.add_argument("--work", type=Path, default=Path("build/windows"))
    args = parser.parse_args()
    if (
        sys.platform != "win32"
        or platform.machine().lower() not in {"amd64", "x86_64"}
        or sys.maxsize < 2**32
    ):
        parser.error("Use 64-bit CPython on Windows x64")
    root = Path(__file__).resolve().parents[1]
    out, work = args.output.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--console",
            "--name",
            "onec-claude-mcp",
            "--paths",
            str(root),
            "--distpath",
            str(out),
            "--workpath",
            str(work / "analysis"),
            "--specpath",
            str(work),
            "--copy-metadata",
            "mcp",
            "--collect-data",
            "jsonschema_specifications",
            str(root / "scripts" / "launcher.py"),
        ],
        env=env,
        check=True,
    )
    copy_notices(out / "third-party-notices")


if __name__ == "__main__":
    main()
