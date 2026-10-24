// Signs the web view's writes (posts, note edits, and the owner's approvals,
// declines, answers, revocations and archiving) with an SSH key the user
// pastes into the Authorize dialog. The key becomes a non-extractable WebCrypto key
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
    note: ["board2-note", ["path", "body", "base_version", "thread", "through", "key", "nonce"]],
    file: ["board2-file", ["path", "body", "key", "nonce"]],
    resolve: ["board2-resolve", ["item", "state", "body", "ref", "key", "nonce"]],
    register: ["board2-register", ["name", "note", "repo", "key", "nonce"]],
    approve: ["board2-approve", ["item", "name", "fingerprint", "key", "nonce"]],
    revoke: ["board2-revoke", ["fingerprint", "reason", "key", "nonce"]],
    archive: ["board2-archive", ["event", "archived", "reason", "key", "nonce"]],
    // uploading and deleting a blob (`board put`, `board blob-rm`); the web view uploads (the forms'
    // attach control and drop target) but does not delete
    blob: ["board2-blob", ["sha256", "size", "media_type", "filename", "key", "nonce"]],
    blobdel: ["board2-blobdel", ["sha256", "key", "nonce"]],
  };
  // Signed submits in a row that may go stale before giving up, as `board` does,
  // and the random wait (ms) before each new try, times the tries so far.
  const NONCE_TRIES = 8;
  const BACKOFF_MS = [10, 100];
  // A write nobody answered is sent again after each of these waits (ms), as
  // `board` does; one that takes longer than WRITE_TIMEOUT_MS counts as that.
  const RETRY = { transportWaitsMs: [1000, 2000, 4000] };
  const WRITE_TIMEOUT_MS = 30000;

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
  // then hand back the refusal. When no answer comes (the connection broke,
  // or it took past WRITE_TIMEOUT_MS), the same write is sent again, as
  // `board` does, waiting RETRY.transportWaitsMs before each resend and then
  // giving up: `values.key` is the write's one client key, so a write that
  // had landed is not made twice, only answered again.
  async function postSigned(signer, kind, url, values, init = {}) {
    const healthz = new URL("/healthz", new URL(url, globalThis.location?.href));
    let dropped = 0;
    for (let attempt = 1; ; ) {
      let response;
      try {
        const { head } = await (await fetch(healthz, { signal: AbortSignal.timeout(WRITE_TIMEOUT_MS) })).json();
        const signed = { ...values, nonce: String(head) };
        signed.signature = await sign(signer, kind, signed);
        response = await fetch(url, { ...init, method: "POST", body: new URLSearchParams(signed),
                                      signal: AbortSignal.timeout(WRITE_TIMEOUT_MS) });
      } catch (e) {
        if (dropped === RETRY.transportWaitsMs.length) throw e;
        await new Promise((resolve) => setTimeout(resolve, RETRY.transportWaitsMs[dropped++]));
        continue;
      }
      if (attempt === NONCE_TRIES || response.headers.get("Board-Error") !== "stale-nonce") {
        return response;
      }
      const [low, high] = BACKOFF_MS;
      const wait = (low + Math.floor(Math.random() * (high - low + 1))) * attempt;
      await new Promise((resolve) => setTimeout(resolve, wait));
      attempt++;
    }
  }

  // The biggest file the page uploads: `crypto.subtle.digest` has no streaming
  // form, so the whole file is read into memory to be hashed. Anything bigger
  // goes in with `board put`, which streams.
  const UPLOAD_MAX = 256 * 1024 * 1024;
  const hex = (b) => Array.from(b, (c) => c.toString(16).padStart(2, "0")).join("");
  // The stored form of a media type, as the board reads one: no parameters,
  // lowercase, type/subtype of token characters; anything else is no type.
  const mediaType = (given) => {
    const main = String(given ?? "").split(";")[0].trim().toLowerCase();
    return main.length <= 127 && /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/.test(main) ? main : "application/octet-stream";
  };

  // Upload `file` (a Blob or File) as the board's `board put` does: hash it,
  // sign `board2-blob` against the head of the moment, PUT it to `url`
  // (`/blob`). Resolves to what the board answers ({hash, reference, size,
  // existed, ...}); rejects with the board's refusal as the CLI prints it.
  //
  // The board answers an upload it already has, or refuses one (a stale
  // nonce, a full store), before it reads the body, and a browser may lose
  // such an answer when the connection closes over a body it is still
  // sending. So a transport failure is not taken for a failure: if the board
  // has the blob by then, it is done; otherwise the upload is signed and sent
  // again, as a stale nonce is.
  async function uploadBlob(signer, file, url) {
    if (file.size === 0) throw new Error("an empty file cannot be uploaded");
    if (file.size > UPLOAD_MAX) {
      throw new Error(`${file.name || "this file"} is over ${UPLOAD_MAX / 1048576} MiB, which is as much as the page hashes in memory; use \`board put\``);
    }
    const base = globalThis.location?.href;
    const sha256 = hex(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer())));
    const type = mediaType(file.type);
    const values = { sha256, size: String(file.size), media_type: type, filename: file.name ?? "", key: crypto.randomUUID() };
    for (let attempt = 1; ; attempt++) {
      let response;
      try {
        const { head } = await (await fetch(new URL("/healthz", new URL(url, base)), { signal: AbortSignal.timeout(WRITE_TIMEOUT_MS) })).json();
        const signed = { ...values, nonce: String(head) };
        const signature = await sign(signer, KINDS.blob, signed);
        const headers = { "Board-Sha256": sha256, "Board-Key": signed.key, "Board-Nonce": signed.nonce,
                          "Board-Signature": signature, "Content-Type": type };
        if (values.filename) headers["Board-Filename"] = encodeURIComponent(values.filename);
        response = await fetch(new URL(url, base), { method: "PUT", headers, body: file,
                                                     signal: AbortSignal.timeout(Math.max(WRITE_TIMEOUT_MS, file.size / 1024)) });
      } catch (e) {
        const there = await fetch(new URL(`/blobs/${sha256}`, new URL(url, base))).catch(() => null);
        if (there?.ok) return { hash: sha256, reference: `board-blob://${sha256}`, size: file.size, existed: true };
        if (attempt === NONCE_TRIES) throw e;
        await new Promise((resolve) => setTimeout(resolve, BACKOFF_MS[1] * attempt));
        continue;
      }
      if (response.ok) return response.json();
      const text = await response.text();
      if (attempt < NONCE_TRIES && response.headers.get("Board-Error") === "stale-nonce") continue;
      let message = text;
      try { message = JSON.parse(text).message ?? text; } catch { /* not JSON: as it came */ }
      throw new Error(refusalText(response, message));
    }
  }

  // The client key of a form's write: one per write, kept until the write
  // lands, so that sending it again after an answer was lost (a click on the
  // button once more) is the same write and cannot be made twice. A form
  // whose content changed since is a different write and gets a new key.
  const pending = new WeakMap();
  function clientKey(form, values) {
    const content = JSON.stringify(Object.entries(values).sort());
    const kept = pending.get(form);
    if (kept?.content === content) return kept.key;
    const key = crypto.randomUUID();
    pending.set(form, { content, key });
    return key;
  }
  const keyUsed = (form) => pending.delete(form);

  // A refusal as the CLI prints it, `code: message`: the board's code is in
  // the Board-Error header, which a reader of the page never sees.
  const refusalText = (response, text) => {
    const code = response.headers?.get("Board-Error");
    return code ? `${code}: ${text}` : text;
  };

  // `create -p`: a signed write carries one path, so the containers along
  // `values.path` are made first, each signed on its own. One that exists
  // already (a conflict) is as good as made; any other refusal is the answer.
  async function postCreate(signer, url, values, init = {}) {
    const parts = values.path.split("/");
    for (let i = 1; i < parts.length; i++) {
      const parent = { path: parts.slice(0, i).join("/"), kind: "container", description: "", owner: "", body: "",
                       key: crypto.randomUUID() };
      const response = await postSigned(signer, KINDS.create, url, parent, init);
      if (!response.ok && response.headers.get("Board-Error") !== "conflict") return response;
    }
    return postSigned(signer, KINDS.create, url, values, init);
  }

  // The pages that refresh themselves (`data-live`: a queue's items, the
  // inbox, the keys) do not while the tab is hidden, nor over something the
  // reader is in the middle of: a field with text in it or in focus, a
  // fold opened, a refusal still shown.
  const liveBusy = (live, active) =>
    (live.contains(active) && !!active.matches?.("input, textarea, select")) ||
    Array.from(live.querySelectorAll("input, textarea")).some((f) => f.value !== f.defaultValue) ||
    live.querySelector("details[open], .error:not([hidden])") !== null;
  globalThis.BoardSign = { KINDS, canonical, parsePrivateKey, importKey, signData, sign, postSigned, postCreate, refusalText, clientKey, keyUsed, RETRY, liveBusy, uploadBlob, mediaType, UPLOAD_MAX };
  if (typeof document === "undefined") return;

  // --- the page --------------------------------------------------------------
  // {key, blob, fingerprint, name, state} once authorized: the signing key,
  // and who the board says it is (GET /keys/<fingerprint>).
  let me = null;
  const $ = (sel) => document.querySelector(sel);
  const usable = window.isSecureContext && window.crypto?.subtle;
  // The page's signed forms, by the path they post to.
  const FORMS = {
    "/ui/create": KINDS.create,
    "/ui/post": KINDS.post,
    "/ui/note": KINDS.note,
    "/ui/ask": KINDS.file,
    "/ui/approve": KINDS.approve,
    "/ui/resolve": KINDS.resolve,
    "/ui/revoke": KINDS.revoke,
    "/ui/archive": KINDS.archive,
    "/ui/register": KINDS.register,
  };
  // The key states each kind of control is shown to: `data-needs="trusted"`
  // (post, edit, create) to any key the board trusts, `data-needs="owner"`
  // (approve, decline, answer, revoke, archive) to the owner key only. The board
  // checks every write regardless; this only spares the reader buttons that
  // would be refused.
  // `data-needs="named"` is for the key named by `data-name` alone, and
  // `data-name` on any other control adds that name to who sees it (a queue's
  // owner answers, and the owner key or the queue's owner declines).
  // `data-needs="unregistered"` (the register form) is for a key the board
  // does not know yet.
  const SHOWN_TO = { trusted: ["trusted", "owner"], owner: ["owner"], named: [],
    unregistered: ["unregistered"] };
  const signedForm = (path) => typeof path === "string" && Object.hasOwn(FORMS, path);

  // Re-applied after every swap and every insertion of controls: a boosted
  // link replaces the whole body.
  function render() {
    const open = $("#auth-open");
    if (!open) return;
    if (!usable) {
      // The button stays: its dialog says why signing is out and what to do.
      $("#auth-note").textContent = "signing needs HTTPS or localhost";
      $("#auth-origin").textContent = location.origin;
      $("#auth-insecure").hidden = false;
      $("#auth-form").elements.privkey.disabled = true;
      $("#auth-submit").disabled = true;
    }
    open.hidden = !!me;
    $("#auth-status").hidden = $("#auth-forget").hidden = !me;
    if (me) $("#auth-status").textContent = `🔒 ${me.name ?? "no name"} · ${me.state} · ${me.fingerprint}`;
    for (const control of document.querySelectorAll("[data-needs]")) {
      const named = control.dataset.name !== undefined && control.dataset.name === me?.name
        && SHOWN_TO.trusted.includes(me.state);
      control.hidden = !((SHOWN_TO[control.dataset.needs] ?? []).includes(me?.state) || named);
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
    // Not signed: which page's piece the answer should be, and which window
    // of it (a thread's messages, a list's page), if the form says.
    for (const f of ["view", "shown", "last", "since", "all", "after"]) if (data.has(f)) values[f] = data.get(f);
    // The signed fields are the write's content; its key stays while the same write is sent again.
    values.key = clientKey(form, values);
    const send = (url, init) => (path === "/ui/create" ? postCreate(me, url, values, init) : postSigned(me, kind, url, values, init)).catch((e) => {
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
    if (form.dataset.uploading) {
      showError(form, "a file is still uploading; its reference lands in the text when it is done");
      return;
    }
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

  // --- attaching files ---------------------------------------------------
  // The post, ask and note forms take files, by the file input (`data-attach`)
  // or by dropping them on the text: each is uploaded as a blob, signed with
  // the authorized key, and its `board-blob://` reference goes into the text
  // at the caret, where the write that follows picks it up.
  async function attach(form, files) {
    const text = form.querySelector("textarea[name=body]");
    const note = form.querySelector(".attach-note");
    if (!text || files.length === 0) return;
    if (!me) { showError(form, "authorize a key first"); return; }
    showError(form, "");
    form.dataset.uploading = "1";
    try {
      for (const file of files) {
        if (note) note.textContent = `uploading ${file.name} ...`;
        const { reference, existed } = await uploadBlob(me, file, "/blob");
        const [from, to] = [text.selectionStart, text.selectionEnd];
        const before = text.value.slice(0, from);
        text.setRangeText(`${before === "" || /\s$/.test(before) ? "" : " "}${reference} `, from, to, "end");
        if (note) note.textContent = `${file.name}: ${existed ? "the board had it already" : "uploaded"}`;
      }
    } catch (e) {
      if (note) note.textContent = "";
      showError(form, `not uploaded: ${e.message}`);
    } finally {
      delete form.dataset.uploading;
    }
  }

  const attachable = (el) => el.closest?.("form[hx-post]") && signedForm(el.closest("form").getAttribute("hx-post"))
    && el.closest("form").querySelector("[data-attach]") !== null;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types ?? []).includes("Files");

  document.addEventListener("change", (e) => {
    if (!e.target.matches?.("input[data-attach]")) return;
    const files = Array.from(e.target.files);
    e.target.value = "";
    attach(e.target.form, files);
  });
  document.addEventListener("dragover", (e) => {
    if (e.target.matches?.("textarea") && attachable(e.target) && hasFiles(e)) e.preventDefault();
  });
  document.addEventListener("drop", (e) => {
    if (!e.target.matches?.("textarea") || !attachable(e.target) || !hasFiles(e)) return;
    e.preventDefault();
    attach(e.target.form, Array.from(e.dataTransfer.files));
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
      showError(form, refusalText(ctx.response, ctx.text));
    } else {
      showError(form, "");
      keyUsed(form);
      form.reset();
    }
  });

  // However the dialog closes (authorized, cancel, Esc), nothing pasted stays.
  document.addEventListener("close", (e) => {
    if (e.target.id === "auth-dialog") $("#auth-form").reset();
  }, true);

  document.addEventListener("htmx:before:request", (e) => {
    const live = e.target;
    if (live.hasAttribute?.("data-live") && (document.hidden || liveBusy(live, document.activeElement))) {
      e.preventDefault();
    }
  });

  // Whatever puts controls into the page, htmx's swap events or not (a
  // history restore, a swap whose source left the page, a later htmx), they
  // are hidden until this has seen them. Only an inserted element that is or
  // holds a control counts, so render's own writes cannot loop back here.
  const isControl = (n) => n.nodeType === 1 && (n.matches("[data-needs]") || n.querySelector("[data-needs]"));
  new MutationObserver((changes) => {
    if (changes.some((c) => Array.from(c.addedNodes).some(isControl))) render();
  }).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("htmx:after:swap", render);
  render();
})();
