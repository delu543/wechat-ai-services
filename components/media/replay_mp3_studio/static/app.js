const state = {
  platform: "auto",
  statusFilter: "all",
  platformFilter: "all",
  jobs: [],
  selectedJobId: "",
  selectedJobIds: new Set(),
  timer: null,
  bridgeLoaded: false,
  runtimeCaptureLoaded: false,
  weixinOpenMode: "manual",
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function platformLabel(job) {
  if (job.is_health_check) return `${job.platform_label || job.platform || "任务"} · 健康检查`;
  const platform = job.platform_label || job.platform || "任务";
  const action = job.action_label || "";
  return action && action !== "转 MP3" ? `${platform} · ${action}` : platform;
}

function stateText(value) {
  return {
    queued: "排队中",
    running: "运行中",
    paused: "已暂停",
    completed: "已完成",
    failed: "失败",
  }[value] || value;
}

function outputStatusText(job) {
  return {
    ready: `MP3 已生成${job.output_bytes ? ` · ${Math.round(job.output_bytes / 1024 / 1024)} MB` : ""}`,
    not_applicable: "诊断任务 · 不产生 MP3",
    pending: "等待生成 MP3",
    missing: "输出异常 · 缺少 MP3",
    optional_missing: "探测完成 · 未发现可转换媒体",
    failed_missing: "失败 · 未生成 MP3",
  }[job.output_status] || "";
}

function platformName(value) {
  return {
    all: "全部平台",
    xiaohongshu: "小红书",
    weixin: "视频号",
    third_party: "第三方",
    other: "其他",
  }[value] || value;
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value >= 1024 * 1024 * 1024) return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (value >= 1024 * 1024) return `${Math.round(value / 1024 / 1024)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function expectedMinDurationSeconds() {
  const minutes = Number($("expectedDurationMinutesInput").value || 0);
  if (!Number.isFinite(minutes) || minutes <= 0) return 0;
  return Math.round(minutes * 60);
}

function candidateProofText(job) {
  const proof = job.weixin_source_proof || null;
  if (!proof) return "";
  const parts = [];
  const sourceBytes = formatBytes(proof.encrypted_bytes || proof.expected_bytes);
  const duration = formatDuration(proof.duration_seconds);
  if (sourceBytes) parts.push(`源 ${sourceBytes}`);
  if (duration) parts.push(`时长 ${duration}`);
  if (proof.candidate_count) parts.push(`候选 ${proof.candidate_count}`);
  if (proof.source_kind) parts.push(proof.source_kind);
  return parts.length ? `候选验证：${parts.join(" · ")}` : "";
}

function setActivePlatform(platform) {
  state.platform = platform;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.platform === platform);
  });
  renderPlatformHint();
}

function currentWeixinOpenMode() {
  const selected = document.querySelector('input[name="weixin_open_mode"]:checked');
  return selected ? selected.value : state.weixinOpenMode || "manual";
}

function syncWeixinFlowMode() {
  state.weixinOpenMode = currentWeixinOpenMode();
  const manual = state.weixinOpenMode === "manual";
  $("weixinPlaybackConfirmedRow").hidden = !manual;
  $("weixinFlowStep2").textContent = manual
    ? "在微信文件传输助手打开链接并播放"
    : "提交后自动通过文件传输助手打开";
  $("weixinFlowStep3").textContent = manual
    ? "勾选已播放后，平台抓最近运行态并转 MP3"
    : "自动尝试源抓取；失败时给出下一步";
  $("openBtn").textContent = state.platform === "third_party"
    ? "登录/打开"
    : state.platform === "weixin" && manual
      ? "手动操作说明"
      : state.platform === "weixin"
        ? "在微信打开"
        : "打开链接";
  if (state.platform === "weixin") {
    $("flowStatus").textContent = manual
      ? "人工模式：先在微信内置浏览器播放，再点“已播放，开始抓取并转 MP3”。"
      : "自动模式：提交后会先验证文件传输助手，再粘贴并打开链接。";
  }
}

function renderPlatformHint() {
  const hints = {
    auto: "自动识别平台；能直接抓取或复用本地 artifact 时会先走非实时路径，成功后输出正常速度 MP3。",
    weixin: "视频号默认先尝试 source / decrypt / 本地 artifact 路线；黑箱录制只作为显式兜底，当前有效速度按官方 3x 计算。",
    xiaohongshu: "优先请求回放元数据，发现可验证媒体后直接转 MP3。",
    third_party: "第三方会打开隔离浏览器资料夹；先登录一次，后续任务复用该登录态。",
    other: "YouTube 或直接媒体 URL 会调用对应脚本；不读取浏览器 cookies，网络或授权不足会明确失败。",
  };
  $("platformHint").textContent = hints[state.platform] || hints.auto;
  $("weixinBridgeGuide").hidden = state.platform !== "weixin";
  $("watchCurrentToggle").hidden = state.platform !== "weixin";
  $("weixinFlow").hidden = state.platform !== "weixin";
  if (state.platform === "weixin") loadBridgeSnippet();
  syncWeixinFlowMode();
  renderActionControls();
}

function renderActionControls() {
  const action = $("actionInput").value;
  const needsDiagnostics = action === "audit-cache" || action === "blackbox-record";
  $("diagnosticDetails").hidden = !needsDiagnostics;
  $("blackboxSpeedInput").disabled = action !== "blackbox-record";
  $("audioDeviceInput").disabled = action !== "blackbox-record";
  $("auditDirsInput").disabled = action !== "audit-cache";
  $("expectedDurationField").hidden = !(state.platform === "weixin" && action === "convert");
  const manualWeixin = state.platform === "weixin" && currentWeixinOpenMode() === "manual";
  $("startBtn").textContent = {
    convert: manualWeixin ? "已播放，开始抓取并转 MP3" : state.platform === "weixin" ? "自动打开并转 MP3" : "开始抓取并转 MP3",
    "probe-url": "开始网络探测",
    "audit-cache": "开始缓存审计",
    "blackbox-record": "开始黑箱录制",
  }[action] || "开始";
}

function renderJobStats() {
  const target = $("jobStats");
  if (!target) return;
  const counts = state.jobs.reduce(
    (memo, job) => {
      memo.total += 1;
      memo[job.state] = (memo[job.state] || 0) + 1;
      return memo;
    },
    { total: 0, queued: 0, running: 0, paused: 0, completed: 0, failed: 0 }
  );
  const statusItems = [
    ["all", "全部", counts.total],
    ["running", "运行", counts.running + counts.queued],
    ["paused", "暂停", counts.paused],
    ["completed", "完成", counts.completed],
    ["failed", "失败", counts.failed],
  ];
  target.innerHTML = `
    ${statusItems
      .map(
        ([value, label, count]) => `
          <button class="stat filter-chip ${state.statusFilter === value ? "active" : ""}"
            type="button"
            data-filter-kind="status"
            data-filter-value="${value}">
            ${label} <strong>${count}</strong>
          </button>`
      )
      .join("")}
  `;
  const platformTarget = $("jobPlatformStats");
  if (!platformTarget) return;
  const platformCounts = state.jobs.reduce(
    (memo, job) => {
      memo[job.platform] = (memo[job.platform] || 0) + 1;
      return memo;
    },
    { all: state.jobs.length, xiaohongshu: 0, weixin: 0, third_party: 0, other: 0 }
  );
  const platformItems = ["all", "weixin", "xiaohongshu", "third_party", "other"];
  platformTarget.innerHTML = `
    ${platformItems
      .map(
        (value) => `
          <button class="stat filter-chip ${state.platformFilter === value ? "active" : ""}"
            type="button"
            data-filter-kind="platform"
            data-filter-value="${value}">
            ${platformName(value)} <strong>${platformCounts[value] || 0}</strong>
          </button>`
      )
      .join("")}
  `;
}

function setStatusFilter(value) {
  state.statusFilter = value || "all";
  renderJobs();
}

function setPlatformFilter(value) {
  state.platformFilter = value || "all";
  $("platformFilter").value = state.platformFilter;
  renderJobs();
}

function parseAudioDeviceLine(line) {
  const match = String(line || "").match(/\[(\d+)\]\s+(.+)$/);
  if (!match) return null;
  return { value: `:${match[1]}`, label: match[2] };
}

async function loadAudioDevices() {
  const target = $("audioDeviceList");
  target.textContent = "读取中...";
  try {
    const body = await fetchJson("/api/audio-devices");
    const systemDevices = (body.system_audio_devices || [])
      .filter((device) => device && device.available)
      .map((device) => ({ value: device.value, label: device.label }));
    const devices = [
      ...systemDevices,
      ...(body.audio_devices || []).map(parseAudioDeviceLine).filter(Boolean),
    ];
    if (!devices.length) {
      target.textContent = "未发现可用音频输入设备";
      return;
    }
    target.innerHTML = "";
    for (const device of devices) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "device-choice";
      button.textContent = `${device.value} ${device.label}`;
      button.onclick = () => {
        $("audioDeviceInput").value = device.value;
      };
      target.appendChild(button);
    }
  } catch (error) {
    target.textContent = error.message;
  }
}

async function loadSpeedSnippet() {
  const target = $("speedSnippetOutput");
  const speed = Number($("speedSnippetRateInput").value || 8);
  target.value = "生成中...";
  try {
    const body = await fetchJson(`/api/speed-snippet?speed=${encodeURIComponent(speed)}`);
    target.value = [
      `目标倍速：${body.speed}x`,
      "书签脚本：",
      body.bookmarklet || "",
      "",
      "页面脚本：",
      body.snippet || "",
      "",
      "时间轴实测脚本：",
      body.timeline_probe_snippet || "",
      "",
      ...(body.notes || []),
    ].join("\n");
  } catch (error) {
    target.value = error.message;
  }
}

async function loadBridgeSnippet() {
  if (!state.runtimeCaptureLoaded) {
    try {
      const response = await fetch("/api/weixin/runtime-capture-snippet");
      $("runtimeCaptureSnippet").value = await response.text();
      state.runtimeCaptureLoaded = true;
    } catch (error) {
      $("runtimeCaptureSnippet").value = error.message;
    }
  }
  if (!state.bridgeLoaded) {
    try {
      const response = await fetch("/api/weixin/bridge-autopost-snippet");
      $("bridgeSnippet").value = await response.text();
      state.bridgeLoaded = true;
    } catch (error) {
      $("bridgeSnippet").value = error.message;
    }
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

async function refresh() {
  const body = await fetchJson("/api/state");
  state.jobs = body.jobs || [];
  syncSelectedJobs();
  if (state.selectedJobId && !state.jobs.some((job) => job.id === state.selectedJobId)) {
    state.selectedJobId = "";
    $("selectedJob").textContent = "未选择";
    $("logView").textContent = "";
  }
  $("libraryRoot").textContent = body.library_root || "";
  renderJobs();
  if (state.selectedJobId) await loadLog(state.selectedJobId);
}

function filteredJobs() {
  const filter = state.platformFilter;
  const query = $("jobSearch").value.trim().toLowerCase();
  return state.jobs.filter((job) => {
    if (filter !== "all" && job.platform !== filter) return false;
    if (state.statusFilter === "running" && !["queued", "running"].includes(job.state)) return false;
    if (state.statusFilter !== "all" && state.statusFilter !== "running" && job.state !== state.statusFilter) {
      return false;
    }
    if (!query) return true;
    const haystack = [
      job.display_title,
      job.platform_label,
      job.state,
      job.url,
      job.run_dir,
      job.error,
      ...(job.artifacts || []).map((artifact) => artifact.name),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function syncSelectedJobs() {
  const existing = new Set(state.jobs.map((job) => job.id));
  for (const jobId of [...state.selectedJobIds]) {
    if (!existing.has(jobId)) state.selectedJobIds.delete(jobId);
  }
}

function renderBulkActions(visibleJobs) {
  const target = $("bulkActions");
  if (!target) return;
  const visibleIds = visibleJobs.map((job) => job.id);
  const selectedVisibleCount = visibleIds.filter((jobId) => state.selectedJobIds.has(jobId)).length;
  const allVisibleSelected = visibleIds.length > 0 && selectedVisibleCount === visibleIds.length;
  const selectedCount = state.selectedJobIds.size;
  target.innerHTML = `
    <label class="bulk-select">
      <input type="checkbox" data-select-visible ${allVisibleSelected ? "checked" : ""} ${visibleIds.length ? "" : "disabled"} />
      当前列表全选
    </label>
    <span class="bulk-count">已选 ${selectedCount}</span>
    <button type="button" data-bulk-action="pause" ${selectedCount ? "" : "disabled"}>暂停选中</button>
    <button type="button" data-bulk-action="delete" ${selectedCount ? "" : "disabled"}>删除选中</button>
  `;
}

function renderJobs() {
  renderJobStats();
  syncSelectedJobs();
  const jobs = filteredJobs();
  renderBulkActions(jobs);
  const list = $("jobList");
  list.innerHTML = "";
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "job-meta";
    empty.textContent = "暂无任务";
    list.appendChild(empty);
    return;
  }
  for (const job of jobs) {
    const item = document.createElement("article");
    item.className = `job ${job.id === state.selectedJobId ? "active" : ""} ${
      state.selectedJobIds.has(job.id) ? "checked" : ""
    }`;
    item.tabIndex = 0;
    item.onclick = (event) => {
      if (event.target.closest("a, button, input, label")) return;
      selectJob(job.id);
    };
    item.onkeydown = (event) => {
      if (event.key === "Enter") selectJob(job.id);
    };

    const title = document.createElement("div");
    title.className = "job-top";
    const displayTitle = String(job.display_title || "").trim();
    const primaryTitle = displayTitle || platformLabel(job);
    const subtitle = displayTitle ? platformLabel(job) : "";
    title.innerHTML = `
      <label class="job-select" title="选择任务">
        <input type="checkbox" data-select-job="${escapeHtml(job.id)}" ${state.selectedJobIds.has(job.id) ? "checked" : ""} />
      </label>
      <div class="job-title-block">
        <div class="job-title">${escapeHtml(primaryTitle)}</div>
        ${subtitle ? `<div class="job-subtitle">${escapeHtml(subtitle)}</div>` : ""}
      </div>
      <span class="pill ${job.state}">${stateText(job.state)}</span>
    `;
    item.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "job-meta";
    const duration = formatDuration(job.verify && job.verify.duration_seconds);
    const minDuration = formatDuration(job.verify && job.verify.min_duration_seconds);
    const nextAction = job.next_action || null;
    const outputStatus = outputStatusText(job);
    const candidateProof = candidateProofText(job);
    meta.innerHTML = `
      <div>${escapeHtml(job.created_at || "")}</div>
      <div>${escapeHtml(job.url || job.action || "")}</div>
      <div>${escapeHtml(job.run_dir || "")}</div>
      ${duration ? `<div>时长 ${escapeHtml(duration)}${minDuration ? ` / 最低 ${escapeHtml(minDuration)}` : ""}</div>` : ""}
      ${outputStatus ? `<div class="output-status ${escapeHtml(job.output_status || "")}">${escapeHtml(outputStatus)}</div>` : ""}
      ${candidateProof ? `<div class="candidate-proof">${escapeHtml(candidateProof)}</div>` : ""}
      ${job.error ? `<div>${escapeHtml(job.error)}</div>` : ""}
      ${nextAction ? `<div class="next-action">${escapeHtml(nextAction.label)}：${escapeHtml(nextAction.detail)}</div>` : ""}
    `;
    item.appendChild(meta);

    const links = document.createElement("div");
    links.className = "job-links";
    if (job.state === "completed" && job.output_exists && job.output_path) {
      const outputPath = encodeURIComponent(job.output_path);
      links.innerHTML = `
        <a href="/api/file?path=${outputPath}" target="_blank" rel="noreferrer">播放</a>
        <a href="/api/file?path=${outputPath}&download=1" download>下载</a>
        <button type="button" class="link-button" data-reveal-path="${escapeHtml(job.output_path)}">Finder</button>
      `;
    }
    links.innerHTML += `<a href="/api/jobs/${job.id}/log" target="_blank" rel="noreferrer">日志</a>`;
    for (const artifact of job.artifacts || []) {
      const label = artifact.name.endsWith(".html") ? "打开包" : artifact.name.endsWith(".json") ? "JSON" : "artifact";
      links.innerHTML += `<a href="/api/file?path=${encodeURIComponent(artifact.path)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
    }
    if (nextAction && nextAction.artifact_path) {
      links.innerHTML += `<a href="/api/file?path=${encodeURIComponent(nextAction.artifact_path)}" target="_blank" rel="noreferrer">捕获 artifact</a>`;
    }
    if (nextAction && nextAction.diagnostics_path) {
      links.innerHTML += `<a href="/api/file?path=${encodeURIComponent(nextAction.diagnostics_path)}" target="_blank" rel="noreferrer">诊断</a>`;
    }
    if (nextAction && nextAction.open_packet_path) {
      links.innerHTML += `<a href="/api/file?path=${encodeURIComponent(nextAction.open_packet_path)}" target="_blank" rel="noreferrer">桥接包</a>`;
    }
    if (nextAction && nextAction.bridge_payload_packet_path) {
      links.innerHTML += `<a href="/api/file?path=${encodeURIComponent(nextAction.bridge_payload_packet_path)}" target="_blank" rel="noreferrer">Bridge payload</a>`;
    }
    if (nextAction && nextAction.bridge_launcher_url) {
      links.innerHTML += `<a href="${escapeHtml(nextAction.bridge_launcher_url)}" target="_blank" rel="noreferrer">Bridge 入口</a>`;
    }
    if (nextAction && nextAction.bridge_page_url) {
      links.innerHTML += `<a href="${escapeHtml(nextAction.bridge_page_url)}" target="_blank" rel="noreferrer">Bridge 页面</a>`;
    }
    if (nextAction && nextAction.bridge_snippet_url) {
      links.innerHTML += `<a href="${escapeHtml(nextAction.bridge_snippet_url)}" target="_blank" rel="noreferrer">Bridge JS</a>`;
    }
    item.appendChild(links);
    list.appendChild(item);
  }
}

