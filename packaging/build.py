"""Run on the target OS: python packaging/build.py. Never bundle user archives."""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
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
label = f"{name}-{platform.system().lower()}-{platform.machine().lower()}"
if sys.platform == "darwin":
    output = release / (label + ".zip")
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(root / "dist" / (name + ".app")), str(output)], check=True)
else:
    extension = ".exe" if sys.platform == "win32" else ""
    output = release / (label + extension)
    shutil.copy2(root / "dist" / (name + extension), output)
checksum = hashlib.sha256(output.read_bytes()).hexdigest()
(release / (output.name + ".sha256")).write_text(f"{checksum}  {output.name}\n")
print(json.dumps({"artifact": str(output), "sha256": checksum}))
