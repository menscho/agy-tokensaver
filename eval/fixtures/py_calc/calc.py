"""Small arithmetic & stats library used for eval/ coding tasks."""

from __future__ import annotations


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def divide(a: float, b: float) -> float:
    # BUG (task-01): does not raise ValueError on b == 0 (raises ZeroDivisionError)
    return a / b


def clamp(val: float, lo: float, hi: float) -> float:
    # BUG (task-02): inverted bounds check
    if val < lo:
        return hi
    if val > hi:
        return lo
    return val


def mean(nums: list[float]) -> float:
    # BUG (task-03): crashes on empty list instead of returning 0.0
    return sum(nums) / len(nums)


def median(nums: list[float]) -> float:
    # BUG (task-04): wrong even-length median (uses upper element instead of average)
    if not nums:
        return 0.0
    s = sorted(nums)
    return float(s[len(s) // 2])


def normalize(nums: list[float]) -> list[float]:
    # BUG (task-05): divides by max instead of (max - min)
    if not nums:
        return []
    hi = max(nums)
    if hi == 0:
        return [0.0 for _ in nums]
    return [x / hi for x in nums]


def parse_csv_ints(text: str) -> list[int]:
    # BUG (task-06): fails on whitespace / trailing comma
    return [int(x) for x in text.split(",")]
