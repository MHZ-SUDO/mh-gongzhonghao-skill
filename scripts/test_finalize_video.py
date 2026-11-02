"""Short synthetic media checks; no real lecture files or publishing operations."""
from array import array
from datetime import datetime, timezone
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import finalize_video as finalizer


class FinalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (Path(os.environ.get('MH_SKILL_TEST_OUTPUT_DIR', tempfile.gettempdir()))
                    / datetime.now(timezone.utc).strftime('mh-finalize-synthetic-%Y%m%d-%H%M%S-%f'))
        cls.root.mkdir(parents=True)
        cls.source = cls.root / 'base.mp4'
        cls.srt = cls.root / 'base.srt'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-f', 'lavfi',
                        '-i', 'testsrc2=s=320x180:r=30', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '3', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', str(cls.source)], check=True)
        cls.srt.write_text('1\n00:00:00,250 --> 00:00:01,250\n定义与讲解\n\n'
                           '2\n00:00:01,250 --> 00:00:03,000\n公式与结尾\n', encoding='utf-8')
        cls.source_hash = finalizer.sha256(cls.source)
        cls.srt_hash = finalizer.sha256(cls.srt)

    def output(self, name):
        return self.root / name

    def test_complete_export_retimes_srt_preserves_inputs_and_pitch(self):
        output = self.output('complete.mp4')
        result = finalizer.finalize(self.source, output, srt=self.srt)
        report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
        self.assertEqual(result['rate'], '5/4')
        self.assertEqual(report['status'], 'technical_checks_passed')
        self.assertEqual(report['statistics']['final_seconds'], 2.4)
        self.assertAlmostEqual(report['actual_final_video_seconds'], 2.4, delta=.04)
        self.assertLess(abs(report['av_duration_difference_seconds']), .05)
        self.assertTrue(all(report['checks'].values()))
        self.assertTrue(report['full_decode_performed'])
        self.assertFalse(report['visual_review_performed'])
        self.assertFalse(report['listening_review_performed'])
        self.assertEqual(report['cue_mapping'][0]['final_ms'], [200, 1000])
        self.assertEqual(report['cue_mapping'][1]['final_ms'], [1000, 2400])
        self.assertIn('00:00:00,200 --> 00:00:01,000', output.with_suffix('.srt').read_text(encoding='utf-8'))
        self.assertEqual(finalizer.sha256(self.source), self.source_hash)
        self.assertEqual(finalizer.sha256(self.srt), self.srt_hash)
        # The last-stage map, not the unaccelerated edit timeline, is accepted
        # by the existing technical verifier as its expected-duration input.
        verify = subprocess.run([sys.executable, str(Path(finalizer.__file__).with_name('media_tools.py')),
                                 'verify-media', str(output), '--out', str(self.root / 'complete.verify.json'),
                                 '--timeline', result['report'], '--srt', str(output.with_suffix('.srt')),
                                 '--fps', '30/1', '--full-decode'], capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        pcm = subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
                                       '-i', str(output), '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'])
        samples = array('f')
        samples.frombytes(pcm)
        samples = samples[3200:-3200]
        frequency = sum(a <= 0 < b for a, b in zip(samples, samples[1:])) * 16000 / len(samples)
        self.assertAlmostEqual(frequency, 440, delta=3)
        (self.root / 'pitch-measurement.json').write_text(json.dumps({
            'synthetic_input_hz': 440, 'measured_output_hz': frequency,
            'method': 'positive zero crossings on decoded central mono PCM',
            'does_not_prove_lecture_listening_quality': True,
        }, indent=2), encoding='utf-8')

    def test_refuses_input_output_collision_and_existing_targets(self):
        with self.assertRaises(ValueError):
            finalizer.finalize(self.source, self.source)
        existing = self.output('existing.mp4')
        existing.write_bytes(b'preserve this file')
        with self.assertRaises(ValueError):
            finalizer.finalize(self.source, existing)
        self.assertEqual(existing.read_bytes(), b'preserve this file')
        with self.assertRaises(ValueError):
            finalizer.finalize(self.source, self.output('srt-collision.mp4'), srt=self.srt, output_srt=self.srt)
        self.assertEqual(finalizer.sha256(self.source), self.source_hash)

    def test_refuses_second_speed_change_even_after_renaming(self):
        marked = self.output('renamed-tagged.mp4')
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-i', str(self.source),
                        '-map', '0', '-c', 'copy', '-metadata', 'comment=' + finalizer.MARKER + '5/4',
                        str(marked)], check=True)
        with self.assertRaisesRegex(ValueError, 'already finalized'):
            finalizer.finalize(marked, self.output('forbidden-double.mp4'))
        sidecar_source = self.output('sidecar-tagged.mp4')
        shutil.copyfile(self.source, sidecar_source)
        sidecar_source.with_suffix('.finalize.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'already finalized'):
            finalizer.finalize(sidecar_source, self.output('forbidden-sidecar-double.mp4'))

    def test_rejects_real_input_av_start_offset(self):
        delayed = self.output('delayed-audio.mp4')
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-f', 'lavfi',
                        '-i', 'color=c=navy:s=320x180:r=30', '-itsoffset', '0.2', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                        '-c:v', 'libx264', '-threads', '1', '-c:a', 'aac', str(delayed)], check=True)
        self.assertGreater(abs(finalizer.probe(delayed)['av_start_difference_seconds']), .1)
        with self.assertRaisesRegex(ValueError, 'start offset'):
            finalizer.finalize(delayed, self.output('offset-not-silently-reset.mp4'))

    def test_srt_uses_exact_fraction_and_rejects_invalid_boundaries(self):
        text, mapping = finalizer.retime_srt(self.srt, Fraction(5, 4), 3)
        self.assertEqual(mapping[-1]['final_ms'], [1000, 2400])
        invalid = self.output('invalid.srt')
        invalid.write_text('1\n00:00:02,000 --> 00:00:04,000\n越界\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'out-of-range'):
            finalizer.retime_srt(invalid, Fraction(5, 4), 3)
        invalid.write_text('1\n00:00:00,002 --> 00:00:00,003\n极短\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'collapses'):
            finalizer.retime_srt(invalid, Fraction(5, 4), 3)

    def test_export_failure_is_recorded_as_failed(self):
        output = self.output('encode-failure.mp4')
        original_run = subprocess.run

        def failing_run(command, *args, **kwargs):
            if '-filter_complex' in command:
                return subprocess.CompletedProcess(command, 1, '', 'synthetic encoder failure')
            return original_run(command, *args, **kwargs)

        with patch.object(finalizer.subprocess, 'run', side_effect=failing_run):
            with self.assertRaisesRegex(ValueError, 'export failed'):
                finalizer.finalize(self.source, output)
        report = json.loads(output.with_suffix('.finalize.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['ffmpeg_exit_code'], 1)
        self.assertFalse(report['full_decode_performed'])
        self.assertEqual(finalizer.sha256(self.source), self.source_hash)

    def test_decode_failure_cannot_be_reported_as_passed(self):
        output = self.output('decode-failure.mp4')
        original_run = subprocess.run

        def failing_decode(command, *args, **kwargs):
            if '-xerror' in command:
                return subprocess.CompletedProcess(command, 1, '', 'synthetic decode failure')
            return original_run(command, *args, **kwargs)

        with patch.object(finalizer.subprocess, 'run', side_effect=failing_decode):
            with self.assertRaisesRegex(ValueError, 'technical checks'):
                finalizer.finalize(self.source, output)
        report = json.loads(output.with_suffix('.finalize.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['checks']['full_decode'])

    def test_cli_preserves_fractional_fps_and_supports_no_external_srt(self):
        fractional = self.output('fractional-fps.mp4')
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-i', str(self.source),
                        '-vf', 'fps=30000/1001', '-c:v', 'libx264', '-threads', '1', '-c:a', 'copy',
                        str(fractional)], check=True)
        output = self.output('fractional-final.mp4')
        result = subprocess.run([sys.executable, str(Path(finalizer.__file__).resolve()), str(fractional),
                                 str(output)], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)['srt'])
        self.assertEqual(finalizer.probe(output)['fps'], '30000/1001')

    def test_invalid_rational_cli_is_a_readable_failure_without_outputs(self):
        output = self.output('invalid-rational.mp4')
        for option in ('--rate', '--fps'):
            with self.subTest(option=option):
                result = subprocess.run([sys.executable, str(Path(finalizer.__file__).resolve()),
                                         str(self.source), str(output), option, '1/0'],
                                        capture_output=True, text=True, encoding='utf-8', timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)['status'], 'failed')
                self.assertIn(option[2:], json.loads(result.stderr)['error'])
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
