(async () => {
  const keep = /(token|auth|login|user|account|bandu|songy|course|784)/i;
  const media = /(raw_url|download|media|m3u8|mp4|m4a|mp3|aac|opus|audio|video|784)/i;
  const limit = 80;

  function safeJson(value) {
    try {
      if (value === undefined || value === null) return null;
      if (typeof value === "string") return value;
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return String(value);
    }
  }

  function shouldKeep(key, value) {
    const text = `${key || ""} ${
      typeof value === "string" ? value : JSON.stringify(value || "")
    }`;
    return keep.test(text) || media.test(text);
  }

  function collectStorage(storage) {
    const out = {};
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key) continue;
      const value = storage.getItem(key);
      if (value && shouldKeep(key, value)) out[key] = value;
    }
    return out;
  }

  async function collectIndexedDB() {
    const out = [];
    if (!("indexedDB" in window) || !indexedDB.databases) return out;
    const dbs = await indexedDB.databases().catch(() => []);
    for (const dbInfo of dbs || []) {
      if (!dbInfo.name) continue;
      const record = { name: dbInfo.name, version: dbInfo.version, stores: [] };
      const db = await new Promise((resolve) => {
        const req = indexedDB.open(dbInfo.name);
        req.onerror = () => resolve(null);
        req.onsuccess = () => resolve(req.result);
      });
      if (!db) continue;
      try {
        for (const storeName of Array.from(db.objectStoreNames || [])) {
          const tx = db.transaction(storeName, "readonly");
          const store = tx.objectStore(storeName);
          const values = await new Promise((resolve) => {
            if (!store.getAll) return resolve([]);
            const req = store.getAll();
            req.onerror = () => resolve([]);
            req.onsuccess = () => resolve(req.result || []);
          });
          const kept = [];
          for (const value of values || []) {
            if (kept.length >= limit) break;
            if (shouldKeep(storeName, value)) kept.push(safeJson(value));
          }
          if (kept.length) record.stores.push({ name: storeName, records: kept });
        }
      } catch (err) {
        record.error = String(err);
      } finally {
        db.close();
      }
      if (record.stores.length || shouldKeep(record.name, "")) out.push(record);
    }
    return out;
  }

  async function collectCaches() {
    const out = [];
    if (!("caches" in window)) return out;
    const names = await caches.keys().catch(() => []);
    for (const name of names || []) {
      const cache = await caches.open(name).catch(() => null);
      if (!cache) continue;
      const requests = await cache.keys().catch(() => []);
      const entries = [];
      for (const request of requests || []) {
        if (entries.length >= limit) break;
        const response = await cache.match(request).catch(() => null);
        const contentType = response ? response.headers.get("content-type") || "" : "";
        const row = {
          url: request.url,
          content_type: contentType,
          status: response ? response.status : null,
        };
        if (!shouldKeep(request.url, contentType)) continue;
        if (/json|text|javascript|mpegurl|vnd\.apple\.mpegurl/i.test(contentType)) {
          row.body = await response.clone().text().catch(() => "");
          if (row.body.length > 200000) row.body = row.body.slice(0, 200000);
        }
        entries.push(row);
      }
      if (entries.length) out.push({ name, entries });
    }
    return out;
  }

  const artifact = {
    source_url: location.href,
    generated_at: new Date().toISOString(),
    boundary:
      "Use only with an authorized Songy company/test account. Do not share this file publicly.",
    localStorage: collectStorage(localStorage),
    sessionStorage: collectStorage(sessionStorage),
    indexedDB: await collectIndexedDB(),
    cacheStorage: await collectCaches(),
  };

  const blob = new Blob([JSON.stringify(artifact, null, 2)], {
    type: "application/json",
  });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "songy_test_session_artifact.json";
  document.body.appendChild(link);
  link.click();
  URL.revokeObjectURL(link.href);
  link.remove();
})();
