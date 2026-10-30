"""Read-only media inventory, deterministic timeline mapping and technical QA.

This does not transcribe, choose edits, rename/delete sources, or render video.
All outputs are new files; an existing report is never silently overwritten.
Python standard library only; ffprobe/ffmpeg are separate optional executables.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from xml.etree import ElementTree as ET
from zipfile import ZipFile

VIDEO = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.wmv', '.mts'}
OTHER = {'.docx', '.srt', '.wav', '.mp3', '.m4a'}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def new_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')


def fresh_dir(path):
    path = Path(path).resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f'Use a new or empty output directory: {path}')
    path.mkdir(parents=True, exist_ok=True)
    return path


def tool(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'{name} not on PATH; supply a verified local installation in PATH')
    return found


def probe(path):
    p = subprocess.run([tool('ffprobe'), '-v', 'error', '-show_format',
                        '-show_streams', '-of', 'json', str(path)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode:
        raise RuntimeError(f'ffprobe failed for {path}: {p.stderr.strip()}')
    return json.loads(p.stdout)


def rounded(value):
    """Half-up rounding for nonnegative exact rational clocks."""
    value = Fraction(value)
    if value < 0:
        raise ValueError('negative clock value')
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def seconds(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a time')
    v = Fraction(str(value))
    if v < 0:
        raise ValueError('time must be nonnegative')
    return v


def positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def inventory(args):
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('inventory root must be a directory')
    paths = root.rglob('*') if args.recursive else root.iterdir()
    files = sorted((p for p in paths if p.is_file() and p.suffix.lower() in VIDEO | OTHER),
                   key=lambda p: str(p).casefold())
    rows, groups = [], {}
    for p in files:
        before = p.stat()
        digest = sha256(p)
        row = {'path': str(p.resolve()), 'name': p.name, 'bytes': before.st_size,
               'sha256': digest, 'modified_ns': before.st_mtime_ns}
        if p.suffix.lower() in VIDEO or p.suffix.lower() in {'.wav', '.mp3', '.m4a'}:
            try:
                row['probe'] = probe(p)
            except RuntimeError as e:
                row['probe_error'] = str(e)
        after = p.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f'File changed during inventory, retry when recording stops: {p}')
        rows.append(row)
        groups.setdefault((row['bytes'], digest), []).append(row['path'])
    result = {'schema': 'mh-video-inventory/v1', 'root': str(root),
              'recursive': args.recursive, 'read_only': True, 'files': rows,
              'exact_duplicate_groups': [v for v in groups.values() if len(v) > 1],
              'note': 'Duplicate groups do not choose a keeper or authorize mutation.'}
    new_json(args.out, result)
    return {'files': len(rows), 'duplicate_groups': len(result['exact_duplicate_groups']),
            'report': str(args.out)}


def compile_project(args):
    spec_file = args.spec.resolve(strict=True)
    spec = read_json(spec_file)
    if spec.get('schema') != 'mh-video-project/v1':
        raise ValueError('Expected schema mh-video-project/v1')
    fps = Fraction(str(spec['fps']))
    if fps <= 0 or fps > 240:
        raise ValueError('fps must be positive and at most 240')
    rate = positive_int(spec.get('sample_rate', 48000), 'sample_rate')
    cover = spec.get('cover_frames', 0)
    if isinstance(cover, bool) or not isinstance(cover, int) or cover < 0:
        raise ValueError('cover_frames must be a nonnegative integer')
    source_list = spec.get('sources', [])
    if not source_list:
        raise ValueError('sources must not be empty')
    rows, source_meta, ids, paths_seen, hashes_seen = [], [], set(), set(), set()
    cursor = cover
    original_seconds = 0.
    for source in source_list:
        key = source['id']
        if not isinstance(key, str) or not key or key in ids:
            raise ValueError('Each source needs a unique nonempty string id')
        ids.add(key)
        path = (spec_file.parent / source['path']).resolve(strict=True)
        if path in paths_seen:
            raise ValueError('Same source path included twice; remove duplicate before computing statistics')
        paths_seen.add(path)
        digest = sha256(path)
        if source.get('sha256') and digest.casefold() != source['sha256'].casefold():
            raise ValueError(f'Source SHA-256 does not match the reviewed inventory: {path}')
        if digest in hashes_seen:
            raise ValueError('Exact duplicate source content; deduplicate the project sources first')
        hashes_seen.add(digest)
        meta = probe(path)
        video = next((s for s in meta['streams'] if s['codec_type'] == 'video'), None)
        if not video:
            raise ValueError(f'No video stream: {path}')
        # Select filters must normalize each source to this project fps first.
        if video.get('duration_ts') is not None and video.get('time_base'):
            duration = Fraction(int(video['duration_ts'])) * Fraction(video['time_base'])
            duration_basis = 'video duration_ts * time_base'
        else:
            duration = seconds(video.get('duration') or meta['format']['duration'])
            duration_basis = 'decimal video/container duration (check final-frame precision)'
        total = math.floor(duration * fps)
        if total < 1:
            raise ValueError(f'Source shorter than one project frame: {path}')
        original_seconds += float(duration)
        source_meta.append({'id': key, 'path': str(path), 'duration_seconds': float(duration),
                            'duration_basis': duration_basis,
                            'normalized_frames': total, 'source_sha256': digest})
        spans = source.get('spans', [])
        if not spans:
            raise ValueError(f'No spans for {key}; represent whole source explicitly')
        prev = 0
        for span in spans:
            a = rounded(seconds(span['start']) * fps)
            end = span['end']
            if end == 'end':
                b = total
            else:
                boundary = seconds(end) * fps
                b = rounded(boundary)
                # Permit only the sub-frame tail when the file's exact end is used.
                if b > total and seconds(end) <= duration:
                    b = total
            speed = span['speed']
            if isinstance(speed, bool) or not isinstance(speed, int) or speed not in (0, 1, 2):
                raise ValueError('speed must be 0 (remove), 1 or 2')
            if a != prev or b <= a or b > total:
                raise ValueError(f'{key}: spans must partition all normalized source frames without gaps/overlap; {a}:{b}, expected start {prev}, total {total}')
            reason = span.get('reason', '').strip()
            if not reason:
                raise ValueError('Every span needs a reason')
            count = 0 if speed == 0 else (b - a + speed - 1) // speed
            start_sample = rounded(Fraction(cursor * rate, 1) / fps)
            end_sample = rounded(Fraction((cursor + count) * rate, 1) / fps)
            rows.append({'source': key, 'source_path': str(path), 'source_start_frame': a,
                         'source_end_frame': b, 'source_start': float(Fraction(a, 1) / fps),
                         'source_end': float(Fraction(b, 1) / fps), 'speed': speed,
                         'reason': reason, 'output_start_frame': cursor,
                         'output_end_frame': cursor + count, 'output_frames': count,
                         'output_start': float(Fraction(cursor, 1) / fps),
                         'output_end': float(Fraction(cursor + count, 1) / fps),
                         'output_start_sample': start_sample, 'output_end_sample': end_sample,
                         'output_samples': end_sample - start_sample})
            cursor += count
            prev = b
        if prev != total:
            raise ValueError(f'{key}: missing tail; last span should end at "end"')
    if cursor == cover:
        raise ValueError('Timeline removes all source content')
    final_seconds = float(Fraction(cursor, 1) / fps)
    result = {'schema': 'mh-video-timeline/v1', 'project': spec.get('title', ''),
              'spec_path': str(spec_file), 'spec_sha256': sha256(spec_file),
              'fps': str(fps), 'sample_rate': rate, 'cover_frames': cover,
              'cover_samples': rounded(Fraction(cover * rate, 1) / fps),
              'sources': source_meta, 'rows': rows,
              'statistics': {'original_seconds': original_seconds,
                             'final_frames': cursor, 'final_seconds': final_seconds,
                             'final_samples': rounded(Fraction(cursor * rate, 1) / fps),
                             'reduction_percent': (1 - final_seconds / original_seconds) * 100}}
    output = fresh_dir(args.output_dir)
    new_json(output / 'timeline.json', result)
    new_json(output / 'statistics.json', result['statistics'])
    with (output / 'edits.csv').open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return {'report': str(output / 'timeline.json'), **result['statistics']}


def map_words(args):
    timeline = read_json(args.timeline)
    if timeline.get('schema') != 'mh-video-timeline/v1':
        raise ValueError('Expected compiled timeline')
    words = read_json(args.words)
    if not isinstance(words, list):
        raise ValueError('words JSON must be a flat list')
    sources = {s['id']: s for s in timeline['sources']}
    mapped, review = [], []
    for i, word in enumerate(words):
        key = word['source']
        if key not in sources:
            raise ValueError(f'Unknown source in word {i}: {key}')
        a, b = float(seconds(word['start'])), float(seconds(word['end']))
        if not b > a or b > sources[key]['duration_seconds'] + .001:
            raise ValueError(f'Invalid word interval at index {i}')
        text = word['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'Empty/nontext word at index {i}')
        pieces, removed, accelerated = [], False, False
        for index, row in enumerate(timeline['rows']):
            if row['source'] != key:
                continue
            lo, hi = max(a, row['source_start']), min(b, row['source_end'])
            if hi <= lo + 1e-9:
                continue
            if row['speed'] == 0:
                removed = True
                continue
            accelerated |= row['speed'] != 1
            pieces.append({'word_index': i, 'source': key, 'text': text, 'row_index': index,
                           'source_start': lo, 'source_end': hi,
                           'start': row['output_start'] + (lo-row['source_start'])/row['speed'],
                           'end': min(row['output_end'], row['output_start']+(hi-row['source_start'])/row['speed'])})
        mapped.extend(pieces)
        reasons = []
        if removed and not word.get('allow_removed', False):
            reasons.append('word_overlaps_removed_content')
        if len(pieces) > 1:
            reasons.append('word_split_across_edit_rows')
        if accelerated:
            reasons.append('transcribed_word_in_accelerated_region')
        tail_start = float(Fraction(sources[key]['normalized_frames'], 1) / Fraction(timeline['fps']))
        if b > tail_start + 1e-8:
            reasons.append('word_overlaps_subframe_source_tail')
        if not pieces and not removed:
            reasons.append('word_in_subframe_source_tail')
        if reasons:
            review.append({'word_index': i, 'source': key, 'start': a, 'end': b,
                           'text': text, 'reasons': reasons})
    mapped.sort(key=lambda w: (w['start'], w['end']))
    result = {'schema': 'mh-mapped-words/v1', 'mapped_words': mapped, 'review_required': review,
              'note': 'Split pieces retain original word text for review; do not concatenate them as subtitles.'}
    new_json(args.out, result)
    return {'mapped_pieces': len(mapped), 'review_items': len(review), 'report': str(args.out)}


def parse_srt(path, duration):
    text = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip()
    pattern = re.compile(r'^(\d+):(\d{2}):(\d{2})[,.](\d{3})$')
    def stamp(value):
        match = pattern.fullmatch(value.strip())
        if not match:
            raise ValueError(f'Invalid SRT timestamp: {value}')
        h, m, s, ms = map(int, match.groups())
        if m >= 60 or s >= 60:
            raise ValueError('Invalid minutes/seconds in SRT')
        return h*3600 + m*60 + s + ms/1000
    cues, previous = [], 0.
    for index, block in enumerate(re.split(r'\n\s*\n', text) if text else [], 1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(index):
            raise ValueError(f'Invalid or nonsequential SRT cue {index}')
        times = lines[1].split('-->')
        if len(times) != 2:
            raise ValueError(f'Invalid SRT range {index}')
        a, b = [stamp(t) for t in times]
        if not (a >= previous - .001 and b > a and b <= duration + .05):
            raise ValueError(f'Overlapping, negative, empty or out-of-range SRT cue {index}')
        if not '\n'.join(lines[2:]).strip():
            raise ValueError(f'Empty SRT cue {index}')
        cues.append({'start': a, 'end': b})
        previous = b
    if not cues:
        raise ValueError('SRT is empty')
    return {'cues': len(cues), 'first_start': cues[0]['start'], 'last_end': previous,
            'timing_checks_passed': True, 'semantic_sync_checked': False}


def verify_media(args):
    if args.out.exists():
        raise ValueError('Report exists; use a new output path')
    path = args.video.resolve(strict=True)
    meta = probe(path)
    video = next((s for s in meta['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in meta['streams'] if s['codec_type'] == 'audio'), None)
    if not video or not audio:
        raise ValueError('Expected a video stream and an audio stream')
    vd = float(video.get('duration') or meta['format']['duration'])
    ad = float(audio.get('duration') or meta['format']['duration'])
    fps = Fraction(video['avg_frame_rate'])
    tolerance = max(.1, 2/float(fps))
    checks = {'av_duration_match': abs(vd-ad) <= tolerance}
    if args.width is not None:
        checks['width'] = video['width'] == args.width
    if args.height is not None:
        checks['height'] = video['height'] == args.height
    if args.fps is not None:
        checks['fps'] = fps == Fraction(args.fps)
    if args.video_codec:
        checks['video_codec'] = video['codec_name'] == args.video_codec
    if args.audio_codec:
        checks['audio_codec'] = audio['codec_name'] == args.audio_codec
    if args.timeline:
        expected = read_json(args.timeline)['statistics']['final_seconds']
        checks['expected_duration'] = abs(vd - expected) <= tolerance
    result = {'schema': 'mh-media-qa/v1', 'video': str(path), 'video_sha256': sha256(path),
              'video_seconds': vd, 'audio_seconds': ad, 'fps': str(fps),
              'width': video['width'], 'height': video['height'],
              'video_codec': video['codec_name'], 'audio_codec': audio['codec_name'],
              'checks': checks, 'full_decode_performed': False,
              'visual_review_performed': False, 'listening_review_performed': False}
    if args.srt:
        try:
            result['subtitles'] = parse_srt(args.srt, vd)
            checks['srt_timing'] = True
        except ValueError as e:
            checks['srt_timing'] = False
            result['subtitle_error'] = str(e)
    if args.full_decode:
        log = args.out.with_suffix('.decode.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('x', encoding='utf-8') as f:
            done = subprocess.run([tool('ffmpeg'), '-nostdin', '-hide_banner', '-v', 'error',
                                   '-xerror', '-threads', '2', '-i', str(path),
                                   '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', os.devnull],
                                  stdout=subprocess.DEVNULL, stderr=f)
        result['full_decode_performed'] = True
        result['full_decode_exit_code'] = done.returncode
        result['full_decode_log'] = str(log)
        checks['full_decode'] = done.returncode == 0 and log.stat().st_size == 0
    result['technical_checks_passed'] = all(checks.values())
    result['status'] = ('technical_checks_passed' if args.full_decode else 'partial_technical_checks_passed') if all(checks.values()) else 'failed'
    new_json(args.out, result)
    if not all(checks.values()):
        raise ValueError(f'Technical QA failed; inspect {args.out}')
    return {'status': result['status'], 'report': str(args.out),
            'note': 'Visual, scientific and listening review are still separate.'}


def inspect_docx(args):
    if args.min_math < 0:
        raise ValueError('min-math must be nonnegative')
    ns = {'m': 'http://schemas.openxmlformats.org/officeDocument/2006/math',
          'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with ZipFile(args.docx) as z:
        bad = z.testzip()
        if bad:
            raise ValueError(f'Corrupt DOCX member: {bad}')
        xml = ET.fromstring(z.read('word/document.xml'))
        counts = {key: len(xml.findall('.//m:'+key, ns))
                  for key in ('oMath', 'oMathPara', 'sSub', 'sSup', 'f', 'nary', 'eqArr', 'm')}
        images = [{'member': n, 'sha256': hashlib.sha256(z.read(n)).hexdigest()}
                  for n in z.namelist() if n.startswith('word/media/') and not n.endswith('/')]
        # A literal LaTeX string inside m:r/m:t is still not a structured
        # fraction/integral; scan both ordinary and math runs.
        text = ''.join(x.text or '' for x in xml.iter()
                       if x.tag in ('{'+ns['w']+'}t', '{'+ns['m']+'}t'))
    cover_hash = sha256(args.cover) if args.cover else None
    checks = {'minimum_omml_count': counts['oMath'] >= args.min_math}
    if cover_hash:
        checks['exact_cover_bytes_embedded'] = any(x['sha256'] == cover_hash for x in images)
    result = {'schema': 'mh-docx-structure-qa/v1', 'docx': str(args.docx.resolve()),
              'docx_sha256': sha256(args.docx), 'math_counts': counts, 'media': images,
              'checks': checks, 'structure_checks_passed': all(checks.values()),
              'possible_raw_latex': re.findall(r'\\(?:frac|begin|end|int|sum|sqrt)\b', text),
              'render_review_performed': False, 'scientific_review_performed': False}
    new_json(args.out, result)
    if not all(checks.values()):
        raise ValueError(f'DOCX structural QA failed; inspect {args.out}')
    return {'native_equations': counts['oMath'], 'media_files': len(images), 'report': str(args.out)}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('inventory', help='Read-only hashes and ffprobe; no automatic keeper selection')
    p.add_argument('root', type=Path); p.add_argument('--out', required=True, type=Path)
    p.add_argument('--recursive', action='store_true'); p.set_defaults(run=inventory)
    p = commands.add_parser('compile', help='Compile explicitly reviewed spans; does not render video')
    p.add_argument('spec', type=Path); p.add_argument('--output-dir', required=True, type=Path)
    p.set_defaults(run=compile_project)
    p = commands.add_parser('map-words', help='Map flat word records and flag edit boundary issues')
    p.add_argument('timeline', type=Path); p.add_argument('words', type=Path)
    p.add_argument('--out', required=True, type=Path); p.set_defaults(run=map_words)
    p = commands.add_parser('verify-media', help='Probe/SRT/full decode; no perceptual or math claims')
    p.add_argument('video', type=Path); p.add_argument('--out', required=True, type=Path)
    p.add_argument('--timeline', type=Path); p.add_argument('--srt', type=Path)
    p.add_argument('--width', type=int); p.add_argument('--height', type=int)
    p.add_argument('--fps'); p.add_argument('--video-codec'); p.add_argument('--audio-codec')
    p.add_argument('--full-decode', action='store_true'); p.set_defaults(run=verify_media)
    p = commands.add_parser('inspect-docx', help='Check OOXML/math and exact embedded cover bytes')
    p.add_argument('docx', type=Path); p.add_argument('--cover', type=Path)
    p.add_argument('--min-math', type=int, default=0); p.add_argument('--out', required=True, type=Path)
    p.set_defaults(run=inspect_docx)
    args = parser.parse_args()
    try:
        print(json.dumps(args.run(args), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f'{type(e).__name__}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
