"""自动更新的网络边界；用本地响应替身验证，不访问 GitHub。"""
import copy
from contextlib import nullcontext, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

from primebuddy.config.compatibility import release_url
from primebuddy.config.storage import SetupError, write_config
from primebuddy.tools import update


def artifact(name, content, version='1.1.0'):
    return {'filename': name, 'url': release_url(version, name),
            'sha256': hashlib.sha256(content).hexdigest()}


def release_response(manifest):
    raw = json.dumps(manifest).encode()
    api = {'tag_name': 'v' + manifest['version'], 'draft': False, 'prerelease': False,
           'assets': [{'name': 'release.json', 'browser_download_url': release_url(manifest['version'], 'release.json'),
                       'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}]}
    return api, raw


class UpdateDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        env = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        code = self.home / 'src/primebuddy/__init__.py'
        code.parent.mkdir(parents=True)
        code.write_text("__version__ = '1.0.0'\n")
        write_config(self.home / 'config.yaml', {'skills': {'directories': []}})
        write_config(self.home / 'skills.yaml', {})
        account = self.home / 'data/account'
        account.parent.mkdir()
        account.write_bytes(b'test-only-private-data')
        self.payload = b'expected framework payload'
        self.manifest = {'schema_version': 1, 'version': '1.1.0', 'skills': {
            'diangetai': {'min_version': '0.4.0', 'max_version': '0.5.0', 'recommended_version': '0.4.0',
                         'artifact': artifact('diangetai-0.4.0.rpk', b'skill')}},
            'framework': artifact('primebuddy-1.1.0.rpk', self.payload)}

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.home.rglob('*') if path.is_file()}

    def test_latest_and_tag_use_only_fixed_official_metadata_addresses(self):
        for version, suffix in [(None, 'latest'), ('1.1.0', 'tags/v1.1.0')]:
            api, raw = release_response(self.manifest)
            with self.subTest(version=version), patch.object(update, 'fetch_bytes', side_effect=[json.dumps(api).encode(), raw]) as fetch:
                self.assertEqual(update.fetch_release(version), self.manifest)
            self.assertEqual(fetch.call_args_list[0].args,
                             ('https://api.github.com/repos/huang-kai/primebuddy/releases/' + suffix, 1024 * 1024))
            self.assertEqual(fetch.call_args_list[0].kwargs, {'api': True})
            self.assertEqual(fetch.call_args_list[1].args,
                             ('https://github.com/huang-kai/primebuddy/releases/download/v1.1.0/release.json', 65536))

    def test_prerelease_draft_and_wrong_tags_stop_before_manifest_download(self):
        for change, requested in [({'prerelease': True}, None), ({'draft': True}, None),
                                  ({'tag_name': '1.1.0'}, None), ({'tag_name': 'vnext'}, None),
                                  ({'tag_name': 'v1.2.0'}, '1.1.0')]:
            api, _ = release_response(self.manifest)
            api.update(change)
            before = self.snapshot()
            with self.subTest(change=change), patch.object(update, 'fetch_bytes', return_value=json.dumps(api).encode()) as fetch:
                with self.assertRaises(SetupError):
                    update.run_update(check=True, version=requested)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.snapshot(), before)

    def test_invalid_requested_version_never_contacts_network(self):
        with patch.object(update, 'fetch_bytes') as fetch, self.assertRaises(SetupError):
            update.fetch_release('../latest')
        fetch.assert_not_called()

    def test_wrong_manifest_address_and_digest_are_rejected(self):
        for address, digest in [('https://example.invalid/release.json', None),
                                (release_url('1.1.0', 'release.json'), 'sha256:' + '0' * 64)]:
            api, raw = release_response(self.manifest)
            api['assets'][0].update(browser_download_url=address, digest=digest)
            before = self.snapshot()
            with self.subTest(address=address, digest=digest), \
                    patch.object(update, 'fetch_bytes', side_effect=[json.dumps(api).encode(), raw]), \
                    self.assertRaises(SetupError):
                update.run_update(check=True)
            self.assertEqual(self.snapshot(), before)

    def test_mismatched_manifest_and_nonofficial_artifact_urls_are_rejected(self):
        cases = []
        wrong_version = copy.deepcopy(self.manifest)
        wrong_version['version'] = '1.2.0'
        wrong_version['framework'] = artifact('primebuddy-1.2.0.rpk', self.payload, '1.2.0')
        wrong_version['skills']['diangetai']['artifact'] = artifact('diangetai-0.4.0.rpk', b'skill', '1.2.0')
        cases.append(wrong_version)
        for kind in ('framework', 'skill'):
            manifest = copy.deepcopy(self.manifest)
            target = manifest['framework'] if kind == 'framework' else manifest['skills']['diangetai']['artifact']
            target['url'] = 'https://example.invalid/' + target['filename']
            cases.append(manifest)
        for manifest in cases:
            api, raw = release_response(manifest)
            api['tag_name'] = 'v1.1.0'
            api['assets'][0]['browser_download_url'] = release_url('1.1.0', 'release.json')
            before = self.snapshot()
            with self.subTest(manifest=manifest), patch.object(update, 'fetch_bytes', side_effect=[json.dumps(api).encode(), raw]), \
                    self.assertRaises(SetupError):
                update.run_update(check=True)
            self.assertEqual(self.snapshot(), before)

    def test_check_is_read_only_and_does_not_download_packages_or_migrate_config(self):
        api, raw = release_response(self.manifest)
        before = self.snapshot()
        with patch.object(update, 'fetch_bytes', side_effect=[json.dumps(api).encode(), raw]) as fetch, \
                patch.object(update, 'download_artifact') as download, patch.object(update, 'update_lock') as lock, \
                patch.object(update, 'commit_update') as commit, redirect_stdout(io.StringIO()):
            update.run_update(check=True)
        self.assertEqual(fetch.call_count, 2)
        download.assert_not_called()
        lock.assert_not_called()
        commit.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / 'cfg').exists())

    def test_download_checks_hash_before_creating_or_overwriting_destination(self):
        destination = self.home / 'download.rpk'
        for exists in (False, True):
            if exists:
                destination.write_bytes(b'keep previous download')
            with self.subTest(exists=exists), patch.object(update, 'fetch_bytes', return_value=b'wrong bytes'), \
                    self.assertRaisesRegex(SetupError, 'SHA-256'):
                update.download_artifact(self.manifest['framework'], destination)
            if exists:
                self.assertEqual(destination.read_bytes(), b'keep previous download')
            else:
                self.assertFalse(destination.exists())
        with patch.object(update, 'fetch_bytes', return_value=self.payload) as fetch:
            update.download_artifact(self.manifest['framework'], destination)
        self.assertEqual(destination.read_bytes(), self.payload)
        fetch.assert_called_once_with(self.manifest['framework']['url'], update.MAX_PACKAGE)

    def test_bad_framework_bytes_never_change_installed_code_or_private_data(self):
        for content, matching_hash in [(b'corrupt download', False), (b'not a ZIP archive', True)]:
            manifest = copy.deepcopy(self.manifest)
            if matching_hash:
                manifest['framework'] = artifact('primebuddy-1.1.0.rpk', content)
            api, raw = release_response(manifest)
            before = self.snapshot()
            with self.subTest(matching_hash=matching_hash), \
                    patch.object(update, 'fetch_bytes', side_effect=[json.dumps(api).encode(), raw, content]), \
                    patch.object(update, 'update_lock', return_value=()), \
                    patch.object(update, 'commit_update') as commit, redirect_stdout(io.StringIO()):
                with self.assertRaises((SetupError, zipfile.BadZipFile)):
                    update.run_update()
            commit.assert_not_called()
            self.assertEqual(self.snapshot(), before)
            self.assertFalse((self.home / '.update').exists())
            self.assertFalse(list(self.home.glob('.update-download-*')))

    def test_oversized_response_reads_only_limit_plus_one_and_is_rejected(self):
        response = Mock()
        response.read.return_value = b'123456789'
        opener = Mock()
        opener.open.return_value = nullcontext(response)
        with patch.object(update.urllib.request, 'build_opener', return_value=opener), \
                self.assertRaisesRegex(SetupError, '超过'):
            update.fetch_bytes(release_url('1.1.0', 'release.json'), 8)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, release_url('1.1.0', 'release.json'))
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 30})
        response.read.assert_called_once_with(9)

    def test_transport_failure_is_reported_without_creating_a_download(self):
        destination = self.home / 'download.rpk'
        for error in (urllib.error.URLError('offline test'), TimeoutError('test timeout')):
            opener = Mock()
            opener.open.side_effect = error
            before = self.snapshot()
            with self.subTest(error=type(error).__name__), patch.object(update.urllib.request, 'build_opener', return_value=opener), \
                    self.assertRaisesRegex(SetupError, '网络'):
                update.download_artifact(self.manifest['framework'], destination)
            self.assertEqual(self.snapshot(), before)

    def test_release_transport_error_keeps_the_actionable_network_message(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('offline test')
        with patch.object(update.urllib.request, 'build_opener', return_value=opener), \
                self.assertRaisesRegex(SetupError, '网络'):
            update.fetch_release()


if __name__ == '__main__':
    unittest.main()
