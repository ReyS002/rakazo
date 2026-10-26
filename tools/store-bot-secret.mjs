#!/usr/bin/env node
// Save a bot credential from inside the api container, the same way the request_secret card
// (packages/adapters/src/run-secret.ts -> prepareBotSecret + persistPreparedSecret) and the
// owner `botSecrets.put` handler (apps/api/src/routes/bots.ts -> storeBotSecret) do. The
// plaintext is read from stdin only: never argv, never printed.
//
// Install + usage (inside the api container):
//   docker cp store-bot-secret.mjs <api>:/app/apps/api/dist/cli/store-bot-secret.mjs
//   cd /app/apps/api && node dist/cli/store-bot-secret.mjs <botId> <name> <origin> --dry-run < /dev/null
//   cd /app/apps/api && <producer writing the key to stdout> | node dist/cli/store-bot-secret.mjs <botId> <name> <origin>
//
// Pipe pattern (from the host; the key never touches argv, a file, or the terminal):
//   set -o pipefail
//   <producer> | docker exec -i -w /app/apps/api <api> node dist/cli/store-bot-secret.mjs <botId> <name> https://api.example.com
//
// Destination: { name, origin, auth: { type: "bearer" } }, validated by the app's own
// normalizeSecretDestination (BotSecretName pattern, origin rules). The origin must be https.
// Scope is the bot-level scope the card uses: { userId: bot.userId, spaceId: bot.spaceId, botId }.
//
// Output contract:
//   stdout: always empty (app loggers and console.* are redirected to stderr for the whole run).
//   stderr: progress and errors. Never contains the plaintext, the ciphertext, user ids, or
//           env values; the plaintext length may be printed. Prisma error messages are not
//           printed (they can echo query arguments); only the error name/code is.
//   exit:   0 ok | 1 env/runtime | 2 bad args or stdin | 3 bot not found/archived |
//           4 validation or store failed (existing secret left intact)
//
// Notes:
//   - Workspace packages (@rakazo/*) ship TypeScript source; this file registers tsx's ESM
//     loader from /app/node_modules before importing them (as rotate-webhook-secret.mjs does).
//   - An existing credential with the same name is replaced in place (same row id, new
//     ciphertext). One saved under the same name with a different origin/auth is refused by
//     the app ("Remove the existing credential before changing its destination").
//   - In dry-run mode stdin is still read and validated when it is not a TTY; with
//     `< /dev/null` the plaintext checks are skipped.

// Anything that would land on stdout (console.info from the app logger, Prisma) goes to stderr.
process.stdout.write = (chunk, encoding, callback) =>
  process.stderr.write(chunk, encoding, callback);

const log = (message) => process.stderr.write(`store-bot-secret: ${message}\n`);

function describeError(error) {
  const name = error?.name ?? typeof error;
  const code = error?.code ? ` code=${error.code}` : "";
  return `${name}${code}`;
}

// Messages authored by bot-secrets.ts that only describe the destination (name/origin/auth
// from argv) or a policy, never the value. Anything else is reported by name/code only.
const SAFE_MESSAGE_PREFIXES = [
  "Invalid credential destination",
  "Remove the existing credential before changing its destination",
  "Credential limit reached",
  "Credential cannot be used with this authentication method",
  "Invalid credential length",
  "Credential changed while saving; retry",
];
function describeStoreError(error) {
  const message = error instanceof Error ? error.message : "";
  if (error?.name === "Error" && SAFE_MESSAGE_PREFIXES.some((p) => message.startsWith(p))) {
    return message;
  }
  return describeError(error);
}

const MAX_PLAINTEXT = 4096;
const USAGE = "usage: node store-bot-secret.mjs <botId> <name> <origin> [--dry-run] < plaintext";

