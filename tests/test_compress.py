"""静态路由表压缩接口测试：行为等价、条目最少、无匹配保持、不可达证据。"""
from __future__ import annotations

import itertools
import random

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.compress import compress
from app.ipcore import parse_prefix, prefix_str
from app.main import app
from app.models import CompressRequest

client = TestClient(app)


def _req(tables, target, hops, topology=None):
    return CompressRequest(
        tables=tables, target=target, next_hops=hops, topology=topology
    )


# ---------------------------------------------------------------------------
# 独立参考实现：按 LPM 逐跳、用于复核结果（不是被测代码）
# ---------------------------------------------------------------------------


def _ref_lpm(routes: dict[str, str], addr: int):
    best = None
    for text, nh in routes.items():
        net, length = parse_prefix(text)
        if (length == 0 or (addr >> (32 - length)) == (net >> (32 - length))) and (
            best is None or length > best[0][1]
        ):
            best = ((net, length), nh)
    return best


def _ref_exit(tables, nodes, topology, start, addr, forbidden=frozenset()):
    """返回外部出口；无路由/环路/无邻接返回 None。"""
    if start not in tables:
        return start
    seen = set()
    cur = start
    for _ in range(64):
        if cur in seen or (cur in forbidden and cur != start):
            return None
        seen.add(cur)
        hit = _ref_lpm(tables[cur], addr)
        if hit is None:
            return None
        nh = hit[1]
        if nh not in nodes:
            return nh
        if topology is not None and cur in topology and nh not in topology[cur]:
            return None
        cur = nh
    return None


