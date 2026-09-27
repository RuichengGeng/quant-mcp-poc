"""Small CSV-backed provider and importable isolated function for the example app."""
from __future__ import annotations

import csv
from pathlib import Path


CSV_PATH = Path(__file__).resolve().with_name("sample_market_data.csv")


class CsvDataProvider:
    """Load a CSV once and keep the rows available for the server lifetime."""

    def __init__(self, csv_path: Path = CSV_PATH):
        self.csv_path = Path(csv_path)
        with self.csv_path.open(newline="", encoding="utf-8") as stream:
            self._quotes = {
                row["symbol"].strip().upper(): {
                    "symbol": row["symbol"].strip().upper(),
                    "price": float(row["price"]),
                    "currency": row["currency"].strip().upper(),
                }
                for row in csv.DictReader(stream)
            }

    def get_quote(self, symbol: str) -> dict:
        """Return a quote from the server's in-memory CSV snapshot.

        Args:
            symbol: Stock symbol, such as AAPL.
        """
        key = symbol.strip().upper()
        quote = self._quotes.get(key)
        if quote is None:
            raise ValueError(f"Unknown symbol: {key}")
        # Return a copy so a caller cannot mutate the provider's stored row.
        return dict(quote)

    def close(self) -> None:
        """Release the provider's in-memory snapshot during server shutdown."""
        self._quotes.clear()


def get_quote_from_csv(symbol: str) -> dict:
    """Read one quote directly from CSV; isolated mode repeats this per call.

    Args:
        symbol: Stock symbol, such as AAPL.
    """
    key = symbol.strip().upper()
    with CSV_PATH.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["symbol"].strip().upper() == key:
                return {
                    "symbol": key,
                    "price": float(row["price"]),
                    "currency": row["currency"].strip().upper(),
                }
    raise ValueError(f"Unknown symbol: {key}")
