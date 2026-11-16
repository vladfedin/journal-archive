"""Pinned Deno runtime for packaged YouTube extraction; no runtime installer."""
import hashlib
import io
import os
import platform
import urllib.request
import zipfile
from pathlib import Path

VERSION = '2.9.8'
DIGESTS = {
    'x86_64-unknown-linux-gnu': 'e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609',
    'x86_64-pc-windows-msvc': 'cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf',
    'x86_64-apple-darwin': '4b2f5edd2639b7470f034238b321d7002d8dfb4726d8558f52c17afac370eb2c',
    'aarch64-apple-darwin': '9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e',
}


def prepare(root):
    arch = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine().lower())
    target = arch + {'Windows': '-pc-windows-msvc', 'Darwin': '-apple-darwin', 'Linux': '-unknown-linux-gnu'}[platform.system()]
    folder = root / 'build' / 'video-runtime'
    folder.mkdir(parents=True, exist_ok=True)
    package = folder / ('deno-' + target + '.zip')
    if not package.exists() or hashlib.sha256(package.read_bytes()).hexdigest() != DIGESTS[target]:
        url = f'https://github.com/denoland/deno/releases/download/v{VERSION}/deno-{target}.zip'
        with urllib.request.urlopen(url, timeout=120) as response: data = response.read()
        if hashlib.sha256(data).hexdigest() != DIGESTS[target]:
            raise RuntimeError('Deno package checksum mismatch')
        package.write_bytes(data)
    binary = folder / ('deno.exe' if os.name == 'nt' else 'deno')
    with zipfile.ZipFile(package) as archive:
        binary.write_bytes(archive.read(binary.name))
    binary.chmod(0o755)
    return binary
