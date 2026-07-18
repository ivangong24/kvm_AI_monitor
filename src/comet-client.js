import https from "node:https";

export function multipartBody({ fields = {}, boundary = `----kvm-ai-${Date.now()}` }) {
  const chunks = [];
  const line = (value) => chunks.push(Buffer.from(value, "utf8"));

  for (const [name, value] of Object.entries(fields)) {
    line(`--${boundary}\r\n`);
    line(`Content-Disposition: form-data; name="${name}"\r\n\r\n`);
    line(`${value}\r\n`);
  }

  line(`--${boundary}--\r\n`);
  return { boundary, body: Buffer.concat(chunks) };
}

function unwrapResponse(payload) {
  if (payload?.ok === false) {
    const message = payload.result?.message ?? payload.error ?? "Comet API request failed";
    throw new Error(String(message));
  }
  return payload?.result ?? payload;
}

export class CometClient {
  constructor({ host, password = "", username = "admin", token = null }) {
    this.host = host;
    this.password = password;
    this.username = username;
    this.token = token;
  }

  request(path, { method = "GET", body = null, headers = {} } = {}) {
    return new Promise((resolve, reject) => {
      const request = https.request(
        {
          hostname: this.host,
          port: 443,
          path,
          method,
          rejectUnauthorized: false,
          timeout: 15_000,
          headers: {
            Accept: "application/json",
            ...(this.token ? { token: this.token } : {}),
            ...(body ? { "Content-Length": body.length } : {}),
            ...headers,
          },
        },
        (response) => {
          const chunks = [];
          response.on("data", (chunk) => chunks.push(chunk));
          response.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf8");
            let payload = null;
            try {
              payload = text ? JSON.parse(text) : {};
            } catch {
              // Preserve the HTTP status when a proxy returns a non-JSON error page.
            }
            if ((response.statusCode ?? 500) >= 400) {
              const detail =
                payload?.result?.message ??
                payload?.result?.reason ??
                payload?.result?.error ??
                payload?.error ??
                payload?.message ??
                payload?.result?.code;
              reject(
                new Error(
                  `Comet API returned HTTP ${response.statusCode}${detail ? `: ${detail}` : ""}`,
                ),
              );
              return;
            }
            try {
              resolve(unwrapResponse(payload ?? {}));
            } catch (error) {
              reject(error);
            }
          });
        },
      );
      request.on("timeout", () => request.destroy(new Error("Comet API request timed out")));
      request.on("error", reject);
      if (body) request.write(body);
      request.end();
    });
  }

  async login(twoFactorCode = "") {
    const { boundary, body } = multipartBody({
      fields: { user: this.username, passwd: `${this.password}${twoFactorCode}` },
    });
    const result = await this.request("/api/auth/login", {
      method: "POST",
      body,
      headers: { "Content-Type": `multipart/form-data; boundary=${boundary}` },
    });
    if (!result?.token) throw new Error("Comet login succeeded without an auth token");
    this.token = result.token;
    return result;
  }

}
