"""Observable DASH references from equivalent timeline encodings."""

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.datastructures import URL

from mediaflow_proxy.mpd_processor import build_hls_playlist
from mediaflow_proxy.utils import mpd_utils


EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def manifest(entries, *, duration="PT20S", period_duration=None, scale=1, offset=0, start_number=1, live=False):
    template = {
        "@media": "s-$Number$-$Time$.m4s",
        "@initialization": "init.mp4",
        "@timescale": str(scale),
        "@presentationTimeOffset": str(offset),
        "@startNumber": str(start_number),
        "SegmentTimeline": {"S": entries},
    }
    period = {
        "AdaptationSet": {
            "@mimeType": "video/mp4",
            "SegmentTemplate": template,
            "Representation": {"@id": "video", "@bandwidth": "1000000", "@codecs": "avc1.640028"},
        }
    }
    if period_duration is not None:
        period["@duration"] = period_duration
    mpd = {"@type": "dynamic" if live else "static", "Period": period}
    if duration is not None:
        mpd["@mediaPresentationDuration"] = duration
    if live:
        mpd.update(
            {
                "@availabilityStartTime": EPOCH.isoformat(),
                "@publishTime": EPOCH.isoformat(),
                "@timeShiftBufferDepth": "PT10S",
            }
        )
    return {"MPD": mpd}


def parsed(data):
    url = "https://media.example/master.mpd"
    profile_id = mpd_utils.parse_mpd_dict(data, url, parse_drm=False)["profiles"][0]["id"]
    return mpd_utils.parse_mpd_dict(data, url, parse_drm=False, parse_segment_profile_id=profile_id)


def segments(data):
    return [s for p in parsed(data)["profiles"] for s in p["segments"]]


def freeze_clock(monkeypatch, seconds):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (EPOCH + timedelta(seconds=seconds)).astimezone(tz or timezone.utc)

    monkeypatch.setattr(mpd_utils, "datetime", Clock)


@pytest.mark.parametrize("duration,count", [("PT20S", 5), ("PT18S", 5), ("PT4S", 1)])
def test_repeat_to_presentation_end_matches_explicit_references(duration, count):
    data = manifest({"@t": "0", "@d": "4", "@r": "-1"}, duration=duration)
    actual = segments(data)
    assert [s["time"] for s in actual] == [4 * i for i in range(count)]
    assert [s["number"] for s in actual] == list(range(1, count + 1))
    assert all(s["extinf"] == 4 for s in actual)


def test_period_duration_wins_over_longer_presentation():
    data = manifest({"@d": "4", "@r": "-1"}, duration="PT100S", period_duration="PT10S")
    assert [s["time"] for s in segments(data)] == [0, 4, 8]


def test_period_without_mpd_duration_can_bound_repeat():
    data = manifest({"@d": "4", "@r": "-1"}, duration=None, period_duration="PT12S")
    assert len(segments(data)) == 3


