from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import final_subtitle_autogen as f


class NarrationSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'done' / 'IRAN3'
        self.project.mkdir(parents=True)
        self.assets = self.base / 'vault' / 'Narration Sources'
        self.assets.mkdir(parents=True)
        source_patch = patch.object(f, 'NARRATION_ASSET_DIR', self.assets)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        log_patch = patch.object(f, 'log')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_exact_project_id_reads_verbatim_obsidian_asset(self):
        expected = b'# Narration\n\nExact punctuation -- and spacing.\n'
        (self.assets / 'IRAN3.md').write_bytes(expected)
        path, raw, text = f.narration_reference(self.project)
        self.assertEqual(path, self.assets / 'IRAN3.md')
        self.assertEqual(raw, expected)
        self.assertEqual(text.encode(), expected)

    def test_does_not_guess_nearby_project(self):
        (self.assets / 'IRAN2.md').write_text('wrong project')
        with patch.object(f, 'BASE', self.base):
            self.assertIsNone(f.narration_reference(self.project))

    def test_iran_family_resolves_explicit_country_source(self):
        legacy = self.base / 'done' / 'IRAN2' / 'subs' / 'Iran_2026_Narration_Master.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('legacy Iran narration')
        with patch.object(f, 'BASE', self.base):
            path, _, text = f.narration_reference(self.project)
        self.assertEqual(path, legacy)
        self.assertEqual(text, 'legacy Iran narration')

    def test_unrelated_project_does_not_load_iran_source(self):
        legacy = self.base / 'done' / 'IRAN2' / 'subs' / 'Iran_2026_Narration_Master.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('Iran only')
        project = self.base / 'done' / 'SYR1'
        project.mkdir()
        with patch.object(f, 'BASE', self.base):
            self.assertIsNone(f.narration_reference(project))

    def test_exact_project_source_precedes_country_source(self):
        (self.assets / 'IRAN_2026.md').write_text('country')
        exact = self.assets / 'IRAN3.md'
        exact.write_text('exact project')
        path, _, text = f.narration_reference(self.project)
        self.assertEqual((path, text), (exact, 'exact project'))

    def test_existing_local_override_keeps_priority(self):
        (self.assets / 'IRAN3.md').write_text('vault')
        local = self.project / 'narration.md'
        local.write_text('local')
        path, raw, text = f.narration_reference(self.project)
        self.assertEqual((path, raw, text), (local, b'local', 'local'))

    def test_required_reference_fails_when_asset_absent(self):
        (self.project / '.reference_required').touch()
        with patch.object(f, 'BASE', self.base):
            with self.assertRaisesRegex(ValueError, 'no local or Obsidian'):
                f.narration_reference(self.project)

    def test_symlink_asset_is_refused(self):
        outside = self.base / 'outside.md'
        outside.write_text('not an approved asset')
        (self.assets / 'IRAN3.md').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            f.narration_reference(self.project)

    def test_supported_correction_keeps_cues_and_does_not_touch_interview(self):
        cues = [
            {'timing': '00:00:00,000 --> 00:00:04,000',
             'text': 'i traveled across this old city during the summer.'},
            {'timing': '00:00:04,000 --> 00:00:07,000',
             'text': 'The guide says the market is busy today.'},
        ]
        before = [(cue['timing'], cue['text']) for cue in cues]
        result = f.apply_narration(
            [dict(cue) for cue in cues], cues, cues,
            'I traveled across this old city during the summer.')
        self.assertEqual(len(result), len(cues))
        self.assertEqual([cue['timing'] for cue in result], [cue['timing'] for cue in cues])
        self.assertEqual(result[0]['text'], 'I traveled across this old city during the summer.')
        self.assertEqual(result[1]['text'], cues[1]['text'])
        self.assertEqual(before[1], (result[1]['timing'], result[1]['text']))


if __name__ == '__main__':
    unittest.main()
