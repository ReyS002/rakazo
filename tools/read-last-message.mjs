#!/usr/bin/env node
// Print the plain text of a bot's most recent non-user messages, read-only.
//
// Install + usage (inside the api container):
//   docker cp read-last-message.mjs <api>:/app/apps/api/dist/cli/read-last-message.mjs
//   cd /app/apps/api && node dist/cli/read-last-message.mjs <botName> [--n 1] [--steps]
//
// Decryption: message blocks are sealed per element (`{kind, $sealed: "rkz1:..."}`) by
// withContentEncryption (packages/db/src/content-encryption.ts). This file uses that same
// path: createDb() builds the key ring from the environment exactly as the app does, and
// each block is opened through contentEncryptionOf(prisma).openRows("Message", ...), which
// is the app's own decryptText/openJson with the same AAD and space checks. Blocks are
// opened one at a time so a block that fails authentication prints `[unreadable block]`
// instead of failing the whole message.
//
// Output contract:
//   stdout: per message `--- <createdAt> role=<role> ---` then the text blocks (and step
//           labels with --steps). Runs of 40+ [A-Za-z0-9_-] are replaced with <redacted>.
//   stderr: progress and errors. Never contains keys, ciphertext, or user/space ids. Prisma
//           errors print name/code only.
//   exit:   0 ok | 1 env/runtime | 2 bad args | 3 bot not found
//
// Read-only: only findFirst/findMany/$queryRaw SELECTs; no writes of any kind.

const realStdoutWrite = process.stdout.write.bind(process.stdout);
// Anything that would land on stdout (console.info from the app logger, Prisma) goes to stderr.
process.stdout.write = (chunk, encoding, callback) =>
  process.stderr.write(chunk, encoding, callback);

const log = (message) => process.stderr.write(`read-last-message: ${message}\n`);

function describeError(error) {
  const name = error?.name ?? typeof error;
  const code = error?.code ? ` code=${error.code}` : "";
  return `${name}${code}`;
}

const redact = (text) => String(text).replace(/[A-Za-z0-9_-]{40,}/g, "<redacted>");

const MAX_N = 5;
const usage = "usage: node read-last-message.mjs <botName> [--n 1] [--steps]";
const args = process.argv.slice(2);
let showSteps = false;
let count = 1;
const positional = [];
for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--steps") showSteps = true;
  else if (arg === "--n" || arg.startsWith("--n=")) {
    const value = arg === "--n" ? args[++i] : arg.slice(4);
    if (!/^\d+$/.test(value ?? "") || Number(value) < 1) {
      log(usage);
      process.exit(2);
    }
    count = Math.min(Number(value), MAX_N);
  } else if (arg.startsWith("-")) {
    log(usage);
    process.exit(2);
  } else positional.push(arg);
}
if (positional.length !== 1 || !positional[0]) {
  log(usage);
  process.exit(2);
}
const botName = positional[0];

for (const name of ["DATABASE_URL"]) {
  if (!process.env[name]?.trim()) {
    log(`${name} is not set in this environment; run inside the api container`);
    process.exit(1);
  }
}

let createDb, runAsSystem, contentEncryptionOf;
try {
  const { register } = await import("tsx/esm/api");
  register();
  ({ createDb, runAsSystem, contentEncryptionOf } = await import("@rakazo/db"));
} catch (error) {
  log(`could not load app modules (${describeError(error)}); run from /app/apps/api in the api container`);
  process.exit(1);
}

let prisma, pool;
try {
  // Same construction as the app: key ring from master keys in the environment.
  ({ prisma, pool } = createDb(process.env.DATABASE_URL, { applicationName: "read-last-message" }));
} catch (error) {
  log(`could not open database client (${describeError(error)})`);
  process.exit(1);
}

function blockLines(block) {
  if (!block || typeof block !== "object" || Array.isArray(block)) return [];
  if (block.kind === "text") return typeof block.text === "string" ? [block.text] : [];
  if (block.kind === "steps" && showSteps) {
    const steps = Array.isArray(block.steps) ? block.steps : [];
    const lines = steps.map((step) =>
      step?.count > 1 ? `[step] ${step.label} x${step.count}` : `[step] ${step?.label ?? ""}`,
    );
    if (typeof block.text === "string") lines.push(block.text);
    return lines;
  }
  return [];
}

const output = [];
try {
  await runAsSystem("operator CLI: read last bot message", async () => {
    const encryption = contentEncryptionOf(prisma);
    if (!encryption) throw Object.assign(new Error(), { name: "ContentEncryptionUnavailable" });

    const bot = await prisma.bot.findFirst({
      where: { name: botName, archivedAt: null },
      select: { id: true },
      orderBy: { createdAt: "desc" },
    });
    if (!bot) {
      log("bot not found (exact, case-sensitive name; archived bots excluded)");
      process.exitCode = 3;
      return;
    }

    // No blocks selected here, so nothing is decrypted yet.
    const messages = await prisma.message.findMany({
      where: { botId: bot.id, role: { not: "user" } },
      select: { id: true, threadId: true, role: true, createdAt: true },
      orderBy: [{ createdAt: "desc" }, { seq: "desc" }],
      take: count,
    });
    if (!messages.length) log("no bot messages found");

    for (const message of messages) {
      // Raw read bypasses the extension; each block is then opened on its own via openRows.
      const [raw] = await prisma.$queryRaw`SELECT "blocks" FROM "messages" WHERE "id" = ${message.id}`;
      const blocks = Array.isArray(raw?.blocks) ? raw.blocks : raw?.blocks ? [raw.blocks] : [];
      output.push(`--- ${message.createdAt.toISOString()} role=${message.role} ---`);
      for (const sealed of blocks) {
        const kind = sealed && typeof sealed === "object" ? sealed.kind : undefined;
        if (kind !== "text" && !(kind === "steps" && showSteps)) continue;
        let block;
        try {
          const [row] = await encryption.openRows("Message", [
            { id: message.id, threadId: message.threadId, blocks: [sealed] },
          ]);
          block = row.blocks[0];
        } catch {
          output.push("[unreadable block]");
          continue;
        }
        output.push(...blockLines(block));
      }
    }
  });
} catch (error) {
  log(`failed (${describeError(error)})`);
  process.exitCode = 1;
}

if (output.length && process.exitCode === undefined) {
  await new Promise((resolve) => {
    realStdoutWrite(`${redact(output.join("\n"))}\n`, (error) => {
      if (error) {
        log("writing to stdout failed");
        process.exitCode = 1;
      }
      resolve();
    });
  });
}

try {
  await prisma.$disconnect();
} catch {
  log("warning: prisma disconnect failed");
}
try {
  await pool.end();
} catch {
  log("warning: pool end failed");
}
