"""Batch water variance and meter reconciliation for the wash-shop pivot."""

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation

from src.services.matching import CERTAIN, DISPUTED


ALL = '*'
UNKNOWN = '未知'
PIVOT = '透视汇总'
BATCHES = '展开批次'
INPUT_PRECISION = Decimal('0.001')
RATIO_PRECISION = Decimal('0.000001')
ZERO = Decimal(0)


@dataclass(frozen=True)
class WaterTotals:
    planned_kg: Decimal = ZERO
    actual_kg: Decimal = ZERO
    actual_liters: Decimal = ZERO
    meter_liters: Decimal = ZERO
    meter_absolute_gap: Decimal = ZERO
    baseline_liters: Decimal = ZERO
    covered_actual_liters: Decimal = ZERO
    standard_variance: Decimal = ZERO
    volume_mix_variance: Decimal = ZERO
    efficiency_variance: Decimal = ZERO
    unknown_actual_liters: Decimal = ZERO
    unknown_actual_kg: Decimal = ZERO
    missing_plan: int = 0
    missing_current: int = 0
    unknown_batches: int = 0

    @property
    def variance(self):
        return self.covered_actual_liters - self.baseline_liters

    @property
    def residual(self):
        return self.actual_liters - (
            self.baseline_liters + self.standard_variance + self.volume_mix_variance
            + self.efficiency_variance + self.unknown_actual_liters
        )


@dataclass(frozen=True)
class Batch:
    day: str
    shop: str
    textile: str
    batch_id: str
    water: WaterTotals


def sample_rows() -> list[dict]:
    shops = ('东港工坊', '白榆工坊', '龙桥工坊')
    textiles = (
        ('棉质床品', Decimal('8.500'), Decimal('8.200')),
        ('厚绒浴巾', Decimal('11.000'), Decimal('10.800')),
        ('涤棉制服', Decimal('7.400'), Decimal('7.400')),
        ('防水护理垫', Decimal('9.300'), Decimal('9.700')),
    )
    rows = []
    for day in range(16, 20):
        for shop_index, shop in enumerate(shops):
            for textile_index, (textile, old_rate, current_rate) in enumerate(textiles):
                planned = 95 + 22 * shop_index + 13 * textile_index + 3 * (day - 16)
                actual = planned + (-9, 14, -5, 11)[(shop_index + day + textile_index) % 4]
                efficiency = (18, 37, -21)[shop_index] + (-6, 24, -9, 8)[textile_index]
                liters = float(actual * current_rate + efficiency)
                rows.append({
                    'work_date': f'2026-11-{day}', 'shop': shop, 'textile_class': textile,
                    'batch_id': f'WX-{day}-{shop_index + 1}{textile_index + 1}',
                    'planned_kg': float(planned), 'actual_kg': float(actual),
                    'plan_standard': str(old_rate), 'current_standard': str(current_rate),
                    'actual_liters': liters, 'meter_liters': liters,
                })
    return rows


def _number(value, label, maximum, *, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f'{label}须填写数字，请更正后重试。') from error
    if not number.is_finite() or not ZERO <= number <= maximum or (positive and not number):
        lower = '大于 0' if positive else '大于等于 0'
        raise ValueError(f'{label}须为{lower}且不超过 {maximum} 的有限数值，请核对单位。')
    rounded = number.quantize(INPUT_PRECISION)
    if number != rounded:
        raise ValueError(f'{label}最多保留 3 位小数，请按计量精度更正。')
    return rounded


def _label(value, name):
    if not isinstance(value, str) or not value.strip() or value.strip() == ALL:
        raise ValueError(f'请填写有效的{name}，星号仅用于查询全部。')
    return value.strip()


