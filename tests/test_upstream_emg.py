import unittest

from myostatic_control import (
    ActivationLimit,
    ControlConfig,
    ControlCore,
    ControlEventKind,
    DetectedSqueeze,
    Muscle,
    UpstreamEmgAdapter,
)


def make_core(max_input_age_s=0.2):
    return ControlCore(
        ControlConfig(
            channel_by_muscle={
                Muscle.BICEPS: "biceps",
                Muscle.TRICEPS: "triceps",
                Muscle.FCR: "fcr",
            },
            activation_limits={
                muscle: ActivationLimit(0.25, 1.0) for muscle in Muscle
            },
            max_input_age_s=max_input_age_s,
            squeeze_sequence_window_s=1.5,
        )
    )


class FakeUpstreamReader:
    """Test double for the upstream read/output/rms public interface."""

    def __init__(self, stale_after_s=0.5):
        self.stale_after_s = stale_after_s
        self.rms = None
        self._active = False
        self._timestamp = None

    def read(self, samples, timestamp_s, *, signal_valid=True):
        values = list(samples)
        if not signal_valid or len(values) < 2:
            self.rms = None
            self._active = False
            return False
        self.rms = 0.5  # A non-None measurement indicates a valid fake window.
        self._active = bool(values[0])
        self._timestamp = timestamp_s
        return self._active

    def output(self, now_s):
        if self._timestamp is None or now_s - self._timestamp > self.stale_after_s:
            self.rms = None
            self._active = False
            return False
        return self._active


class UpstreamEmgAdapterTests(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.readers = {muscle: FakeUpstreamReader() for muscle in Muscle}
        self.adapter = UpstreamEmgAdapter(
            readers=self.readers,
            core=self.core,
            active_activation_by_muscle={muscle: 0.6 for muscle in Muscle},
        )
        self.now = 1.0

    def process(self, active=(), *, valid=None, age=0.0, squeezes=()):
        self.now += 0.1
        samples = {
            muscle: ([1.0, 1.0] if muscle in active else [0.0, 0.0])
            for muscle in Muscle
        }
        qualities = {muscle: True for muscle in Muscle}
        if valid is not None:
            qualities[valid] = False
        return self.adapter.process_window(
            samples_by_muscle=samples,
            signal_valid_by_muscle=qualities,
            sample_timestamp_s=self.now - age,
            now_s=self.now,
            squeeze_events=squeezes,
        )

    def test_upstream_levels_are_encoded_as_explicit_normalized_activations(self):
        relaxed = self.process()
        self.assertTrue(relaxed.input_valid)
        self.assertEqual(relaxed.hold_muscles, frozenset(Muscle))
        self.assertFalse(relaxed.coactivated)

        active = self.process({Muscle.BICEPS, Muscle.TRICEPS})
        self.assertEqual(
            active.active_muscles, frozenset({Muscle.BICEPS, Muscle.TRICEPS})
        )
        self.assertTrue(active.coactivated)
        self.assertEqual(active.events, ())

    def test_invalid_reader_signal_invalidates_all_muscles(self):
        result = self.process({Muscle.BICEPS}, valid=Muscle.FCR)
        self.assertFalse(result.input_valid)
        self.assertEqual(result.hold_muscles, frozenset(Muscle))
        self.assertIn("channels", result.invalid_reason)

    def test_stale_source_data_fails_to_logical_hold(self):
        result = self.process({Muscle.BICEPS}, age=0.21)
        self.assertFalse(result.input_valid)
        self.assertEqual(result.hold_muscles, frozenset(Muscle))
        self.assertEqual(result.invalid_reason, "sample is stale")

    def test_squeeze_events_are_forwarded_but_not_inferred_from_levels(self):
        first = self.process({Muscle.BICEPS})
        second = self.process({Muscle.BICEPS})
        self.assertEqual(first.events, ())
        self.assertEqual(second.events, ())

        first_squeeze = DetectedSqueeze(
            timestamp_s=self.now + 0.1, muscles=frozenset({Muscle.BICEPS})
        )
        self.assertEqual(self.process(squeezes=(first_squeeze,)).events, ())
        second_squeeze = DetectedSqueeze(
            timestamp_s=self.now + 0.1, muscles=frozenset({Muscle.BICEPS})
        )
        result = self.process(squeezes=(second_squeeze,))
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].kind, ControlEventKind.RESET_REQUEST)

    def test_active_level_coefficients_must_be_within_core_limits(self):
        with self.assertRaises(ValueError):
            UpstreamEmgAdapter(
                readers=self.readers,
                core=self.core,
                active_activation_by_muscle={
                    Muscle.BICEPS: 0.2,
                    Muscle.TRICEPS: 0.6,
                    Muscle.FCR: 0.6,
                },
            )
        with self.assertRaises(ValueError):
            UpstreamEmgAdapter(
                readers=self.readers,
                core=self.core,
                active_activation_by_muscle={
                    Muscle.BICEPS: 1.0,
                    Muscle.TRICEPS: 0.6,
                    Muscle.FCR: 0.6,
                },
            )

    def test_malformed_window_mapping_fails_inactive(self):
        self.now += 0.1
        self.process({Muscle.BICEPS})
        self.now += 0.1
        result = self.adapter.process_window(
            samples_by_muscle={},
            signal_valid_by_muscle={muscle: True for muscle in Muscle},
            sample_timestamp_s=self.now,
            now_s=self.now,
        )
        self.assertFalse(result.input_valid)
        self.assertEqual(result.hold_muscles, frozenset(Muscle))
        self.assertIsNone(self.readers[Muscle.BICEPS].rms)


if __name__ == "__main__":
    unittest.main()
