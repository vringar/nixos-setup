"""Add board2-* cases to tests/sign-vectors.json.

Each case's canonical bytes are computed here (an independent encoder),
signed by `ssh-keygen -Y sign` with the vector key, and canonicalized and
signed again by static/board-sign.js under node. All three must agree; the
Rust test then holds src/sign.rs to the result.

usage: python3 -I apps/message-board/tests/make_v2_vectors.py [<apps/message-board dir>]

With no argument the app directory is the one above this file. Rerunning
it leaves sign-vectors.json unchanged: the cases are deterministic.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

KINDS = {
    "board2-create": ["path", "kind", "description", "owner", "body", "key", "nonce"],
    "board2-post": ["path", "body", "key", "nonce"],
    "board2-note": ["path", "body", "base_version", "thread", "through", "key", "nonce"],
    "board2-file": ["path", "body", "key", "nonce"],
    "board2-resolve": ["item", "state", "body", "ref", "key", "nonce"],
    "board2-register": ["name", "note", "repo", "key", "nonce"],
    "board2-approve": ["item", "name", "fingerprint", "key", "nonce"],
    "board2-revoke": ["fingerprint", "reason", "key", "nonce"],
    "board2-archive": ["event", "archived", "reason", "key", "nonce"],
    "board2-blob": ["sha256", "size", "media_type", "filename", "key", "nonce"],
    "board2-blobdel": ["sha256", "key", "nonce"],
}

CASES = [
    ("board2-create", ["reversing", "container", "PSP reverse engineering", "", "", "c-1", "10003"]),
    ("board2-create", ["reversing/emu/aspfuzz", "queue", "aspfuzz's requests", "aspfuzz", "", "", "10042"]),
    ("board2-create", ["reversing/charter", "note", "", "", "# Charter\nrules\n\nkey:3:abc\n", "c-3", "10044"]),
    ("board2-post", ["reversing/emu/log", "line one\nkey:4:evil ü🙂 @qemu-psp", "0f3c", "10050"]),
    ("board2-post", ["", "", "", ""]),
    ("board2-post", ["a/b", "\u0000\u001f\u007f \r\n\t end", "k", "0"]),
    ("board2-note", ["reversing/state", "| agent | state |\n|---|---|\n", "3", "reversing/emu/log", "10123", "n-1", "10200"]),
    ("board2-note", ["reversing/state", "first version", "0", "", "", "", "10201"]),
    ("board2-file", ["reversing/emu/aspfuzz", "Q1: which mailbox?", "f-1", "10300"]),
    ("board2-resolve", ["10300", "answered", "the second one", "10310", "r-1", "10311"]),
    ("board2-resolve", ["10300", "cancelled", "", "", "r-2", "10312"]),
    ("board2-register", ["psp-db", "psptool-rs repo, the database agent", "/home/vringar/src/psp-db", "g-1",
                         "10400"]),
    ("board2-register", ["x", "", "", "", "10401"]),
    ("board2-approve", ["10400", "psp-db", "SHA256:bkAKvEpsC35bXyEi1hTCV9Cd/77+M3t17LPDuSLMwCg", "a-1", "10402"]),
    ("board2-revoke", ["SHA256:bkAKvEpsC35bXyEi1hTCV9Cd/77+M3t17LPDuSLMwCg", "key leaked\nin a log", "v-1",
                       "999999"]),
    ("board2-archive", ["10360", "1", "noise: a wrong guess\nkey:2:x", "ar-1", "10500"]),
    ("board2-archive", ["10360", "0", "", "ar-2", "10501"]),
    ("board2-blob", ["3f786850e387550fdab836ed7e6dc881de23001b9e14a9f8b5a8d1c2e4f60a1b", "5242880",
                     "application/octet-stream", "trace v1.bin", "b-1", "10600"]),
    ("board2-blob", ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "1", "text/plain", "",
                     "", "10601"]),
    ("board2-blob", ["0000000000000000000000000000000000000000000000000000000000000000", "8589934592",
                     "image/svg+xml", "ü 🙂\n.png", "b-3", "0"]),
    ("board2-blobdel", ["3f786850e387550fdab836ed7e6dc881de23001b9e14a9f8b5a8d1c2e4f60a1b", "bd-1", "10602"]),
]

DRIVER = r"""
const fs = require("fs");
const [js, key, cases] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
const byNamespace = Object.fromEntries(Object.values(B.KINDS).map((k) => [k[0], k]));
(async () => {
  const signer = await B.importKey(fs.readFileSync(key, "utf8"));
  const out = [];
  for (const [namespace, values] of JSON.parse(fs.readFileSync(cases, "utf8"))) {
    const kind = byNamespace[namespace];
    const named = Object.fromEntries(kind[1].map((f, i) => [f, values[i]]));
    out.push({ fields: kind[1], bytes: Buffer.from(B.canonical(kind, named)).toString("hex"),
               signature: await B.sign(signer, kind, named) });
  }
  console.log(JSON.stringify({ fingerprint: signer.fingerprint, kinds: Object.keys(byNamespace).sort(), out }));
})();
"""


def canonical(fields, values):
    out = b""
    for name, value in zip(fields, values, strict=True):
        v = value.encode()
        out += f"{name}:{len(v)}:".encode() + v + b"\n"
    return out


def ssh_sign(key, namespace, data):
    armored = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key, "-n", namespace],
                             input=data, capture_output=True, check=True).stdout.decode()
    lines = armored.strip().splitlines()
    assert lines[0] == "-----BEGIN SSH SIGNATURE-----" and lines[-1] == "-----END SSH SIGNATURE-----"
    return "".join(lines[1:-1])


def main():
    app = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    path = app / "tests/sign-vectors.json"
    vectors = json.loads(path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        key = os.path.join(tmp, "key")
        with open(os.open(key, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(vectors["key"]["private"])
        cases_file = os.path.join(tmp, "cases.json")
        Path(cases_file).write_text(json.dumps(CASES))
        driver = os.path.join(tmp, "drive.js")
        Path(driver).write_text(DRIVER)
        node = json.loads(subprocess.run(["node", driver, str((app / "static/board-sign.js").resolve()), key,
                                          cases_file], capture_output=True, text=True, check=True).stdout)
        assert node["fingerprint"] == vectors["key"]["fingerprint"]
        assert node["kinds"] == sorted(KINDS), node["kinds"]
        v2 = []
        for (namespace, values), js in zip(CASES, node["out"], strict=True):
            assert js["fields"] == KINDS[namespace], namespace
            data = canonical(KINDS[namespace], values)
            assert js["bytes"] == data.hex(), (namespace, values)
            signature = ssh_sign(key, namespace, data)
            assert js["signature"] == signature, (namespace, values)
            v2.append({"kind": namespace, "values": values, "canonical_hex": data.hex(),
                       "ssh_keygen": signature, "js": js["signature"]})
    vectors["_about"] = (
        "Made with a throwaway key from ssh-keygen; it signs nothing else. cases: the v1 board's "
        "board-message/board-pin writes, made by board_sign.py and static/board-sign.js, which agreed "
        "on every one. v2_cases: board2-* writes, signed by ssh-keygen -Y sign over canonical bytes "
        "encoded independently of the board, and canonicalized and signed again by "
        "static/board-sign.js under node; all three agreed on every case. The refusals are signatures "
        "board_sign.verify_data refused, with its error text.")
    vectors["v2_cases"] = v2
    order = ["_about", "key", "cases", "v2_cases", "refusals", "accepted"]
    vectors = {k: vectors[k] for k in order}
    path.write_text(json.dumps(vectors, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(v2)} v2 cases agree across ssh-keygen, node and the reference encoder")


if __name__ == "__main__":
    main()
