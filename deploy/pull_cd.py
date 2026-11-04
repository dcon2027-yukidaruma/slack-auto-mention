#!/usr/bin/env python3
"""Opt-in VM pull delivery. No credentials, runner, network changes or self-update."""
import argparse
from contextlib import contextmanager
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

REPOSITORY = 'dcon2027-yukidaruma/slack-auto-mention'
REPOSITORY_ID = 1436489838
WORKFLOW_ID = 398926208
WORKFLOW_PATH = '.github/workflows/ci-cd.yml'
JOB_NAMES = {'test', 'container-delivery-ready'}
SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
NETWORK = 'dcon-slack-ingress'
CONTAINER = 'slack-auto-mention-bot-1'
APPLICATION_FILES = ('bot.py', 'deploy/container_entrypoint.py')
DOCKERFILE = '''ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG APP_REVISION
ENV APP_REVISION=${APP_REVISION}
LABEL org.opencontainers.image.revision=${APP_REVISION}
WORKDIR /app
COPY bot.py /app/bot.py
COPY deploy/container_entrypoint.py /app/container_entrypoint.py
RUN chmod 0444 /app/bot.py /app/container_entrypoint.py
USER 65532:65532
ENTRYPOINT ["python3", "-B", "-u", "/app/container_entrypoint.py"]
'''


class Hold(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Hold(reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PublicHTTP:
    """Only public, unauthenticated HTTPS GETs to fixed GitHub origins."""
    def get(self, url, limit):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'https' and parsed.hostname in {'api.github.com', 'codeload.github.com'}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password, 'unexpected_source_origin')
        require(not any(value for key, value in os.environ.items() if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}),
                'inherited_proxy_configuration_requires_operator_review')
        request = urllib.request.Request(url, method='GET', headers={
            'User-Agent': 'SlackAutoMentionDelivery/1', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=20) as response:
                data = response.read(limit + 1)
        except Exception:
            raise Hold('public_github_read_failed_no_credential_fallback') from None
        require(len(data) <= limit, 'source_response_too_large')
        return data

    def api(self, path):
        return json.loads(self.get('https://api.github.com/repos/' + REPOSITORY + path, 2 * 1024 * 1024))

    def archive(self, revision):
        require(bool(SHA.fullmatch(revision)), 'invalid_revision')
        return self.get('https://codeload.github.com/' + REPOSITORY + '/tar.gz/' + revision, 2 * 1024 * 1024)


class GitHubGate:
    def __init__(self, http):
        self.http = http

    def head(self):
        repo = self.http.api('')
        require(repo.get('id') == REPOSITORY_ID and repo.get('full_name') == REPOSITORY
                and repo.get('private') is False and repo.get('default_branch') == 'main', 'repository_identity_changed')
        branch = self.http.api('/branches/main')
        require(branch.get('name') == 'main' and branch.get('protected') is True, 'protected_main_required')
        revision = branch.get('commit', {}).get('sha', '')
        require(isinstance(revision, str) and bool(SHA.fullmatch(revision)), 'invalid_main_revision')
        return revision

    def verify(self, revision, current):
        require(self.head() == revision, 'main_changed_during_delivery')
        require(bool(SHA.fullmatch(current)), 'invalid_current_revision')
        comparison = self.http.api('/compare/' + current + '...' + revision)
        require(comparison.get('status') == 'ahead' and comparison.get('base_commit', {}).get('sha') == current,
                'non_forward_main_update_held')
        query = urllib.parse.urlencode({'branch': 'main', 'event': 'push', 'head_sha': revision, 'per_page': 10})
        result = self.http.api('/actions/workflows/' + str(WORKFLOW_ID) + '/runs?' + query)
        runs = result.get('workflow_runs', [])
        require(isinstance(runs, list) and 0 < len(runs) <= 10 and result.get('total_count', 11) <= 10, 'ci_runs_missing_or_ambiguous')
        require(all(isinstance(run.get('id'), int) for run in runs), 'invalid_ci_run_identity')
        run = max(runs, key=lambda value: value['id'])
        require(run.get('workflow_id') == WORKFLOW_ID and run.get('path') == WORKFLOW_PATH
                and run.get('event') == 'push' and run.get('head_branch') == 'main'
                and run.get('head_sha') == revision and run.get('repository', {}).get('id') == REPOSITORY_ID
                and run.get('head_repository', {}).get('id') == REPOSITORY_ID,
                'untrusted_ci_source')
        require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'latest_ci_not_successful')
        attempt = run.get('run_attempt')
        require(isinstance(attempt, int) and attempt > 0, 'invalid_ci_attempt')
        jobs = self.http.api('/actions/runs/' + str(run['id']) + '/attempts/' + str(attempt) + '/jobs?per_page=100')
        records = jobs.get('jobs', [])
        require(isinstance(records, list) and jobs.get('total_count', 101) == len(records) <= 100, 'ci_jobs_incomplete')
        for name in JOB_NAMES:
            matches = [job for job in records if job.get('name') == name]
            require(len(matches) == 1 and matches[0].get('status') == 'completed'
                    and matches[0].get('conclusion') == 'success', 'required_delivery_job_not_successful')
        return run['id']


