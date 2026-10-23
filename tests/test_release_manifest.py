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
        (self.source / 'CHANGELOG.md').write_text('# 更新日志\n\n## [1.1.0] - 2026-10-23\n\n本次更新。\n\n## 1.0.0\n\n旧版本。\n')
        self.skills = []
        for name, version in [('diangetai', '0.4.0'), ('yeelight', '0.3.0')]:
            skill = self.root / name
            (skill / 'cfg').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 测试\n---\n测试。')
            (skill / 'cfg/pkg.yaml').write_text(json.dumps({'version': version}))
            self.skills.append(skill)

    def test_complete_release_has_verified_assets_and_matching_embedded_manifest(self):
        with patch.object(builder, 'ROOT', self.source):
            paths = builder.build_releases(self.root / 'output', self.skills, release=True)
        self.assertEqual({path.name for path in paths}, {
            'primebuddy-1.1.0.rpk', 'diangetai-0.4.0.rpk', 'yeelight-0.3.0.rpk', 'release.json', 'release-notes.md'})
        output = self.root / 'output'
        manifest = load_release(output / 'release.json')
        self.assertEqual(manifest['framework']['sha256'], hashlib.sha256((output / 'primebuddy-1.1.0.rpk').read_bytes()).hexdigest())
        with zipfile.ZipFile(output / 'primebuddy-1.1.0.rpk') as archive:
            embedded = json.loads(archive.read('primebuddy/release.json'))
            self.assertNotIn('framework', embedded)
            self.assertEqual(embedded, {key: value for key, value in manifest.items() if key != 'framework'})
            self.assertIn('primebuddy/CHANGELOG.md', archive.namelist())
            self.assertNotIn('primebuddy/changelog.md', archive.namelist())
            for rule in manifest['skills'].values():
                artifact = rule['artifact']
                content = (output / artifact['filename']).read_bytes()
                self.assertEqual(artifact['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(archive.read('primebuddy/resource/bundled/' + artifact['filename']), content)
                self.assertEqual(artifact['url'], 'https://github.com/huang-kai/primebuddy/releases/download/v1.1.0/' + artifact['filename'])
        notes = (output / 'release-notes.md').read_text()
        self.assertIn('本次更新。', notes)
        self.assertNotIn('旧版本。', notes)
        self.assertIn('0.4.0', notes)
        self.assertIn('<0.5.0', notes)
        self.assertTrue(notes.startswith('# PrimeBuddy 1.1.0 - 2026-10-23\n'))
        self.assertEqual(notes.count('### Compatibility'), 1)
        self.assertEqual(notes.count('| Skill | Supported versions | Recommended |'), 1)

    def test_notes_replace_compatibility_with_manifest_and_keep_following_sections(self):
        (self.source / 'CHANGELOG.md').write_text(
            '# Changelog\n\n## [1.1.0] - 2026-10-23\n\n### Features\n\n本次更新。\n\n'
            '### Compatibility\n\n| 技能 | 旧表 |\n| --- | --- |\n| diangetai | 0.0.1 |\n\n'
            '### Fixes\n\n保留后续修复。\n\n## [1.0.0] - 2026-10-14\n\n旧版本。\n')
        manifest = load_release(self.source / 'release.json')
        manifest['skills']['diangetai']['recommended_version'] = '0.4.1'
        with patch.object(builder, 'ROOT', self.source):
            notes = builder.release_notes(manifest)
        self.assertTrue(notes.startswith('# PrimeBuddy 1.1.0 - 2026-10-23\n'))
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
            (self.skills[0] / 'cfg/pkg.yaml').write_text('{"version":"0.4.1"}')
            with self.assertRaises(ValueError):
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
