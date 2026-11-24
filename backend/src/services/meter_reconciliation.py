"""水表读数对账服务：读数登记、迟到重算与版本管理。

业务规则：
- 抄表组单独交读数，读数没有洗涤批号，只有工坊、抄表时刻和读数方量；
- 读数表只追加、不改写；重复读数号直接拒绝（迟到记录是新记录，不是覆盖）；
- 每登记一条读数，就为该工坊排队一次重算；重算范围是该工坊**全部**读数与洗程；
- 重算产出不可变版本，版本号在工坊内递增；新版本生效时旧版本标记 superseded；
- 计算失败时旧版本保持 active，任务记 failed 并保留错误信息；
- 同一工坊并发重算时，先提交的版本优先；后提交者若发现已有更新版本，则放弃
  写入（旧结果不覆盖新结果），任务标记 stale。
"""

import time
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update

from src.models.database import add_event, engine, fetch_one
from src.models.tables import (
    datasets,
    meter_jobs,
    meter_readings,
    reconciliation_versions,
    wash_windows,
)
from src.services.matching import (
    CERTAIN,
    DISPUTED,
    MeterReading,
    WashBatch,
    match,
)

ACTIVE = 'active'
SUPERSEDED = 'superseded'
FAILED = 'failed'
STALE = 'stale'
COMPLETED = 'completed'
QUEUED = 'queued'
RUNNING = 'running'
MAX_LITERS = 100000.0


def _aware_datetime(value, label):
    if value is None:
        raise ValueError(f'{label}不能为空。')
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except ValueError as error:
            raise ValueError(f'{label}请使用 ISO 8601 日期时间（如 2026-11-16T09:30:00+08:00）。') from error
    if moment.utcoffset() is None:
        raise ValueError(f'{label}须带时区偏移，避免跨工坊对时歧义。')
    return moment


def _liters(value):
    try:
        liters = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError('水表读数方量须为数字。') from error
    if not (0.0 <= liters <= MAX_LITERS):
        raise ValueError(f'水表读数方量须为 0 至 {int(MAX_LITERS)} L 的有限数值。')
    return liters


def _label(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}不能为空。')
    return value.strip()


def parse_reading(row, line):
    try:
        reading_id = _label(row.get('reading_id'), '读数编号')
        shop = _label(row.get('shop'), '工坊名称')
        read_at = _aware_datetime(row.get('read_at'), '抄表时刻')
        liters = _liters(row.get('liters'))
    except ValueError as error:
        raise ValueError(f'读数第 {line} 行。{error}') from error
    return {'reading_id': reading_id, 'shop': shop, 'read_at': read_at, 'liters': liters}


def parse_window(row, line):
    try:
        shop = _label(row.get('shop'), '工坊名称')
        batch_id = _label(row.get('batch_id'), '洗涤批号')
        started = _aware_datetime(row.get('started_at'), '洗程开始时刻')
        finished = _aware_datetime(row.get('finished_at'), '洗程结束时刻')
    except ValueError as error:
        raise ValueError(f'洗程第 {line} 行。{error}') from error
    if not started < finished:
        raise ValueError(f'洗程第 {line} 行：结束时刻必须晚于开始时刻。')
    return {'shop': shop, 'batch_id': batch_id, 'started_at': started, 'finished_at': finished}


def register_windows(dataset_id, windows, actor):
    """为数据版本登记批次洗程窗口；同版本同窗号重复提交按已存在处理。"""
    with engine.begin() as connection:
        if connection.execute(select(datasets.c.id).where(datasets.c.id == dataset_id)).scalar() is None:
            raise ValueError('数据版本不存在，无法登记洗程窗口。')
        saved = 0
        for line, window in enumerate(windows, 2):
            parsed = parse_window(window, line)
            exists = connection.execute(
                select(wash_windows.c.id).where(
                    wash_windows.c.dataset_id == dataset_id,
                    wash_windows.c.shop == parsed['shop'],
                    wash_windows.c.batch_id == parsed['batch_id'],
                )
            ).scalar()
            if exists:
                continue
            connection.execute(insert(wash_windows).values(
                dataset_id=dataset_id, shop=parsed['shop'], batch_id=parsed['batch_id'],
                started_at=parsed['started_at'].timestamp(),
                finished_at=parsed['finished_at'].timestamp(),
            ))
            saved += 1
        add_event(connection, actor, 'window_sync', dataset_id, time.time())
    return {'saved': saved, 'dataset_id': dataset_id}


