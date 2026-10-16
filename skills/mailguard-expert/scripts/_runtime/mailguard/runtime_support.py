"""Stdlib-only, workspace-local Python runtime provisioning."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import venv

from .onboarding import private_directory, private_json
from .failures import SetupError


def python_in(directory):
    return Path(directory)/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def probe(interpreter, imports='import sys; assert sys.version_info >= (3, 12)'):
    try:
        return subprocess.run([str(interpreter), '-c', imports], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def repair_local_library(runtime, failed_probe):
    # Some app-managed macOS builds use @executable_path/../lib. Only link this
    # running interpreter's library into our venv; never alter the host runtime.
    if sys.platform != 'darwin' or failed_probe is None:
        return False
    if 'Library not loaded' not in failed_probe.stderr or 'libpython' not in failed_probe.stderr:
        return False
    name = sysconfig.get_config_var('LDLIBRARY') or ''
    library_dir = sysconfig.get_config_var('LIBDIR')
    if Path(name).name != name or not name.startswith('libpython') or not name.endswith('.dylib'):
        return False
    # Relocated standalone builds can retain a build-time LIBDIR such as /install/lib.
    candidates = [Path(sys.base_prefix)/'lib'/name]
    if library_dir:
        candidates.append(Path(library_dir)/name)
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        return False
    target = private_directory(Path(runtime)/'lib')/name
    if target.exists() or target.is_symlink():
        return False
    target.symlink_to(source.resolve())
    return True


def run_checked(argv, stage, *, cwd=None, code='installation_error'):
    try:
        result = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=300)
    except subprocess.TimeoutExpired:
        raise SetupError(f'{stage}超过5分钟，已停止本次子进程。', 'installation_timeout') from None
    if result.returncode:
        # Package-manager diagnostics can contain authenticated index/proxy URLs.
        raise SetupError(f'{stage}失败（退出码{result.returncode}）；请检查Python版本、网络及目录权限。', code)
    return result.stdout


def ensure_runtime(runtime, requirements, *, install_spec=None, extra_imports=()):
    if sys.version_info < (3, 12):
        raise SetupError('需要Python3.12或更新版本。', 'python_unsupported')
    runtime = Path(runtime)
    interpreter = python_in(runtime)
    if not interpreter.is_file():
        if runtime.exists() and any(runtime.iterdir()) and not (runtime/'pyvenv.cfg').is_file():
            raise RuntimeError('运行环境目录已有非venv内容，请选择新的工作区。')
        private_directory(runtime.parent)
        venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(runtime)
    checked = probe(interpreter)
    repaired = repair_local_library(runtime, checked) if checked is not None and checked.returncode else False
    if repaired:
        checked = probe(interpreter)
    if checked is None or checked.returncode:
        raise SetupError('虚拟环境Python不可执行；请使用可用的Python3.12+重试，保持宿主沙箱设置。', 'runtime_unavailable')
    modules = ['imap_tools', 'pydantic', 'dotenv', *extra_imports]
    imports = 'import '+', '.join(modules)
    marker = runtime/'mailguard-runtime.json'
    identity = hashlib.sha256(Path(requirements).read_bytes()+str(install_spec or '').encode()).hexdigest()
    saved = json.loads(marker.read_text()) if marker.is_file() else {}
    ready = probe(interpreter, imports)
    if saved.get('identity') != identity or ready is None or ready.returncode:
        pip = probe(interpreter, 'import pip')
        if pip is None or pip.returncode:
            run_checked([interpreter, '-m', 'ensurepip', '--upgrade'], 'pip初始化', code='dependency_install_failed')
        args = [interpreter, '-m', 'pip', 'install', '--disable-pip-version-check', '-q']
        args += ['-e', install_spec] if install_spec else ['-r', requirements]
        run_checked(args, '依赖安装', code='dependency_install_failed')
        ready = probe(interpreter, imports)
        if ready is None or ready.returncode:
            raise RuntimeError('依赖导入自检失败，请检查运行环境。')
        private_json(marker, {'identity': identity})
    return interpreter, repaired


def core_command(interpreter, source, env_file, state, args):
    # sitecustomize and the original PYTHONPATH remain active. The explicit path
    # selects only this distribution's mailguard package after normal startup.
    code = "import runpy,sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('mailguard.expert_cli',run_name='__main__')"
    return [str(interpreter), '-c', code, str(source), '--env-file', str(env_file),
            '--state-dir', str(state), *[str(x) for x in args]]