def application_files(raw, revision):
    require(len(raw) <= 2 * 1024 * 1024 and bool(SHA.fullmatch(revision)), 'invalid_source_archive')
    prefix = 'slack-auto-mention-' + revision
    files, names, size = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for count, member in enumerate(archive, 1):
            path = PurePosixPath(member.name)
            require(count <= 256 and not path.is_absolute() and '..' not in path.parts and '\\' not in member.name
                    and path.parts and path.parts[0] == prefix and str(path) not in names, 'unsafe_archive_path')
            names.add(str(path))
            require(member.isdir() or member.isfile(), 'archive_links_or_special_files_rejected')
            size += member.size
            require(0 <= member.size <= 1024 * 1024 and size <= 8 * 1024 * 1024, 'archive_expansion_too_large')
            relative = '/'.join(path.parts[1:])
            if relative in APPLICATION_FILES:
                require(member.isfile(), 'application_source_not_regular')
                with archive.extractfile(member) as stream:
                    files[relative] = stream.read()
                compile(files[relative].decode('utf-8'), relative, 'exec')
    require(set(files) == set(APPLICATION_FILES), 'application_source_missing')
    # Downloaded Dockerfiles, Compose files, workflows and deploy scripts are never executed.
    return files


def private_path(path, directory=False):
    require(not path.is_symlink() and path.resolve(strict=True) == path.absolute(), 'private_path_symlink')
    metadata = path.stat()
    require(metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == (0o700 if directory else 0o600)
            and (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)), 'private_path_owner_or_mode')


