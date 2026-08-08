(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  function init(id) {
    return echarts.init(document.getElementById(id), null, { renderer: 'svg' });
  }
  function reg(chart, id) {
    window.addEventListener('resize', function() { chart.resize(); });
  }
  function pct(v) {
    return v.toFixed(0) + '%';
  }

  // --- HAD 方向命中率 (bar) ---
  var c1 = init('chart-had-dir');
  c1.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, formatter: function(p){ return p[0].name + '<br/>命中率: ' + p[0].data + '%'; } },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['胜', '平', '负'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 80, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [52, 33, 61], barWidth: 40,
      itemStyle: { color: function(p){ return p.dataIndex === 1 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c1, 'chart-had-dir');

  // --- HHAD 方向命中率 (bar) ---
  var c2 = init('chart-hhad-dir');
  c2.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['让胜', '让负', '让平(预测)'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 80, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [48, 50, 0], barWidth: 40,
      itemStyle: { color: function(p){ return p.dataIndex === 2 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data === 0 ? '0 预测' : p.data + '%'; }, color: ink }
    }]
  });
  reg(c2, 'chart-hhad-dir');

  // --- HHAD 混淆 (堆叠条) ---
  var c3 = init('chart-hhad-conf');
  c3.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' } },
    legend: { data: ['实际让胜', '实际让平', '实际让负'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 60, right: 20, top: 20, bottom: 40 },
    xAxis: { type: 'value', axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'category', data: ['预测让胜', '预测让负'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    series: [
      { name: '实际让胜', type: 'bar', stack: 't', data: [20, 18], itemStyle: { color: accent }, label: { show: true, color: ink } },
      { name: '实际让平', type: 'bar', stack: 't', data: [11, 10], itemStyle: { color: accent2 }, label: { show: true, color: ink } },
      { name: '实际让负', type: 'bar', stack: 't', data: [11, 8], itemStyle: { color: muted }, label: { show: true, color: ink } }
    ]
  });
  reg(c3, 'chart-hhad-conf');

  // --- 让平出现率 vs 预测 (双柱) ---
  var c4 = init('chart-letdraw');
  c4.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['实际出现的让平率', '模型预测让平的占比'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['让球盘', '受让盘', '全部'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 40, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [
      { name: '实际出现的让平率', type: 'bar', data: [22, 35, 25], barWidth: 28, itemStyle: { color: accent2, borderRadius: [4,4,0,0] }, label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink } },
      { name: '模型预测让平的占比', type: 'bar', data: [0, 0, 0], barWidth: 28, itemStyle: { color: accent }, label: { show: true, position: 'top', formatter: '0%', color: ink } }
    ]
  });
  reg(c4, 'chart-letdraw');

  // --- HAD 赔率档位 ---
  var c5 = init('chart-had-odds');
  c5.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['<1.5', '1.5-2.0', '2.0-2.8', '2.8-4.0'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 70, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [52, 46, 50, 33], barWidth: 34,
      itemStyle: { color: function(p){ return p.data < 45 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c5, 'chart-had-odds');

  // --- HHAD 赔率档位 ---
  var c6 = init('chart-hhad-odds');
  c6.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['<1.5', '1.5-2.0', '2.0-2.8', '2.8-4.0'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 70, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [33, 55, 42, 29], barWidth: 34,
      itemStyle: { color: function(p){ return p.data < 40 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c6, 'chart-hhad-odds');

  // --- 难度分档 ---
  var c7 = init('chart-diff');
  c7.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['低(<45)', '中(45-65)', '高(>65)'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 70, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [31, 50, 54], barWidth: 40,
      itemStyle: { color: function(p){ return p.data < 45 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c7, 'chart-diff');

  // --- 模型一致性 ---
  var c8 = init('chart-agree');
  c8.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['低(<0.7)', '中(0.7-0.9)', '高(≥0.9)'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 70, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [36, 50, 51], barWidth: 40,
      itemStyle: { color: function(p){ return p.data < 40 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c8, 'chart-agree');

  // --- 联赛 (横向bar) ---
  var c9 = init('chart-league');
  var leagues = [['挪超',69],['英联赛杯',75],['欧冠',50],['瑞超',43],['芬超',43],['欧罗巴',33],['韩职',33],['巴西杯',20],['美职',17]];
  c9.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' } },
    grid: { left: 70, right: 40, top: 10, bottom: 30 },
    xAxis: { type: 'value', max: 90, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'category', data: leagues.map(function(d){ return d[0]; }).reverse(), axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    series: [{
      type: 'bar', data: leagues.map(function(d){ return d[1]; }).reverse(), barWidth: 16,
      itemStyle: { color: function(p){ return p.value < 40 ? accent2 : accent; }, borderRadius: [0,4,4,0] },
      label: { show: true, position: 'right', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c9, 'chart-league');

  // --- 主推玩法 ---
  var c10 = init('chart-pb');
  c10.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['命中率', '场次'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['HAD胜', 'HHAD让负', 'HHAD让胜', 'HAD负'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: [
      { type: 'value', name: '命中率%', max: 100, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } }, nameTextStyle: { color: muted } },
      { type: 'value', name: '场次', max: 40, axisLabel: { color: muted }, splitLine: { show: false }, nameTextStyle: { color: muted } }
    ],
    series: [
      { name: '命中率', type: 'bar', data: [51, 42, 61, 83], yAxisIndex: 0, barWidth: 22, itemStyle: { color: function(p){ return p.data < 50 ? accent2 : accent; }, borderRadius: [4,4,0,0] }, label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink } },
      { name: '场次', type: 'line', data: [35, 19, 18, 6], yAxisIndex: 1, itemStyle: { color: muted }, lineStyle: { color: muted, type: 'dashed' }, symbol: 'circle' }
    ]
  });
  reg(c10, 'chart-pb');

  // --- 11. 热量陷阱: 置信度 × 低赔 (主推) ---
  var c11 = init('chart-heat-trap');
  c11.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['命中率', '累计 ROI'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['高置信≥4★+低赔', '中置信3-4★+低赔', '低置信<3★+低赔'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink, interval: 0, fontSize: 11 } },
    yAxis: [
      { type: 'value', name: '命中率%', max: 80, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } }, nameTextStyle: { color: muted } },
      { type: 'value', name: 'ROI', axisLabel: { color: muted }, splitLine: { show: false }, nameTextStyle: { color: muted } }
    ],
    series: [
      { name: '命中率', type: 'bar', data: [29, 55, 60], yAxisIndex: 0, barWidth: 30, itemStyle: { color: function(p){ return p.dataIndex === 0 ? accent2 : accent; }, borderRadius: [4,4,0,0] }, label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink } },
      { name: '累计 ROI', type: 'line', data: [-4.7, 3.9, 0.4], yAxisIndex: 1, itemStyle: { color: '#b91c1c' }, lineStyle: { color: '#b91c1c', width: 2 }, symbol: 'circle' }
    ]
  });
  reg(c11, 'chart-heat-trap');

  // --- 12. 概率校准: 预测p vs 实际命中率 ---
  var c12 = init('chart-calibration');
  c12.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['实际命中率', '理想校准线'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['30-40%', '40-50%', '50-60%', '60-70%', '≥70%'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'value', max: 100, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [
      { name: '实际命中率', type: 'bar', data: [55, 53, 55, 42, 100], barWidth: 32, itemStyle: { color: function(p){ return p.dataIndex === 3 ? accent2 : accent; }, borderRadius: [4,4,0,0] }, label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink } },
      { name: '理想校准线', type: 'line', data: [35, 45, 55, 65, 75], itemStyle: { color: muted }, lineStyle: { color: muted, type: 'dashed' }, symbol: 'none' }
    ]
  });
  reg(c12, 'chart-calibration');

  // --- 13. 主推玩法 × 赔率 黄金窗口 (热力图) ---
  var c13 = init('chart-win-matrix');
  var mData = [
    ['HAD胜', '低赔<1.5', 58, -4.4], ['HAD胜', '1.5-2.0', 43, -4.2],
    ['HHAD让负', '低赔<1.5', 75, 5.6], ['HHAD让负', '1.5-2.0', 33, -1.2],
    ['HHAD让胜', '低赔<1.5', 33, -0.7], ['HHAD让胜', '1.5-2.0', 64, -1.6],
    ['HAD负', '低赔<1.5', 100, 1.7]
  ];
  c13.setOption({
    animation: false,
    tooltip: { appendToBody: true, formatter: function(p){ return p.name + '<br/>命中率: ' + p.value[2] + '%<br/>ROI: ' + p.value[3]; } },
    grid: { left: 90, right: 30, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['低赔<1.5', '1.5-2.0', '2.0-2.8'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    yAxis: { type: 'category', data: ['HAD负', 'HHAD让胜', 'HHAD让负', 'HAD胜'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink } },
    visualMap: { show: false, min: 30, max: 100, inRange: { color: [accent2, '#fbbf24', accent] } },
    series: [{
      type: 'scatter', symbolSize: function(v){ return 40 + v[2]; },
      data: mData.map(function(d){ return { name: d[0] + ' ' + d[1], value: [d[1], d[0], d[2], d[3]] }; }),
      itemStyle: { color: function(p){ return p.value[2] >= 60 ? accent : (p.value[2] < 45 ? accent2 : '#fbbf24'); }, opacity: 0.85 },
      label: { show: true, formatter: function(p){ return p.value[2] + '%'; }, color: ink, fontSize: 11 }
    }]
  });
  reg(c13, 'chart-win-matrix');

  // --- 14. 让平根因: 受让/难度 × 让平率 ---
  var c14 = init('chart-draw-root');
  c14.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    legend: { data: ['实际让平率', '让平漏判场次'], textStyle: { color: muted }, bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: ['受让盘', '让球盘', '难度中', '难度低', '难度高'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink, interval: 0 } },
    yAxis: [
      { type: 'value', name: '让平率%', max: 50, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } }, nameTextStyle: { color: muted } },
      { type: 'value', name: '场次', axisLabel: { color: muted }, splitLine: { show: false }, nameTextStyle: { color: muted } }
    ],
    series: [
      { name: '实际让平率', type: 'bar', data: [36, 23, 44, 18, 14], yAxisIndex: 0, barWidth: 28, itemStyle: { color: function(p){ return p.data >= 35 ? accent2 : accent; }, borderRadius: [4,4,0,0] }, label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink } },
      { name: '让平漏判场次', type: 'line', data: [8, 13, 14, 2, 5], yAxisIndex: 1, itemStyle: { color: muted }, lineStyle: { color: muted, type: 'dashed' }, symbol: 'circle' }
    ]
  });
  reg(c14, 'chart-draw-root');

  // --- 15. 半全场 / 大小球 ---
  var c15 = init('chart-hf-tg');
  c15.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: ['半全场总体', '胜胜', '负负', '平平', '大小球总体'], axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink, interval: 0, fontSize: 11 } },
    yAxis: { type: 'value', max: 50, axisLabel: { color: muted, formatter: '{value}%' }, splitLine: { lineStyle: { color: rule } } },
    series: [{
      type: 'bar', data: [32, 37, 24, 17, 36], barWidth: 34,
      itemStyle: { color: function(p){ return p.data < 25 ? accent2 : accent; }, borderRadius: [4,4,0,0] },
      label: { show: true, position: 'top', formatter: function(p){ return p.data + '%'; }, color: ink }
    }]
  });
  reg(c15, 'chart-hf-tg');
})();