"""Small OOP library used to verify source discovery and real method reuse."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    """Quantity is signed; unit_price is currency per unit."""
    quantity: float
    unit_price: float

    def market_value(self) -> float:
        """Return signed market value in currency units."""
        return self.quantity * self.unit_price


class PortfolioAnalytics:
    def __init__(self, positions: list[Position]):
        self.positions = positions

    def summarize(self) -> dict[str, float]:
        """Return net_value and gross_value in currency units."""
        values = [position.market_value() for position in self.positions]
        return {"net_value": sum(values), "gross_value": sum(abs(value) for value in values)}
