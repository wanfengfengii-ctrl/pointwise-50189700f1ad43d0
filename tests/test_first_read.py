import json
from pathlib import Path

from station.closing.seal import close
from station.closing.tally import CategoryCounts
from station.device.dual import DualDeviceRepository
from station.device.fixture import OpticalObservation
from station.device.optical import apply_observation, observe_dual
from station.device.recording import DeviceRecording
from station.device.trigger import trigger
from station.intake.accept import accept, accept_dual
from station.intake.grading import Grade
from station.intake.repository import IntakeRepository
from station.lookup.round import get_round
from station.lookup.sample import get_sample
from station.rounds.authorization import authorize_read
from station.rounds.opening import open
from station.rounds.workstation import initialize_station
from tests.support import StationCase


def _deliver(case, messages, step):
    for index, channel in step['deliver']:
        yield from messages[(index, channel)]


class DualFirstReadTests(StationCase):
    def _dual(self, mode='dual', round_id='day', pieces=('A',)):
        self.open_round(pieces, round_id=round_id, mode=mode)
        return self.start_dual(pieces[0], round_id)

    def _messages(self, exposure, *observations):
        from station.device.fixture import OpticalObservation
        groups = {}
        for index, observation in enumerate(observations):
            produced = self.observe_dual(exposure, **observation)
            for kind in ('raw', 'evidence'):
                groups[(index, kind)] = tuple(m for m in produced if m['kind'] == kind)
        return groups

    def _accept(self, order_id, message):
        return accept_dual(self.connection, order_id, message)

    def test_right_early_does_not_steal_left_first_read(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'right', 'tick': 11, 'kind': 'reading', 'raw_code': 61},
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
        )
        self.assertIsNone(self._accept('day/A', messages[(0, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'evidence')][0]))
        signed = self._accept('day/A', messages[(1, 'raw')][0])
        sample = get_sample(self.connection, signed.receipt.number)
        self.assertEqual((sample.head, sample.tick, sample.raw_code, sample.grade),
                         ('left', 10, 48, Grade.PASS))

    def test_same_tick_left_wins_and_right_waits(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'right', 'tick': 10, 'kind': 'reading', 'raw_code': 61},
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
        )
        self.assertIsNone(self._accept('day/A', messages[(0, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'evidence')][0]))
        signed = self._accept('day/A', messages[(1, 'raw')][0])
        self.assertEqual(signed.conclusion.head, 'left')
        self.assertEqual(get_sample(self.connection, signed.receipt.number).raw_code, 48)

    def test_right_wins_when_left_same_tick_empty(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'right', 'tick': 10, 'kind': 'reading', 'raw_code': 61},
            {'head': 'left', 'tick': 10, 'kind': 'empty'},
        )
        self.assertIsNone(self._accept('day/A', messages[(0, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        signed = self._accept('day/A', messages[(1, 'evidence')][0])
        self.assertEqual(signed.conclusion.grade, Grade.OVER_LIMIT)
        self.assertEqual(signed.conclusion.head, 'right')

    def test_right_earlier_tick_blocks_left(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 9, 'kind': 'empty'},
        )
        self.assertIsNone(self._accept('day/A', messages[(0, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        signed = self._accept('day/A', messages[(1, 'evidence')][0])
        self.assertEqual((signed.conclusion.head, signed.conclusion.grade), ('left', Grade.PASS))

    def test_inflight_other_original_is_not_an_empty_shot(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 9, 'kind': 'reading', 'raw_code': 61},
        )
        # Left raw/evidence arrives without the right original: cannot sign yet.
        self.assertIsNone(self._accept('day/A', messages[(0, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'evidence')][0]))
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('day', 'A'))
        signed = self._accept('day/A', messages[(1, 'raw')][0])
        # Right tick 9 is strictly earlier, so it beats the left tick 10.
        self.assertEqual(signed.conclusion.head, 'right')
        sample = get_sample(self.connection, signed.receipt.number)
        self.assertEqual((sample.head, sample.tick, sample.raw_code), ('right', 9, 61))

    def test_announced_earlier_read_blocks_later_signing(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 9, 'kind': 'reading', 'raw_code': 0},
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 11, 'kind': 'end'},
        )
        # Evidence announces the left tick 9 original but its raw is still in flight, so
        # neither the tick 10 original nor the tick 9 evidence may sign yet.
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'raw')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(2, 'evidence')][0]))
        # The earlier original lands; it must be the one signed, not the later tick.
        signed = self._accept('day/A', messages[(0, 'raw')][0])
        self.assertEqual((signed.conclusion.head, signed.conclusion.grade),
                         ('left', Grade.PASS))
        sample = get_sample(self.connection, signed.receipt.number)
        self.assertEqual((sample.tick, sample.raw_code), (9, 0))

    def test_close_pending_until_dual_evidence_signs(self):
        self.open_round(('A', 'B'), round_id='day', mode='dual')
        exposure = self.start_dual('A')
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 11, 'kind': 'end'},
        )
        self._accept('day/A', messages[(0, 'raw')][0])
        waiting = close(self.connection, 'day')
        self.assertEqual(waiting.pending, ('A', 'B'))
        self.assertIsNone(waiting.report)
        self._accept('day/A', messages[(1, 'evidence')][0])
        signed = self._accept('day/A', messages[(0, 'evidence')][0])
        self.assertIsNotNone(signed.receipt)
        missing_exposure = self.start_dual('B')
        end_left, end_right = (
            self.observe_dual(missing_exposure, 'left', 10, 'end'),
            self.observe_dual(missing_exposure, 'right', 10, 'end'),
        )
        self.assertIsNone(self._accept('day/B', end_left[-1]))
        conclusion = self._accept('day/B', end_right[-1]).conclusion
        self.assertEqual(conclusion.grade, Grade.MISSING)
        report = close(self.connection, 'day').report
        self.assertEqual(report.counts, CategoryCounts(1, 0, 1))

    def test_observation_replay_does_not_duplicate(self):
        exposure = self._dual()
        first = self.observe_dual(exposure, 'left', 10, 'reading', 48)
        again = self.observe_dual(exposure, 'left', 10, 'reading', 48)
        self.assertEqual(first, again)
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM dual_observations').fetchone(), (1,))
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM dual_reads').fetchone(), (1,))

    def test_both_ended_missing_without_receipt(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 9, 'kind': 'empty'},
            {'head': 'right', 'tick': 9, 'kind': 'empty'},
            {'head': 'left', 'tick': 10, 'kind': 'end'},
            {'head': 'right', 'tick': 10, 'kind': 'end'},
        )
        for index in (0, 1, 2):
            self.assertIsNone(self._accept('day/A', messages[(index, 'evidence')][0]))
        missing = self._accept('day/A', messages[(3, 'evidence')][0])
        self.assertEqual(missing.conclusion.grade, Grade.MISSING)
        self.assertIsNone(missing.receipt)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts').fetchone(),
                         (0,))

    def test_signing_after_one_head_ended_while_other_runs(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'right', 'tick': 9, 'kind': 'end'},
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
        )
        self.assertIsNone(self._accept('day/A', messages[(0, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', messages[(1, 'raw')][0]))
        # Right is provably done at tick 9 and the left raw is in hand: sign immediately
        # even though the left head has not ended.
        signed = self._accept('day/A', messages[(1, 'evidence')][0])
        self.assertIsNotNone(signed)
        self.assertEqual(signed.conclusion.head, 'left')

    def test_single_mode_cannot_be_bypassed(self):
        exposure = self._dual(mode='single')
        with self.assertRaises(ValueError):
            self.observe_dual(exposure, head='left', tick=10, kind='reading', raw_code=48)
        message = {'trigger_id': exposure.trigger_id, 'kind': 'raw', 'head': 'left',
                   'tick': 10, 'raw_code': 48}
        with self.assertRaises(ValueError):
            self._accept('day/A', message)

    def test_dual_mode_rejects_single_head_entry(self):
        exposure = self._dual()
        from station.device.fixture import OpticalObservation
        with self.assertRaises(ValueError):
            apply_observation(self.connection, exposure,
                              OpticalObservation('left', 10, 'reading', 48))

    def test_mode_is_frozen(self):
        self.open_round(('A',), round_id='day', mode='dual')
        with self.assertRaises(ValueError):
            self.open_round(('A',), round_id='day')
        with self.assertRaises(ValueError):
            open(self.connection, 'day', ('A',), 48, mode='other')

    def test_wrong_binding_and_conflict_rejected_without_new_facts(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 10, 'kind': 'end'},
        )
        raw = messages[(0, 'raw')][0]
        forged = dict(raw, raw_code=49)
        with self.assertRaises(ValueError):
            self._accept('day/A', forged)
        with self.assertRaises(ValueError):
            self._accept('day/A', dict(raw, trigger_id='foreign'))
        with self.assertRaises(KeyError):
            self._accept('day/B', raw)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM dual_delivered_raws')
                         .fetchone(), (0,))
        # Right end covers tick 10 with no earlier read; left's raw plus its coverage signs.
        self.assertIsNone(self._accept('day/A', messages[(1, 'evidence')][0]))
        self.assertIsNone(self._accept('day/A', raw))
        signed = self._accept('day/A', messages[(0, 'evidence')][0])
        self.assertEqual(signed.conclusion.grade, Grade.PASS)

    def test_commit_failure_leaves_no_result_and_resumes(self):
        from station.device.fixture import FailOnceBeforeCommit, InjectedCommitFailure
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 11, 'kind': 'reading', 'raw_code': 61},
        )
        self._accept('day/A', messages[(0, 'raw')][0])
        self._accept('day/A', messages[(0, 'evidence')][0])
        self._accept('day/A', messages[(1, 'raw')][0])
        fault = FailOnceBeforeCommit()
        with self.assertRaises(InjectedCommitFailure):
            accept_dual(self.connection, 'day/A', messages[(1, 'evidence')][0], before_commit=fault)
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('day', 'A'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts').fetchone(),
                         (0,))
        # Unfinished evidence is not lost: the same message resumes the decision.
        resumed = accept_dual(self.connection, 'day/A', messages[(1, 'evidence')][0])
        self.assertIsNotNone(resumed.receipt)
        self.assertEqual(resumed.conclusion.head, 'left')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts').fetchone(),
                         (1,))

    def test_late_nonwinner_cannot_rewrite(self):
        exposure = self._dual()
        messages = self._messages(
            exposure,
            {'head': 'left', 'tick': 10, 'kind': 'reading', 'raw_code': 48},
            {'head': 'right', 'tick': 11, 'kind': 'reading', 'raw_code': 61},
        )
        self._accept('day/A', messages[(0, 'raw')][0])
        self._accept('day/A', messages[(1, 'evidence')][0])
        signed = self._accept('day/A', messages[(0, 'evidence')][0])
        self.assertEqual(signed.conclusion.head, 'left')
        replay = self._accept('day/A', messages[(1, 'raw')][0])
        self.assertEqual(replay.conclusion.conclusion_id, signed.conclusion.conclusion_id)
        sample = get_sample(self.connection, signed.receipt.number)
        self.assertEqual((sample.head, sample.tick), ('left', 10))

    def test_close_and_offline_lookup_with_head(self):
        self.open_round(('A', 'B'), round_id='day', mode='dual')
        first = self.start_dual('A')
        second = self.start_dual('B')
        a_messages = self._messages(
            first,
            {'head': 'right', 'tick': 9, 'kind': 'reading', 'raw_code': 61},
            {'head': 'left', 'tick': 9, 'kind': 'end'},
        )
        b_messages = self._messages(
            second,
            {'head': 'left', 'tick': 12, 'kind': 'reading', 'raw_code': 0},
            {'head': 'right', 'tick': 11, 'kind': 'end'},
        )
        receipts = {}
        for order_id, messages, steps in (
            ('day/A', a_messages, ((0, 'raw'), (1, 'evidence'), (0, 'evidence'))),
            ('day/B', b_messages, ((0, 'raw'), (1, 'evidence'), (0, 'evidence'))),
        ):
            result = None
            for group in steps:
                result = self._accept(order_id, messages[group][0])
            receipts[order_id] = result.receipt
        report = close(self.connection, 'day').report
        self.assertEqual(report.counts, CategoryCounts(1, 1, 0))
        self.connection.close()
        offline = self.offline()
        sample = get_sample(offline, receipts['day/B'].number)
        self.assertEqual((sample.head, sample.tick, sample.raw_code, sample.grade),
                         ('left', 12, 0, Grade.PASS))
        self.assertEqual(get_round(offline, report.number).counts, CategoryCounts(1, 1, 0))


