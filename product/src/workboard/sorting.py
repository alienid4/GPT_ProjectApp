"""Deterministic natural ordering shared with the browser, independent of OS locale."""
import re
from decimal import Decimal
from functools import cmp_to_key


def compare_values(left, right):
    if type(left) in (int, float, Decimal) and type(right) in (int, float, Decimal):
        return (left > right) - (left < right)
    def tokens(value):
        value = str(value).lower()
        return [(1, int(part)) if re.fullmatch('[0-9]+', part) else (0, part)
                for part in re.split('([0-9]+)', value)]
    a, b = tokens(left), tokens(right)
    return (a > b) - (a < b)


def ordered(records, key, descending=False, fallback='title'):
    def compare(a, b):
        left, right = a.get(key, a.get(fallback)), b.get(key, b.get(fallback))
        missing_left, missing_right = left in (None, ''), right in (None, '')
        if missing_left != missing_right:
            return 1 if missing_left else -1
        result = 0 if missing_left else compare_values(left, right)
        if not result:
            result = compare_values(a.get('code') or a.get('id') or a.get('person_id') or '',
                                    b.get('code') or b.get('id') or b.get('person_id') or '')
        return -result if descending else result
    return sorted(records, key=cmp_to_key(compare))
