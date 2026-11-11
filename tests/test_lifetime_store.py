"""Creation-based accounting contracts using only disposable ledgers."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, Person, SnapshotApplyError, TZ, validate_key_periods


class LifetimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.now = datetime.now(TZ)
        self.today = self.now.date().isoformat()
        self.start = (self.now - timedelta(days=2)).date().isoformat()
        self.created = int((self.now - timedelta(days=15)).timestamp())
        self.store = self.make('lifetime', 'key_lifetime')

    def tearDown(self):
        self.temp.cleanup()

    def make(self, name, basis):
        store = DataStore(self.base / name)
        store.initialize(3, '300', self.start, fee_basis=basis)
        store.save_people([Person('Alpha', Decimal(10)), Person('Beta', Decimal(20))])
        store.bind_keys([{'name': 'Alpha', 'tracking_id': 'synthetic-alpha'},
                         {'name': 'Beta', 'tracking_id': 'synthetic-beta'}], 'synthetic-account')
        return store

    def periods(self, *, end=None, created=None):
        created = self.created if created is None else created
        return {'synthetic-alpha': {'created_at': created,
            'start_date': datetime.fromtimestamp(created, TZ).date().isoformat(),
            'end_date': end or self.today}}

    def apply(self, source='one', amount='1', *, periods=None, end=None, store=None):
        store = store or self.store
        return store.apply_snapshot_result({'synthetic-alpha': Decimal(amount)}, source,
            store.load().start_date, end or self.today, fee_basis='key_lifetime',
            key_periods=self.periods(end=end) if periods is None else periods)

    def db_state(self, store=None):
        store = store or self.store
        with store._connect() as db:
            return {table: db.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('meta', 'bindings', 'costs', 'snapshots', 'events')}

    def assert_rejected_without_write(self, action, code):
        before = self.db_state()
        with self.assertRaises(SnapshotApplyError) as caught:
            action()
        self.assertEqual(caught.exception.reason_code, code)
        self.assertEqual(caught.exception.persistence_state, 'not_written')
        self.assertEqual(self.db_state(), before)

    def test_new_schema_round_trip_and_legacy_missing_basis_remain_period(self):
        self.assertEqual(self.store.load().fee_basis, 'key_lifetime')
        self.assertEqual(json.loads(self.store.config_path.read_text(encoding='utf-8'))['schema'], 3)
        old = self.make('legacy', 'period')
        raw = json.loads(old.config_path.read_text(encoding='utf-8'))
        self.assertEqual(raw['schema'], 2)
        raw.pop('fee_basis')
        old.config_path.write_text(json.dumps(raw), encoding='utf-8')
        with old._connect() as db:
            db.execute("DELETE FROM meta WHERE key='fee_basis'")
        self.assertEqual(old.load().fee_basis, 'period')
        old.apply_snapshot({'synthetic-alpha': Decimal(1)}, 'legacy', self.start, self.today)
        self.assertEqual(old.read_evaluation()[0]['spent'], Decimal(1))

    def test_config_mode_database_and_schema_must_match(self):
        for change in ({'fee_basis': 'period'}, {'schema': 2}, {'fee_basis': 'unknown'}):
            raw = json.loads(self.store.config_path.read_text(encoding='utf-8'))
            original = dict(raw)
            raw.update(change)
            self.store.config_path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(GuardError):
                self.store.load()
            self.store.config_path.write_text(json.dumps(original), encoding='utf-8')
        with self.store._connect() as db:
            db.execute("UPDATE meta SET value='period' WHERE key='fee_basis'")
        with self.assertRaises(GuardError):
            self.store.load()

    def test_explicit_lifetime_evidence_is_required_and_period_cannot_be_mixed(self):
        self.assert_rejected_without_write(lambda: self.store.apply_snapshot(
            {'synthetic-alpha': Decimal(1)}, 'wrong', self.start, self.today), 'FEE_BASIS_MISMATCH')
        self.assert_rejected_without_write(lambda: self.store.apply_snapshot_result(
            {'synthetic-alpha': Decimal(1)}, 'missing', self.start, self.today,
            fee_basis='key_lifetime'), 'LIFETIME_PERIOD_INVALID')
        old = self.make('period', 'period')
        with self.assertRaises(SnapshotApplyError) as caught:
            self.apply(store=old)
        self.assertEqual(caught.exception.reason_code, 'FEE_BASIS_MISMATCH')

    def test_creation_before_ledger_start_is_counted_and_audited(self):
        result = self.apply(amount='10.25')
        self.assertEqual(result.rows[0]['spent'], Decimal('10.25'))
        self.assertEqual(result.rows[0]['status'], 'triggered')
        self.assertIsNone(result.rows[1]['spent'])
        event = next(e for e in self.store.events() if e['kind'] == 'snapshot')
        self.assertEqual(event['fee_basis'], 'key_lifetime')
        self.assertEqual(event['key_periods'], self.periods())
        self.assertLess(event['key_periods']['synthetic-alpha']['start_date'], self.start)

    def test_empty_invalid_future_and_wrong_day_evidence_rejected_atomically(self):
        cases = [{}, self.periods(created=True), self.periods(created=int(self.now.timestamp()) + 86400)]
        wrong_day = self.periods()
        wrong_day['synthetic-alpha']['start_date'] = self.start
        cases.append(wrong_day)
        wrong_end = self.periods(end=self.start)
        cases.append(wrong_end)
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assert_rejected_without_write(lambda: self.apply(periods=evidence), 'LIFETIME_PERIOD_INVALID')

    def test_unknown_skipped_or_incomplete_evidence_cannot_update_costs(self):
        unbound = {'foreign-id': next(iter(self.periods().values()))}
        self.assert_rejected_without_write(lambda: self.apply(periods=unbound), 'LIFETIME_PERIOD_INVALID')
        extra = {**self.periods(), **unbound}
        self.assert_rejected_without_write(lambda: self.apply(periods=extra), 'LIFETIME_PERIOD_INVALID')
        self.store.save_people([Person('Alpha', Decimal(-1)), Person('Beta', Decimal(20))])
        self.assert_rejected_without_write(self.apply, 'LIFETIME_PERIOD_INVALID')

    def test_partial_fees_can_include_creation_evidence_for_missing_managed_key(self):
        evidence = self.periods()
        evidence['synthetic-beta'] = dict(evidence['synthetic-alpha'])
        result = self.apply(periods=evidence)
        self.assertEqual(result.coverage_summary['active_missing_count'], 1)
        self.assertIsNone(result.rows[1]['spent'])
        with self.store._connect() as db:
            recorded = json.loads(db.execute("SELECT value FROM meta WHERE key='key_creation_times'").fetchone()[0])
        self.assertEqual(recorded, {'synthetic-alpha': self.created, 'synthetic-beta': self.created})

    def test_creation_timestamp_cannot_drift_even_within_same_day(self):
        self.apply()
        self.assert_rejected_without_write(lambda: self.apply('two', '2',
            periods=self.periods(created=self.created - 1)), 'KEY_CREATION_CHANGED')
        self.assertEqual(self.store.current_snapshot_id(), 'one')

    def test_corrupt_saved_creation_witness_is_not_replaced(self):
        self.apply()
        with self.store._connect() as db:
            db.execute("UPDATE meta SET value='[]' WHERE key='key_creation_times'")
        self.assert_rejected_without_write(lambda: self.apply('two', '2'), 'KEY_CREATION_EVIDENCE_INVALID')

    def test_missing_witness_after_existing_fees_cannot_reset_creation_time(self):
        self.apply()
        with self.store._connect() as db:
            db.execute("DELETE FROM meta WHERE key='key_creation_times'")
        self.assert_rejected_without_write(lambda: self.apply('two', '2'), 'KEY_CREATION_EVIDENCE_INVALID')

    def test_identity_only_validation_neither_manufactures_fees_nor_forgets_saved_creation(self):
        before = self.db_state()
        result = self.store.validate_lifetime_observation(self.periods(), end_date=self.today,
                                                          expected_ids=['synthetic-alpha'])
        self.assertEqual(result, self.periods())
        self.assertEqual(self.db_state(), before)
        self.apply()
        before = self.db_state()
        with self.assertRaises(GuardError):
            self.store.validate_lifetime_observation(self.periods(created=self.created - 1),
                end_date=self.today, expected_ids=['synthetic-alpha'])
        self.assertEqual(self.db_state(), before)

    def test_snapshot_failure_rolls_creation_witness_and_costs_back_together(self):
        before = self.db_state()
        with patch.object(self.store, '_record_triggers', side_effect=GuardError('synthetic failure')):
            with self.assertRaises(SnapshotApplyError):
                self.apply()
        self.assertEqual(self.db_state(), before)

    def test_duplicate_source_cannot_add_unseen_period_metadata(self):
        self.apply()
        evidence = self.periods()
        evidence['synthetic-beta'] = dict(evidence['synthetic-alpha'])
        self.assert_rejected_without_write(lambda: self.apply(periods=evidence), 'SNAPSHOT_CONFLICT')

    def test_retired_key_keeps_cost_and_creation_while_next_active_key_updates(self):
        self.apply(amount='10')
        self.store.record_action('Alpha', 'synthetic-alpha', 'absent_verified', 'synthetic absence')
        self.store.bind_keys([{'name': 'Beta', 'tracking_id': 'synthetic-beta'}], 'synthetic-account')
        periods = {'synthetic-beta': next(iter(self.periods().values()))}
        result = self.store.apply_snapshot_result({'synthetic-beta': Decimal('2')}, 'beta',
            self.start, self.today, fee_basis='key_lifetime', key_periods=periods)
        self.assertEqual(result.rows[0]['status'], 'retired')
        self.assertEqual(result.rows[0]['spent'], Decimal(10))
        self.assertEqual(result.rows[1]['spent'], Decimal(2))

    def test_advance_day_replaces_cumulative_total_and_preserves_replay_rules(self):
        self.apply(end=self.start)
        self.apply('two', '2')
        self.assertEqual(self.store.read_evaluation()[0]['spent'], Decimal(2))
        self.assertEqual(self.apply('two', '2').apply_outcome, 'current_duplicate')
        self.assertEqual(self.apply(end=self.start).apply_outcome, 'stale_replay')
        self.assertEqual(self.store.read_evaluation()[0]['spent'], Decimal(2))
        self.assert_rejected_without_write(lambda: self.apply('lower', '.5'), 'CUMULATIVE_DECREASE')

    def test_mode_migration_retains_old_ledger_bindings_and_unresolved_obligation(self):
        old = self.make('period-source', 'period')
        old.apply_snapshot({'synthetic-alpha': Decimal(3)}, 'old-source', self.start, self.today)
        old.record_action('Alpha', 'synthetic-alpha', 'unknown_result', 'synthetic uncertainty')
        before = self.db_state(old)
        successor = old.create_list(self.base / 'migrated', 3, '300', self.today,
                                    fee_basis='key_lifetime')
        self.assertEqual(self.db_state(old), before)
        self.assertEqual(successor.load().fee_basis, 'key_lifetime')
        self.assertEqual(successor.load().account_id, old.load().account_id)
        self.assertEqual(successor.active_bindings(), old.active_bindings())
        self.assertEqual(successor.current_snapshot_id(), '')
        self.assertIsNone(successor.read_evaluation()[0]['spent'])
        self.assertEqual(successor.unresolved_actions()[0]['tracking_id'], 'synthetic-alpha')

    def test_lifetime_successor_inherits_creation_witness_not_fees(self):
        self.apply()
        before = self.db_state()
        successor = self.store.create_successor(self.base / 'successor', keep_account=True)
        self.assertEqual(successor.load().fee_basis, 'key_lifetime')
        self.assertIsNone(successor.read_evaluation()[0]['spent'])
        with self.assertRaises(SnapshotApplyError) as caught:
            self.apply(store=successor, periods=self.periods(created=self.created - 1))
        self.assertEqual(caught.exception.reason_code, 'KEY_CREATION_CHANGED')
        self.assertEqual(self.db_state(), before)

    def test_lifetime_cannot_change_a_global_query_start(self):
        with self.assertRaises(GuardError):
            self.store.update_start_date(self.today, self.base / 'should-not-exist')
        self.assertFalse((self.base / 'should-not-exist').exists())

    def test_switching_via_period_successor_cannot_erase_known_creation_identity(self):
        self.apply()
        period = self.store.create_successor(self.base / 'period-bridge', keep_account=True, fee_basis='period')
        lifetime = period.create_successor(self.base / 'lifetime-again', keep_account=True, fee_basis='key_lifetime')
        with self.assertRaises(SnapshotApplyError) as caught:
            self.apply(store=lifetime, periods=self.periods(created=self.created - 1))
        self.assertEqual(caught.exception.reason_code, 'KEY_CREATION_CHANGED')

    def test_public_identity_only_validator_requires_exact_targets_and_handles_utc8_midnight(self):
        now = datetime(2026, 11, 11, 0, 0, 1, tzinfo=TZ)
        created = int(now.timestamp()) - 1
        periods = {'synthetic-alpha': {'created_at': created, 'start_date': '2026-11-11',
                                      'end_date': '2026-11-11'}}
        result = validate_key_periods(periods, end_date='2026-11-11', expected_ids=['synthetic-alpha'], now=now)
        self.assertEqual(result, periods)
        result['synthetic-alpha']['created_at'] = 1
        self.assertEqual(periods['synthetic-alpha']['created_at'], created)
        for expected in ([], ['synthetic-alpha', 'synthetic-beta'], ['synthetic-alpha', 'synthetic-alpha']):
            with self.assertRaises(GuardError):
                validate_key_periods(periods, end_date='2026-11-11', expected_ids=expected, now=now)


if __name__ == '__main__':
    unittest.main()
