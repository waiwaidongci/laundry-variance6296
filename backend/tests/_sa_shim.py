"""仅用于无 SQLAlchemy 环境下跑集成测试的最小兼容垫片（基于标准库 sqlite3）。

生产环境使用 requirements.txt 中的真实 SQLAlchemy；本垫片只实现本项目服务层
实际用到的表定义、连接上下文管理和 select/insert/update 构造 API，不追求通用。
"""

import sqlite3
import threading

_LOCK = threading.RLock()
_TABLES = {}


class _Expr:
    def __init__(self, sql, params=()):
        self.sql = sql
        self.params = tuple(params)

    def __and__(self, other):
        return _Expr(f'({self.sql}) AND ({other.sql})', self.params + other.params)

    def __or__(self, other):
        return _Expr(f'({self.sql}) OR ({other.sql})', self.params + other.params)

    def in_(self, values):
        values = tuple(values)
        placeholders = ','.join('?' for _ in values)
        return _Expr(f'{self.sql} IN ({placeholders})', self.params + values)

    def is_(self, value):
        if value is None:
            return _Expr(f'{self.sql} IS NULL', self.params)
        return _Expr(f'{self.sql} IS ?', self.params + (value,))

    def is_not(self, value):
        if value is None:
            return _Expr(f'{self.sql} IS NOT NULL', self.params)
        return _Expr(f'{self.sql} IS NOT ?', self.params + (value,))

    def __eq__(self, other):
        if other is None:
            return self.is_(None)
        return _Expr(f'{self.sql} = ?', self.params + (other,))

    def __ne__(self, other):
        if other is None:
            return self.is_not(None)
        return _Expr(f'{self.sql} != ?', self.params + (other,))

    def __gt__(self, other):
        return _Expr(f'{self.sql} > ?', self.params + (other,))

    def __ge__(self, other):
        return _Expr(f'{self.sql} >= ?', self.params + (other,))

    def __lt__(self, other):
        return _Expr(f'{self.sql} < ?', self.params + (other,))

    def __le__(self, other):
        return _Expr(f'{self.sql} <= ?', self.params + (other,))

    def __hash__(self):
        return hash(self.sql)


class _Column(_Expr):
    def __init__(self, name, table_name, kind, pk=False):
        super().__init__(f'{table_name}.{name}')
        self.name = name
        self.table_name = table_name
        self.kind = kind
        self.pk = pk

    def asc(self):
        return _Expr(self.sql + ' ASC')

    def desc(self):
        return _Expr(self.sql + ' DESC')


class _Table:
    def __init__(self, name, metadata, columns):
        self.name = name
        self.metadata = metadata
        self.columns = []
        self._column_map = {}
        for column in columns:
            col = _Column(column['name'], name, column['kind'], column.get('pk', False))
            self.columns.append(col)
            self._column_map[column['name']] = col
        _TABLES[name] = self
        metadata.tables[name] = self

    @property
    def c(self):
        return self

    def __getattr__(self, name):
        try:
            return self.__dict__['_column_map'][name]
        except KeyError as error:
            raise AttributeError(name) from error

    def select(self):
        return _Select(self)

    def insert(self):
        return _Insert(self)

    def update(self):
        return _Update(self)


class _MetaData:
    def __init__(self):
        self.tables = {}

    def create_all(self, engine):
        engine.create_all()

    def drop_all(self, engine):
        engine.drop_all()


MetaData = _MetaData


def Table(name, metadata, *columns):
    parsed = [{'name': col.name, 'kind': col.kind, 'pk': col.pk}
              for col in columns if isinstance(col, Column)]
    return _Table(name, metadata, parsed)


class Column:
    def __init__(self, name, kind, primary_key=False, **kwargs):
        self.name = name
        self.kind = kind
        self.pk = primary_key


class _Type:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        return self


Integer = _Type('INTEGER')
String = _Type('TEXT')
Text = _Type('TEXT')
Double = _Type('REAL')
JSON = _Type('JSON')


class UniqueConstraint:
    def __init__(self, *names):
        self.names = names


def _compile_where(where):
    if where is None:
        return '', ()
    return ' WHERE ' + where.sql, where.params


class _Select:
    def __init__(self, table, columns=None):
        self.table = table
        self.columns = columns
        self.where_expr = None
        self.order = []
        self.limit_value = None

    def select_from(self, table):
        self.table = table
        return self

    def where(self, *exprs):
        self.where_expr = and_(*exprs) if exprs else None
        return self

    def order_by(self, *exprs):
        self.order = exprs
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def _sql(self):
        select_clause = '*'
        if self.columns:
            select_clause = ', '.join(col.sql for col in self.columns)
        table = self.table
        if table is None:
            for column in self.columns or ():
                source = getattr(column, 'source_column', column)
                if isinstance(source, _Column):
                    table = _TABLES[source.table_name]
                    break
        table_name = table.name if table is not None else ''
        sql = f'SELECT {select_clause} FROM {table_name}'
        clause, params = _compile_where(self.where_expr)
        sql += clause
        if self.order:
            sql += ' ORDER BY ' + ', '.join(expr.sql for expr in self.order)
        if self.limit_value is not None:
            sql += f' LIMIT {int(self.limit_value)}'
        return sql, params


class _Insert:
    def __init__(self, table):
        self.table = table
        self.values_data = {}

    def values(self, **data):
        self.values_data = data
        return self


class _Update:
    def __init__(self, table):
        self.table = table
        self.where_expr = None
        self.values_data = {}

    def where(self, *exprs):
        self.where_expr = and_(*exprs) if exprs else None
        return self

    def values(self, **data):
        self.values_data = data
        return self


