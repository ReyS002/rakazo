#!/usr/bin/env node
// Rotate a bot's webhook secret from inside the api container, the same way the
// `bots.rotateWebhookSecret` handler does (apps/api/src/routes/bots.ts), and hand the
// new plaintext to another process on stdout without ever displaying it.
//
// Install + usage (inside the api container):
//   docker cp rotate-webhook-secret.mjs <api>:/app/apps/api/dist/cli/rotate-webhook-secret.mjs
//   cd /app/apps/api && node dist/cli/rotate-webhook-secret.mjs <botId> --dry-run
//   cd /app/apps/api && node dist/cli/rotate-webhook-secret.mjs <botId> | <consumer reading stdin>
//
// Output contract:
//   stdout: the new plaintext, no trailing newline, written only after the database
//           transaction commits. Nothing else is ever written to stdout (app loggers and
//           console.* are redirected to stderr for the whole run).
//   stderr: progress and errors. Never contains the plaintext, the ciphertext, user ids,
//           or env values. Prisma error messages are not printed (they can echo query
//           arguments); only the error name/code is.
//   exit:   0 ok | 1 env/runtime | 2 bad args | 3 bot not found/archived |
//           4 rotation failed before commit (old secret untouched, stdout empty) |
//           5 committed but stdout write failed (new key lost: rotate again)
//
// Safety:
//   - Never run without a consumer on the pipe: a terminal would show the key.
//     Refuses to write the key when stdout is a TTY (exit 4, nothing written).
//   - Use `set -o pipefail` in the caller so a failure here fails the pipeline.
//   - Workspace packages (@rakazo/*) ship TypeScript source; the app's own CLIs load them
//     through tsx (apps/api package.json scripts). This file registers tsx's ESM loader
//     from /app/node_modules before importing them.
//   - No realtime fanout is passed to createSecretStore: it only broadcasts cache
//     invalidation to other processes, and the webhook verifier's cache is keyed by
//     secret id + ciphertext, which both change on rotation.
import { randomBytes } from "node:crypto";

const realStdoutWrite = process.stdout.write.bind(process.stdout);
// Anything that would land on stdout (console.info from the app logger, Prisma) goes to stderr.
process.stdout.write = (chunk, encoding, callback) =>
  process.stderr.write(chunk, encoding, callback);

const log = (message) => process.stderr.write(`rotate-webhook-secret: ${message}\n`);

function describeError(error) {
  const name = error?.name ?? typeof error;
  const code = error?.code ? ` code=${error.code}` : "";
  return `${name}${code}`;
}

const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
const positional = args.filter((arg) => arg !== "--dry-run");
if (positional.length !== 1 || positional[0].startsWith("-")) {
  log("usage: node rotate-webhook-secret.mjs <botId> [--dry-run]");
  process.exit(2);
}
const botId = positional[0];
if (!/^[a-z0-9]{20,30}$/.test(botId)) {
  log("invalid botId (expected 20-30 lowercase letters/digits)");
  process.exit(2);
}

for (const name of ["DATABASE_URL", "ENCRYPTION_KEY"]) {
  if (!process.env[name]?.trim()) {
    log(`${name} is not set in this environment; run inside the api container`);
    process.exit(1);
  }
}
if (!dryRun && process.stdout.isTTY) {
  log("stdout is a terminal; pipe the output into a consumer. Nothing was changed.");
  process.exit(4);
}

let createDb, runAsSystem, createSecretStore, withSecretPersistence, resolveEncryptionKey;
try {
  const { register } = await import("tsx/esm/api");
  register();
  ({ createDb, runAsSystem } = await import("@rakazo/db"));
  ({ createSecretStore, withSecretPersistence } = await import("@rakazo/adapters"));
  ({ resolveEncryptionKey } = await import("@rakazo/core"));
} catch (error) {
  log(`could not load app modules (${describeError(error)}); run from /app/apps/api in the api container`);
  process.exit(1);
}

let encryptionKey;
try {
  encryptionKey = resolveEncryptionKey(process.env);
} catch {
  log("ENCRYPTION_KEY is rejected by the app's key policy");
  process.exit(1);
}

const OPERATION = "bots.rotateWebhookSecret";
const { prisma: basePrisma, pool } = createDb(process.env.DATABASE_URL);
const secrets = createSecretStore(encryptionKey, process.env);
// Same wrapping as app.ts: external secret refs are cleaned up on rollback/delete.
const prisma = withSecretPersistence(basePrisma, secrets);

let committed = false;
let plaintext;
try {
  await runAsSystem("operator CLI: rotate bot webhook secret", async () => {
    const bot = await prisma.bot.findUnique({
      where: { id: botId },
      select: {
        id: true,
        name: true,
        userId: true,
        spaceId: true,
        archivedAt: true,
        webhookSecretId: true,
      },
    });
    if (!bot) {
      log("bot not found");
      process.exitCode = 3;
      return;
    }
    if (bot.archivedAt) {
      // The handler's repos.getBot() excludes archived bots.
      log("bot is archived");
      process.exitCode = 3;
      return;
    }

    if (dryRun) {
      const [user, space, secret] = await Promise.all([
        prisma.user.findUnique({ where: { id: bot.userId }, select: { id: true } }),
        prisma.space.findUnique({ where: { id: bot.spaceId }, select: { id: true } }),
        bot.webhookSecretId
          ? prisma.secret.findFirst({
              where: {
                id: bot.webhookSecretId,
                userId: bot.userId,
                spaceId: bot.spaceId,
                kind: "webhook",
              },
              select: { id: true },
            })
          : null,
      ]);
      log(
        `bot=${JSON.stringify(bot.name)} user=${user ? "ok" : "missing"} space=${space ? "ok" : "missing"} hasSecret=${secret ? "true" : "false"}`,
      );
      return;
    }

    await secrets.start();
    const candidate = randomBytes(32).toString("base64url");
    let stored;
    try {
      stored = await secrets.put(candidate, {
        operationId: OPERATION,
        traceId: OPERATION,
        spaceId: bot.spaceId,
        userId: bot.userId,
        signal: new AbortController().signal,
      });
      await prisma.$transaction(async (tx) => {
        const previousSecretId = bot.webhookSecretId;
        await tx.secret.create({
          data: {
            id: stored.id,
            userId: bot.userId,
            spaceId: bot.spaceId,
            kind: "webhook",
            ciphertext: stored.ciphertext,
          },
        });
        await tx.bot.update({
          where: { id: bot.id },
          data: { webhookSecretId: stored.id },
        });
        if (previousSecretId) {
          await tx.secret.deleteMany({
            where: {
              id: previousSecretId,
              spaceId: bot.spaceId,
              userId: bot.userId,
              kind: "webhook",
            },
          });
        }
      });
    } catch (error) {
      log(`rotation failed before commit (${describeError(error)}); previous secret left intact`);
      process.exitCode = 4;
      return;
    }
    committed = true;
    plaintext = candidate;
    log(`rotated webhook secret for bot=${JSON.stringify(bot.name)}`);
  });
} catch (error) {
  if (!committed) {
    log(`failed (${describeError(error)}); nothing was changed`);
    process.exitCode = 4;
  }
}

if (committed && plaintext) {
  await new Promise((resolve) => {
    realStdoutWrite(plaintext, (error) => {
      if (error) {
        log("committed, but writing the key to stdout failed; the new key is lost, rotate again");
        process.exitCode = 5;
      } else {
        process.exitCode = 0;
      }
      resolve();
    });
  });
  plaintext = undefined;
}

try {
  await secrets.close();
} catch {
  log("warning: secret store close failed");
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
