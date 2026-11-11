import unittest

from myostatic_control import (
    ActivationLimit,
    ControlConfig,
    ControlCore,
    ControlEventKind,
    DetectedSqueeze,
    Muscle,
)


def make_config():
    return ControlConfig(
        channel_by_muscle={
            Muscle.BICEPS: "emg_b",
            Muscle.TRICEPS: "emg_t",
            Muscle.FCR: "emg_f",
        },
        activation_limits={
            muscle: ActivationLimit(activation_minimum=0.25, saturation_limit=1.0)
            for muscle in Muscle
        },
        max_input_age_s=0.2,
        squeeze_sequence_window_s=1.5,
    )


def frame(biceps=0.0, triceps=0.0, fcr=0.0):
    return {"emg_b": biceps, "emg_t": triceps, "emg_f": fcr}


class ControlCoreTests(unittest.TestCase):
    def setUp(self):
        self.core = ControlCore(make_config())
        self.now = 1.0

    def update(self, activations=None, squeezes=(), age=0.0):
        self.now += 0.1
        return self.core.update(
            frame() if activations is None else activations,
            sample_timestamp_s=self.now - age,
            now_s=self.now,
            squeeze_events=squeezes,
        )

    def test_relaxed_activity_is_logical_hold(self):
        result = self.update()
        self.assertTrue(result.input_valid)
        self.assertEqual(result.active_muscles, frozenset())
        self.assertEqual(result.hold_muscles, frozenset(Muscle))
        self.assertFalse(result.coactivated)
        self.assertEqual(result.events, ())

    def test_activation_threshold_and_coactivation_are_reported(self):
        below = self.update(frame(biceps=0.249))
        self.assertNotIn(Muscle.BICEPS, below.active_muscles)
        active = self.update(frame(biceps=0.25))
        self.assertEqual(active.active_muscles, frozenset({Muscle.BICEPS}))
        self.assertEqual(active.hold_muscles, frozenset(Muscle) - {Muscle.BICEPS})
        simultaneous = self.update(frame(biceps=0.4, triceps=0.7))
        self.assertEqual(
            simultaneous.active_muscles, frozenset({Muscle.BICEPS, Muscle.TRICEPS})
        )
        self.assertTrue(simultaneous.coactivated)
        self.assertEqual(simultaneous.events, ())

    def test_stale_missing_invalid_and_saturated_inputs_hold_everything(self):
        stale = self.update(age=0.21)
        self.assertFalse(stale.input_valid)
        self.assertEqual(stale.hold_muscles, frozenset(Muscle))
        self.assertEqual(stale.invalid_reason, "sample is stale")

        missing = self.update({"emg_b": 0.0})
        self.assertFalse(missing.input_valid)
        self.assertIn("channels", missing.invalid_reason)

        invalid = self.update(frame(fcr=float("nan")))
        self.assertFalse(invalid.input_valid)
        self.assertIn("invalid", invalid.invalid_reason)

        saturated = self.update(frame(fcr=1.0))
        self.assertFalse(saturated.input_valid)
        self.assertEqual(saturated.invalid_reason, "activation coefficient is saturated")

    def test_two_individual_squeezes_emit_logical_reset_request(self):
        first = DetectedSqueeze(self.now + 0.1, frozenset({Muscle.BICEPS}))
        self.assertEqual(self.update(squeezes=(first,)).events, ())
        second = DetectedSqueeze(self.now + 0.1, frozenset({Muscle.BICEPS}))
        result = self.update(squeezes=(second,))
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].kind, ControlEventKind.RESET_REQUEST)
        self.assertEqual(result.events[0].muscle, Muscle.BICEPS)

    def test_three_all_muscle_squeezes_emit_only_stop_request(self):
        events = []
        for _ in range(3):
            squeeze = DetectedSqueeze(self.now + 0.1, frozenset(Muscle))
            events.extend(self.update(squeezes=(squeeze,)).events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, ControlEventKind.STOP_REQUEST)
        self.assertIsNone(events[0].muscle)

    def test_expired_squeeze_sequence_starts_again(self):
        first = DetectedSqueeze(self.now + 0.1, frozenset({Muscle.FCR}))
        self.assertEqual(self.update(squeezes=(first,)).events, ())
        timestamp = self.now + 1.7
        second = DetectedSqueeze(timestamp, frozenset({Muscle.FCR}))
        result = self.core.update(
            frame(), sample_timestamp_s=timestamp, now_s=timestamp, squeeze_events=(second,)
        )
        self.assertTrue(result.input_valid)
        self.assertEqual(result.events, ())

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ActivationLimit(activation_minimum=0.5, saturation_limit=0.5)
        with self.assertRaises(ValueError):
            ControlConfig(
                channel_by_muscle={},
                activation_limits={},
                max_input_age_s=0.2,
                squeeze_sequence_window_s=1.5,
            )

    def test_squeeze_timing_and_shape_are_validated(self):
        with self.assertRaises(ValueError):
            DetectedSqueeze(0.0, frozenset())
        self.assertFalse(
            self.update(
                squeezes=(DetectedSqueeze(self.now + 1.0, frozenset({Muscle.FCR})),)
            ).input_valid
        )


if __name__ == "__main__":
    unittest.main()
