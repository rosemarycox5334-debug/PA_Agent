/* K 线图组件：lightweight-charts 封装（蜡烛 + EMA20 + 决策价位线） */

const DARK = {
  layout: { background: { color: "#1b1e27" }, textColor: "#8b91a3" },
  grid: {
    vertLines: { color: "#2e3342" },
    horzLines: { color: "#2e3342" },
  },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2e3342" },
  rightPriceScale: { borderColor: "#2e3342" },
  crosshair: { mode: 0 },
};

function ema20(closes) {
  const a = 2 / 21;
  const out = [];
  closes.forEach((c, i) => {
    out.push(i === 0 ? c : a * c + (1 - a) * out[i - 1]);
  });
  return out;
}

/**
 * 渲染 K 线图。
 * @param {HTMLElement} el 容器
 * @param {Array} klineData 记录里的 kline_data（seq=1 为最新，ts_open 毫秒）
 * @param {Object|null} decision stage2 decision 内层（价位线来源，可空）
 * @returns {{destroy: Function}}
 */
export function renderKlineChart(el, klineData, decision) {
  el.innerHTML = "";
  const chart = LightweightCharts.createChart(el, {
    ...DARK,
    width: el.clientWidth || 640,
    height: 360,
  });
  // seq=1 最新 → 图表需要 oldest-first；time 用秒。
  // lightweight-charts 按 UTC 渲染刻度：加本地时区偏移使轴显示本地钟点
  const tzOff = -new Date().getTimezoneOffset() * 60;
  const bars = [...klineData].reverse().map((b) => ({
    time: Math.floor(b.ts_open / 1000) + tzOff,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
  }));
  const candles = chart.addCandlestickSeries({
    upColor: "#34c98e",
    downColor: "#f05c5c",
    wickUpColor: "#34c98e",
    wickDownColor: "#f05c5c",
    borderVisible: false,
  });
  candles.setData(bars);

  const emaVals = ema20(bars.map((b) => b.close));
  const emaSeries = chart.addLineSeries({
    color: "#4a80f0",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  emaSeries.setData(bars.map((b, i) => ({ time: b.time, value: emaVals[i] })));

  const lines = [
    ["entry_price", "入场", "#4a80f0"],
    ["stop_loss_price", "止损", "#f05c5c"],
    ["take_profit_price", "TP1", "#34c98e"],
    ["take_profit_price_2", "TP2", "#34c98e"],
  ];
  if (decision) {
    for (const [key, title, color] of lines) {
      const v = Number(decision[key]);
      if (Number.isFinite(v) && v > 0) {
        candles.createPriceLine({
          price: v,
          color,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title,
        });
      }
    }
  }
  chart.timeScale().fitContent();

  const onResize = () => chart.applyOptions({ width: el.clientWidth || 640 });
  window.addEventListener("resize", onResize);
  return {
    destroy() {
      window.removeEventListener("resize", onResize);
      chart.remove();
    },
  };
}
