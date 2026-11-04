import copy
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy import pull_cd as cd

OLD, NEW = 'b' * 40, 'c' * 40
PREVIOUS_IMAGE, NEXT_IMAGE = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64


def archive(files=None, *, extra=None):
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode='w:gz') as tar:
        for name, data in (files or {'bot.py': b'pass\n', 'deploy/container_entrypoint.py': b'pass\n'}).items():
            info = tarfile.TarInfo('slack-auto-mention-' + NEW + '/' + name)
            info.size = len(data); tar.addfile(info, io.BytesIO(data))
        if extra:
            tar.addfile(extra, io.BytesIO(b'x' * extra.size))
    return body.getvalue()


class FakeHTTP:
    def __init__(self):
        self.repo = {'id': cd.REPOSITORY_ID, 'full_name': cd.REPOSITORY, 'private': False, 'default_branch': 'main'}
        self.branch = {'name': 'main', 'protected': True, 'commit': {'sha': NEW}}
        self.compare = {'status': 'ahead', 'base_commit': {'sha': OLD}}
        self.runs = [{'id': 10, 'run_attempt': 1, 'workflow_id': cd.WORKFLOW_ID, 'path': cd.WORKFLOW_PATH,
                      'event': 'push', 'head_branch': 'main', 'head_sha': NEW,
                      'repository': {'id': cd.REPOSITORY_ID}, 'head_repository': {'id': cd.REPOSITORY_ID},
                      'status': 'completed', 'conclusion': 'success'}]
        self.jobs = [{'name': n, 'status': 'completed', 'conclusion': 'success'} for n in cd.JOB_NAMES]
        self.calls = []; self.archive_calls = 0

    def api(self, path):
        self.calls.append(path)
        if path == '': return copy.deepcopy(self.repo)
        if path == '/branches/main': return copy.deepcopy(self.branch)
        if path.startswith('/compare/'): return copy.deepcopy(self.compare)
        if '/attempts/' in path: return {'total_count': len(self.jobs), 'jobs': copy.deepcopy(self.jobs)}
        if '/runs?' in path: return {'total_count': len(self.runs), 'workflow_runs': copy.deepcopy(self.runs)}
        raise AssertionError('Unexpected API path')

    def archive(self, revision):
        assert revision == NEW
        self.archive_calls += 1
        return archive()


class GateTests(unittest.TestCase):
    def setUp(self): self.http = FakeHTTP(); self.gate = cd.GitHubGate(self.http)

    def test_exact_main_push_workflow_and_both_jobs_required(self):
        self.assertEqual(self.gate.verify(NEW, OLD), 10)
        self.assertTrue(any('/attempts/1/jobs?' in path for path in self.http.calls))

    def test_repository_replacement_private_or_unprotected_main_rejected(self):
        for key, value in [('id', 1), ('private', True), ('default_branch', 'other')]:
            original = dict(self.http.repo); self.http.repo[key] = value
            with self.subTest(key=key), self.assertRaises(cd.Hold): self.gate.head()
            self.http.repo = original
        self.http.branch['protected'] = False
        with self.assertRaisesRegex(cd.Hold, 'protected_main'): self.gate.head()

    def test_pr_fork_wrong_branch_workflow_path_or_revision_never_admitted(self):
        variants = [('event', 'pull_request'), ('head_branch', 'feature'), ('workflow_id', 7),
                    ('path', 'other.yml'), ('head_sha', 'd' * 40), ('head_repository', {'id': 8})]
        original = copy.deepcopy(self.http.runs)
        for key, value in variants:
            self.http.runs = copy.deepcopy(original); self.http.runs[0][key] = value
            with self.subTest(key=key), self.assertRaises(cd.Hold): self.gate.verify(NEW, OLD)

    def test_latest_pending_or_failed_attempt_blocks_earlier_success(self):
        for status, conclusion in [('in_progress', None), ('completed', 'failure')]:
            latest = dict(self.http.runs[0], id=11, run_attempt=2, status=status, conclusion=conclusion)
            self.http.runs = [dict(self.http.runs[0], id=10), latest]
            with self.subTest(status=status), self.assertRaises(cd.Hold): self.gate.verify(NEW, OLD)

    def test_missing_skipped_or_duplicate_delivery_gate_holds(self):
        original = copy.deepcopy(self.http.jobs)
        for jobs in ([], [dict(j, conclusion='skipped') for j in original], original + [original[0]]):
            self.http.jobs = jobs
            with self.subTest(jobs=jobs), self.assertRaises(cd.Hold): self.gate.verify(NEW, OLD)

    def test_downgrade_divergence_and_main_race_rejected(self):
        for status in ['behind', 'diverged', 'identical']:
            self.http.compare['status'] = status
            with self.subTest(status=status), self.assertRaises(cd.Hold): self.gate.verify(NEW, OLD)
        self.http.branch['commit']['sha'] = 'd' * 40
        with self.assertRaisesRegex(cd.Hold, 'main_changed'): self.gate.verify(NEW, OLD)

    def test_public_get_sends_no_inherited_credentials_and_refuses_other_origins(self):
        response = io.BytesIO(b'{}')
        with patch.dict(os.environ, {'GH_TOKEN': 'DUMMY', 'GITHUB_TOKEN': 'DUMMY', 'SSH_AUTH_SOCK': 'DUMMY'}, clear=True), patch('urllib.request.build_opener') as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(cd.PublicHTTP().get('https://api.github.com/repos/' + cd.REPOSITORY, 20), b'{}')
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertNotIn('Authorization', dict(request.header_items()))
        for url in ['http://api.github.com/x', 'https://user:pass' + '@' + 'api.github.com/x', 'https://other.invalid/x']:
            with self.subTest(url=url), self.assertRaises(cd.Hold): cd.PublicHTTP().get(url, 20)
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://not-used.invalid'}), self.assertRaisesRegex(cd.Hold, 'proxy_configuration'):
            cd.PublicHTTP().get('https://api.github.com/repos/' + cd.REPOSITORY, 20)


