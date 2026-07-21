/* PA Agent 控制台前端（Vue 3 无构建单页） */
import { createApp, nextTick } from "./vendor/vue.esm-browser.prod.js";
import { renderKlineChart } from "./chart.js";

const PHASE_LABELS = {
  switching: "切换品种",
  waiting_data: "等待数据",
  stage1: "阶段一分析中",
  stage2: "阶段二分析中",
  notifying: "推送通知",
  done: "完成",
};

const DECISION_FIELDS = [
  ["方向", "order_direction"],
  ["方式", "order_type"],
  ["入场价", "entry_price"],
  ["止损价", "stop_loss_price"],
  ["止盈 TP1", "take_profit_price"],
  ["止盈 TP2", "take_profit_price_2"],
  ["信心", "trade_confidence"],
];

function decisionOf(record) {
  return (record && record.stage2_decision && record.stage2_decision.decision) || null;
}

function rowsOf(decision) {
  if (!decision) return [];
  return DECISION_FIELDS.map(([label, key]) => [label, decision[key] ?? "—"]);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    const msg =
      (body && (body.error || body.detail || JSON.stringify(body))) ||
      `HTTP ${resp.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body;
}

createApp({
  data() {
    return {
      tabs: [
        { key: "monitor", label: "监控" },
        { key: "config", label: "配置" },
        { key: "history", label: "历史" },
      ],
      tab: "monitor",
      status: { scheduler: { running: false, error: null }, current: {}, round_wait_eta: null, results: {}, events: [] },
      countdown: null,
      cfg: null,
      masked: {},
      records: [],
      histFilter: "",
      detail: null,          // 历史抽屉的完整记录
      detailSymbol: "",      // 详情页品种
      detailRecord: null,    // 详情页最新记录
      live: null,            // 详情页实时推理
      feishuTestResult: null,
      saveMsg: null,
      toast: null,
      busy: { watch: false, save: false, feishu: false },
      _pollTimer: null,
      _tickTimer: null,
      _liveTimer: null,
      _liveSeq: -1,
      _liveBusy: false,
      _detailRecordName: "",
      _chart: null,          // 详情页图表句柄
      _drawerChart: null,    // 抽屉图表句柄
    };
  },
  computed: {
    running() {
      return this.status.scheduler.running;
    },
    activeCount() {
      return Object.keys(this.status.current || {}).length;
    },
    eventsDesc() {
      return [...this.status.events].reverse();
    },
    countdownText() {
      if (this.countdown === null) return "";
      const s = Math.max(0, Math.floor(this.countdown));
      return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")} 后开始下一轮`;
    },
    marketClosedText() {
      const until = this.status.market_closed_until;
      if (!until) return "";
      const dt = new Date(until * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())} 恢复轮巡`;
    },
    detailDecision() {
      return decisionOf(this.detail);
    },
    decisionRows() {
      return rowsOf(this.detailDecision);
    },
    detailDecision2() {
      return decisionOf(this.detailRecord);
    },
    decisionRows2() {
      return rowsOf(this.detailDecision2);
    },
  },
  methods: {
    phaseLabel(p) {
      return PHASE_LABELS[p] || p;
    },
    dirClass(dir) {
      if (!dir) return "";
      if (String(dir).includes("多")) return "dir-long";
      if (String(dir).includes("空")) return "dir-short";
      return "";
    },
    fmtTime(ts) {
      return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    },
    fmtTs(ms) {
      return ms ? new Date(ms).toLocaleString("zh-CN", { hour12: false }) : "—";
    },
    relTime(ts) {
      const diff = Date.now() / 1000 - ts;
      if (diff < 60) return "刚刚";
      if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
      if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
      return `${Math.floor(diff / 86400)} 天前`;
    },
    pretty(obj) {
      return obj ? JSON.stringify(obj, null, 2) : "（无）";
    },
    showToast(text, ok = true) {
      this.toast = { text, ok };
      setTimeout(() => (this.toast = null), 4000);
    },
    switchTab(key) {
      if (this.tab === "detail" && key !== "detail") {
        this.stopLivePoll();
        if (this._chart) {
          this._chart.destroy();
          this._chart = null;
        }
      }
      this.tab = key;
      if (key === "config" && !this.cfg) this.loadConfig();
      if (key === "history") this.loadRecords();
    },

    /* ── 状态轮询 ── */
    async pollStatus() {
      try {
        this.status = await api("/api/status");
        this.countdown =
          this.status.round_wait_eta !== null
            ? this.status.round_wait_eta - Date.now() / 1000
            : null;
      } catch {
        /* 网络瞬断时保留上次状态 */
      }
    },
    async toggleWatch() {
      this.busy.watch = true;
      try {
        await api(this.running ? "/api/watch/stop" : "/api/watch/start", { method: "POST" });
        this.showToast(this.running ? "已停止轮巡" : "轮巡已启动");
        await this.pollStatus();
      } catch (e) {
        this.showToast(e.message, false);
      } finally {
        this.busy.watch = false;
      }
    },

    /* ── 详情页 ── */
    async openDetail(symbol) {
      this.detailSymbol = symbol;
      this.detailRecord = null;
      this.live = null;
      this._liveSeq = -1;
      this._detailRecordName = "";
      this.tab = "detail";
      try {
        const list = await api(
          `/api/records?latest=1&symbol=${encodeURIComponent(symbol)}`
        );
        // 用户可能已切换品种或离开详情页：过期响应直接丢弃
        if (this.detailSymbol !== symbol || this.tab !== "detail") return;
        if (list.items.length) {
          const rec = await api(
            `/api/records/${encodeURIComponent(list.items[0].name)}`
          );
          if (this.detailSymbol !== symbol || this.tab !== "detail") return;
          this.detailRecord = rec;
          await nextTick();
          this.renderDetailChart();
        }
      } catch (e) {
        this.showToast("记录加载失败：" + e.message, false);
      }
      this.startLivePoll();
    },
    closeDetail() {
      this.stopLivePoll();
      if (this._chart) {
        this._chart.destroy();
        this._chart = null;
      }
      this.tab = "monitor";
    },
    renderDetailChart() {
      const el = this.$refs.chartEl;
      if (!el || !this.detailRecord || !this.detailRecord.kline_data) return;
      if (this._chart) this._chart.destroy();
      this._chart = renderKlineChart(
        el,
        this.detailRecord.kline_data,
        decisionOf(this.detailRecord)
      );
    },
    startLivePoll() {
      this.stopLivePoll();
      const tick = async () => {
        if (this.tab !== "detail" || document.hidden || this._liveBusy) return;
        const sym = this.detailSymbol;
        this._liveBusy = true;
        try {
          const live = await api(`/api/live/${encodeURIComponent(sym)}`);
          // 品种已切换/页面已离开：过期响应丢弃，防止串台
          if (sym !== this.detailSymbol || this.tab !== "detail") return;
          if (live.seq !== this._liveSeq) {
            this._liveSeq = live.seq;
            this.live = live;
            await nextTick();
            this.scrollLive();
            // 有新内容且分析已结束：刷新记录与图表
            // （覆盖「页面隐藏期间整个分析开始并完成」的场景）
            if (!live.running) this.refreshDetailRecord();
          } else if (this.live && this.live.running !== live.running) {
            this.live = live;
            if (!live.running) this.refreshDetailRecord();
          }
        } catch {
          /* 404 = 尚无分析活动，忽略 */
        } finally {
          this._liveBusy = false;
        }
      };
      tick();
      this._liveTimer = setInterval(tick, 1000);
    },
    stopLivePoll() {
      if (this._liveTimer) {
        clearInterval(this._liveTimer);
        this._liveTimer = null;
      }
    },
    scrollLive() {
      for (const pre of document.querySelectorAll(".live-pre")) {
        pre.scrollTop = pre.scrollHeight;
      }
    },
    async refreshDetailRecord() {
      const sym = this.detailSymbol;
      try {
        const list = await api(
          `/api/records?latest=1&symbol=${encodeURIComponent(sym)}`
        );
        if (sym !== this.detailSymbol || this.tab !== "detail") return;
        if (list.items.length) {
          // 记录没变就不重复拉全量/重画
          if (this.detailRecord && this._detailRecordName === list.items[0].name)
            return;
          const rec = await api(
            `/api/records/${encodeURIComponent(list.items[0].name)}`
          );
          if (sym !== this.detailSymbol || this.tab !== "detail") return;
          this.detailRecord = rec;
          this._detailRecordName = list.items[0].name;
          await nextTick();
          this.renderDetailChart();
        }
      } catch {
        /* 忽略刷新失败 */
      }
    },

    /* ── 配置 ── */
    async loadConfig() {
      const data = await api("/api/settings");
      this.masked = {
        api_key: data.provider.api_key,
        feishu_secret: data.feishu.secret,
        app_secret: data.feishu.app_secret,
        tushare_token: (data.tushare && data.tushare.token) || "",
      };
      // 秘密字段清空展示：留空提交 = 后端保留旧值
      data.provider.api_key = "";
      data.feishu.secret = "";
      data.feishu.app_secret = "";
      if (data.tushare) data.tushare.token = "";
      this.cfg = data;
    },
    async saveConfig() {
      this.busy.save = true;
      this.saveMsg = null;
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify(this.cfg) });
        this.saveMsg = { ok: true, text: "✅ 已保存（轮巡运行中则下一品种生效）" };
        await this.loadConfig();
      } catch (e) {
        this.saveMsg = { ok: false, text: "保存失败：" + e.message };
      } finally {
        this.busy.save = false;
      }
    },
    async testFeishu() {
      this.busy.feishu = true;
      this.feishuTestResult = null;
      try {
        // 直接用表单当前值测试（不必先保存）；secret 留空时后端回落已保存值
        this.feishuTestResult = await api("/api/feishu/test", {
          method: "POST",
          body: JSON.stringify({
            webhook_url: this.cfg.feishu.webhook_url,
            secret: this.cfg.feishu.secret,
          }),
        });
      } catch (e) {
        this.feishuTestResult = { ok: false, detail: e.message };
      } finally {
        this.busy.feishu = false;
      }
    },

    /* ── 历史 ── */
    async loadRecords() {
      try {
        const q = this.histFilter.trim() ? `?symbol=${encodeURIComponent(this.histFilter.trim())}` : "";
        this.records = (await api(`/api/records${q}`)).items;
      } catch (e) {
        this.showToast("历史加载失败：" + e.message, false);
      }
    },
    async openRecord(name) {
      try {
        this.detail = await api(`/api/records/${encodeURIComponent(name)}`);
        await nextTick();
        const el = this.$refs.drawerChartEl;
        if (el && this.detail.kline_data && this.detail.kline_data.length) {
          if (this._drawerChart) this._drawerChart.destroy();
          this._drawerChart = renderKlineChart(
            el,
            this.detail.kline_data,
            decisionOf(this.detail)
          );
        }
      } catch (e) {
        this.showToast("记录读取失败：" + e.message, false);
      }
    },
    closeDrawer() {
      if (this._drawerChart) {
        this._drawerChart.destroy();
        this._drawerChart = null;
      }
      this.detail = null;
    },
  },
  mounted() {
    this.pollStatus();
    this._pollTimer = setInterval(() => {
      if (!document.hidden) this.pollStatus();
    }, 2000);
    this._tickTimer = setInterval(() => {
      if (this.countdown !== null && this.countdown > 0) this.countdown -= 1;
    }, 1000);
  },
  beforeUnmount() {
    clearInterval(this._pollTimer);
    clearInterval(this._tickTimer);
    this.stopLivePoll();
  },
}).mount("#app");
