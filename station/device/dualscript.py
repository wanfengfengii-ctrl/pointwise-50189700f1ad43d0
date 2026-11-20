from dataclasses import dataclass

from station.device.optical import observe_dual
from station.device.fixture import FailOnceBeforeCommit, InjectedCommitFailure, OpticalObservation
from station.device.trigger import trigger
from station.intake.accept import accept_dual
from station.intake.repository import IntakeRepository
from station.lookup.sample import get_sample
from station.rounds.authorization import authorize_read
from station.rounds.opening import open


@dataclass(frozen=True)
class StepOutcome:
    step: int
    failed_once: bool
    signed: bool
    sample: dict | None


def _group_observations(connection, exposure, rows):
    groups = {}
    for index, row in enumerate(rows):
        produced = observe_dual(connection, exposure, OpticalObservation(**row))
        for kind in ('raw', 'evidence'):
            groups[(index, kind)] = tuple(m for m in produced if m['kind'] == kind)
    return groups


def run_case(connection, case: dict, limit: int, *, round_id: str = 'dual-case',
             piece: str = 'P') -> tuple[StepOutcome, ...]:
    """Feed one ``examples/first_read.json`` case through a fresh dual round."""
    open(connection, round_id, (piece,), limit, mode='dual')
    order_id = f'{round_id}/{piece}'
    authorize_read(connection, round_id, piece, order_id)
    exposure = trigger(connection, round_id, piece, order_id)
    groups = _group_observations(connection, exposure, case['observations'])
    outcomes = []
    for number, step in enumerate(case['steps'], 1):
        hook = FailOnceBeforeCommit() if step.get('fail_once') else None
        failed = False
        result = None
        for index, channel in step['deliver']:
            for message in groups[(index, channel)]:
                try:
                    current = accept_dual(connection, order_id, message, before_commit=hook)
                except InjectedCommitFailure:
                    if hook is None:
                        raise
                    failed = True
                    if IntakeRepository(connection).find_for_piece(round_id, piece) is not None:
                        raise RuntimeError('failed publish left a conclusion')
                    current = accept_dual(connection, order_id, message, before_commit=hook)
                if current is not None:
                    result = current
        conclusion = IntakeRepository(connection).find_for_piece(round_id, piece)
        sample = None
        if conclusion is not None and result is not None and result.receipt is not None:
            fixed = get_sample(connection, result.receipt.number)
            sample = {'receipt': result.receipt.number, 'head': fixed.head,
                      'tick': fixed.tick, 'raw_code': fixed.raw_code,
                      'grade': fixed.grade.value}
        outcomes.append(StepOutcome(number, failed, conclusion is not None, sample))
    return tuple(outcomes)
