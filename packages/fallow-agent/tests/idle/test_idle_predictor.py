"""Unit tests for the idle-time predictor.

Every reading is scripted through ``FakeIdleDetector``; there is no clock and no
timer, so the model is exercised deterministically.
"""

from fallow_agent.idle import FakeIdleDetector, IdlePredictor


def _learn_windows(detector: FakeIdleDetector, predictor: IdlePredictor, length: float, count: int):
    """Drive ``count`` complete idle windows of ``length`` seconds each.

    A window is "seen" only when input resets the reading, so each iteration
    ramps idle up to ``length`` and then simulates a keystroke.
    """
    for _ in range(count):
        detector.set_idle(length)
        predictor.sample()
        detector.simulate_input()
        predictor.sample()


def test_no_history_predicts_nothing_with_zero_confidence():
    predictor = IdlePredictor(FakeIdleDetector(0.0))
    prediction = predictor.sample()
    assert prediction.remaining_s == 0.0
    assert prediction.confidence == 0.0


def test_remaining_rises_as_the_idle_window_extends():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=3)
    _learn_windows(detector, predictor, length=100.0, count=3)

    detector.set_idle(20.0)
    low = predictor.sample()
    detector.set_idle(60.0)
    mid = predictor.sample()
    detector.set_idle(90.0)
    high = predictor.sample()

    assert low.remaining_s < mid.remaining_s < high.remaining_s


def test_remaining_is_capped_at_the_typical_window():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=3)
    _learn_windows(detector, predictor, length=100.0, count=4)

    detector.set_idle(1000.0)  # far past anything in history
    prediction = predictor.sample()
    assert prediction.remaining_s <= 100.0


def test_estimate_drops_after_input_resets_the_window():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=3)
    _learn_windows(detector, predictor, length=100.0, count=3)

    detector.set_idle(90.0)
    extended = predictor.sample()
    detector.simulate_input()
    after_reset = predictor.sample()

    assert after_reset.remaining_s < extended.remaining_s
    assert after_reset.remaining_s == 0.0


def test_confidence_grows_as_windows_accumulate():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=4)

    seen = []
    for _ in range(4):
        _learn_windows(detector, predictor, length=100.0, count=1)
        detector.set_idle(50.0)  # sample mid-window, within the typical length
        seen.append(predictor.sample().confidence)

    assert seen == sorted(seen)  # non-decreasing
    assert seen[0] < seen[-1]
    assert seen[-1] == 1.0


def test_confidence_decays_when_extrapolating_beyond_history():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=3)
    _learn_windows(detector, predictor, length=100.0, count=4)

    detector.set_idle(80.0)  # within the typical window
    within = predictor.sample()
    detector.set_idle(400.0)  # well beyond it
    beyond = predictor.sample()

    assert beyond.confidence < within.confidence


def test_confidence_never_leaves_the_unit_interval():
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, min_windows=2)
    _learn_windows(detector, predictor, length=100.0, count=6)

    for idle in (0.0, 25.0, 100.0, 250.0, 10_000.0):
        detector.set_idle(idle)
        confidence = predictor.sample().confidence
        assert 0.0 <= confidence <= 1.0


def test_history_is_bounded():
    # With the window count capped at 4 but 8 needed for full confidence, the
    # sample term can never exceed 4/8, no matter how many windows are fed.
    detector = FakeIdleDetector(0.0)
    predictor = IdlePredictor(detector, history=4, min_windows=8)
    _learn_windows(detector, predictor, length=100.0, count=40)

    detector.set_idle(50.0)  # sample within the typical window, so fit == 1.0
    assert predictor.sample().confidence == 0.5
