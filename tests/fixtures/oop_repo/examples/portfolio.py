"""Construct objects, then call the existing portfolio method.

For this educational fixture, object construction opens no external resources.
"""
from sample_risk import PortfolioAnalytics, Position

portfolio = PortfolioAnalytics([Position(quantity=2, unit_price=10)])
metrics = portfolio.summarize()  # {"net_value": 20, "gross_value": 20}
