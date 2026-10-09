#!/usr/bin/env node
// Experimental Windows-only MiMo Desktop renderer patch.
// This does NOT extract/repack app.asar.unpacked. It appends the two patched
// renderer files to app.asar's data section and updates only their header entries.
// The original app.asar is backed up before apply and can be restored explicitly.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { extractFile, getRawHeader } from "@electron/asar";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT_FILE = path.join(HERE, "renderer-menu-patch.js");
const STYLE_FILE = path.join(HERE, "renderer-menu-patch.css");
const DEFAULT_ASAR = "D:\\Program Files\\Xiaomi MiMo\\resources\\app.asar";
const INDEX_ENTRY = "out\\renderer\\index.html";
const SCRIPT_ENTRY = "out\\renderer\\usage-dashboard-menu-patch.js";
const STYLE_ENTRY = "out\\renderer\\usage-dashboard-menu-patch.css";
const PATCH_MARKER = "<!-- MIMO_USAGE_MENU_PATCH -->";
const INTEGRITY_BLOCK_SIZE = 4 * 1024 * 1024;

function paths(asar) {
  return { asar, backup: `${asar}.mimo-usage-backup` };
}

function appAsarFromArgs(args) {
  const i = args.indexOf("--asar");
  return path.resolve(i >= 0 && args[i + 1] ? args[i + 1] : process.env.MIMO_APP_ASAR || DEFAULT_ASAR);
}

function requireWindows() {
  if (process.platform !== "win32") throw new Error("This app.asar patcher currently supports Windows only.");
}

function assertAppClosed() {
  try {
    const output = execFileSync("tasklist", ["/FI", "IMAGENAME eq Xiaomi MiMo.exe", "/FO", "CSV", "/NH"], {
      encoding: "utf8",
      windowsHide: true,
    });
    if (output.toLowerCase().includes("xiaomi mimo.exe")) {
      throw new Error("MiMo Desktop is still running. Quit it fully (including the tray process), then retry.");
    }
  } catch (error) {
    if (error instanceof Error && error.message.includes("still running")) throw error;
    // tasklist may be unavailable/restricted; file operations still fail safely if locked.
  }
}

function verifyWritable(target) {
  try {
    const fd = fs.openSync(target, "r+");
    fs.closeSync(fd);
  } catch (error) {
    throw new Error(`Cannot write ${target}. Close MiMo Desktop and run PowerShell as Administrator. ${error.message}`);
  }
}

function addIntegrity(entry, data) {
  const blocks = [];
  for (let offset = 0; offset < data.length; offset += INTEGRITY_BLOCK_SIZE) {
    blocks.push(createHash("sha256").update(data.subarray(offset, Math.min(offset + INTEGRITY_BLOCK_SIZE, data.length))).digest("hex"));
  }
  if (!blocks.length) blocks.push(createHash("sha256").update(data).digest("hex"));
  entry.integrity = {
    algorithm: "SHA256",
    hash: createHash("sha256").update(data).digest("hex"),
    blockSize: INTEGRITY_BLOCK_SIZE,
    blocks,
  };
}

function getEntry(root, archivePath, { create = false } = {}) {
  const parts = archivePath.split(/[\\/]/).filter(Boolean);
  let node = root;
  for (let i = 0; i < parts.length; i++) {
    if (!node.files) throw new Error(`ASAR entry path is not a directory: ${parts.slice(0, i).join("/")}`);
    const part = parts[i];
    if (!node.files[part]) {
      if (!create) throw new Error(`ASAR entry not found: ${archivePath}`);
      node.files[part] = i < parts.length - 1 ? { files: {} } : {};
    }
    node = node.files[part];
  }
  return node;
}

function pickleUInt32(value) {
  // Pickle has a 4-byte payload-length field followed by aligned payload.
  const out = Buffer.alloc(8);
  out.writeUInt32LE(4, 0);
  out.writeUInt32LE(value, 4);
  return out;
}

function pickleString(value) {
  const bytes = Buffer.from(value, "utf8");
  const aligned = Math.ceil(bytes.length / 4) * 4;
  const payloadBytes = 4 + aligned; // int32 string length, then padded UTF-8 bytes
  const out = Buffer.alloc(4 + payloadBytes);
  out.writeUInt32LE(payloadBytes, 0);
  out.writeInt32LE(bytes.length, 4);
  bytes.copy(out, 8);
  return out;
}

