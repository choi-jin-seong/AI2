let chartInstances = [];
let currentAnalysis = null;
let currentPerLogAi = [];
let lastUploadedFile = null;
let isLoading = false;
let loadingStepTimer = null;

const ANALYSIS_LOADING_STEPS = [
  {
    title: "로그 분석을 준비하고 있습니다",
    message: "업로드한 ZIP 파일을 확인하고\n분석에 필요한 항목을 읽는 중입니다.",
    step: "1/3 ZIP 파일 구조 확인"
  },
  {
    title: "로그 패턴을 정리하고 있습니다",
    message: "장비별 지표를 추출하고 비교할 수 있게\n데이터를 정리하고 있습니다.",
    step: "2/3 장비 로그 해석"
  },
  {
    title: "결과 화면을 만드는 중입니다",
    message: "요약 정보와 차트, 판정 결과를\n화면에 보여줄 준비를 하고 있습니다.",
    step: "3/3 결과 시각화 준비"
  }
];

const RERENDER_LOADING_STEPS = [
  {
    title: "정렬 기준을 반영하고 있습니다",
    message: "선택한 조건에 맞춰 결과 순서를 다시 맞추는 중입니다.",
    step: "1/2 결과 재정렬"
  },
  {
    title: "화면을 새로 그리는 중입니다",
    message: "차트와 표를 다시 배치해서 최신 결과를 보여주고 있습니다.",
    step: "2/2 화면 갱신"
  }
];

function setStatus(message) {
  document.getElementById("statusArea").textContent = message;
}

function clearLoadingStepTimer() {
  if (loadingStepTimer) {
    clearInterval(loadingStepTimer);
    loadingStepTimer = null;
  }
}

function updateLoadingView(stepConfig, stepIndex, totalSteps) {
  const loadingEyebrow = document.getElementById("loadingEyebrow");
  const loadingTitle = document.getElementById("loadingTitle");
  const loadingMessage = document.getElementById("loadingMessage");
  const loadingStep = document.getElementById("loadingStep");
  const loadingProgressFill = document.getElementById("loadingProgressFill");

  if (loadingEyebrow) {
    loadingEyebrow.textContent = totalSteps > 1 ? `ANALYSIS STEP ${stepIndex + 1}` : "RRU QUALITY ANALYZER";
  }

  if (loadingTitle) {
    loadingTitle.textContent = stepConfig?.title || "로그 분석 중";
  }

  if (loadingMessage) {
    loadingMessage.textContent = stepConfig?.message || "잠시만 기다려 주세요.";
  }

  if (loadingStep) {
    loadingStep.textContent = stepConfig?.step || "분석 진행 중";
  }

  if (loadingProgressFill) {
    const ratio = totalSteps > 0 ? ((stepIndex + 1) / totalSteps) * 100 : 20;
    loadingProgressFill.style.width = `${Math.max(18, ratio)}%`;
  }
}