class ArchiveTests(unittest.TestCase):
    def test_only_two_application_sources_selected_not_remote_deployer(self):
        raw = archive({'bot.py': b'pass\n', 'deploy/container_entrypoint.py': b'pass\n', 'Dockerfile': b'RUN forbidden\n', 'deploy/pull_cd.py': b'raise RuntimeError()\n'})
        self.assertEqual(set(cd.application_files(raw, NEW)), set(cd.APPLICATION_FILES))

    def test_traversal_links_duplicate_aliases_missing_or_invalid_python_rejected(self):
        prefix = 'slack-auto-mention-' + NEW + '/'
        for name, kind in [(prefix + '../escape', tarfile.REGTYPE), (prefix + 'link', tarfile.SYMTYPE),
                           (prefix + './bot.py', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE)]:
            info = tarfile.TarInfo(name); info.type = kind
            if kind == tarfile.SYMTYPE: info.linkname = '/outside'
            with self.subTest(name=name), self.assertRaises(cd.Hold): cd.application_files(archive(extra=info), NEW)
        with self.assertRaises(cd.Hold): cd.application_files(archive({'bot.py': b'pass\n'}), NEW)
        with self.assertRaises(SyntaxError): cd.application_files(archive({'bot.py': b'if !', 'deploy/container_entrypoint.py': b'pass\n'}), NEW)


