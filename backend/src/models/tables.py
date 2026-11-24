from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False), Column('scheduled', Double, nullable=False),
                   Column('delivered', Double, nullable=False), UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
meter_readings = Table('meter_readings', metadata, Column('id', Integer, primary_key=True),
                       Column('reading_id', String(60), nullable=False, unique=True),
                       Column('shop', String(80), nullable=False, index=True),
                       Column('read_at', Double, nullable=False), Column('liters', Double, nullable=False),
                       Column('received_at', Double, nullable=False), Column('owner', String(40), nullable=False))
wash_windows = Table('wash_windows', metadata, Column('id', Integer, primary_key=True),
                     Column('dataset_id', Integer, nullable=False, index=True),
                     Column('shop', String(80), nullable=False), Column('batch_id', String(60), nullable=False),
                     Column('started_at', Double, nullable=False), Column('finished_at', Double, nullable=False),
                     UniqueConstraint('dataset_id', 'shop', 'batch_id'))
reconciliation_versions = Table('reconciliation_versions', metadata, Column('id', Integer, primary_key=True),
                                Column('shop', String(80), nullable=False), Column('version', Integer, nullable=False),
                                Column('state', String(16), nullable=False, index=True),
                                Column('pairings', JSON, nullable=False), Column('candidates', JSON, nullable=False),
                                Column('unmatched_reading_ids', JSON, nullable=False),
                                Column('unmatched_batch_ids', JSON, nullable=False),
                                Column('disputed_edges', JSON, nullable=False),
                                Column('reading_ids', JSON, nullable=False), Column('batch_ids', JSON, nullable=False),
                                Column('max_cardinality', Integer, nullable=False),
                                Column('certain_count', Integer, nullable=False),
                                Column('disputed_count', Integer, nullable=False),
                                Column('meter_gap_total', Double, nullable=False),
                                Column('superseded_by', Integer), Column('error', Text),
                                Column('owner', String(40), nullable=False), Column('created', Double, nullable=False),
                                UniqueConstraint('shop', 'version'))
meter_jobs = Table('meter_jobs', metadata, Column('id', Integer, primary_key=True),
                   Column('shop', String(80), nullable=False, index=True),
                   Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
                   Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
                   Column('version_id', Integer), Column('error', Text))
