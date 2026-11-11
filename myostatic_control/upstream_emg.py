"""Adapter for the project's hardware-independent upstream EMG readers.

The upstream repository performs per-muscle calibration and converts raw
sample windows into a debounced boolean level plus a window RMS value. It does
not acquire samples. This adapter accepts already-acquired windows and maps
those boolean levels to explicitly configured coefficients for ``ControlCore``;
it never maps raw samples or RMS values directly to actuator commands.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, Mapping, Protocol

from .core import ControlCore, ControlSnapshot, DetectedSqueeze, Muscle


class EmgReader(Protocol):
    """Public interface evidenced by the upstream reader classes."""

    @property
    def rms(self) -> float | None: ...

    def read(
        self, samples: Iterable[float], timestamp_s: float, *, signal_valid: bool = True
    ) -> bool: ...

    def output(self, now_s: float) -> bool: ...


class UpstreamEmgAdapter:
    """Feed the upstream readers' calibrated levels into the logical core.

    ``active_activation_by_muscle`` is a required, caller-selected coefficient
    per muscle. A relaxed reader level is encoded as 0.0; an active level is
    encoded using this mapping. Every configured active value must satisfy that
    muscle's ``ControlCore`` activation minimum and remain below its saturation
    limit. This is a discrete bridge from the upstream reader's already
    calibrated/debounced level, not a reconstruction of normalized EMG
    amplitude.

    Acquisition, channel wiring, and signal-quality assessment remain outside
    this adapter. ``squeeze_events`` must be supplied by a separate validated
    detector; reader activity is never interpreted as a squeeze.
    """

    def __init__(
        self,
        *,
        readers: Mapping[Muscle, EmgReader],
        core: ControlCore,
        active_activation_by_muscle: Mapping[Muscle, float],
    ) -> None:
        expected = set(Muscle)
        if set(readers) != expected:
            raise ValueError("readers must define exactly all three muscles")
        if set(active_activation_by_muscle) != expected:
            raise ValueError(
                "active_activation_by_muscle must define exactly all three muscles"
            )
        if not isinstance(core, ControlCore):
            raise TypeError("core must be a ControlCore")

        config = core.config
        activation_values: Dict[Muscle, float] = {}
        for muscle, value in active_activation_by_muscle.items():
            try:
                finite_value = math.isfinite(value)
            except (TypeError, OverflowError):
                finite_value = False
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not finite_value:
                raise ValueError("active activation coefficients must be finite numbers")
            limits = config.activation_limits[muscle]
            if not limits.activation_minimum <= value < limits.saturation_limit:
                raise ValueError(
                    f"active coefficient for {muscle.value} must be at least its "
                    "activation minimum and below its saturation limit"
                )
            activation_values[muscle] = float(value)

        self._readers = dict(readers)
        self._core = core
        self._active_activation = activation_values

    def process_window(
        self,
        *,
        samples_by_muscle: Mapping[Muscle, Iterable[float]],
        sample_timestamp_s: float,
        now_s: float,
        signal_valid_by_muscle: Mapping[Muscle, bool],
        squeeze_events: Iterable[DetectedSqueeze] = (),
    ) -> ControlSnapshot:
        """Process one set of acquired windows and return logical status only.

        Each sample iterable and timestamp is passed to the corresponding
        upstream reader. Missing windows, malformed mappings, an invalid
        signal, an uncalibrated reader, or stale output invalidate the complete
        three-muscle frame so ``ControlCore`` reports HOLD for all muscles.
        """
        expected = set(Muscle)
        if (
            set(samples_by_muscle) != expected
            or set(signal_valid_by_muscle) != expected
            or any(not isinstance(value, bool) for value in signal_valid_by_muscle.values())
        ):
            self._invalidate_readers(sample_timestamp_s, now_s)
            return self._core.update(
                {},
                sample_timestamp_s=sample_timestamp_s,
                now_s=now_s,
                squeeze_events=squeeze_events,
            )

        activations: Dict[str, float] = {}
        valid_frame = True
        for muscle, reader in self._readers.items():
            level = reader.read(
                samples_by_muscle[muscle],
                sample_timestamp_s,
                signal_valid=signal_valid_by_muscle[muscle],
            )
            # Upstream readers expose None RMS after invalid/un-calibrated data.
            # output(now_s) additionally expires their own stale levels.
            fresh_level = reader.output(now_s)
            if reader.rms is None:
                valid_frame = False
            activations[self._core.config.channel_by_muscle[muscle]] = (
                self._active_activation[muscle] if level and fresh_level else 0.0
            )

        if not valid_frame:
            activations.clear()
        return self._core.update(
            activations,
            sample_timestamp_s=sample_timestamp_s,
            now_s=now_s,
            squeeze_events=squeeze_events,
        )

    def _invalidate_readers(self, sample_timestamp_s: float, now_s: float) -> None:
        for reader in self._readers.values():
            reader.read((), sample_timestamp_s, signal_valid=False)
            reader.output(now_s)
