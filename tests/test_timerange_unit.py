"""Time windows and unit conversion — hermetic.

The unit bugs these guard are all **silent**: Loki answers a seconds-based query
with `success` and an empty result, so a wrong conversion looks exactly like "no
logs". There is no error to catch. Tests are the only place this gets caught.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from agentcli.errors import ValidationError

from grafanacli.timerange import TimeRange, parse_duration, parse_instant


# ---- durations -------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("7d", timedelta(days=7)),
        ("1w", timedelta(weeks=1)),
        ("500ms", timedelta(milliseconds=500)),
        ("  2h  ", timedelta(hours=2)),
        ("2H", timedelta(hours=2)),
    ],
)
def test_parses_durations(text, expected):
    assert parse_duration(text) == expected


def test_bare_number_is_rejected_rather_than_guessed():
    """`--since 30` is ambiguous: Loki would read nanoseconds, a human means
    minutes. Guessing either way is worse than asking."""
    with pytest.raises(ValidationError) as e:
        parse_duration("30")
    assert "15m" in str(e.value), "the error must show the accepted syntax"


@pytest.mark.parametrize("bad", ["", "later", "1 fortnight", "-5m", "m5", None])
def test_rejects_nonsense_durations(bad):
    with pytest.raises(ValidationError):
        parse_duration(bad)


# ---- instants --------------------------------------------------------

def test_now_is_utc_aware():
    assert parse_instant("now").tzinfo is not None


def test_rfc3339_with_zone():
    dt = parse_instant("2026-07-17T08:00:00Z")
    assert dt == datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def test_naive_input_is_treated_as_utc_not_local():
    """The other half of the silent-wrong-window bug. A naive local time compared
    against a UTC instant is off by the offset — two hours on the instance this
    was built against: long enough to miss the deploy, short enough to look
    plausible."""
    dt = parse_instant("2026-07-17T08:00:00")
    assert dt.tzinfo is timezone.utc
    assert dt.hour == 8


@pytest.mark.parametrize(
    "raw,expected_year",
    [
        ("1784278682", 2026),               # seconds
        ("1784278682956", 2026),            # millis
        ("1784278682956119000", 2026),      # nanos
    ],
)
def test_unix_timestamps_infer_their_unit_from_digit_count(raw, expected_year):
    """10/13/19 digits do not overlap this century, so magnitude is a safe guess
    — and much friendlier than making the caller declare the unit."""
    assert parse_instant(raw).year == expected_year


def test_rejects_unparseable_time():
    with pytest.raises(ValidationError) as e:
        parse_instant("tuesday-ish")
    assert "RFC 3339" in str(e.value)


# ---- the window ------------------------------------------------------

def test_since_counts_back_from_now():
    tr = TimeRange.resolve(since="1h")
    assert 3595 <= tr.seconds() <= 3605


def test_explicit_from_wins_over_since():
    tr = TimeRange.resolve(since="1h", start="2026-07-17T00:00:00Z", end="2026-07-17T06:00:00Z")
    assert tr.seconds() == 6 * 3600


def test_since_counts_back_from_to_not_from_now():
    tr = TimeRange.resolve(since="2h", end="2026-07-17T12:00:00Z")
    assert tr.start == datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)


def test_default_since_is_used_when_nothing_given():
    tr = TimeRange.resolve(default_since="15m")
    assert 890 <= tr.seconds() <= 905


def test_inverted_window_is_a_clean_error():
    """Loki answers an inverted range with an empty success, so catching it here
    is the only place a human learns they typed the dates backwards."""
    with pytest.raises(ValidationError) as e:
        TimeRange.resolve(start="2026-07-17T12:00:00Z", end="2026-07-17T06:00:00Z")
    assert "not before" in str(e.value)


def test_zero_length_window_is_rejected():
    with pytest.raises(ValidationError):
        TimeRange.resolve(start="2026-07-17T06:00:00Z", end="2026-07-17T06:00:00Z")


# ---- THE conversions -------------------------------------------------

def test_loki_wants_nanoseconds():
    """The single most expensive trap in this codebase: seconds return
    `{"status":"success"}` with no data and no error."""
    tr = TimeRange(
        datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
    )
    start, end = tr.loki()
    assert start == 1784246400_000000000
    assert end == 1784250000_000000000
    assert end - start == 3600 * 10**9


def test_prometheus_wants_seconds():
    """The same instant, a different unit. Feeding Loki's nanoseconds to
    Prometheus asks about the year 58,500 — which, unhelpfully, is also not an
    error."""
    tr = TimeRange(
        datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
    )
    start, end = tr.prometheus()
    assert start == 1784246400.0
    assert end - start == 3600.0


def test_the_two_backends_disagree_by_exactly_a_billion():
    """Guards the relationship itself, so a 'fix' to one converter that breaks the
    other cannot pass."""
    tr = TimeRange.resolve(since="1h")
    assert tr.loki()[0] == pytest.approx(int(tr.prometheus()[0] * 1e9), rel=1e-9)


def test_nanos_avoid_float_rounding():
    """`int(ts * 1e9)` loses the low digits — float64 has 53 bits of mantissa and
    a nanosecond epoch needs ~61. Timestamps double as line identity when
    de-duplicating, so noisy low digits become 'the same line, twice'."""
    tr = TimeRange(
        datetime(2026, 7, 17, 8, 53, 26, 123456, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc),
    )
    start, _end = tr.loki()
    assert start % 1000 == 0, "microsecond input must land on a clean nanosecond boundary"
    assert str(start).endswith("123456000")


def test_describe_reports_the_window_used():
    """Every payload embeds this: Loki's label APIs are time-bounded, so a result
    without its window is half an answer."""
    tr = TimeRange(
        datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
    )
    d = tr.describe()
    assert d == {"start": "2026-07-17T00:00:00Z", "end": "2026-07-17T01:00:00Z", "seconds": 3600}