def select(*items):
    if not items:
        raise ValueError('select() 需要参数')
    first = items[0]
    if isinstance(first, _Table):
        return _Select(first)
    if isinstance(first, _Column):
        table = _TABLES[first.table_name]
        return _Select(table, list(items))
    if isinstance(first, (_Count, _Expr)):
        return _Select(None, list(items))
    raise ValueError('不支持的 select 形式')


def insert(table):
    return table.insert()


def update(table):
    return table.update()


def and_(*exprs):
    result = exprs[0]
    for expr in exprs[1:]:
        result = result & expr
    return result


def or_(*exprs):
    result = exprs[0]
    for expr in exprs[1:]:
        result = result | expr
    return result


class _Count:
    def __init__(self):
        self.sql = 'COUNT(*)'

    def __call__(self):
        return self


def func_count():
    return _Count()


class _Func:
    @property
    def count(self):
        return _Count()

    @property
    def max(self):
        class _Max:
            def __call__(self, column):
                expr = _Expr(f'MAX({column.sql})')
                expr.source_column = column
                return expr
        return _Max()


func = _Func()


class _Result:
    def __init__(self, cursor, table):
        self.cursor = cursor
        self.table = table
        self.description = cursor.description or []
        self.names = [item[0] for item in self.description]
        self._rows = None

    def mappings(self):
        return self

    def _load(self):
        if self._rows is None:
            import json
            rows = self.cursor.fetchall()
            json_columns = {col.name for col in (self.table.columns if self.table else [])
                            if getattr(col.kind, 'name', col.kind) == 'JSON'}
            loaded = []
            for row in rows:
                mapping = _Mapping(dict(zip(self.names, row)))
                for name in json_columns & set(mapping):
                    if isinstance(mapping[name], str):
                        mapping[name] = json.loads(mapping[name])
                loaded.append(mapping)
            self._rows = loaded
        return self._rows

    def __iter__(self):
        return iter(self._load())

    def all(self):
        return self._load()

    def one(self):
        return self._load()[0]

    def one_or_none(self):
        rows = self._load()
        return rows[0] if rows else None

    def scalar(self):
        row = self.cursor.fetchone()
        return row[0] if row else None


class _Mapping(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Connection:
    def __init__(self, engine, transactional=False):
        self.engine = engine
        self.connection = engine.raw
        self.transactional = transactional

    def __enter__(self):
        if self.transactional:
            self.connection.execute('BEGIN')
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.transactional:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()

    def execute(self, statement, params=()):
        import json
        if isinstance(statement, _Select):
            sql, sql_params = statement._sql()
            cursor = self.connection.execute(sql, sql_params)
            return _Result(cursor, statement.table)
        if isinstance(statement, _Insert):
            table = statement.table
            data = dict(statement.values_data)
            columns = list(data)
            for name, col in table._column_map.items():
                if getattr(col.kind, 'name', col.kind) == 'JSON' and name in data and not isinstance(data[name], str):
                    data[name] = json.dumps(data[name], ensure_ascii=False, default=str)
            placeholders = ','.join('?' for _ in columns)
            sql = f'INSERT INTO {table.name} ({",".join(columns)}) VALUES ({placeholders})'
            cursor = self.connection.execute(sql, [data[col] for col in columns])
            return _InsertResult(cursor.lastrowid)
        if isinstance(statement, _Update):
            table = statement.table
            data = dict(statement.values_data)
            for name, col in table._column_map.items():
                if getattr(col.kind, 'name', col.kind) == 'JSON' and name in data and not isinstance(data[name], str):
                    import json as _json
                    data[name] = _json.dumps(data[name], ensure_ascii=False, default=str)
            sets = ', '.join(f'{name} = ?' for name in data)
            clause, where_params = _compile_where(statement.where_expr)
            sql = f'UPDATE {table.name} SET {sets}{clause}'
            cursor = self.connection.execute(sql, [*data.values(), *where_params])
            return _UpdateResult(cursor.rowcount)
        # 原生 SQL（如 SELECT 1）
        cursor = self.connection.execute(statement.sql if hasattr(statement, 'sql') else statement, params)
        return cursor

    def scalar(self, statement):
        return self.execute(statement).scalar()


class _InsertResult:
    def __init__(self, row_id):
        self.inserted_primary_key = [row_id]


class _UpdateResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Engine:
    def __init__(self, url):
        self.raw = sqlite3.connect(url.replace('sqlite:///', ''), check_same_thread=False,
                                   isolation_level=None)
        self.raw.row_factory = None

    def begin(self):
        return _Connection(self, transactional=True)

    def connect(self):
        return _Connection(self, transactional=False)

    def create_all(self):
        with _LOCK:
            for name, table in _TABLES.items():
                defs = []
                for col in table.columns:
                    kind_name = col.kind.name if hasattr(col.kind, 'name') else str(col.kind)
                    kind = 'TEXT' if kind_name in ('TEXT', 'JSON') else kind_name
                    extra = ' PRIMARY KEY AUTOINCREMENT' if col.pk and kind_name == 'INTEGER' else ''
                    defs.append(f'{col.name} {kind}{extra}')
                self.raw.execute(f'CREATE TABLE IF NOT EXISTS {name} ({", ".join(defs)})')

    def drop_all(self):
        with _LOCK:
            for name in list(_TABLES):
                self.raw.execute(f'DROP TABLE IF EXISTS {name}')


def create_engine(url, **kwargs):
    if not url.startswith('sqlite'):
        raise RuntimeError('测试垫片仅支持 sqlite')
    path = url.replace('sqlite:///', '')
    if path == ':memory:':
        # 内存库需要全局唯一连接，避免每个连接拿到独立空库
        if not hasattr(create_engine, '_memory'):
            create_engine._memory = _Engine('sqlite:////tmp/laundry-shim-memory.db')
        return create_engine._memory
    return _Engine(url)