def _sample_addresses(prefixes, n_random=6, rng=None):
    """在若干前缀内随机取样（用于复核，不代表穷举）。"""
    rng = rng or random.Random(7)
    addrs = set()
    for net, length in prefixes:
        size = 1 << (32 - length)
        addrs.add(net)
        addrs.add(net + size - 1)
        addrs.add(net + size // 2)
        for _ in range(n_random):
            addrs.add(net + rng.randrange(size))
        if length == 32:
            addrs.add(net)
    return sorted(a for a in addrs if 0 <= a < (1 << 32))


# ---------------------------------------------------------------------------
# 基本可行场景
# ---------------------------------------------------------------------------


def test_aggregates_equivalent_entries_into_shorter_prefix():
    tables = {
        "T": {
            "10.0.0.0/9": "A",
            "10.128.0.0/9": "A",
            "0.0.0.0/0": "A",
        },
        "A": {"0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["A"]))
    assert res["status"] == "ok"
    assert res["routes"] == {"0.0.0.0/0": "A"}
    assert res["compressed_route_count"] == 1
    assert res["verified_equivalent"] is True


def test_keeps_longer_prefix_override():
    # 10.3.0.0/16 原表走 ISP2，其余走 ISP1：最少 2 条
    tables = {
        "T": {
            "10.1.0.0/16": "ISP1",
            "10.2.0.0/16": "ISP1",
            "10.3.0.0/16": "ISP2",
            "192.168.0.0/16": "CORE",
            "0.0.0.0/0": "ISP1",
        },
        "CORE": {"10.0.0.0/8": "ISP1", "0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["ISP1", "ISP2", "CORE"]))
    assert res["status"] == "ok"
    assert res["routes"] == {"0.0.0.0/0": "ISP1", "10.3.0.0/16": "ISP2"}
    assert res["compressed_route_count"] == 2


def test_unmatched_addresses_stay_unmatched():
    # 全网只在 10.0.0.0/8 内有路由，目标原表只覆盖其中一个 /24
    tables = {
        "T": {"10.1.0.0/24": "ISP1"},
        "B": {"10.0.0.0/8": "ISP1"},
    }
    res = compress(_req(tables, "T", ["ISP1", "B"]))
    assert res["status"] == "ok"
    # 不能合并成 10.0.0.0/8 或默认路由，否则原表无匹配的地址会被覆盖
    assert res["routes"] == {"10.1.0.0/24": "ISP1"}
    assert res["unmatched_region_count"] > 0


def test_next_hop_must_be_reachable_via_allowed_list():
    # 原表把 10/8 交给 B；名单里没有 B，也没有能等价复现的候选
    with pytest.raises(ValidationError):
        _req({"T": {"10.0.0.0/8": "B"}}, "T", ["ISP1"])


def test_topology_mismatch_in_original_is_unsat_with_hop_path():
    tables = {
        "T": {"10.0.0.0/8": "B"},
        "B": {"0.0.0.0/0": "ISP1"},
        "C": {"0.0.0.0/0": "ISP1"},
    }
    # 拓扑显示 B 不是 T 的直连邻居：按原表逐跳会命中 no_link
    res = compress(
        _req(tables, "T", ["B", "C"], topology={"T": ["C"], "C": ["T"], "B": []})
    )
    assert res["status"] == "unsat"
    assert res["witness"]["reason"] == "no_link"
    assert [h["node"] for h in res["witness"]["hop_by_hop"]] == ["T"]


def test_topology_excludes_non_neighbor_candidates():
    tables = {
        "T": {"10.0.0.0/8": "A", "10.1.0.0/16": "A"},
        "A": {"0.0.0.0/0": "ISP1"},
        "B": {"0.0.0.0/0": "ISP1"},
    }
    # 不给拓扑时 A、B 都能等价复现；给了拓扑后 B 不直连 T，必须排除。
    # 先给拓扑来验证结果只使用 A
    res = compress(
        _req(tables, "T", ["A", "B"], topology={"T": ["A"], "A": ["T"], "B": []})
    )
    assert res["status"] == "ok"
    assert set(res["routes"].values()) == {"A"}
    # 反事实：去掉拓扑后条目数不会更多，说明拓扑确实没有放宽候选
    loose = compress(_req(tables, "T", ["A", "B"]))
    assert loose["compressed_route_count"] <= res["compressed_route_count"]


# ---------------------------------------------------------------------------
# 原表有匹配却到不了外部出口 => 证据
# ---------------------------------------------------------------------------


def test_unsat_when_original_has_match_but_no_external_exit():
    tables = {
        "T": {"10.0.0.0/8": "CORE"},
        "CORE": {"192.168.0.0/16": "ISP1"},
    }
    res = compress(_req(tables, "T", ["CORE", "ISP1"]))
    assert res["status"] == "unsat"
    w = res["witness"]
    assert w["reason"] == "no_route"
    assert w["dst_equivalence_class"] == "10.0.0.0/8"
    assert w["matched_prefix"] == "10.0.0.0/8"
    assert [h["node"] for h in w["hop_by_hop"]] == ["T", "CORE"]
    assert "routes" not in res


def test_unsat_loop_witness_in_original():
    tables = {
        "T": {"10.0.0.0/8": "B", "0.0.0.0/0": "ISP1"},
        "B": {"10.0.0.0/8": "T", "0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["B", "ISP1"]))
    assert res["status"] == "unsat"
    assert res["witness"]["reason"] == "loop"
    assert [h["node"] for h in res["witness"]["hop_by_hop"]] == ["T", "B", "T"]


def test_replacement_never_loops_back_to_target():
    # 从 B 出发对 10.1/16 会绕回 T；A 是安全候选
    tables = {
        "T": {"10.0.0.0/8": "A", "0.0.0.0/0": "ISP1"},
        "A": {"0.0.0.0/0": "ISP1"},
        "B": {"10.1.0.0/16": "T", "0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["A", "B", "ISP1"]))
    assert res["status"] == "ok"
    assert res["routes"] == {"0.0.0.0/0": "A"}


# ---------------------------------------------------------------------------
# 等价性复核 + 最少条目（与独立穷举对照）
# ---------------------------------------------------------------------------


def _effective_exit(tables, target, replacement, addr, nodes, topology=None):
    effective = dict(tables)
    effective[target] = replacement
    return _ref_exit(effective, nodes, topology, target, addr, frozenset({target}))


def test_result_is_equivalent_on_sampled_addresses():
    tables = {
        "T": {
            "10.1.0.0/16": "ISP1",
            "10.2.0.0/16": "CORE",
            "10.3.0.0/16": "ISP2",
            "172.16.0.0/12": "CORE",
            "192.168.1.0/24": "ISP2",
            "0.0.0.0/0": "ISP1",
        },
        "CORE": {"10.0.0.0/8": "ISP1", "0.0.0.0/0": "ISP1"},
        "EDGE": {"0.0.0.0/0": "ISP2"},
    }
    res = compress(_req(tables, "T", ["ISP1", "ISP2", "CORE", "EDGE"]))
    assert res["status"] == "ok"
    nodes = set(tables)
    prefixes = [(parse_prefix(p),) for p in tables["T"]]
    prefixes = [parse_prefix(p) for p in tables["T"]] + [
        parse_prefix(p) for p in tables["CORE"]
    ]
    for addr in _sample_addresses(prefixes, n_random=10):
        old = _ref_exit(tables, nodes, None, "T", addr)
        new = _effective_exit(tables, "T", res["routes"], addr, nodes)
        assert old == new, f"地址 {addr} 原出口 {old} != 新出口 {new}"


def _brute_force_min_entries(tables, target, hops):
    """小规模穷举：在“可安全使用的候选条目”上枚举子集，返回最小条目数。

    候选条目 = 等价类树上所有“整段原表都有匹配”的节点前缀 × 名单下一跳
    （任何合法替换条目都必然落在这样的节点上）。等价性用独立参考实现逐类核对。
    仅用于极小规模输入，与 DP 相互独立。
    """
    from app.compress import _all_nodes, _annotate, _leaves, _make_tree

    boundaries = [parse_prefix(p) for t in tables.values() for p in t]
    root = _make_tree(boundaries)
    leaves = _leaves(root)
    nodes = set(tables)

    # 标记每个区段是否原表有匹配，用于 _annotate 的最少覆盖判定
    for leaf in leaves:
        hit = _ref_lpm(tables[target], leaf.prefix[0])
        leaf.matched = hit is not None
    _annotate(root)

    samples = []
    for leaf in leaves:
        addr = leaf.prefix[0]
        samples.append((addr, _ref_exit(tables, nodes, None, target, addr)))

    candidates = [
        (prefix_str(node.prefix), hop)
        for node in _all_nodes(root)
        if node.all_matched
        for hop in hops
        if hop != target
    ]
    for k in range(0, len(candidates) + 1):
        for combo in itertools.combinations(candidates, k):
            repl = dict(combo)
            if len(repl) != k:  # 同一前缀被选了两次
                continue
            ok = True
            for addr, old in samples:
                hit = _ref_lpm(repl, addr)
                if old is None:
                    if hit is not None:
                        ok = False
                        break
                elif hit is None:
                    ok = False
                    break
                elif _effective_exit(tables, target, repl, addr, nodes) != old:
                    ok = False
                    break
            if ok:
                return k
    return None


def test_minimal_entry_count_matches_brute_force_small_cases():
    rng = random.Random(20261126)
    checked = 0
    for _ in range(25):
        # 所有路由都在 10.0.0.0/24 内，规模小、候选集合有限，可穷举
        sub_prefixes = rng.sample(
            ["10.0.0.0/26", "10.0.0.64/26", "10.0.0.0/27", "10.0.0.128/25",
             "10.0.0.32/27", "10.0.0.16/28", "10.0.0.0/24"],
            rng.randint(2, 4),
        )
        target_routes = {"10.0.0.0/24": rng.choice(["A", "B", "ISP1"])}
        for p in sub_prefixes:
            target_routes[p] = rng.choice(["A", "B", "ISP1", "ISP2"])
        tables = {
            "T": target_routes,
            "A": {"0.0.0.0/0": "ISP1"},
            "B": {"0.0.0.0/0": "ISP2"},
        }
        hops = ["A", "B", "ISP1", "ISP2"]
        res = compress(_req(tables, "T", hops))
        if res["status"] != "ok":
            continue
        expected = _brute_force_min_entries(tables, "T", hops)
        assert expected is not None
        assert res["compressed_route_count"] == expected, (
            f"DP 得到 {res['compressed_route_count']}，穷举最少 {expected}，"
            f"原表 {target_routes}，替换表 {res['routes']}"
        )
        checked += 1
    assert checked >= 20


# ---------------------------------------------------------------------------
# 参数与边界
# ---------------------------------------------------------------------------


def test_target_in_next_hops_is_never_used():
    tables = {
        "T": {"10.0.0.0/8": "A"},
        "A": {"0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["T", "A"]))
    assert res["status"] == "ok"
    assert "T" not in res["routes"].values()


def test_original_next_hop_is_always_a_valid_candidate():
    # 原表下一跳一定在名单里，且原表路径本身有效，因此它必然能复现原出口
    tables = {
        "T": {"10.0.0.0/8": "ISP2"},
        "A": {"0.0.0.0/0": "ISP1"},
    }
    res = compress(_req(tables, "T", ["ISP2", "A"]))
    assert res["status"] == "ok"
    assert res["routes"] == {"10.0.0.0/8": "ISP2"}


def test_original_next_hop_is_preferred_on_ties():
    tables = {
        "T": {"10.0.0.0/8": "ISP1"},
        "A": {"0.0.0.0/0": "ISP1"},
    }
    # A 和 ISP1 都能到 ISP1；并列最优时沿用原表的 ISP1
    res = compress(_req(tables, "T", ["A", "ISP1"]))
    assert res["status"] == "ok"
    assert res["routes"] == {"10.0.0.0/8": "ISP1"}


def test_topology_unknown_node_is_400():
    from app.compress import CompressError

    with pytest.raises(CompressError):
        compress(
            _req(
                {"T": {"0.0.0.0/0": "ISP1"}},
                "T",
                ["ISP1"],
                topology={"GHOST": ["T"]},
            )
        )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_compress_ok_and_unsat():
    payload = {
        "tables": {
            "T": {"10.1.0.0/16": "ISP1", "10.2.0.0/16": "ISP1", "0.0.0.0/0": "ISP1"},
            "CORE": {"0.0.0.0/0": "ISP1"},
        },
        "target": "T",
        "next_hops": ["ISP1", "CORE"],
    }
    r = client.post("/compress", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["routes"] == {"0.0.0.0/0": "ISP1"}
    assert body["compressed_route_count"] <= body["original_route_count"]

    payload["tables"]["T"] = {"10.0.0.0/8": "CORE"}
    payload["tables"]["CORE"] = {"192.168.0.0/16": "ISP1"}
    payload["next_hops"] = ["CORE", "ISP1"]
    r2 = client.post("/compress", json=payload)
    assert r2.status_code == 200
    assert r2.json()["status"] == "unsat"
    assert r2.json()["witness"]["reason"] == "no_route"


def test_api_compress_rejects_missing_next_hop_and_bad_prefix():
    r = client.post(
        "/compress",
        json={
            "tables": {"T": {"10.0.0.0/8": "B"}},
            "target": "T",
            "next_hops": ["ISP1"],
        },
    )
    assert r.status_code == 422

    r2 = client.post(
        "/compress",
        json={
            "tables": {"T": {"10.0.0.1/8": "ISP1"}},
            "target": "T",
            "next_hops": ["ISP1"],
        },
    )
    assert r2.status_code == 422


def test_api_compress_unknown_target_is_422():
    r = client.post(
        "/compress",
        json={
            "tables": {"T": {"0.0.0.0/0": "ISP1"}},
            "target": "GHOST",
            "next_hops": ["ISP1"],
        },
    )
    assert r.status_code == 422


def test_api_compress_duplicate_hops_rejected():
    r = client.post(
        "/compress",
        json={
            "tables": {"T": {"0.0.0.0/0": "ISP1"}},
            "target": "T",
            "next_hops": ["ISP1", "ISP1"],
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 规模：不逐地址穷举
# ---------------------------------------------------------------------------


def test_large_address_space_does_not_change_runtime():
    # 原表只有 6 条，但覆盖 0.0.0.0/0；等价类数量只与前缀数有关
    tables = {
        "T": {
            "0.0.0.0/0": "ISP1",
            "10.0.0.0/8": "CORE",
            "10.1.0.0/16": "ISP2",
            "10.1.2.0/24": "CORE",
            "172.16.0.0/12": "ISP2",
            "192.168.0.0/16": "CORE",
        },
        "CORE": {"0.0.0.0/0": "ISP1", "172.16.0.0/12": "ISP2"},
    }
    import time

    t0 = time.time()
    res = compress(
        _req(tables, "T", ["ISP1", "ISP2", "CORE"])
    )
    elapsed = time.time() - t0
    assert res["status"] == "ok"
    assert elapsed < 2.0
    assert res["equivalence_class_count"] <= 2 * (len(tables["T"]) + len(tables["CORE"])) * 32
