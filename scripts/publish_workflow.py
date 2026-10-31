#!/usr/bin/env python3
"""Freeze local publishing inputs and record one browser submission per kind.

This utility never opens a browser, uploads, clicks, scans a QR code, or publishes.
Python 3.10+, Pillow, lxml, and ffprobe are required for ``prepare``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import unquote
import uuid
import zipfile


SCHEMA_VERSION = 1
REVIEW_MAX_AGE_SECONDS = 600
MODES = {"video_and_article", "video_only", "article_only", "prepare_only"}
REVIEW_FLAGS = (
    "scope_verified", "preview_verified", "media_verified",
    "settings_verified", "existing_record_checked",
)
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("invalid_json", f"无法读取JSON：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError("invalid_json", f"JSON顶层必须是对象：{path}")
    return value


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2,
                                  allow_nan=False) + "\n").encode("utf-8"))


@contextmanager
def exclusive_lock(target: Path):
    """No retry or stale-lock removal: a second operation must stop."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkflowError(
            "locked", f"已有操作锁：{lock}。停止本次操作；不会自动重试或清除锁。"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at_utc": iso_utc()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def required(obj: dict[str, Any], keys: tuple[str, ...], location: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise WorkflowError("missing_fields", f"{location}缺少字段：{', '.join(missing)}")


def nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("invalid_field", f"{name}必须是非空字符串。")
    return value.strip()


def asset_path(value: Any, name: str) -> Path:
    value = nonempty_string(value, name)
    path = Path(value)
    if not path.is_absolute():
        raise WorkflowError("absolute_path_required", f"{name}必须使用绝对路径。")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError("missing_asset", f"{name}文件不存在：{path}") from exc
    if not path.is_file():
        raise WorkflowError("invalid_asset", f"{name}必须是普通文件：{path}")
    return path


def publish_intent(authorization: dict[str, Any], intended_kinds: set[str] | None = None) -> tuple[bool, str]:
    """Reject obvious placeholders/prepare-only requests, never prove user intent.

    The caller asserts ``publish`` only after checking the current conversation.
    There is intentionally no positive keyword/phrase whitelist: for example,
    “发本期” and “照录屏发这一期” can be valid instructions in context.
    """
    if authorization.get("requested_action") != "publish":
        return False, "requested_action不是publish。"
    instruction = authorization.get("user_instruction", "").strip()
    if not instruction:
        return False, "没有当次用户的明确发布指令。"
    lowered = instruction.lower()
    if lowered.strip("<>[]{}（）() ") in {
        "当次用户原话", "填写当次用户原话", "用户原话", "示例", "example",
        "todo", "tbd", "...", "待填写", "paste user instruction here",
    } or re.match(r"^(?:示例|example)\s*[:：]", lowered):
        return False, "用户原话仍是示例或占位内容。"
    negative = (
        r"(?:不要|不许|禁止|暂不|不必|别)(?:立即|现在|直接)?(?:发布|发表|群发|发出去)",
        r"(?:只|仅)(?:做|进行|先)?(?:准备|检查|整理|生成清单)",
        r"\b(?:do not|don't|never)\s+(?:publish|post)\b",
        r"\b(?:prepare|review|inspect)\s+only\b",
    )
    for pattern in negative:
        for match in re.finditer(pattern, lowered):
            # Preserve the actual quote: “发表视频，图文不要发表” authorizes
            # video while explicitly restricting the other type. Unqualified
            # restrictions still block every type. This guard never replaces
            # the caller's interpretation of the current conversation.
            boundary = r"[，,。.;；！？!?\n]|\b(?:but|while)\b|但是|不过|但"
            clause = (re.split(boundary, lowered[:match.start()])[-1]
                      + match.group() + re.split(boundary, lowered[match.end():])[0])
            mentioned = set()
            if re.search(r"视频|\bvideo\b", clause):
                mentioned.add("video")
            if re.search(r"图文|文章|\barticles?\b", clause):
                mentioned.add("article")
            if intended_kinds and mentioned and mentioned.isdisjoint(intended_kinds):
                continue
            return False, "用户原话含当前类型不发布或仅准备的限制。"
    meta_creation = re.search(
        r"(?:创建|编写|制作|生成|设计|完善|开发|安装|写|做).{0,35}"
        r"(?:skill|技能|脚本|workflow|工作流|流程|自动化|教程)|"
        r"\b(?:create|build|write|design|install)\b.{0,45}"
        r"\b(?:skill|script|workflow|automation|tutorial)\b", lowered,
    )
    if meta_creation:
        return False, "创建技能/脚本/流程的请求不构成本次内容发布授权。"
    return True, "publish标记附有非占位用户原话；助手仍须从当次会话核实授权，本脚本不能证明意图。"