def _read_batch(row, line):
    try:
        day = row.get('work_date')
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError('生产日请使用 YYYY-MM-DD 格式填写有效日期。') from error
        shop = _label(row.get('shop'), '工坊名称')
        textile = _label(row.get('textile_class'), '布类名称')
        batch_id = _label(row.get('batch_id'), '洗涤批号')
        planned = _number(row.get('planned_kg'), '计划重量（kg）', 1000)
        actual = _number(row.get('actual_kg'), '实际重量（kg）', 1000)
        actual_liters = _number(row.get('actual_liters'), '批次实耗（L）', 100000)
        meter_liters = _number(row.get('meter_liters'), '水表实耗（L）', 100000)
        plan_rate = row.get('plan_standard')
        current_rate = row.get('current_standard')
        old = None if plan_rate == UNKNOWN else _number(plan_rate, '计划水耗定额（L/kg）', 100, positive=True)
        new = None if current_rate == UNKNOWN else _number(current_rate, '执行水耗定额（L/kg）', 100, positive=True)
    except ValueError as error:
        raise ValueError(f'台账第 {line} 行。{error}') from error

    if planned == actual == actual_liters == meter_liters == ZERO:
        raise ValueError(f'台账第 {line} 行没有重量或用水，请核对批次或删除空记录。')
    common = dict(planned_kg=planned, actual_kg=actual, actual_liters=actual_liters,
                  meter_liters=meter_liters, meter_absolute_gap=abs(actual_liters - meter_liters))
    if old is None or new is None:
        water = WaterTotals(**common, unknown_actual_liters=actual_liters, unknown_actual_kg=actual,
                            missing_plan=int(old is None), missing_current=int(new is None), unknown_batches=1)
    else:
        water = WaterTotals(
            **common, baseline_liters=planned * old, covered_actual_liters=actual_liters,
            standard_variance=planned * (new - old), volume_mix_variance=(actual - planned) * new,
            efficiency_variance=actual_liters - actual * new,
        )
    return Batch(day, shop, textile, batch_id, water)


def _aggregate(waters):
    members = tuple(waters)
    return WaterTotals(**{
        field.name: sum(getattr(water, field.name) for water in members)
        for field in fields(WaterTotals)
    })


def _weighted_rate(liters, kg):
    return float((liters / kg).quantize(RATIO_PRECISION)) if kg else None


def build_reconciliation_view(selected_batches, reconciliation, scope_shops, meter_limit):
    """把分析时刻冻结的对账版本快照，折算到当前筛选范围。

    reconciliation: {shop: version_row}，version_row 含 pairings、unmatched_*、
    disputed_edges 等不可变字段。范围内若某工坊没有生效版本，视为该工坊读数
    尚未完成对账，整批发售拦截。
    """
    view = {
        'pairings': [], 'version_ids': {}, 'blockers': [],
        'certain_count': 0, 'disputed_count': 0,
        'unmatched_reading_count': 0, 'unmatched_batch_count': 0,
        'gap_total': 0.0, 'missing_shops': [],
    }
    batch_ids_in_scope = {batch.batch_id for batch in selected_batches}
    over_limit = []
    for shop in sorted(scope_shops):
        version = reconciliation.get(shop)
        if version is None:
            view['missing_shops'].append(shop)
            continue
        view['version_ids'][shop] = version['id']
        pairings = [pair for pair in version['pairings']
                    if pair['batch_id'] in batch_ids_in_scope]
        view['pairings'].extend(pairings)
        view['certain_count'] += sum(1 for pair in pairings if pair['status'] == CERTAIN)
        view['disputed_count'] += sum(1 for pair in pairings if pair['status'] == DISPUTED)
        # 范围内批次维度的未配/争议统计
        scope_batch_ids = {batch.batch_id for batch in selected_batches if batch.shop == shop}
        matched_batch_ids = {pair['batch_id'] for pair in pairings}
        view['unmatched_batch_count'] += len(scope_batch_ids - matched_batch_ids)
        view['unmatched_reading_count'] += len(version.get('unmatched_reading_ids', []))
        for pair in pairings:
            if pair['status'] == CERTAIN:
                view['gap_total'] += pair['gap']
                if pair['gap'] > float(meter_limit):
                    over_limit.append((pair['reading_id'], pair['batch_id'], pair['gap']))
    view['gap_total'] = round(view['gap_total'], 6)
    if view['missing_shops']:
        view['blockers'].append(
            '以下工坊尚无生效的水表对账版本，请先完成读数重算：' + '、'.join(view['missing_shops']))
    if view['disputed_count']:
        view['blockers'].append(
            f'{view["disputed_count"]} 对配对存在多种同基数配法，争议批次不通过水表对账，请现场核对读数时刻。')
    if view['unmatched_reading_count']:
        view['blockers'].append(f'{view["unmatched_reading_count"]} 条抄表读数未配上任何洗程批次。')
    if view['unmatched_batch_count']:
        view['blockers'].append(f'{view["unmatched_batch_count"]} 个批次没有任何可配水表读数，不通过水表对账。')
    if over_limit:
        detail = '；'.join(f'{reading}/{batch} 差 {gap:f} L' for reading, batch, gap in over_limit[:5])
        view['blockers'].append(f'确定配对的水表差超出 {meter_limit:f} L 容差：{detail}')
    return view