const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
const positional = args.filter((arg) => arg !== "--dry-run");
if (positional.length !== 3 || positional.some((arg) => arg.startsWith("-"))) {
  log(USAGE);
  process.exit(2);
}
const [botId, secretName, originArg] = positional;
if (!/^[a-z0-9]{20,30}$/.test(botId)) {
  log("invalid botId (expected 20-30 lowercase letters/digits)");
  process.exit(2);
}
let originUrl;
try {
  originUrl = new URL(originArg);
} catch {
  log("invalid origin (expected https://host[:port])");
  process.exit(2);
}
if (originUrl.protocol !== "https:") {
  log("origin must be https");
  process.exit(2);
}

for (const name of ["DATABASE_URL", "ENCRYPTION_KEY"]) {
  if (!process.env[name]?.trim()) {
    log(`${name} is not set in this environment; run inside the api container`);
    process.exit(1);
  }
}

// ---- stdin -------------------------------------------------------------------------------
async function readStdin(limitBytes) {
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    total += chunk.length;
    if (total > limitBytes) {
      // Drop what was read; do not keep a partial key in memory longer than needed.
      for (const c of chunks) c.fill(0);
      return null;
    }
    chunks.push(chunk);
  }
  const buffer = Buffer.concat(chunks);
  for (const c of chunks) c.fill(0);
  const text = buffer.toString("utf8");
  buffer.fill(0);
  return text;
}

let plaintext;
if (process.stdin.isTTY) {
  if (!dryRun) {
    log("stdin is a terminal; pipe the key in (… | node store-bot-secret.mjs …). Nothing was changed.");
    process.exit(2);
  }
  log("stdin is a terminal; dry run skips the value checks");
} else {
  let raw;
  try {
    // Room for the trailing newline/whitespace that is trimmed below.
    raw = await readStdin(MAX_PLAINTEXT * 4 + 64);
  } catch (error) {
    log(`could not read stdin (${describeError(error)}). Nothing was changed.`);
    process.exit(2);
  }
  if (raw === null) {
    log(`value on stdin is too long (max ${MAX_PLAINTEXT} chars). Nothing was changed.`);
    process.exit(2);
  }
  // Strip the trailing newline an `echo`/heredoc adds, and surrounding whitespace.
  const value = raw.trim();
  raw = undefined;
  if (!value) {
    if (!dryRun) {
      log("value on stdin is empty. Nothing was changed.");
      process.exit(2);
    }
    log("stdin is empty; dry run skips the value checks");
  } else if (value.length > MAX_PLAINTEXT) {
    log(`value on stdin is too long (length=${value.length}, max ${MAX_PLAINTEXT}). Nothing was changed.`);
    process.exit(2);
  } else {
    plaintext = value;
  }
}

// Mirrors credentialHeader() in bot-secrets.ts for bearer: the value must survive a Headers
// round trip unchanged. prepareBotSecret repeats this check on the real run.
function bearerUsable(value) {
  try {
    const header = `Bearer ${value}`;
    return new Headers({ Authorization: header }).get("Authorization") === header;
  } catch {
    return false;
  }
}
if (plaintext !== undefined && !bearerUsable(plaintext)) {
  log(`value cannot be sent as a bearer token (control characters?) length=${plaintext.length}. Nothing was changed.`);
  process.exit(2);
}

// ---- app modules -------------------------------------------------------------------------
let createDb, runAsSystem, createSecretStore, withSecretPersistence, resolveEncryptionKey;
let storeBotSecret, normalizeSecretDestination;
try {
  const { register } = await import("tsx/esm/api");
  register();
  ({ createDb, runAsSystem } = await import("@rakazo/db"));
  ({ createSecretStore, withSecretPersistence, storeBotSecret, normalizeSecretDestination } =
    await import("@rakazo/adapters"));
  ({ resolveEncryptionKey } = await import("@rakazo/core"));
} catch (error) {
  log(`could not load app modules (${describeError(error)}); run from /app/apps/api in the api container`);
  process.exit(1);
}
for (const [name, fn] of Object.entries({ storeBotSecret, normalizeSecretDestination })) {
  if (typeof fn !== "function") {
    log(`@rakazo/adapters does not export ${name}; this build is not supported`);
    process.exit(1);
  }
}

