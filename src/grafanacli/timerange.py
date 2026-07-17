"""Time windows — parsing, and the unit conversions each backend demands.

This module exists because **the backends this CLI talks to read the same instant
differently**, and getting it wrong is silent:

* **Loki, through the datasource proxy**, switches on the **number of digits**,
  not on any declared unit. Its ``parseTimestamp`` does ``len(value) <= 10`` ->
  seconds, else nanoseconds. Verified live, all four encodings of one instant:

  ===============  ======  ====================================
  encoding         digits  result
  ===============  ======  ====================================
  seconds          10      works
  **milliseconds** **13**  **read as nanos -> 1970 -> EMPTY**
  microseconds     16      read as nanos -> 1970 -> empty
  nanoseconds      19      works
  ===============  ======  ====================================

  Milliseconds are the dangerous case exactly because they are the natural reach:
  ``Date.now()``, ``time.time()*1000`` and Grafana's own UI all speak millis. The
  wrong window returns ``{"status":"success","data":{"result":[]}}`` — no error,
  no warning, and indistinguishable from "there really are no logs", which is the
  worst failure available to a tool whose job is answering "what happened".

  So `loki()` below always emits **19 digits**: the one encoding that cannot be
  misread. (An earlier note in the spike claimed seconds silently fail. It was
  wrong, and a live test caught it. Read the server, not the notes.)
* **Prometheus/Mimir, through the proxy** wants **seconds** (float, RFC 3339 also
  accepted).
* **``/api/ds/query``** accepts Grafana's relative strings (``now-1h``) — which is
  friendlier, but returns dataframes instead of native JSON, so we do not use it.

So: parse once into an aware UTC datetime, then convert at the call site with the
explicit helper. Never pass a raw int between layers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from agentcli.errors import ValidationError

#: Grafana/Prometheus duration syntax. Deliberately a superset of what Loki
#: accepts so that `--since 2d` works the way a human expects.
_DURATION_RE = re.compile(r"^(\d+)\s*(ms|s|m|h|d|w|y)$", re.IGNORECASE)

_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


def parse_duration(text: str) -> timedelta:
    """``"90m"`` -> timedelta. Raises ValidationError with the accepted syntax.

    Bare numbers are rejected on purpose. ``--since 30`` is ambiguous — Loki would
    read it as 30 *nanoseconds*, a human means 30 minutes — and quietly guessing
    either way is worse than asking.
    """
    if text is None:
        raise ValidationError("a duration is required, e.g. '1h'.")
    raw = str(text).strip()
    m = _DURATION_RE.match(raw)
    if not m:
        raise ValidationError(
            f"cannot read {raw!r} as a duration. Use <number><unit>, "
            f"e.g. 15m, 2h, 7d (units: ms, s, m, h, d, w, y)."
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(seconds=value * _UNIT_SECONDS[unit])


def parse_instant(text: str) -> datetime:
    """Read ``--from``/``--to``: RFC 3339, a unix timestamp, or ``now``.

    Always returns an **aware UTC** datetime. Naive datetimes are the other half
    of the silent-wrong-window bug: a naive local time compared against a UTC
    instant is off by the offset, which on this instance (CEST) is two hours —
    long enough to miss the deploy you are investigating and short enough that
    the output still looks plausible.
    """
    raw = str(text).strip()
    if raw.lower() == "now":
        return datetime.now(timezone.utc)
    if re.fullmatch(r"\d{10,19}", raw):
        # A bare number here is a unix timestamp, and the digit count tells us the
        # unit: 10 = seconds, 13 = millis, 19 = nanos. Guessing by magnitude
        # rather than asking is safe -- these ranges do not overlap this century.
        n = int(raw)
        if len(raw) >= 19:
            return datetime.fromtimestamp(n / 1e9, tz=timezone.utc)
        if len(raw) >= 13:
            return datetime.fromtimestamp(n / 1e3, tz=timezone.utc)
        return datetime.fromtimestamp(n, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"cannot read {raw!r} as a time. Use RFC 3339 "
            f"(2026-07-17T08:00:00Z), a unix timestamp, or 'now'."
        ) from exc
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TimeRange:
    """A resolved [start, end] window, plus the units each backend wants.

    Carrying the window as an object rather than two ints is what lets every
    command **report the window it actually used**. That is not a nicety: Loki's
    label endpoints are time-bounded, so "what can I get logs from" has a
    different answer at 09:00 and at 17:00. A command that does not say which
    window it asked about is telling half the truth.
    """

    __slots__ = ("start", "end")

    def __init__(self, start: datetime, end: datetime) -> None:
        if start >= end:
            raise ValidationError(
                f"empty time window: start ({start.isoformat()}) is not before "
                f"end ({end.isoformat()})."
            )
        self.start = start
        self.end = end

    @classmethod
    def resolve(
        cls,
        *,
        since: str | None = None,
        start: str | None = None,
        end: str | None = None,
        default_since: str = "1h",
    ) -> "TimeRange":
        """Build a window from the usual flags.

        Precedence: an explicit ``--from`` wins; otherwise ``--since`` counts back
        from ``--to`` (default now); otherwise the configured default.
        """
        end_dt = parse_instant(end) if end else datetime.now(timezone.utc)
        if start:
            return cls(parse_instant(start), end_dt)
        delta = parse_duration(since or default_since)
        return cls(end_dt - delta, end_dt)

    # ---- the unit conversions, named so a call site cannot get them wrong ----

    def loki(self) -> tuple[int, int]:
        """(start, end) in **nanoseconds** — for Loki through the proxy.

        19 digits, always. Loki would also accept 10-digit seconds, but emitting
        the widest unambiguous encoding means no caller can ever land in the
        11-to-18-digit range that Loki silently reads as nanoseconds.
        """
        return (_to_nanos(self.start), _to_nanos(self.end))

    def prometheus(self) -> tuple[float, float]:
        """(start, end) in **seconds** — for Prometheus/Mimir through the proxy."""
        return (self.start.timestamp(), self.end.timestamp())

    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def describe(self) -> dict:
        """The window, for embedding in every result payload."""
        return {
            "start": self.start.isoformat().replace("+00:00", "Z"),
            "end": self.end.isoformat().replace("+00:00", "Z"),
            "seconds": round(self.seconds()),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"TimeRange({self.start.isoformat()} .. {self.end.isoformat()})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, TimeRange)
            and self.start == other.start
            and self.end == other.end
        )


def _to_nanos(dt: datetime) -> int:
    """Seconds -> nanoseconds without float rounding.

    ``int(dt.timestamp() * 1e9)`` loses precision: a float64 has 53 bits of
    mantissa and a nanosecond timestamp needs ~61, so the low digits are noise.
    It rarely *matters* — but a log line's timestamp is also its identity when
    de-duplicating, so noisy low digits turn into "the same line, twice".
    """
    return int(dt.timestamp() * 1_000_000) * 1_000