def validate_config(config: dict[str, Any], preserved_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    required(config, ("schema_version", "mode", "episode", "title", "account_name",
                      "collection", "authorization", "settings"), "config")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise WorkflowError("schema_version", "仅支持schema_version=1。")
    if not isinstance(config["mode"], str) or config["mode"] not in MODES:
        raise WorkflowError("mode", f"mode必须为：{', '.join(sorted(MODES))}")
    normalized = dict(config)
    for key in ("episode", "title", "account_name"):
        normalized[key] = nonempty_string(config[key], key)
    normalized["collection"] = (None if config["collection"] is None
                                else nonempty_string(config["collection"], "collection"))
    authorization = config["authorization"]
    settings = config["settings"]
    if not isinstance(authorization, dict) or not isinstance(settings, dict):
        raise WorkflowError("invalid_field", "authorization和settings必须是对象。")
    required(authorization, ("requested_action", "user_instruction"), "authorization")
    if not isinstance(authorization["requested_action"], str) or authorization["requested_action"] not in {"prepare", "publish"}:
        raise WorkflowError("authorization", "requested_action只能为prepare或publish。")
    normalized["authorization"] = {
        "requested_action": authorization["requested_action"],
        "user_instruction": nonempty_string(authorization["user_instruction"],
                                            "authorization.user_instruction"),
    }
    required(settings, ("group_notify", "schedule", "sync_video_channel",
                        "original_author"), "settings")
    if settings["group_notify"] is not False or settings["schedule"] is not None:
        raise WorkflowError("settings", "本工作流要求group_notify=false且schedule=null。")
    if type(settings["sync_video_channel"]) is not bool:
        raise WorkflowError("settings", "sync_video_channel必须是JSON布尔值。")
    normalized["settings"] = {
        "group_notify": False, "schedule": None,
        "sync_video_channel": settings["sync_video_channel"],
        "original_author": (None if settings["original_author"] is None
                            else nonempty_string(settings["original_author"], "original_author")),
    }
    limits = config.get("upload_limits", {})
    if not isinstance(limits, dict):
        raise WorkflowError("upload_limits", "upload_limits必须为对象。")
    duration_limit = limits.get("video_duration_less_than_seconds", 3600)
    article_limit = limits.get("docx_conservative_max_bytes", 15_000_000)
    if (type(duration_limit) not in {int, float} or not math.isfinite(duration_limit)
            or duration_limit <= 0 or type(article_limit) is not int or article_limit <= 0):
        raise WorkflowError("upload_limits", "视频阈值须为正有限秒数，DOCX阈值须为正整数字节数。")
    normalized["upload_limits"] = {
        "video_duration_less_than_seconds": duration_limit,
        "docx_conservative_max_bytes": article_limit,
    }
    requirements = {
        "video_and_article": {"video", "article", "cover"},
        "video_only": {"video", "cover"},
        "article_only": {"article", "cover"},
        "prepare_only": {"cover"},
    }[config["mode"]]
    selected = {"video", "article", "cover"} if config["mode"] == "prepare_only" else requirements
    paths = {}
    for key in sorted(selected):
        if config.get(key) is not None:
            if preserved_inputs and key in preserved_inputs:
                candidate = Path(nonempty_string(config[key], key))
                previous = Path(preserved_inputs[key]["path"])
                if not candidate.is_absolute() or candidate.resolve() != previous.resolve():
                    raise WorkflowError("locked_asset_changed", f"已提交/认领的{key}不能更换素材路径。")
                paths[key] = previous
            else:
                paths[key] = asset_path(config[key], key)
    if config["mode"] == "prepare_only" and not ({"video", "article"} & paths.keys()):
        raise WorkflowError("missing_asset", "prepare_only仍需提供video或article，及cover。")
    missing = requirements - paths.keys()
    if missing:
        raise WorkflowError("missing_asset", f"当前模式缺少素材：{', '.join(sorted(missing))}")
    references = config.get("reference_recordings", [])
    if not isinstance(references, list):
        raise WorkflowError("reference_recordings", "reference_recordings必须是绝对路径列表。")
    normalized["asset_paths"] = paths
    normalized["reference_paths"] = [asset_path(v, "reference_recordings") for v in references]
    return normalized


def inspect_video(path: Path, duration_limit: float = 3600) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise WorkflowError("dependency", "未找到ffprobe，请将它加入PATH。")
    try:
        process = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("probe_timeout", "ffprobe超时；不会继续准备发布。") from exc
    if process.returncode:
        raise WorkflowError("invalid_video", f"ffprobe无法读取视频：{path}")
    try:
        probe = json.loads(process.stdout)
        streams = probe.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise ValueError("没有视频轨")
        stream = videos[0]
        duration = float(probe.get("format", {}).get("duration", stream.get("duration", 0)))
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        if not math.isfinite(duration) or not 0 < duration < duration_limit or min(width, height) <= 0:
            raise ValueError(f"视频时长应大于0且小于配置阈值{duration_limit}秒，分辨率须有效")
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowError("invalid_video", f"视频不符合本地检查阈值：{exc}") from exc
    return {
        "duration_seconds": duration, "width": width, "height": height,
        "codec_name": stream.get("codec_name"),
        "frame_rate": stream.get("avg_frame_rate", stream.get("r_frame_rate")),
        "format_name": probe.get("format", {}).get("format_name"),
        "audio_stream_count": sum(s.get("codec_type") == "audio" for s in streams),
    }


def inspect_cover(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
        with Image.open(path) as image:
            result = {"width": image.width, "height": image.height,
                      "format": image.format, "mode": image.mode}
            image.verify()
    except ImportError as exc:
        raise WorkflowError("dependency", "prepare需要Pillow。") from exc
    except Exception as exc:
        raise WorkflowError("invalid_cover", f"无法读取封面：{path}") from exc
    return result


def inspect_article(path: Path, max_bytes: int = 15_000_000) -> dict[str, Any]:
    if path.suffix.lower() != ".docx":
        raise WorkflowError("invalid_article", "article必须是DOCX文件。")
    if path.stat().st_size > max_bytes:
        raise WorkflowError("article_too_large", f"DOCX超过配置的本地检查阈值{max_bytes}字节。")
    try:
        from lxml import etree
    except ImportError as exc:
        raise WorkflowError("dependency", "prepare需要lxml。") from exc
    try:
        with zipfile.ZipFile(path) as package:
            if package.testzip() is not None:
                raise ValueError("ZIP CRC检查失败")
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            root = etree.fromstring(package.read("word/document.xml"), parser)
            bodies = root.xpath("./w:body", namespaces=NS)
            if not bodies:
                raise ValueError("没有Word正文")
            body = bodies[0]
            paragraphs = body.xpath(".//w:p", namespaces=NS)
            text_paragraphs = ["".join(p.xpath(".//w:t/text()", namespaces=NS)).strip()
                               for p in paragraphs]
            nonempty = [p for p in text_paragraphs if p]
            rel_root = etree.fromstring(package.read("word/_rels/document.xml.rels"), parser)
            rels = {r.get("Id"): r for r in rel_root}
            embeds = body.xpath(".//a:blip/@r:embed", namespaces=NS)
            targets = []
            for rid in embeds:
                rel = rels.get(rid)
                if rel is None or rel.get("TargetMode") == "External":
                    raise ValueError("正文内嵌图关系缺失或误指向外部")
                target = posixpath.normpath(posixpath.join("word", rel.get("Target", "")))
                if target not in package.namelist():
                    raise ValueError("正文内嵌图文件缺失")
                targets.append(target)
            title = None
            for p, text in zip(paragraphs, text_paragraphs):
                style = p.xpath("string(./w:pPr/w:pStyle/@w:val)", namespaces=NS)
                if style == "Title" and text:
                    title = text
                    break
            if title is None:
                candidates = []
                for i, (p, text) in enumerate(zip(paragraphs[:12], text_paragraphs[:12])):
                    if not text:
                        continue
                    sizes = p.xpath(".//w:rPr/w:sz/@w:val", namespaces=NS)
                    size = max([int(s) for s in sizes if str(s).isdigit()] or [0])
                    candidates.append((size, -i, text))
                title = max(candidates)[2] if candidates else ""
            text = "\n".join(nonempty)
            return {
                "document_title": title,
                "first_paragraphs": nonempty[:6],
                "body_summary": text[:900],
                "body_paragraph_count": len(paragraphs),
                "body_han_character_count": len(re.findall(r"[\u4e00-\u9fff]", text)),
                "body_embedded_image_references": len(embeds),
                "body_unique_embedded_images": len(set(targets)),
                "body_external_image_references": len(body.xpath(".//a:blip/@r:link", namespaces=NS)),
                "body_omml_group_count": len(body.xpath(".//m:oMath", namespaces=NS)),
                "body_table_count": len(body.xpath(".//w:tbl", namespaces=NS)),
                "headings": [s for s in nonempty if re.match(r"^\d{1,2}\s", s)],
                "raw_latex_suspected": bool(re.search(r"\\(?:frac|begin|int|sum|psi|omega)\b", text)),
            }
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise WorkflowError("invalid_article", f"DOCX包结构检查失败：{exc}") from exc


def inspect_asset(path: Path, kind: str, limits: dict[str, Any] | None = None) -> dict[str, Any]:
    before = file_hash(path)
    limits = limits or {}
    if kind == "video":
        metadata = inspect_video(path, limits.get("video_duration_less_than_seconds", 3600))
    elif kind == "article":
        metadata = inspect_article(path, limits.get("docx_conservative_max_bytes", 15_000_000))
    else:
        metadata = inspect_cover(path)
    after = file_hash(path)
    if before != after:
        raise WorkflowError("asset_changed", f"检查过程中素材发生变化：{path}")
    return {"path": str(path), "sha256": after, "size_bytes": path.stat().st_size,
            "metadata": metadata}


def manifest_payload(config: dict[str, Any], preserved_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = validate_config(config, preserved_inputs)
    references = [{"path": str(p), "sha256": file_hash(p), "size_bytes": p.stat().st_size}
                  for p in normalized["reference_paths"]]
    paths = normalized["asset_paths"]
    if "video" in paths and any(paths["video"] == p for p in normalized["reference_paths"]):
        raise WorkflowError("reference_as_video", "待发布视频不能是参考操作录屏。")
    inputs = {kind: (preserved_inputs[kind] if preserved_inputs and kind in preserved_inputs
                     else inspect_asset(path, kind, normalized["upload_limits"])) for kind, path in paths.items()}
    if "video" in inputs and inputs["video"]["sha256"] in {r["sha256"] for r in references}:
        raise WorkflowError("reference_as_video", "待发布视频与参考操作录屏内容相同，即使改名也不能发布。")
    publish_kinds = {
        "video_and_article": ["video", "article"], "video_only": ["video"],
        "article_only": ["article"], "prepare_only": [],
    }[normalized["mode"]]
    allowed, reason = publish_intent(normalized["authorization"], set(publish_kinds))
    if not publish_kinds:
        allowed, reason = False, "prepare_only模式禁止commit。"
    return {
        "schema_version": 1,
        **{k: normalized[k] for k in ("mode", "episode", "title", "account_name", "collection")},
        "authorization": normalized["authorization"], "settings": normalized["settings"],
        "authorization_check": {"publish_allowed": allowed, "reason": reason},
        "publish_kinds": publish_kinds, "inputs": inputs, "reference_recordings": references,
        "upload_limits": normalized["upload_limits"],
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    required(manifest, ("schema_version", "manifest_id", "frozen", "created_at_utc"), "manifest")
    if manifest["schema_version"] != 1 or not isinstance(manifest["frozen"], dict):
        raise WorkflowError("manifest_schema", "manifest格式不正确。")
    if object_hash(manifest["frozen"]) != manifest["manifest_id"]:
        raise WorkflowError("manifest_changed", "manifest冻结内容与manifest_id不一致。")


def preparation_note(manifest: dict[str, Any]) -> str:
    frozen = manifest["frozen"]
    lines = [f"# 第{frozen['episode']}期本地检查说明", "",
             f"- 标题：{frozen['title']}", f"- 公众号：{frozen['account_name']}",
             f"- 合集：{frozen['collection']}", f"- 模式：{frozen['mode']}",
             f"- 清单ID：{manifest['manifest_id']}",
             f"- 当前发布意图检查：{frozen['authorization_check']['reason']}", "",
             "## 冻结素材", ""]
    for kind, item in frozen["inputs"].items():
        lines += [f"### {kind}", "", f"路径：{item['path']}", "",
                  f"字节数：{item['size_bytes']}；SHA256：{item['sha256']}", "",
                  "```json", json.dumps(item["metadata"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 下一步约束", "",
              "本命令只读取素材并生成本地清单，不上传、不点击网页、不发表。",
              "每个video/article需单独进行即时页面核验和10分钟内的review。",
              "commit先记录一次attempted，实际网页操作由浏览器执行；超时或失败均不允许再commit。",
              "pending表示等待回执，不能报告为发表成功。不得通过改名、删状态或换状态文件重试。", ""]
    return "\n".join(lines)


def prepare(config_path: Path, output_dir: Path) -> dict[str, Any]:
    return save_preparation(config_path, output_dir, manifest_payload(read_json(config_path)))


def save_preparation(config_path: Path, output_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    proposed = {"schema_version": 1, "manifest_id": object_hash(payload),
                "frozen": payload, "created_at_utc": iso_utc(),
                "config_file": str(config_path.resolve())}
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "manifest.json"
    with exclusive_lock(target):
        if target.exists():
            existing = read_json(target)
            validate_manifest(existing)
            if existing["frozen"] != payload:
                raise WorkflowError("manifest_conflict", "输出目录已有不同清单；拒绝覆盖。")
            manifest = existing
        else:
            manifest = proposed
        note = output_dir / "本期检查说明.md"
        content = preparation_note(manifest).encode("utf-8")
        if note.exists() and note.read_bytes() != content:
            raise WorkflowError("note_conflict", "本期检查说明已存在不同内容；拒绝覆盖。")
        if not target.exists():
            write_json(target, manifest)
        if not note.exists():
            atomic_write(note, content)
    return {"command": "prepare", "manifest": str(target.resolve()),
            "manifest_id": manifest["manifest_id"], "uploaded": False,
            "publish_allowed_by_text_guard": payload["authorization_check"]["publish_allowed"]}


def load_state(path: Path, verify_manifest_file: bool = True) -> dict[str, Any]:
    state = read_json(path)
    required(state, ("schema_version", "manifest", "manifest_path", "manifest_file_sha256",
                     "items", "events"), "state")
    if state["schema_version"] != 1:
        raise WorkflowError("state_schema", "仅支持状态schema_version=1。")
    validate_manifest(state["manifest"])
    if not isinstance(state["items"], dict) or not isinstance(state["events"], list):
        raise WorkflowError("state_schema", "state.items/events格式错误。")
    if verify_manifest_file:
        manifest_path = Path(state["manifest_path"])
        if not manifest_path.is_file() or file_hash(manifest_path) != state["manifest_file_sha256"]:
            raise WorkflowError("manifest_changed", "已冻结的manifest文件缺失或被修改，停止操作。")
        if read_json(manifest_path) != state["manifest"]:
            raise WorkflowError("manifest_changed", "state与manifest快照不一致。")
    return state


def init_state(manifest_path: Path, state_path: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    validate_manifest(manifest)
    digest = file_hash(manifest_path)
    with exclusive_lock(state_path):
        if state_path.exists():
            state = load_state(state_path)
            if state["manifest_file_sha256"] != digest or state["manifest"] != manifest:
                raise WorkflowError("state_conflict", "已有state绑定了不同清单；拒绝覆盖或重置。")
            return {"command": "init", "state": str(state_path.resolve()), "unchanged": True}
        state = {
            "schema_version": 1, "created_at_utc": iso_utc(), "updated_at_utc": iso_utc(),
            "manifest_path": str(manifest_path.resolve()), "manifest_file_sha256": digest,
            "manifest": manifest,
            "items": {kind: {"status": "not_attempted", "review": None,
                             "attempt": None, "external_observation": None, "receipts": []}
                      for kind in manifest["frozen"]["publish_kinds"]},
            "events": [],
        }
        write_json(state_path, state)
    return {"command": "init", "state": str(state_path.resolve()), "unchanged": False}


def get_item(state: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind not in {"video", "article"} or kind not in state["items"]:
        raise WorkflowError("kind_not_in_scope", f"当前清单不允许{kind}操作。")
    return state["items"][kind]


def already_attempted(state: dict[str, Any], kind: str) -> bool:
    item = get_item(state, kind)
    return item.get("attempt") is not None or any(
        e.get("action") == "commit" and e.get("kind") == kind for e in state["events"]
    )


def existing_record_adopted(state: dict[str, Any], kind: str) -> bool:
    item = get_item(state, kind)
    return item.get("external_observation") is not None or any(
        e.get("action") == "adopt" and e.get("kind") == kind for e in state["events"]
    )


def submission_locked(state: dict[str, Any], kind: str) -> bool:
    return already_attempted(state, kind) or existing_record_adopted(state, kind)


def require_unused_submission(state: dict[str, Any], kind: str) -> None:
    if already_attempted(state, kind):
        raise WorkflowError("already_attempted", "此kind已commit；后续只可record实际回执，不可重试或adopt。")
    if existing_record_adopted(state, kind):
        raise WorkflowError("existing_record_adopted", "此kind已认领线上既有记录；只可record后续回执，不可review、commit或再次adopt。")


def refresh(state_path: Path, config_path: Path, output_dir: Path) -> dict[str, Any]:
    """Revise unsubmitted material/authorization without resetting submission history."""
    with exclusive_lock(state_path):
        state = load_state(state_path)
        old = state["manifest"]["frozen"]
        config = read_json(config_path)
        for field in ("account_name", "episode"):
            if config.get(field) != old[field]:
                raise WorkflowError("refresh_identity", f"refresh不能更换{field}；请定位正确的本期记录。")
        locked = {kind for kind in state["items"] if submission_locked(state, kind)}
        preserved = {kind: old["inputs"][kind] for kind in locked}
        for kind, asset in preserved.items():
            if config.get(kind) is not None:
                candidate = Path(nonempty_string(config[kind], kind))
                if not candidate.is_absolute() or candidate.resolve() != Path(asset["path"]).resolve():
                    raise WorkflowError("locked_asset_changed", f"已提交/认领的{kind}不能替换。")
            config[kind] = asset["path"]
        payload = manifest_payload(config, preserved)
        if locked:
            for field in ("title", "collection", "settings"):
                if payload[field] != old[field]:
                    raise WorkflowError("locked_metadata_changed", f"已存在提交/认领，不能改写其{field}。")
            if payload["inputs"]["cover"]["sha256"] != old["inputs"]["cover"]["sha256"]:
                raise WorkflowError("locked_asset_changed", "共享封面已用于提交/认领，不能更换其内容。")
        # A locked item remains in the audit snapshot even when the new scope
        # only authorizes the other type. Its local file need not still exist.
        payload["inputs"].update(preserved)
        if output_dir.exists():
            raise WorkflowError("refresh_output_exists", "refresh须使用新的预检目录；不覆盖旧清单或重复执行。")
        prepared = save_preparation(config_path, output_dir, payload)
        new_manifest_path = Path(prepared["manifest"])
        new_manifest = read_json(new_manifest_path)
        previous_path = output_dir / "previous-state.json"
        write_json(previous_path, state)
        reset = []
        for kind in set(state["items"]) | set(payload["publish_kinds"]):
            if kind not in locked:
                state["items"][kind] = {"status": "not_attempted", "review": None,
                                        "attempt": None, "external_observation": None,
                                        "receipts": []}
                reset.append(kind)
        state["events"].append({
            "action": "refresh", "at_utc": iso_utc(),
            "previous_manifest_id": state["manifest"]["manifest_id"],
            "previous_state": str(previous_path.resolve()),
            "previous_state_sha256": file_hash(previous_path),
            "preserved_kinds": sorted(locked), "reset_reviews": sorted(reset),
        })
        state["manifest"] = new_manifest
        state["manifest_path"] = str(new_manifest_path.resolve())
        state["manifest_file_sha256"] = file_hash(new_manifest_path)
        state["updated_at_utc"] = iso_utc()
        write_json(state_path, state)
    return {"command": "refresh", "state": str(state_path.resolve()),
            "manifest": str(new_manifest_path.resolve()), "preserved_kinds": sorted(locked),
            "reset_reviews": sorted(reset), "browser_action_performed": False}


def check_external_observation(evidence: dict[str, Any], frozen: dict[str, Any], kind: str) -> None:
    required(evidence, ("account", "title", "kind", "status", "platform_identity",
                        "existing_record_checked", "observed_at", "notes"), "adopt evidence")
    if (evidence["account"] != frozen["account_name"]
            or evidence["title"] != frozen["title"] or evidence["kind"] != kind):
        raise WorkflowError("adopt_identity", "线上观察的账号、标题或kind与清单不一致。")
    if evidence["status"] not in ("pending", "published"):
        raise WorkflowError("adopt_status", "adopt只能认领pending或published的线上既有记录。")
    if evidence["existing_record_checked"] is not True:
        raise WorkflowError("adopt_incomplete", "必须已检查线上既有记录（existing_record_checked=true）。")
    identity = evidence["platform_identity"]
    if not isinstance(identity, str) or not identity.strip():
        raise WorkflowError("adopt_platform_identity", "platform_identity必须是非空平台记录标识或公开链接。")
    decoded = unquote(unquote(identity)).lower()
    if re.search(
        r"\b(?:token|access_token|pass_ticket|ticket|session(?:id|_id|_key)?|sid|wxsid)"
        r"\s*[\"']?\s*[:=]", decoded,
    ):
        raise WorkflowError("adopt_platform_identity", "platform_identity含会话/认证参数；请仅保存记录ID或无会话参数的公开链接。")
    nonempty_string(evidence["notes"], "adopt.notes")
    value = evidence["observed_at"]
    if not isinstance(value, str):
        raise WorkflowError("adopt_time", "observed_at必须是ISO UTC字符串。")
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("必须明确使用UTC（Z或+00:00）")
        age = (utc_now() - stamp).total_seconds()
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkflowError("adopt_time", f"无效UTC观察时间：{exc}") from exc
    if not 0 <= age <= REVIEW_MAX_AGE_SECONDS:
        raise WorkflowError("adopt_expired", "线上观察不在过去10分钟内，或时间在未来。")


def adopt(state_path: Path, kind: str, evidence_path: Path) -> dict[str, Any]:
    """Observe a pre-existing platform record, without claiming a local submit.

    This command intentionally needs no publication authorization. It records
    no new ``attempt`` and permanently locks that kind against a new commit.
    """
    evidence = read_json(evidence_path)
    with exclusive_lock(state_path):
        state = load_state(state_path)
        frozen = state["manifest"]["frozen"]
        observable = (kind in frozen["inputs"] if frozen["mode"] == "prepare_only"
                      else kind in frozen["publish_kinds"])
        if kind not in {"video", "article"} or not observable:
            raise WorkflowError("kind_not_in_scope", f"当前清单没有可认领的{kind}素材范围。")
        # A prepare_only state starts without publishable items. A read-only
        # adoption can add an observed item but cannot make it committable.
        if kind not in state["items"]:
            state["items"][kind] = {"status": "not_attempted", "review": None,
                                    "attempt": None, "external_observation": None,
                                    "receipts": []}
        item = get_item(state, kind)
        require_unused_submission(state, kind)
        check_external_observation(evidence, frozen, kind)
        observation = {
            "evidence": evidence, "recorded_at_utc": iso_utc(),
            "evidence_file": str(evidence_path.resolve()),
            "evidence_sha256": object_hash(evidence),
        }
        item["external_observation"] = observation
        item["status"] = evidence["status"]
        state["updated_at_utc"] = iso_utc()
        state["events"].append({
            "action": "adopt", "kind": kind, "at_utc": iso_utc(),
            "status": evidence["status"], "platform_identity": evidence["platform_identity"],
        })
        write_json(state_path, state)
    return {
        "command": "adopt", "kind": kind, "status": evidence["status"],
        "submitted_here": False, "existing_record_adopted": True,
        "publication_confirmed": evidence["status"] == "published",
        "retry_allowed": False, "browser_action_performed": False,
    }


def check_review(evidence: dict[str, Any], frozen: dict[str, Any], expected_kind: str) -> float:
    required(evidence, ("account", "title", "editor_kind", "editor_identity",
                        "reviewed_at") + REVIEW_FLAGS, "review evidence")
    if evidence["account"] != frozen["account_name"] or evidence["title"] != frozen["title"]:
        raise WorkflowError("review_identity", "review账号或标题与冻结清单不一致。")
    if evidence["editor_kind"] != expected_kind:
        raise WorkflowError("review_kind", "review的editor_kind与本次video/article操作不一致。")
    editor_identity = evidence["editor_identity"]
    if not isinstance(editor_identity, str) or not editor_identity.strip():
        raise WorkflowError("review_editor_identity", "editor_identity必须是非空的当前编辑器标识。")
    if re.search(
        r"\b(?:token|access_token|pass_ticket|ticket|session(?:id|_id|_key)?|sid|wxsid)"
        r"\s*[\"']?\s*[:=]", unquote(unquote(editor_identity)).lower(),
    ):
        raise WorkflowError("review_editor_identity", "editor_identity不能保存会话/认证参数。")
    if any(evidence.get(flag) is not True for flag in REVIEW_FLAGS):
        raise WorkflowError("review_incomplete", "五项页面核验必须全部为JSON true。")
    value = evidence["reviewed_at"]
    if not isinstance(value, str):
        raise WorkflowError("review_time", "reviewed_at必须是ISO UTC字符串。")
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("必须明确使用UTC（Z或+00:00）")
        age = (utc_now() - stamp).total_seconds()
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkflowError("review_time", f"无效UTC核验时间：{exc}") from exc
    if not 0 <= age <= REVIEW_MAX_AGE_SECONDS:
        raise WorkflowError("review_expired", "review不在过去10分钟内，或时间在未来。")
    return age


def review_state(state_path: Path, kind: str, evidence_path: Path) -> dict[str, Any]:
    evidence = read_json(evidence_path)
    with exclusive_lock(state_path):
        state = load_state(state_path)
        item = get_item(state, kind)
        require_unused_submission(state, kind)
        if kind not in state["manifest"]["frozen"]["publish_kinds"]:
            raise WorkflowError("kind_not_in_scope", f"当前授权范围不包含{kind}。")
        check_review(evidence, state["manifest"]["frozen"], kind)
        item["review"] = {"evidence": evidence, "recorded_at_utc": iso_utc(),
                          "evidence_file": str(evidence_path.resolve()),
                          "evidence_sha256": object_hash(evidence)}
        item["status"] = "reviewed"
        state["updated_at_utc"] = iso_utc()
        state["events"].append({"action": "review", "kind": kind, "at_utc": iso_utc()})
        write_json(state_path, state)
    return {"command": "review", "kind": kind, "status": "reviewed",
            "reviewed_at": evidence["reviewed_at"]}


def verify_frozen_assets(frozen: dict[str, Any], kind_to_submit: str | None = None) -> None:
    reference_hashes = {r["sha256"] for r in frozen.get("reference_recordings", [])}
    for kind, asset in frozen["inputs"].items():
        if kind_to_submit is not None and kind not in {kind_to_submit, "cover"}:
            continue
        path = Path(asset["path"])
        if not path.is_file() or file_hash(path) != asset["sha256"]:
            raise WorkflowError("asset_changed", f"冻结素材缺失或SHA256发生变化：{kind}：{path}")
        if kind == "video" and asset["sha256"] in reference_hashes:
            raise WorkflowError("reference_as_video", "参考操作录屏不能作为待发布视频。")


def commit(state_path: Path, kind: str) -> dict[str, Any]:
    with exclusive_lock(state_path):
        state = load_state(state_path)
        item = get_item(state, kind)
        require_unused_submission(state, kind)
        frozen = state["manifest"]["frozen"]
        if kind not in frozen["publish_kinds"]:
            raise WorkflowError("kind_not_in_scope", f"当前授权范围不包含{kind}。")
        allowed, reason = publish_intent(frozen["authorization"], {kind})
        if frozen["mode"] == "prepare_only" or not allowed:
            raise WorkflowError("not_authorized", f"不能commit：{reason}")
        if not item.get("review"):
            raise WorkflowError("review_required", "此kind尚无即时review。")
        review = item["review"]
        if object_hash(review["evidence"]) != review["evidence_sha256"]:
            raise WorkflowError("review_changed", "review证据与保存时的摘要不一致。")
        check_review(review["evidence"], frozen, kind)
        verify_frozen_assets(frozen, kind)
        check_review(review["evidence"], frozen, kind)
        # Save and fsync the attempted marker BEFORE the agent clicks a web button.
        attempt = {"id": uuid.uuid4().hex, "at_utc": iso_utc(),
                   "reviewed_at": review["evidence"]["reviewed_at"]}
        item["attempt"] = attempt
        item["status"] = "attempted"
        state["updated_at_utc"] = iso_utc()
        state["events"].append({"action": "commit", "kind": kind,
                                "at_utc": attempt["at_utc"], "attempt_id": attempt["id"]})
        write_json(state_path, state)
    return {"command": "commit", "kind": kind, "status": "attempted",
            "attempt_id": attempt["id"], "publication_confirmed": False,
            "retry_allowed": False, "browser_action_performed": False}


def record(state_path: Path, kind: str, status: str, evidence: str) -> dict[str, Any]:
    if status not in {"pending", "published", "failed"}:
        raise WorkflowError("record_status", "回执状态只能为pending、published、failed。")
    evidence = nonempty_string(evidence, "record.evidence")
    with exclusive_lock(state_path):
        state = load_state(state_path)
        item = get_item(state, kind)
        if not submission_locked(state, kind):
            raise WorkflowError("attempt_required", "尚无commit或adopt，不能记录线上后续回执。")
        receipt = {"status": status, "evidence": evidence, "recorded_at_utc": iso_utc()}
        item["receipts"].append(receipt)
        item["status"] = status
        state["updated_at_utc"] = iso_utc()
        state["events"].append({"action": "record", "kind": kind, **receipt})
        write_json(state_path, state)
    return {"command": "record", "kind": kind, "status": status,
            "publication_confirmed": status == "published", "retry_allowed": False,
            "submitted_here": already_attempted(state, kind),
            "existing_record_adopted": existing_record_adopted(state, kind)}


def status_state(state_path: Path) -> dict[str, Any]:
    state = load_state(state_path, verify_manifest_file=False)
    frozen = state["manifest"]["frozen"]
    result = {"command": "status", "manifest_id": state["manifest"]["manifest_id"],
              "account_name": frozen["account_name"], "title": frozen["title"], "items": {}}
    for kind, item in state["items"].items():
        review_valid = False
        review_error = None
        if item.get("review"):
            try:
                check_review(item["review"]["evidence"], frozen, kind)
                review_valid = True
            except WorkflowError as exc:
                review_error = exc.code
        result["items"][kind] = {
            "status": item["status"], "attempt": item.get("attempt"),
            "publication_confirmed": item["status"] == "published",
            "review_valid_now": review_valid, "review_error": review_error,
            "last_receipt": item["receipts"][-1] if item.get("receipts") else None,
            "attempt_recorded": already_attempted(state, kind), "retry_allowed": False,
            "submitted_here": already_attempted(state, kind),
            "existing_record_adopted": existing_record_adopted(state, kind),
            "submission_locked": submission_locked(state, kind),
            "external_observation": item.get("external_observation"),
        }
    expected_kinds = set(frozen["publish_kinds"]) | {
        kind for kind in state["items"] if submission_locked(state, kind)
    }
    result["all_publications_confirmed"] = bool(frozen["publish_kinds"]) and all(
        result["items"].get(kind, {}).get("publication_confirmed", False)
        for kind in expected_kinds
    )
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="只读检查素材并冻结清单，不上传")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("init", help="初始化状态；不会覆盖/重置已有状态")
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--state", required=True, type=Path)
    p = sub.add_parser("refresh", help="保留提交历史，仅更新未提交素材/当次授权；须新预检目录")
    p.add_argument("--state", required=True, type=Path)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    for command in ("review", "commit", "record", "status", "adopt"):
        p = sub.add_parser(command)
        p.add_argument("--state", required=True, type=Path)
        if command != "status":
            p.add_argument("--kind", required=True, choices=["video", "article"])
        if command in {"review", "adopt"}:
            p.add_argument("--evidence", required=True, type=Path)
        if command == "record":
            p.add_argument("--status", required=True, choices=["pending", "published", "failed"])
            p.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.config.resolve(), args.out.resolve())
        elif args.command == "init":
            result = init_state(args.manifest.resolve(), args.state.resolve())
        elif args.command == "refresh":
            result = refresh(args.state.resolve(), args.config.resolve(), args.out.resolve())
        elif args.command == "review":
            result = review_state(args.state.resolve(), args.kind, args.evidence.resolve())
        elif args.command == "adopt":
            result = adopt(args.state.resolve(), args.kind, args.evidence.resolve())
        elif args.command == "commit":
            result = commit(args.state.resolve(), args.kind)
        elif args.command == "record":
            result = record(args.state.resolve(), args.kind, args.status, args.evidence)
        else:
            result = status_state(args.state.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print(json.dumps({"command": args.command, "error": exc.code,
                          "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"command": args.command, "error": "io_error",
                          "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