function patchHtml(html) {
  if (html.includes(PATCH_MARKER)) return html;
  if (!html.includes("</body>")) throw new Error("Renderer entry has no </body> insertion point; refusing to patch.");
  return html.replace(
    "</body>",
    `  <link rel="stylesheet" href="./usage-dashboard-menu-patch.css" />\n  <script src="./usage-dashboard-menu-patch.js"></script>\n  ${PATCH_MARKER}\n</body>`,
  );
}

function buildPatchedArchive(asar) {
  const { header, headerSize } = getRawHeader(asar);
  const raw = fs.readFileSync(asar);
  const oldDataStart = 8 + headerSize;
  if (oldDataStart > raw.length) throw new Error("Invalid ASAR header/data boundary.");
  const oldData = raw.subarray(oldDataStart);
  const originalHtml = extractFile(asar, INDEX_ENTRY).toString("utf8");
  if (originalHtml.includes(PATCH_MARKER)) throw new Error("The usage menu patch is already present.");
  const patchedHtml = Buffer.from(patchHtml(originalHtml), "utf8");
  const patchScript = fs.readFileSync(SCRIPT_FILE);
  const patchStyle = fs.readFileSync(STYLE_FILE);

  const indexEntry = getEntry(header, INDEX_ENTRY);
  const scriptEntry = getEntry(header, SCRIPT_ENTRY, { create: true });
  const styleEntry = getEntry(header, STYLE_ENTRY, { create: true });
  const scriptOffset = oldData.length;
  const styleOffset = scriptOffset + patchScript.length;
  const htmlOffset = styleOffset + patchStyle.length;
  Object.assign(scriptEntry, { size: patchScript.length, offset: String(scriptOffset), executable: false });
  Object.assign(styleEntry, { size: patchStyle.length, offset: String(styleOffset), executable: false });
  Object.assign(indexEntry, { size: patchedHtml.length, offset: String(htmlOffset) });
  addIntegrity(scriptEntry, patchScript);
  addIntegrity(styleEntry, patchStyle);
  addIntegrity(indexEntry, patchedHtml);

  const headerPickle = pickleString(JSON.stringify(header));
  const sizePickle = pickleUInt32(headerPickle.length);
  const archive = Buffer.concat([sizePickle, headerPickle, oldData, patchScript, patchStyle, patchedHtml]);
  return { archive, header, oldDataBytes: oldData.length };
}

function verifyPatchedArchive(archivePath) {
  const { header } = getRawHeader(archivePath);
  const html = extractFile(archivePath, INDEX_ENTRY).toString("utf8");
  const script = extractFile(archivePath, SCRIPT_ENTRY).toString("utf8");
  const style = extractFile(archivePath, STYLE_ENTRY).toString("utf8");
  if (!html.includes(PATCH_MARKER) || !html.includes("usage-dashboard-menu-patch.js") || !html.includes("usage-dashboard-menu-patch.css")) {
    throw new Error("Patched renderer HTML verification failed.");
  }
  if (!script.includes("用量统计") || !script.includes("127.0.0.1:8790")) {
    throw new Error("Injected menu script verification failed.");
  }
  if (!style.includes(".mimo-usage-overlay") || !style.includes(".mimo-usage-menu-item")) {
    throw new Error("Injected stylesheet verification failed.");
  }
  const idx = getEntry(header, INDEX_ENTRY);
  const js = getEntry(header, SCRIPT_ENTRY);
  const css = getEntry(header, STYLE_ENTRY);
  if (!idx.integrity || !js.integrity || !css.integrity) throw new Error("ASAR integrity metadata is missing.");
}

function verifyPreservedEntries(originalAsar, patchedAsar) {
  for (const archivePath of ["package.json", "out\\main\\launch.mjs"]) {
    const before = extractFile(originalAsar, archivePath);
    const after = extractFile(patchedAsar, archivePath);
    if (!before.equals(after)) throw new Error(`Existing ASAR entry changed unexpectedly: ${archivePath}`);
  }
}

function inspect(asar) {
  if (!fs.existsSync(asar)) throw new Error(`MiMo app.asar not found: ${asar}`);
  const { header, headerSize } = getRawHeader(asar);
  const html = extractFile(asar, INDEX_ENTRY).toString("utf8");
  console.log(`app.asar: ${asar}`);
  console.log(`Archive size: ${(fs.statSync(asar).size / (1024 * 1024)).toFixed(1)} MiB`);
  console.log(`Header size: ${headerSize} bytes`);
  console.log(`Renderer entry: ${html.length} chars; </body>: ${html.includes("</body>") ? "yes" : "no"}`);
  console.log(`Existing patch: ${html.includes(PATCH_MARKER) ? "yes" : "no"}`);
  console.log(`Unpacked native directory: ${fs.existsSync(`${asar}.unpacked`) ? "present (left untouched)" : "not found"}`);
  console.log("No app files were changed.");
  void header;
}

