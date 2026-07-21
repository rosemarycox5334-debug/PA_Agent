/* PA Agent 控制台前端（Vue 3 无构建单页） */
import { createApp } from "./vendor/vue.esm-browser.prod.js";

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
      status: { scheduler: { running: false, error: null }, current: null, round_wait_eta: null, results: {}, events: [] },
      countdown: null,
      cfg: null,
      masked: {},
      records: [],
      histFilter: "",
      detail: null,
      feishuTestResult: null,
      saveMsg: null,
      toast: null,
      busy: { watch: false, save: false, feishu: false },
      _pollTimer: null,
      _tickTimer: null,
    };
  },
  computed: {
    running() {
      return this.status.scheduler.running;
    },
    eventsDesc() {
      return [...this.status.events].reverse();
    },
    countdownText() {
      if (this.countdown === null) return "";
      const s = Math.max(0, Math.floor(this.countdown));
      return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")} 后开始下一轮`;
    },
    detailDecision() {
      return (this.detail && this.detail.stage2_decision && this.detail.stage2_decision.decision) || null;
    },
    decisionRows() {
      const d = this.detailDecision;
      if (!d) return [];
      return DECISION_FIELDS.map(([label, key]) => [label, d[key] ?? "—"]);
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
      this.tab = key;
      if (key === "config" && !this.cfg) this.loadConfig();
      if (key === "history") this.loadRecords();
    },

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
        this.feishuTestResult = await api("/api/feishu/test", { method: "POST" });
      } catch (e) {
        this.feishuTestResult = { ok: false, detail: e.message };
      } finally {
        this.busy.feishu = false;
      }
    },

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
      } catch (e) {
        this.showToast("记录读取失败：" + e.message, false);
      }
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
  },
}).mount("#app");