class LegacySchemaMigrationTests(StationCase):
    def test_legacy_single_original_table_accepts_two_heads(self):
        import sqlite3
        legacy_path = self.database.with_name('legacy.sqlite3')
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript('''
            CREATE TABLE device_terminals (
                source_id TEXT PRIMARY KEY NOT NULL,
                trigger_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('reading', 'missing')),
                physical_tick TEXT,
                raw_code TEXT
            );
        ''')
        legacy.commit()
        legacy.close()
        with sqlite3.connect(legacy_path) as connection:
            initialize_station(connection)
            open(connection, 'legacy', ('P',), 48, mode='dual')
            authorize_read(connection, 'legacy', 'P', 'legacy/P')
            exposure = trigger(connection, 'legacy', 'P', 'legacy/P')
            left = observe_dual(connection, exposure, OpticalObservation('left', 10, 'reading', 48))
            right = observe_dual(connection, exposure, OpticalObservation('right', 9, 'reading', 61))
            for message in left + right:
                accept_dual(connection, 'legacy/P', message)
            self.assertEqual(
                connection.execute('SELECT COUNT(*) FROM device_terminals').fetchone(), (1,))
            conclusion = IntakeRepository(connection).find_for_piece('legacy', 'P')
            self.assertEqual((conclusion.head, conclusion.grade), ('right', Grade.OVER_LIMIT))