async function selectJob(jobId) {
  state.selectedJobId = jobId;
  renderJobs();
  await loadLog(jobId);
}

function toggleSelectedJob(jobId, checked) {
  if (checked) state.selectedJobIds.add(jobId);
  else state.selectedJobIds.delete(jobId);
  renderJobs();
}

function toggleVisibleJobs(checked) {
  for (const job of filteredJobs()) {
    if (checked) state.selectedJobIds.add(job.id);
    else state.selectedJobIds.delete(job.id);
  }
  renderJobs();
}

async function pauseSelectedJobs() {
  const jobIds = [...state.selectedJobIds];
  if (!jobIds.length) return;
  try {
    await fetchJson("/api/jobs/pause", {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds }),
    });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
}

async function deleteSelectedJobs() {
  const jobIds = [...state.selectedJobIds];
  if (!jobIds.length) return;
  if (!confirm(`从列表删除 ${jobIds.length} 个任务？已完成/失败任务会移到本地归档，运行中的任务会先隐藏。`)) {
    return;
  }
  try {
    await fetchJson("/api/jobs/delete", {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds }),
    });
    for (const jobId of jobIds) state.selectedJobIds.delete(jobId);
    if (jobIds.includes(state.selectedJobId)) {
      state.selectedJobId = "";
      $("selectedJob").textContent = "未选择";
      $("logView").textContent = "";
    }
    await refresh();
  } catch (error) {
    alert(error.message);
  }
}

