"""Match explicit stage ranges without inferring missing phenological stages."""
import re
from collections.abc import Iterable


def expand_stage_codes(codes: Iterable[str]) -> set[str]:
    result = set(codes)
    for code in tuple(result):
        match = re.fullmatch(r'([A-Za-z]+)(\d+)-\1(\d+)', code)
        if match:
            prefix, first, last = match.groups()
            if 0 <= int(last) - int(first) <= 100:
                result.update(f'{prefix}{number}' for number in range(int(first), int(last) + 1))
    return result


def stages_overlap(stage: str, allowed: Iterable[str]) -> bool:
    return bool(expand_stage_codes([stage]) & expand_stage_codes(allowed))


def new_validation_errors(before: list[str], after: list[str]) -> list[str]:
    """Allow independent edits while preserving, and reporting, existing defects."""
    from collections import Counter
    return list((Counter(after) - Counter(before)).elements())