def submit_readings(rows, actor):
    """追加登记读数，并为每个受影响工坊排队一次重算。"""
    if not 1 <= len(rows) <= 5000:
        raise ValueError('每次请提交 1 至 5000 条抄表读数。')
    parsed_rows = [parse_reading(row, line) for line, row in enumerate(rows, 2)]
    now = time.time()
    affected_shops = []
    with engine.begin() as connection:
        for parsed in parsed_rows:
            duplicate = connection.execute(
                select(meter_readings.c.id).where(meter_readings.c.reading_id == parsed['reading_id'])
            ).scalar()
            if duplicate is not None:
                raise ValueError(f"读数编号 {parsed['reading_id']} 已存在，读数记录不可覆盖；"
                                 '迟到数据请使用新的读数编号登记。')
            connection.execute(insert(meter_readings).values(
                reading_id=parsed['reading_id'], shop=parsed['shop'],
                read_at=parsed['read_at'].timestamp(), liters=parsed['liters'],
                received_at=now, owner=actor,
            ))
            if parsed['shop'] not in affected_shops:
                affected_shops.append(parsed['shop'])
        for shop in affected_shops:
            job_id = connection.execute(insert(meter_jobs).values(
                shop=shop, state=QUEUED, owner=actor, created=now,
            )).inserted_primary_key[0]
            add_event(connection, actor, 'meter_queue', job_id, now)
    return {'accepted': len(parsed_rows), 'shops': affected_shops}


def claim_meter_job():
    """认领一个排队中的工坊重算任务，返回任务与该工坊当前全部输入。

    同工坊已有在算任务时跳过该工坊的排队任务（保留在队列中），改领其他工坊的
    任务，保证同一工坊同一时刻只有一个重算在执行。在算超过 120 秒的任务视为
    worker 崩溃，回收为排队，避免该工坊被永久卡死。
    """
    now = time.time()
    with engine.begin() as connection:
        connection.execute(
            update(meter_jobs).where(meter_jobs.c.state == RUNNING,
                                     meter_jobs.c.started < now - 120)
            .values(state=QUEUED, started=None)
        )
        running_shops = {row[0] for row in connection.execute(
            select(meter_jobs.c.shop).where(meter_jobs.c.state == RUNNING)
        ).all()}
        queued = connection.execute(
            select(meter_jobs).where(meter_jobs.c.state == QUEUED)
            .order_by(meter_jobs.c.id).limit(20)
        ).mappings().all()
        job = next((item for item in queued if item['shop'] not in running_shops), None)
        if job is None:
            return None
        claimed = connection.execute(
            update(meter_jobs).where(meter_jobs.c.id == job['id'], meter_jobs.c.state == QUEUED)
            .values(state=RUNNING, started=now)
        )
        if claimed.rowcount != 1:
            return None
        readings = [dict(row) for row in connection.execute(
            select(meter_readings).where(meter_readings.c.shop == job['shop'])
            .order_by(meter_readings.c.read_at, meter_readings.c.reading_id)
        ).mappings()]
        dataset_id = connection.execute(
            select(func.max(datasets.c.id))
        ).scalar()
        windows = [dict(row) for row in connection.execute(
            select(wash_windows).where(wash_windows.c.dataset_id == dataset_id,
                                       wash_windows.c.shop == job['shop'])
            .order_by(wash_windows.c.batch_id)
        ).mappings()]
        batch_rows = []
        if dataset_id is not None:
            dataset = connection.execute(select(datasets).where(datasets.c.id == dataset_id)).mappings().one()
            batch_rows = [row for row in dataset['records'] if row.get('shop') == job['shop']]
        return {
            'job_id': job['id'], 'claim': now, 'shop': job['shop'],
            'job_created': job['created'],
            'dataset_id': dataset_id, 'readings': readings, 'windows': windows,
            'batch_rows': batch_rows,
        }


