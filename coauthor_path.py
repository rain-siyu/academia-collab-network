#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询两个学者之间的合作路径 (co-authorship shortest path) —— 基于 OpenAlex 免费 API。

功能:
  1) 把学者姓名解析成 OpenAlex 作者 ID
  2) 用「双向 BFS」从两端同时扩展合作者网络, 找到最短合作链
  3) 把探索到的合作网络: 可视化成网络图 / 导出 nodes.csv & edges.csv / 计算中心性指标

用法:
  python coauthor_path.py "Yoshua Bengio" "Geoffrey Hinton"
  # 也可以直接传 OpenAlex 作者 ID, 例如 "A5023888391"

依赖:
  pip install requests networkx matplotlib
"""

import sys
import time
import csv
from collections import deque

import requests
import networkx as nx
import matplotlib
matplotlib.use("Agg")  # 无显示环境也能出图
import matplotlib.pyplot as plt

# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------
# OpenAlex「礼貌池」: 填上你的邮箱能拿到更稳定的速率 (10 req/s, 10万/天)。改成你自己的。
MAILTO = "you@example.com"
BASE = "https://api.openalex.org"

# 每个作者最多抓多少篇论文来提取合作者 (越大越全但越慢; 200 是单页上限)
WORKS_PER_AUTHOR = 200
# BFS 安全上限: 最多展开多少个节点, 防止热门作者把网络炸开
MAX_EXPANSIONS = 250
# 路径最大跳数; 超过就认为「太远/未连通」
MAX_DEPTH = 6

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"coauthor-path-demo (mailto:{MAILTO})"})


def _get(url, params=None):
    """带礼貌邮箱和简单重试的 GET。"""
    params = dict(params or {})
    params["mailto"] = MAILTO
    for attempt in range(3):
        r = SESSION.get(url, params=params, timeout=30)
        if r.status_code == 429:          # 被限速, 退避后重试
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# ------------------------------------------------------------------
# 1. 姓名 -> 作者 ID
# ------------------------------------------------------------------
def resolve_author(name_or_id):
    """传入姓名则搜索并返回最匹配的作者; 传入 A 开头的 ID 则直接取详情。"""
    if name_or_id.startswith("A") and name_or_id[1:].isdigit():
        data = _get(f"{BASE}/authors/{name_or_id}")
        return _short_id(data["id"]), data["display_name"]

    data = _get(f"{BASE}/authors", params={"search": name_or_id, "per-page": 5})
    results = data.get("results", [])
    if not results:
        raise ValueError(f"找不到学者: {name_or_id}")

    top = results[0]
    if len(results) > 1:
        print(f"  「{name_or_id}」匹配到多个, 默认选第一个 (按作品量排序):")
        for a in results[:5]:
            inst = (a.get("last_known_institutions") or [{}])
            inst = inst[0].get("display_name", "?") if inst else "?"
            print(f"    - {a['display_name']:<28} 作品 {a['works_count']:>5}  机构 {inst}")
    return _short_id(top["id"]), top["display_name"]


def _short_id(full_url):
    """https://openalex.org/A5023888391 -> A5023888391"""
    return full_url.rstrip("/").split("/")[-1]


# ------------------------------------------------------------------
# 2. 取某作者的合作者 (邻居)
# ------------------------------------------------------------------
_neighbor_cache = {}

def get_coauthors(author_id):
    """返回 {coauthor_id: coauthor_name} —— 该作者论文里出现过的所有合作者。"""
    if author_id in _neighbor_cache:
        return _neighbor_cache[author_id]

    data = _get(f"{BASE}/works", params={
        "filter": f"author.id:{author_id}",
        "per-page": WORKS_PER_AUTHOR,
        "select": "id,authorships",
    })
    neighbors = {}
    for work in data.get("results", []):
        for au in work.get("authorships", []):
            a = au.get("author") or {}
            aid = _short_id(a.get("id", "")) if a.get("id") else None
            if aid and aid != author_id:
                neighbors[aid] = a.get("display_name", aid)
    _neighbor_cache[author_id] = neighbors
    return neighbors


# ------------------------------------------------------------------
# 3. 双向 BFS 找最短合作路径
# ------------------------------------------------------------------
def find_path(src, dst, graph):
    """从 src、dst 两端同时 BFS, 在中间会合。graph 累积所有探索到的边。"""
    if src == dst:
        return [src]

    parent_s = {src: None}   # 从 src 出发的来路
    parent_t = {dst: None}   # 从 dst 出发的来路
    frontier_s, frontier_t = deque([src]), deque([dst])
    depth_s, depth_t = {src: 0}, {dst: 0}
    expansions = 0

    while frontier_s and frontier_t:
        # 总是展开较小的前沿, 效率更高
        if len(frontier_s) <= len(frontier_t):
            frontier, parent_this, parent_other, depth_this, side = \
                frontier_s, parent_s, parent_t, depth_s, "s"
        else:
            frontier, parent_this, parent_other, depth_this, side = \
                frontier_t, parent_t, parent_s, depth_t, "t"

        node = frontier.popleft()
        if depth_this[node] >= MAX_DEPTH:
            continue
        if expansions >= MAX_EXPANSIONS:
            print(f"  达到展开上限 {MAX_EXPANSIONS}, 停止搜索。")
            break
        expansions += 1
        print(f"  展开 #{expansions} [{side}] {node}  (已知节点 {graph.number_of_nodes()})", end="\r")

        for nbr_id, nbr_name in get_coauthors(node).items():
            graph.add_node(nbr_id, name=nbr_name)
            graph.add_edge(node, nbr_id)
            if nbr_id in parent_other:                 # 两端会合 -> 拼出完整路径
                print()
                return _stitch(node, nbr_id, parent_s, parent_t, side)
            if nbr_id not in parent_this:
                parent_this[nbr_id] = node
                depth_this[nbr_id] = depth_this[node] + 1
                frontier.append(nbr_id)
    print()
    return None


