"""
Graph endpoints.

GET  /api/graph/nodes?group_id=&type=&label=   → search nodes
GET  /api/graph/node/{node_id}                 → get node
GET  /api/graph/expand/{node_id}?depth=        → BFS expand
GET  /api/graph/doc_states?group_id=           → per-doc pipeline states
POST /api/graph/node                           → create node (admin)
POST /api/graph/edge                           → create edge (admin)
DELETE /api/graph/group/{group_id}             → wipe group graph (admin)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from local_ai_law.config import load_edition_config, log
from local_ai_law.models import GraphNodeIn, GraphEdgeIn
from local_ai_law.routes.auth import get_current_username, require_auth, require_admin
from local_ai_law.services.audit import audit_log
from local_ai_law.services.graph import (
    find_nodes_by_label, get_node, expand_node,
    upsert_node, upsert_edge, delete_group_graph, list_doc_states,
    _conn as _graph_conn,
)

router = APIRouter(prefix="/api/graph", tags=["graph"])


def _require_graph():
    cfg = load_edition_config()
    modules = cfg.get("enabled_modules", [])
    if modules and "graph" not in modules:
        raise HTTPException(403, "Graph module is not enabled for this edition")


@router.get("/nodes")
async def search_nodes(
    group_id: str = Query(...),
    node_type: str = Query(""),
    label: str = Query(""),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — search graph nodes in a group by label substring and/or node type."""
    _require_graph()
    nodes = find_nodes_by_label(username, group_id, label=label, node_type=node_type)
    return {"nodes": nodes, "count": len(nodes)}


