"""Run on the target OS: python packaging/build.py. Never bundle user archives."""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
name = "LiveJournalArchive"
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", name,
           "--collect-data", "ljarchive", "--distpath", str(root / "dist"),
           "--workpath", str(root / "build"), "--specpath", str(root / "build"),
           "--paths", str(root), "--noupx"]
if sys.platform == "darwin":
    command += ["--onedir", "--windowed", "--osx-bundle-identifier", "org.livejournalarchive.desktop"]
else:
    command += ["--onefile"]
    if sys.platform == "win32":
        command += ["--windowed"]
command.append(str(root / "packaging" / "desktop_entry.py"))
subprocess.run(command, cwd=root, check=True)
release = root / "release"
release.mkdir(exist_ok=True)
from ljarchive import __version__
architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
system = "macos" if sys.platform == "darwin" else platform.system().lower()
label = f"JournalArchive-{__version__}-{system}-{architecture}"
if sys.platform == "darwin":
    output = release / (label + ".zip")
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(root / "dist" / (name + ".app")), str(output)], check=True)
elif sys.platform == "win32":
    output = release / (label + ".exe")
    shutil.copy2(root / "dist" / (name + ".exe"), output)
else:
    output = release / (label + ".tar.gz")
    with tarfile.open(output, "w:gz") as archive:
        archive.add(root / "dist" / name, arcname=name)
        archive.add(root / "README.md", arcname="README.md")
checksum = hashlib.sha256(output.read_bytes()).hexdigest()
(release / (output.name + ".sha256")).write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
print(json.dumps({"artifact": str(output), "sha256": checksum}))
