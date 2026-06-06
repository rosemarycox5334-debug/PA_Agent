/* Decision-flow canvas visualization — sci-fi themed bezier graph. */

const _NEON_CYAN = "#37f8ff";
const _NEON_BLUE = "#58a6ff";
const _NEON_VIOLET = "#a371f7";
const _NEON_AMBER = "#ffcf33";
const _GLASS_BG = "#09111d";

const _PHASE_ZH = { gate: "闸门", decision: "策略" };
const _OUTCOME_ZH = { wait: "等待", reject: "放弃", trade: "交易", proceed: "继续评估" };
const _OUTCOME_COLOR = { wait: "#ffcf33", reject: "#ff6b6b", trade: "#34d399", proceed: "#58a6ff" };
const _ANSWER_COLOR = { "是": "#34d399", "否": "#ff6b6b", "中性": "#ffcf33", "等待": "#ffcf33", "不适用": "#6b7280" };

const NODE_W = 420;
const NODE_H = 140;
const LEVEL_DY = 180;
const BRANCH_DX = 220;
const PAD_X = 60;
const PAD_Y = 60;

/* ------------------------------------------------------------------ */
/*  Public entry point                                                */
/* ------------------------------------------------------------------ */

export function initDecisionFlow({ canvas, trace, container }) {
    if (!canvas) return () => {};
    const ctx = canvas.getContext("2d");
    let animId = null;
    let phase = 0;
    let dragging = false;
    let dragStart = { x: 0, y: 0 };
    let offset = { x: 0, y: 0 };
    let scale = 1;
    let lastTraceLen = 0;
    let hoverIdx = -1;
    let mx = 0, my = 0;

    /* ---- layout ---- */
    function layout() {
        const items = trace;
        if (!items || !items.length) return { w: 0, h: 0, cols: [], nodes: [] };
        const cols = {};                       // x-offset -> [{item, idx}]
        const xs = [];
        items.forEach((item, idx) => {
            const side = _branchSide(item);
            const parentX = idx > 0 ? xs[idx - 1] : 0;
            let x = parentX;
            if (side === "left") x = parentX - BRANCH_DX;
            else if (side === "right") x = parentX + BRANCH_DX;
            xs.push(x);
            if (!cols[x]) cols[x] = [];
            cols[x].push({ item, idx });
        });
        const keys = Object.keys(cols).map(Number).sort((a, b) => a - b);
        const colCount = keys.length;
        const totalW = colCount * NODE_W + PAD_X * 2 + (colCount > 1 ? (colCount - 1) * 40 : 0);
        const nodes = [];
        keys.forEach((xOff, ci) => {
            const colX = PAD_X + ci * (NODE_W + 40);
            cols[xOff].forEach(({ item, idx }, ri) => {
                nodes.push({
                    item, idx,
                    x: colX + NODE_W / 2,
                    y: PAD_Y + ri * (NODE_H + LEVEL_DY) + NODE_H / 2,
                    colX, row: ri,
                });
            });
        });
        const maxRow = nodes.reduce((m, n) => Math.max(m, n.row), 0);
        const totalH = PAD_Y + maxRow * (NODE_H + LEVEL_DY) + NODE_H + PAD_Y;
        return { w: totalW, h: totalH, cols, nodes };
    }

    function _branchSide(item) {
        if (item.skipped) return "down";
        const ans = String(item.answer || "").split("（")[0];
        if (ans === "否") return "left";
        if (ans === "是" || ans === "等待" || ans === "中性") return "right";
        return "down";
    }

    function _answerColor(ans) {
        const base = String(ans || "").split("（")[0];
        return _ANSWER_COLOR[base] || _NEON_BLUE;
    }

    /* ---- drawing ---- */
    function drawBg(w, h) {
        const grad = ctx.createLinearGradient(0, 0, w, h);
        grad.addColorStop(0, "#020711");
        grad.addColorStop(0.48, "#07111f");
        grad.addColorStop(1, "#030409");
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, w, h);

        /* grid */
        ctx.strokeStyle = "rgba(34,231,255,0.09)";
        ctx.lineWidth = 1;
        for (let x = 0; x < w; x += 32) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
        for (let y = 0; y < h; y += 32) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
        ctx.strokeStyle = "rgba(88,166,255,0.14)";
        for (let x = 0; x < w; x += 128) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
        for (let y = 0; y < h; y += 128) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }

        /* scan beam */
        const sy = ((phase * 0.18) % 1.0) * (h + 220) - 110;
        const sg = ctx.createLinearGradient(0, sy - 34, 0, sy + 34);
        sg.addColorStop(0, "rgba(55,248,255,0)");
        sg.addColorStop(0.5, "rgba(55,248,255,0.08)");
        sg.addColorStop(1, "rgba(55,248,255,0)");
        ctx.fillStyle = sg;
        ctx.fillRect(0, sy - 34, w, 68);
    }

    function drawPath(x0, y0, x1, y1, accent, active) {
        const col = active ? accent : "rgba(107,114,128,0.35)";
        const glow = ctx.createLinearGradient(x0, y0, x1, y1);
        glow.addColorStop(0, active ? col : "rgba(107,114,128,0.15)");
        glow.addColorStop(1, active ? col : "rgba(107,114,128,0.15)");
        ctx.strokeStyle = glow;
        ctx.lineWidth = active ? 6 : 2;
        ctx.globalAlpha = active ? 0.35 : 0.2;
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.bezierCurveTo(x0, y0 + 60, x1, y1 - 60, x1, y1);
        ctx.stroke();
        ctx.globalAlpha = 1;

        ctx.strokeStyle = col;
        ctx.lineWidth = active ? 2.5 : 1;
        if (!active) ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.bezierCurveTo(x0, y0 + 60, x1, y1 - 60, x1, y1);
        ctx.stroke();
        ctx.setLineDash([]);

        /* particles */
        for (let i = 0; i < 3; i++) {
            const pct = ((phase * 0.42 + i * 0.33) % 1.0);
            const t = pct;
            const px = (1 - t) * (1 - t) * (1 - t) * x0 + 3 * (1 - t) * (1 - t) * t * x0 + 3 * (1 - t) * t * t * x1 + t * t * t * x1;
            const py = (1 - t) * (1 - t) * (1 - t) * y0 + 3 * (1 - t) * (1 - t) * t * (y0 + 60) + 3 * (1 - t) * t * t * (y1 - 60) + t * t * t * y1;
            ctx.fillStyle = col;
            ctx.globalAlpha = 0.7 - i * 0.15;
            ctx.beginPath();
            ctx.arc(px, py, 4 - i, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.globalAlpha = 1;

        /* arrow */
        const dy = y1 - y0;
        const dx = x1 - x0;
        const angle = Math.atan2(dy, dx);
        const ah = 8;
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x1 - ah * Math.cos(angle - 0.45), y1 - ah * Math.sin(angle - 0.45));
        ctx.lineTo(x1 - ah * Math.cos(angle + 0.45), y1 - ah * Math.sin(angle + 0.45));
        ctx.closePath();
        ctx.fill();
    }

    function drawNode(node) {
        const { item, x, y } = node;
        const w = NODE_W, h = NODE_H;
        const accent = _answerColor(item.answer || "");
        const rect = { x: x - w / 2, y: y - h / 2, w, h };

        /* hover glow */
        if (node.idx === hoverIdx) {
            ctx.shadowColor = accent;
            ctx.shadowBlur = 24;
        }

        /* card bg */
        const grad = ctx.createLinearGradient(rect.x, rect.y, rect.x, rect.y + h);
        grad.addColorStop(0, "#172943");
        grad.addColorStop(0.5, _GLASS_BG);
        grad.addColorStop(1, "#030711");
        ctx.fillStyle = grad;
        roundRect(ctx, rect.x, rect.y, w, h, 14);
        ctx.fill();
        ctx.strokeStyle = accent;
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = 1.5;
        roundRect(ctx, rect.x, rect.y, w, h, 14);
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.shadowBlur = 0;

        /* corner brackets */
        ctx.strokeStyle = accent;
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = 1.5;
        const l = 12;
        [[rect.x + 6, rect.y + 6, 1, 1], [rect.x + w - 6, rect.y + 6, -1, 1],
         [rect.x + 6, rect.y + h - 6, 1, -1], [rect.x + w - 6, rect.y + h - 6, -1, -1]].forEach(([bx, by, dx, dy]) => {
            ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + dx * l, by); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx, by + dy * l); ctx.stroke();
        });
        ctx.globalAlpha = 1;

        /* left accent stripe */
        const sg = ctx.createLinearGradient(rect.x, 0, rect.x + 10, 0);
        sg.addColorStop(0, accent);
        sg.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = sg;
        roundRect(ctx, rect.x, rect.y + 8, 8, h - 16, 4);
        ctx.fill();

        /* text */
        const padX = 20;
        const innerW = w - padX * 2;
        const phase = _PHASE_ZH[item.phase] || item.phase || "";
        ctx.font = "bold 12px Consolas, monospace";
        ctx.fillStyle = "#9ca3af";
        ctx.textBaseline = "top";
        ctx.fillText(`#${String(node.idx + 1).padStart(2, "0")} · ${phase}`, rect.x + padX, rect.y + 12);

        const nid = item.node_id || "?";
        ctx.font = "bold 12px Consolas, monospace";
        ctx.fillStyle = _NEON_CYAN;
        ctx.textAlign = "right";
        ctx.fillText(`§${nid}`, rect.x + w - padX, rect.y + 12);
        ctx.textAlign = "left";

        const section = item.section_id || "";
        const barRange = item.bar_range || "";
        const meta = [section, barRange].filter(Boolean).join(" · ");
        if (meta) {
            ctx.font = "11px Consolas, monospace";
            ctx.fillStyle = "#9ca3af";
            ctx.fillText(meta, rect.x + padX, rect.y + 34);
        }

        const question = _plainQuestion(item);
        ctx.font = "12px 'Microsoft YaHei UI', sans-serif";
        ctx.fillStyle = "#e5e7eb";
        const lines = wrapText(ctx, question, innerW);
        lines.slice(0, 3).forEach((line, i) => {
            ctx.fillText(line, rect.x + padX, rect.y + 56 + i * 16);
        });

        const answer = String(item.answer || "—");
        ctx.font = "bold 13px 'Microsoft YaHei UI', sans-serif";
        ctx.fillStyle = accent;
        ctx.fillText(answer, rect.x + padX, rect.y + 108);

        const outcome = item.outcome || item.gate_result || "";
        if (outcome && _OUTCOME_ZH[outcome]) {
            ctx.font = "11px sans-serif";
            ctx.fillStyle = _OUTCOME_COLOR[outcome] || accent;
            ctx.textAlign = "right";
            ctx.fillText(_OUTCOME_ZH[outcome], rect.x + w - padX, rect.y + 110);
            ctx.textAlign = "left";
        }
    }

    function _plainQuestion(item) {
        const q = item.question || item.node_id || "";
        return q.replace(/（基于[^）]+判断）$/, "").trim();
    }

    function wrapText(ctx, text, maxW) {
        const words = text.split("");
        const lines = [];
        let line = "";
        for (const ch of words) {
            const test = line + ch;
            if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = ch; }
            else line = test;
        }
        if (line) lines.push(line);
        return lines;
    }

    function roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    /* ---- hit-test ---- */
    function hitTest(lx, ly) {
        const { nodes } = layout();
        for (let i = nodes.length - 1; i >= 0; i--) {
            const n = nodes[i];
            if (lx >= n.x - NODE_W / 2 && lx <= n.x + NODE_W / 2 &&
                ly >= n.y - NODE_H / 2 && ly <= n.y + NODE_H / 2) return i;
        }
        return -1;
    }

    /* ---- render loop ---- */
    function frame() {
        const l = layout();
        const cw = canvas.clientWidth;
        const ch = canvas.clientHeight;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = cw * dpr;
        canvas.height = ch * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cw, ch);

        drawBg(cw, ch);

        ctx.save();
        const autoScale = Math.min(1, (cw - 40) / (l.w || 1), (ch - 40) / (l.h || 1));
        const finalScale = scale * autoScale;
        const tx = (cw - l.w * finalScale) / 2 + offset.x;
        const ty = 20 + offset.y;
        ctx.translate(tx, ty);
        ctx.scale(finalScale, finalScale);

        /* paths */
        const { nodes } = layout();
        const nodeByIdx = {};
        nodes.forEach(n => { nodeByIdx[n.idx] = n; });
        for (let i = 1; i < trace.length; i++) {
            const parent = nodeByIdx[i - 1];
            const child = nodeByIdx[i];
            if (parent && child) {
                drawPath(parent.x, parent.y + NODE_H / 2, child.x, child.y - NODE_H / 2, _answerColor(child.item.answer), true);
            }
        }

        /* nodes */
        nodes.forEach(drawNode);

        ctx.restore();
        phase += 0.016;
        animId = requestAnimationFrame(frame);
    }

    /* ---- events ---- */
    function onDown(e) {
        dragging = true;
        const r = canvas.getBoundingClientRect();
        dragStart = { x: e.clientX - r.left - offset.x, y: e.clientY - r.top - offset.y };
    }
    function onMove(e) {
        const r = canvas.getBoundingClientRect();
        mx = e.clientX - r.left;
        my = e.clientY - r.top;
        const l = layout();
        const cw = canvas.clientWidth, ch = canvas.clientHeight;
        const autoScale = Math.min(1, (cw - 40) / (l.w || 1), (ch - 40) / (l.h || 1));
        const finalScale = scale * autoScale;
        const tx = (cw - l.w * finalScale) / 2 + offset.x;
        const ty = 20 + offset.y;
        const lx = (mx - tx) / finalScale;
        const ly = (my - ty) / finalScale;
        hoverIdx = hitTest(lx, ly);
        canvas.style.cursor = hoverIdx >= 0 ? "pointer" : (dragging ? "grabbing" : "grab");
        if (dragging) {
            offset.x = e.clientX - r.left - dragStart.x;
            offset.y = e.clientY - r.top - dragStart.y;
        }
    }
    function onUp() { dragging = false; }
    function onWheel(e) {
        e.preventDefault();
        const r = canvas.getBoundingClientRect();
        const cx = e.clientX - r.left;
        const cy = e.clientY - r.top;
        const old = scale;
        scale = Math.max(0.2, Math.min(3, scale * (e.deltaY < 0 ? 1.08 : 0.92)));
        const delta = scale / old;
        offset.x = cx - (cx - offset.x) * delta;
        offset.y = cy - (cy - offset.y) * delta;
    }
    function onDblClick() {
        const l = layout();
        if (hoverIdx >= 0 && hoverIdx < trace.length) {
            const item = trace[hoverIdx];
            const tip = [item.question || "", item.bar_range ? `K线：${item.bar_range}` : "", item.reason || ""].filter(Boolean).join("\n");
            if (tip) alert(tip);
        }
    }

    canvas.addEventListener("mousedown", onDown);
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseup", onUp);
    canvas.addEventListener("mouseleave", onUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);

    /* ---- start ---- */
    if (trace.length !== lastTraceLen) {
        offset = { x: 0, y: 0 };
        scale = 1;
        lastTraceLen = trace.length;
    }
    animId = requestAnimationFrame(frame);

    return () => {
        if (animId) cancelAnimationFrame(animId);
        canvas.removeEventListener("mousedown", onDown);
        canvas.removeEventListener("mousemove", onMove);
        canvas.removeEventListener("mouseup", onUp);
        canvas.removeEventListener("mouseleave", onUp);
        canvas.removeEventListener("wheel", onWheel);
        canvas.removeEventListener("dblclick", onDblClick);
    };
}
