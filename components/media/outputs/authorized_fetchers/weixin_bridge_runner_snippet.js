(async function () {
  const warn = "Only use on authorized company/test WeChat devices. Stop if this is a personal WeChat account.";
  const eid = prompt(warn + "\n\nPaste encrypted_objectid / dynamicExportId:", new URL(location.href).searchParams.get("eid") || "");
  if (!eid) return;
  const out = document.createElement("textarea");
  out.style.cssText = "position:fixed;z-index:2147483647;left:8px;right:8px;top:8px;width:calc(100% - 16px);height:55vh;background:#111;color:#0f0;font:12px monospace";
  document.body.appendChild(out);
  const log = data => { out.value = typeof data === "string" ? data : JSON.stringify(data, null, 2); };
  const requestId = () => String(Date.now()) + String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const invoke = (name, params) => new Promise((resolve, reject) => {
    if (!window.WeixinJSBridge || !window.WeixinJSBridge.invoke) return reject(new Error("WeixinJSBridge.invoke unavailable"));
    window.WeixinJSBridge.invoke(name, params, resolve);
  });
  const parseTransfer = resp => {
    const raw = resp && resp.jsapi_resp && resp.jsapi_resp.resp_json;
    if (!raw) return resp;
    try { return JSON.parse(raw); } catch (_) { return resp; }
  };
  const transfer = (url, cmdid, req, token) => invoke("finderH5ExtTransfer", {
    req_json: JSON.stringify(req),
    url,
    cgi_cmdid: cmdid,
    h5AuthToken: token || "",
    is_security_check: false,
    scope: "finderLive"
  }).then(parseTransfer);
  const liveIdFrom = detail => detail && detail.object && detail.object.liveInfo && detail.object.liveInfo.liveId ||
    detail && detail.data && detail.data.object && detail.data.object.liveInfo && detail.data.object.liveInfo.liveId || "";
  const replayFrom = info => {
    const liveInfo = info && info.liveInfo || info && info.data && info.data.liveInfo || {};
    const replay = liveInfo.replayInfo || {};
    return { renderReplayHlsUrl: replay.renderReplayHlsUrl || "", renderReplayUrl: replay.renderReplayUrl || "" };
  };
  try {
    log("Running finderH5Auth...");
    const auth = await invoke("finderH5Auth", { h5Version: 3774873601, scope: "finderLive" });
    const token = auth && auth.h5AuthToken || "";
    log("Running FinderGetCommentDetail...");
    const detail = await transfer("/cgi-bin/micromsg-bin/pc_findergetcommentdetail", 5259, {
      finder_basereq: { expt_flag: 1, request_id: requestId() },
      platform_scene: 2,
      encrypted_objectid: eid,
      need_object: 1,
      scene: 141,
      direction: 2,
      identity_scene: 2,
      pull_scene: 1
    }, token);
    const liveId = liveIdFrom(detail);
    if (!liveId) throw new Error("No liveId in FinderGetCommentDetail response");
    log("Running FinderGetLiveInfo...");
    const liveInfo = await transfer("/cgi-bin/micromsg-bin/pc_findergetliveinfo", 10064, { finder_basereq: {}, live_id: liveId }, token);
    log({ auth: { hasToken: !!token }, liveId, detail, liveInfo, replay: replayFrom(liveInfo) });
  } catch (err) {
    log({ error: err && err.message ? err.message : String(err) });
  }
}());