@dataclasses.dataclass
class Settings:
    home: Path
    enabled: bool
    python_base: str
    profile_sha256: str

    @property
    def root(self):
        return self.home / '.local/share/slack-auto-mention/cd'

    @property
    def profile(self):
        return self.root / 'approved/compose.ingress.yml'

    @property
    def runtime(self):
        return self.home / '.config/slack-auto-mention/runtime.env'

    @property
    def state(self):
        return self.home / '.local/state/slack-auto-mention'

    @classmethod
    def load(cls, path):
        private_path(path)
        data = json.loads(path.read_text())
        require(set(data) == {'enabled', 'python_base', 'profile_sha256'} and type(data['enabled']) is bool, 'invalid_delivery_settings')
        require(isinstance(data['python_base'], str) and re.fullmatch(r'python@sha256:[a-f0-9]{64}', data['python_base'])
                and isinstance(data['profile_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', data['profile_sha256']), 'invalid_delivery_pins')
        return cls(Path.home(), **data)


@contextmanager
def delivery_lock(settings):
    import fcntl
    private_path(settings.root, directory=True)
    fd = os.open(settings.root / 'deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(os.fstat(fd).st_uid == os.getuid() and stat.S_IMODE(os.fstat(fd).st_mode) == 0o600, 'unsafe_delivery_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('another_delivery_is_running') from None
        yield
    finally:
        os.close(fd)


class DockerRuntime:
    def __init__(self, settings):
        self.settings = settings
        # No inherited tokens, registry credential config, SSH agent or remote Docker context.
        self.environment = {'PATH': os.defpath, 'HOME': str(settings.home),
                            'DOCKER_HOST': 'unix:///var/run/docker.sock',
                            'DOCKER_CONFIG': str(settings.root / 'empty-docker-config')}

    def run(self, args, timeout=60, environment=None):
        try:
            result = subprocess.run(args, env=environment or self.environment, capture_output=True, text=True, timeout=timeout)
        except Exception:
            raise Hold('fixed_docker_operation_failed') from None
        require(result.returncode == 0, 'fixed_docker_operation_failed')
        return result.stdout

    def inspect(self, container, field):
        return json.loads(self.run(['docker', 'inspect', '--format', '{{json ' + field + '}}', container]))

    def profile_check(self):
        private_path(self.settings.root / 'empty-docker-config', directory=True)
        private_path(self.settings.profile)
        require(hashlib.sha256(self.settings.profile.read_bytes()).hexdigest() == self.settings.profile_sha256, 'approved_container_profile_changed')
        private_path(self.settings.runtime)
        private_path(self.settings.runtime.parent, directory=True)
        private_path(self.settings.state, directory=True)
        network = json.loads(self.run(['docker', 'network', 'inspect', NETWORK, '--format', '{{json .}}']))
        require(network.get('Driver') == 'bridge' and network.get('Labels', {}).get('app') == 'slack-auto-mention', 'approved_network_missing_or_changed')
        require(NETWORK in self.inspect('dcon-minutes-tunnel-1', '.NetworkSettings.Networks'), 'approved_tunnel_attachment_missing_no_automatic_network_change')
        return hashlib.sha256(self.settings.runtime.read_bytes()).digest()

    def snapshot(self):
        self.profile_check()
        containers = [json.loads(line) for line in self.run(['docker', 'ps', '--filter', 'label=com.docker.compose.project=slack-auto-mention', '--format', '{{json .}}']).splitlines()]
        require(len(containers) == 1 and containers[0].get('Names') == CONTAINER, 'single_running_bot_required')
        state = self.inspect(CONTAINER, '.State')
        require(state.get('Running') and state.get('Health', {}).get('Status') == 'healthy', 'current_bot_not_healthy')
        require(self.inspect(CONTAINER, '.Config.User') == str(os.getuid()) + ':' + str(os.getgid())
                and self.inspect(CONTAINER, '.HostConfig.ReadonlyRootfs') is True
                and self.inspect(CONTAINER, '.HostConfig.Privileged') is False
                and 'ALL' in self.inspect(CONTAINER, '.HostConfig.CapDrop'), 'container_privilege_boundary_changed')
        require(not self.inspect(CONTAINER, '.HostConfig.PortBindings') and set(self.inspect(CONTAINER, '.NetworkSettings.Networks')) == {NETWORK}, 'container_isolation_changed')
        image = self.inspect(CONTAINER, '.Image')
        require(bool(DIGEST.fullmatch(image)), 'invalid_current_image')
        labels = self.inspect(CONTAINER, '.Config.Labels')
        require(labels.get('com.docker.compose.service') == 'bot', 'unexpected_container_service')
        revision = labels.get('org.opencontainers.image.revision')
        if not revision:
            reference = self.inspect(CONTAINER, '.Config.Image')
            revision = reference.removeprefix('slack-auto-mention:')
        require(isinstance(revision, str) and bool(SHA.fullmatch(revision)), 'current_revision_not_recognized')
        return {'revision': revision, 'image': image, 'container_id': self.inspect(CONTAINER, '.Id')}

    def dependencies(self):
        result = {}
        for name in ('dcon-minutes-tunnel-1', 'dcon-minutes-portal-1'):
            state = self.inspect(name, '.State')
            require(state.get('Running') and state.get('Health', {}).get('Status') == 'healthy', 'existing_minutes_not_healthy')
            result[name] = {'id': self.inspect(name, '.Id'), 'started': state['StartedAt']}
        return result

    def healthy(self, revision):
        command = "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2))))"
        for _ in range(30):
            try:
                data = json.loads(self.run(['docker', 'exec', CONTAINER, 'python3', '-B', '-c', command], timeout=8))
                if data.get('ready') is True and data.get('revision') == revision:
                    return True
            except Hold:
                pass
            time.sleep(1)
        return False

    def build(self, directory, revision):
        self.run(['docker', 'image', 'inspect', self.settings.python_base, '--format', '{{.Id}}'])
        image = 'slack-auto-mention:' + revision
        self.run(['docker', 'build', '--network=none', '--pull=false', '--build-arg', 'PYTHON_BASE=' + self.settings.python_base,
                  '--build-arg', 'APP_REVISION=' + revision, '-t', image, '-f', str(directory / 'Dockerfile'), str(directory)], timeout=180)
        image_id = json.loads(self.run(['docker', 'image', 'inspect', image, '--format', '{{json .Id}}']))
        require(bool(DIGEST.fullmatch(image_id)), 'invalid_built_image')
        name = 'slack-auto-mention-smoke-' + uuid.uuid4().hex
        try:
            self.run(['docker', 'run', '-d', '--name', name, '--label', 'app=slack-auto-mention-smoke', '--network=none', '--log-driver=none',
                      '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '32', '--memory', '128m',
                      '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                      '--user', str(os.getuid()) + ':' + str(os.getgid()), '--entrypoint', 'python3', image_id, '-B', '-c',
                      'import bot,container_entrypoint'], timeout=15)
            result = self.run(['docker', 'wait', name], timeout=20).strip()
            require(result == '0', 'non_root_offline_image_smoke_failed')
        finally:
            # Only the uniquely named, exact-image, owned smoke container may be removed.
            try:
                if self.inspect(name, '.Image') == image_id and self.inspect(name, '.Config.Labels').get('app') == 'slack-auto-mention-smoke':
                    self.run(['docker', 'rm', '-f', name], timeout=15)
            except Hold:
                pass
        return image_id

    def switch(self, image, revision):
        require(bool(DIGEST.fullmatch(image)) and bool(SHA.fullmatch(revision)), 'invalid_switch_identity')
        self.profile_check()
        environment = dict(self.environment, SLACK_IMAGE=image, SLACK_REVISION=revision, PYTHON_BASE=self.settings.python_base,
                           SLACK_UID=str(os.getuid()), SLACK_GID=str(os.getgid()), SLACK_STATE_DIR=str(self.settings.state),
                           SLACK_ENV_FILE=str(self.settings.runtime))
        self.run(['docker', 'compose', '-f', str(self.settings.profile), 'up', '-d', '--no-build', '--pull', 'never', '--force-recreate', 'bot'], environment=environment)

    def stop(self):
        require(self.inspect(CONTAINER, '.Config.Labels').get('com.docker.compose.project') == 'slack-auto-mention', 'unexpected_container_owner')
        self.run(['docker', 'stop', CONTAINER], timeout=45)


def write_metadata(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stage(settings, revision, files):
    releases = settings.root / 'releases'
    releases.mkdir(exist_ok=True, mode=0o700)
    private_path(releases, directory=True)
    directory = releases / revision
    directory.mkdir(exist_ok=True, mode=0o700)
    private_path(directory, directory=True)
    expected = dict(files, Dockerfile=DOCKERFILE.encode())
    for name, data in expected.items():
        target = directory / name
        target.parent.mkdir(exist_ok=True, mode=0o700)
        private_path(target.parent, directory=True)
        require(not target.is_symlink(), 'release_symlink')
        if target.exists():
            require(target.read_bytes() == data, 'immutable_release_changed')
        else:
            with open(target, 'xb') as stream:
                stream.write(data)
            target.chmod(0o600)
    return directory


def deliver(settings, gate, runtime, http, *, apply=False, lock=delivery_lock, staging=stage):
    require(not apply or settings.enabled, 'apply_not_enabled')
    if not apply:
        revision = gate.head()
        current = runtime.snapshot()
        if revision == current['revision']:
            return {'status': 'up_to_date', 'revision': revision, 'changed': False}
        run_id = gate.verify(revision, current['revision'])
        return {'status': 'eligible_plan_only', 'revision': revision, 'ci_run_id': run_id, 'changed': False}
    with lock(settings):
        intent = settings.root / 'intent.json'
        require(not intent.exists() and not intent.is_symlink(), 'incomplete_delivery_requires_reconciliation')
        revision = gate.head()
        current = runtime.snapshot()
        if revision == current['revision']:
            return {'status': 'up_to_date', 'revision': revision, 'changed': False}
        failed = settings.root / 'failed.json'
        if failed.exists() or failed.is_symlink():
            private_path(failed)
            require(json.loads(failed.read_text()).get('revision') != revision, 'failed_revision_quarantined')
        run_id = gate.verify(revision, current['revision'])
        switched = False
        try:
            directory = staging(settings, revision, application_files(http.archive(revision), revision))
            original_configuration = runtime.profile_check()
            dependencies = runtime.dependencies()
            image = runtime.build(directory, revision)
            gate.verify(revision, current['revision'])
            require(runtime.snapshot() == current, 'current_bot_changed_during_build')
            require(runtime.profile_check() == original_configuration, 'runtime_configuration_changed_during_build')
            require(runtime.dependencies() == dependencies, 'existing_minutes_changed_during_build')
            write_metadata(intent, {'revision': revision, 'previous_revision': current['revision'], 'previous_image': current['image']})
            switched = True  # a failed switch may already have stopped/replaced the container
            runtime.switch(image, revision)
            require(runtime.healthy(revision), 'new_revision_not_ready')
            require(runtime.profile_check() == original_configuration, 'runtime_configuration_changed_during_delivery')
            require(runtime.dependencies() == dependencies, 'existing_minutes_changed_during_delivery')
            write_metadata(settings.root / 'receipt.json', {'revision': revision, 'image': image, 'previous_revision': current['revision'], 'previous_image': current['image'], 'ci_run_id': run_id})
            intent.unlink()
            return {'status': 'deployed', 'revision': revision, 'ci_run_id': run_id, 'changed': True}
        except Exception:
            rollback = 'not_needed'
            if switched:
                try:
                    runtime.switch(current['image'], current['revision'])
                    require(runtime.healthy(current['revision']), 'rollback_not_ready')
                    rollback = 'previous_image_restored_ready'
                except Exception:
                    try:
                        runtime.stop()
                        rollback = 'bot_stopped_data_preserved'
                    except Exception:
                        rollback = 'unknown_manual_reconciliation_required'
            write_metadata(failed, {'revision': revision, 'rollback': rollback})
            if intent.exists() and rollback != 'unknown_manual_reconciliation_required':
                intent.unlink()
            raise Hold('delivery_failed_quarantined_' + rollback) from None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/slack-auto-mention/cd.json')
    parser.add_argument('--apply', action='store_true', help='requires separately approved enabled configuration and user timer')
    args = parser.parse_args(argv)
    try:
        require(os.name == 'posix' and hasattr(os, 'getuid'), 'linux_vm_required')
        settings = Settings.load(args.config)
        http = PublicHTTP()
        result = deliver(settings, GitHubGate(http), DockerRuntime(settings), http, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Hold as error:
        print(json.dumps({'status': 'held_no_automatic_retry_of_failed_revision', 'reason': str(error)}))
        return 2
    except Exception:
        print(json.dumps({'status': 'held', 'reason': 'unexpected_failure_details_suppressed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
