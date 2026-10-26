import assert from "node:assert/strict";
import { generateKeyPairSync, createVerify } from "node:crypto";
import test from "node:test";

import {
  VERIFICATION_FAILED,
  createAppJwt,
  expectedRunTitle,
  main,
  parseRepository,
  parseEventType,
  parseWorkflowFile,
  waitForVerification,
} from "../.github/actions/release-verification/verify.mjs";

test("parseRepository accepts one owner and repository", () => {
  assert.deepEqual(parseRepository("example-org/example-target"), {
    owner: "example-org",
    name: "example-target",
  });
});

test("parseRepository rejects malformed repository names", () => {
  for (const value of ["example-target", "example/a/b", "example/a b"]) {
    assert.throws(() => parseRepository(value));
  }
});

test("parseWorkflowFile accepts a workflow file name", () => {
  assert.equal(parseWorkflowFile("example.yaml"), "example.yaml");
});

test("parseWorkflowFile rejects malformed workflow file names", () => {
  for (const value of ["../example.yml", "workflows/example.yml", "example"]) {
    assert.throws(() => parseWorkflowFile(value));
  }
});

test("parseEventType accepts a repository_dispatch event type", () => {
  assert.equal(parseEventType("example-event"), "example-event");
});

test("parseEventType rejects malformed event types", () => {
  for (const value of ["Example-Event", "example_event", "example--event"]) {
    assert.throws(() => parseEventType(value));
  }
});

test("expectedRunTitle correlates a unique request", () => {
  assert.equal(
    expectedRunTitle("67d76238-b7a5-41b9-bec2-0ab809b30a90"),
    "Release verification 67d76238-b7a5-41b9-bec2-0ab809b30a90",
  );
});

test("createAppJwt signs a short-lived GitHub App token", () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", {
    modulusLength: 2048,
  });
  const now = 1_800_000_000;
  const jwt = createAppJwt("12345", privateKey, now);
  const [header, payload, signature] = jwt.split(".");

  assert.deepEqual(
    JSON.parse(Buffer.from(header, "base64url").toString()),
    { alg: "RS256", typ: "JWT" },
  );
  assert.deepEqual(
    JSON.parse(Buffer.from(payload, "base64url").toString()),
    { iat: now - 60, exp: now + 540, iss: "12345" },
  );

  const verifier = createVerify("RSA-SHA256");
  verifier.update(`${header}.${payload}`);
  verifier.end();
  assert.equal(verifier.verify(publicKey, signature, "base64url"), true);
});

async function runMainWithFetch(fetchHandler) {
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const originalEnvironment = { ...process.env };
  const originalFetch = globalThis.fetch;
  const originalLog = console.log;
  const requests = [];
  const logLines = [];

  Object.assign(process.env, {
    APP_ID: "12345",
    APP_PRIVATE_KEY: privateKey.export({ type: "pkcs8", format: "pem" }),
    TARGET_REPO: "example-org/example-target",
    TARGET_WORKFLOW_FILE: "example.yaml",
    TARGET_EVENT_TYPE: "example-event",
    VERSION: "0.12.0-rc.1",
    SOURCE_REPO: "mlrun/ce",
    SOURCE_SHA: "a".repeat(40),
    SOURCE_RUN_ID: "40212874786",
    SOURCE_RUN_ATTEMPT: "2",
    TRIGGERED_BY: "release-operator",
    GITHUB_API_URL: "https://api.github.test",
  });
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return fetchHandler(url, options, requests);
  };
  console.log = (line) => logLines.push(line);

  try {
    await main();
    return { requests, logLines, error: null };
  } catch (error) {
    return { requests, logLines, error };
  } finally {
    process.env = originalEnvironment;
    globalThis.fetch = originalFetch;
    console.log = originalLog;
  }
}

function tokenHandler() {
  let tokenRequestCount = 0;
  const handler = (url, options) => {
    if (url.endsWith("/installation")) {
      return new Response(JSON.stringify({ id: 99 }), { status: 200 });
    }
    if (url.endsWith("/app/installations/99/access_tokens")) {
      tokenRequestCount += 1;
      return new Response(
        JSON.stringify({
          token: `installation-token-${tokenRequestCount}`,
          expires_at: new Date(
            Date.now() + (tokenRequestCount === 1 ? 60_000 : 3_600_000),
          ).toISOString(),
        }),
        { status: 201 },
      );
    }
    if (url.endsWith("/installation/token") && options.method === "DELETE") {
      return new Response(null, { status: 204 });
    }
    return null;
  };
  handler.tokenRequestCount = () => tokenRequestCount;
  return handler;
}

