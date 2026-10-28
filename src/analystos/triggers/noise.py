"""Anti-noise gate for monitors and triggers (spec S09 §3.2): debounce, cooldown, quiet hours, hysteresis,
correlation and frequency caps. Pure functions over the monitor's configuration and its `noise_state`, so
every rule is testable without a database and the same gate serves scheduled, webhook and Kafka triggers.

Semantics (each is configuration on the monitor row):

* **hysteresis** (`condition: {enter, exit}`): a monitor enters `alerting` when `enter` holds and stays there
  until `exit` holds, so a value oscillating between the two thresholds raises one alert, not one per
  reading. Without it the monitor's own verdict is `enter` and its negation is `exit` (previous behaviour).
* **debounce** (a duration): the condition must still hold that long after it was first seen before
  anything fires; a single spike that is gone by the next reading never alerts.
* **cooldown** (a duration): after a fire, a re-entry within the cooldown is suppressed (counted, not raised).
* **quiet_hours** (`{window: "22:00-07:00", tz: "America/New_York", allow_critical: false}`; the time zone is
  required): a fire inside the window is recorded, but people are told when the window ends.
* **caps** (`{per_dataset_per_day, per_tenant_per_day}`): beyond the cap a fire is suppressed.
* **correlation_key** (`[metric, processor]`): alerts with the same values join one open alert group.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from analystos.core.errors import InvalidInput
from analystos.triggers import conditions

NOISE_KEYS = ("condition", "hysteresis", "debounce", "cooldown", "quiet_hours", "correlation_key", "caps", "timezone")
CAP_KEYS = ("per_dataset_per_day", "per_tenant_per_day")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_DURATION = timedelta(days=30)


def parse_duration(value: Any, label: str) -> timedelta | None:
    """`2h`, `30m`, `1d`, `90` (seconds) or None."""
    if value is None or value == 0 or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidInput(f"{label} must be a duration like 30m, 2h or 1d")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        m = _DURATION.match(str(value))
        if not m:
            raise InvalidInput(f"{label} must be a duration like 30m, 2h or 1d (got {value!r})")
        seconds = float(m.group(1)) * _UNITS[m.group(2)]
    if seconds < 0 or timedelta(seconds=seconds) > MAX_DURATION:
        raise InvalidInput(f"{label} must be between 0 and 30 days")
    return timedelta(seconds=seconds) if seconds else None


def _clock(value: str, label: str) -> time:
    m = _CLOCK.match(str(value or "").strip())
    if not m:
        raise InvalidInput(f"{label} must be HH:MM (24-hour)")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time
    tz: str
    allow_critical: bool = False

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.tz))

    def active(self, now: datetime) -> bool:
        t = self._local(now).time()
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # wraps midnight

    def ends(self, now: datetime) -> datetime:
        """The UTC instant the window that contains `now` ends (DST-aware through the zone)."""
        local = self._local(now)
        end = local.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        if end <= local:
            end = (local + timedelta(days=1)).replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        # replace() keeps the offset of `local`; rebuild the wall time in the zone so a DST change in between
        # lands on the right instant.
        end = datetime(end.year, end.month, end.day, end.hour, end.minute, tzinfo=ZoneInfo(self.tz))
        return end.astimezone(UTC)

    def defers(self, now: datetime, severity: str | None) -> datetime | None:
        if not self.active(now) or (self.allow_critical and severity == "critical"):
            return None
        return self.ends(now)


def parse_quiet_hours(value: Any, default_tz: str | None) -> QuietHours | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = {"window": value}
    if not isinstance(value, dict):
        raise InvalidInput('quiet_hours must be {window: "22:00-07:00", tz: "<IANA zone>"}')
    extra = set(value) - {"window", "start", "end", "tz", "allow_critical"}
    if extra:
        raise InvalidInput(f"unknown quiet_hours fields: {sorted(extra)}")
    if value.get("window"):
        start, sep, end = str(value["window"]).partition("-")
        if not sep:
            raise InvalidInput('quiet_hours.window must look like "22:00-07:00"')
    else:
        start, end = value.get("start"), value.get("end")
    tz = value.get("tz") or default_tz
    if not tz:
        raise InvalidInput("quiet_hours needs an explicit time zone (quiet_hours.tz or config.timezone)")
    try:
        ZoneInfo(str(tz))
    except Exception:
        raise InvalidInput(f"unknown time zone {tz}") from None
    return QuietHours(_clock(start, "quiet_hours start"), _clock(end, "quiet_hours end"), str(tz),
                      bool(value.get("allow_critical", False)))


@dataclass(frozen=True)
class NoiseConfig:
    enter: str | None = None
    exit: str | None = None
    debounce: timedelta | None = None
    cooldown: timedelta | None = None
    quiet_hours: QuietHours | None = None
    correlation_key: tuple[str, ...] = ()
    caps: dict[str, int] = field(default_factory=dict)


def configured(config: dict[str, Any] | None) -> bool:
    """Does this monitor use the gate at all? Monitors without any of its keys keep the previous path."""
    return any(k in (config or {}) for k in NOISE_KEYS if k != "timezone")


def parse(config: dict[str, Any] | None) -> NoiseConfig:
    """Validate the anti-noise part of a monitor config; InvalidInput says what is wrong."""
    config = config or {}
    cond = config.get("condition") if config.get("condition") is not None else config.get("hysteresis")
    enter = exit_ = None
    if cond is not None:
        if not isinstance(cond, dict) or not cond.get("enter"):
            raise InvalidInput("condition must be {enter: <expression>, exit: <expression>}")
        extra = set(cond) - {"enter", "exit"}
        if extra:
            raise InvalidInput(f"unknown condition fields: {sorted(extra)}")
        enter = str(cond["enter"])
        conditions.parse(enter)
        if cond.get("exit"):
            exit_ = str(cond["exit"])
            conditions.parse(exit_)
    key = config.get("correlation_key") or []
    if isinstance(key, str):
        key = [key]
    if not isinstance(key, list) or not all(isinstance(k, str) and k.strip() for k in key) or len(key) > 6:
        raise InvalidInput("correlation_key must be a list of up to 6 field names, e.g. [metric, processor]")
    caps = config.get("caps") or {}
    if not isinstance(caps, dict) or set(caps) - set(CAP_KEYS) or \
            not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in caps.values()):
        raise InvalidInput(f"caps must be positive integers for {list(CAP_KEYS)}")
    return NoiseConfig(enter=enter, exit=exit_, debounce=parse_duration(config.get("debounce"), "debounce"),
                       cooldown=parse_duration(config.get("cooldown"), "cooldown"),
                       quiet_hours=parse_quiet_hours(config.get("quiet_hours"), config.get("timezone")),
                       correlation_key=tuple(k.strip() for k in key), caps=dict(caps))


# ------------------------------------------------------------------------------------ observations
def variables(result: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a condition can name: the monitor reading (`observed`, `baseline`, `n`, `z`, `pct_change`, every
    numeric field of the result) and, for an observed trigger event, the event's typed payload fields."""
    env: dict[str, Any] = {k: v for k, v in result.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for k, v in (payload or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            env[k] = v
    if "observed" not in env and isinstance(result.get("value"), (int, float)):
        env["observed"] = result["value"]
    if "baseline" not in env:
        for k in ("baseline_median", "expected", "before_mean"):
            if isinstance(result.get(k), (int, float)):
                env["baseline"] = result[k]
                break
    if "n" not in env and isinstance(result.get("points"), int):
        env["n"] = result["points"]
    return env


def breach_and_exit(cfg: NoiseConfig, result: dict[str, Any], env: dict[str, Any]) -> tuple[bool | None, bool | None]:
    """(enter holds, exit holds): from the configured conditions, else from the monitor's own verdict."""
    own = result.get("alert")
    breach = conditions.evaluate(cfg.enter, env) if cfg.enter else (None if own is None else bool(own))
    if cfg.exit:
        exited = conditions.evaluate(cfg.exit, env)
    elif cfg.enter:
        exited = None if breach is None else not breach
    else:
        exited = None if own is None else not bool(own)
    return breach, exited


def correlation_values(names: tuple[str, ...], *, monitor_config: dict[str, Any], result: dict[str, Any],
                       payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The value of each correlation field: the event payload, the reading, the monitor's scope filters, its config."""
    scope = (monitor_config.get("scope") or {}) if isinstance(monitor_config.get("scope"), dict) else {}
    filters = scope.get("filters") or {}
    out: dict[str, Any] = {}
    for name in names:
        for source in (payload or {}, result, result.get("dims") or {}, filters, monitor_config):
            if isinstance(source, dict) and source.get(name) is not None and not isinstance(source.get(name), (dict, list)):
                out[name] = source[name]
                break
        else:
            out[name] = None
    return out


# ------------------------------------------------------------------------------------ the gate
@dataclass
class Decision:
    """`action`: fire | hold | pending | suppressed | clear | ok | unknown. `fire` raises an alert;
    `keep_open` means open alerts must not be resolved by this reading."""

    action: str
    phase: str
    state: dict[str, Any]
    reason: str = ""
    defer_until: datetime | None = None

    @property
    def fire(self) -> bool:
        return self.action == "fire"

    @property
    def keep_open(self) -> bool:
        return self.action in ("hold", "pending", "suppressed", "unknown")

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "phase": self.phase, "reason": self.reason,
                "defer_until": self.defer_until.isoformat() if self.defer_until else None}


def _at(value: Any) -> datetime | None:
    if not value:
        return None
    d = datetime.fromisoformat(value) if isinstance(value, str) else value
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def decide(cfg: NoiseConfig, state: dict[str, Any] | None, *, breach: bool | None, exited: bool | None,
           now: datetime, severity: str | None = None, cap_hit: str | None = None) -> Decision:
    """One reading through the gate. Returns the action and the monitor's next `noise_state`."""
    st = dict(state or {})
    phase = st.get("phase", "ok")
    st["last_seen_at"] = now.isoformat()
    if phase == "alerting":
        if exited is True:
            st.update(phase="ok", pending_since=None, cleared_at=now.isoformat())
            return Decision("clear", "ok", st, "exit condition met")
        if exited is None and breach is None:
            return Decision("unknown", phase, st, "the reading lacks a value the exit condition needs")
        return Decision("hold", phase, st, "still alerting: exit condition not met (hysteresis)")
    if breach is None:
        return Decision("unknown", phase, st, "the reading lacks a value the enter condition needs")
    if not breach:
        st.update(phase="ok", pending_since=None)
        return Decision("ok", "ok", st, "pending condition cleared before the debounce window" if phase == "pending" else "")
    if cfg.debounce is not None:
        since = _at(st.get("pending_since")) or now
        st["pending_since"] = since.isoformat()
        if now - since < cfg.debounce:
            st["phase"] = "pending"
            return Decision("pending", "pending", st,
                            f"debounce: condition must hold until {(since + cfg.debounce).isoformat()}")
    last = _at(st.get("last_fired_at"))
    if cfg.cooldown is not None and last is not None and now - last < cfg.cooldown:
        st["suppressed"] = int(st.get("suppressed") or 0) + 1
        st["phase"] = "pending" if cfg.debounce is not None else "ok"
        return Decision("suppressed", st["phase"], st, f"cooldown until {(last + cfg.cooldown).isoformat()}")
    if cap_hit:
        st["suppressed"] = int(st.get("suppressed") or 0) + 1
        st["phase"] = "pending" if cfg.debounce is not None else "ok"
        return Decision("suppressed", st["phase"], st, f"frequency cap reached: {cap_hit}")
    st.update(phase="alerting", pending_since=None, last_fired_at=now.isoformat(),
              fired=int(st.get("fired") or 0) + 1)
    defer = cfg.quiet_hours.defers(now, severity) if cfg.quiet_hours else None
    return Decision("fire", "alerting", st, "quiet hours: notification deferred" if defer else "", defer)