async function loadLog(jobId) {
  const response = await fetch(`/api/jobs/${jobId}/log`);
  const text = await response.text();
  $("selectedJob").textContent = jobId;
  $("logView").textContent = text || "";
  $("logView").scrollTop = $("logView").scrollHeight;
}

async function createJob(event) {
  event.preventDefault();
  const action = $("actionInput").value;
  const weixinMode = currentWeixinOpenMode();
  const weixinManual = state.platform === "weixin" && weixinMode === "manual";
  const artifactPath = $("artifactInput").value.trim();
  const artifactText = $("artifactTextInput").value.trim();
  const minDurationSeconds = expectedMinDurationSeconds();
  if (weixinManual && action === "convert" && !artifactPath && !artifactText && !$("weixinPlaybackConfirmedInput").checked) {
    $("flowStatus").textContent = "请先在微信内置浏览器中打开并播放，再勾选“已播放”。";
    alert("请先在微信内置浏览器中打开并播放，然后勾选“已播放”。");
    return;
  }
  const payload = {
    platform: state.platform,
    action,
    url: $("urlInput").value.trim(),
    artifact_path: artifactPath,
    artifact_text: artifactText,
    artifact_ext: $("artifactExtInput").value,
    duration: Number($("durationInput").value || 300),
    wait_seconds: Number($("waitInput").value || 180),
    fast_record: $("fastRecordInput").checked,
    watch_current: !weixinManual && $("watchCurrentInput").checked,
    weixin_open_mode: state.platform === "weixin" ? weixinMode : "",
    weixin_manual_playback: weixinManual,
    weixin_playback_confirmed: state.platform === "weixin" && $("weixinPlaybackConfirmedInput").checked,
    blackbox_speed: Number($("blackboxSpeedInput").value || 3),
    audio_device: $("audioDeviceInput").value.trim(),
    cache_dirs: $("auditDirsInput").value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean),
    mode: weixinManual ? "manual-playback" : state.platform === "weixin" && $("watchCurrentInput").checked ? "watch-current" : "auto",
  };
  if (minDurationSeconds > 0 && state.platform === "weixin" && action === "convert") {
    payload.min_duration_seconds = minDurationSeconds;
  }
  $("startBtn").disabled = true;
  if (state.platform === "weixin") {
    $("flowStatus").textContent = weixinManual
      ? "任务提交中：将扫描最近播放产生的安全运行态文件并尝试解密转 MP3。"
      : "任务提交中：接下来会自动尝试打开微信并抓取源文件。";
  }
  try {
    const job = await fetchJson("/api/jobs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedJobId = job.id;
    if (state.platform === "weixin") {
      $("flowStatus").textContent = `已提交任务 ${job.id}：保持微信可用，完成后在任务卡里播放、下载或 Finder 显示 MP3。`;
    }
    await refresh();
  } catch (error) {
    alert(error.message);
  } finally {
    $("startBtn").disabled = false;
  }
}

