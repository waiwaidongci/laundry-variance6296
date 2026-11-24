"""独立水表读数与洗涤批次的二部图对账匹配。

抄表组单独交读数，记录里没有洗涤批号。候选边仅在同工坊、且读数时刻落在批次
洗程窗口（含端点）内建立。匹配目标是在「每条读数最多配一批、每批最多配一条
读数」的约束下尽量配满（最大基数匹配）。

当存在多种配法且都能达到同一最大基数时，只有在**每一种**最大基数匹配中都出现
的配对才被认可（确定配对）；其余可达同样基数的可选配对标为争议，连同未配上的
读数和批次一起不通过水表对账。

判定方式（匹配边在所有最大基数匹配中，当且仅当它不处于以下任一结构）：
1. 交替环：残量有向图（未匹配边 reading -> batch、已匹配边 batch -> reading）中
   该边两端处于同一强连通分量；
2. 以自由读数（未匹配读数）为起点的交替路径：读数端可从自由读数点沿残量图到达；
3. 以自由批次（未匹配批次）为终点的交替路径：批次端可沿残量图到达自由批次点。
满足任一条件的匹配边都能在另一个最大基数匹配中被替换，记为争议边。
"""

from dataclasses import dataclass, field
from datetime import datetime

CERTAIN = '确定配对'
DISPUTED = '争议配对'
UNMATCHED_READING = '未配读数'
UNMATCHED_BATCH = '未配批次'


@dataclass(frozen=True)
class WashBatch:
    """参与对账的洗涤批次洗程窗口。"""

    batch_id: str
    shop: str
    started_at: datetime
    finished_at: datetime
    actual_liters: float


@dataclass(frozen=True)
class MeterReading:
    """抄表组单独提交的水表读数，不含洗涤批号。"""

    reading_id: str
    shop: str
    read_at: datetime
    liters: float
    received_at: datetime | None = None


@dataclass
class Pairing:
    reading_id: str
    batch_id: str
    shop: str
    read_at: datetime
    reading_liters: float
    batch_liters: float
    gap: float
    status: str


@dataclass
class MatchingResult:
    pairings: list[Pairing] = field(default_factory=list)
    unmatched_reading_ids: list[str] = field(default_factory=list)
    unmatched_batch_ids: list[str] = field(default_factory=list)
    # 参与候选但被判争议的边（reading_id -> 可互换的 batch_id 列表）
    disputed_edges: dict[str, list[str]] = field(default_factory=dict)
    candidates: dict[str, list[str]] = field(default_factory=dict)

    @property
    def max_cardinality(self):
        return len(self.pairings)

    @property
    def certain_pairings(self):
        return [pair for pair in self.pairings if pair.status == CERTAIN]

    @property
    def disputed_pairings(self):
        return [pair for pair in self.pairings if pair.status == DISPUTED]

    @property
    def matched_reading_ids(self):
        return {pair.reading_id for pair in self.pairings}

    @property
    def matched_batch_ids(self):
        return {pair.batch_id for pair in self.pairings}


def is_candidate(reading: MeterReading, batch: WashBatch) -> bool:
    """同工坊且读数时刻落在洗程窗口内（闭区间）。"""
    return (
        reading.shop == batch.shop
        and batch.started_at <= reading.read_at <= batch.finished_at
    )


def build_edges(readings, batches):
    """生成候选边：reading_id -> 候选 batch_id 列表（按批号排序，保证结果稳定）。"""
    edges = {}
    for reading in readings:
        neighbors = sorted(
            batch.batch_id
            for batch in batches
            if is_candidate(reading, batch)
        )
        if neighbors:
            edges[reading.reading_id] = neighbors
    return edges


def hopcroft_karp(edges, reading_ids, batch_ids):
    """返回最大基数匹配的一个实例：reading_id -> batch_id。"""
    adjacency = {reading: list(neighbors) for reading, neighbors in edges.items()}
    pair_reading = {}   # batch_id -> reading_id
    pair_batch = {}     # reading_id -> batch_id
    infinity = float('inf')
    dist = {}

    def bfs():
        queue = []
        for reading in reading_ids:
            if reading not in pair_batch:
                dist[reading] = 0
                queue.append(reading)
            else:
                dist[reading] = infinity
        found = False
        head = 0
        while head < len(queue):
            reading = queue[head]
            head += 1
            for batch in adjacency.get(reading, ()):
                owner = pair_reading.get(batch)
                if owner is None:
                    found = True
                elif dist.get(owner, infinity) == infinity:
                    dist[owner] = dist[reading] + 1
                    queue.append(owner)
        return found

    def dfs(reading):
        for batch in adjacency.get(reading, ()):
            owner = pair_reading.get(batch)
            if owner is None or (dist.get(owner, infinity) == dist[reading] + 1 and dfs(owner)):
                pair_batch[reading] = batch
                pair_reading[batch] = reading
                return True
        dist[reading] = infinity
        return False

    matching = 0
    while bfs():
        for reading in reading_ids:
            if reading not in pair_batch and dfs(reading):
                matching += 1
    return pair_batch, matching


def _strong_components(graph, nodes):
    """Tarjan 强连通分量，返回 node -> 分量编号（编号按发现顺序）。"""
    index = {}
    low = {}
    stack = []
    on_stack = set()
    counter = 0
    component = {}

    def visit(start):
        nonlocal counter
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, neighbors = work[-1]
            advanced = False
            for neighbor in neighbors:
                if neighbor not in index:
                    index[neighbor] = low[neighbor] = counter
                    counter += 1
                    stack.append(neighbor)
                    on_stack.add(neighbor)
                    work.append((neighbor, iter(sorted(graph.get(neighbor, ())))))
                    advanced = True
                    break
                if neighbor in on_stack:
                    low[node] = min(low[node], index[neighbor])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component_id = len(component)
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component[member] = component_id
                    if member == node:
                        break

    for node in nodes:
        if node not in index:
            visit(node)
    return component


