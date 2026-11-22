import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FMIN = 50.0
FMAX = 1500.0
MIN_CONFIDENCE = 0.8
SILENCE_RMS = 0.003


def hz_to_note(hz, a4=440.0):
    midi = 69 + 12 * np.log2(hz / a4)
    nearest = int(round(midi))
    cents = float((midi - nearest) * 100)
    return NOTE_NAMES[nearest % 12], nearest // 12 - 1, cents


def cents_bar(cents, width=31):
    middle = width // 2
    spot = middle + int(round(max(-50.0, min(50.0, cents)) / 50.0 * middle))
    cells = ["-"] * width
    cells[middle] = "|"
    cells[spot] = "█"
    return "".join(cells)


def yin(frame, rate, threshold=0.15):
    x = np.asarray(frame, dtype=np.float64)
    x = x - x.mean()
    n = len(x)
    if np.sqrt(np.mean(x * x)) < SILENCE_RMS:
        return None
    tau_max = min(int(rate / FMIN), n // 2)
    tau_min = max(2, int(rate / FMAX))
    window = n - tau_max
    size = 1
    while size < n + window:
        size = size * 2
    cross = np.fft.irfft(np.conj(np.fft.rfft(x[:window], size)) * np.fft.rfft(x, size), size)[:tau_max + 1]
    energy = np.concatenate([[0.0], np.cumsum(x * x)])
    shifted = energy[window:window + tau_max + 1] - energy[:tau_max + 1]
    diff = energy[window] + shifted - 2 * cross
    diff[0] = 0.0
    running = np.cumsum(diff[1:])
    normalized = np.ones(tau_max + 1)
    normalized[1:] = diff[1:] * np.arange(1, tau_max + 1) / np.maximum(running, 1e-12)

    tau = None
    for t in range(tau_min, tau_max):
        if normalized[t] < threshold:
            while t + 1 < tau_max and normalized[t + 1] < normalized[t]:
                t = t + 1
            tau = t
            break
    if tau is None:
        tau = tau_min + int(np.argmin(normalized[tau_min:tau_max]))
    if tau <= 1 or tau >= tau_max:
        return None
    left, middle, right = normalized[tau - 1], normalized[tau], normalized[tau + 1]
    bend = left - 2 * middle + right
    offset = 0.0
    if bend != 0:
        offset = 0.5 * (left - right) / bend
    return rate / (tau + offset), float(1.0 - normalized[tau])


class PitchTracker:

    def __init__(self, rate, window=4096, hop=1024, use_aubio=True):
        self.rate = rate
        self.window = window
        self.hop = hop
        self.buffer = np.zeros(0, dtype=np.float32)
        self.detector = None
        self.backend = "numpy"
        self.warmup = 0
        if use_aubio:
            try:
                import aubio
                self.detector = aubio.pitch("yin", window, hop, rate)
                self.detector.set_unit("Hz")
                self.detector.set_tolerance(0.15)
                self.detector.set_silence(-50)
                self.backend = "aubio"
                self.warmup = window // hop - 1
            except ImportError:
                self.detector = None

    def accept(self, hz, confidence):
        if hz < FMIN or hz > FMAX or confidence < MIN_CONFIDENCE:
            return None
        return hz, confidence

    def feed(self, samples):
        self.buffer = np.concatenate([self.buffer, np.asarray(samples, dtype=np.float32)])
        results = []
        if self.detector is not None:
            while len(self.buffer) >= self.hop:
                chunk = np.ascontiguousarray(self.buffer[:self.hop])
                self.buffer = self.buffer[self.hop:]
                hz = float(self.detector(chunk)[0])
                confidence = float(self.detector.get_confidence())
                if self.warmup > 0:
                    self.warmup = self.warmup - 1
                    results.append(None)
                else:
                    results.append(self.accept(hz, confidence))
            return results
        while len(self.buffer) >= self.window:
            found = yin(self.buffer[:self.window], self.rate)
            self.buffer = self.buffer[self.hop:]
            if found is None:
                results.append(None)
            else:
                results.append(self.accept(found[0], found[1]))
        return results