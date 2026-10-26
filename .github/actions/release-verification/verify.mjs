import { createSign, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

const API_VERSION = "2022-11-28";
const HTTP_TIMEOUT_MS = 30_000;
const MAX_REQUEST_ATTEMPTS = 5;
const POLL_INTERVAL_MS = 60_000;
const WAIT_TIMEOUT_MS = 21_300_000;
const TOKEN_REFRESH_MARGIN_MS = 300_000;
export const VERIFICATION_FAILED = "Release verification failed";

class SafeError extends Error {}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function annotation(message) {
  return message
    .replaceAll("%", "%25")
    .replaceAll("\r", "%0D")
    .replaceAll("\n", "%0A");
}

export function parseRepository(value) {
  const parts = value.split("/");
  if (
    parts.length !== 2 ||
    !parts.every((part) => /^[A-Za-z0-9_.-]+$/.test(part))
  ) {
    throw new SafeError("Target repository must use owner/name format");
  }
  return { owner: parts[0], name: parts[1] };
}

export function parseWorkflowFile(value) {
  if (!/^[A-Za-z0-9_.-]+\.ya?ml$/.test(value)) {
    throw new SafeError("Target workflow file name is invalid");
  }
  return value;
}

export function parseEventType(value) {
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(value)) {
    throw new SafeError("Target event type is invalid");
  }
  return value;
}

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) {
    throw new SafeError(`Required input ${name} is missing`);
  }
  return value;
}

function normalizePrivateKey(value) {
  return value.includes("\n") ? value : value.replaceAll("\\n", "\n");
}

function base64UrlJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

export function createAppJwt(appId, privateKey, nowSeconds) {
  const header = base64UrlJson({ alg: "RS256", typ: "JWT" });
  const payload = base64UrlJson({
    iat: nowSeconds - 60,
    exp: nowSeconds + 540,
    iss: appId,
  });
  const unsignedToken = `${header}.${payload}`;
  const signer = createSign("RSA-SHA256");
  signer.update(unsignedToken);
  signer.end();
  return `${unsignedToken}.${signer.sign(privateKey, "base64url")}`;
}

class GitHubApi {
  constructor(apiUrl, appId, privateKey, repository) {
    this.apiUrl = apiUrl.replace(/\/$/, "");
    this.appId = appId;
    this.privateKey = normalizePrivateKey(privateKey);
    this.repository = repository;
    this.installationId = null;
    this.installationToken = null;
    this.installationTokenExpiresAt = 0;
  }

  async request(path, token, options = {}) {
    const method = options.method || "GET";
    const expectedStatuses = options.expectedStatuses || [200];
    const maxAttempts = options.maxAttempts || MAX_REQUEST_ATTEMPTS;
    const deadline = options.deadline ?? Number.POSITIVE_INFINITY;
    let lastStatus = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const remainingTime = deadline - Date.now();
      if (remainingTime <= 0) {
        throw new SafeError("Timed out");
      }
      const controller = new AbortController();
      const timeout = setTimeout(
        () => controller.abort(),
        Math.min(HTTP_TIMEOUT_MS, remainingTime),
      );
      try {
        const response = await fetch(`${this.apiUrl}${path}`, {
          method,
          signal: controller.signal,
          headers: {
            Accept: "application/vnd.github+json",
            Authorization: `Bearer ${token}`,
            "User-Agent": "mlrun-ce-release-verification",
            "X-GitHub-Api-Version": API_VERSION,
            ...(options.body ? { "Content-Type": "application/json" } : {}),
          },
          body: options.body ? JSON.stringify(options.body) : undefined,
        });
        lastStatus = response.status;
        if (expectedStatuses.includes(response.status)) {
          return response.status === 204 ? null : await response.json();
        }
        if (response.status < 500 && response.status !== 429) {
          throw new SafeError(`API request failed with HTTP ${response.status}`);
        }
      } catch (error) {
        if (error instanceof SafeError) {
          throw error;
        }
        if (attempt === maxAttempts) {
          throw new SafeError("API request failed");
        }
      } finally {
        clearTimeout(timeout);
      }

      if (attempt < maxAttempts) {
        const retryDelay = Math.min(
          attempt * 5_000,
          Math.max(0, deadline - Date.now()),
        );
        if (retryDelay <= 0) {
          throw new SafeError("Timed out");
        }
        await sleep(retryDelay);
      }
    }