async function openTarget() {
  const url = $("urlInput").value.trim();
  if (!url && state.platform !== "third_party" && state.platform !== "weixin") return;
  if (state.platform === "weixin" && currentWeixinOpenMode() === "manual") {
    $("flowStatus").textContent = "人工模式：请把链接发送到微信文件传输助手，点开后在内置浏览器里开始播放，再勾选已播放并提交。";
    alert("人工模式：请在微信文件传输助手里打开并播放这个链接，然后回到本页勾选“已播放”。");
    return;
  }
  try {
    await fetchJson("/api/open", {
      method: "POST",
      body: JSON.stringify({ url, platform: state.platform }),
    });
    if (state.platform === "weixin") {
      $("flowStatus").textContent = "已请求在微信打开：确认播放页出现后，可直接提交自动转 MP3 任务。";
    }
  } catch (error) {
    alert(error.message);
  }
}

async function revealFile(path, event) {
  if (event) event.stopPropagation();
  try {
    await fetchJson("/api/reveal", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  } catch (error) {
    alert(error.message);
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => setActivePlatform(button.dataset.platform));
});

document.querySelectorAll('input[name="weixin_open_mode"]').forEach((input) => {
  input.addEventListener("change", () => {
    syncWeixinFlowMode();
    renderActionControls();
  });
});

