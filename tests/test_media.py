import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from media import (
    _check_content_length,
    _ensure_image_content_type,
    _is_allowed_ha_media_url,
    _prune_cache,
    _reject_redirect,
)


class MediaUrlGuardTest(unittest.TestCase):
    def test_allows_ha_image_proxy_on_same_host(self):
        self.assertTrue(
            _is_allowed_ha_media_url(
                "http://localhost:8123",
                "/api/image_proxy/image.kaamera1_person?token=abc",
            )
        )

    def test_rejects_different_host(self):
        self.assertFalse(
            _is_allowed_ha_media_url(
                "http://localhost:8123",
                "http://evil.invalid/api/image_proxy/image.kaamera1_person?token=abc",
            )
        )

    def test_rejects_non_media_ha_path(self):
        self.assertFalse(
            _is_allowed_ha_media_url(
                "http://localhost:8123",
                "/api/websocket",
            )
        )

    def test_rejects_media_player_proxy(self):
        self.assertFalse(
            _is_allowed_ha_media_url(
                "http://localhost:8123",
                "/api/media_player_proxy/media_player.radio",
            )
        )

    def test_rejects_redirects(self):
        with self.assertRaisesRegex(RuntimeError, "redirect rejected"):
            _reject_redirect(302, "http://evil.invalid/pixel.jpg")

    def test_requires_image_content_type(self):
        _ensure_image_content_type("image/jpeg")
        with self.assertRaisesRegex(RuntimeError, "not an image"):
            _ensure_image_content_type("text/html")

    def test_rejects_large_content_length(self):
        with self.assertRaisesRegex(RuntimeError, "too large"):
            _check_content_length(str(6 * 1024 * 1024))

    def test_prunes_cache_by_file_count(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(55):
                path = root / f"ha_media_{idx:02d}.jpg"
                path.write_bytes(b"x")
            _prune_cache(root)
            self.assertLessEqual(len(list(root.glob("ha_media_*"))), 50)
