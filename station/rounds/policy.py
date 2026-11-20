from dataclasses import dataclass

MODES = ('single', 'dual')


@dataclass(frozen=True)
class LimitPolicy:
    limit: int


def freeze_limit(limit: int) -> LimitPolicy:
    if type(limit) is not int:
        raise ValueError('limit must be an integer raw code')
    return LimitPolicy(limit)


def freeze_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f'round mode must be one of {MODES!r}')
    return mode
