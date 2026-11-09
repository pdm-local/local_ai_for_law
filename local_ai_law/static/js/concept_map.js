/**
 * Concept Map — D3 v7 force-directed graph.
 * Hub-aware physics, label halos, degree-scaled forces, a stale-fetch generation
 * counter, edge dimming on crowded hubs, and a layout settled and fitted before it paints.
 * Flat: no shadows and no count badges, so the documents' names carry the picture.
 */

(function () {
  'use strict';

  // Colours are tokens (static/css/00-tokens.css), resolved to real colours when the
  // graph is drawn: an SVG attribute cannot read var(), and Export SVG must carry them.
  const TYPE_TOKEN = {
    WikiPage:  '--node-wiki',
    Document:  '--node-doc',
    Concept:   '--node-concept',
    Policy:    '--node-policy',
    Paper:     '--node-paper',
    Protocol:  '--node-protocol',
    Procedure: '--node-procedure',
  };
  const EDGE_TOKEN = {
    FOUND_IN:      '--node-concept',
    SUMMARIZED_AS: '--node-wiki',
    DEFINES:       '--node-protocol',
    CITES:         '--node-paper',
    MENTIONS:      '--edge',
  };
  const EDGE_COLOR = EDGE_TOKEN;  // keys only: which relations get their own arrow

  // Every word the graph writes goes through the shell's translation (uiT in
  // index.html), so a Dutch account reads Dutch here too. Types and relations get
  // plain names instead of their internal codes.
  const t = (s) => (window.uiT ? window.uiT(s) : s);
  const TYPE_LABEL = { WikiPage: 'Wiki', Document: 'Document', Concept: 'Concept' };
  const REL_LABEL = {
    HAS_KEYWORD: 'Mentions', MENTIONS: 'Mentions', DEFINES: 'Defines', CITES: 'Cites',
    FOUND_IN: 'Found in', SUMMARIZED_AS: 'Summary',
  };
  const typeLabel = (ty) => t(TYPE_LABEL[ty] || ty || '');
  const relLabel = (r) => t(REL_LABEL[(r || '').toUpperCase()] || r || '');

  let _tokCache = {};
  function tok(name) {
    if (_tokCache[name]) return _tokCache[name];
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return (_tokCache[name] = c);
  }
  function nodeColor(type) { return tok(TYPE_TOKEN[type] || '--node-other'); }
  function edgeColor(rel)  { return tok(EDGE_TOKEN[(rel || '').toUpperCase()] || '--edge'); }

  function edgeOpacity(rel, maxDeg) {
    const r = (rel || '').toUpperCase();
    let base;
    if (r === 'DEFINES' || r === 'CITES')         base = 0.85;
    else if (r === 'FOUND_IN' || r === 'SUMMARIZED_AS') base = 0.65;
    else if (r === 'MENTIONS')                     base = 0.35;
    else                                           base = 0.55;
    // Progressively fade spokes on very crowded hubs
    if (maxDeg > 15) base = Math.max(0.12, base - (maxDeg - 15) * 0.018);
    return base;
  }

  function nodeRadius(type, deg) {
    if (type === 'Document') return Math.max(16, Math.min(30, 16 + Math.sqrt(deg || 0) * 1.5));
    if (type === 'WikiPage') return 12;
    return 9;
  }

  let _svg = null, _sim = null, _gAll = null, _zoom = null;
  let _nodes = [], _edges = [], _groupId = null, _authToken = null;
  let _fetchGen = 0;   // monotonic counter — prevents stale fetch rendering (F8 fix)
  let _activeRelType = '';

  window.ConceptMap = { init, load, search: searchNodes, destroy, exportSVG };

  // ── Init ──────────────────────────────────────────────────────────────────────

  function init(containerId, groupId, authToken) {
    _tokCache = {};  // the preset may have changed since the last draw
    _groupId   = groupId;
    _authToken = authToken;
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    _svg = d3.select(container)
      .append('svg')
      .attr('width',  '100%')
      .attr('height', '100%')
      .style('background', 'var(--bg)');

    _zoom = d3.zoom().scaleExtent([0.05, 6])
      .on('zoom', ({ transform }) => _gAll && _gAll.attr('transform', transform));
    _svg.call(_zoom);

    _gAll = _svg.append('g').attr('class', 'cm-all');

    const defs = _svg.append('defs');

    // Arrow markers
    Object.keys(EDGE_COLOR).forEach(rel => {
      defs.append('marker')
        .attr('id', `arr-${rel}`)
        .attr('viewBox', '-0 -5 10 10').attr('refX', 28).attr('refY', 0)
        .attr('orient', 'auto').attr('markerWidth', 5).attr('markerHeight', 5)
        .append('path').attr('d', 'M 0,-5 L 10,0 L 0,5').attr('fill', edgeColor(rel));
    });
    defs.append('marker')
      .attr('id', 'arr-DEFAULT')
      .attr('viewBox', '-0 -5 10 10').attr('refX', 18).attr('refY', 0)
      .attr('orient', 'auto').attr('markerWidth', 5).attr('markerHeight', 5)
      .append('path').attr('d', 'M 0,-5 L 10,0 L 0,5').attr('fill', edgeColor(''));

    load(groupId);
  }

  // ── Load ──────────────────────────────────────────────────────────────────────

  async function load(groupId, nodeType, doc, limit, minConn, relType) {
    _groupId = groupId || _groupId;
    _activeRelType = relType || '';
    if (!_groupId || !_svg) return;

    const params = new URLSearchParams({ group_id: _groupId });
    if (nodeType) params.set('node_type', nodeType);
    if (doc)      params.set('doc', doc);
    if (limit)    params.set('limit', limit);
    if (minConn !== undefined) params.set('min_connections', minConn);

    const gen = ++_fetchGen;
    try {
      const resp = await fetch(`/api/graph/visualize?${params}`, {
        headers: { Authorization: `Bearer ${_authToken}` },
        credentials: 'include',
      });
      if (!resp.ok) throw new Error(await resp.text());
      if (gen !== _fetchGen || !_svg) return;   // stale — a newer load superseded this one
      const data = await resp.json();
      _nodes = data.nodes || [];
      _edges = data.edges || [];

      const badge = document.getElementById('cm-node-count');
      if (badge) badge.textContent = `${_nodes.length} ${t('nodes')} · ${_edges.length} ${t('edges')}`;

      if (_nodes.length === 0) { _renderEmpty(); return; }
      _render();
    } catch (e) {
      console.error('ConceptMap load error:', e);
    }
  }

  // ── Empty state ─────────────────────────────────────────────────────────────

  function _renderEmpty() {
    if (_sim) { _sim.stop(); _sim = null; }
    if (_gAll) _gAll.selectAll('*').remove();
    const svgEl = _svg.node();
    const W = svgEl.clientWidth  || svgEl.parentElement?.clientWidth  || 900;
    const H = svgEl.clientHeight || svgEl.parentElement?.clientHeight || 620;
    const g = _gAll.append('g').attr('class', 'cm-empty')
      .attr('transform', `translate(${W / 2}, ${H / 2})`);
    g.append('text')
      .attr('text-anchor', 'middle').attr('y', -8)
      .attr('fill', tok('--text2'))
      .style('font-size', '15px').style('font-weight', '600')
      .text(t('No knowledge graph yet'));
    g.append('text')
      .attr('text-anchor', 'middle').attr('y', 18)
      .attr('fill', tok('--text2'))
      .style('font-size', '13px')
      .text(t('Re-index this matter to extract concepts and relations.'));
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  function _render() {
    if (!_gAll) return;
    if (_sim) { _sim.stop(); _sim = null; }
    _gAll.selectAll('*').remove();

    const svgEl = _svg.node();
    const W = svgEl.clientWidth  || svgEl.parentElement?.clientWidth  || 900;
    const H = svgEl.clientHeight || svgEl.parentElement?.clientHeight || 620;

    const nodeById = new Map(_nodes.map(n => [n.id, { ...n }]));
    const nodeArr  = Array.from(nodeById.values());

    // Real degree from actual edges (more accurate than stored node.connections)
    const degreeMap = new Map();
    _edges.forEach(e => {
      const s = typeof e.source === 'object' ? e.source.id : e.source;
      const t = typeof e.target === 'object' ? e.target.id : e.target;
      degreeMap.set(s, (degreeMap.get(s) || 0) + 1);
      degreeMap.set(t, (degreeMap.get(t) || 0) + 1);
    });

    const validEdges = _edges
      .filter(e => {
        const s = typeof e.source === 'object' ? e.source.id : e.source;
        const t = typeof e.target === 'object' ? e.target.id : e.target;
        return nodeById.has(s) && nodeById.has(t);
      })
      .filter(e => !_activeRelType || (e.relation || '').toUpperCase() === _activeRelType.toUpperCase())
      .map(e => ({
        source:   typeof e.source === 'object' ? e.source.id : e.source,
        target:   typeof e.target === 'object' ? e.target.id : e.target,
        relation: e.relation,
      }));

    // Pre-position hubs in a ring so the simulation starts from a better state
    const hubNodes = nodeArr.filter(n => (degreeMap.get(n.id) || 0) > 20);
    hubNodes.forEach((n, i) => {
      if (n.x !== undefined) return;
      const angle = (i / Math.max(hubNodes.length, 1)) * 2 * Math.PI;
      const r = Math.min(W, H) * 0.28;
      n.x = W / 2 + r * Math.cos(angle);
      n.y = H / 2 + r * Math.sin(angle);
    });

    // ── Edges ─────────────────────────────────────────────────────────────────
    const linkSel = _gAll.append('g').attr('class', 'cm-links')
      .selectAll('line').data(validEdges).enter().append('line')
      .attr('stroke',         d => edgeColor(d.relation))
      .attr('stroke-opacity', d => {
        const sd = degreeMap.get(d.source) || 0;
        const td = degreeMap.get(d.target) || 0;
        return edgeOpacity(d.relation, Math.max(sd, td));
      })
      .attr('stroke-width', d => {
        const r = (d.relation || '').toUpperCase();
        const maxDeg = Math.max(degreeMap.get(d.source) || 0, degreeMap.get(d.target) || 0);
        const base = r === 'SUMMARIZED_AS' ? 2 : 1.5;
        return maxDeg > 20 ? Math.max(0.5, base * 0.55) : base;
      })
      .attr('marker-end', d => {
        const r = (d.relation || '').toUpperCase();
        return `url(#arr-${EDGE_COLOR[r] ? r : 'DEFAULT'})`;
      });

    // ── Nodes ─────────────────────────────────────────────────────────────────
    const _labelCol    = tok('--text');
    const _labelColDim = tok('--text2');
    const _bgCol       = tok('--bg');
    const _ringCol     = tok('--bg');

    const nodeSel = _gAll.append('g').attr('class', 'cm-nodes')
      .selectAll('g').data(nodeArr).enter().append('g')
      .attr('class', 'cm-node').style('cursor', 'pointer')
      .on('click', (event, d) => _showSidebar(d))
      .call(d3.drag()
        .on('start', (event, d) => { if (!event.active) _sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag',  (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on('end',   (event, d) => { if (!event.active) _sim.alphaTarget(0); d.fx = null; d.fy = null; })
      );

    // Main circle
    nodeSel.append('circle')
      .attr('r',            d => nodeRadius(d.type, degreeMap.get(d.id) || d.connections || 0))
      .attr('fill',         d => nodeColor(d.type))
      .attr('stroke',       _ringCol)
      .attr('stroke-opacity', d => d.type === 'Document' ? 0.9 : 0.3)
      .attr('stroke-width', d => d.type === 'Document' ? 2.5 : 1.2)
      .attr('opacity',      0.95);

    // Dashed ring on very busy document hubs (>25 connections)
    nodeSel.filter(d => d.type === 'Document' && (degreeMap.get(d.id) || 0) > 25)
      .append('circle')
      .attr('r',              d => nodeRadius(d.type, degreeMap.get(d.id) || 0) + 6)
      .attr('fill',           'none')
      .attr('stroke',         nodeColor('Document'))
      .attr('stroke-width',   1.5)
      .attr('stroke-dasharray', '5 3')
      .attr('opacity',        0.45);

    // Label with dark halo via paint-order stroke for readability on any background
    nodeSel.append('text')
      .attr('dy',           d => nodeRadius(d.type, degreeMap.get(d.id) || 0) + 13)
      .attr('text-anchor',  'middle')
      .attr('font-size',    d => d.type === 'Document' ? 14 : 13)
      .attr('font-weight',  d => d.type === 'Document' ? 700 : 500)
      .attr('fill',         d => d.type === 'Document' ? _labelCol : _labelColDim)
      .attr('stroke',       _bgCol)
      .attr('stroke-width', 3.5)
      .attr('paint-order',  'stroke')
      .style('pointer-events', 'none')
      .text(d => {
        const max = d.type === 'Document' ? 28 : 22;
        return d.label.length > max ? d.label.slice(0, max - 1) + '…' : d.label;
      });

    // ── Simulation (hub-aware forces) ─────────────────────────────────────────
    _sim = d3.forceSimulation(nodeArr)
      .alphaDecay(0.014)        // slower cooling → better settled layout
      .velocityDecay(0.38)
      .force('link', d3.forceLink(validEdges).id(d => d.id)
        .distance(d => {
          const r      = (d.relation || '').toUpperCase();
          const sd     = degreeMap.get(d.source) || 0;
          const td     = degreeMap.get(d.target) || 0;
          const hubDeg = Math.max(sd, td);
          const base   = r === 'SUMMARIZED_AS' ? 85 : 115;
          // Stretch spokes on crowded hubs so they fan out instead of piling up
          return base + Math.sqrt(hubDeg) * 16;
        })
        .strength(d => {
          const sd     = degreeMap.get(d.source) || 0;
          const td     = degreeMap.get(d.target) || 0;
          const hubDeg = Math.max(sd, td);
          // Weaker spring on crowded hubs — lets repulsion spread the spokes
          return hubDeg > 20 ? 0.12 : 0.38;
        })
      )
      .force('charge', d3.forceManyBody()
        .strength(d => {
          const deg = degreeMap.get(d.id) || d.connections || 0;
          if (d.type === 'Document') return -(700 + deg * 35);
          return -(180 + deg * 22);
        })
        .distanceMax(700)
      )
      .force('center',    d3.forceCenter(W / 2, H / 2).strength(0.04))
      // A gentle pull to the view's shape, so a chain of documents folds into the
      // screen instead of running off one edge (the fit then needs less shrinking).
      .force('x', d3.forceX(W / 2).strength(0.03))
      .force('y', d3.forceY(H / 2).strength(0.03 * W / Math.max(H, 1)))
      .force('collision', d3.forceCollide()
        .radius(d => nodeRadius(d.type, degreeMap.get(d.id) || 0) + 18)
        .strength(0.85)
      )
      .on('tick', () => {
        linkSel
          .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        nodeSel.attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`);
      });
    // Settle the layout before the first paint, fit it to the view, then let it
    // breathe a little: the graph opens whole and still, not flying in from the edges.
    _sim.stop();
    for (let i = 0; i < 400; i++) _sim.tick();
    _sim.alpha(0.03).restart();
    _fit(nodeArr, W, H);
  }

  // Zoom so every node (and its label) is inside the view, never past 1.6x.
  function _fit(nodes, W, H) {
    if (!_svg || !_zoom || !nodes.length) return;
    const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y);
    const pad = 90, padX = 130;  // a document's label runs about 130px either side
    const x0 = Math.min(...xs) - padX, x1 = Math.max(...xs) + padX;
    const y0 = Math.min(...ys) - pad / 2, y1 = Math.max(...ys) + pad * 0.7;
    const k = Math.min(1.6, W / (x1 - x0), H / (y1 - y0));
    const t = d3.zoomIdentity.translate(W / 2, H / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
    _svg.call(_zoom.transform, t);
  }

  // ── Sidebar ───────────────────────────────────────────────────────────────────

  // Node labels/slugs/docs come from LLM extraction over user documents —
  // untrusted; everything interpolated into sidebar HTML must be escaped.
  function _esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _showSidebar(node) {
    const sidebar = document.getElementById('cm-sidebar');
    if (!sidebar) return;
    const deg = node.connections || 0;
    const relatedEdges = _edges.filter(e => {
      const src = typeof e.source === 'object' ? e.source.id : e.source;
      const tgt = typeof e.target === 'object' ? e.target.id : e.target;
      return src === node.id || tgt === node.id;
    });
    const relHtml = relatedEdges.slice(0, 20).map(e => {
      const src = typeof e.source === 'object' ? e.source.id : e.source;
      const tgt = typeof e.target === 'object' ? e.target.id : e.target;
      const isSource  = src === node.id;
      const otherId   = isSource ? tgt : src;
      const otherNode = _nodes.find(n => n.id === otherId);
      const otherLabel = otherNode ? otherNode.label : otherId;
      const dir = isSource ? '→' : '←';
      const label = otherLabel.length > 28 ? otherLabel.slice(0, 26) + '…' : otherLabel;
      return `<div style="font-size:var(--fs-xs);color:var(--text2);padding:2px 0">${dir} <b style="color:${edgeColor(e.relation)}">${_esc(relLabel(e.relation))}</b> ${_esc(label)}</div>`;
    }).join('');

    const wikiBtn = node.type === 'WikiPage' && node.slug
      ? `<button class="btn btn-primary btn-sm" id="cm-btn-wiki" style="width:100%;margin-top:6px">${_esc(t('Open summary'))}</button>`
      : '';
    const chatBtn = `<button class="btn btn-secondary btn-sm" id="cm-btn-chat" style="width:100%;margin-top:6px">${_esc(t('Chat About This'))}</button>`;

    sidebar.innerHTML = `
      <div style="padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div style="font-size:var(--fs-xs);font-weight:700;color:var(--text)">${_esc(node.label)}</div>
          <button class="btn btn-ghost btn-icon btn-sm" id="cm-btn-close" title="${_esc(t('Close'))}" aria-label="${_esc(t('Close'))}">✕</button>
        </div>
        <div style="font-size:var(--fs-xs);color:${nodeColor(node.type)};font-weight:600;margin-bottom:2px">${_esc(typeLabel(node.type))}</div>
        ${node.doc ? `<div style="font-size:var(--fs-xs);color:var(--text3);margin-bottom:2px">${_esc(t('Source:'))} ${_esc(String(node.doc).split('/').pop())}</div>` : ''}
        <div style="font-size:var(--fs-xs);color:var(--text3);margin-bottom:10px">${relatedEdges.length} ${_esc(t('visible'))} · ${deg} ${_esc(t(deg !== 1 ? 'total connections' : 'total connection'))}</div>
        ${relHtml ? `<div style="border-top:1px solid var(--border);padding-top:8px;margin-bottom:8px;max-height:220px;overflow-y:auto">${relHtml}</div>` : ''}
        ${wikiBtn}${chatBtn}
      </div>`;
    // Untrusted strings never go into inline handlers — bind with closures instead.
    const wikiEl = sidebar.querySelector('#cm-btn-wiki');
    if (wikiEl) wikiEl.addEventListener('click', () => { if (window._cmOpenWiki) window._cmOpenWiki(node.slug); });
    const chatEl = sidebar.querySelector('#cm-btn-chat');
    if (chatEl) chatEl.addEventListener('click', () => { if (window._cmChat) window._cmChat(node.label); });
    const closeEl = sidebar.querySelector('#cm-btn-close');
    if (closeEl) closeEl.addEventListener('click', () => { sidebar.style.display = 'none'; });
    sidebar.style.display = 'block';
  }

  // ── Search ────────────────────────────────────────────────────────────────────

  function searchNodes(query) {
    if (!_gAll) return;
    if (!query) {
      _gAll.selectAll('.cm-node circle').attr('opacity', 0.93);
      _gAll.selectAll('.cm-node text').attr('opacity', 1);
      _gAll.selectAll('.cm-links line').attr('stroke-opacity', d => {
        const sd = d.source?.connections || 0;
        const td = d.target?.connections || 0;
        return edgeOpacity(d.relation, Math.max(sd, td));
      });
      return;
    }
    const q = query.toLowerCase();
    const matchIds = new Set(
      _nodes.filter(n => n.label.toLowerCase().includes(q) || n.type.toLowerCase().includes(q)).map(n => n.id)
    );
    const hopIds = new Set(matchIds);
    _edges.forEach(e => {
      const src = typeof e.source === 'object' ? e.source.id : e.source;
      const tgt = typeof e.target === 'object' ? e.target.id : e.target;
      if (matchIds.has(src)) hopIds.add(tgt);
      if (matchIds.has(tgt)) hopIds.add(src);
    });
    _gAll.selectAll('.cm-node circle').attr('opacity', d => hopIds.has(d.id) ? 0.95 : 0.07);
    _gAll.selectAll('.cm-node text').attr('opacity', d => hopIds.has(d.id) ? 1 : 0.05);
    _gAll.selectAll('.cm-links line').attr('stroke-opacity', d => {
      const src = typeof d.source === 'object' ? d.source.id : d.source;
      const tgt = typeof d.target === 'object' ? d.target.id : d.target;
      return (hopIds.has(src) && hopIds.has(tgt)) ? 0.85 : 0.02;
    });
  }

  // ── Export / Destroy ──────────────────────────────────────────────────────────

  function exportSVG() {
    if (!_svg) return;
    const blob = new Blob([_svg.node().outerHTML], { type: 'image/svg+xml' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `concept_map_${_groupId}.svg`;
    a.click();
  }

  function destroy() {
    if (_sim) _sim.stop();
    _svg = _sim = _gAll = null;
    _nodes = []; _edges = [];
    _fetchGen++;   // invalidate any in-flight fetches
  }

})();
