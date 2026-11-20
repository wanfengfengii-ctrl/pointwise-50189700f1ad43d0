import sqlite3
from collections.abc import Sequence

from station.rounds.policy import freeze_limit, freeze_mode
from station.rounds.repository import RoundRecord, RoundRepository, transaction
from station.rounds.roster import require_name, validate_roster


def open(connection: sqlite3.Connection, round_id: str, roster: Sequence[str],
         limit: int, *, mode: str = 'single') -> RoundRecord:
    round_id = require_name(round_id, 'round_id')
    requested_roster = validate_roster(roster)
    policy = freeze_limit(limit)
    mode = freeze_mode(mode)
    repository = RoundRepository(connection)
    with transaction(connection, write=True):
        existing = repository.find_round(round_id)
        if existing is None:
            return repository.insert_round(round_id, requested_roster, policy, mode)
        if (existing.roster != requested_roster or existing.policy != policy
                or existing.mode != mode):
            raise ValueError(f'round {round_id!r} already has a different frozen plan')
        return existing