def _load_batches(shop, windows, batch_rows):
    """把台账批次与登记的洗程窗口合并为匹配输入；缺窗口的批次直接报错。"""
    liters_by_batch = {row['batch_id']: float(row['actual_liters']) for row in batch_rows}
    batches = []
    missing = []
    for window in windows:
        liters = liters_by_batch.get(window['batch_id'])
        if liters is None:
            missing.append(window['batch_id'])
            continue
        batches.append(WashBatch(
            batch_id=window['batch_id'], shop=shop,
            started_at=datetime.fromtimestamp(window['started_at'], tz=timezone.utc),
            finished_at=datetime.fromtimestamp(window['finished_at'], tz=timezone.utc),
            actual_liters=liters,
        ))
    if missing:
        raise ValueError('以下洗涤批号在当前台账中找不到实耗，无法对账：' + '、'.join(sorted(missing)))
    return batches


def build_version_document(shop, readings, windows, batch_rows):
    """纯计算：把某工坊的全部输入算成一份版本文档（不落库、不排队）。"""
    meter_readings = [
        MeterReading(
            reading_id=row['reading_id'], shop=row['shop'],
            read_at=datetime.fromtimestamp(row['read_at'], tz=timezone.utc),
            liters=float(row['liters']),
            received_at=datetime.fromtimestamp(row['received_at'], tz=timezone.utc),
        )
        for row in readings
    ]
    batches = _load_batches(shop, windows, batch_rows)
    result = match(meter_readings, batches)
    certain = result.certain_pairings
    disputed = result.disputed_pairings
    pairings = [
        {
            'reading_id': pair.reading_id, 'batch_id': pair.batch_id, 'shop': pair.shop,
            'read_at': pair.read_at.isoformat(), 'reading_liters': pair.reading_liters,
            'batch_liters': pair.batch_liters, 'gap': pair.gap, 'status': pair.status,
        }
        for pair in result.pairings
    ]
    return {
        'pairings': pairings,
        'candidates': result.candidates,
        'unmatched_reading_ids': result.unmatched_reading_ids,
        'unmatched_batch_ids': result.unmatched_batch_ids,
        'disputed_edges': result.disputed_edges,
        'reading_ids': [item.reading_id for item in meter_readings],
        'batch_ids': [item.batch_id for item in batches],
        'max_cardinality': result.max_cardinality,
        'certain_count': len(certain),
        'disputed_count': len(disputed),
        'meter_gap_total': round(sum(pair.gap for pair in certain), 6),
    }


