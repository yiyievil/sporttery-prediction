// charts.js — code-review-sporttery 报告图表
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var ok = style.getPropertyValue('--ok').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var fontFamily = "'Noto Sans CJK SC','WenQuanYi Micro Hei','Microsoft YaHei',sans-serif";

  var textStyle = { fontFamily: fontFamily };

  // ---- Chart 1: severity distribution ----
  var c1 = echarts.init(document.getElementById('chart-severity'), null, { renderer: 'svg' });
  c1.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 30, bottom: 40 },
    xAxis: { type: 'category', data: ['已修复', '严重', '中等', '轻微'], axisLabel: textStyle, axisLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'value', axisLabel: textStyle, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar',
      barWidth: 52,
      data: [
        { value: 3, itemStyle: { color: ok } },
        { value: 9, itemStyle: { color: accent2 } },
        { value: 28, itemStyle: { color: warn } },
        { value: 35, itemStyle: { color: muted } }
      ],
      label: { show: true, position: 'top', color: ink, fontFamily: textStyle.fontFamily }
    }]
  });
  window.addEventListener('resize', function () { c1.resize(); });

  // ---- Chart 2: by module ----
  var c2 = echarts.init(document.getElementById('chart-module'), null, { renderer: 'svg' });
  c2.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 110, right: 30, top: 10, bottom: 30 },
    xAxis: { type: 'value', axisLabel: textStyle, splitLine: { lineStyle: { color: rule } } },
    yAxis: {
      type: 'category',
      data: ['记忆/回测/其他', '报告生成 gen_*', 'SWOT 模块', '采集脚本', 'v215_update', 'v215_simulate', 'v215_verify', 'v215_e2e'],
      axisLabel: textStyle,
      axisLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'bar',
      data: [
        { value: 17, itemStyle: { color: muted } },
        { value: 5, itemStyle: { color: warn } },
        { value: 7, itemStyle: { color: warn } },
        { value: 12, itemStyle: { color: accent } },
        { value: 5, itemStyle: { color: accent } },
        { value: 4, itemStyle: { color: accent } },
        { value: 14, itemStyle: { color: accent2 } },
        { value: 16, itemStyle: { color: accent2 } }
      ],
      label: { show: true, position: 'right', color: ink, fontFamily: textStyle.fontFamily }
    }]
  });
  window.addEventListener('resize', function () { c2.resize(); });

  // ---- Chart 3: heatmap module x severity ----
  var c3 = echarts.init(document.getElementById('chart-heat'), null, { renderer: 'svg' });
  var mods = ['v215_e2e', 'v215_verify', 'v215_simulate', 'v215_update', '采集脚本', 'SWOT', '报告生成', '其他'];
  var sevs = ['严重', '中等', '轻微'];
  var data = [
    [0, 0, 3], [0, 1, 7], [0, 2, 6],
    [1, 0, 2], [1, 1, 4], [1, 2, 8],
    [2, 0, 0], [2, 1, 2], [2, 2, 2],
    [3, 0, 0], [3, 1, 4], [3, 2, 1],
    [4, 0, 2], [4, 1, 5], [4, 2, 5],
    [5, 0, 0], [5, 1, 4], [5, 2, 3],
    [6, 0, 0], [6, 1, 2], [6, 2, 3],
    [7, 0, 2], [7, 1, 5], [7, 2, 7]
  ];
  c3.setOption({
    animation: false,
    tooltip: { appendToBody: true, formatter: function (p) { return mods[p.value[0]] + ' / ' + sevs[p.value[1]] + '：' + p.value[2] + ' 项'; } },
    grid: { left: 90, right: 40, top: 30, bottom: 60 },
    xAxis: { type: 'category', data: sevs, splitArea: { show: false }, axisLabel: textStyle, axisLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'category', data: mods, splitArea: { show: false }, axisLabel: textStyle, axisLine: { lineStyle: { color: rule } } },
    visualMap: {
      min: 0, max: 8,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      inRange: { color: [bg2, accent2] },
      textStyle: textStyle
    },
    series: [{
      type: 'heatmap',
      data: data,
      label: { show: true, color: ink, fontFamily: textStyle.fontFamily },
      itemStyle: { borderColor: '#fff', borderWidth: 1 }
    }]
  });
  window.addEventListener('resize', function () { c3.resize(); });

  // ---- Chart 4: phase allocation ----
  var c4 = echarts.init(document.getElementById('chart-phase'), null, { renderer: 'svg' });
  c4.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['严重', '中等', '治理项'], textStyle: textStyle, top: 0 },
    grid: { left: 50, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: ['Phase 1 止崩', 'Phase 2 防错', 'Phase 3 治理'], axisLabel: textStyle, axisLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'value', axisLabel: textStyle, splitLine: { lineStyle: { color: rule } } },
    series: [
      { name: '严重', type: 'bar', stack: 't', barWidth: 56, data: [4, 2, 3], itemStyle: { color: accent2 }, label: { show: true, color: ink, fontFamily: textStyle.fontFamily } },
      { name: '中等', type: 'bar', stack: 't', data: [6, 12, 10], itemStyle: { color: warn }, label: { show: true, color: ink, fontFamily: textStyle.fontFamily } },
      { name: '治理项', type: 'bar', stack: 't', data: [0, 0, 8], itemStyle: { color: accent }, label: { show: true, color: ink, fontFamily: textStyle.fontFamily } }
    ]
  });
  window.addEventListener('resize', function () { c4.resize(); });
})();
