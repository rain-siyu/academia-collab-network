#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive co-authorship network explorer.
Usage: python app.py
Then open http://localhost:5001 in your browser.
"""

import time
from collections import defaultdict
import requests
from flask import Flask, jsonify, request, send_from_directory

import os
MAILTO = os.environ.get("MAILTO", "you@example.com")
BASE = "https://api.openalex.org"
WORKS_PER_AUTHOR = 200

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"coauthor-explorer (mailto:{MAILTO})"})

app = Flask(__name__, static_folder="static")

# Cache: author_id -> {coauthor_id: {name, count, works: [...]}}
_neighbor_cache = {}


def _get(url, params=None):
    params = dict(params or {})
    params["mailto"] = MAILTO
    for attempt in range(3):
        r = SESSION.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _short_id(full_url):
    return full_url.rstrip("/").split("/")[-1] if full_url else ""


def _get_institution(author_data):
    insts = author_data.get("last_known_institutions") or []
    return insts[0].get("display_name", "") if insts else ""


# ------------------------------------------------------------------
# Author resolution
# ------------------------------------------------------------------
def resolve_author(name_or_id):
    if name_or_id.startswith("A") and name_or_id[1:].isdigit():
        data = _get(f"{BASE}/authors/{name_or_id}")
        return {
            "id": _short_id(data["id"]),
            "name": data["display_name"],
            "works_count": data.get("works_count", 0),
            "institution": _get_institution(data),
        }, []
    data = _get(f"{BASE}/authors", params={"search": name_or_id, "per-page": 5})
    results = data.get("results", [])
    if not results:
        raise ValueError(f"Author not found: {name_or_id}")
    candidates = [{
        "id": _short_id(a["id"]),
        "name": a["display_name"],
        "works_count": a.get("works_count", 0),
        "institution": _get_institution(a),
    } for a in results[:5]]
    return candidates[0], candidates


# ------------------------------------------------------------------
# Coauthor fetching — stores rich per-work data
# ------------------------------------------------------------------
def get_coauthors_rich(author_id):
    """
    Returns {coauthor_id: {name, count, works: [{id, title, year, topics,
             my_position, my_corresponding, their_position, their_corresponding}]}}
    """
    if author_id in _neighbor_cache:
        return _neighbor_cache[author_id]

    data = _get(f"{BASE}/works", params={
        "filter": f"author.id:{author_id}",
        "per-page": WORKS_PER_AUTHOR,
        "select": "id,title,publication_year,authorships,topics",
    })

    coauthors = {}  # aid -> {name, count, works:[]}

    for work in data.get("results", []):
        work_id  = _short_id(work.get("id", ""))
        title    = work.get("title") or ""
        year     = work.get("publication_year")
        topics   = [t.get("display_name", "") for t in (work.get("topics") or [])[:3]]

        # Find this author's own position/corresponding in this work
        authorships = work.get("authorships", [])
        my_pos  = None
        my_corr = False
        for au in authorships:
            a = au.get("author") or {}
            if _short_id(a.get("id", "")) == author_id:
                my_pos  = au.get("author_position", "middle")
                my_corr = bool(au.get("is_corresponding"))
                break

        for au in authorships:
            a = au.get("author") or {}
            aid = _short_id(a.get("id", "")) if a.get("id") else None
            if not aid or aid == author_id:
                continue
            if aid not in coauthors:
                coauthors[aid] = {"name": a.get("display_name", aid), "count": 0, "works": []}
            coauthors[aid]["count"] += 1
            coauthors[aid]["works"].append({
                "id":                 work_id,
                "title":              title,
                "year":               year,
                "topics":             topics,
                "my_position":        my_pos,
                "my_corresponding":   my_corr,
                "their_position":     au.get("author_position", "middle"),
                "their_corresponding": bool(au.get("is_corresponding")),
            })

    _neighbor_cache[author_id] = coauthors
    return coauthors


# ------------------------------------------------------------------
# Author profile (institution + location)
# ------------------------------------------------------------------
_profile_cache = {}

def get_author_profile(author_id):
    if author_id in _profile_cache:
        return _profile_cache[author_id]
    try:
        data = _get(f"{BASE}/authors/{author_id}")
    except Exception:
        return {}

    insts = data.get("last_known_institutions") or []
    institutions = []
    for inst in insts:
        geo = inst.get("geo") or {}
        # If geo is missing, fetch institution detail for city info
        if not geo and inst.get("id"):
            try:
                inst_id = _short_id(inst["id"])   # e.g. I200769079
                inst_detail = _get(f"{BASE}/institutions/{inst_id}")
                geo = inst_detail.get("geo") or {}
            except Exception:
                geo = {}
        entry = {
            "name":    inst.get("display_name", ""),
            "country": geo.get("country", "") or inst.get("country_code", ""),
            "city":    geo.get("city", ""),
            "region":  geo.get("region", ""),
            "type":    inst.get("type", ""),
        }
        institutions.append(entry)

    profile = {
        "name":         data.get("display_name", ""),
        "works_count":  data.get("works_count", 0),
        "cited_by_count": data.get("cited_by_count", 0),
        "h_index":      (data.get("summary_stats") or {}).get("h_index", None),
        "institutions": institutions,
        "orcid":        data.get("orcid", ""),
    }
    _profile_cache[author_id] = profile
    return profile


# ------------------------------------------------------------------
# Collaboration summary between two authors
# ------------------------------------------------------------------
def collaboration_summary(id_a, id_b, name_a, name_b):
    rich_a = get_coauthors_rich(id_a)
    if id_b not in rich_a:
        # Try from B's side
        rich_b = get_coauthors_rich(id_b)
        if id_a not in rich_b:
            return None
        works = rich_b[id_a]["works"]
        # Swap perspective: their_ = A, my_ = B
        works = [{
            **w,
            "my_position":        w["their_position"],
            "my_corresponding":   w["their_corresponding"],
            "their_position":     w["my_position"],
            "their_corresponding": w["my_corresponding"],
        } for w in works]
    else:
        works = rich_a[id_b]["works"]

    if not works:
        return None

    total = len(works)

    # --- Author roles ---
    def role_label(positions, correspondents):
        first = positions.count("first")
        last  = positions.count("last")
        corr  = sum(correspondents)
        parts = []
        if first:  parts.append(f"first author {first}x")
        if last:   parts.append(f"last author {last}x")
        if corr:   parts.append(f"corresponding {corr}x")
        mid = len(positions) - first - last
        if mid:    parts.append(f"middle author {mid}x")
        return ", ".join(parts) if parts else "various"

    a_positions  = [w["my_position"]    for w in works]
    a_corrs      = [w["my_corresponding"] for w in works]
    b_positions  = [w["their_position"] for w in works]
    b_corrs      = [w["their_corresponding"] for w in works]

    # --- Topics ---
    topic_counts = defaultdict(int)
    for w in works:
        for t in w["topics"]:
            if t:
                topic_counts[t] += 1
    top_topics = sorted(topic_counts.items(), key=lambda x: -x[1])[:6]

    # --- Year distribution ---
    year_counts = defaultdict(int)
    for w in works:
        if w["year"]:
            year_counts[int(w["year"])] += 1

    # --- Recent 3-year trend ---
    current_year = 2026
    recent = {y: year_counts.get(y, 0) for y in range(current_year - 2, current_year + 1)}

    # --- Recent papers list ---
    sorted_works = sorted(works, key=lambda w: w["year"] or 0, reverse=True)
    recent_papers = [{
        "title": w["title"],
        "year":  w["year"],
        "topics": w["topics"],
    } for w in sorted_works[:8]]

    # --- Author profiles (institution + location) ---
    profile_a = get_author_profile(id_a)
    profile_b = get_author_profile(id_b)

    return {
        "total":        total,
        "name_a":       name_a,
        "name_b":       name_b,
        "role_a":       role_label(a_positions, a_corrs),
        "role_b":       role_label(b_positions, b_corrs),
        "profile_a":    profile_a,
        "profile_b":    profile_b,
        "top_topics":   [{"topic": t, "count": c} for t, c in top_topics],
        "year_counts":  {str(k): v for k, v in sorted(year_counts.items())},
        "recent_trend": {str(k): v for k, v in recent.items()},
        "recent_papers": recent_papers,
    }


# ------------------------------------------------------------------
# Flask API
# ------------------------------------------------------------------

# Global error handlers — always return JSON, never HTML
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": f"Bad request: {e}"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"Server error: {e}"}), 500

@app.errorhandler(Exception)
def unhandled(e):
    return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    try:
        data = request.get_json(force=True, silent=True) or {}
        names = data.get("names", [])
        results = []
        for name in names:
            try:
                top, candidates = resolve_author(name)
                results.append({"query": name, "author": top, "candidates": candidates})
            except Exception as e:
                import traceback
                traceback.print_exc()
                results.append({"query": name, "error": f"{type(e).__name__}: {e}"})
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/coauthors/<author_id>")
def api_coauthors(author_id):
    try:
        limit = int(request.args.get("limit", 30))
        rich = get_coauthors_rich(author_id)
        sorted_items = sorted(rich.items(), key=lambda x: -x[1]["count"])[:limit]
        nodes = [{"id": aid, "name": info["name"], "count": info["count"]}
                 for aid, info in sorted_items]
        edges = [{"source": author_id, "target": aid, "count": info["count"]}
                 for aid, info in sorted_items]
        return jsonify({"nodes": nodes, "edges": edges})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/collaboration/<id_a>/<id_b>")
def api_collaboration(id_a, id_b):
    try:
        name_a = request.args.get("na", id_a)
        name_b = request.args.get("nb", id_b)
        summary = collaboration_summary(id_a, id_b, name_a, name_b)
        if not summary:
            return jsonify({"error": "No shared works found"}), 404
        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mutual_coauthors", methods=["POST"])
def api_mutual_coauthors():
    try:
        data = request.get_json(force=True, silent=True) or {}
        author_list = data.get("authors", [])

        nodes = [{"id": a["id"], "name": a["name"], "group": "seed",
                  "institution": a.get("institution", ""),
                  "works_count": a.get("works_count", 0)}
                 for a in author_list]
        edges = []
        seen  = set()

        for a in author_list:
            aid = a["id"]
            try:
                rich = get_coauthors_rich(aid)
            except Exception:
                continue
            for b in author_list:
                bid = b["id"]
                if aid == bid:
                    continue
                key = tuple(sorted([aid, bid]))
                if key in seen:
                    continue
                if bid in rich:
                    seen.add(key)
                    edges.append({
                        "source": aid,
                        "target": bid,
                        "count":  rich[bid]["count"],
                    })

        return jsonify({"nodes": nodes, "edges": edges})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"Starting server at http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
