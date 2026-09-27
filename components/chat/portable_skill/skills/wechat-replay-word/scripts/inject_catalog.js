/* Task-scoped read-only RPC adapter. No cookies, storage, cloud service or eval. */
(function install(exports, config) {
  'use strict';
  if (location.hostname !== 'channels.weixin.qq.com') return;
  const marker = '__codexReplayCatalogV1';
  if (globalThis[marker]) return;
  const apis = Object.values(exports).filter(x => x && (typeof x === 'object' || typeof x === 'function'));
  const catalogAPI = apis.find(x => typeof x.finderLiveUserPage === 'function');
  const detailAPI = apis.find(x => typeof x.finderGetCommentDetail === 'function');
  if (!catalogAPI || !detailAPI) return;
  const state = {started: false, finished: false, status: 'waiting_for_target'};
  Object.defineProperty(globalThis, marker, {value: state, configurable: true});
  const id = value => {
    if (typeof value === 'string' && value.length && value.length <= 256) return value;
    if (Number.isSafeInteger(value) && value >= 0) return String(value);
    throw new Error('unsafe_identifier');
  };
  const send = async body => {
    const response = await fetch(config.endpoint, {
      method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: {'Content-Type': 'application/json', 'X-Codex-Catalog': config.token},
      body: JSON.stringify(body), signal: AbortSignal.timeout(30000)
    });
    if (!response.ok) throw new Error('local_collector_rejected');
    return response.json();
  };
  const dataOf = result => {
    if (!result || (result.errCode !== undefined && result.errCode !== 0)) throw new Error('api_error');
    if (!result.data || typeof result.data !== 'object') throw new Error('unknown_api_schema');
    const base = result.data.BaseResponse || result.data.baseResponse;
    if (base && (base.ret || base.Ret)) throw new Error('api_base_error');
    return result.data;
  };
  const sanitize = (obj, account) => {
    const o = obj.object || obj;
    if (o.contact && o.contact.username && o.contact.username !== account) throw new Error('wrong_account');
    const desc = o.objectDesc || {};
    const media = (desc.media || []).map(m => ({
      url: m.url, urlToken: m.urlToken || '', decodeKey: String(m.decodeKey || ''),
      fileSize: Number(m.fileSize || 0), encLimit: Number(m.encLimit || 0),
      videoPlayLen: Number(m.videoPlayLen || 0)
    }));
    return {id: id(o.id || o.objectId), nonce: String(o.objectNonceId || o.nonceId || ''),
      account, title: desc.description || o.description || '', created: Number(o.createtime || o.createTime || 0), media};
  };
  const originalList = catalogAPI.finderLiveUserPage;
  const originalDetail = detailAPI.finderGetCommentDetail;
  const run = async account => {
    if (state.started) return;
    state.started = true;
    state.status = 'catalog_running';
    await send({kind: 'bind', account, name: config.targetName, title: config.seedTitle});
    let cursor = '';
    const seen = new Set(['']);
    for (let index = 0; index < config.maxPages; index++) {
      const result = await originalList.call(catalogAPI, {username: account,
        finderUsername: account, lastBuffer: cursor, needFansCount: 0, objectId: '0'});
      const data = dataOf(result);
      const list = Array.isArray(data.object) ? data.object : data.liveObjects;
      if (!Array.isArray(list) || ![0, 1].includes(data.continueFlag)) throw new Error('unknown_catalog_schema');
      const next = data.lastBuffer || '';
      if (data.continueFlag && (!next || seen.has(next))) throw new Error('cursor_cycle');
      const items = list.map(item => sanitize(item, account));
      await send({kind: 'page', index, account, cursor, next, continueFlag: data.continueFlag, items});
      state.pages = index + 1;
      if (!data.continueFlag) {state.finished = true; state.status = 'catalog_complete'; return;}
      seen.add(next); cursor = next;
      await new Promise(resolve => setTimeout(resolve, 800));
    }
    throw new Error('catalog_page_limit');
  };
  const observe = result => {
    if (state.started) return;
    let data;
    try {data = dataOf(result);} catch {return;}
    const candidates = Array.isArray(data.object) ? data.object : [data.object];
    for (const obj of candidates) {
      if (obj && obj.contact && obj.contact.nickname === config.targetName &&
          obj.objectDesc && obj.objectDesc.description === config.seedTitle) {
        run(id(obj.contact.username)).catch(() => {state.status = 'stopped_check_local_evidence';});
        return;
      }
    }
  };
  // Preserve normal page method arguments, result and rejection behavior.
  detailAPI.finderGetCommentDetail = async function (...args) {
    const result = await originalDetail.apply(this, args);
    observe(result);
    return result;
  };
  state.status = 'ready_reopen_seed_once';
})(__CODEX_EXPORTS__, __CODEX_CONFIG__);