document.addEventListener("click", (event) => {
  const selectJobBox = event.target.closest("[data-select-job]");
  if (selectJobBox) {
    event.stopPropagation();
    toggleSelectedJob(selectJobBox.dataset.selectJob, selectJobBox.checked);
    return;
  }
  const selectVisibleBox = event.target.closest("[data-select-visible]");
  if (selectVisibleBox) {
    event.stopPropagation();
    toggleVisibleJobs(selectVisibleBox.checked);
    return;
  }
  const bulkButton = event.target.closest("[data-bulk-action]");
  if (bulkButton) {
    event.stopPropagation();
    if (bulkButton.dataset.bulkAction === "pause") pauseSelectedJobs();
    if (bulkButton.dataset.bulkAction === "delete") deleteSelectedJobs();
    return;
  }
  const filterButton = event.target.closest("[data-filter-kind]");
  if (filterButton) {
    const kind = filterButton.dataset.filterKind;
    const value = filterButton.dataset.filterValue || "all";
    if (kind === "status") setStatusFilter(value);
    if (kind === "platform") setPlatformFilter(value);
    return;
  }
  const revealButton = event.target.closest("[data-reveal-path]");
  if (revealButton) {
    revealFile(revealButton.dataset.revealPath, event);
  }
});

$("jobForm").addEventListener("submit", createJob);
$("refreshBtn").addEventListener("click", refresh);
$("openBtn").addEventListener("click", openTarget);
$("actionInput").addEventListener("change", renderActionControls);
$("audioDevicesBtn").addEventListener("click", loadAudioDevices);
$("speedSnippetBtn").addEventListener("click", loadSpeedSnippet);
$("platformFilter").addEventListener("change", () => setPlatformFilter($("platformFilter").value));
$("jobSearch").addEventListener("input", renderJobs);

renderPlatformHint();
renderActionControls();
refresh();
state.timer = setInterval(refresh, 3000);
