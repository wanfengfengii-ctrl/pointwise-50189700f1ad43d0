import json
import sqlite3

from station.device.fixture import OpticalObservation
from station.device.recording import DeviceRecording, ExposureRecord
from station.rounds.repository import RoundRepository, transaction

HEADS = ('left', 'right')


class DualDeviceRepository:
    """Durable per-head device progress and the messages published for observations."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_head_progress (
                trigger_id TEXT NOT NULL REFERENCES exposures(trigger_id),
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                observed_through TEXT NOT NULL,
                ended INTEGER NOT NULL CHECK (ended IN (0, 1)),
                PRIMARY KEY (trigger_id, head)
            )
        ''')
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_reads (
                trigger_id TEXT NOT NULL,
                head TEXT NOT NULL,
                tick TEXT NOT NULL,
                raw_code TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, tick)
            )
        ''')
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_observations (
                trigger_id TEXT NOT NULL,
                head TEXT NOT NULL,
                tick TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reading', 'empty', 'end')),
                raw_code TEXT,
                raw_payload TEXT,
                evidence_payload TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, tick, kind)
            )
        ''')

    def find_observation(self, trigger_id: str, head: str, tick: int, kind: str):
        return self.connection.execute('''
            SELECT raw_code, raw_payload, evidence_payload FROM dual_observations
            WHERE trigger_id = ? AND head = ? AND tick = ? AND kind = ?
        ''', (trigger_id, head, hex(tick), kind)).fetchone()

    def last_observation(self, trigger_id: str, head: str):
        row = self.connection.execute('''
            SELECT tick, kind FROM dual_observations
            WHERE trigger_id = ? AND head = ? ORDER BY rowid DESC LIMIT 1
        ''', (trigger_id, head)).fetchone()
        return None if row is None else (int(row[0], 16), row[1])

    def progress(self, trigger_id: str, head: str) -> tuple[int | None, bool]:
        row = self.connection.execute('''
            SELECT observed_through, ended FROM dual_head_progress
            WHERE trigger_id = ? AND head = ?
        ''', (trigger_id, head)).fetchone()
        if row is None:
            return None, False
        return int(row[0], 16), bool(row[1])

    def reads(self, trigger_id: str, head: str) -> tuple[int, ...]:
        rows = self.connection.execute('''
            SELECT tick FROM dual_reads WHERE trigger_id = ? AND head = ?
        ''', (trigger_id, head))
        return tuple(sorted(int(row[0], 16) for row in rows))

    def published_read(self, trigger_id: str, head: str, tick: int) -> int | None:
        row = self.connection.execute('''
            SELECT raw_code FROM dual_reads
            WHERE trigger_id = ? AND head = ? AND tick = ?
        ''', (trigger_id, head, hex(tick))).fetchone()
        return None if row is None else int(row[0], 16)

    def evidence_messages(self, trigger_id: str, head: str) -> tuple[dict, ...]:
        rows = self.connection.execute('''
            SELECT evidence_payload FROM dual_observations
            WHERE trigger_id = ? AND head = ?
        ''', (trigger_id, head))
        return tuple(json.loads(row[0]) for row in rows)

    def _record(self, trigger_id: str, observation: OpticalObservation) -> None:
        head = observation.head
        tick_hex = hex(observation.tick)
        if observation.kind == 'reading':
            code_hex = hex(observation.raw_code)
            stored = self.connection.execute('''
                SELECT raw_code FROM dual_reads WHERE trigger_id = ? AND head = ? AND tick = ?
            ''', (trigger_id, head, tick_hex)).fetchone()
            if stored is not None and int(stored[0], 16) != observation.raw_code:
                raise ValueError('one reading or empty observation per head and tick')
            self.connection.execute('''
                INSERT OR IGNORE INTO dual_reads (trigger_id, head, tick, raw_code)
                VALUES (?, ?, ?, ?)
            ''', (trigger_id, head, tick_hex, code_hex))
        previous, was_ended = self.progress(trigger_id, head)
        observed = observation.tick if previous is None else max(previous, observation.tick)
        ended = was_ended or observation.kind == 'end'
        self.connection.execute('''
            INSERT INTO dual_head_progress (trigger_id, head, observed_through, ended)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (trigger_id, head) DO UPDATE SET
                observed_through = excluded.observed_through,
                ended = excluded.ended
        ''', (trigger_id, head, hex(observed), int(ended)))

    def _store_messages(self, trigger_id: str, observation: OpticalObservation,
                        raw_payload: dict | None, evidence_payload: dict) -> None:
        self.connection.execute('''
            INSERT INTO dual_observations
                (trigger_id, head, tick, kind, raw_code, raw_payload, evidence_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (trigger_id, observation.head, hex(observation.tick), observation.kind,
              None if observation.raw_code is None else hex(observation.raw_code),
              None if raw_payload is None else json.dumps(raw_payload, sort_keys=True),
              json.dumps(evidence_payload, sort_keys=True)))


def _require_dual_exposure(connection: sqlite3.Connection,
                           exposure: ExposureRecord) -> None:
    recordings = DeviceRecording(connection)
    if recordings.find_exposure(exposure.order_id) != exposure:
        raise ValueError('observation targets an unknown or conflicting exposure')
    round_record = RoundRepository(connection).require_round(exposure.round_id)
    if round_record.mode != 'dual':
        raise ValueError(f'round {exposure.round_id!r} is not collecting two heads')


def _ordering_conflict(last, observation: OpticalObservation) -> None:
    if last is None:
        return
    last_tick, last_kind = last
    if last_kind == 'end':
        raise ValueError('the head has already ended')
    if observation.tick < last_tick:
        raise ValueError('observations must follow the physical order for each head')
    if observation.tick == last_tick and observation.kind != 'end':
        raise ValueError('one reading or empty observation per head and tick')


def observe_dual(connection: sqlite3.Connection, exposure: ExposureRecord,
                 observation: OpticalObservation) -> tuple[dict, ...]:
    """Consume one dual-head observation and return the messages it publishes."""
    if not isinstance(observation, OpticalObservation):
        raise ValueError('observe_dual requires an OpticalObservation')
    with transaction(connection, write=True):
        _require_dual_exposure(connection, exposure)
        repository = DualDeviceRepository(connection)
        trigger_id = exposure.trigger_id
        identical = repository.find_observation(trigger_id, observation.head,
                                                observation.tick, observation.kind)
        if identical is not None:
            stored_code, raw_payload, evidence_payload = identical
            if observation.kind == 'reading':
                if int(stored_code, 16) != observation.raw_code:
                    raise ValueError('observation conflicts with the published original')
            messages = []
            if raw_payload is not None:
                messages.append(json.loads(raw_payload))
            messages.append(json.loads(evidence_payload))
            return tuple(messages)
        _ordering_conflict(repository.last_observation(trigger_id, observation.head),
                           observation)
        repository._record(trigger_id, observation)
        raw_payload = None
        messages = []
        if observation.kind == 'reading':
            raw_payload = {'trigger_id': trigger_id, 'kind': 'raw',
                           'head': observation.head, 'tick': observation.tick,
                           'raw_code': observation.raw_code}
            messages.append(raw_payload)
        observed, ended = repository.progress(trigger_id, observation.head)
        evidence_payload = {'trigger_id': trigger_id, 'kind': 'evidence',
                            'head': observation.head, 'observed_through': observed,
                            'reading_ticks': list(repository.reads(trigger_id, observation.head)),
                            'ended': ended}
        messages.append(evidence_payload)
        repository._store_messages(trigger_id, observation, raw_payload, evidence_payload)
        return tuple(messages)