function setLoading(loading, options = {}) {
  isLoading = loading;
  clearLoadingStepTimer();

  const overlay = document.getElementById("loadingOverlay");
  const analyzeBtn = document.getElementById("analyzeBtn");
  const resetBtn = document.getElementById("resetBtn");
  const sortMode = document.getElementById("sortMode");
  const zipFileInput = document.getElementById("zipFileInput");

  if (overlay) {
    overlay.classList.toggle("hidden", !loading);
  }

  document.body.classList.toggle("loading-active", loading);

  if (analyzeBtn) analyzeBtn.disabled = loading;
  if (resetBtn) resetBtn.disabled = loading;
  if (sortMode) sortMode.disabled = loading;
  if (zipFileInput) zipFileInput.disabled = loading;

  if (!loading) {
    updateLoadingView(
      {
        title: "로그 분석 중",
        message: "잠시만 기다려 주세요.",
        step: "분석 대기"
      },
      0,
      1
    );
    return;
  }

  const steps = Array.isArray(options.steps) && options.steps.length
    ? options.steps
    : [{
        title: options.title || "로그 분석 중",
        message: options.message || "잠시만 기다려 주세요.",
        step: "분석 진행 중"
      }];

  let currentStepIndex = 0;
  updateLoadingView(steps[currentStepIndex], currentStepIndex, steps.length);

  if (steps.length > 1) {
    loadingStepTimer = setInterval(() => {
      currentStepIndex = (currentStepIndex + 1) % steps.length;
      updateLoadingView(steps[currentStepIndex], currentStepIndex, steps.length);
    }, options.intervalMs || 1600);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
  return Number(value).toFixed(digits);
}

function destroyCharts() {
  for (const chart of chartInstances) {
    if (chart) chart.destroy();
  }
  chartInstances = [];
}

function statusClass(status) {
  if (status === "PASS") return "status-pass";
  if (status === "WARNING") return "status-warning";
  if (status === "FAIL") return "status-fail";
  return "status-na";
}

function renderStatusChip(status) {
  const text = status || "N/A";
  return `<span class="status-chip ${statusClass(text)}">${escapeHtml(text)}</span>`;
}

function getWorstReturnLoss(item) {
  const values = [item?.rl?.p0, item?.rl?.p1, item?.rl?.p2, item?.rl?.p3].filter(Number.isFinite);
  if (!values.length) return null;
  return Math.min(...values);
}

function getPerLogAiMap(perLogAi) {
  const map = new Map();
  for (const item of perLogAi || []) {
    map.set(item.displayId, item);
  }
  return map;
}

function renderSummary(analysis) {
  const cards = [
    { label: "전체 로그 수", value: analysis.sortedResults.length },
    { label: "DL Power 평균", value: safeNumber(analysis.dl.avg, 3) },
    { label: "DL Power 표준편차", value: safeNumber(analysis.dl.std, 3) },
    { label: "DL Power 범위", value: safeNumber(analysis.dl.range, 3) },
    { label: "UL Power 평균", value: safeNumber(analysis.ul.avg, 3) },
    { label: "UL Power 표준편차", value: safeNumber(analysis.ul.std, 3) },
    { label: "UL Power 범위", value: safeNumber(analysis.ul.range, 3) },
    { label: "VSWR P0 평균", value: safeNumber(analysis.vswr.p0Avg, 2) },
    { label: "VSWR P1 평균", value: safeNumber(analysis.vswr.p1Avg, 2) },
    { label: "DTU 평균 온도", value: safeNumber(analysis.temp.dtuAvg, 1) + "C" },
    { label: "FPGA 평균 온도", value: safeNumber(analysis.temp.fpgaAvg, 1) + "C" },
    { label: "RFU0 평균 온도", value: safeNumber(analysis.temp.rfu0Avg, 1) + "C" },
    { label: "RFU1 평균 온도", value: safeNumber(analysis.temp.rfu1Avg, 1) + "C" }
  ];

  document.getElementById("summaryGrid").innerHTML = cards.map(card => `
    <div class="summary-card">
      <div class="label">${escapeHtml(card.label)}</div>
      <div class="value">${escapeHtml(card.value)}</div>
    </div>
  `).join("");
}

function isDbAnalysis(analysis) {
  return Boolean(
    analysis &&
    analysis.sortedResults &&
    analysis.sortedResults.some(item => item.sourceType === "db_xls" || item.db)
  );
}

function ensureDbAnalysisPanel() {
  let panel = document.getElementById("dbAnalysisPanel");
  if (panel) return panel;

  panel = document.createElement("div");
  panel.className = "panel";
  panel.id = "dbAnalysisPanel";

  panel.innerHTML = `
    <h2>DB 성적서 기반 불량/정상 비교 분석</h2>
    <div id="dbSummaryGrid" class="summary-grid"></div>

    <div style="overflow-x:auto; margin-top:16px;">
      <table>
        <thead>
          <tr>
            <th>항목</th>
            <th>불량군 평균</th>
            <th>정상군 평균</th>
            <th>차이</th>
            <th>정상군 σ</th>
            <th>불량군 평균 z-score</th>
            <th>판단</th>
          </tr>
        </thead>
        <tbody id="dbCompareTableBody"></tbody>
      </table>
    </div>
  `;

  const aiSummaryPanel = document.getElementById("aiSummaryArea")?.closest(".panel");
  const resultSection = document.getElementById("resultSection");

  if (resultSection && aiSummaryPanel) {
    resultSection.insertBefore(panel, aiSummaryPanel);
  } else if (resultSection) {
    resultSection.prepend(panel);
  }

  return panel;
}

function renderDbAnalysisPanel(analysis) {
  if (!isDbAnalysis(analysis)) {
    const oldPanel = document.getElementById("dbAnalysisPanel");
    if (oldPanel) oldPanel.remove();
    return;
  }

  ensureDbAnalysisPanel();

  const db = analysis.db || {};
  const groupCompare = db.groupCompare || {};
  const metrics = groupCompare.metrics || {};

  const faultCount = groupCompare.faultCount ?? 0;
  const normalCount = groupCompare.normalCount ?? 0;

  const summaryCards = [
    { label: "불량군 DB 수", value: faultCount },
    { label: "정상군 DB 수", value: normalCount },
    { label: "분석 방식", value: "정상군 대비 상대 편차" },
    { label: "주요 기준", value: "Power 낮음 / VSWR 높음" }
  ];

  const summaryGrid = document.getElementById("dbSummaryGrid");
  if (summaryGrid) {
    summaryGrid.innerHTML = summaryCards.map(card => `
      <div class="summary-card">
        <div class="label">${escapeHtml(card.label)}</div>
        <div class="value">${escapeHtml(String(card.value))}</div>
      </div>
    `).join("");
  }

  const tbody = document.getElementById("dbCompareTableBody");
  if (!tbody) return;

  const order = [
    "dl0_power",
    "dl1_power",
    "dl0_vswr",
    "ul0_vswr",
    "dl1_vswr",
    "ul1_vswr"
  ];

  tbody.innerHTML = order.map(key => {
    const row = metrics[key] || {};
    const z = row.zOfFaultAvg;

    let judge = "정상 범위";
    let judgeClass = "status-pass";

    if (Number.isFinite(z)) {
      if (
        (key.includes("power") && z <= -3.0) ||
        (key.includes("vswr") && z >= 3.0)
      ) {
        judge = "강한 이상 편차";
        judgeClass = "status-fail";
      } else if (
        (key.includes("power") && z <= -2.0) ||
        (key.includes("vswr") && z >= 2.0)
      ) {
        judge = "주의 편차";
        judgeClass = "status-warning";
      }
    }

    return `
      <tr>
        <td>${escapeHtml(row.label || key)}</td>
        <td>${safeNumber(row.faultAvg, 4)}</td>
        <td>${safeNumber(row.normalAvg, 4)}</td>
        <td>${safeNumber(row.diff, 4)}</td>
        <td>${safeNumber(row.normalStd, 4)}</td>
        <td>${safeNumber(z, 2)}</td>
        <td><span class="status-chip ${judgeClass}">${escapeHtml(judge)}</span></td>
      </tr>
    `;
  }).join("");
}

function renderAiSummary(aiSummary) {
  const area = document.getElementById("aiSummaryArea");
  if (!area) return;

  if (!aiSummary) {
    area.innerHTML = "<div>규칙 기반 분석 결과 없음</div>";
    return;
  }

  let badgeClass = "ok";
  if (aiSummary.overall === "주의") badgeClass = "warn";
  if (aiSummary.overall === "점검 필요") badgeClass = "danger";

  const summaryHtml = (aiSummary.summary || [])
    .map(v => `<li>${escapeHtml(v)}</li>`)
    .join("");

  const risksHtml = (aiSummary.risks || [])
    .map(v => `<li>${escapeHtml(v)}</li>`)
    .join("");

  area.innerHTML = `
    <div class="ai-badge ${badgeClass}">종합 판정: ${escapeHtml(aiSummary.overall || "N/A")}</div>
    <div style="margin-bottom:12px;">
      <b>요약</b>
      <ul class="compact-list">${summaryHtml || "<li>없음</li>"}</ul>
    </div>
    <div>
      <b>점검 포인트</b>
      <ul class="compact-list">${risksHtml || "<li>없음</li>"}</ul>
    </div>
  `;
}

function renderOpenAiSolution(solution) {
  const area = document.getElementById("openAiSolutionArea");
  if (!area) return;

  if (!solution) {
    area.innerHTML = "<div>GEMINI 결과 없음</div>";
    return;
  }

  if (solution.status === "READY") {
    area.innerHTML = `
      <div class="ai-badge ok">GEMINI 분석 완료 / Model: ${escapeHtml(solution.model || "N/A")}</div>
      <div class="markdown-like">${escapeHtml(solution.content || "")}</div>
    `;
    return;
  }

  if (solution.status === "UNAVAILABLE") {
    area.innerHTML = `
      <div class="ai-badge warn">GEMINI 분석 미수행</div>
      <div class="markdown-like">${escapeHtml(solution.error || "사유 없음")}</div>
    `;
    return;
  }

  area.innerHTML = `
    <div class="ai-badge danger">GEMINI 분석 오류</div>
    <div class="markdown-like">${escapeHtml(solution.error || "오류 내용 없음")}</div>
  `;
}

function renderPerLogAi(perLogAi) {
  const tbody = document.getElementById("perLogAiTableBody");
  if (!tbody) return;

  if (!perLogAi || !perLogAi.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="15">결과 없음</td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = perLogAi.map(item => {
    const levelStyle =
      item.level === "HIGH" ? "color:#dc2626;font-weight:bold;" :
      item.level === "MEDIUM" ? "color:#d97706;font-weight:bold;" :
      item.level === "LOW" ? "color:#2563eb;font-weight:bold;" :
      "color:#059669;font-weight:bold;";

    const sb = item.statusByItem || {};

    const issues = (item.issues || []).map(v => `<li>${escapeHtml(v)}</li>`).join("");
    const causes = (item.causes || []).map(v => `<li>${escapeHtml(v)}</li>`).join("");
    const checks = (item.checks || []).map(v => `<li>${escapeHtml(v)}</li>`).join("");

    return `
      <tr>
        <td>${escapeHtml(item.displayId)}</td>
        <td>${renderStatusChip(item.overallStatus)}</td>
        <td style="${levelStyle}">${escapeHtml(item.level)}</td>
        <td>${escapeHtml(String(item.score ?? 0))}</td>
        <td>${renderStatusChip(sb.dl_pwr?.status)}</td>
        <td>${renderStatusChip(sb.return_loss?.status)}</td>
        <td>${renderStatusChip(sb.ul_pwr?.status)}</td>
        <td>${renderStatusChip(sb.dtu_temp?.status)}</td>
        <td>${renderStatusChip(sb.rfu_temp?.status)}</td>
        <td>${renderStatusChip(sb.psu_in?.status)}</td>
        <td>${renderStatusChip(sb.sfp_tx?.status)}</td>
        <td>${renderStatusChip(sb.sfp_rx?.status)}</td>
        <td><ul class="compact-list">${issues || "<li>없음</li>"}</ul></td>
        <td><ul class="compact-list">${causes || "<li>없음</li>"}</ul></td>
        <td><ul class="compact-list">${checks || "<li>없음</li>"}</ul></td>
      </tr>
    `;
  }).join("");
}

function calculateBarAxisRange(values) {
  if (!values.length) {
    return { yMin: undefined, yMax: undefined };
  }

  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);

  let yMin = Math.floor((rawMin - 0.1) * 10) / 10;
  let yMax = Math.ceil((rawMax + 0.1) * 10) / 10;

  if ((yMax - yMin) < 0.3) {
    const center = (rawMin + rawMax) / 2;
    yMin = Math.floor((center - 0.15) * 10) / 10;
    yMax = Math.ceil((center + 0.15) * 10) / 10;
  }

  return { yMin, yMax };
}

function buildDlBarChartColors(analysis) {
  const targetValue = analysis.options.targetValue;
  const toleranceValue = analysis.options.toleranceValue;

  return analysis.sortedResults
    .filter(v => Number.isFinite(v.dlPwr))
    .map(item => {
      if (targetValue === null || toleranceValue === null) {
        return "rgba(37, 99, 235, 0.7)";
      }
      const delta = item.dlPwr - targetValue;
      if (Math.abs(delta) <= toleranceValue) {
        return "rgba(5, 150, 105, 0.7)";
      }
      return "rgba(220, 38, 38, 0.7)";
    });
}

function createDlBarChart(analysis) {
  const ctx = document.getElementById("dlBarChartCanvas").getContext("2d");
  const valid = analysis.sortedResults.filter(v => Number.isFinite(v.dlPwr));
  const labels = valid.map(v => v.displayId);
  const values = valid.map(v => v.dlPwr);
  const colors = buildDlBarChartColors(analysis);
  const { yMin, yMax } = calculateBarAxisRange(values);

  const datasets = [{
    label: "DL Power",
    data: values,
    backgroundColor: colors,
    borderColor: colors.map(c => c.replace("0.7", "1")),
    borderWidth: 1
  }];

  if (analysis.options.targetValue !== null) {
    datasets.push({
      type: "line",
      label: "Target",
      data: new Array(values.length).fill(analysis.options.targetValue),
      borderColor: "rgba(217, 119, 6, 1)",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0
    });
  }

  const chart = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true },
        tooltip: {
          callbacks: {
            label(context) {
              return `${context.dataset.label}: ${Number(context.parsed.y).toFixed(2)}`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: {
            autoSkip: false,
            maxRotation: 60,
            minRotation: 60
          }
        },
        y: {
          min: yMin,
          max: yMax,
          ticks: { stepSize: 0.1 },
          title: {
            display: true,
            text: "DL Power"
          }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function createDlHistogram(analysis) {
  const ctx = document.getElementById("dlHistChartCanvas").getContext("2d");
  const labels = analysis.dl.histogram.map(bin => `${Number(bin.start).toFixed(2)} ~ ${Number(bin.end).toFixed(2)}`);
  const counts = analysis.dl.histogram.map(bin => bin.count);

  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Count",
        data: counts,
        backgroundColor: "rgba(37, 99, 235, 0.7)",
        borderColor: "rgba(37, 99, 235, 1)",
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true }
      },
      scales: {
        x: {
          title: {
            display: true,
            text: "DL Power Bin"
          }
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: "Count"
          },
          ticks: { precision: 0 }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function createUlBarChart(analysis) {
  const ctx = document.getElementById("ulBarChartCanvas").getContext("2d");
  const valid = analysis.sortedResults.filter(v => Number.isFinite(v.ulPwr));
  const labels = valid.map(v => v.displayId);
  const values = valid.map(v => v.ulPwr);
  const { yMin, yMax } = calculateBarAxisRange(values);

  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "UL Power",
        data: values,
        backgroundColor: "rgba(16, 185, 129, 0.75)",
        borderColor: "rgba(16, 185, 129, 1)",
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true },
        tooltip: {
          callbacks: {
            label(context) {
              return `${context.dataset.label}: ${Number(context.parsed.y).toFixed(2)}`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: {
            autoSkip: false,
            maxRotation: 60,
            minRotation: 60
          }
        },
        y: {
          min: yMin,
          max: yMax,
          ticks: { stepSize: 0.1 },
          title: {
            display: true,
            text: "UL Power"
          }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function createUlHistogram(analysis) {
  const ctx = document.getElementById("ulHistChartCanvas").getContext("2d");
  const labels = analysis.ul.histogram.map(bin => `${Number(bin.start).toFixed(2)} ~ ${Number(bin.end).toFixed(2)}`);
  const counts = analysis.ul.histogram.map(bin => bin.count);

  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Count",
        data: counts,
        backgroundColor: "rgba(16, 185, 129, 0.75)",
        borderColor: "rgba(16, 185, 129, 1)",
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true }
      },
      scales: {
        x: {
          title: {
            display: true,
            text: "UL Power Bin"
          }
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: "Count"
          },
          ticks: { precision: 0 }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function createLineChart(canvasId, labels, datasets, yTitle) {
  const ctx = document.getElementById(canvasId).getContext("2d");

  const chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "index",
        intersect: false
      },
      plugins: {
        legend: {
          display: true,
          position: "top"
        },
        tooltip: {
          callbacks: {
            label(context) {
              const y = context.parsed.y;
              return `${context.dataset.label}: ${y == null ? "N/A" : Number(y).toFixed(2)}`;
            }
          }
        }
      },
      elements: {
        line: {
          tension: 0,
          borderWidth: 2
        },
        point: {
          radius: 2.5,
          hoverRadius: 4
        }
      },
      scales: {
        x: {
          ticks: {
            autoSkip: false,
            maxRotation: 60,
            minRotation: 60
          }
        },
        y: {
          title: {
            display: true,
            text: yTitle
          }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function renderVswrRlCharts(analysis) {
  const valid = analysis.sortedResults.filter(v => v.vswr !== null);
  const labels = valid.map(v => v.displayId);

  createLineChart(
    "vswrChart01",
    labels,
    [
      {
        label: "P0",
        data: valid.map(v => v.vswr?.p0 ?? null),
        borderColor: "rgba(37, 99, 235, 1)",
        backgroundColor: "rgba(37, 99, 235, 0.12)",
        pointBackgroundColor: "rgba(37, 99, 235, 1)",
        pointBorderColor: "rgba(37, 99, 235, 1)",
        fill: true
      },
      {
        label: "P1",
        data: valid.map(v => v.vswr?.p1 ?? null),
        borderColor: "rgba(107, 114, 128, 1)",
        backgroundColor: "rgba(107, 114, 128, 0.10)",
        pointBackgroundColor: "rgba(107, 114, 128, 1)",
        pointBorderColor: "rgba(107, 114, 128, 1)",
        fill: true
      }
    ],
    "VSWR"
  );

  createLineChart(
    "vswrChart23",
    labels,
    [
      {
        label: "P2",
        data: valid.map(v => v.vswr?.p2 ?? null),
        borderColor: "rgba(5, 150, 105, 1)",
        backgroundColor: "rgba(5, 150, 105, 0.12)",
        pointBackgroundColor: "rgba(5, 150, 105, 1)",
        pointBorderColor: "rgba(5, 150, 105, 1)",
        fill: true
      },
      {
        label: "P3",
        data: valid.map(v => v.vswr?.p3 ?? null),
        borderColor: "rgba(220, 38, 38, 1)",
        backgroundColor: "rgba(220, 38, 38, 0.10)",
        pointBackgroundColor: "rgba(220, 38, 38, 1)",
        pointBorderColor: "rgba(220, 38, 38, 1)",
        fill: true
      }
    ],
    "VSWR"
  );

  createLineChart(
    "rlChart01",
    labels,
    [
      {
        label: "P0",
        data: valid.map(v => v.rl?.p0 ?? null),
        borderColor: "rgba(37, 99, 235, 1)",
        backgroundColor: "rgba(37, 99, 235, 0.12)",
        pointBackgroundColor: "rgba(37, 99, 235, 1)",
        pointBorderColor: "rgba(37, 99, 235, 1)",
        fill: true
      },
      {
        label: "P1",
        data: valid.map(v => v.rl?.p1 ?? null),
        borderColor: "rgba(107, 114, 128, 1)",
        backgroundColor: "rgba(107, 114, 128, 0.10)",
        pointBackgroundColor: "rgba(107, 114, 128, 1)",
        pointBorderColor: "rgba(107, 114, 128, 1)",
        fill: true
      }
    ],
    "dB (Return Loss)"
  );

  createLineChart(
    "rlChart23",
    labels,
    [
      {
        label: "P2",
        data: valid.map(v => v.rl?.p2 ?? null),
        borderColor: "rgba(5, 150, 105, 1)",
        backgroundColor: "rgba(5, 150, 105, 0.12)",
        pointBackgroundColor: "rgba(5, 150, 105, 1)",
        pointBorderColor: "rgba(5, 150, 105, 1)",
        fill: true
      },
      {
        label: "P3",
        data: valid.map(v => v.rl?.p3 ?? null),
        borderColor: "rgba(220, 38, 38, 1)",
        backgroundColor: "rgba(220, 38, 38, 0.10)",
        pointBackgroundColor: "rgba(220, 38, 38, 1)",
        pointBorderColor: "rgba(220, 38, 38, 1)",
        fill: true
      }
    ],
    "dB (Return Loss)"
  );
}

function renderTemperatureChart(analysis) {
  const valid = analysis.sortedResults.filter(v =>
    Number.isFinite(v.temp?.dtu) ||
    Number.isFinite(v.temp?.fpga) ||
    Number.isFinite(v.temp?.rfu0) ||
    Number.isFinite(v.temp?.rfu1)
  );

  const labels = valid.map(v => v.displayId);

  createLineChart(
    "tempChartCanvas",
    labels,
    [
      {
        label: "DTU",
        data: valid.map(v => v.temp?.dtu ?? null),
        borderColor: "rgba(37, 99, 235, 1)",
        backgroundColor: "rgba(37, 99, 235, 0.08)",
        pointBackgroundColor: "rgba(37, 99, 235, 1)",
        pointBorderColor: "rgba(37, 99, 235, 1)",
        fill: false
      },
      {
        label: "FPGA",
        data: valid.map(v => v.temp?.fpga ?? null),
        borderColor: "rgba(107, 114, 128, 1)",
        backgroundColor: "rgba(107, 114, 128, 0.08)",
        pointBackgroundColor: "rgba(107, 114, 128, 1)",
        pointBorderColor: "rgba(107, 114, 128, 1)",
        fill: false
      },
      {
        label: "RFU0",
        data: valid.map(v => v.temp?.rfu0 ?? null),
        borderColor: "rgba(5, 150, 105, 1)",
        backgroundColor: "rgba(5, 150, 105, 0.08)",
        pointBackgroundColor: "rgba(5, 150, 105, 1)",
        pointBorderColor: "rgba(5, 150, 105, 1)",
        fill: false
      },
      {
        label: "RFU1",
        data: valid.map(v => v.temp?.rfu1 ?? null),
        borderColor: "rgba(220, 38, 38, 1)",
        backgroundColor: "rgba(220, 38, 38, 0.08)",
        pointBackgroundColor: "rgba(220, 38, 38, 1)",
        pointBorderColor: "rgba(220, 38, 38, 1)",
        fill: false
      }
    ],
    "Temperature (C)"
  );
}

function renderTemperatureRangeChart(analysis) {
  const canvas = document.getElementById("tempRangeChartCanvas");
  const ctx = canvas.getContext("2d");
  const stats = analysis.temp.stats;

  const labels = ["DTU", "FPGA", "PMC", "PSU", "AISG", "RFU0", "RFU1"];
  const minData = [
    stats.dtu.min,
    stats.fpga.min,
    stats.pmc.min,
    stats.psu.min,
    stats.aisg.min,
    stats.rfu0.min,
    stats.rfu1.min
  ];
  const avgData = [
    stats.dtu.avg,
    stats.fpga.avg,
    stats.pmc.avg,
    stats.psu.avg,
    stats.aisg.avg,
    stats.rfu0.avg,
    stats.rfu1.avg
  ];
  const maxData = [
    stats.dtu.max,
    stats.fpga.max,
    stats.pmc.max,
    stats.psu.max,
    stats.aisg.max,
    stats.rfu0.max,
    stats.rfu1.max
  ];

  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Min",
          data: minData,
          backgroundColor: "rgba(37, 99, 235, 0.85)",
          borderColor: "rgba(37, 99, 235, 1)",
          borderWidth: 1
        },
        {
          label: "Mean",
          data: avgData,
          backgroundColor: "rgba(107, 114, 128, 0.85)",
          borderColor: "rgba(107, 114, 128, 1)",
          borderWidth: 1
        },
        {
          label: "Max",
          data: maxData,
          backgroundColor: "rgba(220, 38, 38, 0.85)",
          borderColor: "rgba(220, 38, 38, 1)",
          borderWidth: 1
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          position: "top"
        },
        tooltip: {
          callbacks: {
            label(context) {
              const y = context.parsed.y;
              return `${context.dataset.label}: ${y == null ? "N/A" : Number(y).toFixed(2)} C`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: {
            autoSkip: false
          }
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: "C"
          }
        }
      }
    }
  });

  chartInstances.push(chart);
}

function interpolateColor(value, min, max) {
  if (!Number.isFinite(value)) return "#e5e7eb";
  if (min === max) return "#e55361";

  const ratio = Math.max(0, Math.min(1, (value - min) / (max - min)));

  const start = { r: 200, g: 215, b: 197 };
  const end = { r: 229, g: 83, b: 97 };

  const r = Math.round(start.r + (end.r - start.r) * ratio);
  const g = Math.round(start.g + (end.g - start.g) * ratio);
  const b = Math.round(start.b + (end.b - start.b) * ratio);

  return `rgb(${r}, ${g}, ${b})`;
}

function getTextColorForBg(value, min, max) {
  if (!Number.isFinite(value)) return "#9ca3af";
  const ratio = (value - min) / ((max - min) || 1);
  return ratio > 0.55 ? "#ffffff" : "#111827";
}

function chunkArray(arr, size) {
  const chunks = [];
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size));
  }
  return chunks;
}

function renderTemperatureHeatmap(analysis) {
  const container = document.getElementById("tempHeatmapContainer");
  const items = analysis.sortedResults;
  const chunked = chunkArray(items, 20);

  const tempValues = items.flatMap(v => [v.temp?.rfu0, v.temp?.rfu1]).filter(Number.isFinite);
  const min = tempValues.length ? Math.floor(Math.min(...tempValues)) : 0;
  const max = tempValues.length ? Math.ceil(Math.max(...tempValues)) : 0;

  let html = `
    <div class="heatmap-header">
      RFU0 (top) / RFU1 (bottom) - Color: ${min}C(light) ~ ${max}C(dark)
    </div>
  `;

  for (const chunk of chunked) {
    html += `<div class="heatmap-chunk">`;

    html += `<div class="heatmap-row">`;
    for (const item of chunk) {
      const value = item.temp?.rfu0;
      const bg = interpolateColor(value, min, max);
      const fg = getTextColorForBg(value, min, max);
      html += `
        <div
          class="heatmap-cell ${Number.isFinite(value) ? "" : "empty"}"
          title="${escapeHtml(item.displayId)} / RFU0: ${Number.isFinite(value) ? value + "C" : "N/A"}"
          style="background:${bg}; color:${fg};"
        >${Number.isFinite(value) ? Math.round(value) : ""}</div>
      `;
    }
    html += `</div>`;

    html += `<div class="heatmap-row">`;
    for (const item of chunk) {
      const value = item.temp?.rfu1;
      const bg = interpolateColor(value, min, max);
      const fg = getTextColorForBg(value, min, max);
      html += `
        <div
          class="heatmap-cell ${Number.isFinite(value) ? "" : "empty"}"
          title="${escapeHtml(item.displayId)} / RFU1: ${Number.isFinite(value) ? value + "C" : "N/A"}"
          style="background:${bg}; color:${fg};"
        >${Number.isFinite(value) ? Math.round(value) : ""}</div>
      `;
    }
    html += `</div>`;

    html += `</div>`;
  }

  html += `
    <div class="heatmap-legend">
      <span>${min}C</span>
      <div class="heatmap-legend-bar"></div>
      <span>${max}C</span>
    </div>
  `;

  container.innerHTML = html;
}

function renderMergedTable(analysis, perLogAi) {
  const tbody = document.getElementById("mergedResultTableBody");
  const aiMap = getPerLogAiMap(perLogAi);

  tbody.innerHTML = analysis.sortedResults.map(item => {
    const ai = aiMap.get(item.displayId);
    const sb = ai?.statusByItem || {};
    const notes = [];
	const dbMode = item.sourceType === "db_xls" || item.db;

	if (item.dlPwr === null) notes.push(dbMode ? "DL Power 성적서 값 없음" : "dl_pwr 추출 실패");
	if (item.ulPwr === null && !dbMode) notes.push("ul_pwr 추출 실패");
	if (item.vswr === null) notes.push(dbMode ? "VSWR 성적서 값 없음" : "vswr 추출 실패");
   

    if (
      !Number.isFinite(item.temp?.dtu) &&
      !Number.isFinite(item.temp?.fpga) &&
      !Number.isFinite(item.temp?.rfu0) &&
      !Number.isFinite(item.temp?.rfu1) &&
      !Number.isFinite(item.temp?.pmc) &&
      !Number.isFinite(item.temp?.pwr12v) &&
      !Number.isFinite(item.temp?.aisg145v)
    ) {
      notes.push("temp 추출 실패");
    }
	if (dbMode) {
  	  notes.push("DB 성적서 기반 분석");
	}
	

    const worstRl = getWorstReturnLoss(item);

    return `
      <tr>
        <td>${escapeHtml(item.displayId)}</td>
        <td>${renderStatusChip(ai?.overallStatus)}</td>
        <td>${renderStatusChip(sb.dl_pwr?.status)}</td>
        <td>${renderStatusChip(sb.return_loss?.status)}</td>
        <td>${renderStatusChip(sb.ul_pwr?.status)}</td>
        <td>${renderStatusChip(sb.dtu_temp?.status)}</td>
        <td>${renderStatusChip(sb.rfu_temp?.status)}</td>
        <td>${renderStatusChip(sb.psu_in?.status)}</td>
        <td>${renderStatusChip(sb.sfp_tx?.status)}</td>
        <td>${renderStatusChip(sb.sfp_rx?.status)}</td>
        <td>${safeNumber(item.dlPwr, 4)}</td>
        <td>${safeNumber(item.ulPwr, 4)}</td>
        <td>${safeNumber(worstRl, 2)}</td>
        <td>${safeNumber(item.temp?.dtu, 1)}</td>
        <td>${safeNumber(item.temp?.fpga, 1)}</td>
        <td>${safeNumber(item.temp?.rfu0, 1)}</td>
        <td>${safeNumber(item.temp?.rfu1, 1)}</td>
        <td>${safeNumber(item.psuIn, 2)}</td>
        <td>${safeNumber(item.sfpTx, 2)}</td>
        <td>${safeNumber(item.sfpRx, 2)}</td>
        <td>${escapeHtml(notes.join(", "))}</td>
      </tr>
    `;
  }).join("");
}

function renderFailedList(analysis) {
  const failedList = document.getElementById("failedList");

  if (!analysis.failed.length) {
    failedList.innerHTML = `<li style="color:#059669;">없음</li>`;
    return;
  }

  failedList.innerHTML = analysis.failed.map(item => {
    return `<li>${escapeHtml(item.displayId)} - ${escapeHtml(item.name)}</li>`;
  }).join("");
}

function renderAll(analysis, perLogAi) {
  document.getElementById("resultSection").classList.remove("section-hidden");
  renderSummary(analysis);
  destroyCharts();
  renderDbAnalysisPanel(analysis);	
  createDlBarChart(analysis);
  createDlHistogram(analysis);
  createUlBarChart(analysis);
  createUlHistogram(analysis);
  renderVswrRlCharts(analysis);
  renderTemperatureChart(analysis);
  renderTemperatureRangeChart(analysis);
  renderTemperatureHeatmap(analysis);
  renderMergedTable(analysis, perLogAi);
  renderFailedList(analysis);
}

async function requestAnalysis(file) {
  const formData = new FormData();
  formData.append("zipFile", file);

  formData.append("histBinCount", "10");
  formData.append("sortMode", document.getElementById("sortMode").value);
  formData.append("targetValue", "");
  formData.append("toleranceValue", "");

  const response = await fetch("/analyze", {
    method: "POST",
    body: formData
  });

  const contentType = response.headers.get("content-type") || "";

  let data;

  if (contentType.includes("application/json")) {
    data = await response.json();
  } else {
    const text = await response.text();
    throw new Error(
      `서버가 JSON이 아닌 응답을 반환했습니다.\n` +
      `HTTP ${response.status}\n` +
      text.slice(0, 500)
    );
  }

  if (!response.ok || !data.ok) {
    throw new Error(data.message || data.error || `분석 실패: HTTP ${response.status}`);
  }

  return data;
}


async function runAnalysis() {
  if (isLoading) return;

  const file = document.getElementById("zipFileInput").files[0];

  if (!file) {
    setStatus("분석할 ZIP 파일을 먼저 선택해 주세요.");
    return;
  }

  if (!file.name.toLowerCase().endsWith(".zip")) {
    setStatus("ZIP 형식의 파일만 분석할 수 있습니다.");
    return;
  }

  try {
    setStatus("ZIP 파일을 업로드하고 분석을 시작합니다...");
    setLoading(true, { steps: ANALYSIS_LOADING_STEPS, intervalMs: 1700 });
    lastUploadedFile = file;

    const result = await requestAnalysis(file);
    currentAnalysis = result.analysis;
    currentPerLogAi = result.perLogAi || [];

    renderAll(currentAnalysis, currentPerLogAi);
    renderAiSummary(result.aiSummary);
    renderPerLogAi(currentPerLogAi);
    renderOpenAiSolution(result.openAiSolution);

    setStatus(
      `분석이 완료되었습니다.\n` +
      `- 전체 로그 수: ${result.counts.total}\n` +
      `- DL Power 추출 수: ${result.counts.dlExtracted}\n` +
      `- VSWR 추출 수: ${result.counts.vswrExtracted}\n` +
      `- Temp 추출 수: ${result.counts.tempExtracted}\n` +
      `- 완전 실패 수: ${result.counts.failed}\n` +
      `- GEMINI 분석 상태: ${result.openAiSolution?.status || "N/A"}`
    );
  } catch (err) {
    console.error(err);
    setStatus(`분석 중 오류가 발생했습니다.\n${err.message || err}`);
  } finally {
    setLoading(false);
  }
}

function resetScreen() {
  lastUploadedFile = null;
  currentAnalysis = null;
  currentPerLogAi = [];
  destroyCharts();

  document.getElementById("zipFileInput").value = "";
  document.getElementById("resultSection").classList.add("section-hidden");
  document.getElementById("summaryGrid").innerHTML = "";
  document.getElementById("mergedResultTableBody").innerHTML = "";
  document.getElementById("failedList").innerHTML = "";
  document.getElementById("tempHeatmapContainer").innerHTML = "";

  const aiSummaryArea = document.getElementById("aiSummaryArea");
  if (aiSummaryArea) aiSummaryArea.innerHTML = "";

  const perLogAiBody = document.getElementById("perLogAiTableBody");
  if (perLogAiBody) perLogAiBody.innerHTML = "";

  const openAiSolutionArea = document.getElementById("openAiSolutionArea");
  if (openAiSolutionArea) openAiSolutionArea.innerHTML = "";

  setStatus("분석할 ZIP 파일을 선택해 주세요.");
}

async function rerenderIfNeeded() {
  if (!lastUploadedFile) return;
  if (isLoading) return;

  try {
    setStatus("변경한 설정을 반영해 결과를 다시 정리하고 있습니다...");
    setLoading(true, { steps: RERENDER_LOADING_STEPS, intervalMs: 1500 });
    const result = await requestAnalysis(lastUploadedFile);
    currentAnalysis = result.analysis;
    currentPerLogAi = result.perLogAi || [];

    renderAll(currentAnalysis, currentPerLogAi);
    renderAiSummary(result.aiSummary);
    renderPerLogAi(currentPerLogAi);
    renderOpenAiSolution(result.openAiSolution);

    setStatus("설정 변경이 반영되었습니다.");
  } catch (err) {
    console.error(err);
    setStatus(`결과를 다시 그리는 중 오류가 발생했습니다.\n${err.message || err}`);
  } finally {
    setLoading(false);
  }
}

document.getElementById("analyzeBtn").addEventListener("click", runAnalysis);
document.getElementById("resetBtn").addEventListener("click", resetScreen);
document.getElementById("sortMode").addEventListener("change", rerenderIfNeeded);
