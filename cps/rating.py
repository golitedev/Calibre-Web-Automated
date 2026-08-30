# -*- coding: utf-8 -*-
"""Helpers for the user-facing five-star rating scale.

Calibre stores ratings as integers from 0 to 10. CWA presents that value as
zero through five stars in half-star increments; this module keeps that
conversion in one small, first-party place.
"""

import math


def normalize_rating(value):
    """Return a valid user-facing rating rounded to the nearest half star."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric) or numeric < 0 or numeric > 5:
        return None
    return math.floor(numeric * 2 + 0.5) / 2


def rating_to_calibre(value):
    """Convert a normalized five-star value to Calibre's 0-10 integer."""
    normalized = normalize_rating(value)
    if normalized is None:
        return None
    return int(normalized * 2)