def test_segment_overlapping_period_start_is_retained():
    data = manifest({"@t": "0", "@d": "4", "@r": "-1"}, duration="PT6S", offset=5, start_number=10)
    actual = segments(data)
    assert [(s["time"], s["number"], s["extinf"]) for s in actual] == [(4, 11, 4), (8, 12, 4)]
    assert actual[0]["start_time"] == datetime(1970, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    assert actual[0]["media"] == "https://media.example/s-11-4.m4s"


def test_next_explicit_start_ends_repeat_and_preserves_numbering():
    data = manifest(
        [{"@t": "100", "@d": "4", "@r": "-1"}, {"@t": "112", "@d": "2", "@r": "1"}],
        duration="PT16S",
        offset=100,
        start_number=37,
    )
    actual = segments(data)
    assert [s["time"] for s in actual] == [100, 104, 108, 112, 114]
    assert [s["number"] for s in actual] == [37, 38, 39, 40, 41]
    assert [s["extinf"] for s in actual] == [4, 4, 4, 2, 2]


def test_offset_scale_and_implicit_start_after_positive_run():
    data = manifest(
        [{"@t": "500", "@d": "125", "@r": "1"}, {"@d": "250", "@r": "-1"}],
        duration="PT7.5S",
        scale=100,
        offset=500,
        start_number=9,
    )
    actual = segments(data)
    assert [(s["time"], s["number"], s["extinf"]) for s in actual] == [
        (500, 9, 1.25),
        (625, 10, 1.25),
        (750, 11, 2.5),
        (1000, 12, 2.5),
    ]
    assert actual[-1]["media"] == "https://media.example/s-12-1000.m4s"


def test_large_sample_clock_keeps_exact_url_ticks():
    origin = 2**54 + 1
    actual = segments(manifest({"@t": str(origin), "@d": "3", "@r": "-1"}, duration="PT1S", scale=10, offset=origin))
    assert [s["time"] for s in actual] == [origin, origin + 3, origin + 6, origin + 9]
    assert actual[-1]["media"].endswith(f"s-4-{origin + 9}.m4s")


def test_adjacent_period_bounds_do_not_use_entire_mpd_duration():
    data = manifest({"@d": "4", "@r": "-1"}, duration="PT20S")
    first = data["MPD"]["Period"]
    second = copy.deepcopy(first)
    second["@start"] = "PT12S"
    data["MPD"]["Period"] = [first, second]
    profiles = parsed(data)["profiles"]
    assert [[s["time"] for s in p["segments"]] for p in profiles] == [[0, 4, 8], [0, 4]]
    assert profiles[1]["segments"][0]["start_time"] == datetime(1970, 1, 1, 0, 0, 12, tzinfo=timezone.utc)


def test_omitted_second_period_start_is_inferred():
    data = manifest({"@d": "4", "@r": "-1"}, duration="PT20S", period_duration="PT12S")
    first = data["MPD"]["Period"]
    second = copy.deepcopy(first)
    second.pop("@duration")
    data["MPD"]["Period"] = [first, second]
    profiles = parsed(data)["profiles"]
    assert [len(p["segments"]) for p in profiles] == [3, 2]
    assert profiles[1]["segments"][0]["start_time"].timestamp() == 12


@pytest.mark.parametrize(
    "now,expected", [(4, [0]), (20, [8, 12, 16]), (21, [8, 12, 16]), (24, [12, 16, 20])]
)
def test_live_window_contains_only_available_unexpired_segments(monkeypatch, now, expected):
    freeze_clock(monkeypatch, now)
    data = manifest({"@d": "4", "@r": "-1"}, duration=None, live=True, start_number=7)
    actual = segments(data)
    assert [s["time"] for s in actual] == expected
    assert [s["number"] for s in actual] == [7 + t // 4 for t in expected]


@pytest.mark.parametrize("repeat", ["-1", "1000000000000"])
def test_long_running_live_repeat_skips_history_without_enumerating_it(repeat):
    # A bad expander must fail this test rather than exhaust the test runner.
    code = (
        "import json, sys; from tests.test_dash_repeat_windows import long_live_result; "
        "print(json.dumps(long_live_result(sys.argv[1])))"
    )
    result = subprocess.run([sys.executable, "-c", code, repeat], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[99_999_988, 24_999_998], [99_999_992, 24_999_999], [99_999_996, 25_000_000]]


def long_live_result(repeat):
    with pytest.MonkeyPatch.context() as monkeypatch:
        freeze_clock(monkeypatch, 100_000_000)
        data = manifest({"@d": "4", "@r": repeat}, duration=None, live=True)
        return [(s["time"], s["number"]) for s in segments(data)]


def test_live_period_start_and_offset_use_same_presentation_clock(monkeypatch):
    freeze_clock(monkeypatch, 34)
    data = manifest(
        {"@t": "9000", "@d": "4000", "@r": "-1"}, duration=None, scale=1000, offset=9000, start_number=50, live=True
    )
    data["MPD"]["Period"]["@start"] = "PT20S"
    actual = segments(data)
    assert [(s["time"], s["number"]) for s in actual] == [(13000, 51), (17000, 52)]
    assert actual[0]["start_time"] == EPOCH + timedelta(seconds=24)


def test_finite_live_period_does_not_extend_to_current_time(monkeypatch):
    freeze_clock(monkeypatch, 30)
    data = manifest({"@d": "4", "@r": "-1"}, duration=None, period_duration="PT24S", live=True)
    assert [s["time"] for s in segments(data)] == [20]


def test_live_refresh_preserves_urls_and_numbers_of_overlapping_segments(monkeypatch):
    data = manifest({"@d": "4", "@r": "-1"}, duration=None, live=True)
    freeze_clock(monkeypatch, 20)
    before = {s["time"]: s["media"] for s in segments(data)}
    freeze_clock(monkeypatch, 24)
    after = {s["time"]: s["media"] for s in segments(data)}
    assert set(before) & set(after) == {12, 16}
    assert all(before[t] == after[t] for t in set(before) & set(after))


def test_repeat_segments_reach_hls_playlist():
    mpd = parsed(manifest({"@d": "4", "@r": "-1"}))

    class Request:
        query_params = {}
        headers = {}
        url = URL("http://localhost/proxy/mpd/playlist.m3u8")

        def url_for(self, name, **kwargs):
            return URL(f"http://localhost/{name}")

    playlist = build_hls_playlist(mpd, mpd["profiles"], Request())
    urls = [line for line in playlist.splitlines() if line and not line.startswith("#")]
    assert len(urls) == 5
    assert [parse_qs(urlparse(url).query)["segment_url"][0] for url in urls] == [
        f"https://media.example/s-{n + 1}-{n * 4}.m4s" for n in range(5)
    ]
    assert playlist.count("#EXTINF:4.000,") == 5
    dates = [
        datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00"))
        for line in playlist.splitlines()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:")
    ]
    assert dates == [datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=4 * n) for n in range(5)]
    assert "#EXT-X-ENDLIST" in playlist


@pytest.mark.parametrize("scale,duration", [(0, 4), (1, 0), (1, -4)])
def test_invalid_units_fail_promptly(scale, duration):
    with pytest.raises(ValueError):
        segments(manifest({"@d": str(duration), "@r": "-1"}, scale=scale))


def test_unbounded_static_repeat_is_reported_as_invalid():
    with pytest.raises(ValueError):
        segments(manifest({"@d": "4", "@r": "-1"}, duration=None))