def _stitch(node, meet, parent_s, parent_t, side):
    """把会合点两侧的来路拼成 src -> ... -> dst。"""
    if side == "t":
        node, meet = meet, node      # 统一成 node 在 src 侧、meet 在 dst 侧

    left = []
    cur = node
    while cur is not None:
        left.append(cur); cur = parent_s[cur]
    left.reverse()

    right = []
    cur = meet
    while cur is not None:
        right.append(cur); cur = parent_t[cur]

    return left + right


# ------------------------------------------------------------------
# 4. 输出: 可视化 / CSV / 指标
# ------------------------------------------------------------------
def export_csv(graph, prefix="coauthor"):
    with open(f"{prefix}_nodes.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["id", "name", "degree"])
        for n, d in graph.degree():
            w.writerow([n, graph.nodes[n].get("name", n), d])
    with open(f"{prefix}_edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["source", "target"])
        for u, v in graph.edges():
            w.writerow([u, v])
    print(f"  已导出 {prefix}_nodes.csv ({graph.number_of_nodes()} 点) "
          f"和 {prefix}_edges.csv ({graph.number_of_edges()} 边)")


def compute_metrics(graph, path):
    print("\n=== 网络指标 (基于已探索到的合作网络) ===")
    print(f"  节点数 {graph.number_of_nodes()}  边数 {graph.number_of_edges()}")
    deg = nx.degree_centrality(graph)
    # 介数中心性在大图上较慢, 用采样近似
    k = min(graph.number_of_nodes(), 100)
    bet = nx.betweenness_centrality(graph, k=k, seed=42) if graph.number_of_nodes() > 2 else {}
    name = lambda n: graph.nodes[n].get("name", n)

    print("\n  度中心性 Top 5 (合作者最多):")
    for n, c in sorted(deg.items(), key=lambda x: -x[1])[:5]:
        print(f"    {name(n):<30} {c:.3f}")
    if bet:
        print("\n  介数中心性 Top 5 (最关键的「桥梁」):")
        for n, c in sorted(bet.items(), key=lambda x: -x[1])[:5]:
            print(f"    {name(n):<30} {c:.3f}")


def visualize(graph, path, out="coauthor_path.png"):
    name = lambda n: graph.nodes[n].get("name", n)
    path_set = set(path)
    path_edges = set(zip(path, path[1:])) | set(zip(path[1:], path))

    # 图太大时只画路径附近的子图, 否则一团糊
    if graph.number_of_nodes() > 120:
        keep = set(path)
        for n in path:
            keep.update(list(graph.neighbors(n))[:8])
        G = graph.subgraph(keep).copy()
    else:
        G = graph

    pos = nx.spring_layout(G, seed=42, k=0.6)
    plt.figure(figsize=(13, 9))

    node_colors = ["#e63946" if n in path_set else "#a8dadc" for n in G.nodes()]
    node_sizes = [700 if n in path_set else 120 for n in G.nodes()]
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes,
                           edgecolors="white", linewidths=0.5)

    edge_colors, widths = [], []
    for u, v in G.edges():
        if (u, v) in path_edges:
            edge_colors.append("#e63946"); widths.append(3.0)
        else:
            edge_colors.append("#cccccc"); widths.append(0.5)
    nx.draw_networkx_edges(G, pos, edge_color=edge_colors, width=widths, alpha=0.7)

    # 只给路径上的节点标名字, 免得挤
    labels = {n: name(n) for n in G.nodes() if n in path_set}
    nx.draw_networkx_labels(G, pos, labels=labels, font_size=10, font_weight="bold")

    plt.title("Co-authorship path  (red = shortest collaboration chain)", fontsize=14)
    plt.axis("off"); plt.tight_layout()
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"  已保存网络图 {out}")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    if len(sys.argv) != 3:
        print('用法: python coauthor_path.py "学者A" "学者B"')
        sys.exit(1)

    print("1) 解析学者...")
    src_id, src_name = resolve_author(sys.argv[1])
    dst_id, dst_name = resolve_author(sys.argv[2])
    print(f"   起点: {src_name} ({src_id})")
    print(f"   终点: {dst_name} ({dst_id})")

    graph = nx.Graph()
    graph.add_node(src_id, name=src_name)
    graph.add_node(dst_id, name=dst_name)

    print("\n2) 双向 BFS 搜索合作路径...")
    path = find_path(src_id, dst_id, graph)

    if not path:
        print("\n✗ 在限定范围内没找到合作路径 (可能确实很远, 或需调大 MAX_EXPANSIONS)。")
        return

    print(f"\n✓ 找到合作路径, 共 {len(path)-1} 跳:")
    names = [graph.nodes[n].get("name", n) for n in path]
    print("   " + "  →  ".join(names))

    print("\n3) 导出与分析...")
    export_csv(graph)
    compute_metrics(graph, path)
    visualize(graph, path)
    print("\n完成。")


if __name__ == "__main__":
    main()
