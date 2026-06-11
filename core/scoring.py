"""Shared scoring helpers (adapted from source project)."""


def _safe_rr(target: float, entry_ref: float, sl: float) -> float | None:
    """Risk-reward ratio with divide-by-zero guard. Returns None when SL == entry."""
    denom = abs(entry_ref - sl)
    if denom < 0.01:
        return None
    return abs(target - entry_ref) / denom


def score_confluences(confluences: list[str], weights: dict[str, int] | None = None) -> int:
    """Weighted confluence score capped at 10.

    Exact key match first, then prefix match, then 1 point fallback.
    """
    if weights is None:
        weights = {}
    total = 0
    for label in confluences:
        if label in weights:
            total += weights[label]
        else:
            prefix_match = next((w for w in weights if label.startswith(w)), None)
            total += weights[prefix_match] if prefix_match else 1
    return min(10, total)
