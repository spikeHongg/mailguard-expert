"""Initialization and lightweight distribution boundaries; no live credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mailguard.bootstrap import bundle_inventory, copy_skill, skill_main
from mailguard.imap_source import FIELDS, check_connection
from mailguard.models import AnalysisError
from mailguard.onboarding import (ChoiceRequired, choose_installation,
                                 prepare_configuration, private_json, workspace_layout)
from mailguard import runtime_support

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT/'skills/mailguard-expert'


def choices(workspace, **values):
    return argparse.Namespace(workspace=workspace, mode=None, imap=None,
                              env_file=None, state_dir=None, provider=None,
                              glm_channel=None, **values)


@pytest.mark.parametrize('arguments,question', [
    ([], 'mode'), (['--mode', 'skill'], 'imap'),
    (['--mode', 'service', '--imap', 'later'], 'provider'),
    (['--mode', 'service', '--imap', 'later', '--provider', 'glm'], 'glm_channel'),
])
def test_noninteractive_questions_before_any_installation(tmp_path, arguments, question):
    workspace = tmp_path/'empty'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'),
                             '--workspace', str(workspace), *arguments], input='',
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)['question'] == question
    assert not workspace.exists()


def test_interactive_order_and_explicit_choice_not_repeated(tmp_path):
    answers = iter(['1', '2'])
    args = choose_installation(choices(tmp_path), interactive=True, input_fn=lambda _: next(answers))
    assert (args.mode, args.imap) == ('skill', 'later')
    args.mode, args.imap = 'service', 'now'
    args.provider, args.glm_channel = 'glm', 'coding'
    assert choose_installation(args, interactive=True, input_fn=lambda _: pytest.fail('repeated question')) is args


@pytest.mark.parametrize('channel,url', [
    ('coding', 'https://open.bigmodel.cn/api/coding/paas/v4'),
    ('standard', 'https://open.bigmodel.cn/api/paas/v4/'),
])
def test_configuration_is_private_preserves_data_and_uses_selected_channel(tmp_path, channel, url):
    layout = workspace_layout(tmp_path)
    prepare_configuration(layout, imap='now', mode='service', provider='glm', glm_channel=channel)
    env = layout['env_file']
    assert 'GLM_BASE_URL='+url in env.read_text()
    assert 'IMAP_HOST=\n' in env.read_text() and 'IMAP_FOLDER=\n' in env.read_text()
    env.write_text(env.read_text().replace('IMAP_PASSWORD=', 'IMAP_PASSWORD=fixture-app-password'))
    before = env.read_bytes()
    record = layout['state']/'review.json'; record.write_text('{"fixture":true}')
    prepare_configuration(layout, imap='edit', mode='service', provider='glm', glm_channel=channel)
    assert env.read_bytes() == before
    assert record.read_text() == '{"fixture":true}'
    assert (layout['private']/'.gitignore').read_text() == '*\n'
    if os.name != 'nt':
        assert env.stat().st_mode & 0o777 == 0o600
        assert layout['private'].stat().st_mode & 0o777 == 0o700


def test_later_and_reuse_old_explicit_configuration(tmp_path):
    layout = workspace_layout(tmp_path/'workspace')
    prepare_configuration(layout, imap='later', mode='skill')
    assert not any(name+'=' in layout['env_file'].read_text() for name in FIELDS)
    old_env = tmp_path/'legacy.env'; old_env.write_text('IMAP_PASSWORD=fixture-only\n')
    old_state = tmp_path/'old-state'
    private_json(layout['marker'], {'env_file': str(old_env), 'state_directory': str(old_state)})
    args = choices(tmp_path/'workspace'); args.mode = 'skill'
    with pytest.raises(ChoiceRequired) as error:
        choose_installation(args, interactive=False)
    assert error.value.payload['options'] == ['reuse', 'edit', 'later']
    args.imap = 'reuse'
    choose_installation(args, interactive=False)
    current = workspace_layout(args.workspace)
    prepare_configuration(current, imap='reuse', mode='skill')
    assert current['state'] == old_state
    assert old_env.read_text() == 'IMAP_PASSWORD=fixture-only\n'


@pytest.fixture
def mailbox_config(monkeypatch):
    for key in FIELDS:
        monkeypatch.delenv(key, raising=False)
    values = dict(IMAP_HOST='imap.example.test', IMAP_PORT='993',
                  IMAP_USERNAME='fixture@example.test', IMAP_PASSWORD='fixture-app-secret',
                  IMAP_FOLDER='INBOX')
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


class MailboxProbe:
    def __init__(self, stage=None):
        self.stage, self.calls = stage, []
        self.folder = SimpleNamespace(set=self.select)

    def factory(self, host, **kwargs):
        self.calls.append(('connect', host, kwargs))
        if self.stage == 'tls':
            raise ssl.SSLError('fixture-app-secret')
        if self.stage == 'connection':
            raise OSError('fixture-app-secret')
        return self

    def login(self, username, password, **kwargs):
        self.calls.append(('login', username, kwargs))
        if self.stage == 'auth':
            raise RuntimeError(password)
        return self

    def select(self, folder, **kwargs):
        self.calls.append(('select', folder, kwargs))
        if self.stage == 'folder':
            raise RuntimeError('fixture-app-secret')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(('logout',))

    def fetch(self, *args, **kwargs):
        pytest.fail('initial configuration must not fetch any messages')


def test_imap_check_validates_tls_auth_readonly_folder_without_fetch(mailbox_config):
    mailbox = MailboxProbe()
    result = check_connection(mailbox.factory)
    assert result['imap_verified'] is True and result['mails_fetched'] == 0
    assert result['sent_to_model'] is False
    assert [call[0] for call in mailbox.calls] == ['connect', 'login', 'select', 'logout']
    assert mailbox.calls[1][2] == {'initial_folder': None}
    assert mailbox.calls[2][2] == {'readonly': True}
    context = mailbox.calls[0][2]['ssl_context']
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert mailbox_config['IMAP_PASSWORD'] not in json.dumps(result)


@pytest.mark.parametrize('stage,code', [
    ('tls', 'imap_tls_failed'), ('connection', 'imap_connection_failed'),
    ('auth', 'imap_auth_failed'), ('folder', 'imap_folder_failed'),
])
def test_imap_check_failures_are_sanitized(mailbox_config, stage, code):
    with pytest.raises(AnalysisError) as error:
        check_connection(MailboxProbe(stage).factory)
    assert error.value.code == code
    assert 'fixture-app-secret' not in str(error.value)
    assert mailbox_config['IMAP_USERNAME'] not in str(error.value)


@pytest.mark.parametrize('key', ['IMAP_HOST', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_FOLDER'])
def test_missing_imap_field_does_not_connect(mailbox_config, monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(AnalysisError) as error:
        check_connection(lambda *a, **k: pytest.fail('must not connect'))
    assert error.value.code == 'imap_not_configured' and key in str(error.value)


def test_imap_bad_port_and_cli_check_do_not_create_mail_jobs(mailbox_config, monkeypatch, tmp_path, capsys):
    from mailguard import expert_cli
    monkeypatch.setenv('IMAP_PORT', 'not-a-port')
    assert expert_cli.main(['--state-dir', str(tmp_path/'state'), 'check-imap']) == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'invalid_config'
    assert not (tmp_path/'state').exists()


def test_bundle_matches_canonical_and_rejects_extra_runtime(tmp_path):
    manifest = bundle_inventory(SKILL)
    assert not any('labels' in name for name in manifest['files'])
    for policy in (ROOT/'policies/detection').glob('*/SKILL.md'):
        bundled = SKILL/'scripts/_runtime/mailguard/_assets/skills'/policy.parent.name/'SKILL.md'
        assert bundled.read_bytes() == policy.read_bytes()
    target = copy_skill(SKILL, tmp_path/'skill')
    (target/'scripts/_runtime/unlisted.py').write_text('print("unexpected")')
    with pytest.raises(RuntimeError, match='清单之外'):
        bundle_inventory(target)


def test_skill_update_preserves_workspace_configuration_and_records(tmp_path):
    target = copy_skill(SKILL, tmp_path/'.codebuddy/skills/mailguard-expert')
    layout = workspace_layout(tmp_path)
    prepare_configuration(layout, imap='now', mode='skill')
    layout['env_file'].write_text('IMAP_PASSWORD=fixture-private\n')
    record = layout['state']/'record.json'; record.write_text('{"fixture":"keep"}')
    (target/'SKILL.md').write_text('previous version')
    copy_skill(SKILL, target, update=True)
    assert layout['env_file'].read_text() == 'IMAP_PASSWORD=fixture-private\n'
    assert record.read_text() == '{"fixture":"keep"}'
    assert bundle_inventory(target) == bundle_inventory(SKILL)
    with pytest.raises(RuntimeError):
        workspace_layout(target)


def test_skill_update_requires_setup_and_missing_workspace_is_actionable(tmp_path, capsys):
    assert skill_main(SKILL, ['doctor']) == 2
    assert json.loads(capsys.readouterr().out)['question'] == 'workspace'
    layout = workspace_layout(tmp_path)
    private_json(layout['marker'], {'modes': {'skill': {}}, 'skill_manifest_hash': 'old-version'})
    assert skill_main(SKILL, ['--workspace', str(tmp_path), 'doctor']) == 1
    assert 'setup' in json.loads(capsys.readouterr().out)['error']


def test_local_dylib_repair_keeps_host_library_and_other_environments(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_support.sys, 'platform', 'darwin')
    libraries = tmp_path/'managed-lib'; libraries.mkdir()
    library = libraries/'libpython3.13.dylib'; library.write_bytes(b'fixture')
    config = {'LDLIBRARY': library.name, 'LIBDIR': str(libraries)}
    monkeypatch.setattr(runtime_support.sysconfig, 'get_config_var', config.get)
    failure = SimpleNamespace(stderr='Library not loaded: @rpath/libpython3.13.dylib')
    runtime = tmp_path/'workspace/.mailguard/runtime/skill'
    assert runtime_support.repair_local_library(runtime, failure)
    assert (runtime/'lib'/library.name).resolve() == library
    assert library.read_bytes() == b'fixture'
    assert not runtime_support.repair_local_library(runtime, failure)
    assert not runtime_support.repair_local_library(tmp_path/'other', SimpleNamespace(stderr='permission denied'))
    assert not (tmp_path/'other').exists()


def test_relocated_python_uses_actual_prefix_library(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_support.sys, 'platform', 'darwin')
    monkeypatch.setattr(runtime_support.sys, 'base_prefix', str(tmp_path/'python'))
    lib = tmp_path/'python/lib/libpython3.13.dylib'
    lib.parent.mkdir(parents=True); lib.write_bytes(b'fixture')
    monkeypatch.setattr(runtime_support.sysconfig, 'get_config_var',
                        {'LDLIBRARY': lib.name, 'LIBDIR': '/nonexistent-build-prefix/lib'}.get)
    failed = SimpleNamespace(stderr='Library not loaded: libpython3.13.dylib')
    assert runtime_support.repair_local_library(tmp_path/'venv', failed)
    assert (tmp_path/'venv/lib'/lib.name).resolve() == lib


def test_install_timeout_is_bounded_and_guidance_does_not_leak_output(monkeypatch):
    from mailguard.failures import SetupError, failure
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 300
        raise subprocess.TimeoutExpired(args[0], 300, output='fixture-secret')
    monkeypatch.setattr(runtime_support.subprocess, 'run', timeout)
    with pytest.raises(SetupError) as error:
        runtime_support.run_checked(['fixture'], '依赖安装')
    payload = failure(error.value.code, str(error.value))
    assert payload['error_code'] == 'installation_timeout'
    assert payload['recovery']['retryable'] and not payload['recovery']['automatic_retry']
    assert 'fixture-secret' not in json.dumps(payload)


def test_cancelled_model_task_is_recorded_as_error_and_does_not_remain_running(tmp_path):
    from mailguard.expert import Expert
    expert = Expert(tmp_path)
    job = expert.prepare(sample='m002')
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    result = expert.analyze(job['id'], 'deepseek', assessor=interrupted)
    assert result['status'] == 'error'
    assert result['result']['error_code'] == 'operation_cancelled'
    assert result['result']['recovery']['category'] == 'cancelled'
    assert 'assessment' not in result['result']


def test_service_status_does_not_follow_redirect_or_expose_token(tmp_path, monkeypatch, capsys):
    from mailguard import expert_cli
    import http.client
    token = 'fixture-local-service-token'
    (tmp_path/'service.token').write_text(token)
    calls = []
    class Connection:
        def __init__(self, host, port, **kwargs):
            calls.append(('connect', host, port))
        def request(self, method, path, **kwargs):
            calls.append(('request', method, path))
            assert kwargs['headers'] == {'Authorization': 'Bearer '+token}
        def getresponse(self):
            return SimpleNamespace(status=302, read=lambda limit: b'{}')
        def close(self):
            calls.append(('close',))
    monkeypatch.setattr(http.client, 'HTTPConnection', Connection)
    assert expert_cli.main(['--state-dir', str(tmp_path), 'service-status']) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['error_code'] == 'service_unreachable' and token not in output
    assert calls == [('connect', '127.0.0.1', 8765), ('request', 'GET', '/v1/capabilities'), ('close',)]
