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
    def test_failed_health_restores_and_checks_previous_revision(self):
        release, previous = Path('/releases/new'), Path('/releases/previous')
        with patch('deploy.deploy.switch') as switch, patch('deploy.deploy.restart') as restart, patch('deploy.deploy.healthy', side_effect=[False, True]) as healthy:
            with self.assertRaisesRegex(SystemExit, 'previous revision restored and healthy'):
                activate(release, previous)
            self.assertEqual(switch.call_args_list, [call(release), call(previous)])
            self.assertEqual(restart.call_count, 2)
            self.assertEqual(healthy.call_args_list, [call('new'), call('previous')])

    def test_first_deploy_failure_stops_service(self):
        with patch('deploy.deploy.switch'), patch('deploy.deploy.restart'), patch('deploy.deploy.healthy', return_value=False), patch('deploy.deploy.subprocess.run') as run:
            with self.assertRaisesRegex(SystemExit, 'Initial deployment failed'):
                activate(Path('/releases/new'), None)
            self.assertEqual(run.call_args.args[0], ['systemctl', 'stop', 'slack-auto-mention.service'])

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
