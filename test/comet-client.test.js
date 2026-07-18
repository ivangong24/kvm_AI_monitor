import assert from "node:assert/strict";
import test from "node:test";
import { multipartBody } from "../src/comet-client.js";

test("Comet multipart body encodes login fields", () => {
  const { boundary, body } = multipartBody({
    boundary: "test-boundary",
    fields: { user: "admin", passwd: "secret" },
  });
  const text = body.toString("utf8");
  assert.equal(boundary, "test-boundary");
  assert.match(text, /name="user"\r\n\r\nadmin/);
  assert.match(text, /name="passwd"\r\n\r\nsecret/);
  assert.ok(text.endsWith("--test-boundary--\r\n"));
});
