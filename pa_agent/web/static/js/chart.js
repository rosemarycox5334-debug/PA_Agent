export function createChart(container) {
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 600;
    const chart = LightweightCharts.createChart(container, {
        width, height,
        layout: { background: { color: "#0d1117" }, textColor: "#e6edf3" },
        grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#30363d" },
        timeScale: { borderColor: "#30363d" },
    });

    const candleSeries = chart.addCandlestickSeries({
        upColor: "#00d084", downColor: "#ff4757",
        borderUpColor: "#00d084", borderDownColor: "#ff4757",
        wickUpColor: "#00d084", wickDownColor: "#ff4757",
    });

    const volSeries = chart.addHistogramSeries({
        color: "#26a69a", priceFormat: { type: "volume" },
        priceScaleId: "", scaleMargins: { top: 0.8, bottom: 0 },
    });

    let first = true;

    return {
        update(frame) {
            // bars from backend: index 0 = newest
            // Lightweight Charts needs oldest first
            const bars = frame.bars.slice().reverse();
            const candleData = bars.map(b => ({
                time: b.ts_open / 1000,
                open: b.open, high: b.high, low: b.low, close: b.close,
            }));
            const volData = bars.map(b => ({
                time: b.ts_open / 1000,
                value: b.volume,
                color: b.close >= b.open ? "#00d08480" : "#ff475780",
            }));
            if (first) {
                candleSeries.setData(candleData);
                volSeries.setData(volData);
                chart.timeScale().fitContent();
                first = false;
            } else {
                // Incremental update: only push the newest bar
                const last = candleData[candleData.length - 1];
                const lastVol = volData[volData.length - 1];
                candleSeries.update(last);
                volSeries.update(lastVol);
            }
        },
        resize() {
            chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
        },
        destroy() {
            chart.remove();
        },
    };
}
