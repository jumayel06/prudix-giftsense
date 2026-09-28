"""Spending ledger for the offline evaluation: every real LLM call is charged
from its token counts, and a call that would take the run over the cap is
refused before it's made."""
from collections import defaultdict

from app.llm import calc_cost


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self.spent = 0.0
        self._steps: dict[str, float] = defaultdict(float)

    def check(self, projected_usd: float) -> None:
        if self.spent + projected_usd > self.cap_usd:
            raise BudgetExceeded(
                f"Stopping: ${self.spent:.2f} spent, next call ~${projected_usd:.3f} would exceed the ${self.cap_usd:.2f} cap"
            )

    def charge(self, step: str, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = calc_cost(model, input_tokens, output_tokens)
        self.spent += cost
        self._steps[step] += cost
        return cost

    def charge_usd(self, step: str, usd: float) -> None:
        self.spent += usd
        self._steps[step] += usd

    def by_step(self) -> dict[str, float]:
        return dict(self._steps)
