"""One percentile definition for all reports: empirical nearest rank."""
import math


def p95(values):
    ordered=sorted(values)
    return ordered[max(0,math.ceil(.95*len(ordered))-1)] if ordered else None
