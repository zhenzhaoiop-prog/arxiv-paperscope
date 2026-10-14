from arxiv_tracker.costs import Budget
from .data import cost_totals


class SharedBudget(Budget):
    """Count local calls plus the latest immutable cloud ledger; never import cost rows."""
    def __init__(self, store, cfg, clock, cloud_sources=()):
        super().__init__(store, cfg, clock)
        self.cloud_sources = cloud_sources

    def totals(self, month=None, date=None):
        local = super().totals(month, date)
        remote = cost_totals(self.cloud_sources, local["month"], date)
        for key in ("estimated_cny", "uncertain_reserved_cny", "committed_cny"):
            local[key] = round(local[key] + remote[key], 12)
        for stage, values in remote["stages"].items():
            group = local["stages"].setdefault(stage, {"estimated_cny": 0, "uncertain_reserved_cny": 0, "calls": 0})
            for key, value in values.items():
                group[key] += value
        local["warning"] = local["committed_cny"] >= self.cfg["budget"]["monthly_cny"] * self.cfg["budget"]["warning_fraction"]
        return local