async function dryRun(asar) {
  requireWindows();
  if (!fs.existsSync(asar)) throw new Error(`MiMo app.asar not found: ${asar}`);
  // Validate against the pristine archive when a backup exists (asar itself may be patched).
  const source = fs.existsSync(`${asar}.mimo-usage-backup`) ? `${asar}.mimo-usage-backup` : asar;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "mimo-usage-asar-dryrun-"));
  const staged = path.join(temp, "app.asar");
  try {
    const built = buildPatchedArchive(source);
    fs.writeFileSync(staged, built.archive);
    verifyPatchedArchive(staged);
    verifyPreservedEntries(source, staged);
    console.log("Dry-run passed; original data and app.asar.unpacked are preserved byte-for-byte.");
    console.log(`Original archive: ${(fs.statSync(asar).size / (1024 * 1024)).toFixed(1)} MiB; patched archive: ${(built.archive.length / (1024 * 1024)).toFixed(1)} MiB.`);
    console.log("No installed app files or backups were changed.");
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function apply(asar) {
  requireWindows();
  if (!fs.existsSync(asar)) throw new Error(`MiMo app.asar not found: ${asar}\nPass --asar <path> if MiMo is installed elsewhere.`);
  assertAppClosed();
  verifyWritable(asar);
  const backup = `${asar}.mimo-usage-backup`;
  // Always patch from the pristine backup so the operation is repeatable.
  if (!fs.existsSync(backup)) fs.copyFileSync(asar, backup, fs.constants.COPYFILE_EXCL);
  const pristine = backup;
  const alreadyPatched = extractFile(pristine, INDEX_ENTRY).toString("utf8").includes(PATCH_MARKER);
  if (alreadyPatched) throw new Error(`Backup is not a pristine copy (already patched): ${backup}`);

  const temp = fs.mkdtempSync(path.join(path.dirname(asar), "mimo-usage-patch-"));
  const staged = path.join(temp, "app.asar");
  const displaced = `${asar}.mimo-usage-displaced`;
  try {
    const built = buildPatchedArchive(pristine);
    fs.writeFileSync(staged, built.archive);
    verifyPatchedArchive(staged);
    verifyPreservedEntries(pristine, staged);
    fs.renameSync(asar, displaced);
    try {
      fs.renameSync(staged, asar);
      fs.rmSync(displaced, { force: true });
    } catch (error) {
      if (fs.existsSync(displaced)) fs.renameSync(displaced, asar);
      throw error;
    }
    console.log(`Patch applied. Original app.asar backup: ${backup}`);
    console.log("app.asar.unpacked was not changed.");
    console.log("Restart MiMo Desktop; the account menu should show 用量统计.");
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
    fs.rmSync(displaced, { force: true });
  }
}

function restore(asar) {
  requireWindows();
  const backup = `${asar}.mimo-usage-backup`;
  if (!fs.existsSync(backup)) throw new Error(`Backup not found: ${backup}`);
  assertAppClosed();
  verifyWritable(asar);
  const staged = `${asar}.mimo-usage-restore`;
  const displaced = `${asar}.mimo-usage-displaced`;
  fs.copyFileSync(backup, staged);
  try {
    fs.renameSync(asar, displaced);
    fs.renameSync(staged, asar);
    fs.rmSync(displaced, { force: true });
  } catch (error) {
    if (!fs.existsSync(asar) && fs.existsSync(displaced)) fs.renameSync(displaced, asar);
    throw error;
  } finally {
    fs.rmSync(staged, { force: true });
  }
  console.log("Original app.asar restored. app.asar.unpacked was never changed.");
  console.log(`Backup retained at ${backup}`);
}

const [command = "inspect", ...args] = process.argv.slice(2);
const asar = appAsarFromArgs(args);
try {
  if (command === "inspect") inspect(asar);
  else if (command === "dry-run") await dryRun(asar);
  else if (command === "apply") await apply(asar);
  else if (command === "restore") restore(asar);
  else {
    console.error("Usage: node desktop-patch/patch.mjs <inspect|dry-run|apply|restore> [--asar <path>]");
    process.exitCode = 2;
  }
} catch (error) {
  console.error(`Desktop patch failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}
