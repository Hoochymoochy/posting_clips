#!/usr/bin/env python3
"""Unit tests for URL parsers (no network)."""

from analytics.fetchers.base import (
    extract_instagram_shortcode,
    extract_tiktok_video_id,
    extract_youtube_video_id,
    is_usable_post_url,
)


def test_youtube_shorts():
    assert (
        extract_youtube_video_id("https://youtube.com/shorts/AbCdEf12345")
        == "AbCdEf12345"
    )


def test_youtube_watch():
    assert (
        extract_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )


def test_instagram_reel():
    assert (
        extract_instagram_shortcode("https://www.instagram.com/reel/CxYzAbCdEf/")
        == "CxYzAbCdEf"
    )


def test_tiktok_video():
    assert (
        extract_tiktok_video_id("https://www.tiktok.com/@user/video/7123456789012345678")
        == "7123456789012345678"
    )


def test_tiktok_studio_unusable():
    url = "https://www.tiktok.com/tiktokstudio"
    assert extract_tiktok_video_id(url) is None
    assert is_usable_post_url("tiktok", url) is False
