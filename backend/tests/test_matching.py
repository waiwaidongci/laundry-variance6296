"""水表读数对账匹配算法的单元测试（仅依赖标准库）。"""

import itertools
import unittest
from datetime import datetime

from src.services.matching import (
    CERTAIN,
    DISPUTED,
    MeterReading,
    WashBatch,
    build_edges,
    classify,
    hopcroft_karp,
    is_candidate,
    match,
)


def dt(value):
    return datetime.fromisoformat(value)


def batch(batch_id, shop, start, finish, liters=1000.0):
    return WashBatch(batch_id, shop, dt(start), dt(finish), liters)


def reading(reading_id, shop, moment, liters=1000.0, received=None):
    return MeterReading(reading_id, shop, dt(moment), liters,
                        dt(received) if received else None)


class CandidateTests(unittest.TestCase):
    def test_same_shop_and_window_inclusive(self):
        b = batch('B1', '东港工坊', '2026-11-16T08:00', '2026-11-16T10:00')
        self.assertTrue(is_candidate(reading('R1', '东港工坊', '2026-11-16T08:00'), b))
        self.assertTrue(is_candidate(reading('R2', '东港工坊', '2026-11-16T10:00'), b))
        self.assertTrue(is_candidate(reading('R3', '东港工坊', '2026-11-16T09:30'), b))

    def test_other_shop_rejected(self):
        b = batch('B1', '东港工坊', '2026-11-16T08:00', '2026-11-16T10:00')
        self.assertFalse(is_candidate(reading('R1', '白榆工坊', '2026-11-16T09:00'), b))

    def test_outside_window_rejected(self):
        b = batch('B1', '东港工坊', '2026-11-16T08:00', '2026-11-16T10:00')
        self.assertFalse(is_candidate(reading('R1', '东港工坊', '2026-11-16T07:59'), b))
        self.assertFalse(is_candidate(reading('R2', '东港工坊', '2026-11-16T10:01'), b))


