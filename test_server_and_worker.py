#!/usr/bin/env python3
"""
test_server_and_worker.py — Verification script for server endpoints and worker resolution.
"""

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from starlette.testclient import TestClient

import server
import worker


class TestServerAndWorker(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for clips during test
        self.temp_dir = tempfile.mkdtemp(prefix="test_clips_")
        self.orig_clips_dir = worker.os.environ.get("CLIPS_DIR")
        worker.os.environ["CLIPS_DIR"] = self.temp_dir
        self.client = TestClient(server.app)

    def tearDown(self):
        # Restore environment and delete temp directory
        if self.orig_clips_dir is not None:
            worker.os.environ["CLIPS_DIR"] = self.orig_clips_dir
        else:
            worker.os.environ.pop("CLIPS_DIR", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("supabase", data)
        self.assertIn("background_worker", data)
        self.assertIn("storage", data)

    def test_upload_clip_with_id(self):
        clip_id = "test-clip-12345"
        dummy_video_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42"  # mock mp4 header
        files = {
            "video": ("my_raw_clip.mp4", io.BytesIO(dummy_video_bytes), "video/mp4"),
        }
        data = {
            "caption": "Incredible Fred Again drop at Boiler Room London! 🔥 #DJ #ElectronicMusic",
            "title": "Fred Again Boiler Room",
            "tags": "DJ, ElectronicMusic, BoilerRoom",
        }

        # 1. Post to /api/clips/{id}
        response = self.client.post(f"/api/clips/{clip_id}", files=files, data=data)
        self.assertEqual(response.status_code, 200, response.text)
        res_json = response.json()
        self.assertTrue(res_json["success"])
        self.assertEqual(res_json["id"], clip_id)
        self.assertEqual(res_json["storage_url"], f"clips/{clip_id}/clip.mp4")

        # 2. Verify video only (no metadata.json)
        target_folder = Path(self.temp_dir) / clip_id
        self.assertTrue(target_folder.is_dir(), f"Folder {target_folder} should exist")

        video_file = target_folder / "clip.mp4"
        self.assertTrue(video_file.is_file(), f"Video file {video_file} should exist")
        self.assertEqual(video_file.read_bytes(), dummy_video_bytes)

        meta_file = target_folder / "metadata.json"
        self.assertFalse(meta_file.is_file(), "metadata.json should not be written")

        # 3. Test GET /api/clips/{id}
        get_res = self.client.get(f"/api/clips/{clip_id}")
        self.assertEqual(get_res.status_code, 200)
        get_data = get_res.json()
        self.assertTrue(get_data["video_exists"])
        self.assertEqual(get_data["storage_url"], f"clips/{clip_id}/clip.mp4")

        # 4. Test GET /api/clips
        list_res = self.client.get("/api/clips")
        self.assertEqual(list_res.status_code, 200)
        list_data = list_res.json()
        self.assertEqual(list_data["total"], 1)
        self.assertEqual(list_data["clips"][0]["id"], clip_id)

        # 5. Test Worker video resolution
        resolved_path = worker.resolve_video_path(None, clip_id=clip_id)
        self.assertIsNotNone(resolved_path)
        self.assertEqual(Path(resolved_path).resolve(), video_file.resolve())

        # 6. Delete stored files
        del_res = self.client.delete(f"/api/clips/{clip_id}")
        self.assertEqual(del_res.status_code, 200, del_res.text)
        del_data = del_res.json()
        self.assertTrue(del_data["success"])
        self.assertTrue(del_data["deleted"])
        self.assertFalse(target_folder.is_dir())
        self.assertIsNone(worker.resolve_video_path(None, clip_id=clip_id))


if __name__ == "__main__":
    unittest.main()