    throw new SafeError(`API request failed with HTTP ${lastStatus}`);
  }

  appJwt() {
    return createAppJwt(
      this.appId,
      this.privateKey,
      Math.floor(Date.now() / 1000),
    );
  }

  async resolveInstallationId(deadline = Number.POSITIVE_INFINITY) {
    if (this.installationId) {
      return this.installationId;
    }
    const { owner, name } = this.repository;
    const installation = await this.request(
      `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/installation`,
      this.appJwt(),
      { deadline },
    );
    this.installationId = installation.id;
    return this.installationId;
  }

  async token(deadline = Number.POSITIVE_INFINITY) {
    if (
      this.installationToken &&
      Date.now() + TOKEN_REFRESH_MARGIN_MS < this.installationTokenExpiresAt
    ) {
      return this.installationToken;
    }

    const installationId = await this.resolveInstallationId(deadline);
    const previousToken = this.installationToken;
    const tokenResponse = await this.request(
      `/app/installations/${installationId}/access_tokens`,
      this.appJwt(),
      {
        method: "POST",
        expectedStatuses: [201],
        deadline,
        body: {
          repositories: [this.repository.name],
          permissions: {
            actions: "read",
            contents: "write",
          },
        },
      },
    );
    this.installationToken = tokenResponse.token;
    this.installationTokenExpiresAt = Date.parse(tokenResponse.expires_at);
    if (previousToken && previousToken !== this.installationToken) {
      await this.revoke(previousToken, deadline);
    }
    return this.installationToken;
  }

  async revoke(token, deadline = Number.POSITIVE_INFINITY) {
    if (!token) {
      return;
    }
    try {
      await this.request("/installation/token", token, {
        method: "DELETE",
        expectedStatuses: [204],
        maxAttempts: 1,
        deadline,
      });
    } catch {
      // Tokens expire on their own; a failed revoke must not change the outcome.
    }
  }

  async revokeToken(deadline = Number.POSITIVE_INFINITY) {
    const token = this.installationToken;
    this.installationToken = null;
    this.installationTokenExpiresAt = 0;
    await this.revoke(token, deadline);
  }

  async installationRequest(path, options = {}) {
    const deadline = options.deadline ?? Number.POSITIVE_INFINITY;
    return this.request(path, await this.token(deadline), options);
  }
}

export function expectedRunTitle(correlationId) {
  return `Release verification ${correlationId}`;
}

async function dispatchVerification(api, inputs, deadline) {
  const { owner, name } = api.repository;
  await api.installationRequest(
    `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/dispatches`,
    {
      method: "POST",
      expectedStatuses: [204],
      maxAttempts: 1,
      deadline,
      body: {
        event_type: inputs.eventType,
        client_payload: {
          version: inputs.version,
          source_repo: inputs.sourceRepo,
          source_sha: inputs.sourceSha,
          source_run_id: inputs.sourceRunId,
          source_run_attempt: inputs.sourceRunAttempt,
          correlation_id: inputs.correlationId,
          triggered_by: inputs.triggeredBy,
        },
      },
    },
  );
}

async function findVerificationRun(
  api,
  workflowFile,
  title,
  earliestCreationTime,
  deadline,
) {
  const { owner, name } = api.repository;
  const workflow = encodeURIComponent(workflowFile);
  const runs = await api.installationRequest(
    `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/actions/workflows/${workflow}/runs?event=repository_dispatch&per_page=100`,
    { deadline },
  );
  return (
    runs.workflow_runs.find(
      (run) =>
        run.display_title === title &&
        Date.parse(run.created_at) >= earliestCreationTime,
    ) || null
  );
}

export async function waitForVerification(
  api,
  workflowFile,
  title,
  earliestCreationTime,
  deadline,
) {
  while (Date.now() < deadline) {
    const run = await findVerificationRun(
      api,
      workflowFile,
      title,
      earliestCreationTime,
      deadline,
    );
    if (run?.status === "completed") {
      if (run.conclusion === "success") {
        return;
      }
      throw new SafeError("Target run did not succeed");
    }

    const pollDelay = Math.min(
      POLL_INTERVAL_MS,
      Math.max(0, deadline - Date.now()),
    );
    if (pollDelay > 0) {
      await sleep(pollDelay);
    }
  }

  throw new SafeError("Timed out");
}

export async function main() {
  const repository = parseRepository(requiredEnvironment("TARGET_REPO"));
  const workflowFile = parseWorkflowFile(
    requiredEnvironment("TARGET_WORKFLOW_FILE"),
  );
  const inputs = {
    eventType: parseEventType(requiredEnvironment("TARGET_EVENT_TYPE")),
    version: requiredEnvironment("VERSION"),
    sourceRepo: requiredEnvironment("SOURCE_REPO"),
    sourceSha: requiredEnvironment("SOURCE_SHA"),
    sourceRunId: requiredEnvironment("SOURCE_RUN_ID"),
    sourceRunAttempt: requiredEnvironment("SOURCE_RUN_ATTEMPT"),
    triggeredBy: requiredEnvironment("TRIGGERED_BY"),
    correlationId: randomUUID(),
  };
  const api = new GitHubApi(
    process.env.GITHUB_API_URL || "https://api.github.com",
    requiredEnvironment("APP_ID"),
    requiredEnvironment("APP_PRIVATE_KEY"),
    repository,
  );
  const title = expectedRunTitle(inputs.correlationId);
  const deadline = Date.now() + WAIT_TIMEOUT_MS;
  const operationDeadline = deadline - HTTP_TIMEOUT_MS;

  console.log("Release verification started");
  const earliestCreationTime = Date.now() - 60_000;
  try {
    await dispatchVerification(api, inputs, operationDeadline);
    await waitForVerification(
      api,
      workflowFile,
      title,
      earliestCreationTime,
      operationDeadline,
    );
  } catch {
    // Runtime failures share one public message so the log cannot tell a
    // failed target run apart from a timeout or an API error.
    throw new SafeError(VERIFICATION_FAILED);
  } finally {
    await api.revokeToken(deadline);
  }
  console.log("Release verification succeeded");
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  main().catch((error) => {
    const message =
      error instanceof SafeError ? error.message : VERIFICATION_FAILED;
    console.error(`::error::${annotation(message)}`);
    process.exitCode = 1;
  });
}
