import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from station.device.dual import DualDeviceRepository
from station.device.recording import DeviceRecording
from station.device.terminal import RawReading
from station.intake.grading import Grade, grade_terminal
from station.intake.receipt import ReceiptStore
from station.intake.repository import IntakeRepository, PieceConclusion
from station.intake.result import AcceptanceResult
from station.rounds.repository import RoundRepository, transaction

HEADS = ('left', 'right')
HEAD_PRIORITY = {'left': 0, 'right': 1}


@dataclass(frozen=True)
class _HeadView:
    observed_through: int | None
    ended: bool
    reads: frozenset[int]


@dataclass(frozen=True)
class _Winner:
    head: str
    tick: int
    raw_code: int


class DualIntakeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_delivered_raws (
                trigger_id TEXT NOT NULL,
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                tick TEXT NOT NULL,
                raw_code TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, tick)
            )
        ''')
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_delivered_evidence (
                trigger_id TEXT NOT NULL,
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                observed_through TEXT NOT NULL,
                ended INTEGER NOT NULL CHECK (ended IN (0, 1)),
                reading_ticks TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, observed_through, ended, reading_ticks)
            )
        ''')

    def find_raw(self, trigger_id: str, head: str, tick: int) -> int | None:
        row = self.connection.execute('''
            SELECT raw_code FROM dual_delivered_raws
            WHERE trigger_id = ? AND head = ? AND tick = ?
        ''', (trigger_id, head, hex(tick))).fetchone()
        return None if row is None else int(row[0], 16)

    def add_raw(self, trigger_id: str, head: str, tick: int, raw_code: int) -> None:
        self.connection.execute('''
            INSERT OR IGNORE INTO dual_delivered_raws (trigger_id, head, tick, raw_code)
            VALUES (?, ?, ?, ?)
        ''', (trigger_id, head, hex(tick), hex(raw_code)))

    def add_evidence(self, trigger_id: str, head: str, observed_through: int,
                     ended: bool, reading_ticks: tuple[int, ...]) -> None:
        self.connection.execute('''
            INSERT OR IGNORE INTO dual_delivered_evidence
                (trigger_id, head, observed_through, ended, reading_ticks)
            VALUES (?, ?, ?, ?, ?)
        ''', (trigger_id, head, hex(observed_through), int(ended),
              json.dumps([hex(tick) for tick in sorted(reading_ticks)])))

    def head_view(self, trigger_id: str, head: str) -> _HeadView:
        raws = self.connection.execute('''
            SELECT tick FROM dual_delivered_raws WHERE trigger_id = ? AND head = ?
        ''', (trigger_id, head))
        reads = {int(row[0], 16) for row in raws}
        observed = None
        ended = False
        rows = self.connection.execute('''
            SELECT observed_through, ended, reading_ticks
            FROM dual_delivered_evidence WHERE trigger_id = ? AND head = ?
        ''', (trigger_id, head))
        for observed_hex, ended_value, ticks_json in rows:
            current = int(observed_hex, 16)
            observed = current if observed is None else max(observed, current)
            ended = ended or bool(ended_value)
            reads.update(int(tick, 16) for tick in json.loads(ticks_json))
        return _HeadView(observed, ended, frozenset(reads))


def _published_evidence(device: DualDeviceRepository, trigger_id: str) -> list[dict]:
    messages = []
    for head in HEADS:
        messages.extend(device.evidence_messages(trigger_id, head))
    return messages


def _validate_evidence(message: dict, device: DualDeviceRepository, trigger_id: str) -> tuple[str, int, bool, tuple[int, ...]]:
    head = message.get('head')
    observed = message.get('observed_through')
    ended = message.get('ended')
    ticks = message.get('reading_ticks')
    if (head not in HEADS or type(observed) is not int or type(ended) is not bool
            or not isinstance(ticks, list) or any(type(tick) is not int for tick in ticks)):
        raise ValueError('evidence message has an inconsistent shape')
    if message not in _published_evidence(device, trigger_id):
        raise ValueError('evidence message does not match the fixed device source')
    return head, observed, ended, tuple(ticks)


def _deliver(connection: sqlite3.Connection, order_id: str,
             message: object) -> tuple[str, str]:
    """Validate one delivered message and record it as an accepted fact."""
    rounds = RoundRepository(connection)
    order = rounds.find_order(order_id)
    if order is None:
        raise KeyError(f'unknown read order {order_id!r}')
    round_record = rounds.require_round(order.round_id)
    if round_record.mode != 'dual':
        raise ValueError(f'round {order.round_id!r} does not collect two heads')
    exposure = DeviceRecording(connection).find_exposure(order_id)
    if exposure is None:
        raise ValueError(f'read order {order_id!r} has not triggered')
    if not isinstance(message, dict):
        raise ValueError('a delivered message must be a message dictionary')
    if message.get('trigger_id') != exposure.trigger_id:
        raise ValueError('message has a conflicting original binding')
    device = DualDeviceRepository(connection)
    delivered = DualIntakeRepository(connection)
    kind = message.get('kind')
    if kind == 'raw':
        head = message.get('head')
        tick = message.get('tick')
        raw_code = message.get('raw_code')
        if head not in HEADS or type(tick) is not int or type(raw_code) is not int:
            raise ValueError('raw message has an inconsistent shape')
        published = device.published_read(exposure.trigger_id, head, tick)
        if published != raw_code:
            raise ValueError('raw message does not match the fixed device source')
        delivered.add_raw(exposure.trigger_id, head, tick, raw_code)
        return 'raw', exposure.trigger_id
    if kind == 'evidence':
        head, observed, ended, ticks = _validate_evidence(message, device, exposure.trigger_id)
        delivered.add_evidence(exposure.trigger_id, head, observed, ended, ticks)
        return 'evidence', exposure.trigger_id
    raise ValueError('message kind must be raw or evidence')


