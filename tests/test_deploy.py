import io
import tarfile
import unittest
from deploy.deploy import verify_archive
from deploy.deploy import activate
from pathlib import Path
from unittest.mock import patch, call

REVISION = 'a' * 40


def archive(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as tar:
        for name, content, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    data.seek(0)
    return data


class DeployTests(unittest.TestCase):
    @patch('deploy.deploy.legacy_is_static', return_value=True)
    def test_failed_health_restores_and_checks_previous_revision(self, _static):
        release, previous = Path('/releases/new'), Path('/releases/previous')
        with patch('deploy.deploy.switch') as switch, patch('deploy.deploy.restart') as restart, patch('deploy.deploy.healthy', side_effect=[False, True]) as healthy:
            with self.assertRaisesRegex(SystemExit, 'previous revision restored and healthy'):
                activate(release, previous)
            self.assertEqual(switch.call_args_list, [call(release), call(previous)])
            self.assertEqual(restart.call_count, 2)
            self.assertEqual(healthy.call_args_list, [call('new'), call('previous')])

    @patch('deploy.deploy.legacy_is_static', return_value=True)
    def test_first_deploy_failure_stops_service(self, _static):
        with patch('deploy.deploy.switch'), patch('deploy.deploy.restart'), patch('deploy.deploy.healthy', return_value=False), patch('deploy.deploy.subprocess.run') as run:
            with self.assertRaisesRegex(SystemExit, 'Initial deployment failed'):
                activate(Path('/releases/new'), None)
            self.assertEqual(run.call_args.args[0], ['systemctl', 'stop', 'slack-auto-mention.service'])

    def test_fresh_legacy_configuration_refused_before_switch(self):
        with patch('deploy.deploy.legacy_is_static',return_value=False),patch('deploy.deploy.switch') as switch,patch('deploy.deploy.restart') as restart:
            with self.assertRaisesRegex(SystemExit,'Legacy deployment refused'):activate(Path('/releases/new'),Path('/releases/old'))
        switch.assert_not_called();restart.assert_not_called()

    def test_fresh_configuration_during_failure_stops_instead_of_old_restart(self):
        with patch('deploy.deploy.legacy_is_static',side_effect=[True,False]),patch('deploy.deploy.switch') as switch,patch('deploy.deploy.restart') as restart,patch('deploy.deploy.healthy',return_value=False),patch('deploy.deploy.subprocess.run') as run:
            with self.assertRaisesRegex(SystemExit,'Fresh roster rollback refused'):activate(Path('/releases/new'),Path('/releases/old'))
        self.assertEqual(switch.call_args_list,[call(Path('/releases/new'))]);self.assertEqual(restart.call_count,1)
        self.assertEqual(run.call_args.args[0],['systemctl','stop','slack-auto-mention.service'])

    def test_legacy_env_guard_static_fresh_and_unverifiable(self):
        import tempfile
        from deploy.deploy import legacy_is_static
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'env';self.assertFalse(legacy_is_static(path))
            for body,expected in [('OTHER=fixture\n',True),('SLACK_ROSTER_MODE="static"\n',True),
                                  ('SLACK_ROSTER_MODE="fresh_sheet"\n',False),('SLACK_ROSTER_SOURCE_ID="fixture-sheet"\n',False)]:
                path.write_text(body);self.assertEqual(legacy_is_static(path),expected)

    def test_valid_release(self):
        data = archive([('bot.py', b'print(1)\n', tarfile.REGTYPE),
                        ('revision.env', f'APP_REVISION={REVISION}\n'.encode(), tarfile.REGTYPE)])
        self.assertEqual(verify_archive(data, REVISION)['bot.py'], b'print(1)\n')

    def test_reject_unexpected_paths_links_revision_or_code(self):
        valid = [('bot.py', b'print(1)\n', tarfile.REGTYPE),
                 ('revision.env', f'APP_REVISION={REVISION}\n'.encode(), tarfile.REGTYPE)]
        cases = [valid + [('../escape', b'bad', tarfile.REGTYPE)],
                 [('../bot.py', b'bad', tarfile.REGTYPE), valid[1]],
                 [('bot.py', b'', tarfile.SYMTYPE), valid[1]],
                 [valid[0], ('revision.env', b'APP_REVISION=wrong\n', tarfile.REGTYPE)],
                 [('bot.py', b'invalid python syntax!', tarfile.REGTYPE), valid[1]],
                 [valid[0], valid[0]]]
        for entries in cases:
            with self.subTest(names=[x[0] for x in entries]):
                with self.assertRaises((ValueError, SyntaxError)):
                    verify_archive(archive(entries), REVISION)
