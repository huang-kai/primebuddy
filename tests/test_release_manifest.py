"""完整发行产物的版本、兼容信息和 SHA-256 互相对应。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from primebuddy.config.compatibility import load_release
from primebuddy.tools import build_release as builder
from primebuddy.tools.install import stage_application

ROOT = Path(__file__).resolve().parents[1]


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        stage_application(ROOT, self.source)
        (self.source / 'release.json').write_bytes((ROOT / 'release.json').read_bytes())
        (self.source / 'CHANGELOG.md').write_text('# 更新日志\n\n## [1.2.0] - 2026-10-29\n\n本次更新。\n\n## 1.0.0\n\n旧版本。\n')
        self.skills = []
        for name, version in [('diangetai', '0.4.1'), ('yeelight', '0.3.1'), ('chengyu-chain', '0.2.0')]:
            skill = self.root / name
            (skill / 'cfg').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 测试\n---\n测试。')
            (skill / 'main.py').write_text('')
            (skill / 'cfg/pkg.yaml').write_text(json.dumps({'version': version, 'primebuddy': {
                'version': 1, 'cli': {'configure': ['main.py', 'configure']}}}))
            self.skills.append(skill)

    def test_complete_release_has_verified_assets_and_matching_embedded_manifest(self):
        with patch.object(builder, 'ROOT', self.source):
            paths = builder.build_releases(self.root / 'output', self.skills, release=True)
        self.assertEqual({path.name for path in paths}, {
            'primebuddy-1.2.0.rpk', 'diangetai-0.4.1.rpk', 'yeelight-0.3.1.rpk', 'chengyu-chain-0.2.0.rpk', 'primebuddy-1.2.0-core.rpk', 'release.json', 'release-notes.md', 'SHA256SUMS'})
        output = self.root / 'output'
        manifest = load_release(output / 'release.json')
        self.assertEqual(manifest['framework']['sha256'], hashlib.sha256((output / 'primebuddy-1.2.0.rpk').read_bytes()).hexdigest())
        with zipfile.ZipFile(output / 'primebuddy-1.2.0.rpk') as archive:
            embedded = json.loads(archive.read('primebuddy/release.json'))
            self.assertNotIn('framework', embedded)
            self.assertEqual(embedded, {key: value for key, value in manifest.items() if key not in ('framework', 'framework_core')})
            self.assertIn('primebuddy/CHANGELOG.md', archive.namelist())
            self.assertIn('primebuddy/docs/audio-flow.md', archive.namelist())
            self.assertNotIn('primebuddy/changelog.md', archive.namelist())
            for rule in manifest['skills'].values():
                artifact = rule['artifact']
                content = (output / artifact['filename']).read_bytes()
                self.assertEqual(artifact['sha256'], hashlib.sha256(content).hexdigest())
                member = 'primebuddy/resource/bundled/' + artifact['filename']
                if artifact['filename'].startswith('yeelight-'):
                    self.assertNotIn(member, archive.namelist())
                else:
                    self.assertEqual(archive.read(member), content)
                self.assertEqual(artifact['url'], 'https://github.com/huang-kai/primebuddy/releases/download/v1.2.0/' + artifact['filename'])
        notes = (output / 'release-notes.md').read_text()
        self.assertIn('本次更新。', notes)
        self.assertNotIn('旧版本。', notes)
        self.assertIn('0.4.0', notes)
        self.assertIn('<0.5.0', notes)
        self.assertTrue(notes.startswith('# PrimeBuddy 1.2.0 - 2026-10-29\n'))
        self.assertEqual(notes.count('### Compatibility'), 1)
        self.assertEqual(notes.count('| Skill | Supported versions | Recommended |'), 1)

    def test_core_package_has_same_platform_without_bundled_skills_and_all_checksums_match(self):
        with patch.object(builder, 'ROOT', self.source):
            outputs = builder.build_releases(self.root / 'output', self.skills, release=True)
        output = self.root / 'output'
        manifest = load_release(output / 'release.json')
        core_artifact = manifest['framework_core']
        core = output / core_artifact['filename']
        self.assertEqual(core_artifact['sha256'], hashlib.sha256(core.read_bytes()).hexdigest())
        with zipfile.ZipFile(core) as package, zipfile.ZipFile(output / manifest['framework']['filename']) as full:
            self.assertFalse(any(name.startswith('primebuddy/resource/bundled/') for name in package.namelist()))
            embedded = json.loads(package.read('primebuddy/release.json'))
            self.assertEqual(embedded['bundled_skills'], [])
            self.assertEqual(embedded['skills'], manifest['skills'])
            self.assertIn('primebuddy/docs/README.md', package.namelist())
            for name in package.namelist():
                if name != 'primebuddy/release.json':
                    self.assertEqual(package.read(name), full.read(name), name)
        checksums = dict(line.split('  ', 1)[::-1] for line in (output / 'SHA256SUMS').read_text().splitlines())
        self.assertEqual(set(checksums), {path.name for path in outputs if path.name != 'SHA256SUMS'})
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)

    def test_notes_replace_compatibility_with_manifest_and_keep_following_sections(self):
        (self.source / 'CHANGELOG.md').write_text(
            '# Changelog\n\n## [1.2.0] - 2026-10-29\n\n### Features\n\n本次更新。\n\n'
            '### Compatibility\n\n| 技能 | 旧表 |\n| --- | --- |\n| diangetai | 0.0.1 |\n\n'
            '### Fixes\n\n保留后续修复。\n\n## [1.0.0] - 2026-10-14\n\n旧版本。\n')
        manifest = load_release(self.source / 'release.json')
        manifest['skills']['diangetai']['recommended_version'] = '0.4.1'
        with patch.object(builder, 'ROOT', self.source):
            notes = builder.release_notes(manifest)
        self.assertTrue(notes.startswith('# PrimeBuddy 1.2.0 - 2026-10-29\n'))
        self.assertEqual(notes.count('### Compatibility'), 1)
        self.assertEqual(notes.count('| Skill | Supported versions | Recommended |'), 1)
        self.assertIn('| `diangetai` | `>=0.4.0, <0.5.0` | `0.4.1` |', notes)
        self.assertIn('### Fixes\n\n保留后续修复。', notes)
        self.assertLess(notes.index('### Compatibility'), notes.index('### Fixes'))
        self.assertNotIn('旧表', notes)
        self.assertNotIn('0.0.1', notes)
        self.assertNotIn('旧版本。', notes)

    def test_release_requires_framework_and_exact_recommended_skill_versions(self):
        with patch.object(builder, 'ROOT', self.source):
            for skills, framework in [(self.skills, False), (self.skills[:1], True)]:
                with self.assertRaises(ValueError):
                    builder.build_releases(self.root / 'output', skills, framework=framework, release=True)
            path = self.skills[0] / 'cfg/pkg.yaml'
            metadata = json.loads(path.read_text())
            metadata['version'] = '0.4.2'
            path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                builder.build_releases(self.root / 'output', self.skills, release=True)
        self.assertFalse((self.root / 'output').exists())

    def test_official_release_requires_wizard_protocol(self):
        (self.skills[0] / 'cfg/pkg.yaml').write_text('{"version":"0.4.0"}')
        with patch.object(builder, 'ROOT', self.source), self.assertRaisesRegex(ValueError, 'version'):
            builder.build_releases(self.root / 'output', self.skills, release=True)
        self.assertFalse((self.root / 'output').exists())

    def test_missing_changelog_section_does_not_replace_existing_release(self):
        with patch.object(builder, 'ROOT', self.source):
            paths = builder.build_releases(self.root / 'output', self.skills, release=True)
            before = {path: path.read_bytes() for path in paths}
            (self.source / 'CHANGELOG.md').write_text('# 更新日志\n\n## 1.0.0\n旧版本。\n')
            with self.assertRaises(ValueError):
                builder.build_releases(self.root / 'output', self.skills, release=True, overwrite=True)
            self.assertEqual({path: path.read_bytes() for path in paths}, before)


if __name__ == '__main__':
    unittest.main()
