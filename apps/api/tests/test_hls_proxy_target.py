"""URL resolution for the public HLS relay.

The relay has to serve two upstream shapes: MediaMTX's directory-style
manifests (the Dahua path) and go2rtc's query-style manifests (the Eufy
path). These are pure-function tests -- no network, no provider.
"""
from app.api.routes import _resolve_hls_target

MEDIAMTX = "http://edge.tailnet.ts.net:8888/dahua-1/index.m3u8"
GO2RTC = "http://edge.tailnet.ts.net:1984/api/stream.m3u8?src=eufy-T8210P123"


def test_entry_point_resolves_to_the_upstream_manifest_for_directory_style():
    assert _resolve_hls_target(MEDIAMTX, "index.m3u8", "") == MEDIAMTX


def test_entry_point_preserves_query_style_manifest_url():
    # Naive rsplit("/") would produce ".../api/index.m3u8" and drop ?src=,
    # which is a 404 on go2rtc.
    assert _resolve_hls_target(GO2RTC, "index.m3u8", "") == GO2RTC


def test_empty_path_resolves_to_the_upstream_manifest():
    assert _resolve_hls_target(GO2RTC, "", "") == GO2RTC


def test_child_playlist_resolves_against_the_manifest_directory():
    assert (
        _resolve_hls_target(MEDIAMTX, "video1_stream.m3u8", "")
        == "http://edge.tailnet.ts.net:8888/dahua-1/video1_stream.m3u8"
    )


def test_child_request_query_string_is_forwarded():
    # go2rtc identifies sub-playlists/segments by opaque query parameters.
    assert (
        _resolve_hls_target(GO2RTC, "hls/playlist.m3u8", "id=abc123")
        == "http://edge.tailnet.ts.net:1984/api/hls/playlist.m3u8?id=abc123"
    )


def test_nested_segment_path_is_preserved():
    assert (
        _resolve_hls_target(MEDIAMTX, "seg/0.mp4", "")
        == "http://edge.tailnet.ts.net:8888/dahua-1/seg/0.mp4"
    )


def test_directory_style_child_without_query_has_no_trailing_question_mark():
    assert "?" not in _resolve_hls_target(MEDIAMTX, "segment0.ts", "")