def _covered_through(view: _HeadView, tick: int) -> bool:
    """True when every tick at or below ``tick`` on this head is known to be read-free/known.

    ``observed_through`` is the furthest tick the head has observed, so every reading at
    or below it is listed in ``reading_ticks``; an ended head has no observations above
    the tick it ended on, so its observations are complete regardless of how far below
    the candidate it stopped.
    """
    if view.observed_through is None:
        return False
    return view.ended or view.observed_through >= tick


def _no_read_below(view: _HeadView, tick: int) -> bool:
    """The head has no valid original earlier than ``tick``."""
    return _covered_through(view, tick - 1) and not any(read < tick for read in view.reads)


def _no_read_at_or_below(view: _HeadView, tick: int) -> bool:
    """The head has no valid original at or earlier than ``tick``."""
    return _covered_through(view, tick) and not any(read <= tick for read in view.reads)


def _decide(connection: sqlite3.Connection, trigger_id: str) -> _Winner | str | None:
    delivered = DualIntakeRepository(connection)
    views = {head: delivered.head_view(trigger_id, head) for head in HEADS}
    # Evidence can announce an original whose raw has not landed yet; such a read still
    # competes for first-read so a later tick cannot be signed ahead of it.
    announced = {(head, tick) for head, view in views.items() for tick in view.reads}
    with_raw = {(head, tick) for head, view in views.items()
                for tick in view.reads
                if delivered.find_raw(trigger_id, head, tick) is not None}
    if not announced:
        if views['left'].ended and views['right'].ended:
            return 'missing'
        return None
    head, tick = min(announced, key=lambda pair: (pair[1], HEAD_PRIORITY[pair[0]]))
    if (head, tick) not in with_raw:
        return None
    raw_code = delivered.find_raw(trigger_id, head, tick)
    own = views[head]
    if not _covered_through(own, tick - 1):
        return None
    other = views['right'] if head == 'left' else views['left']
    if head == 'left':
        # A left candidate only needs the right head cleared strictly earlier.
        if not _no_read_below(other, tick):
            return None
    elif not _no_read_at_or_below(other, tick):
        return None
    return _Winner(head, tick, raw_code)


def _replay(existing: PieceConclusion, receipts: ReceiptStore) -> AcceptanceResult:
    receipt = receipts.find_for_conclusion(existing.conclusion_id)
    if existing.grade == Grade.MISSING:
        if receipt is not None:
            raise RuntimeError('accepted missing conclusion has an inconsistent receipt')
    elif receipt is None:
        raise RuntimeError('accepted reading has an incomplete receipt')
    return AcceptanceResult(existing, receipt)


def accept_dual(connection: sqlite3.Connection, order_id: str, message: dict, *,
                before_commit: Callable[[], None] | None = None) -> AcceptanceResult | None:
    intake = IntakeRepository(connection)
    receipts = ReceiptStore(connection)
    with transaction(connection, write=True):
        kind, trigger_id = _deliver(connection, order_id, message)
        rounds = RoundRepository(connection)
        order = rounds.find_order(order_id)
        existing = intake.find_for_piece(order.round_id, order.piece)
        if existing is not None:
            return _replay(existing, receipts)
        outcome = _decide(connection, trigger_id)
        if outcome is None:
            return None
        if outcome == 'missing':
            _record_missing_original(connection, trigger_id)
            conclusion = intake.insert_bound_conclusion(
                order.round_id, order.piece, order.order_id, trigger_id,
                f'dual-missing:{trigger_id}', Grade.MISSING, None)
            if before_commit is not None:
                before_commit()
            return AcceptanceResult(conclusion, None)
        policy = rounds.require_round(order.round_id).policy
        source_id = f'dual:{trigger_id}:{outcome.head}:{hex(outcome.tick)}'
        grade = grade_terminal(RawReading(source_id, order.round_id, order.piece,
                                          order.order_id, trigger_id,
                                          outcome.tick, outcome.raw_code), policy)
        _record_reading_original(connection, trigger_id, source_id, outcome)
        conclusion = intake.insert_bound_conclusion(
            order.round_id, order.piece, order.order_id, trigger_id, source_id, grade,
            outcome.head)
        receipt = receipts.issue(conclusion)
        if before_commit is not None:
            before_commit()
    return AcceptanceResult(conclusion, receipt)


def _record_reading_original(connection: sqlite3.Connection, trigger_id: str,
                             source_id: str, winner: _Winner) -> None:
    connection.execute('''
        INSERT INTO device_terminals (source_id, trigger_id, kind, physical_tick, raw_code)
        VALUES (?, ?, 'reading', ?, ?)
    ''', (source_id, trigger_id, hex(winner.tick), hex(winner.raw_code)))


def _record_missing_original(connection: sqlite3.Connection, trigger_id: str) -> None:
    connection.execute('''
        INSERT INTO device_terminals (source_id, trigger_id, kind, physical_tick, raw_code)
        VALUES (?, ?, 'missing', NULL, NULL)
    ''', (f'dual-missing:{trigger_id}', trigger_id))
