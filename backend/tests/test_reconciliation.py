"""对账服务与发布门槛的集成测试（sqlite，无需外部服务）。

若环境缺少 SQLAlchemy（如精简 CI），整个模块自动跳过；在项目容器或已安装
requirements 的环境中会完整执行。
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('ARTIFACTS_DIR', '/tmp/laundry-test-artifacts')

try:
    import sqlalchemy  # noqa: F401
    import redis  # noqa: F401
except ImportError:
    import _sa_shim
    sys.modules['sqlalchemy'] = _sa_shim
    sys.modules['sqlalchemy.exc'] = type(sys)('sqlalchemy.exc')

    class _IntegrityError(Exception):
        pass

    sys.modules['sqlalchemy.exc'].IntegrityError = _IntegrityError

    class _FakeRedis:
        def __init__(self, *args, **kwargs):
            self.values = {}

        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

        def set(self, key, value, ex=None):
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

        def ping(self):
            return True

    redis_shim = type(sys)('redis')
    redis_shim.Redis = _FakeRedis
    sys.modules['redis'] = redis_shim

from src.models import database as db_module
from src.models.tables import metadata, meter_jobs, reconciliation_versions
from src.services import meter_reconciliation as mr
from src.services.domain import compute
from src.services.matching import CERTAIN, DISPUTED

TZ = timezone(timedelta(hours=8))


def iso(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).isoformat()


class ReconciliationDBTest(unittest.TestCase):
    def setUp(self):
        self.engine = db_module.engine
        metadata.create_all(self.engine)
        # 直接插入一个数据版本，含两个工坊各两个批次
        from src.models.tables import datasets
        records = []
        for shop in ('东港工坊', '白榆工坊'):
            for index, batch_id in enumerate((f'{shop[0]}1', f'{shop[0]}2'), 1):
                records.append({
                    'work_date': '2026-11-16', 'shop': shop, 'textile_class': '棉质床品',
                    'batch_id': batch_id, 'planned_kg': 100.0, 'actual_kg': 100.0,
                    'plan_standard': '8.5', 'current_standard': '8.2',
                    'actual_liters': 1000.0 * index, 'meter_liters': 1000.0 * index,
                })
        with self.engine.begin() as connection:
            self.dataset_id = connection.execute(
                datasets.insert().values(
                    name='demo', checksum='test-checksum-001', records=records,
                    owner='tester', created=time.time())
            ).inserted_primary_key[0]
        # 登记洗程窗口：东港两个窗口互不重叠；白榆两个窗口完全重叠（制造争议）
        mr.register_windows(self.dataset_id, [
            {'shop': '东港工坊', 'batch_id': '东1',
             'started_at': iso(16, 8), 'finished_at': iso(16, 10)},
            {'shop': '东港工坊', 'batch_id': '东2',
             'started_at': iso(16, 10), 'finished_at': iso(16, 12)},
            {'shop': '白榆工坊', 'batch_id': '白1',
             'started_at': iso(16, 8), 'finished_at': iso(16, 12)},
            {'shop': '白榆工坊', 'batch_id': '白2',
             'started_at': iso(16, 8), 'finished_at': iso(16, 12)},
        ], 'tester')

    def tearDown(self):
        metadata.drop_all(self.engine)

    # ------------------------------------------------------------------
    def _run_one_job(self):
        payload = mr.claim_meter_job()
        self.assertIsNotNone(payload)
        job_id, version_id, error = mr.recompute_shop(payload)
        return payload, job_id, version_id, error

    def test_submit_and_certain_match(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
            {'reading_id': 'R2', 'shop': '东港工坊', 'read_at': iso(16, 11), 'liters': 2000.0},
        ], 'analyst')
        payload, job_id, version_id, error = self._run_one_job()
        self.assertIsNone(error)
        self.assertEqual(payload['shop'], '东港工坊')
        version = mr.require_version(version_id)
        self.assertEqual(version['state'], mr.ACTIVE)
        self.assertEqual(version['certain_count'], 2)
        self.assertEqual(version['disputed_count'], 0)
        self.assertEqual(version['unmatched_reading_ids'], [])
        self.assertEqual(version['unmatched_batch_ids'], [])
        statuses = {pair['reading_id']: pair['status'] for pair in version['pairings']}
        self.assertEqual(statuses, {'R1': CERTAIN, 'R2': CERTAIN})

    def test_duplicate_reading_rejected(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        with self.assertRaises(ValueError):
            mr.submit_readings([
                {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 999.0},
            ], 'analyst')

    def test_late_reading_recomputes_and_supersedes(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        _, _, first_id, _ = self._run_one_job()
        first = mr.require_version(first_id)
        self.assertEqual(first['version'], 1)
        self.assertEqual(first['certain_count'], 1)

        # 迟到的第二条读数：连同旧配对整体重算
        mr.submit_readings([
            {'reading_id': 'R2', 'shop': '东港工坊', 'read_at': iso(16, 11), 'liters': 2000.0},
        ], 'analyst')
        _, _, second_id, _ = self._run_one_job()
        second = mr.require_version(second_id)
        first_reloaded = mr.require_version(first_id)
        self.assertEqual(second['version'], 2)
        self.assertEqual(second['state'], mr.ACTIVE)
        self.assertEqual(second['certain_count'], 2)
        self.assertEqual(first_reloaded['state'], mr.SUPERSEDED)
        self.assertEqual(first_reloaded['superseded_by'], second_id)
        # 旧版本内容不可变
        self.assertEqual(first_reloaded['certain_count'], 1)

    def test_failure_keeps_previous_version(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        _, _, first_id, _ = self._run_one_job()

        # 删掉窗口对应的台账实耗后重算必然失败：旧版本仍 active
        from src.models.tables import datasets
        with self.engine.begin() as connection:
            connection.execute(datasets.update().values(records=[]))
        mr.submit_readings([
            {'reading_id': 'R9', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        payload = mr.claim_meter_job()
        job_id, version_id, error = mr.recompute_shop(payload)
        self.assertIsNone(version_id)
        self.assertIsNotNone(error)
        self.assertEqual(mr.require_version(first_id)['state'], mr.ACTIVE)
        with self.engine.connect() as connection:
            job = connection.execute(meter_jobs.select().where(meter_jobs.c.id == job_id)).mappings().one()
        self.assertEqual(job['state'], mr.FAILED)
        self.assertIn('找不到实耗', job['error'])

    def test_concurrent_old_result_does_not_overwrite_new(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        first_payload = mr.claim_meter_job()
        # 第一条任务还在算，又来迟到读数，排队第二个任务
        mr.submit_readings([
            {'reading_id': 'R2', 'shop': '东港工坊', 'read_at': iso(16, 11), 'liters': 2000.0},
        ], 'analyst')
        # 同店在算，第二个任务暂不可认领
        self.assertIsNone(mr.claim_meter_job())
        # 第一个任务先提交（输入只有 R1），得到 v1
        j1, v1, error = mr.recompute_shop(first_payload)
        self.assertIsNone(error)
        # 第二个任务随后认领并提交，得到 v2
        second_payload = mr.claim_meter_job()
        self.assertIsNotNone(second_payload)
        j2, v2, error = mr.recompute_shop(second_payload)
        self.assertIsNone(error)
        self.assertEqual(mr.require_version(v1)['state'], mr.SUPERSEDED)
        self.assertEqual(mr.require_version(v2)['state'], mr.ACTIVE)

        # 模拟旧任务延迟提交：构造一个更早创建、此刻才落库的任务，应被代际保护拦截
        with self.engine.begin() as connection:
            stale_job = connection.execute(meter_jobs.insert().values(
                shop='东港工坊', state=mr.RUNNING, owner='tester',
                created=time.time() - 100, started=time.time() - 90,
            )).inserted_primary_key[0]
        stale_payload = {**second_payload, 'job_id': stale_job,
                         'claim': time.time() - 90, 'job_created': time.time() - 100}
        job_id, version_id, error = mr.recompute_shop(stale_payload)
        self.assertIsNone(version_id)
        self.assertIn('放弃写入', error)
        with self.engine.connect() as connection:
            row = connection.execute(meter_jobs.select().where(meter_jobs.c.id == stale_job)).mappings().one()
        self.assertEqual(row['state'], mr.STALE)
        # 新版本依旧 active 且未被覆盖
        self.assertEqual(mr.require_version(v2)['state'], mr.ACTIVE)

    def test_disputed_and_unmatched_block_publish(self):
        # 白榆两条读数都能配两个批次：争议配对，未配上 0 但全部争议
        mr.submit_readings([
            {'reading_id': 'W1', 'shop': '白榆工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
            {'reading_id': 'W2', 'shop': '白榆工坊', 'read_at': iso(16, 10), 'liters': 2000.0},
        ], 'analyst')
        _, _, version_id, _ = self._run_one_job()
        version = mr.require_version(version_id)
        self.assertEqual(version['max_cardinality'], 2)
        self.assertEqual(version['disputed_count'], 2)
        self.assertEqual(version['certain_count'], 0)

        from src.models.tables import datasets
        with self.engine.connect() as connection:
            records = connection.execute(
                datasets.select().where(datasets.c.id == self.dataset_id)
            ).mappings().one()['records']
        reconciliation = mr.active_version_for_shops({'白榆工坊'})
        result = compute(records, {'shop': '白榆工坊', 'textile_class': '*',
                                   'view': '透视汇总', 'meter_tolerance': 0},
                         reconciliation)
        self.assertFalse(result['can_publish'])
        self.assertTrue(any('争议' in note for note in result['notes']))
        self.assertEqual(result['metrics']['争议配对'], 2)
        # 快照写入结果，供批准后的报告长期留档
        self.assertEqual(result['reconciliation']['version_ids']['白榆工坊'], version_id)

    def test_missing_version_blocks_publish_but_legacy_path_still_runs(self):
        from src.models.tables import datasets
        with self.engine.connect() as connection:
            records = connection.execute(
                datasets.select().where(datasets.c.id == self.dataset_id)
            ).mappings().one()['records']
        # 不带对账快照：老口径仍可计算，但因缺对账版本而拦截发布
        result = compute(records, {'shop': '*', 'textile_class': '*',
                                   'view': '透视汇总', 'meter_tolerance': 0})
        self.assertFalse(result['can_publish'])
        self.assertTrue(any('尚无生效的水表对账版本' in note for note in result['notes']))

    def test_approved_report_keeps_frozen_pairings_after_late_reread(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
            {'reading_id': 'R2', 'shop': '东港工坊', 'read_at': iso(16, 11), 'liters': 2000.0},
        ], 'analyst')
        _, _, v1, _ = self._run_one_job()
        from src.models.tables import datasets
        with self.engine.connect() as connection:
            records = connection.execute(
                datasets.select().where(datasets.c.id == self.dataset_id)
            ).mappings().one()['records']
        snapshot = mr.active_version_for_shops({'东港工坊'})
        approved = compute(records, {'shop': '东港工坊', 'textile_class': '*',
                                     'view': '透视汇总', 'meter_tolerance': 0}, snapshot)
        self.assertTrue(approved['can_publish'])
        frozen_ids = approved['reconciliation']['version_ids'].copy()
        frozen_pairs = approved['reconciliation']['pairings'].copy()

        # 迟到读数触发重算，新版本生效
        mr.submit_readings([
            {'reading_id': 'R3', 'shop': '东港工坊', 'read_at': iso(16, 11, 30), 'liters': 2001.0},
        ], 'analyst')
        self._run_one_job()
        # 已批准报告持有的仍是 v1 快照
        self.assertEqual(frozen_ids['东港工坊'], v1)
        self.assertEqual(len(frozen_pairs), 2)
        newest = mr.active_versions()[0]
        self.assertNotEqual(newest['id'], v1)

    def test_window_validation(self):
        with self.assertRaises(ValueError):
            mr.register_windows(self.dataset_id, [
                {'shop': '东港工坊', 'batch_id': 'X',
                 'started_at': iso(16, 10), 'finished_at': iso(16, 10)},
            ], 'tester')
        with self.assertRaises(ValueError):
            mr.parse_reading({'reading_id': 'R', 'shop': 'S',
                              'read_at': '2026-11-16T09:00:00', 'liters': 1}, 2)

    def test_stuck_running_job_is_reclaimed(self):
        mr.submit_readings([
            {'reading_id': 'R1', 'shop': '东港工坊', 'read_at': iso(16, 9), 'liters': 1000.0},
        ], 'analyst')
        payload = mr.claim_meter_job()
        stuck_id = payload['job_id']
        # 新鲜的在算任务会阻塞同店后续排队
        mr.submit_readings([
            {'reading_id': 'R2', 'shop': '东港工坊', 'read_at': iso(16, 11), 'liters': 2000.0},
        ], 'analyst')
        self.assertIsNone(mr.claim_meter_job())
        # 模拟 worker 崩溃后超过 120 秒阈值
        with self.engine.begin() as connection:
            connection.execute(meter_jobs.update().where(meter_jobs.c.id == stuck_id)
                               .values(started=time.time() - 200))
        # 卡死任务被回收为排队并可被认领，且拿到全部读数
        reclaimed = mr.claim_meter_job()
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed['job_id'], stuck_id)
        self.assertEqual(len(reclaimed['readings']), 2)
        job_id, version_id, error = mr.recompute_shop(reclaimed)
        self.assertIsNone(error)
        version = mr.require_version(version_id)
        self.assertEqual(version['certain_count'], 2)
        with self.engine.connect() as connection:
            states = {row['id']: row['state'] for row in
                      connection.execute(meter_jobs.select()).mappings()}
        self.assertEqual(states[stuck_id], mr.COMPLETED)


if __name__ == '__main__':
    unittest.main()
