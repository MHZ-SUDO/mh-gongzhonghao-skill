"""Isolated fixtures for the local one-attempt publishing helper."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

import publish_workflow as workflow


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_parent = Path(tempfile.gettempdir()).resolve()
        cls.root = Path(tempfile.mkdtemp(prefix="wechat_publish_test_", dir=cls.temp_parent)).resolve()
        cls.fixture = cls.root / "fixtures"
        cls.fixture.mkdir()
        cls.base_video = cls.fixture / "video.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=10",
             "-t", "0.5", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
             str(cls.base_video)], check=True,
        )
        cls.base_cover = cls.fixture / "cover.png"
        Image.new("RGB", (320, 180), "navy").save(cls.base_cover)
        cls.base_article = cls.fixture / "article.docx"
        document = '''<?xml version="1.0" encoding="utf-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Fixture episode 03</w:t></w:r></w:p>
  <w:p><w:r><w:t>01 教学正文</w:t></w:r></w:p>
  <w:p><w:r><w:drawing><a:blip r:embed="rIdImage"/></w:drawing></w:r></w:p>
  <w:p><m:oMathPara><m:oMath><m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e><m:sub><m:r><m:t>k</m:t></m:r></m:sub></m:sSub></m:oMath></m:oMathPara></w:p>
 </w:body>
</w:document>'''
        relationships = '''<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>'''
        types = '''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
        root_rels = '''<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
        with zipfile.ZipFile(cls.base_article, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("word/document.xml", document)
            z.writestr("word/_rels/document.xml.rels", relationships)
            z.write(cls.base_cover, "word/media/image1.png")

    @classmethod
    def tearDownClass(cls):
        # Verify the resolved absolute target before recursive Windows cleanup.
        target = cls.root.resolve()
        if target.parent != cls.temp_parent or not target.name.startswith("wechat_publish_test_"):
            raise RuntimeError("Unexpected temporary cleanup target; refusing removal")
        shutil.rmtree(target)

    def setUp(self):
        self.case = self.root / self._testMethodName
        self.case.mkdir()
        self.video = self.case / "lecture.mp4"
        self.cover = self.case / "cover.png"
        self.article = self.case / "article.docx"
        for src, dst in [(self.base_video, self.video), (self.base_cover, self.cover),
                         (self.base_article, self.article)]:
            shutil.copyfile(src, dst)
        self.config = {
            "schema_version": 1, "mode": "video_and_article", "episode": "03",
            "title": "第03期_动力学模型离散化", "account_name": "本期配置中的公众号名称",
            "collection": "本期配置中的合集名称",
            "video": str(self.video), "article": str(self.article), "cover": str(self.cover),
            "reference_recordings": [],
            "authorization": {"requested_action": "publish", "user_instruction": "照录屏发这一期"},
            "settings": {"group_notify": False, "schedule": None,
                         "sync_video_channel": True, "original_author": "本期已确认作者"},
        }
        self.config_path = self.case / "config.json"
        self.output = self.case / "prepared"
        self.manifest = self.output / "manifest.json"
        self.state = self.output / "state.json"

    def prepare(self):
        workflow.write_json(self.config_path, self.config)
        return workflow.prepare(self.config_path, self.output)

    def initialize(self):
        self.prepare()
        workflow.init_state(self.manifest, self.state)

    def refresh(self, name="refreshed"):
        workflow.write_json(self.config_path, self.config)
        output = self.case / name
        return workflow.refresh(self.state, self.config_path, output), output

    def revised_article(self):
        revised = self.case / "article_revised.docx"
        with zipfile.ZipFile(self.article) as source, zipfile.ZipFile(
                revised, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for member in source.infolist():
                content = source.read(member.filename)
                if member.filename == "word/document.xml":
                    content = content.replace("01 教学正文".encode("utf-8"),
                                              "01 已核实的修订讲解".encode("utf-8"))
                target.writestr(member, content)
        return revised

    def evidence(self, name="video", **updates):
        kind = "article" if name == "article" else "video"
        data = {"account": self.config["account_name"], "title": self.config["title"],
                "editor_kind": kind, "editor_identity": f"mp:{kind}:fixture-editor-03",
                "reviewed_at": workflow.iso_utc(), **{k: True for k in workflow.REVIEW_FLAGS}}
        data.update(updates)
        path = self.case / f"evidence_{name}.json"
        workflow.write_json(path, data)
        return path

    def observation(self, kind="video", name="observation", **updates):
        data = {
            "account": self.config["account_name"], "title": self.config["title"],
            "kind": kind, "status": "pending", "platform_identity": "wx:fixture-record-03",
            "existing_record_checked": True, "observed_at": workflow.iso_utc(),
            "notes": "本地测试：观察到用户先前已经提交的记录，未执行新网页动作。",
        }
        data.update(updates)
        path = self.case / f"{name}.json"
        workflow.write_json(path, data)
        return path

    def assert_error(self, code, operation, *args):
        with self.assertRaises(workflow.WorkflowError) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, code)

    def test_prepare_is_read_only_and_records_real_metadata(self):
        before = {str(p): workflow.file_hash(p) for p in (self.video, self.cover, self.article)}
        result = self.prepare()
        self.assertFalse(result["uploaded"])
        m = workflow.read_json(self.manifest)
        self.assertEqual(m["frozen"]["inputs"]["video"]["metadata"]["width"], 320)
        self.assertEqual(m["frozen"]["inputs"]["article"]["metadata"]["body_embedded_image_references"], 1)
        self.assertEqual(m["frozen"]["inputs"]["article"]["metadata"]["body_omml_group_count"], 1)
        self.assertEqual(before, {str(p): workflow.file_hash(p) for p in (self.video, self.cover, self.article)})

    def test_material_change_prevents_commit(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        with self.cover.open("ab") as handle:
            handle.write(b"changed after review")
        self.assert_error("asset_changed", workflow.commit, self.state, "video")
        self.assertIsNone(workflow.load_state(self.state)["items"]["video"]["attempt"])

    def test_prepare_authorization_cannot_commit(self):
        self.config["authorization"] = {"requested_action": "prepare", "user_instruction": "只准备清单"}
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        self.assert_error("not_authorized", workflow.commit, self.state, "video")

    def test_skill_creation_is_not_publication_authorization(self):
        self.config["authorization"] = {"requested_action": "publish", "user_instruction": "按录屏做一个详细skill，以后我只扫三次码"}
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        self.assert_error("not_authorized", workflow.commit, self.state, "video")

    def test_placeholder_or_missing_authorization_is_rejected(self):
        self.config["authorization"] = {"requested_action": "publish", "user_instruction": "当次用户原话"}
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        self.assert_error("not_authorized", workflow.commit, self.state, "video")
        missing = dict(self.config)
        missing.pop("authorization")
        self.assert_error("missing_fields", workflow.validate_config, missing)

    def test_same_title_video_and_article_are_separate_allowed_attempts(self):
        self.initialize()
        for kind in ("video", "article"):
            workflow.review_state(self.state, kind, self.evidence(kind))
            result = workflow.commit(self.state, kind)
            self.assertEqual(result["status"], "attempted")
            workflow.record(self.state, kind, "published", f"fixture actual {kind} receipt")
        state = workflow.load_state(self.state)
        self.assertEqual(sum(e["action"] == "commit" for e in state["events"]), 2)
        self.assertTrue(workflow.status_state(self.state)["all_publications_confirmed"])

    def test_double_commit_and_failed_retry_are_blocked(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        self.assert_error("already_attempted", workflow.commit, self.state, "video")
        workflow.record(self.state, "video", "failed", "fixture timeout or failure")
        self.assert_error("already_attempted", workflow.commit, self.state, "video")
        self.assert_error("already_attempted", workflow.review_state, self.state, "video", self.evidence())

    def test_pending_remains_pending_and_does_not_mean_success(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        receipt = workflow.record(self.state, "video", "pending", "页面仍在等待验证/审核")
        self.assertFalse(receipt["publication_confirmed"])
        result = workflow.status_state(self.state)
        self.assertEqual(result["items"]["video"]["status"], "pending")
        self.assertFalse(result["items"]["video"]["publication_confirmed"])
        self.assertFalse(result["all_publications_confirmed"])
        self.assert_error("already_attempted", workflow.commit, self.state, "video")

    def test_expired_review_rejected_on_review_and_commit(self):
        self.initialize()
        expired = self.evidence("old", reviewed_at=workflow.iso_utc(workflow.utc_now()-timedelta(seconds=601)))
        self.assert_error("review_expired", workflow.review_state, self.state, "video", expired)
        now = workflow.utc_now().replace(microsecond=0)
        fresh = self.evidence("fresh", reviewed_at=workflow.iso_utc(now))
        with patch.object(workflow, "utc_now", return_value=now):
            workflow.review_state(self.state, "video", fresh)
        with patch.object(workflow, "utc_now", return_value=now+timedelta(seconds=601)):
            self.assert_error("review_expired", workflow.commit, self.state, "video")
        self.assertIsNone(workflow.load_state(self.state)["items"]["video"]["attempt"])

    def test_false_flag_and_wrong_account_title_rejected(self):
        self.initialize()
        self.assert_error("review_incomplete", workflow.review_state, self.state, "video", self.evidence("false", preview_verified=False))
        self.assert_error("review_identity", workflow.review_state, self.state, "video", self.evidence("account", account="另一账号"))
        self.assert_error("review_identity", workflow.review_state, self.state, "video", self.evidence("title", title="另一标题"))

    def test_same_title_wrong_editor_kind_is_rejected(self):
        self.initialize()
        article_evidence = self.evidence("article")
        self.assert_error("review_kind", workflow.review_state, self.state, "video", article_evidence)
        state = workflow.load_state(self.state)
        self.assertIsNone(state["items"]["video"]["review"])
        workflow.review_state(self.state, "article", article_evidence)
        workflow.commit(self.state, "article")
        self.assertIsNone(workflow.load_state(self.state)["items"]["video"]["attempt"])

    def test_editor_identity_is_required_and_cannot_hold_session_parameters(self):
        self.initialize()
        self.assert_error("review_editor_identity", workflow.review_state, self.state, "video",
                          self.evidence("blank_editor", editor_identity=" "))
        self.assert_error("review_editor_identity", workflow.review_state, self.state, "video",
                          self.evidence("session_editor", editor_identity="https://mp.weixin.qq.com/?token=TEST"))
        missing = workflow.read_json(self.evidence())
        missing.pop("editor_identity")
        path = self.case / "missing_editor.json"
        workflow.write_json(path, missing)
        self.assert_error("missing_fields", workflow.review_state, self.state, "video", path)

    def test_commit_rechecks_editor_kind_from_saved_review(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        # Local negative fixture: an old/incorrectly migrated review is placed
        # in state with a valid evidence digest. Commit must still check kind.
        state = workflow.load_state(self.state)
        evidence = state["items"]["video"]["review"]["evidence"]
        evidence["editor_kind"] = "article"
        state["items"]["video"]["review"]["evidence_sha256"] = workflow.object_hash(evidence)
        workflow.write_json(self.state, state)
        self.assert_error("review_kind", workflow.commit, self.state, "video")
        self.assertIsNone(workflow.load_state(self.state)["items"]["video"]["attempt"])

    def test_reference_recording_excluded_by_path_and_bytes(self):
        self.config["reference_recordings"] = [str(self.video)]
        self.assert_error("reference_as_video", self.prepare)
        reference = self.case / "reference_recording.mp4"
        shutil.copyfile(self.video, reference)
        self.config["reference_recordings"] = [str(reference)]
        self.assert_error("reference_as_video", self.prepare)

    def test_inconsistent_manifest_and_state_not_overwritten(self):
        self.initialize()
        before = self.manifest.read_bytes()
        self.config["title"] = "另一期"
        self.assert_error("manifest_conflict", self.prepare)
        self.assertEqual(self.manifest.read_bytes(), before)
        other = self.case / "other"
        workflow.prepare(self.config_path, other)
        self.assert_error("state_conflict", workflow.init_state, other/"manifest.json", self.state)

    def test_consistent_init_keeps_attempted_state(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        before = self.state.read_bytes()
        result = workflow.init_state(self.manifest, self.state)
        self.assertTrue(result["unchanged"])
        self.assertEqual(before, self.state.read_bytes())

    def test_concurrent_commits_only_one_attempt(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        script = Path(workflow.__file__).resolve()
        command = [sys.executable, str(script), "commit", "--state", str(self.state), "--kind", "video"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(subprocess.run, command, capture_output=True,
                                   text=True, encoding="utf-8", timeout=30) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(r.returncode for r in results), [0, 2])
        state = workflow.load_state(self.state)
        self.assertEqual(sum(e["action"] == "commit" for e in state["events"]), 1)

    def test_scope_paths_and_prepare_only(self):
        self.config["video"] = "relative.mp4"
        self.assert_error("absolute_path_required", workflow.validate_config, self.config)
        self.config["video"] = str(self.video)
        self.config["mode"] = "prepare_only"
        self.initialize()
        self.assert_error("kind_not_in_scope", workflow.review_state, self.state, "video", self.evidence())

    def test_short_actual_user_instruction_not_keyword_whitelisted(self):
        for instruction in ["发本期", "照录屏发这一期", "就按刚才确认的内容发", "Publish this episode now."]:
            allowed, _ = workflow.publish_intent({"requested_action": "publish", "user_instruction": instruction})
            self.assertTrue(allowed, instruction)

    def test_adopt_without_publish_authorization_never_fakes_attempt(self):
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "只做发布技能，不进行真实发布",
        }
        self.initialize()
        result = workflow.adopt(self.state, "video", self.observation())
        self.assertFalse(result["submitted_here"])
        self.assertTrue(result["existing_record_adopted"])
        self.assertFalse(result["publication_confirmed"])
        state = workflow.load_state(self.state)
        item = state["items"]["video"]
        self.assertIsNone(item["attempt"])
        self.assertIsNotNone(item["external_observation"])
        self.assertFalse(any(e["action"] == "commit" for e in state["events"]))
        self.assertEqual(sum(e["action"] == "adopt" for e in state["events"]), 1)
        self.assert_error("existing_record_adopted", workflow.commit, self.state, "video")
        self.assert_error("existing_record_adopted", workflow.review_state, self.state, "video", self.evidence())
        self.assert_error("existing_record_adopted", workflow.adopt, self.state, "video", self.observation())

    def test_adopt_prepare_only_is_read_only_even_for_published_record(self):
        self.config["mode"] = "prepare_only"
        self.config["authorization"]["requested_action"] = "prepare"
        self.initialize()
        workflow.adopt(self.state, "video", self.observation(status="published"))
        status = workflow.status_state(self.state)
        item = status["items"]["video"]
        self.assertTrue(item["publication_confirmed"])
        self.assertFalse(item["submitted_here"])
        self.assertTrue(item["existing_record_adopted"])
        self.assertFalse(item["attempt_recorded"])
        self.assertFalse(status["all_publications_confirmed"])
        self.assert_error("existing_record_adopted", workflow.commit, self.state, "video")

    def test_adopt_identity_flags_and_platform_id_are_required(self):
        self.initialize()
        cases = [
            ({"account": "另一账号"}, "adopt_identity"),
            ({"title": "另一标题"}, "adopt_identity"),
            ({"kind": "article"}, "adopt_identity"),
            ({"status": "failed"}, "adopt_status"),
            ({"existing_record_checked": False}, "adopt_incomplete"),
            ({"platform_identity": ""}, "adopt_platform_identity"),
            ({"platform_identity": "https://mp.weixin.qq.com/cgi-bin/appmsg?token=TEST"}, "adopt_platform_identity"),
            ({"platform_identity": "https://mp.weixin.qq.com/?%74oken%3DTEST"}, "adopt_platform_identity"),
            ({"notes": ""}, "invalid_field"),
        ]
        for i, (updates, code) in enumerate(cases):
            with self.subTest(updates=updates):
                self.assert_error(code, workflow.adopt, self.state, "video",
                                  self.observation(name=f"bad_{i}", **updates))
        missing = workflow.read_json(self.observation())
        missing.pop("platform_identity")
        path = self.case / "missing_id.json"
        workflow.write_json(path, missing)
        self.assert_error("missing_fields", workflow.adopt, self.state, "video", path)
        self.assertIsNone(workflow.load_state(self.state)["items"]["video"]["external_observation"])

    def test_adopt_expired_or_non_utc_observation_is_rejected(self):
        self.initialize()
        path = self.observation(observed_at=workflow.iso_utc(workflow.utc_now()-timedelta(seconds=601)))
        self.assert_error("adopt_expired", workflow.adopt, self.state, "video", path)
        path = self.observation(observed_at="2026-10-30T20:00:00+08:00")
        self.assert_error("adopt_time", workflow.adopt, self.state, "video", path)

    def test_adopt_does_not_lock_the_other_kind(self):
        self.initialize()
        workflow.adopt(self.state, "video", self.observation(status="published"))
        workflow.review_state(self.state, "article", self.evidence("article"))
        workflow.commit(self.state, "article")
        state = workflow.load_state(self.state)
        self.assertIsNone(state["items"]["video"]["attempt"])
        self.assertIsNotNone(state["items"]["article"]["attempt"])
        self.assertIsNone(state["items"]["article"]["external_observation"])

    def test_adopt_allows_actual_later_receipts_but_no_retry(self):
        self.initialize()
        workflow.adopt(self.state, "video", self.observation())
        result = workflow.record(self.state, "video", "published", "本地模拟回读：原有记录现已发表")
        self.assertTrue(result["publication_confirmed"])
        self.assertFalse(result["submitted_here"])
        self.assertTrue(result["existing_record_adopted"])
        status = workflow.status_state(self.state)["items"]["video"]
        self.assertEqual(status["status"], "published")
        self.assertIsNone(status["attempt"])
        workflow.record(self.state, "video", "failed", "本地模拟后续审核失败回执")
        self.assert_error("existing_record_adopted", workflow.commit, self.state, "video")

    def test_committed_kind_cannot_be_adopted(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        self.assert_error("already_attempted", workflow.adopt, self.state, "video", self.observation())
        result = workflow.record(self.state, "video", "pending", "等待本地模拟回执")
        self.assertTrue(result["submitted_here"])
        self.assertFalse(result["existing_record_adopted"])

    def test_concurrent_adopt_and_commit_cannot_both_win(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        observation = self.observation()
        script = Path(workflow.__file__).resolve()
        base = [sys.executable, str(script)]
        commands = [base+["commit", "--state", str(self.state), "--kind", "video"],
                    base+["adopt", "--state", str(self.state), "--kind", "video", "--evidence", str(observation)]]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(subprocess.run, command, capture_output=True,
                                   text=True, encoding="utf-8", timeout=30) for command in commands]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(r.returncode for r in results), [0, 2])
        state = workflow.load_state(self.state)
        self.assertEqual(sum(e["action"] in {"commit", "adopt"} for e in state["events"]), 1)
        item = state["items"]["video"]
        self.assertNotEqual(item.get("attempt") is not None, item.get("external_observation") is not None)

    def test_refresh_upgrades_draft_without_erasing_history_or_changing_assets(self):
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "只存本期视频和图文草稿",
        }
        self.initialize()
        for kind in ("video", "article"):
            workflow.review_state(self.state, kind, self.evidence(kind))
        previous = workflow.load_state(self.state)
        previous_bytes = self.state.read_bytes()
        manifest_bytes = self.manifest.read_bytes()
        hashes = {p: workflow.file_hash(p) for p in (self.video, self.article, self.cover)}
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "现在发表本期视频和配套图文",
        }
        _, output = self.refresh()
        current = workflow.load_state(self.state)
        self.assertEqual(workflow.read_json(output / "previous-state.json"), previous)
        self.assertEqual((output / "previous-state.json").read_bytes(), previous_bytes)
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assertEqual(current["events"][:len(previous["events"])], previous["events"])
        self.assertEqual(current["events"][-1]["action"], "refresh")
        self.assertEqual(Path(current["manifest_path"]), (output / "manifest.json").resolve())
        self.assertEqual(hashes, {p: workflow.file_hash(p) for p in hashes})
        for kind in ("video", "article"):
            self.assertIsNone(current["items"][kind]["review"])
            self.assertIsNone(current["items"][kind]["attempt"])
        self.assert_error("review_required", workflow.commit, self.state, "video")
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")

    def test_refresh_prepare_only_can_enable_only_the_existing_video(self):
        self.config["mode"] = "prepare_only"
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "检查本期素材，先不要发表",
        }
        self.initialize()
        self.config["mode"] = "video_only"
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "现在只发表本期视频",
        }
        self.refresh()
        state = workflow.load_state(self.state)
        self.assertEqual(state["manifest"]["frozen"]["publish_kinds"], ["video"])
        self.assert_error("kind_not_in_scope", workflow.review_state, self.state,
                          "article", self.evidence("article"))
        self.assert_error("kind_not_in_scope", workflow.commit, self.state, "article")
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")

    def test_refresh_narrowed_scope_keeps_old_item_but_cannot_submit_it(self):
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "只保存视频和图文草稿",
        }
        self.initialize()
        workflow.review_state(self.state, "article", self.evidence("article"))
        self.config["mode"] = "video_only"
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "本次只发视频，图文继续保留草稿",
        }
        self.refresh()
        current = workflow.load_state(self.state)
        self.assertIn("article", current["items"])
        self.assertIsNone(current["items"]["article"]["review"])
        self.assert_error("kind_not_in_scope", workflow.review_state, self.state,
                          "article", self.evidence("article"))
        self.assert_error("kind_not_in_scope", workflow.commit, self.state, "article")
        self.assertIsNone(workflow.load_state(self.state)["items"]["article"]["attempt"])

    def test_refresh_revised_article_keeps_published_video_even_when_file_moved(self):
        self.initialize()
        for kind in ("video", "article"):
            workflow.review_state(self.state, kind, self.evidence(kind))
        workflow.commit(self.state, "video")
        workflow.record(self.state, "video", "pending", "本地模拟：视频已受理")
        workflow.record(self.state, "video", "published", "本地模拟：视频现已发表")
        previous = workflow.load_state(self.state)
        previous_manifest = self.manifest.read_bytes()
        self.video.rename(self.case / "published_video_archived.mp4")
        self.config["article"] = str(self.revised_article())
        self.refresh()
        current = workflow.load_state(self.state)
        self.assertEqual(current["items"]["video"], previous["items"]["video"])
        self.assertEqual(current["manifest"]["frozen"]["inputs"]["video"],
                         previous["manifest"]["frozen"]["inputs"]["video"])
        self.assertEqual(self.manifest.read_bytes(), previous_manifest)
        self.assertIsNone(current["items"]["article"]["review"])
        self.assertNotEqual(current["manifest"]["frozen"]["inputs"]["article"]["sha256"],
                            previous["manifest"]["frozen"]["inputs"]["article"]["sha256"])
        self.assert_error("already_attempted", workflow.commit, self.state, "video")
        workflow.review_state(self.state, "article", self.evidence("article"))
        workflow.commit(self.state, "article")
        self.assertEqual(workflow.load_state(self.state)["items"]["video"],
                         previous["items"]["video"])

    def test_refresh_preserves_adopted_record_without_creating_local_attempt(self):
        self.initialize()
        workflow.adopt(self.state, "video", self.observation(status="published"))
        workflow.record(self.state, "video", "published", "本地模拟：再次只读核对原记录")
        previous = workflow.load_state(self.state)
        self.config["article"] = str(self.revised_article())
        self.refresh()
        current = workflow.load_state(self.state)
        self.assertEqual(current["items"]["video"], previous["items"]["video"])
        self.assertIsNone(current["items"]["video"]["attempt"])
        self.assertFalse(any(e["action"] == "commit" for e in current["events"]))
        self.assert_error("existing_record_adopted", workflow.commit, self.state, "video")
        workflow.review_state(self.state, "article", self.evidence("article"))
        workflow.commit(self.state, "article")

    def test_refresh_identity_changes_are_rejected_without_output_or_state_mutation(self):
        self.initialize()
        original = json.loads(json.dumps(self.config))
        state_bytes, manifest_bytes = self.state.read_bytes(), self.manifest.read_bytes()
        for field, value in (("account_name", "另一公众号"), ("episode", "04")):
            with self.subTest(field=field):
                self.config = json.loads(json.dumps(original))
                self.config[field] = value
                output = self.case / f"identity_rejected_{field}"
                workflow.write_json(self.config_path, self.config)
                with self.assertRaises(workflow.WorkflowError):
                    workflow.refresh(self.state, self.config_path, output)
                self.assertEqual(self.state.read_bytes(), state_bytes)
                self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
                self.assertFalse(output.exists())

    def test_refresh_locked_material_or_metadata_cannot_be_rewritten(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        original = json.loads(json.dumps(self.config))
        video_bytes = self.video.read_bytes()
        state_bytes, manifest_bytes = self.state.read_bytes(), self.manifest.read_bytes()
        renamed_video = self.case / "same_video_new_path.mp4"
        renamed_video.write_bytes(video_bytes)
        other_cover = self.case / "other_cover.png"
        Image.new("RGB", (320, 180), "green").save(other_cover)
        changes = [
            {"title": "重新命名的标题"},
            {"collection": "另一合集"},
            {"settings": {**original["settings"], "original_author": "另一作者"}},
            {"video": str(renamed_video)},
            {"cover": str(other_cover)},
        ]
        for index, updates in enumerate(changes):
            with self.subTest(updates=updates, index=index):
                self.config = {**json.loads(json.dumps(original)), **updates}
                output = self.case / f"locked_rejected_{index}"
                workflow.write_json(self.config_path, self.config)
                with self.assertRaises(workflow.WorkflowError):
                    workflow.refresh(self.state, self.config_path, output)
                self.assertEqual(self.state.read_bytes(), state_bytes)
                self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
                self.assertFalse(output.exists())
        # Local files may be regenerated after publication. Refresh must not
        # rewrite what was submitted or turn those changed bytes into a retry.
        previous = workflow.load_state(self.state)
        self.config = json.loads(json.dumps(original))
        self.video.write_bytes(video_bytes + b"regenerated after publication")
        self.refresh("refresh_with_historical_video")
        current = workflow.load_state(self.state)
        self.assertEqual(current["items"]["video"], previous["items"]["video"])
        self.assertEqual(current["manifest"]["frozen"]["inputs"]["video"],
                         previous["manifest"]["frozen"]["inputs"]["video"])
        self.assertNotEqual(workflow.file_hash(self.video),
                            current["manifest"]["frozen"]["inputs"]["video"]["sha256"])
        self.assert_error("already_attempted", workflow.commit, self.state, "video")

    def test_refresh_existing_output_is_not_overwritten(self):
        self.initialize()
        output = self.case / "existing_refresh_output"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_bytes(b"unrelated existing evidence")
        state_bytes, manifest_bytes = self.state.read_bytes(), self.manifest.read_bytes()
        workflow.write_json(self.config_path, self.config)
        with self.assertRaises(workflow.WorkflowError):
            workflow.refresh(self.state, self.config_path, output)
        self.assertEqual(marker.read_bytes(), b"unrelated existing evidence")
        self.assertEqual(set(output.iterdir()), {marker})
        self.assertEqual(self.state.read_bytes(), state_bytes)
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)

    def test_refresh_new_authorization_adds_article_without_resetting_submitted_video(self):
        self.config["mode"] = "video_only"
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        workflow.commit(self.state, "video")
        previous = workflow.load_state(self.state)
        self.config["mode"] = "video_and_article"
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "视频已经提交，现在再发表本期配套图文",
        }
        self.refresh()
        current = workflow.load_state(self.state)
        self.assertEqual(current["items"]["video"], previous["items"]["video"])
        self.assertIn("article", current["items"])
        self.assertIsNone(current["items"]["article"]["review"])
        self.assertIsNone(current["items"]["article"]["attempt"])
        self.assert_error("review_required", workflow.commit, self.state, "article")
        self.assert_error("already_attempted", workflow.commit, self.state, "video")
        workflow.review_state(self.state, "article", self.evidence("article"))
        workflow.commit(self.state, "article")
        self.assertEqual(workflow.load_state(self.state)["items"]["video"],
                         previous["items"]["video"])

    def test_single_type_ignores_irrelevant_paths_and_accepts_no_collection_or_author(self):
        original = json.loads(json.dumps(self.config))
        cases = (("video_only", "video", "article"),
                 ("article_only", "article", "video"))
        for mode, active, inactive in cases:
            with self.subTest(mode=mode):
                config = json.loads(json.dumps(original))
                config["mode"] = mode
                config[inactive] = "irrelevant_missing_relative_file"
                config["collection"] = None
                config["settings"]["original_author"] = None
                payload = workflow.manifest_payload(config)
                self.assertEqual(payload["publish_kinds"], [active])
                self.assertEqual(set(payload["inputs"]), {active, "cover"})
                self.assertIsNone(payload["collection"])
                self.assertIsNone(payload["settings"]["original_author"])

    def test_commit_only_rechecks_current_kind_and_shared_cover(self):
        self.initialize()
        workflow.review_state(self.state, "video", self.evidence())
        self.article.write_bytes(b"unsubmitted article is being regenerated")
        workflow.commit(self.state, "video")
        state = workflow.load_state(self.state)
        self.assertIsNotNone(state["items"]["video"]["attempt"])
        self.assertIsNone(state["items"]["article"]["attempt"])
        workflow.review_state(self.state, "article", self.evidence("article"))
        self.assert_error("asset_changed", workflow.commit, self.state, "article")

    def test_refresh_cli_keeps_same_state_and_returns_without_publication(self):
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "先只保存视频和图文草稿",
        }
        self.initialize()
        previous = workflow.load_state(self.state)
        old_manifest_bytes = self.manifest.read_bytes()
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "现在发本期视频和图文",
        }
        workflow.write_json(self.config_path, self.config)
        output = self.case / "cli_refresh"
        command = [sys.executable, str(Path(workflow.__file__).resolve()), "refresh",
                   "--state", str(self.state), "--config", str(self.config_path),
                   "--out", str(output)]
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["command"], "refresh")
        self.assertEqual(workflow.read_json(output / "previous-state.json"), previous)
        self.assertEqual(self.manifest.read_bytes(), old_manifest_bytes)
        current = workflow.load_state(self.state)
        self.assertFalse(any(e["action"] in {"commit", "adopt"} for e in current["events"]))
        self.assertTrue(all(item["attempt"] is None for item in current["items"].values()))

    def test_configurable_upload_limits_are_recorded_and_actually_enforced(self):
        article_size = self.article.stat().st_size
        self.config["upload_limits"] = {
            "video_duration_less_than_seconds": 1,
            "docx_conservative_max_bytes": article_size,
        }
        payload = workflow.manifest_payload(self.config)
        self.assertEqual(payload["upload_limits"], self.config["upload_limits"])
        self.assertGreater(payload["inputs"]["video"]["metadata"]["duration_seconds"], 0)
        self.config["upload_limits"]["video_duration_less_than_seconds"] = 0.25
        self.assert_error("invalid_video", workflow.manifest_payload, self.config)
        self.config["upload_limits"]["video_duration_less_than_seconds"] = 1
        self.config["upload_limits"]["docx_conservative_max_bytes"] = article_size - 1
        self.assert_error("article_too_large", workflow.manifest_payload, self.config)

    def test_upload_limits_reject_nonpositive_nonfinite_and_boolean_values(self):
        invalid = [None, [], {"video_duration_less_than_seconds": 0},
                   {"video_duration_less_than_seconds": -1},
                   {"video_duration_less_than_seconds": True},
                   {"video_duration_less_than_seconds": float("inf")},
                   {"video_duration_less_than_seconds": float("nan")},
                   {"docx_conservative_max_bytes": 0},
                   {"docx_conservative_max_bytes": -1},
                   {"docx_conservative_max_bytes": True},
                   {"docx_conservative_max_bytes": 1.5}]
        for limits in invalid:
            with self.subTest(limits=limits):
                config = {**self.config, "upload_limits": limits}
                self.assert_error("upload_limits", workflow.validate_config, config)

    def test_publish_intent_allows_target_when_only_other_type_is_forbidden(self):
        cases = [
            ({"video"}, "现在先发表视频，图文不要发表"),
            ({"article"}, "本次先发表图文，视频不要发表"),
            ({"video"}, "Publish the video now; do not publish the article."),
        ]
        for kinds, instruction in cases:
            with self.subTest(kinds=kinds, instruction=instruction):
                allowed, _ = workflow.publish_intent(
                    {"requested_action": "publish", "user_instruction": instruction}, kinds,
                )
                self.assertTrue(allowed)

    def test_publish_intent_rejects_target_or_unqualified_publication_ban(self):
        cases = [
            ({"video"}, "图文可以发表，但视频不要发表"),
            ({"article"}, "视频可以发表，但不要发表图文"),
            ({"video"}, "这期先不要发表"),
            ({"article"}, "不要发表"),
            ({"video"}, "Do not publish this episode."),
        ]
        for kinds, instruction in cases:
            with self.subTest(kinds=kinds, instruction=instruction):
                allowed, _ = workflow.publish_intent(
                    {"requested_action": "publish", "user_instruction": instruction}, kinds,
                )
                self.assertFalse(allowed)

    def test_refresh_video_only_with_article_ban_can_submit_video_but_not_article(self):
        self.config["authorization"] = {
            "requested_action": "prepare", "user_instruction": "先保存视频和图文草稿",
        }
        self.initialize()
        previous_article = workflow.load_state(self.state)["items"]["article"]
        self.config["mode"] = "video_only"
        self.config["authorization"] = {
            "requested_action": "publish", "user_instruction": "现在先发表视频，图文不要发表",
        }
        self.refresh()
        current = workflow.load_state(self.state)
        self.assertTrue(current["manifest"]["frozen"]["authorization_check"]["publish_allowed"])
        self.assertEqual(current["items"]["article"], previous_article)
        workflow.review_state(self.state, "video", self.evidence())
        result = workflow.commit(self.state, "video")
        self.assertEqual(result["status"], "attempted")
        self.assert_error("kind_not_in_scope", workflow.commit, self.state, "article")
        current = workflow.load_state(self.state)
        self.assertIsNotNone(current["items"]["video"]["attempt"])
        self.assertEqual(current["items"]["article"], previous_article)


if __name__ == "__main__":
    unittest.main(verbosity=2)
