# Robotic_Movement
Take the myostatic and make it movement

## Non-actuating control core

The `myostatic_control` Python package currently provides **logical signal
status and event requests only**. It does not acquire or condition raw EMG,
map a muscle to a joint, generate a position/velocity target, access GPIO or a
servo API, invoke shutdown, or implement an emergency stop.

The core accepts one caller-provided, calibrated, normalized, non-negative
activation coefficient for each configured muscle channel. The unitless scale,
channel identifiers, per-muscle activation minimum, saturation limit, maximum
input age, and squeeze-sequence window must all be explicitly configured; the
library supplies no defaults. The upstream acquisition/conditioning system
must establish and validate that scale and its calibration. A missing, stale,
invalid, or saturated sample is reported invalid and produces logical HOLD
status for every muscle. HOLD is not a physical hold guarantee.

Use the standard library only:

```python
from myostatic_control import (
    ActivationLimit, ControlConfig, ControlCore, DetectedSqueeze, Muscle
)

config = ControlConfig(
    channel_by_muscle={
        Muscle.BICEPS: "configured_biceps_channel",
        Muscle.TRICEPS: "configured_triceps_channel",
        Muscle.FCR: "configured_fcr_channel",
    },
    activation_limits={
        # Replace these illustrative coefficients with validated calibration.
        Muscle.BICEPS: ActivationLimit(activation_minimum=0.25, saturation_limit=1.0),
        Muscle.TRICEPS: ActivationLimit(activation_minimum=0.25, saturation_limit=1.0),
        Muscle.FCR: ActivationLimit(activation_minimum=0.25, saturation_limit=1.0),
    },
    max_input_age_s=0.2,            # Replace with the validated input timeout.
    squeeze_sequence_window_s=1.5, # Replace with the validated squeeze timing.
)
core = ControlCore(config)
status = core.update(
    activations={
        "configured_biceps_channel": 0.0,
        "configured_triceps_channel": 0.0,
        "configured_fcr_channel": 0.0,
    },
    sample_timestamp_s=10.0,
    now_s=10.0,  # Caller-supplied timestamps must use one monotonic clock.
)
```

Activation at or above the configured minimum is reported active; inactive
muscles are reported in `hold_muscles`, and simultaneous active muscles are
reported as co-activation. The core does not infer a movement from these
states. A two-squeeze individual reset or three-squeeze all-muscle stop request
can only be counted from upstream `DetectedSqueeze` events. The upstream
detector must define and validate what constitutes a squeeze; the configured
sequence window only bounds the spacing between already-detected events.
These produce `RESET_REQUEST` and `STOP_REQUEST` events respectively. Neither
event changes a physical default position, stops hardware, or shuts down the
computer. A stop request must be handled by an independently implemented,
hardware-rated safety path and must not replace an emergency stop or interlock.

The described forearm-rotation, biceps/elbow, and triceps/claw intentions are
not wired to outputs: this repository has no established EMG format/calibration,
sensor/channel layout, joint/DOF configuration, actuator interface, motion
limits, or safe-state behavior. FCR's intended control and physical default
positions also remain unspecified. Resolve and validate these before adding
any actuator mapping. No physical hardware validation is implied.

## Adapter for the upstream EMG reader project

The public source project,
[myostatic-electrical-signal-reading-biceps-brachii-triceps-brachii-Flexor-Carpi-Radialis-](https://github.com/MoshDaBoss/myostatic-electrical-signal-reading-biceps-brachii-triceps-brachii-Flexor-Carpi-Radialis-),
contains hardware-independent `BicepsEmgReader`, `TricepsEmgReader`, and
`FlexorCarpiRadialisEmgReader` classes. Each accepts a caller-acquired sample
window, performs mean removal and RMS measurement, applies its separately
calibrated rest/contraction thresholds, hysteresis, debounce, and stale-data
handling, and exposes a boolean level and `rms`. It does **not** implement
sensor/ADC acquisition or a robot connection. Its documentation specifies no
raw units, sampling rate, sensor, wiring, or acquisition API.

The upstream reader code's software defaults are a minimum of two samples per
window, activation/release at 0.60/0.35 of the calibrated rest-to-contraction
RMS span, two consecutive windows for activation and release, and a 0.5-second
stale timeout. These are source-code defaults, not validated settings for this
robot or an implied acquisition cadence. Calibration must be performed per
reader with real relaxed and deliberate-contraction windows before reads.

`myostatic_control.UpstreamEmgAdapter` bridges those reader instances to the
existing logical core without requiring a hardware dependency. Install or
place the upstream Python modules on `PYTHONPATH`, then provide your own
acquisition windows, validated per-muscle signal-quality decisions, and
explicit coefficient configuration. For example, the values and window names
below are placeholders, not hardware recommendations:

```python
from biceps_emg import BicepsEmgReader
from triceps_emg import TricepsEmgReader
from flexor_carpi_radialis_emg import FlexorCarpiRadialisEmgReader
from myostatic_control import Muscle, UpstreamEmgAdapter

readers = {
    Muscle.BICEPS: BicepsEmgReader(),  # Configure thresholds/timeouts as validated.
    Muscle.TRICEPS: TricepsEmgReader(),
    Muscle.FCR: FlexorCarpiRadialisEmgReader(),
}
# Before use, calibrate each reader using its documented
# calibrate_rest(...) then calibrate_contraction(...) calls and actual windows.
adapter = UpstreamEmgAdapter(
    readers=readers,
    core=core,  # A ControlCore configured for the same three muscles.
    # Explicit binary encoding of each reader's calibrated active level.
    # Every value must meet that muscle's configured activation minimum and
    # remain below its saturation limit.
    active_activation_by_muscle={
        Muscle.BICEPS: configured_biceps_active_coefficient,
        Muscle.TRICEPS: configured_triceps_active_coefficient,
        Muscle.FCR: configured_fcr_active_coefficient,
    },
)
snapshot = adapter.process_window(
    samples_by_muscle={
        Muscle.BICEPS: biceps_window,
        Muscle.TRICEPS: triceps_window,
        Muscle.FCR: fcr_window,
    },
    signal_valid_by_muscle={
        Muscle.BICEPS: biceps_quality_ok,
        Muscle.TRICEPS: triceps_quality_ok,
        Muscle.FCR: fcr_quality_ok,
    },
    sample_timestamp_s=window_monotonic_timestamp,
    now_s=monotonic_now,
)
```

The adapter maps an upstream inactive/active **level** to `0.0`/the explicitly
configured active coefficient; it does not convert raw RMS amplitude to a
continuous normalized activation. Invalid, uncalibrated, missing, or stale
input invalidates the complete frame and yields logical HOLD. The source
readers do not report a raw-input clipping/overrange condition or recognize
squeeze gestures. The acquisition layer must detect saturation using the
actual device specification and mark that input invalid; that capability
cannot be implemented without the device's raw range/quality interface. Any
reset/stop sequence still needs a separate, validated squeeze detector and can
only be passed to the adapter as `DetectedSqueeze` events. No squeeze,
movement, GPIO, or servo behavior is inferred by this bridge.

Before any physical integration, the remaining blockers are an actual sensor
acquisition implementation and its units/cadence/quality checks; validated
calibration, thresholds and normalized coefficient encoding; squeeze
recognition criteria; verified sensor placement and degree-of-freedom/direction
mappings; servo protocol and tendon/joint geometry; documented position,
velocity and hardware limits; and an independently validated physical safe
state and emergency-stop/interlock path. The current adapter has no live
device connection or actuation code.

Run the synthetic, non-hardware unit tests with:

```sh
python -m unittest discover -s tests -v
```
