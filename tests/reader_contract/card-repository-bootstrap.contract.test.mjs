import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


const manifest = JSON.parse(readFileSync(
  new URL("../../ios/BWReader/native_reader_interface_manifest.json", import.meta.url),
  "utf8",
));


test("legacy card bootstrap is an authenticated Pi gateway route without a remote-book identity", () => {
  const route = manifest.routes.find(
    (candidate) => candidate.path === "/pdf/api/card-repository/bootstrap",
  );
  assert.deepEqual(route, {
    path: "/pdf/api/card-repository/bootstrap",
    match: "exact",
    owner: "pi",
    methods: ["GET"],
    surfaces: ["epub", "pdf"],
    status: "supported",
    remoteBook: null,
    description: "Read a private paginated legacy card snapshot for the App-local repository bootstrap.",
  });
});
