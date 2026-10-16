import importlib.util
from pathlib import Path
import zipfile

import pytest


def exporter():
    path = Path(__file__).resolve().parents[1]/'scripts/export_distribution.py'
    spec = importlib.util.spec_from_file_location('mailguard_export_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distribution_excludes_internal_records_and_machine_state():
    module = exporter()
    names = {str(p.relative_to(module.ROOT)) for p in module.distribution_files()}
    assert 'INSTALL.md' in names and 'PUBLISH.md' in names
    assert 'skills/mailguard-expert/scripts/mailguard.py' in names
    assert not any(n.startswith(('.git/', '.codebuddy/', '.workbuddy/', 'reports/', 'data/private/', 'data/replays/')) for n in names)
    assert {n for n in names if n.startswith('docs/')} == {'docs/local-api.md'}
    assert '.env' not in names and not any(n.endswith('runtime.json') for n in names)
    assert not {'scripts/report_html_eval.py', 'scripts/compare_restored_run.py'} & names
    assert len([n for n in names if n.endswith('.eml')]) == 100


def test_distribution_refuses_known_secret_in_allowlisted_file(tmp_path, monkeypatch):
    module = exporter()
    readme = tmp_path/'README.md'
    secret = 'test-secret-for-export-guard'
    readme.write_text('Accidental credential: '+secret)
    (tmp_path/'.env').write_text('GLM_API_KEY='+secret+'\n')
    monkeypatch.setattr(module, 'distribution_files', lambda root: [readme])
    target = tmp_path/'output.zip'
    with pytest.raises(ValueError) as error:
        module.export(target, root=tmp_path)
    assert secret not in str(error.value) and not target.exists()


def test_token_count_is_not_a_credential(tmp_path, monkeypatch):
    module = exporter()
    readme = tmp_path/'README.md'; readme.write_text('GLM_MAX_TOKENS=4500')
    (tmp_path/'.env').write_text('GLM_MAX_TOKENS=4500\n')
    monkeypatch.setattr(module, 'distribution_files', lambda root: [readme])
    target = tmp_path/'output.zip'
    assert module.export(target, root=tmp_path) == 1
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ['mailguard/README.md']
