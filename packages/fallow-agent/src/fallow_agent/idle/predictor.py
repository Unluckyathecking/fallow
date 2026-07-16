"""Near-future idle prediction on top of the idle detector (module A1).

The detectors answer "how many seconds since the last input, right now?". This
predicts the near future from that stream: how much longer the current idle
window is likely to last, and how much to trust that estimate. The fabric can
then avoid loading a multi-GB model onto a machine that has only just gone idle,
and wind down before predicted use instead of only reacting to input.

Model (a deliberately simple, explainable first cut -- see ADR 049):

- Each time the detector's reading drops (the user touched the machine), the
  window that just ended is recorded and folded into an exponential moving
  average of typical idle-window length. History is bounded.
- The remaining estimate for the *current* window is ``min(elapsed, typical)``:
  a machine that has already stayed idle longer is projected to stay idle longer
  (the inspection-paradox intuition), capped at the typical window so a single
  long absence does not make the estimate run away.
- Confidence rises with the number of windows observed and falls once the
  current window runs past anything in recent history (we are extrapolating).

Deterministic and injectable: the detector's reading is itself the elapsed-time
signal, so no wall clock or timer is introduced -- a second time source would
only drift against the detector. Tests script the readings through
``FakeIdleDetector`` and assert on the output.
"""

from __future__ import annotations

from collections import deque
from typing import NamedTuple

from fallow_protocol.interfaces import IdleDetector

DEFAULT_HISTORY = 32  # completed idle-window lengths kept for the average
DEFAULT_SMOOTHING = 0.3  # EWMA weight on the newest window (0, 1]
DEFAULT_MIN_WINDOWS = 4  # windows observed before confidence can reach 1.0
DEFAULT_RESET_EPSILON_S = 1.0  # a drop this far below the last reading = input

_ZERO = 0.0
_FULL_CONFIDENCE = 1.0


class IdlePrediction(NamedTuple):
    """The predictor's output for one sample.

    ``remaining_s`` is how much longer the current idle window is projected to
    last; ``confidence`` is a 0..1 trust weight for that projection.
    """

    remaining_s: float
    confidence: float


def _clamp_unit(value: float) -> float:
    return max(_ZERO, min(_FULL_CONFIDENCE, value))


class IdlePredictor:
    """Predicts remaining idle time from a stream of detector readings.

    Consumes the :class:`IdleDetector` seam (injected, never a concrete
    detector). :meth:`sample` is called once per heartbeat: it folds the latest
    reading into the model and returns the current prediction.
    """

    def __init__(
        self,
        detector: IdleDetector,
        *,
        history: int = DEFAULT_HISTORY,
        smoothing: float = DEFAULT_SMOOTHING,
        min_windows: int = DEFAULT_MIN_WINDOWS,
        reset_epsilon_s: float = DEFAULT_RESET_EPSILON_S,
    ) -> None:
        self._detector = detector
        self._smoothing = smoothing
        self._min_windows = min_windows
        self._reset_epsilon_s = reset_epsilon_s
        self._windows: deque[float] = deque(maxlen=history)
        self._mean: float | None = None
        self._prev_idle: float | None = None

    def sample(self) -> IdlePrediction:
        """Read the detector, update the model, and return the prediction."""
        elapsed = self._observe()
        return self._predict(elapsed)

    def _observe(self) -> float:
        """Fold the latest reading in; return the current elapsed idle seconds."""
        elapsed = max(_ZERO, self._detector.seconds_since_input())
        prev = self._prev_idle
        if prev is not None and elapsed < prev - self._reset_epsilon_s:
            self._record_window(prev)
        self._prev_idle = elapsed
        return elapsed

    def _record_window(self, length: float) -> None:
        """Add a completed idle-window length to the bounded EWMA history."""
        self._windows.append(length)
        if self._mean is None:
            self._mean = length
        else:
            self._mean = self._smoothing * length + (1.0 - self._smoothing) * self._mean

    def _predict(self, elapsed: float) -> IdlePrediction:
        mean = self._mean
        if mean is None:
            return IdlePrediction(_ZERO, _ZERO)
        remaining = min(elapsed, mean)
        return IdlePrediction(remaining, self._confidence(elapsed, mean))

    def _confidence(self, elapsed: float, mean: float) -> float:
        """Trust the estimate more with history, less when extrapolating."""
        sample_conf = min(_FULL_CONFIDENCE, len(self._windows) / self._min_windows)
        denom = max(mean, elapsed)
        fit = mean / denom if denom > _ZERO else _FULL_CONFIDENCE
        return _clamp_unit(sample_conf * fit)
