"""Install a complete, validated managed venv; never overlay old distributions.

Called by postinst while capture/web are quiesced. The previous venv is retained
outside the managed directory for recovery, including any local additions.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def validate(path):
    site = next((path / 'lib').glob('python*/site-packages'))
    # Exclude host dist-packages/.pth: check the real bundled requirements only.
    code = "import sys,runpy;sys.path.insert(0,sys.argv[1]);sys.argv=['pip','check'];runpy.run_module('pip',run_name='__main__')"
    subprocess.run([str(path / 'bin/python3'), '-I', '-S', '-c', code, str(site)], check=True)
    code = "import sys;sys.path.insert(0,sys.argv[1]);import flask,sqlalchemy,PyIndi,setuptools;from importlib.metadata import version;assert version('pyindi-client')=='2.3.0';print('Validated PyIndi',version('pyindi-client'))"
    subprocess.run([str(path / 'bin/python3'), '-I', '-S', '-c', code, str(site)], check=True)


def install(bundle, target):
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise RuntimeError('Managed venv must be a real directory')
    # No hash shortcut: an older overlay may already have written that hash.
    staged = Path(tempfile.mkdtemp(prefix='.venv-new-', dir=target.parent))
    try:
        subprocess.run(['tar', '--zstd', '-xf', str(bundle), '-C', str(staged), '--no-same-owner'], check=True)
        validate(staged)
        with bundle.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        (staged / '.installed_hash').write_text(digest + '\n')
        previous = target.with_name('venv.previous-' + str(time.time_ns()))
        if target.exists():
            target.rename(previous)
        try:
            staged.rename(target)
        except BaseException:
            if previous.exists():
                previous.rename(target)
            raise
        print('Installed complete managed venv; previous environment retained at', previous)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


if __name__ == '__main__':
    install(Path(sys.argv[1]), Path(sys.argv[2]))