test("main dispatches and waits for the correlated run", async () => {
  const tokens = tokenHandler();
  const { requests, logLines, error } = await runMainWithFetch(
    (url, options, requests) => {
      const tokenResponse = tokens(url, options);
      if (tokenResponse) {
        return tokenResponse;
      }
      if (url.endsWith("/dispatches")) {
        return new Response(null, { status: 204 });
      }
      if (url.includes("/actions/workflows/example.yaml")) {
        const dispatch = requests.find(({ url: requestUrl }) =>
          requestUrl.endsWith("/dispatches"),
        );
        const correlationId = JSON.parse(
          dispatch.options.body,
        ).client_payload.correlation_id;
        return new Response(
          JSON.stringify({
            workflow_runs: [
              {
                display_title: expectedRunTitle(correlationId),
                created_at: new Date(Date.now() - 3_600_000).toISOString(),
                status: "completed",
                conclusion: "failure",
              },
              {
                display_title: expectedRunTitle(correlationId),
                created_at: new Date().toISOString(),
                status: "completed",
                conclusion: "success",
              },
            ],
          }),
          { status: 200 },
        );
      }
      throw new Error(`Unexpected test URL: ${url}`);
    },
  );

  assert.equal(error, null);
  assert.deepEqual(logLines, [
    "Release verification started",
    "Release verification succeeded",
  ]);

  const dispatch = requests.find(({ url }) => url.endsWith("/dispatches"));
  const payload = JSON.parse(dispatch.options.body);
  assert.equal(payload.event_type, "example-event");
  assert.deepEqual(Object.keys(payload.client_payload).sort(), [
    "correlation_id",
    "source_repo",
    "source_run_attempt",
    "source_run_id",
    "source_sha",
    "triggered_by",
    "version",
  ]);
  assert.match(
    payload.client_payload.correlation_id,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );

  const tokenRequest = requests.find(({ url }) =>
    url.endsWith("/app/installations/99/access_tokens"),
  );
  assert.deepEqual(JSON.parse(tokenRequest.options.body).permissions, {
    actions: "read",
    contents: "write",
  });
  assert.equal(tokens.tokenRequestCount(), 2);
  assert.equal(
    requests.filter(
      ({ url, options }) =>
        url.endsWith("/installation/token") && options.method === "DELETE",
    ).length,
    2,
  );
  const statusRequest = requests.find(({ url }) =>
    url.includes("/actions/workflows/"),
  );
  assert.equal(
    statusRequest.options.headers.Authorization,
    "Bearer installation-token-2",
  );
});

test("main reports API failures with the shared generic message", async () => {
  const tokens = tokenHandler();
  const { requests, logLines, error } = await runMainWithFetch(
    (url, options) => {
      const tokenResponse = tokens(url, options);
      if (tokenResponse) {
        return tokenResponse;
      }
      if (url.endsWith("/dispatches")) {
        return new Response(JSON.stringify({ message: "Not Found" }), {
          status: 404,
        });
      }
      throw new Error(`Unexpected test URL: ${url}`);
    },
  );

  assert.equal(error.message, VERIFICATION_FAILED);
  assert.deepEqual(logLines, ["Release verification started"]);
  assert.equal(
    requests.filter(
      ({ url, options }) =>
        url.endsWith("/installation/token") && options.method === "DELETE",
    ).length,
    1,
  );
});

test("waitForVerification rejects a failed target run", async () => {
  const title = expectedRunTitle("correlation-id");
  const api = {
    installationRequest: async (_path, options) => {
      assert.equal(Number.isFinite(options.deadline), true);
      return {
        workflow_runs: [
          {
            display_title: title,
            created_at: new Date().toISOString(),
            status: "completed",
            conclusion: "failure",
          },
        ],
      };
    },
    repository: { owner: "example-org", name: "example-target" },
  };

  await assert.rejects(
    waitForVerification(
      api,
      "example.yaml",
      title,
      Date.now() - 60_000,
      Date.now() + 60_000,
    ),
  );
});