// The app's own validator; never bypassed. Same input shape the card's credential carries.
let destination;
try {
  destination = normalizeSecretDestination({
    name: secretName,
    origin: originArg,
    auth: { type: "bearer" },
  });
} catch (error) {
  log(`${describeStoreError(error)}. Nothing was changed.`);
  process.exit(2);
}
if (!destination.origin.startsWith("https://")) {
  log("origin must be https");
  process.exit(2);
}

let encryptionKey;
try {
  encryptionKey = resolveEncryptionKey(process.env);
} catch {
  log("ENCRYPTION_KEY is rejected by the app's key policy");
  process.exit(1);
}

const { prisma: basePrisma, pool } = createDb(process.env.DATABASE_URL);
const secrets = createSecretStore(encryptionKey, process.env);
// Same wrapping as app.ts: replaced/rolled-back secret refs are cleaned up.
const prisma = withSecretPersistence(basePrisma, secrets);

function sameDestination(row) {
  try {
    const stored = normalizeSecretDestination(row);
    return (
      stored.name === destination.name &&
      stored.origin === destination.origin &&
      JSON.stringify(stored.auth) === JSON.stringify(destination.auth)
    );
  } catch {
    return false;
  }
}

try {
  await runAsSystem("operator CLI: store bot secret", async () => {
    const bot = await prisma.bot.findUnique({
      where: { id: botId },
      select: { id: true, name: true, userId: true, spaceId: true, archivedAt: true },
    });
    if (!bot) {
      log("bot not found");
      process.exitCode = 3;
      return;
    }
    if (bot.archivedAt) {
      // The handlers' repos.getBot() excludes archived bots.
      log("bot is archived");
      process.exitCode = 3;
      return;
    }
    // Bot-level scope, as run-secret.ts builds it: { userId: run.userId, spaceId, botId }.
    // A bot's runs belong to its owner, so run.userId === bot.userId.
    const scope = { userId: bot.userId, spaceId: bot.spaceId, botId: bot.id };

    const existing = await prisma.botSecret.findFirst({
      where: { ...scope, name: destination.name },
      select: { name: true, origin: true, auth: true },
    });

    if (dryRun) {
      const count = await prisma.botSecret.count({ where: scope });
      let state = "ok";
      if (existing && !sameDestination(existing)) state = "conflict(stored under another origin/auth; remove it first)";
      else if (!existing && count >= 100) state = "limit(100 credentials)";
      log(
        `bot=${JSON.stringify(bot.name)} exists=${existing ? "true" : "false"} destination=${state}` +
          (plaintext !== undefined ? ` valueLength=${plaintext.length}` : ""),
      );
      if (state !== "ok") process.exitCode = 4;
      return;
    }

    if (plaintext === undefined) {
      log("no value on stdin. Nothing was changed.");
      process.exitCode = 2;
      return;
    }

    let row;
    try {
      await secrets.start();
      row = await storeBotSecret({
        prisma,
        secretStore: secrets,
        scope,
        destination,
        plaintext,
      });
    } catch (error) {
      log(`store failed (${describeStoreError(error)}); existing secret left intact`);
      process.exitCode = 4;
      return;
    }
    if (!row) {
      log("store returned no row (concurrent removal?); check the bot's credentials");
      process.exitCode = 4;
      return;
    }
    log(
      `stored secret name=${destination.name} bot=${JSON.stringify(bot.name)} origin=${destination.origin} replaced=${existing ? "true" : "false"}`,
    );
    process.exitCode = 0;
  });
} catch (error) {
  log(`failed (${describeError(error)}); nothing was changed`);
  process.exitCode = 4;
} finally {
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
