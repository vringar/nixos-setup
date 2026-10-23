// Signs the web view's writes (posts, note edits, and the owner's approvals,
// declines, answers and revocations) with an SSH key the user pastes into
// the Authorize dialog. The key becomes a non-extractable WebCrypto key
// held in one variable of this script: never stored, never sent, gone on
// reload. The signatures are the same SSHSIG over the same canonical bytes
// as the board's own signing (src/sign.rs), which must stay byte-identical
// with this file; tests/sign-vectors.json holds both to it.
//
// Also loaded by the test suite under node, which uses what this exports as
// globalThis.BoardSign: the signing functions, and postSigned against a
// test server.
"use strict";
(() => {
  const enc = new TextEncoder();
  const MAGIC = enc.encode("SSHSIG");
  const KEY_TYPE = "ssh-ed25519";
  // [namespace, fields in signing order] per write, as in src/sign.rs. A
  // field the write leaves out is signed as "".
  const KINDS = {
    create: ["board2-create", ["path", "kind", "description", "owner", "body", "key", "nonce"]],
    post: ["board2-post", ["path", "body", "key", "nonce"]],
    note: ["board2-note", ["path", "body", "base_version", "through", "key", "nonce"]],
    file: ["board2-file", ["path", "body", "key", "nonce"]],
    resolve: ["board2-resolve", ["item", "state", "body", "ref", "key", "nonce"]],
    register: ["board2-register", ["name", "note", "repo", "key", "nonce"]],
    approve: ["board2-approve", ["item", "name", "fingerprint", "key", "nonce"]],
    revoke: ["board2-revoke", ["fingerprint", "reason", "key", "nonce"]],
  };
  // Signed submits in a row that may go stale before giving up, as `board` does,
  // and the random wait (ms) before each new try, times the tries so far.
  const NONCE_TRIES = 8;
  const BACKOFF_MS = [10, 100];

  const bytes = (x) => (typeof x === "string" ? enc.encode(x) : x);
  const concat = (...parts) => {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  };
  const u32 = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n); return b; };
  const sshString = (x) => { const b = bytes(x); return concat(u32(b.length), b); };
  const b64 = (b) => btoa(Array.from(b, (c) => String.fromCharCode(c)).join(""));
  const b64url = (b) => b64(b).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  // `<name>:<utf-8 byte length>:<value>\n` per field, in the kind's order.
  function canonical([, fields], values) {
    return concat(...fields.map((name) => {
      if (typeof values[name] !== "string") throw new Error(`no ${name} to sign`);
      const v = enc.encode(values[name]);
      return concat(enc.encode(`${name}:${v.length}:`), v, enc.encode("\n"));
    }));
  }

  class Reader {
    constructor(b) { this.b = b; this.at = 0; }
    take(n) {
      if (this.at + n > this.b.length) throw new Error("truncated key");
      return this.b.subarray(this.at, (this.at += n));
    }
    u32() { const b = this.take(4); return new DataView(b.buffer, b.byteOffset, 4).getUint32(0); }
    string() { return this.take(this.u32()); }
    text() { return new TextDecoder().decode(this.string()); }
  }

  // An openssh-key-v1 private key: unencrypted, exactly one ed25519 key.
  function parsePrivateKey(text) {
    const m = /-----BEGIN OPENSSH PRIVATE KEY-----([\s\S]*?)-----END OPENSSH PRIVATE KEY-----/.exec(text);
    if (!m) throw new Error("not an OpenSSH private key (-----BEGIN OPENSSH PRIVATE KEY-----)");
    let raw;
    try { raw = Uint8Array.from(atob(m[1].replace(/\s+/g, "")), (c) => c.charCodeAt(0)); }
    catch { throw new Error("the key is not valid base64"); }
    const r = new Reader(raw);
    if (new TextDecoder().decode(r.take(15)) !== "openssh-key-v1\0") throw new Error("not an openssh-key-v1 key");
    const cipher = r.text(), kdf = r.text();
    r.string();  // kdf options
    if (cipher !== "none" || kdf !== "none") {
      throw new Error("this key is passphrase-protected; use an unencrypted dedicated key (board keygen)");
    }
    if (r.u32() !== 1) throw new Error("expected exactly one key in the file");
    r.string();  // the public key, repeated below
    const p = new Reader(r.string());
    if (p.u32() !== p.u32()) throw new Error("corrupt key (check bytes differ)");
    const type = p.text();
    if (type !== KEY_TYPE) throw new Error(`only ed25519 keys are supported, not ${type}`);
    const pub = p.string(), priv = p.string();  // priv is seed || pub
    if (pub.length !== 32 || priv.length !== 64 || priv.subarray(32).some((c, i) => c !== pub[i])) {
      throw new Error("corrupt ed25519 key");
    }
    return { seed: priv.slice(0, 32), pub: pub.slice(), comment: p.text() };
  }

  // {key, blob, fingerprint}: a signing key that cannot be read back out.
  async function importKey(text) {
    const { seed, pub } = parsePrivateKey(text);
    const key = await crypto.subtle.importKey(
      "jwk", { kty: "OKP", crv: "Ed25519", d: b64url(seed), x: b64url(pub) },
      { name: "Ed25519" }, false, ["sign"]);
    seed.fill(0);
    const blob = concat(sshString(KEY_TYPE), sshString(pub));
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", blob));
    return { key, blob, fingerprint: "SHA256:" + b64(hash).replace(/=+$/, "") };
  }

  // An SSHSIG (sha512) over `data`, as single-line base64.
  async function signData(signer, namespace, data) {
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-512", data));
    const head = concat(sshString(namespace), sshString(""), sshString("sha512"));
    const sig = new Uint8Array(await crypto.subtle.sign(
      { name: "Ed25519" }, signer.key, concat(MAGIC, head, sshString(hash))));
    return b64(concat(MAGIC, u32(1), sshString(signer.blob), head,
                      sshString(concat(sshString(KEY_TYPE), sshString(sig)))));
  }

  const sign = (signer, kind, values) => signData(signer, kind[0], canonical(kind, values));

  // POST `values` as a signed form; resolves to the Response. The nonce is
  // the board's head, read right before signing rather than when the form
  // was rendered, since someone may type for minutes. If another write lands
  // in between, the board answers stale-nonce: read, sign and send again,
  // NONCE_TRIES times in all (after a random wait that grows with the tries),
  // then hand back the refusal.
  async function postSigned(signer, kind, url, values, init = {}) {
    const healthz = new URL("/healthz", new URL(url, globalThis.location?.href));
    for (let attempt = 1; ; attempt++) {
      const { head } = await (await fetch(healthz)).json();
      const signed = { ...values, nonce: String(head) };
      signed.signature = await sign(signer, kind, signed);
      const response = await fetch(url, { ...init, method: "POST", body: new URLSearchParams(signed) });
      if (attempt === NONCE_TRIES || response.headers.get("Board-Error") !== "stale-nonce") {
        return response;
      }
      const [low, high] = BACKOFF_MS;
      const wait = (low + Math.floor(Math.random() * (high - low + 1))) * attempt;
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }

  globalThis.BoardSign = { KINDS, canonical, parsePrivateKey, importKey, signData, sign, postSigned };
  if (typeof document === "undefined") return;

  // --- the page --------------------------------------------------------------
  // {key, blob, fingerprint, name, state} once authorized: the signing key,
  // and who the board says it is (GET /keys/<fingerprint>).
  let me = null;
  const $ = (sel) => document.querySelector(sel);
  const usable = window.isSecureContext && window.crypto?.subtle;
  // The page's signed forms, by the path they post to.
  const FORMS = {
    "/ui/post": KINDS.post,
    "/ui/note": KINDS.note,
    "/ui/approve": KINDS.approve,
    "/ui/resolve": KINDS.resolve,
    "/ui/revoke": KINDS.revoke,
  };
  // The key states each kind of control is shown to: `data-needs="trusted"`
  // (post, edit) to any key the board trusts, `data-needs="owner"`
  // (approve, decline, answer, revoke) to the owner key only. The board
  // checks every write regardless; this only spares the reader buttons that
  // would be refused.
  const SHOWN_TO = { trusted: ["trusted", "owner"], owner: ["owner"] };
  const signedForm = (path) => typeof path === "string" && Object.hasOwn(FORMS, path);

  // Re-applied after every swap: a boosted link replaces the whole body.
  function render() {
    const open = $("#auth-open");
    if (!open) return;
    if (!usable) {
      open.disabled = true;
      $("#auth-note").textContent = "signing needs HTTPS or localhost";
    }
    open.hidden = !!me;
    $("#auth-status").hidden = $("#auth-forget").hidden = !me;
    if (me) $("#auth-status").textContent = `🔒 ${me.name ?? "no name"} · ${me.state} · ${me.fingerprint}`;
    for (const control of document.querySelectorAll("[data-needs]")) {
      control.hidden = !(SHOWN_TO[control.dataset.needs] ?? []).includes(me?.state);
    }
  }

  // The key's name and state as the board has them; a key it does not know
  // is "unregistered", as `board whoami` says.
  async function lookUp(fingerprint) {
    const response = await fetch(`/keys/${encodeURIComponent(fingerprint)}`);
    if (response.status === 404) return { name: null, state: "unregistered" };
    if (!response.ok) throw new Error(`the board cannot say whose key this is (${response.status})`);
    const { name, state } = await response.json();
    return { name, state };
  }

  const closeDialog = () => $("#auth-dialog").close();

  async function authorize(form) {
    const error = $("#auth-error");
    const text = form.elements.privkey.value;
    form.elements.privkey.value = "";
    try {
      const signer = await importKey(text);
      me = { ...signer, ...(await lookUp(signer.fingerprint)) };
      closeDialog();
      render();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    }
  }

  function showError(form, message) {
    const error = form.querySelector(".error");
    if (!error) return;
    error.textContent = message;
    error.hidden = !message;
  }

  // htmx builds a request synchronously, but signing is async and needs the
  // head first: take the submit away from htmx and give htmx.ajax a fetch
  // that signs, so htmx still swaps in whatever the board answers.
  function submitSigned(form, path) {
    const kind = FORMS[path];
    const data = new FormData(form);
    // Exactly the fields the kind signs; one the form leaves out is "".
    const values = Object.fromEntries(kind[1].filter((f) => f !== "nonce").map((f) => [f, data.get(f) ?? ""]));
    values.key = crypto.randomUUID();  // one write, however often re-signed
    const send = (url, init) => postSigned(me, kind, url, values, init).catch((e) => {
      showError(form, `not sent: ${e.message}`);
      throw e;
    });
    htmx.ajax("POST", path, { source: form, fetch: send });
  }

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.id === "auth-form") {
      e.preventDefault();
      authorize(form);
      return;
    }
    const path = form.getAttribute("hx-post");
    if (!me || !signedForm(path)) return;  // unsigned: htmx's own submit, which the board refuses
    e.preventDefault();
    e.stopPropagation();  // capture phase: htmx's listener on the form never sees it
    submitSigned(form, path);
  }, true);

  document.addEventListener("click", (e) => {
    const id = e.target.closest("button")?.id;
    if (id === "auth-open") {
      $("#auth-error").hidden = true;
      $("#auth-dialog").showModal();
    } else if (id === "auth-cancel") {
      closeDialog();
    } else if (id === "auth-forget") {
      me = null;
      render();
    }
  });

  // A refused write keeps the page and what was typed; the reason goes next
  // to the form. One that landed clears its form, and htmx swaps in the
  // piece of page it changed.
  document.addEventListener("htmx:after:request", (e) => {
    const form = e.target;
    if (!signedForm(form.getAttribute?.("hx-post"))) return;
    const { ctx } = e.detail;
    if (ctx.response.status >= 400) {
      e.preventDefault();
      showError(form, ctx.text);
    } else {
      showError(form, "");
      form.reset();
    }
  });

  // However the dialog closes (authorized, cancel, Esc), nothing pasted stays.
  document.addEventListener("close", (e) => {
    if (e.target.id === "auth-dialog") $("#auth-form").reset();
  }, true);

  document.addEventListener("htmx:after:swap", render);
  render();
})();
