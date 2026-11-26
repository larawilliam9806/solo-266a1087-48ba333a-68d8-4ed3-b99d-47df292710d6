"""静态路由表压缩：只改目标设备，返回条目数最少、行为等价的完整替换表。

问题
----
给定全网各路由器的 IPv4 静态路由表、目标路由器，以及目标设备**可直连**的下一跳
名单，求目标设备原表的一份完整替换表：

* 原表没有匹配项的目的地址，替换表仍须无匹配；
* 原表有匹配项的目的地址，替换后仍须从**原来的外部出口**离开；
* 替换后的逐跳路径不得绕回目标设备；
* 条目数最少。

思路（不逐地址穷举）
--------------------
1. 取全网所有路由前缀为边界，把 IPv4 地址空间递归二分成有限个“路由等价类”
   （均匀区段）。区段内任意地址在任意路由器上的最长前缀匹配结果都相同，因此
   每段只需用一个代表地址核对。
2. 对每个原表有匹配的区段，按原表从目标设备逐跳仿真，得到原来的外部出口；
   若原表有匹配项却到不了外部出口（下游无路由 / 环路 / 无直连邻接），立即返回
   该目的地址与逐跳路径，不生成替换表。
3. 对每个区段枚举名单中的候选下一跳：从该候选出发（其余设备仍用原表）若能到达
   同一外部出口、沿途不回到目标设备，且首跳与目标设备直连（若给了拓扑），则
   该候选对该区段合法。
4. 在等价类二叉树做动态规划：状态是“祖先最近一条条目指向哪个下一跳”（或没有），
   每个节点可选一：不再细分、用一条条目覆盖整段；或二分递归。条目会覆盖整段
   地址，因此只有“整段原表都有匹配”的节点才允许放条目。由此得到全局最少条目，
   且原表无匹配的地址永远不会被任何条目覆盖。
5. 生成替换表后，按替换表重新逐跳仿真每个等价类做自校验，保证等价。

复杂度只与全网前缀条目数、名单长度有关，与地址空间大小、主机数无关。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .ipcore import Hop, Prefix, ip_str, parse_prefix, prefix_str
from .models import CompressRequest

MAX_HOPS = 64
INF = 1 << 30

_MASKS = [
    0 if length == 0 else ((0xFFFFFFFF << (32 - length)) & 0xFFFFFFFF)
    for length in range(33)
]


class CompressError(ValueError):
    """入参或规模问题（HTTP 400）。"""


# ---------------------------------------------------------------------------
# 最长前缀匹配：按前缀长度分桶，单次查询 O(32)
# ---------------------------------------------------------------------------


class _LPM:
    __slots__ = ("by_len",)

    def __init__(self, routes: dict[Prefix, str]):
        by_len: list[dict[int, str]] = [{} for _ in range(33)]
        for (net, length), nh in routes.items():
            by_len[length][net] = nh
        self.by_len = by_len

    def lookup(self, addr: int) -> tuple[Prefix | None, str | None]:
        for length in range(32, -1, -1):
            net = addr & _MASKS[length]
            nh = self.by_len[length].get(net)
            if nh is not None:
                return (net, length), nh
        return None, None


# ---------------------------------------------------------------------------
# 沿路由表逐跳转发
# ---------------------------------------------------------------------------


def _trace(
    lpms: dict[str, _LPM],
    nodes: set[str],
    topology: dict[str, list[str]] | None,
    start: str,
    addr: int,
    forbidden: frozenset[str] = frozenset(),
    lpm_cache: dict[str, tuple[Prefix | None, str | None]] | None = None,
) -> tuple[str | None, str | None, list[Hop]]:
    """从 start 出发按各设备当前表逐跳转发 addr。

    返回 (外部出口名, 失效原因, 逐跳记录)。外部出口指不在受管路由器集合中的
    下一跳名称（直连出口/接口）。forbidden 中的节点再次进入即判为环路；
    start 本身不算“再次进入”。
    """
    hops: list[Hop] = []
    if start not in nodes:  # start 本身就是直连外部出口
        return start, None, hops
    seen: set[str] = set()
    cur = start
    for _ in range(MAX_HOPS):
        if cur in seen or (cur in forbidden and cur != start):
            hops.append(Hop(cur, None, None, note=f"回到 {cur}，形成环路"))
            return None, "loop", hops
        seen.add(cur)
        if lpm_cache is not None and cur in lpm_cache:
            matched, nh = lpm_cache[cur]
        else:
            matched, nh = lpms[cur].lookup(addr)
            if lpm_cache is not None:
                lpm_cache[cur] = (matched, nh)
        if nh is None:
            hops.append(Hop(cur, None, None))
            return None, "no_route", hops
        hop = prefix_str(matched) if matched else None
        if nh not in nodes:
            hops.append(Hop(cur, hop, nh))
            return nh, None, hops
        hops.append(Hop(cur, hop, nh))
        # 拓扑可能是局部的：只有确实给出 cur 的邻接表时才校验直连关系
        if topology is not None and cur in topology and nh not in topology[cur]:
            return None, "no_link", hops
        cur = nh
    return None, "loop", hops


# ---------------------------------------------------------------------------
# 等价类二叉树：内部节点 = 分支点，叶子 = 均匀区段
# ---------------------------------------------------------------------------


@dataclass
class _Leaf:
    prefix: Prefix
    matched: bool = False  # 原表在该区段是否有匹配项
    matched_prefix: Prefix | None = None
    original_nh: str | None = None
    original_exit: str | None = None
    valid: tuple[str, ...] = ()


@dataclass
class _Node:
    prefix: Prefix
    children: tuple["_Node", "_Node"] | None = None
    leaf: _Leaf | None = None
    # 本子树内所有区段都“原表有匹配”
    all_matched: bool = False
    # 本子树内各区段合法候选的并集（条目可覆盖整段，更深的区段再用更长前缀改指）
    valid_union: frozenset[str] = frozenset()
    # 本子树内出现过的原表下一跳
    originals: frozenset[str] = frozenset()
    # 继承到的下一跳 -> (最少条目数, 本节点放置的条目或 None)
    memo: dict[str | None, tuple[int, str | None]] = field(default_factory=dict)


def _make_tree(boundaries: list[Prefix]) -> _Node:
    """由边界前缀递归二分出均匀区段。

    区段 = 内部不含任何“更深边界前缀”的极大区段；因此区段内任意地址在任意
    路由器上的最长前缀匹配结果相同。用二分定位子区间，整体 O(n log n)。
    """
    items = sorted(set(boundaries))
    nets = [p[0] for p in items]

    def build(net: int, length: int, lo: int, hi: int) -> _Node:
        # items[lo:hi] 全部落在 (net, length) 内；跳过等于自身的边界
        start = bisect_right(items, (net, length), lo, hi)
        if start >= hi:
            return _Node((net, length), None, _Leaf((net, length)))
        mid = net | (1 << (31 - length))
        k = bisect_left(nets, mid, start, hi)
        left = build(net, length + 1, start, k)
        right = build(mid, length + 1, k, hi)
        return _Node((net, length), (left, right), None)

    if not items:
        return _Node((0, 0), None, _Leaf((0, 0)))
    return build(0, 0, 0, len(items))


def _leaves(node: _Node) -> list[_Leaf]:
    out: list[_Leaf] = []

    def rec(n: _Node) -> None:
        if n.leaf is not None:
            out.append(n.leaf)
            return
        assert n.children is not None
        rec(n.children[0])
        rec(n.children[1])

    rec(node)
    return out


def _all_nodes(node: _Node) -> list[_Node]:
    out = [node]
    if node.children is not None:
        out.extend(_all_nodes(node.children[0]))
        out.extend(_all_nodes(node.children[1]))
    return out


# ---------------------------------------------------------------------------
# 动态规划：最少条目覆盖
# ---------------------------------------------------------------------------


def _annotate(node: _Node) -> None:
    """自底向上汇总 all_matched / valid_union / originals。"""
    if node.leaf is not None:
        leaf = node.leaf
        node.all_matched = leaf.matched
        node.valid_union = frozenset(leaf.valid) if leaf.matched else frozenset()
        node.originals = (
            frozenset({leaf.original_nh}) if leaf.original_nh else frozenset()
        )
        return
    assert node.children is not None
    left, right = node.children
    _annotate(left)
    _annotate(right)
    node.all_matched = left.all_matched and right.all_matched
    node.valid_union = left.valid_union | right.valid_union
    node.originals = left.originals | right.originals


def _ordered(
    hops: frozenset[str], originals: frozenset[str], order: dict[str, int]
) -> list[str]:
    """并列最优时优先沿用原表下一跳，其次按名单顺序。"""
    return sorted(hops, key=lambda h: (h not in originals, order[h]))


def _preferred(
    hops: frozenset[str], originals: frozenset[str], order: dict[str, int]
) -> str:
    return _ordered(hops, originals, order)[0]


def _cost(node: _Node, inh: str | None, order: dict[str, int]) -> int:
    """在“祖先最近一条条目指向 inh”（None 表示没有）前提下的最少条目数。"""
    cached = node.memo.get(inh)
    if cached is not None:
        return cached[0]

    if node.leaf is not None:
        leaf = node.leaf
        if not leaf.matched or (inh is not None and inh in leaf.valid):
            result: tuple[int, str | None] = (0, None)
        elif leaf.valid:
            result = (1, _preferred(frozenset(leaf.valid), node.originals, order))
        else:
            result = (INF, None)
        node.memo[inh] = result
        return result[0]

    assert node.children is not None
    left, right = node.children
    best = _cost(left, inh, order) + _cost(right, inh, order)
    best_choice: str | None = None
    if node.all_matched:
        # 整段原表都有匹配，才可以用一条条目覆盖整段。
        for hop in _ordered(node.valid_union, node.originals, order):
            cand = 1 + _cost(left, hop, order) + _cost(right, hop, order)
            if cand < best:
                best, best_choice = cand, hop
    node.memo[inh] = (best, best_choice)
    return best


def _emit(
    node: _Node,
    inh: str | None,
    order: dict[str, int],
    out: list[tuple[Prefix, str]],
) -> None:
    _, hop = node.memo[inh]
    if hop is not None:
        out.append((node.prefix, hop))
        # 覆盖整段的条目之下，更深的区段仍可能用更长前缀改指
        if node.children is not None:
            _emit(node.children[0], hop, order, out)
            _emit(node.children[1], hop, order, out)
        return
    if node.leaf is not None:
        leaf = node.leaf
        if leaf.matched and leaf.valid and (inh is None or inh not in leaf.valid):
            out.append(
                (node.prefix, _preferred(frozenset(leaf.valid), node.originals, order))
            )
        return
    assert node.children is not None
    _emit(node.children[0], inh, order, out)
    _emit(node.children[1], inh, order, out)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def _build_lpms(req: CompressRequest) -> tuple[list[str], dict[str, _LPM]]:
    nodes = sorted(req.tables)
    lpms: dict[str, _LPM] = {}
    for name in nodes:
        routes = {parse_prefix(p): nh for p, nh in req.tables[name].items()}
        lpms[name] = _LPM(routes)
    return nodes, lpms


def _validate_topology(
    req: CompressRequest, nodes: list[str]
) -> dict[str, list[str]] | None:
    if req.topology is None:
        return None
    node_set = set(nodes)
    topo: dict[str, list[str]] = {}
    for name, nbrs in req.topology.items():
        if name not in node_set:
            raise CompressError(f"topology 中的节点 {name!r} 不在全网路由表设备列表中")
        for nbr in nbrs:
            if nbr not in node_set:
                raise CompressError(
                    f"topology 邻接 {name}->{nbr} 中 {nbr!r} 不是受管路由器；"
                    f"直连外部出口只应作为路由下一跳名称出现"
                )
        topo[name] = list(nbrs)
    return topo


def compress(req: CompressRequest) -> dict:
    nodes, lpms = _build_lpms(req)
    target = req.target
    if target not in lpms:
        raise CompressError(f"目标设备 {target!r} 不在全网路由表设备列表中")
    topo = _validate_topology(req, nodes)

    original = req.tables[target]
    missing = sorted(set(original.values()) - set(req.next_hops))
    if missing:
        raise CompressError(
            "下一跳名单必须包含目标设备原表的全部下一跳，缺少：" + "、".join(missing)
        )

    boundaries = [parse_prefix(text) for name in nodes for text in req.tables[name]]
    tree = _make_tree(boundaries)
    leaves = _leaves(tree)

    target_lpm = lpms[target]
    node_set = set(nodes)
    ec_count = 0

    # 第一遍：每个区段原表的 LPM / 外部出口；原表有匹配却到不了出口则报证据
    for leaf in leaves:
        matched, nh = target_lpm.lookup(leaf.prefix[0])
        if nh is None:
            continue  # 原表无匹配：该区段不得被任何条目覆盖
        ec_count += 1
        leaf.matched = True
        leaf.matched_prefix = matched
        leaf.original_nh = nh
        exit_name, reason, hops = _trace(lpms, node_set, topo, target, leaf.prefix[0])
        if exit_name is None:
            return _unsat(req, leaf, reason or "unknown", hops, ec_count)
        leaf.original_exit = exit_name

    # 第二遍：每个区段的合法候选下一跳（按名单顺序）
    order = {h: i for i, h in enumerate(req.next_hops)}
    target_nbrs = None if topo is None else (set(topo[target]) if target in topo else None)
    for leaf in leaves:
        if not leaf.matched:
            continue
        cache: dict[str, tuple[Prefix | None, str | None]] = {}
        valid: list[str] = []
        for hop in req.next_hops:
            if hop == target:
                continue  # 绕回目标设备，不能作为下一跳
            if (
                target_nbrs is not None
                and hop in node_set
                and hop not in target_nbrs
            ):
                continue  # 拓扑上并不与目标设备直连
            exit_name, _, _ = _trace(
                lpms, node_set, topo, hop, leaf.prefix[0], frozenset({target}), cache
            )
            if exit_name == leaf.original_exit:
                valid.append(hop)
        if not valid:  # 防御性：原表下一跳必在名单内，正常不会为空
            raise CompressError(
                f"下一跳名单无法复现原表行为：目的地址 {ip_str(leaf.prefix[0])}"
                f"（等价类 {prefix_str(leaf.prefix)}）原出口 {leaf.original_exit!r}"
            )
        leaf.valid = tuple(valid)

    _annotate(tree)
    total = _cost(tree, None, order)
    if total >= INF:
        raise CompressError(
            "给定下一跳名单无法复现原表的外部出口行为；请补充可直连的下一跳"
        )

    entries: list[tuple[Prefix, str]] = []
    _emit(tree, None, order, entries)
    entries.sort(key=lambda item: (item[0][1], item[0][0]))
    routes = {prefix_str(p): nh for p, nh in entries}

    _verify(req, lpms, topo, leaves, routes)

    return {
        "status": "ok",
        "target_router": target,
        "original_route_count": len(original),
        "compressed_route_count": len(routes),
        "removed_entries": len(original) - len(routes),
        "routes": routes,
        "equivalence_class_count": ec_count,
        "unmatched_region_count": len(leaves) - ec_count,
        "verified_equivalent": True,
        "note": (
            "替换表只下发到目标设备；按最长前缀匹配，原表无匹配的地址仍无匹配，"
            "其余地址仍从原外部出口离开且路径不回到目标设备。条目数由前缀树上的"
            "最小覆盖动态规划给出，校验基于路由等价类而非逐地址穷举。"
        ),
    }


def _verify(
    req: CompressRequest,
    lpms: dict[str, _LPM],
    topo: dict[str, list[str]] | None,
    leaves: list[_Leaf],
    routes: dict[str, str],
) -> None:
    """用替换表重建目标设备视图，逐等价类重放，确认行为与原表一致。"""
    new_lpms = dict(lpms)
    new_lpms[req.target] = _LPM({parse_prefix(p): nh for p, nh in routes.items()})
    nodes = set(lpms)
    for leaf in leaves:
        addr = leaf.prefix[0]
        matched, _ = new_lpms[req.target].lookup(addr)
        if leaf.original_exit is None:
            if matched is not None:
                raise CompressError(
                    f"内部错误：无匹配区段 {prefix_str(leaf.prefix)} 被替换表覆盖"
                )
            continue
        exit_name, reason, _ = _trace(
            new_lpms, nodes, topo, req.target, addr, frozenset({req.target})
        )
        if exit_name != leaf.original_exit:
            raise CompressError(
                f"内部错误：替换表在区段 {prefix_str(leaf.prefix)} 上出口不符"
                f"（{exit_name or reason} != {leaf.original_exit}）"
            )


def _unsat(
    req: CompressRequest,
    leaf: _Leaf,
    reason: str,
    hops: list[Hop],
    ec_count: int,
) -> dict:
    return {
        "status": "unsat",
        "target_router": req.target,
        "equivalence_class_count": ec_count,
        "message": (
            "目标设备原表对某些目的地址有匹配项，但这些地址无法从外部出口离开；"
            "未生成替换表"
        ),
        "witness": {
            "dst_address": ip_str(leaf.prefix[0]),
            "dst_equivalence_class": prefix_str(leaf.prefix),
            "matched_prefix": prefix_str(leaf.matched_prefix)
            if leaf.matched_prefix
            else None,
            "reason": reason,
            "hop_by_hop": [
                {
                    "node": h.node,
                    "matched_prefix": h.matched_prefix,
                    "next_hop": h.next_hop,
                    **({"note": h.note} if h.note else {}),
                }
                for h in hops
            ],
        },
        "reproduction": [
            f"保持全网其余设备原表不变，只在 {req.target} 上按原表转发目的地址 "
            f"{ip_str(leaf.prefix[0])}（等价类 {prefix_str(leaf.prefix)}）",
            _hops_text(hops),
            f"逐跳失败原因：{_reason_cn(reason)}，无法从任何外部出口离开，故不生成替换表",
        ],
    }


def _hops_text(hops: list[Hop]) -> str:
    if not hops:
        return "未产生任何转发跳"
    parts = []
    for h in hops:
        if h.next_hop is None:
            parts.append(f"{h.node}（无匹配路由）")
        else:
            parts.append(f"{h.node} --[{h.matched_prefix}]--> {h.next_hop}")
    return "逐跳路径：" + "；".join(parts)


def _reason_cn(reason: str) -> str:
    return {
        "loop": "转发环路",
        "no_route": "无匹配路由（断路）",
        "no_link": "下一跳与当前设备无直连邻接",
    }.get(reason, reason)
