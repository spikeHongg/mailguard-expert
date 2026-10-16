"""Wheel assets are an explicit allowlist; never collect local data or secrets."""
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py

POLICIES = (
    'mail-triage-core', 'mail-identity-access', 'mail-payment-bec',
    'mail-link-attachment', 'mail-data-exfil', 'mail-remote-extortion',
    'mail-prompt-injection', 'mail-false-positive-review', 'mail-html-forms',
)

class Build(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        dest = Path(self.build_lib) / 'mailguard/_assets'
        if dest.exists():
            shutil.rmtree(dest)
        files = [(f'policies/detection/{name}/SKILL.md', f'skills/{name}/SKILL.md') for name in POLICIES]
        files += [('policies/expert-guidance.txt', 'guidance.txt')]
        files += [(f'data/testbench/{name}.eml', f'samples/{name}.eml') for name in ('m002', 'm026', 'm031')]
        for source, target in files:
            path = dest / target
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / source, path)

setup(cmdclass={'build_py': Build})