@router.get("/node/{node_id}")
async def get_node_endpoint(
    node_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — one graph node by id, or 404."""
    _require_graph()
    node = get_node(username, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return node


@router.get("/expand/{node_id}")
async def expand_node_endpoint(
    node_id: str,
    depth: int = Query(1, ge=1, le=3),
    max_nodes: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — BFS-expand a node up to `depth` hops (capped by `max_nodes`)."""
    _require_graph()
    node = get_node(username, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    related = expand_node(username, node_id, depth=depth, max_nodes=max_nodes)
    return {"root": node, "related": related, "count": len(related)}


@router.get("/visualize")
async def visualize_graph(
    group_id: str = Query(...),
    depth: int = Query(2, ge=1, le=3),
    node_type: str = Query(""),
    doc: str = Query(""),
    min_connections: int = Query(2, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """Return nodes + edges for D3 concept map rendering."""
    _require_graph()
    import json as _json

    c = _graph_conn(username)

    base = "SELECT id, type, label, source_path, metadata FROM nodes WHERE group_id=?"

    if node_type:
        q = base + " AND type=?"
        params: list = [group_id, node_type]
        if doc:
            q += " AND source_path LIKE ?"
            params.append(f"%{doc}%")
        q += " LIMIT ?"
        params.append(limit)
        node_rows = c.execute(q, params).fetchall()
    else:
        if doc:
            # All concepts linked to a specific document via FOUND_IN + that document
            doc_concept_q = """
                SELECT DISTINCT n.id, n.type, n.label, n.source_path, n.metadata
                FROM nodes n
                JOIN edges e ON e.source_id = n.id
                JOIN nodes d ON d.id = e.target_id AND d.source_path LIKE ? AND d.group_id = ?
                WHERE n.group_id = ? AND n.type = 'Concept' AND e.relation = 'FOUND_IN'
                ORDER BY n.label
            """
            concept_rows = c.execute(doc_concept_q, [f"%{doc}%", group_id, group_id]).fetchall()
            hub_rows = c.execute(
                base + " AND type='Document'", [group_id]
            ).fetchall()
            node_rows = list(hub_rows) + list(concept_rows)

        elif min_connections >= 2:
            # Cross-doc view: only concepts shared across 2+ documents.
            # Compute concept set first, then pull only the Document hubs that
            # actually connect to those concepts — avoids flooding the map with
            # isolated lecture nodes.
            concept_rows = c.execute(
                base + " AND type='Concept' AND id IN ("
                "  SELECT source_id FROM edges WHERE relation='FOUND_IN'"
                "  GROUP BY source_id HAVING COUNT(DISTINCT target_id) >= ?"
                "  ORDER BY COUNT(DISTINCT target_id) DESC"
                ") LIMIT ?",
                [group_id, min_connections, limit],
            ).fetchall()

            if concept_rows:
                concept_id_ph = ",".join("?" * len(concept_rows))
                concept_ids   = [r["id"] for r in concept_rows]
                # Only Document hubs reachable from the visible concept set
                connected_doc_ids = {
                    row[0] for row in c.execute(
                        f"SELECT DISTINCT target_id FROM edges"
                        f" WHERE source_id IN ({concept_id_ph}) AND relation='FOUND_IN'",
                        concept_ids,
                    ).fetchall()
                }
                hub_rows = c.execute(
                    base + f" AND type='Document' AND id IN ({','.join('?'*len(connected_doc_ids))})",
                    [group_id] + list(connected_doc_ids),
                ).fetchall() if connected_doc_ids else []
            else:
                hub_rows = c.execute(base + " AND type='Document'", [group_id]).fetchall()

            node_rows = list(hub_rows) + list(concept_rows)

        else:
            # Full view (cross-doc OFF): all concepts + all Documents + WikiPages
            hub_rows = c.execute(
                base + " AND type IN ('Document','WikiPage')", [group_id]
            ).fetchall()
            concept_rows = c.execute(
                base + " AND type='Concept' AND id IN ("
                "  SELECT source_id FROM edges WHERE relation='FOUND_IN'"
                "  GROUP BY source_id ORDER BY COUNT(DISTINCT target_id) DESC"
                ") LIMIT ?",
                [group_id, limit],
            ).fetchall()
            node_rows = list(hub_rows) + list(concept_rows)

    if not node_rows:
        return {"nodes": [], "edges": []}

    node_ids = {r["id"] for r in node_rows}

    # Connection counts (batch)
    conn_counts: dict[str, int] = {}
    if node_ids:
        placeholders_c = ",".join("?" * len(node_ids))
        for row in c.execute(
            f"SELECT source_id AS nid, COUNT(*) AS cnt FROM edges WHERE source_id IN ({placeholders_c}) GROUP BY source_id"
            f" UNION ALL "
            f"SELECT target_id, COUNT(*) FROM edges WHERE target_id IN ({placeholders_c}) GROUP BY target_id",
            list(node_ids) * 2,
        ).fetchall():
            conn_counts[row[0]] = conn_counts.get(row[0], 0) + row[1]

    nodes_out = []
    for r in node_rows:
        meta = {}
        try:
            meta = _json.loads(r["metadata"] or "{}")
        except Exception:
            log.debug("Bad node metadata JSON for %s", r["id"], exc_info=True)
        nodes_out.append({
            "id":     r["id"],
            "label":  r["label"],
            "type":   r["type"],
            "doc":    r["source_path"] or "",
            "slug":   meta.get("slug", ""),
            "connections": conn_counts.get(r["id"], 0),
        })

    # Fetch all edges where source is in node set (target may be outside — include it)
    placeholders = ",".join("?" * len(node_ids))
    edge_rows = c.execute(
        f"SELECT source_id, target_id, relation FROM edges WHERE source_id IN ({placeholders})",
        list(node_ids),
    ).fetchall()

    # Add any target nodes not already in the set
    extra_target_ids = {r["target_id"] for r in edge_rows} - node_ids
    if extra_target_ids:
        ep = ",".join("?" * len(extra_target_ids))
        extra_rows = c.execute(
            f"SELECT id, type, label, source_path, metadata FROM nodes WHERE id IN ({ep})",
            list(extra_target_ids),
        ).fetchall()
        for r in extra_rows:
            meta = {}
            try:
                meta = _json.loads(r["metadata"] or "{}")
            except Exception:
                log.debug("Bad node metadata JSON for %s", r["id"], exc_info=True)
            nodes_out.append({
                "id": r["id"], "label": r["label"], "type": r["type"],
                "doc": r["source_path"] or "", "slug": meta.get("slug", ""),
                "connections": conn_counts.get(r["id"], 0),
            })

    edges_out = [
        {"source": r["source_id"], "target": r["target_id"], "relation": r["relation"]}
        for r in edge_rows
    ]

    return {"nodes": nodes_out, "edges": edges_out, "group_id": group_id}

@router.get("/doc_states")
async def doc_states(
    group_id: str = Query(...),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — per-document pipeline states (indexed / summarized / wiki / graph) for a group."""
    states = list_doc_states(username, group_id)
    return {"states": states}


@router.post("/node")
async def create_node(
    body: GraphNodeIn,
    user: dict = Depends(require_admin),
    username: str = Depends(get_current_username),
):
    """POST (admin) — create or update a graph node; returns its id."""
    _require_graph()
    node_id = upsert_node(
        username,
        node_type=body.type,
        label=body.label,
        group_id=body.group_id,
        source_path=body.source_path or "",
        metadata=body.metadata,
    )
    audit_log(user["sub"], "graph_node_create", target=body.group_id, details=body.label)
    return {"node_id": node_id}


@router.post("/edge")
async def create_edge(
    body: GraphEdgeIn,
    user: dict = Depends(require_admin),
    username: str = Depends(get_current_username),
):
    """POST (admin) — create an edge between two nodes with a relation/weight."""
    _require_graph()
    upsert_edge(
        username, body.source_id, body.target_id,
        body.relation, body.weight, body.metadata,
    )
    audit_log(user["sub"], "graph_edge_create",
              target=f"{body.source_id}→{body.target_id}", details=body.relation)
    return {"message": "Edge created"}


@router.delete("/group/{group_id}")
async def delete_group(
    group_id: str,
    user: dict = Depends(require_admin),
    username: str = Depends(get_current_username),
):
    """DELETE (admin) — wipe all graph nodes/edges for a group."""
    delete_group_graph(username, group_id)
    audit_log(user["sub"], "graph_group_delete", target=group_id)
    return {"message": f"Graph for group {group_id} deleted"}