def recompute_shop(payload):
    """执行单个工坊的重算并落版本，返回 (job_id, version_id|None, error|None)。"""
    job_id = payload['job_id']
    shop = payload['shop']
    now = time.time()
    try:
        document = build_version_document(
            shop, payload['readings'], payload['windows'], payload['batch_rows'])
    except ValueError as error:
        _fail_job(job_id, str(error))
        return job_id, None, str(error)

    with engine.begin() as connection:
        active_job = connection.execute(
            select(meter_jobs.c.id).where(meter_jobs.c.id == job_id,
                                          meter_jobs.c.state == RUNNING)
        ).scalar()
        if active_job is None:
            return job_id, None, '任务已被更新的重算接管。'
        # 代际保护：本任务认领之后，若已有更晚创建的同工坊任务把版本写出，
        # 本次结果作废，旧结果绝不覆盖新结果。
        newer_finished = connection.execute(
            select(func.count()).select_from(meter_jobs).where(
                meter_jobs.c.shop == shop,
                meter_jobs.c.version_id.is_not(None),
                meter_jobs.c.created > payload['job_created'],
            )
        ).scalar()
        if newer_finished:
            connection.execute(update(meter_jobs).where(meter_jobs.c.id == job_id)
                               .values(state=STALE, finished=time.time()))
            add_event(connection, 'worker', 'meter_stale', job_id, time.time())
            return job_id, None, '已有更新的重算版本生效，本次旧结果放弃写入。'
        newest = connection.execute(
            select(func.max(reconciliation_versions.c.version))
            .where(reconciliation_versions.c.shop == shop,
                   reconciliation_versions.c.state == ACTIVE)
        ).scalar()
        next_version = (newest or 0) + 1
        version_id = connection.execute(insert(reconciliation_versions).values(
            shop=shop, version=next_version, state=ACTIVE, owner='worker', created=now,
            **document,
        )).inserted_primary_key[0]
        # 旧版本整体转 superseded（不可变，仅状态迁移）
        connection.execute(update(reconciliation_versions).where(
            reconciliation_versions.c.shop == shop,
            reconciliation_versions.c.id != version_id,
            reconciliation_versions.c.state == ACTIVE,
        ).values(state=SUPERSEDED, superseded_by=version_id))
        connection.execute(update(meter_jobs).where(
            meter_jobs.c.id == job_id, meter_jobs.c.state == RUNNING,
        ).values(state=COMPLETED, finished=time.time(), version_id=version_id))
        add_event(connection, 'worker', 'meter_version', version_id, time.time())
    return job_id, version_id, None


def _fail_job(job_id, error):
    with engine.begin() as connection:
        updated = connection.execute(update(meter_jobs).where(
            meter_jobs.c.id == job_id, meter_jobs.c.state == RUNNING,
        ).values(state=FAILED, error=error, finished=time.time()))
        if updated.rowcount:
            add_event(connection, 'worker', 'meter_failed', job_id, time.time())


def active_versions():
    """每个工坊当前生效的对账版本。"""
    with engine.connect() as connection:
        rows = connection.execute(
            select(reconciliation_versions).where(reconciliation_versions.c.state == ACTIVE)
            .order_by(reconciliation_versions.c.shop)
        ).mappings().all()
        return [dict(row) for row in rows]


def version_history(shop=None, limit=100):
    statement = select(reconciliation_versions).order_by(
        reconciliation_versions.c.id.desc()).limit(limit)
    if shop:
        statement = statement.where(reconciliation_versions.c.shop == shop)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


def require_version(version_id):
    with engine.connect() as connection:
        row = fetch_one(connection, reconciliation_versions, version_id)
    if row is None:
        try:
            from fastapi import HTTPException
        except ImportError:
            raise LookupError(f'对账版本 {version_id} 不存在。')
        raise HTTPException(404, '对账版本不存在。')
    return dict(row)


def active_version_for_shops(shops):
    """供分析计算取用：返回工坊 -> 当前生效版本（含配对明细）。"""
    if not shops:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(reconciliation_versions).where(
                reconciliation_versions.c.state == ACTIVE,
                reconciliation_versions.c.shop.in_(shops),
            )
        ).mappings().all()
    return {row['shop']: dict(row) for row in rows}


def meter_jobs_recent(limit=50):
    with engine.connect() as connection:
        rows = connection.execute(select(meter_jobs).order_by(meter_jobs.c.id.desc())
                                  .limit(limit)).mappings().all()
        return [dict(row) for row in rows]


def list_readings(shop=None, limit=500):
    statement = select(meter_readings).order_by(meter_readings.c.id.desc()).limit(limit)
    if shop:
        statement = statement.where(meter_readings.c.shop == shop)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]
