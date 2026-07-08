from __future__ import annotations

from radar.models import Metrics


def virality_score(metrics: Metrics) -> float:
    """Phase 1 placeholder formula -- tunable, expected to be revised once
    Phase 2's velocity/clustering logic sees real data.
    """
    return float(metrics.score + metrics.comments * 2 + metrics.likes + metrics.shares)
