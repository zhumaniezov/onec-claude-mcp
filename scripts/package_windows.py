"""Package the runtime, its notices, the 1C extension and basic setup files."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("dist/windows"))
    parser.add_argument("--archive", type=Path, default=Path("dist/onec-claude-mcp-v2.1.1-win-x64.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime = args.runtime.resolve()
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    files = {
        name: root / name
        for name in (
            "README.md",
            "examples/profiles.example.json",
            "extension/OneCMCP.cfe",
            "docs/verification.md",
        )
    }
    files["onec-claude-mcp.exe"] = runtime / "onec-claude-mcp.exe"
    notices = runtime / "third-party-notices"
    if not (notices / "CPython-LICENSE.txt").is_file():
        raise SystemExit("The runtime is missing third-party notices. Run build_windows.py first.")
    for file in sorted(notices.rglob("*")):
        if file.is_file():
            files[file.relative_to(runtime).as_posix()] = file
    with ZipFile(args.archive, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for name, file in files.items():
            archive.write(file, name)
    with ZipFile(args.archive) as archive:
        if archive.testzip() or set(archive.namelist()) != set(files):
            raise RuntimeError("Archive verification failed.")
    print(args.archive.resolve())


if __name__ == "__main__":
    main()