class FixtureDrivenDualTests(StationCase):
    def test_examples_first_read_script(self):
        data = json.loads(Path('examples/first_read.json').read_text(encoding='utf-8'))
        for case in data['cases']:
            with self.subTest(case=case['id']):
                self._run_case(case, data['limit'])

    def _run_case(self, case, limit):
        from station.device.fixture import OpticalObservation
        self.connection.close()
        import sqlite3
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        from station.rounds.workstation import initialize_station
        initialize_station(self.connection)
        open(self.connection, 'case', ('P',), limit, mode='dual')
        order_id = 'case/P'
        authorize_read(self.connection, 'case', 'P', order_id)
        exposure = trigger(self.connection, 'case', 'P', order_id)
        groups = {}
        observations = [OpticalObservation(**row) for row in case['observations']]
        for index, observation in enumerate(observations):
            produced = __import__('station.device.dual', fromlist=['observe_dual']).observe_dual(
                self.connection, exposure, observation)
            for kind in ('raw', 'evidence'):
                groups[(index, kind)] = tuple(m for m in produced if m['kind'] == kind)
        for slot, step in enumerate(case['steps'], 1):
            result = None
            for index, channel in step['deliver']:
                for message in groups[(index, channel)]:
                    last = accept_dual(self.connection, order_id, message)
                    if last is not None:
                        result = last
            expected = step['expect']
            conclusion = IntakeRepository(self.connection).find_for_piece('case', 'P')
            if expected is None:
                self.assertIsNone(conclusion, (slot, result))
            elif expected.get('grade') == 'missing':
                self.assertEqual(conclusion.grade, Grade.MISSING)
                self.assertIsNone(result.receipt)
            else:
                sample = get_sample(self.connection, result.receipt.number)
                self.assertEqual((conclusion.head, sample.tick, sample.raw_code, sample.grade.value),
                                 (expected['head'], expected['tick'], expected['raw_code'],
                                  expected['grade']))
