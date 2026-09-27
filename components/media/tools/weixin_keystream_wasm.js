#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const KEYSTREAM_SIZE = 131072;

function usage() {
  console.error("usage: weixin_keystream_wasm.js --decode-key-file KEY --wasm-dir DIR --out OUT");
  process.exit(2);
}

function argValue(args, name) {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 >= args.length) {
    return "";
  }
  return args[index + 1];
}

class LocalXMLHttpRequest {
  constructor() {
    this.status = 0;
    this.responseType = "";
    this.response = null;
    this.responseText = "";
    this.onload = null;
    this.onerror = null;
    this._url = "";
    this._async = false;
  }

  open(_method, url, asyncFlag) {
    this._url = url;
    this._async = Boolean(asyncFlag);
  }

  send() {
    try {
      const filePath = this._url.startsWith("file://") ? new URL(this._url) : path.resolve(this._url);
      const data = fs.readFileSync(filePath);
      this.status = 200;
      if (this.responseType === "arraybuffer") {
        this.response = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
      } else {
        this.responseText = data.toString("utf8");
        this.response = this.responseText;
      }
      if (this._async && this.onload) {
        setImmediate(() => this.onload());
      }
    } catch (error) {
      this.status = 404;
      if (this._async && this.onerror) {
        setImmediate(() => this.onerror(error));
      } else {
        throw error;
      }
    }
  }
}

async function waitForRuntime(context, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (context.Module && context.Module.WxIsaac64) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error("WASM runtime did not expose WxIsaac64 before timeout");
}

async function main() {
  const args = process.argv.slice(2);
  const decodeKeyFile = argValue(args, "--decode-key-file");
  const wasmDir = argValue(args, "--wasm-dir");
  const outPath = argValue(args, "--out");
  if (!decodeKeyFile || !wasmDir || !outPath) {
    usage();
  }

  const decodeKey = fs.readFileSync(decodeKeyFile, "utf8").trim();
  if (!decodeKey) {
    throw new Error("decode key file is empty");
  }
  const wasmJs = path.resolve(wasmDir, "wasm_video_decode.js");
  const wasmBinary = path.resolve(wasmDir, "wasm_video_decode.wasm");
  if (!fs.existsSync(wasmJs) || !fs.existsSync(wasmBinary)) {
    throw new Error("missing wasm_video_decode.js or wasm_video_decode.wasm");
  }

  const context = {
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Uint8Array,
    ArrayBuffer,
    WebAssembly,
    XMLHttpRequest: LocalXMLHttpRequest,
    document: { title: "" },
    self: { location: { href: `file://${wasmJs}` } },
    VTS_WASM_URL: wasmBinary,
    MAX_HEAP_SIZE: 33554432,
    wasm_isaac_generate(ptr, size) {
      const wasmArray = new Uint8Array(context.Module.HEAPU8.buffer, ptr, size);
      context.__keystream = Uint8Array.from(wasmArray).reverse();
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(wasmJs, "utf8"), context, { filename: wasmJs });
  await waitForRuntime(context, 30000);
  context.__decodeKey = decodeKey;
  vm.runInContext(
    `
    __keystream = null;
    const decryptor = new Module.WxIsaac64(__decodeKey);
    decryptor.generate(${KEYSTREAM_SIZE});
    decryptor.delete();
    if (!__keystream || __keystream.length !== ${KEYSTREAM_SIZE}) {
      throw new Error("keystream generation failed");
    }
    `,
    context,
  );
  fs.mkdirSync(path.dirname(path.resolve(outPath)), { recursive: true });
  fs.writeFileSync(outPath, Buffer.from(context.__keystream));
  console.log(JSON.stringify({ ok: true, bytes: context.__keystream.length }));
}

main().catch((error) => {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
});
