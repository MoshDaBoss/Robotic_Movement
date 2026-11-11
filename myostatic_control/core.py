"""Pure logical control core; this module has no hardware or OS interfaces.

Inputs are caller-conditioned, unitless activation coefficients, not raw EMG.
Squeeze events must likewise be detected upstream using a separately calibrated
signal-processing path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Tuple


class Muscle(str, Enum):
    BICEPS = "biceps_brachii"
    TRICEPS = "triceps_brachii"
    FCR = "flexor_carpi_radialis"


ALL_MUSCLES = frozenset(Muscle)


class ControlEventKind(str, Enum):
    RESET_REQUEST = "reset_request"
    STOP_REQUEST = "stop_request"


@dataclass(frozen=True)
class ActivationLimit:
    """Per-muscle limits for an upstream normalized activation coefficient."""

    activation_minimum: float
    saturation_limit: float

    def __post_init__(self) -> None:
        if not _finite_positive(self.activation_minimum):
            raise ValueError("activation_minimum must be finite and greater than zero")
        if not _finite_positive(self.saturation_limit):
            raise ValueError("saturation_limit must be finite and greater than zero")
        if self.activation_minimum >= self.saturation_limit:
            raise ValueError("activation_minimum must be below saturation_limit")


@dataclass(frozen=True)
class ControlConfig:
    """All policy values used by this logical core are required explicitly.

    ``channel_by_muscle`` maps project/device channel identifiers to the named
    muscles. The activation coefficients and limits must share the same
    caller-defined normalized scale.
    """

    channel_by_muscle: Mapping[Muscle, str]
    activation_limits: Mapping[Muscle, ActivationLimit]
    max_input_age_s: float
    squeeze_sequence_window_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel_by_muscle", dict(self.channel_by_muscle))
        object.__setattr__(self, "activation_limits", dict(self.activation_limits))
        expected = set(Muscle)
        if set(self.channel_by_muscle) != expected:
            raise ValueError("channel_by_muscle must define exactly all three muscles")
        channels = tuple(self.channel_by_muscle.values())
        if any(not isinstance(channel, str) or not channel.strip() for channel in channels):
            raise ValueError("channel identifiers must be non-empty strings")
        if len(set(channels)) != len(channels):
            raise ValueError("each muscle must have a distinct channel identifier")
        if set(self.activation_limits) != expected:
            raise ValueError("activation_limits must define exactly all three muscles")
        if any(not isinstance(limit, ActivationLimit) for limit in self.activation_limits.values()):
            raise ValueError("each activation limit must be an ActivationLimit")
        if not _finite_positive(self.max_input_age_s):
            raise ValueError("max_input_age_s must be finite and greater than zero")
        if not _finite_positive(self.squeeze_sequence_window_s):
            raise ValueError("squeeze_sequence_window_s must be finite and greater than zero")
        object.__setattr__(self, "channel_by_muscle", MappingProxyType(dict(self.channel_by_muscle)))
        object.__setattr__(self, "activation_limits", MappingProxyType(dict(self.activation_limits)))


@dataclass(frozen=True)
class DetectedSqueeze:
    """An upstream-confirmed squeeze, timestamped in the caller's monotonic clock.

    Supported events are one individual muscle or all three muscles together.
    Squeeze recognition, including pulse duration and release criteria, is
    deliberately outside this module.
    """

    timestamp_s: float
    muscles: FrozenSet[Muscle]

    def __post_init__(self) -> None:
        if not _finite_nonnegative(self.timestamp_s):
            raise ValueError("squeeze timestamp must be finite and non-negative")
        if not isinstance(self.muscles, (set, frozenset)) or not all(
            isinstance(muscle, Muscle) for muscle in self.muscles
        ):
            raise ValueError("muscles must be a set of configured Muscle values")
        normalized_muscles = frozenset(self.muscles)
        if len(normalized_muscles) != 1 and normalized_muscles != ALL_MUSCLES:
            raise ValueError("a squeeze must identify one muscle or all three muscles")
        object.__setattr__(self, "muscles", normalized_muscles)


@dataclass(frozen=True)
class ControlEvent:
    kind: ControlEventKind
    timestamp_s: float
    muscle: Optional[Muscle] = None


@dataclass(frozen=True)
class ControlSnapshot:
    """Logical status only; no joint target or actuator command is produced."""

    input_valid: bool
    timestamp_s: float
    active_muscles: FrozenSet[Muscle]
    hold_muscles: FrozenSet[Muscle]
    coactivated: bool
    events: Tuple[ControlEvent, ...]
    invalid_reason: Optional[str] = None


def _finite_positive(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _finite_nonnegative(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


class ControlCore:
    """Translate normalized activity and detected squeezes into logical events."""

    def __init__(self, config: ControlConfig) -> None:
        self._config = config
        self._last_sample_s: Optional[float] = None
        self._sequence_count: Dict[object, int] = {}
        self._sequence_last_s: Dict[object, float] = {}

    @property
    def config(self) -> ControlConfig:
        """The immutable policy configuration used by this core."""
        return self._config

    def update(
        self,
        activations: Mapping[str, float],
        sample_timestamp_s: float,
        now_s: float,
        squeeze_events: Iterable[DetectedSqueeze] = (),
    ) -> ControlSnapshot:
        """Return activity/hold status and event requests, never hardware output.

        ``activations`` must contain exactly the configured channel identifiers
        and caller-calibrated, non-negative normalized coefficients. A value at
        or above its configured saturation limit invalidates the whole sample.
        Invalid or stale input clears pending squeeze sequences and reports
        logical HOLD for every muscle.
        """

        invalid_reason = self._validate_frame(activations, sample_timestamp_s, now_s)
        if invalid_reason is not None:
            self._clear_sequences()
            return self._invalid_snapshot(now_s, invalid_reason)

        detected = tuple(squeeze_events)
        invalid_reason = self._validate_squeezes(detected, now_s)
        if invalid_reason is not None:
            self._clear_sequences()
            return self._invalid_snapshot(now_s, invalid_reason)

        active = frozenset(
            muscle
            for muscle, channel in self._config.channel_by_muscle.items()
            if activations[channel] >= self._config.activation_limits[muscle].activation_minimum
        )
        events = self._consume_squeezes(detected)
        self._last_sample_s = sample_timestamp_s
        return ControlSnapshot(
            input_valid=True,
            timestamp_s=sample_timestamp_s,
            active_muscles=active,
            hold_muscles=ALL_MUSCLES - active,
            coactivated=len(active) > 1,
            events=tuple(events),
        )

    def _validate_frame(
        self, activations: Mapping[str, float], sample_timestamp_s: float, now_s: float
    ) -> Optional[str]:
        if not _finite_nonnegative(sample_timestamp_s) or not _finite_nonnegative(now_s):
            return "timestamps must be finite and non-negative"
        if sample_timestamp_s > now_s:
            return "sample timestamp is in the future"
        if now_s - sample_timestamp_s > self._config.max_input_age_s:
            return "sample is stale"
        if self._last_sample_s is not None and sample_timestamp_s <= self._last_sample_s:
            return "sample timestamp is not newer than the previous sample"

        expected_channels = set(self._config.channel_by_muscle.values())
        if set(activations) != expected_channels:
            return "activation channels do not match configured channels"
        for muscle, channel in self._config.channel_by_muscle.items():
            value = activations[channel]
            if not _finite_nonnegative(value):
                return "activation coefficient is invalid"
            if value >= self._config.activation_limits[muscle].saturation_limit:
                return "activation coefficient is saturated"
        return None

    def _validate_squeezes(
        self, squeezes: Tuple[DetectedSqueeze, ...], now_s: float
    ) -> Optional[str]:
        previous_s: Optional[float] = None
        for squeeze in squeezes:
            if not isinstance(squeeze, DetectedSqueeze):
                return "squeeze events must be DetectedSqueeze values"
            if squeeze.timestamp_s > now_s:
                return "squeeze timestamp is in the future"
            if now_s - squeeze.timestamp_s > self._config.max_input_age_s:
                return "squeeze event is stale"
            if previous_s is not None and squeeze.timestamp_s < previous_s:
                return "squeeze events are not timestamp-ordered"
            previous_s = squeeze.timestamp_s
        return None

    def _consume_squeezes(
        self, squeezes: Tuple[DetectedSqueeze, ...]
    ) -> Tuple[ControlEvent, ...]:
        output = []
        for squeeze in squeezes:
            is_all = squeeze.muscles == ALL_MUSCLES
            key: object = "all_muscles" if is_all else next(iter(squeeze.muscles))
            count = self._sequence_count.get(key, 0)
            last_s = self._sequence_last_s.get(key)
            if last_s is None or squeeze.timestamp_s - last_s > self._config.squeeze_sequence_window_s:
                count = 0
            count += 1
            if (is_all and count == 3) or (not is_all and count == 2):
                output.append(
                    ControlEvent(
                        kind=ControlEventKind.STOP_REQUEST if is_all else ControlEventKind.RESET_REQUEST,
                        timestamp_s=squeeze.timestamp_s,
                        muscle=None if is_all else key,  # type: ignore[arg-type]
                    )
                )
                count = 0
                last_s = None
            else:
                last_s = squeeze.timestamp_s
            self._sequence_count[key] = count
            if last_s is None:
                self._sequence_last_s.pop(key, None)
            else:
                self._sequence_last_s[key] = last_s
        return tuple(output)

    def _clear_sequences(self) -> None:
        self._sequence_count.clear()
        self._sequence_last_s.clear()

    @staticmethod
    def _invalid_snapshot(timestamp_s: float, reason: str) -> ControlSnapshot:
        return ControlSnapshot(
            input_valid=False,
            timestamp_s=timestamp_s if _finite_nonnegative(timestamp_s) else 0.0,
            active_muscles=frozenset(),
            hold_muscles=ALL_MUSCLES,
            coactivated=False,
            events=(),
            invalid_reason=reason,
        )
