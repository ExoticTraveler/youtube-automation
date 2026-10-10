import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from tools import publishing as p

try:
    from PIL import Image
except ImportError:
    Image = None


def image_bytes(fmt, size=(4, 3)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (200, 10, 10)).save(buffer, format=fmt)
    return buffer.getvalue()


class SipsPathTests(unittest.TestCase):
    """When /usr/bin/sips exists, it remains the only decoder (macOS production path)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'thumbnail.png'
        self.path.write_bytes(b'image-bytes')
        exists = patch.object(p.os.path, 'exists', side_effect=lambda x: x == p.SIPS or Path(x).exists())
        exists.start(); self.addCleanup(exists.stop)

    def run_sips(self, stdout, returncode=0):
        result = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')
        with patch.object(p.subprocess, 'run', return_value=result) as run, \
                patch.object(p, '_validate_image_pillow') as pillow:
            try:
                p.validate_image(self.path, b'image-bytes')
            finally:
                pillow.assert_not_called()
        return run

    def test_sips_is_called_with_the_unchanged_command(self):
        run = self.run_sips('/x\n  format: png\n  pixelWidth: 1280\n  pixelHeight: 720\n')
        run.assert_called_once_with(['/usr/bin/sips', '-g', 'format', '-g', 'pixelWidth', '-g', 'pixelHeight',
                                     str(self.path)], capture_output=True, text=True, timeout=15)

    def test_sips_rejections_are_unchanged(self):
        with self.assertRaisesRegex(ValueError, 'not a decodable'):
            self.run_sips('/x\n  format: gif\n  pixelWidth: 1\n  pixelHeight: 1\n')
        with self.assertRaisesRegex(ValueError, 'not a decodable'):
            self.run_sips('', returncode=1)
        with self.assertRaisesRegex(ValueError, 'invalid image dimensions'):
            self.run_sips('/x\n  format: jpeg\n  pixelWidth: 0\n  pixelHeight: 720\n')

    def test_size_limit_is_checked_before_any_decoder(self):
        with patch.object(p.subprocess, 'run') as run:
            for raw in (b'', b'x' * (50 * 1024 * 1024 + 1)):
                with self.assertRaisesRegex(ValueError, '1 byte to 50 MiB'):
                    p.validate_image(self.path, raw)
        run.assert_not_called()


@unittest.skipIf(Image is None, 'Pillow not installed')
class PillowFallbackTests(unittest.TestCase):
    """Without /usr/bin/sips, Pillow enforces the same contract and never shells out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        sips = patch.object(p, 'SIPS', str(Path(self.tmp.name) / 'no-sips'))
        sips.start(); self.addCleanup(sips.stop)
        run = patch.object(p.subprocess, 'run', side_effect=AssertionError('sips must not run'))
        run.start(); self.addCleanup(run.stop)

    def check(self, name, raw):
        path = Path(self.tmp.name) / name
        path.write_bytes(raw)
        return p.validate_image(path, raw)

    def test_jpeg_png_webp_are_accepted(self):
        for fmt, name in (('JPEG', 'thumbnail.jpg'), ('PNG', 'thumbnail.png'), ('WEBP', 'thumbnail.webp')):
            with self.subTest(fmt=fmt):
                self.assertIsNone(self.check(name, image_bytes(fmt)))

    def test_corrupt_or_truncated_image_is_not_decodable(self):
        png = image_bytes('PNG', (64, 64))
        for raw in (b'not an image', png[:len(png) // 2]):
            with self.subTest(size=len(raw)), self.assertRaisesRegex(ValueError, 'not a decodable'):
                self.check('thumbnail.png', raw)

    def test_unsupported_format_is_rejected_even_with_an_accepted_extension(self):
        for fmt in ('GIF', 'BMP'):
            with self.subTest(fmt=fmt), self.assertRaisesRegex(ValueError, 'not a decodable'):
                self.check('thumbnail.png', image_bytes(fmt))

    def test_size_limit_still_applies(self):
        with self.assertRaisesRegex(ValueError, '1 byte to 50 MiB'):
            p.validate_image(Path(self.tmp.name) / 'thumbnail.png', b'')

    def test_missing_pillow_is_reported_as_wait_not_crash(self):
        raw = image_bytes('PNG')
        with patch.dict(sys.modules, {'PIL': None}), self.assertRaisesRegex(ValueError, 'decoder unavailable'):
            self.check('thumbnail.png', raw)


if __name__ == '__main__':
    unittest.main()
