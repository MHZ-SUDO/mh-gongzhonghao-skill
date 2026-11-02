"""Export one final whole-video speed change, preserving pitch and retiming SRT.

Python 3.10+, ffmpeg and ffprobe. Default rate is exactly 5/4. The input is an
already edited video, not raw clips. Burned-in captions move with the picture;
an optional external SRT is independently mapped by t_final = t_base / rate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


MARKER = "mh-gongzhonghao-finalized;rate="


def rational(value, name):
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{name} must be a finite rational number with a nonzero denominator") from exc


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def tool(name):
    found = shutil.which(name)
    if not found:
        raise ValueError(f"{name} is not on the current PATH")
    return found


def probe(path):
    result = subprocess.run([tool("ffprobe"), "-v", "error", "-show_format",
                             "-show_streams", "-show_chapters", "-of", "json", str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise ValueError(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if data.get("chapters"):
        raise ValueError("Remove chapter metadata from the base edit first; this helper does not retime chapters")
    videos = [s for s in data["streams"] if s["codec_type"] == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audio) != 1:
        raise ValueError("This finalizer requires exactly one video and one audio stream")
    if any(s["codec_type"] == "subtitle" for s in data["streams"]):
        raise ValueError("Export embedded text subtitles to SRT first; this helper does not remux them")
    video, sound = videos[0], audio[0]
    fps = rational(video.get("avg_frame_rate") or video["r_frame_rate"], "source fps")
    if fps <= 0:
        raise ValueError("A valid source frame rate is required")
    durations = [Fraction(str(s.get("duration") or data["format"]["duration"]))
                 for s in (video, sound)]
    if min(durations) <= 0:
        raise ValueError("Positive video and audio durations are required")
    starts = [Fraction(str(s.get("start_time", "0"))) for s in (video, sound)]
    return {"video_seconds": float(durations[0]), "audio_seconds": float(durations[1]),
            "video_start_seconds": float(starts[0]), "audio_start_seconds": float(starts[1]),
            "av_start_difference_seconds": float(starts[1] - starts[0]),
            "fps": str(fps), "width": video["width"], "height": video["height"],
            "sample_rate": int(sound["sample_rate"]), "audio_codec": sound["codec_name"],
            "comment": data.get("format", {}).get("tags", {}).get("comment", "")}


def rounded(value):
    value = Fraction(value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def stamp_ms(value):
    match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Invalid SRT minutes or seconds")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def format_ms(value):
    seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def retime_srt(path, rate, source_seconds):
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    cues, mapping, previous, previous_final = [], [], 0, 0
    for index, block in enumerate(re.split(r"\n\s*\n", text) if text else [], 1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(index):
            raise ValueError(f"Invalid or nonsequential SRT cue {index}")
        parts = lines[1].split("-->")
        if len(parts) != 2:
            raise ValueError(f"Invalid SRT range {index}")
        start, end = map(stamp_ms, parts)
        if start < previous or end <= start or end > source_seconds * 1000 + 50:
            raise ValueError(f"Overlapping, empty or out-of-range SRT cue {index}")
        if not "\n".join(lines[2:]).strip():
            raise ValueError(f"Empty SRT cue {index}")
        final_start, final_end = [rounded(Fraction(v) / rate) for v in (start, end)]
        if final_start < previous_final or final_end <= final_start:
            raise ValueError(f"SRT cue {index} collapses after millisecond rounding; revise that cue")
        cues.append(f"{index}\n{format_ms(final_start)} --> {format_ms(final_end)}\n" + "\n".join(lines[2:]))
        mapping.append({"cue": index, "base_ms": [start, end], "final_ms": [final_start, final_end]})
        previous, previous_final = end, final_end
    if not cues:
        raise ValueError("SRT is empty")
    return "\n\n".join(cues) + "\n", mapping


def finalize(video, output, *, srt=None, output_srt=None, report=None, rate="5/4", fps=None):
    rate = rational(rate, "rate")
    if not Fraction(1, 2) <= rate <= 2:
        raise ValueError("This helper supports final rates from 1/2 to 2; default is 5/4")
    video = Path(video).resolve(strict=True)
    output = Path(output).resolve()
    srt = Path(srt).resolve(strict=True) if srt else None
    if output_srt and not srt:
        raise ValueError("--output-srt requires --srt")
    output_srt = Path(output_srt).resolve() if output_srt else (output.with_suffix(".srt") if srt else None)
    report = Path(report).resolve() if report else output.with_suffix(".finalize.json")
    targets = [p for p in (output, output_srt, report) if p is not None]
    if output.suffix.lower() != ".mp4":
        raise ValueError("The output must be a new .mp4 file")
    if len(set(targets)) != len(targets) or any(p in {video, srt} for p in targets):
        raise ValueError("Input/output/report paths must be distinct")
    if any(p.exists() for p in targets):
        raise ValueError("An output already exists; use a new version, never overwrite")
    source = probe(video)
    if MARKER in source["comment"] or video.with_suffix(".finalize.json").exists():
        raise ValueError("The input is already finalized; return to its base edit instead of applying speed twice")
    final_fps = rational(fps, "fps") if fps else rational(source["fps"], "source fps")
    if final_fps <= 0:
        raise ValueError("--fps must be positive")
    # A codec packet of start-time rounding is tolerated, not a real offset.
    start_tolerance = (1024 / source["sample_rate"] + .001
                       if source["audio_codec"] == "aac" else .001)
    if abs(source["av_start_difference_seconds"]) > start_tolerance:
        raise ValueError("Input audio/video start offset exceeds codec rounding; align the base edit first")
    tolerance = max(.1, 2 / float(Fraction(source["fps"])))
    if abs(source["video_seconds"] - source["audio_seconds"]) > tolerance:
        raise ValueError("Input audio/video durations differ; repair the base edit before finalization")
    subtitle_text, cue_mapping = retime_srt(srt, rate, source["video_seconds"]) if srt else (None, [])
    source_hash = sha256(video)
    srt_hash = sha256(srt) if srt else None
    expected = source["video_seconds"] / float(rate)
    data = {"schema": "mh-final-speed/v1", "status": "started",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "rate": str(rate), "time_map": "t_final = t_base / rate; millisecond half-up SRT rounding",
            "source": {"path": str(video), "sha256": source_hash, "metadata": source},
            "output": {"path": str(output)}, "statistics": {"final_seconds": expected},
            "expected_final_seconds": expected, "cue_mapping": cue_mapping,
            "visual_review_performed": False, "listening_review_performed": False,
            "pitch_preserving_filter": f"atempo={float(rate):.12g}",
            "source_start_tolerance_seconds": start_tolerance,
            "source_start_rounding_normalized_seconds": source["av_start_difference_seconds"],
            "full_decode_performed": False, "checks": {}}
    if srt:
        data["subtitles"] = {"source": str(srt), "source_sha256": srt_hash,
                             "output": str(output_srt), "cues": len(cue_mapping)}
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    filters = (f"[0:v:0]setpts=(PTS-STARTPTS)*{rate.denominator}/{rate.numerator},fps={final_fps}[v];"
               f"[0:a:0]asetpts=PTS-STARTPTS,atempo={float(rate):.12g}[a]")
    command = [tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", str(video),
               "-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-map_metadata", "0",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
               "-metadata", f"comment={MARKER}{rate}", str(output)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        data["ffmpeg_exit_code"] = process.returncode
        data["ffmpeg_error"] = process.stderr
        if process.returncode:
            raise ValueError("Final speed export failed; see the report and preserve the base input")
        actual = probe(output)
        data["output"].update({"sha256": sha256(output), "metadata": actual})
        checks = data["checks"]
        final_tolerance = max(.1, 2 / float(final_fps))
        checks["expected_duration"] = abs(actual["video_seconds"] - expected) <= final_tolerance
        checks["av_duration_match"] = abs(actual["video_seconds"] - actual["audio_seconds"]) <= final_tolerance
        checks["fps"] = Fraction(actual["fps"]) == final_fps
        checks["dimensions"] = all(actual[key] == source[key] for key in ("width", "height"))
        checks["source_unchanged"] = sha256(video) == source_hash
        if srt:
            checks["source_srt_unchanged"] = sha256(srt) == srt_hash
            checks["srt_within_final_duration"] = cue_mapping[-1]["final_ms"][1] <= (actual["video_seconds"] + .05) * 1000
            with output_srt.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(subtitle_text)
            data["subtitles"]["output_sha256"] = sha256(output_srt)
        decode = subprocess.run([tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                                 "-i", str(output), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        data.update({"full_decode_performed": True, "full_decode_exit_code": decode.returncode,
                     "full_decode_error": decode.stderr,
                     "actual_final_video_seconds": actual["video_seconds"],
                     "actual_final_audio_seconds": actual["audio_seconds"],
                     "av_duration_difference_seconds": actual["audio_seconds"] - actual["video_seconds"]})
        checks["full_decode"] = decode.returncode == 0 and not decode.stderr.strip()
        if not all(checks.values()):
            raise ValueError("Finalized media failed technical checks; see the report")
        data["status"] = "technical_checks_passed"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        data["status"] = "failed"
        data["error"] = str(exc)
        raise
    finally:
        with report.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return {"status": data["status"], "video": str(output), "srt": str(output_srt) if srt else None,
            "report": str(report), "rate": str(rate), "actual_seconds": actual["video_seconds"]}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--output-srt", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--rate", default="5/4")
    parser.add_argument("--fps")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(finalize(**vars(args)), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