class FakeRuntime:
    def __init__(self):
        self.current = {'revision': OLD, 'image': PREVIOUS_IMAGE, 'container_id': 'old-container'}
        self.switches = []; self.builds = 0; self.stopped = False; self.fail_new_health = False; self.fail_rollback = False

    def snapshot(self): return dict(self.current)
    def profile_check(self): return b'unchanged-configuration'
    def dependencies(self): return {'minutes': 'unchanged'}
    def build(self, path, revision): self.builds += 1; return NEXT_IMAGE
    def switch(self, image, revision): self.switches.append((image, revision)); self.current = {'revision': revision, 'image': image, 'container_id': revision}
    def healthy(self, revision): return not (self.fail_new_health if revision == NEW else self.fail_rollback)
    def stop(self): self.stopped = True


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = cd.Settings(Path(self.temp.name), True, 'python@' + 'sha256:' + '0' * 64, '0' * 64)
        self.settings.root.mkdir(parents=True, mode=0o700)
        self.http = FakeHTTP(); self.gate = cd.GitHubGate(self.http); self.runtime = FakeRuntime()
        if os.name != 'posix':
            patcher = patch.object(cd, 'private_path'); patcher.start(); self.addCleanup(patcher.stop)

    def deliver(self, **kwargs):
        return cd.deliver(self.settings, self.gate, self.runtime, self.http, lock=lambda _: nullcontext(),
                          staging=lambda *args: self.settings.root, **kwargs)

    def test_default_plan_does_not_download_build_switch_or_write_receipt(self):
        result = self.deliver()
        self.assertEqual(result['status'], 'eligible_plan_only')
        self.assertEqual((self.http.archive_calls, self.runtime.builds, self.runtime.switches), (0, 0, []))
        self.assertFalse((self.settings.root / 'receipt.json').exists())

    def test_disabled_apply_cannot_read_network_or_change_runtime(self):
        self.settings.enabled = False
        with self.assertRaisesRegex(cd.Hold, 'apply_not_enabled'): self.deliver(apply=True)
        self.assertEqual(self.http.calls, []); self.assertEqual(self.runtime.switches, [])

    def test_success_pins_image_and_preserves_configuration_and_metadata(self):
        result = self.deliver(apply=True)
        self.assertEqual(result['status'], 'deployed')
        self.assertEqual(self.runtime.switches, [(NEXT_IMAGE, NEW)])
        receipt = json.loads((self.settings.root / 'receipt.json').read_text())
        self.assertEqual(receipt['previous_image'], PREVIOUS_IMAGE)
        self.assertFalse((self.settings.root / 'intent.json').exists())

    def test_health_failure_restores_previous_image_and_quarantines_no_retry(self):
        self.runtime.fail_new_health = True
        with self.assertRaisesRegex(cd.Hold, 'previous_image_restored_ready'): self.deliver(apply=True)
        self.assertEqual(self.runtime.switches, [(NEXT_IMAGE, NEW), (PREVIOUS_IMAGE, OLD)])
        self.assertFalse(self.runtime.stopped)
        with self.assertRaisesRegex(cd.Hold, 'quarantined'): self.deliver(apply=True)
        self.assertEqual(self.runtime.builds, 1)

    def test_rollback_failure_stops_only_bot_and_keeps_state(self):
        self.runtime.fail_new_health = self.runtime.fail_rollback = True
        with self.assertRaisesRegex(cd.Hold, 'bot_stopped_data_preserved'): self.deliver(apply=True)
        self.assertTrue(self.runtime.stopped)

    def test_build_failure_does_not_stop_running_bot(self):
        with patch.object(self.runtime, 'build', side_effect=cd.Hold('offline_build_failed')):
            with self.assertRaisesRegex(cd.Hold, 'not_needed'): self.deliver(apply=True)
        self.assertEqual(self.runtime.switches, []); self.assertFalse(self.runtime.stopped)

    def test_configuration_changed_during_build_holds_before_switch(self):
        with patch.object(self.runtime, 'profile_check', side_effect=[b'old', b'changed']):
            with self.assertRaises(cd.Hold): self.deliver(apply=True)
        self.assertEqual(self.runtime.switches, [])

    def test_crash_after_intent_never_blindly_replays_switch(self):
        with patch.object(self.runtime, 'switch', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.deliver(apply=True)
        self.assertTrue((self.settings.root / 'intent.json').exists())
        with self.assertRaisesRegex(cd.Hold, 'incomplete_delivery'): self.deliver(apply=True)
        self.assertEqual(self.runtime.builds, 1)

    def test_up_to_date_does_not_build_or_switch(self):
        self.http.branch['commit']['sha'] = OLD
        self.assertEqual(self.deliver(apply=True)['status'], 'up_to_date')
        self.assertEqual(self.runtime.builds, 0)


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'getuid'), 'Linux ownership/locking')
class LinuxSafetyTests(unittest.TestCase):
    setUp = DeliveryTests.setUp
    def test_same_owner_concurrent_delivery_lock_refuses_second_process(self):
        with cd.delivery_lock(self.settings):
            with self.assertRaisesRegex(cd.Hold, 'another_delivery'):
                with cd.delivery_lock(self.settings): pass

    def test_approved_profile_pin_and_no_credential_environment(self):
        runtime = cd.DockerRuntime(self.settings)
        for key in ['GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'DOCKER_AUTH_CONFIG']:
            self.assertNotIn(key, runtime.environment)
        self.assertEqual(runtime.environment['DOCKER_HOST'], 'unix:///var/run/docker.sock')


if __name__ == '__main__': unittest.main()