def classify(edges, reading_ids, batch_ids, pair_batch):
    """区分确定匹配边与可被替换的争议匹配边。

    构造残量有向图：未匹配候选边 reading -> batch，已匹配边 batch -> reading。
    匹配边 (r, b) 可被另一最大匹配替换，当且仅当：r、b 同处一个强连通分量
    （交替环），或 r 可由自由读数点到达（自由读数起的交替路径），或 b 可沿
    残量图到达某个自由批次点（终于自由批次的交替路径）。
    """
    r_prefix = 'R:'
    b_prefix = 'B:'
    graph = {}
    all_nodes = [r_prefix + reading for reading in reading_ids]
    all_nodes += [b_prefix + batch for batch in batch_ids]
    free_readings = []
    for reading in reading_ids:
        graph.setdefault(r_prefix + reading, [])
    for batch in batch_ids:
        graph.setdefault(b_prefix + batch, [])
    for reading, neighbors in edges.items():
        r_node = r_prefix + reading
        for batch in neighbors:
            b_node = b_prefix + batch
            if pair_batch.get(reading) == batch:
                # 已匹配边反向：batch -> reading
                graph.setdefault(b_node, []).append(r_node)
            else:
                # 未匹配边正向：reading -> batch
                graph.setdefault(r_node, []).append(b_node)
    for reading in reading_ids:
        if reading not in pair_batch:
            free_readings.append(r_prefix + reading)

    component = _strong_components(graph, all_nodes)

    # 从所有自由读数点出发，在残量图中正向可达的节点（左侧自由点引出的交替路径）
    reachable_from_free_reading = set()
    for start in free_readings:
        if start in reachable_from_free_reading:
            continue
        queue = [start]
        reachable_from_free_reading.add(start)
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for neighbor in graph.get(node, ()):
                if neighbor not in reachable_from_free_reading:
                    reachable_from_free_reading.add(neighbor)
                    queue.append(neighbor)

    # 能沿残量图到达自由批次点的节点（在反向图上从自由批次做 BFS），
    # 对应以自由批次收尾的交替路径
    reverse_graph = {}
    for source, neighbors in graph.items():
        for neighbor in neighbors:
            reverse_graph.setdefault(neighbor, []).append(source)
    free_batches = [b_prefix + batch for batch in batch_ids if batch not in set(pair_batch.values())]
    can_reach_free_batch = set()
    for start in free_batches:
        if start in can_reach_free_batch:
            continue
        queue = [start]
        can_reach_free_batch.add(start)
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for neighbor in reverse_graph.get(node, ()):
                if neighbor not in can_reach_free_batch:
                    can_reach_free_batch.add(neighbor)
                    queue.append(neighbor)

    certain = {}
    disputed = {}
    for reading, batch in pair_batch.items():
        r_node = r_prefix + reading
        b_node = b_prefix + batch
        replaceable = (
            component[r_node] == component[b_node]
            or r_node in reachable_from_free_reading
            or b_node in can_reach_free_batch
        )
        if replaceable:
            disputed[reading] = batch
        else:
            certain[reading] = batch
    return certain, disputed


def match(readings, batches, tolerance=0.0):
    """对一批读数和批次执行完整对账匹配。

    返回 :class:`MatchingResult`。``tolerance`` 仅用于在配对上预填水表差是否
    超差，匹配本身只由工坊与洗程时刻决定。
    """
    readings = sorted(readings, key=lambda item: item.reading_id)
    batches = sorted(batches, key=lambda item: item.batch_id)
    reading_ids = [item.reading_id for item in readings]
    batch_ids = [item.batch_id for item in batches]
    reading_by_id = {item.reading_id: item for item in readings}
    batch_by_id = {item.batch_id: item for item in batches}

    edges = build_edges(readings, batches)
    pair_batch, cardinality = hopcroft_karp(edges, reading_ids, batch_ids)
    certain, disputed = classify(edges, reading_ids, batch_ids, pair_batch)

    pairings = []
    for reading in readings:
        matched_batch = pair_batch.get(reading.reading_id)
        if matched_batch is None:
            continue
        batch = batch_by_id[matched_batch]
        gap = abs(reading.liters - batch.actual_liters)
        status = CERTAIN if reading.reading_id in certain else DISPUTED
        pairings.append(Pairing(
            reading_id=reading.reading_id,
            batch_id=matched_batch,
            shop=reading.shop,
            read_at=reading.read_at,
            reading_liters=reading.liters,
            batch_liters=batch.actual_liters,
            gap=gap,
            status=status,
        ))

    disputed_edges = {}
    for reading_id, batch_id in disputed.items():
        disputed_edges[reading_id] = sorted(
            candidate for candidate in edges.get(reading_id, [])
        )
        # 该读数在其他最大匹配中可能落到的批次，至少包含当前批次
        if batch_id not in disputed_edges[reading_id]:
            disputed_edges[reading_id].append(batch_id)
            disputed_edges[reading_id].sort()

    unmatched_reading_ids = [
        reading_id for reading_id in reading_ids if reading_id not in pair_batch
    ]
    unmatched_batch_ids = [
        batch_id for batch_id in batch_ids
        if batch_id not in set(pair_batch.values())
    ]
    return MatchingResult(
        pairings=pairings,
        unmatched_reading_ids=unmatched_reading_ids,
        unmatched_batch_ids=unmatched_batch_ids,
        disputed_edges=disputed_edges,
        candidates=edges,
    )