def _result_row(level, shop, textile, water, *, day='', batch_id=''):
    return {
        '粒度': level,
        '工坊': shop,
        '布类': textile,
        '生产日': day,
        '洗涤批号': batch_id,
        '计划重量（kg）': float(water.planned_kg),
        '实际重量（kg）': float(water.actual_kg),
        '实耗（L）': float(water.actual_liters),
        '加权实耗（L/kg）': _weighted_rate(water.actual_liters, water.actual_kg),
        '已匹配计划基准（L）': float(water.baseline_liters),
        '已匹配耗水差异（L）': float(water.variance),
        '标准差（L）': float(water.standard_variance),
        '量与结构差（L）': float(water.volume_mix_variance),
        '效率差（L）': float(water.efficiency_variance),
        '缺计划定额批次': water.missing_plan,
        '缺执行定额批次': water.missing_current,
        '缺标批次': water.unknown_batches,
        '缺标重量（kg）': float(water.unknown_actual_kg),
        '缺标实耗（L）': float(water.unknown_actual_liters),
        '独立水表（L）': float(water.meter_liters),
        '水表累计绝对差（L）': float(water.meter_absolute_gap),
        '分解残差（L）': float(water.residual),
    }


def compute(rows: list[dict], options: dict, reconciliation: dict | None = None) -> dict:
    if not 1 <= len(rows) <= 5000:
        raise ValueError('请导入 1 至 5000 个洗涤批次，超出时按生产周期拆分台账。')
    shop_filter = options.get('shop', ALL)
    textile_filter = options.get('textile_class', ALL)
    if shop_filter != ALL:
        shop_filter = _label(shop_filter, '工坊筛选名称')
    if textile_filter != ALL:
        textile_filter = _label(textile_filter, '布类筛选名称')
    view = options.get('view', PIVOT)
    if view not in (PIVOT, BATCHES):
        raise ValueError('请选择「透视汇总」或「展开批次」。')
    meter_limit = _number(options.get('meter_tolerance', 0), '水表累计容差（L）', 1000)

    seen = set()
    selected = []
    for line, row in enumerate(rows, 2):
        batch = _read_batch(row, line)
        key = (batch.day, batch.shop, batch.batch_id)
        if key in seen:
            raise ValueError(f'台账第 {line} 行批次 {batch.batch_id} 重复，请按生产日、工坊和批号去重。')
        seen.add(key)
        if shop_filter in (ALL, batch.shop) and textile_filter in (ALL, batch.textile):
            selected.append(batch)
    if not selected:
        raise ValueError('筛选范围内没有洗涤批次，请按台账填写工坊和布类全名，或用 * 查看全部。')

    groups = defaultdict(lambda: defaultdict(list))
    for batch in selected:
        groups[batch.shop][batch.textile].append(batch)
    total = _aggregate(batch.water for batch in selected)
    shop_scope = '全部工坊' if shop_filter == ALL else shop_filter
    textile_scope = '全部布类' if textile_filter == ALL else textile_filter
    result_rows = [_result_row('范围合计', shop_scope, textile_scope, total)]
    exact = total.residual == ZERO
    for shop, textiles in sorted(groups.items()):
        shop_total = _aggregate(batch.water for batches in textiles.values() for batch in batches)
        exact &= shop_total.residual == ZERO
        result_rows.append(_result_row('工坊小计', shop, textile_scope, shop_total))
        for textile, batches in sorted(textiles.items()):
            cell = _aggregate(batch.water for batch in batches)
            exact &= cell.residual == ZERO and all(batch.water.residual == ZERO for batch in batches)
            result_rows.append(_result_row('工坊 / 布类', shop, textile, cell))
            if view == BATCHES:
                for batch in sorted(batches, key=lambda item: (item.day, item.batch_id)):
                    result_rows.append(_result_row('批次', shop, textile, batch.water,
                                                   day=batch.day, batch_id=batch.batch_id))

    blockers = []
    if total.unknown_batches:
        blockers.append(f'{total.unknown_batches} 个批次缺定额，请补齐对应版本后重算。')
    if not exact:
        blockers.append('耗水分解残差非零，请复核基准和三项差异。')
    if total.meter_absolute_gap > meter_limit:
        blockers.append(f'批次水表累计绝对差超过 {meter_limit:f} L 容差，请核对批次实耗与水表的计量窗口。')

    # 独立水表对账：抄表组读数不带批号，按同工坊与洗程时刻做最大匹配；未配上或
    # 存在争议配对的批次不通过水表对账。reconciliation 为分析时刻冻结的版本快照。
    scope_shops = {shop_filter} if shop_filter != ALL else set(groups)
    reconciliation_view = build_reconciliation_view(
        selected, reconciliation or {}, scope_shops, meter_limit,
    )
    blockers.extend(reconciliation_view['blockers'])
    status = '复核发布已拦截' if blockers else '对账通过，可提交复核'
    metrics = {
        '纳入批次': len(selected),
        '实耗（L）': float(total.actual_liters),
        '加权实耗（L/kg）': _weighted_rate(total.actual_liters, total.actual_kg),
        '已匹配耗水差异（L）': float(total.variance),
        '缺标批次': total.unknown_batches,
        '缺标实耗（L）': float(total.unknown_actual_liters),
        '分解残差（L）': float(total.residual),
        '水表累计绝对差（L）': float(total.meter_absolute_gap),
        '水表累计容差（L）': float(meter_limit),
        '确定配对': reconciliation_view['certain_count'],
        '争议配对': reconciliation_view['disputed_count'],
        '未配读数': reconciliation_view['unmatched_reading_count'],
        '未配批次': reconciliation_view['unmatched_batch_count'],
        '确定配对水表差合计（L）': reconciliation_view['gap_total'],
        '对账状态': status,
    }
    return {
        'headline': f'{shop_scope} / {textile_scope} · {status}',
        'metrics': metrics,
        'rows': result_rows,
        'reconciliation': {
            'version_ids': reconciliation_view['version_ids'],
            'pairings': reconciliation_view['pairings'],
            'missing_shops': reconciliation_view['missing_shops'],
            'certain_count': reconciliation_view['certain_count'],
            'disputed_count': reconciliation_view['disputed_count'],
            'unmatched_reading_count': reconciliation_view['unmatched_reading_count'],
            'unmatched_batch_count': reconciliation_view['unmatched_batch_count'],
            'gap_total': reconciliation_view['gap_total'],
        },
        'notes': [
            '用工坊与布类全名筛选。选「展开批次」可在每个工坊与布类交叉项下查看生产日和洗涤批号。各层均为独立小计。',
            '已匹配计划基准为计划 kg × 计划定额。标准差为计划 kg ×（执行定额 − 计划定额），量与结构差为（实洗 kg − 计划 kg）× 执行定额，效率差为实耗 − 实洗 kg × 执行定额。',
            '各层实耗由已匹配计划基准、三项差异和缺标实耗相加得到。加权实耗用各层总实耗除以实洗总 kg，分母为零时留空。正差表示增加耗水。',
            '定额未知的批次保留重量、实耗和水表读数，并计入缺标实耗。独立水表按同一批次的计量窗口对账，逐批取绝对差再加总。复核门槛适用于本次筛选范围。',
            '抄表读数不带洗涤批号：按同工坊且读数时刻落在洗程窗口内建立候选，求最大基数匹配；仅在所有最大匹配中都出现的配对才记为确定配对，争议配对、未配读数与未配批次均不通过水表对账。',
            '本结果冻结分析时刻各工坊的生效对账版本及配对明细；报告批准后即便随后因迟到读数重算，已批准报告仍沿用当时的配对。',
            *blockers,
        ],
        'chart': {
            'label': '已匹配批次耗水差异', 'unit': 'L',
            'points': [
                {'label': '标准差', 'value': float(total.standard_variance)},
                {'label': '量与结构差', 'value': float(total.volume_mix_variance)},
                {'label': '效率差', 'value': float(total.efficiency_variance)},
            ],
        },
        'can_publish': not blockers,
    }