class MatchingTests(unittest.TestCase):
    def test_one_to_one_certain(self):
        batches = [batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T10:00')]
        readings = [reading('R1', 'S', '2026-11-16T09:00')]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 1)
        self.assertEqual(len(result.certain_pairings), 1)
        self.assertEqual(result.certain_pairings[0].batch_id, 'B1')
        self.assertEqual(result.certain_pairings[0].status, CERTAIN)
        self.assertEqual(result.unmatched_reading_ids, [])
        self.assertEqual(result.unmatched_batch_ids, [])

    def test_unmatched_reading_and_batch(self):
        batches = [batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T10:00')]
        readings = [
            reading('R1', 'S', '2026-11-16T09:00'),
            reading('R2', 'S', '2026-11-16T12:00'),  # 不在任何窗口
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 1)
        self.assertEqual(result.unmatched_reading_ids, ['R2'])
        self.assertEqual(result.unmatched_batch_ids, [])

        result2 = match([reading('R9', 'S', '2026-11-16T12:00')], batches)
        self.assertEqual(result2.max_cardinality, 0)
        self.assertEqual(result2.unmatched_batch_ids, ['B1'])
        self.assertEqual(result2.unmatched_reading_ids, ['R9'])

    def test_square_cycle_both_edges_disputed(self):
        # 两个读数都落在两个批次的窗口内：两种最大配法，配对全部有争议
        batches = [
            batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B2', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
        ]
        readings = [
            reading('R1', 'S', '2026-11-16T09:00'),
            reading('R2', 'S', '2026-11-16T10:00'),
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 2)
        self.assertEqual(len(result.certain_pairings), 0)
        self.assertEqual(len(result.disputed_pairings), 2)
        self.assertEqual(set(result.disputed_edges), {'R1', 'R2'})
        for reading_id in ('R1', 'R2'):
            self.assertEqual(result.disputed_edges[reading_id], ['B1', 'B2'])

    def test_chain_alternating_path_disputed(self):
        # R1 只可配 B1；R2 可配 B1、B2。最大基数 2 时 R1-B1、R2-B2 均确定。
        batches = [
            batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T10:00'),
            batch('B2', 'S', '2026-11-16T10:00', '2026-11-16T12:00'),
        ]
        readings = [
            reading('R1', 'S', '2026-11-16T09:00'),  # 仅 B1
            reading('R2', 'S', '2026-11-16T10:00'),  # B1、B2（端点闭区间）
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 2)
        self.assertEqual({p.reading_id: p.batch_id for p in result.certain_pairings},
                         {'R1': 'B1', 'R2': 'B2'})

    def test_alternating_path_from_free_reading(self):
        # R3 自由且与 B1 相连；R1-B1、R2-B2 已配满，R3 引发替换链：
        # R3->B1->R1->B2，因此 R1-B1 与 R2-B2 都是争议边。
        batches = [
            batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B2', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
        ]
        readings = [
            reading('R1', 'S', '2026-11-16T09:00'),
            reading('R2', 'S', '2026-11-16T09:30'),
            reading('R3', 'S', '2026-11-16T10:00'),
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 2)
        self.assertEqual(len(result.certain_pairings), 0)
        self.assertEqual(len(result.disputed_pairings), 2)
        self.assertEqual(sorted(result.unmatched_reading_ids), ['R3'])

    def test_partial_certain_in_disputed_graph(self):
        # B3 只被 R3 命中，R3 同时能配 B1：R3-B3 仍为确定（去掉它无法配满 B3）
        batches = [
            batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B2', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B3', 'S', '2026-11-16T13:00', '2026-11-16T15:00'),
        ]
        readings = [
            reading('R1', 'S', '2026-11-16T09:00'),
            reading('R2', 'S', '2026-11-16T10:00'),
            reading('R3', 'S', '2026-11-16T11:00'),  # B1、B2
            reading('R4', 'S', '2026-11-16T14:00'),  # 仅 B3
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 3)
        certain = {p.reading_id: p.batch_id for p in result.certain_pairings}
        self.assertEqual(certain, {'R4': 'B3'})
        disputed_readings = {p.reading_id for p in result.disputed_pairings}
        self.assertEqual(disputed_readings, {'R1', 'R2'})

    def test_alternating_path_ending_at_free_batch(self):
        # R1 同时落在 B1、B2 窗口，B2 没有别的读数：最大基数 1，但 R1 配 B1
        # 或 B2 都能达到，配对必须判争议而非确定。
        batches = [
            batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B2', 'S', '2026-11-16T08:00', '2026-11-16T12:00'),
        ]
        readings = [reading('R1', 'S', '2026-11-16T09:00')]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 1)
        self.assertEqual(len(result.certain_pairings), 0)
        self.assertEqual(len(result.disputed_pairings), 1)
        self.assertEqual(len(result.unmatched_batch_ids), 1)
        self.assertEqual(result.unmatched_reading_ids, [])

    def test_shops_are_independent_partitions(self):
        # 跨工坊绝不相连：两个工坊各自的 R 与 B 只能内部配对
        batches = [
            batch('B1', '东港工坊', '2026-11-16T08:00', '2026-11-16T12:00'),
            batch('B2', '白榆工坊', '2026-11-16T08:00', '2026-11-16T12:00'),
        ]
        readings = [
            reading('R1', '东港工坊', '2026-11-16T09:00'),
            reading('R2', '白榆工坊', '2026-11-16T09:00'),
        ]
        result = match(readings, batches)
        self.assertEqual(result.max_cardinality, 2)
        self.assertEqual(len(result.certain_pairings), 2)
        self.assertEqual(result.candidates['R1'], ['B1'])
        self.assertEqual(result.candidates['R2'], ['B2'])

    def test_gap_recorded_on_pairing(self):
        batches = [batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T10:00', liters=1713.4)]
        readings = [reading('R1', 'S', '2026-11-16T09:00', liters=1710.0)]
        result = match(readings, batches)
        pairing = result.certain_pairings[0]
        self.assertAlmostEqual(pairing.gap, 3.4, places=6)
        self.assertEqual(pairing.batch_liters, 1713.4)
        self.assertEqual(pairing.reading_liters, 1710.0)

    def test_empty_inputs(self):
        self.assertEqual(match([], []).max_cardinality, 0)
        batches = [batch('B1', 'S', '2026-11-16T08:00', '2026-11-16T10:00')]
        result = match([], batches)
        self.assertEqual(result.unmatched_batch_ids, ['B1'])


def brute_force_max_matchings(edges, reading_ids):
    """枚举所有最大基数匹配，返回匹配集合（元素为 (reading, batch)）。"""
    options = {reading: edges.get(reading, []) for reading in reading_ids}
    best = []
    best_size = 0
    keys = list(options)

    def walk(index, chosen, used_batches):
        nonlocal best, best_size
        if len(chosen) + len(keys) - index < best_size:
            return
        if index == len(keys):
            size = len(chosen)
            if size > best_size:
                best_size = size
                best = [set(chosen)]
            elif size == best_size:
                best.append(set(chosen))
            return
        reading = keys[index]
        walk(index + 1, chosen, used_batches)
        for candidate in options[reading]:
            if candidate not in used_batches:
                used_batches.add(candidate)
                chosen.append((reading, candidate))
                walk(index + 1, chosen, used_batches)
                chosen.pop()
                used_batches.discard(candidate)

    walk(0, [], set())
    return best, best_size


class BruteForceCrossCheck(unittest.TestCase):
    """用穷举所有最大匹配的方式交叉验证 SCC 判定（多组随机小图）。"""

    def _cross_check(self, edges, reading_ids, batch_ids):
        pair_batch, hk_size = hopcroft_karp(edges, reading_ids, batch_ids)
        all_max, brute_size = brute_force_max_matchings(edges, reading_ids)
        self.assertEqual(hk_size, brute_size)
        intersection = set.intersection(*all_max) if all_max else set()
        certain, disputed = classify(edges, reading_ids, batch_ids, pair_batch)
        certain_pairs = set(certain.items())
        disputed_pairs = set(disputed.items())
        self.assertEqual(certain_pairs | disputed_pairs, set(pair_batch.items()))
        self.assertEqual(certain_pairs, intersection)
        # 每条争议边至少能在另一个最大匹配里找到替换
        for reading_id, batch_id in disputed_pairs:
            self.assertTrue(
                any((reading_id, batch_id) not in one for one in all_max),
                f'争议边 {(reading_id, batch_id)} 实际出现在所有最大匹配中',
            )

    def test_all_small_graphs(self):
        # 3 个读数 × 3 个批次的全部候选图（2^9 = 512 张）
        reading_ids = ['R1', 'R2', 'R3']
        batch_ids = ['B1', 'B2', 'B3']
        for mask in range(1 << 9):
            edges = {}
            for r_index, reading in enumerate(reading_ids):
                neighbors = [batch_ids[c] for c in range(3) if mask & (1 << (r_index * 3 + c))]
                if neighbors:
                    edges[reading] = neighbors
            self._cross_check(edges, reading_ids, batch_ids)

    def test_asymmetric_graphs(self):
        reading_ids = ['R1', 'R2', 'R3', 'R4']
        batch_ids = ['B1', 'B2']
        for mask in range(1 << 8):
            edges = {}
            for r_index, reading in enumerate(reading_ids):
                neighbors = [batch_ids[c] for c in range(2) if mask & (1 << (r_index * 2 + c))]
                if neighbors:
                    edges[reading] = neighbors
            self._cross_check(edges, reading_ids, batch_ids)


if __name__ == '__main__':
    unittest.main()
