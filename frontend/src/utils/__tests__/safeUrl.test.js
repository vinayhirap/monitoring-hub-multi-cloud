import test from "node:test";
import assert from "node:assert/strict";
import { isSafeHttpUrl, assertHttpUrl } from "../safeUrl.js";

test("accepts normal console URLs", () => {
  assert.equal(isSafeHttpUrl("https://signin.aws.amazon.com/federation?Action=login&SigninToken=abc"), true);
  assert.equal(isSafeHttpUrl("https://portal.azure.com/#@tenant/resource/x"), true);
  assert.equal(isSafeHttpUrl("http://example.com/x"), true);
});

test("rejects script and data schemes and junk", () => {
  for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>",
                     "vbscript:x", "//evil.example/x", "/relative", "", null, undefined, 42, "https://", "file:///etc/passwd"]) {
    assert.equal(isSafeHttpUrl(bad), false, String(bad));
  }
});

test("assertHttpUrl returns the url or throws", () => {
  assert.equal(assertHttpUrl("https://a.example/x"), "https://a.example/x");
  assert.throws(() => assertHttpUrl("javascript:alert(1)"), /unexpected link/);
});
