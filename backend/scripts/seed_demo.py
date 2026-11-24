import hashlib
import json
import secrets
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role
from src.models.database import engine
from src.models.tables import (
    datasets,
    meter_readings,
    reconciliation_versions,
    users,
    wash_windows,
)
from src.services.data import PROFILE, convert, save_revision
from src.services.domain import sample_rows
from src.services.meter_reconciliation import ACTIVE, build_version_document

TZ = timezone(timedelta(hours=8))


def protect(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 240000)
    return f'{salt}:{digest.hex()}'


def _seed_reconciliation(dataset_id, records):
    """为演示台账生成洗程窗口、一一对应的抄表读数与初始对账版本。"""
    shops = sorted({row['shop'] for row in records})
    now = datetime.now(TZ).timestamp()
    summary = {}
    with engine.begin() as connection:
        for shop in shops:
            shop_records = sorted((row for row in records if row['shop'] == shop),
                                  key=lambda row: (row['work_date'], row['batch_id']))
            window_rows = []
            reading_rows = []
            for index, row in enumerate(shop_records):
                day = datetime.fromisoformat(row['work_date'])
                started = datetime.combine(day, time(6, 0), TZ) + timedelta(hours=2 * index)
                finished = started + timedelta(hours=2)
                read_at = started + timedelta(hours=1)
                window_rows.append({
                    'dataset_id': dataset_id, 'shop': shop, 'batch_id': row['batch_id'],
                    'started_at': started.timestamp(), 'finished_at': finished.timestamp(),
                })
                reading_rows.append({
                    'reading_id': f"M-{row['batch_id']}", 'shop': shop,
                    'read_at': read_at.timestamp(), 'liters': float(row['actual_liters']),
                    'received_at': now, 'owner': 'seed',
                })
            for window in window_rows:
                exists = connection.execute(select(wash_windows.c.id).where(
                    wash_windows.c.dataset_id == dataset_id,
                    wash_windows.c.shop == shop,
                    wash_windows.c.batch_id == window['batch_id'],
                )).scalar()
                if not exists:
                    connection.execute(insert(wash_windows).values(**window))
            for reading in reading_rows:
                exists = connection.execute(select(meter_readings.c.id).where(
                    meter_readings.c.reading_id == reading['reading_id']
                )).scalar()
                if not exists:
                    connection.execute(insert(meter_readings).values(**reading))
            has_version = connection.execute(select(reconciliation_versions.c.id).where(
                reconciliation_versions.c.shop == shop,
                reconciliation_versions.c.state == ACTIVE,
            )).scalar()
            if not has_version:
                document = build_version_document(
                    shop, reading_rows, window_rows, shop_records)
                version_id = connection.execute(insert(reconciliation_versions).values(
                    shop=shop, version=1, state=ACTIVE, owner='seed', created=now, **document,
                )).inserted_primary_key[0]
                summary[shop] = {'version': version_id, **{
                    key: document[key] for key in
                    ('max_cardinality', 'certain_count', 'disputed_count', 'meter_gap_total')
                }}
    return summary


def seed():
    for role in Role:
        try:
            with engine.begin() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    connection.execute(insert(users).values(username=role.value, role=role.value,
                                                            password=protect(f'{role.value}-demo-24')))
        except IntegrityError:
            with engine.connect() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    raise
    records = [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']} for row in sample_rows()]
    revision, created = save_revision(PROFILE['filename'], records, 'seed')
    meter_summary = _seed_reconciliation(revision, records)
    with engine.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in (users, datasets)}
    return {'revision': revision, 'created': created, **counts, 'rows': len(records),
            'meter_versions': meter_summary}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
