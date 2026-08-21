// Canonical Reader learning cards are separate from page placements. Codex
// Voice addresses them by stable batch id + card index, applies optimistic
// revision guards, and projects known external notes through a bounded
// AnkiConnect/sync bridge without making Anki the Reader source of truth.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const MCP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
);
const QUERY = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
);
const OUTPUT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const DIRECT_PROTOCOL = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs",
);
const DIRECT_SERVER = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeServer.cs",
);
const LOCAL_ANKI = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderLocalAnki.cs",
);
const PACKAGE = read(
  "extensions/bw-reader-webext/windows/package_computer_voice_direct.py",
);
const COMPUTER = read("_server_deploy/static/pdf/rc-computer-voice.js");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const REVIEW = read("_server_deploy/static/pdf/rc-review.js");
const PI = read("_server_deploy/pdf_reader.py");

function method(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const open = source.indexOf("{", start);
  assert.notEqual(open, -1, `missing body for ${signature}`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  assert.fail(`unbalanced ${signature}`);
}

function between(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

test("五个 canonical learning-card MCP 工具同时注册进源码与 Direct 候选", () => {
  const tools = new Map([
    ["LearningCardsToolName", "reader_learning_cards"],
    ["LearningCardReadToolName", "reader_learning_card_read"],
    ["LearningCardEditToolName", "reader_learning_card_edit"],
    ["LearningCardDeleteToolName", "reader_learning_card_delete"],
    ["ReviewCurrentCardToolName", "reader_review_current_card"],
  ]);
  for (const [constant, name] of tools) {
    assert.ok(
      MCP.includes(`internal const string ${constant} = "${name}";`),
      `${name} must have one stable MCP constant`,
    );
    assert.ok(
      PACKAGE.includes(`"${name}"`),
      `${name} must survive immutable Direct packaging`,
    );
  }

  const readSpecs = between(
    MCP,
    '["name"] = LearningCardsToolName',
    '["name"] = NotesToolName',
  );
  assert.match(readSpecs, /\["name"\] = LearningCardReadToolName/);
  assert.match(readSpecs, /\["name"\] = ReviewCurrentCardToolName/);
  assert.ok(
    (readSpecs.match(/\["annotations"\] = ReadOnlyAnnotations\(\)/g) || [])
      .length >= 3,
    "all three query tools must stay read-only",
  );

  const editSpec = between(
    MCP,
    '["name"] = LearningCardEditToolName',
    '["name"] = LearningCardDeleteToolName',
  );
  assert.match(editSpec, /BuildLearningCardEditArgumentsSchema\(\)/);
  assert.match(editSpec, /\["destructiveHint"\] = false/);
  const deleteSpec = MCP.slice(
    MCP.indexOf('["name"] = LearningCardDeleteToolName'),
    MCP.indexOf('["name"] = NoteCreateToolName'),
  );
  assert.match(deleteSpec, /BuildLearningCardDeleteArgumentsSchema\(\)/);
  assert.match(deleteSpec, /\["destructiveHint"\] = true/);
});

test("三条 Reader 查询在 MCP、桥接白名单和页面执行侧一一对应", () => {
  const dispatch = between(
    MCP,
    "toolName == LearningCardsToolName",
    "toolName == TocToolName",
  );
  assert.match(dispatch, /RunReaderQueryAsync\([\s\S]*"learning-cards"/);
  assert.match(dispatch, /RunReaderQueryAsync\([\s\S]*"learning-card"/);
  assert.match(dispatch, /RunReaderQueryAsync\([\s\S]*"review-current"/);
  assert.match(dispatch, /TryReadLearningCardIdentity/);
  assert.match(dispatch, /HasNoArguments\(arguments\)/);

  const queryNames = method(QUERY, "internal static bool IsQuery(");
  for (const query of ["learning-cards", "learning-card", "review-current"]) {
    assert.ok(queryNames.includes(`"${query}"`));
    assert.ok(COMPUTER.includes(`"${query}": function`));
  }
  const surfaces = method(QUERY, "internal static bool IsQueryForSurface(");
  assert.match(
    surfaces,
    /"learning-cards" or "learning-card" or "review-current"[\s\S]*kind is "pdf" or "epub" or "web"/,
  );
  assert.match(
    QUERY,
    /query is "learning-cards" or "learning-card" or "review-current"[\s\S]*MaximumLearningCardResultBytes/,
    "complete card/source/state payloads use the dedicated safety ceiling",
  );
});

test("单卡读写拒绝模糊身份，并以不同 revision 守卫编辑和删除", () => {
  const identity = method(MCP, "private static JsonObject BuildLearningCardIdentitySchema()");
  assert.match(identity, /\["additionalProperties"\] = false/);
  assert.match(identity, /\["required"\] = new JsonArray\("id", "cardIndex"\)/);
  const idSchema = method(MCP, "private static JsonObject LearningCardIdSchema()");
  assert.ok(idSchema.includes('["pattern"] = "^card_[a-f0-9]{4,64}$"'));
  const indexSchema = method(MCP, "private static JsonObject LearningCardIndexSchema()");
  assert.match(indexSchema, /\["minimum"\] = 0/);
  assert.match(indexSchema, /\["maximum"\] = 255/);

  const edit = method(MCP, "private static JsonObject BuildLearningCardEditArgumentsSchema()");
  assert.match(
    edit,
    /\["required"\] = new JsonArray\([\s\S]*"id", "cardIndex", "expectedEntityRevision", "card"/,
  );
  assert.doesNotMatch(edit, /expectedStateRevision/);
  const remove = method(MCP, "private static JsonObject BuildLearningCardDeleteArgumentsSchema()");
  assert.match(
    remove,
    /\["required"\] = new JsonArray\([\s\S]*"id", "cardIndex", "expectedStateRevision"/,
  );
  assert.doesNotMatch(remove, /expectedEntityRevision|"card"/);

  const identityParser = method(MCP, "internal static bool TryReadLearningCardIdentity(");
  assert.match(identityParser, /RequireNoDuplicateKeys\(arguments\)/);
  assert.match(identityParser, /fields\.Length != 2/);
  assert.match(identityParser, /names\.SetEquals\(new\[\] \{ "id", "cardIndex" \}\)/);
  assert.match(identityParser, /cardIndex is < 0 or > 255/);

  const mutationParser = method(MCP, "internal static bool TryReadLearningCardMutation(");
  assert.match(mutationParser, /RequireNoDuplicateKeys\(arguments\)/);
  assert.match(mutationParser, /actual\.SetEquals\(expected\)/);
  assert.match(mutationParser, /IsLearningCardId\(id\)/);
  assert.match(mutationParser, /index is < 0 or > 255/);
  assert.match(mutationParser, /revision is < 0 or > MaximumSafeInteger/);
  assert.match(mutationParser, /"expectedEntityRev"[\s\S]*"expectedStateRev"/);

  const outputGuard = method(OUTPUT, "private static void ValidateClientAction(");
  assert.match(outputGuard, /fn is "_nativeReaderLearningCardMutate"/);
  assert.match(outputGuard, /Exact\([\s\S]*"expectedEntityRev"[\s\S]*"card"/);
  assert.match(outputGuard, /Exact\([\s\S]*"expectedStateRev"/);
  assert.match(outputGuard, /cardIndex is < 0 or > 255/);
  assert.match(VOICECALL, /_nativeReaderLearningCardMutate/);
  assert.match(VOICECALL, /_caLearning\.expectedEntityRev/);
  assert.match(VOICECALL, /_caLearning\.expectedStateRev/);
});

test("默认 sync-if-projected 只投影已有外部 note，并保留 reader-only 退出路径", () => {
  const policy = method(MCP, "private static JsonObject LearningCardExternalPolicySchema()");
  assert.match(policy, /new JsonArray\("sync-if-projected", "reader-only"\)/);
  assert.match(policy, /\["default"\] = "sync-if-projected"/);
  const parser = method(MCP, "internal static bool TryReadLearningCardMutation(");
  assert.match(parser, /string externalPolicy = "sync-if-projected"/);
  assert.match(parser, /policy is not \("sync-if-projected" or "reader-only"\)/);

  const projection = method(COMPUTER, "function projectLearningCardMutation(");
  assert.match(
    projection,
    /receipt\.status === "succeeded" \|\| receipt\.status === "failed"/,
  );
  assert.doesNotMatch(projection, /receipt\.status === "unknown"/);
  assert.match(projection, /Array\.isArray\(receipt\.noteIds\)/);
  assert.match(projection, /receipt\.noteIds\.length/);
  const mutation = method(COMPUTER, "function nativeReaderLearningCardMutate(");
  assert.match(mutation, /value\.externalPolicy === "reader-only"/);
  assert.match(mutation, /projectLearningCardMutation\(/);
  assert.match(mutation, /selected\.state\.removed === true/);
  assert.match(mutation, /BW_READER_LEARNING_CARD_REMOVED/);
  assert.match(mutation, /BW_READER_LEARNING_CARD_CONFLICT/);
  assert.match(mutation, /reader_applied: \{ status: "succeeded", dedup: true \}/);
  assert.match(mutation, /external_results: \{\}/);
});

test("Windows 与 Pi 分流到各自 AnkiConnect 入口，且成功写入后请求同步", () => {
  const localClient = method(COMPUTER, "function operateLocalAnkiCard(");
  assert.match(localClient, /"anki-card-operation-local"/);
  assert.match(localClient, /BW_READER_ANKI_OPERATION_OUTCOME_UNKNOWN/);
  assert.match(localClient, /error\.outcomeUnknown = true/);
  assert.match(DIRECT_PROTOCOL, /case "anki-card-operation-local":/);
  assert.match(DIRECT_SERVER, /"anki-card-operation-local"/);
  assert.match(LOCAL_ANKI, /"update-note-fields"/);
  assert.match(LOCAL_ANKI, /"delete-notes"/);
  assert.match(LOCAL_ANKI, /"answer-cards"/);
  assert.match(LOCAL_ANKI, /"sync"/);

  const piClient = method(COMPUTER, "function operatePiAnkiCard(");
  assert.match(piClient, /delete payload\.syncMode/);
  assert.match(piClient, /fetch\("\/pdf\/api\/anki-card-operation"/);
  assert.match(
    PI,
    /@bp\.route\("\/api\/anki-card-operation", methods=\["POST"\]\)/,
  );
  const piValidation = between(
    PI,
    "def _anki_card_operation_validate(",
    "def _anki_card_operation_info_ids(",
  );
  for (const operation of [
    "read-notes",
    "read-cards",
    "update-note-fields",
    "delete-notes",
    "answer-cards",
    "sync",
  ]) {
    assert.ok(piValidation.includes(`"${operation}"`));
  }
  assert.match(PI, /_anki_card_operation_connect\("sync"\)/);
  assert.match(COMPUTER, /syncMode: "background"/);
});

test("修改回执分开报告 Reader、本机 Anki、AnkiWeb；删除明确为 note 级", () => {
  const layers = between(
    PI,
    "def _anki_card_operation_layers(",
    "def _anki_card_operation_sync_layer(",
  );
  for (const layer of [
    "reader_applied",
    "anki_local_applied",
    "anki_web_sync",
  ]) {
    assert.ok(layers.includes(`"${layer}"`));
  }

  const projection = method(COMPUTER, "function projectLearningCardMutation(");
  assert.match(projection, /reader_applied: \{ status: "succeeded" \}/);
  assert.match(projection, /anki_local_applied: \{ status: "pending" \}/);
  assert.match(projection, /anki_web_sync: \{ status: "not-requested" \}/);
  assert.match(projection, /delete_scope: "note"/);
  assert.match(projection, /deleted_note_ids: projections\[target\]\.noteIds/);

  const externalWrite = method(COMPUTER, "function writeProjectedAnki(");
  assert.match(externalWrite, /operation: "delete-notes"/);
  assert.match(externalWrite, /noteIds: noteIds/);
  assert.doesNotMatch(externalWrite, /delete-cards/);
  assert.match(PI, /_anki_card_operation_connect\([\s\S]*"deleteNotes"/);
  assert.match(PI, /"delete_scope": "note"/);
  assert.match(PI, /"deleted_note_ids": payload\["noteIds"\]/);
});

test("编辑保留 Anki 出处脚注，复习删除也走同一投影链", () => {
  const fields = method(COMPUTER, "function ankiFieldsForReaderCard(");
  assert.match(
    fields,
    /<hr><div class="bw-reader-anki-source">来源：/,
  );
  assert.match(fields, /legacyMarker/);
  assert.match(fields, /legacyMarker[\s\S]*safeMarker/);
  assert.match(fields, /provenanceFooter\(textField\)/);
  assert.match(fields, /provenanceFooter\(front\)/);
  assert.match(fields, /provenanceFooter\(back\)/);
  assert.match(fields, /ankiProjectionHtml\(card\.front \|\| ""\) \+ "<hr>"/);

  const piClient = method(COMPUTER, "function operatePiAnkiCard(");
  assert.match(piClient, /toLowerCase\(\)[\s\S]*indexOf\("unknown"\)/);
  assert.match(
    COMPUTER,
    /addEventListener\("rc:learning-card-removed",[\s\S]*?projectLearningCardMutation\([\s\S]*?"delete"/,
  );
  assert.match(COMPUTER, /"rc:learning-card-removal-projected"/);
  assert.match(
    COMPUTER,
    /addEventListener\("rc:learning-card-rated",[\s\S]*?projectReaderPcReviewRating/,
  );
  const rating = method(COMPUTER, "function projectReaderPcReviewRating(");
  assert.match(rating, /projections\.anki\.readerpc/);
  assert.match(rating, /cardIds\.length !== 1/);
  assert.match(rating, /operation: "answer-cards"/);
  assert.match(rating, /syncMode: "background"/);
  assert.match(REVIEW, /CustomEvent\('rc:learning-card-rated'/);

  const legacyDelete = method(REVIEW, "function _deleteLegacyReviewCard(");
  assert.match(legacyDelete, /operation: 'delete-notes'/);
  assert.match(legacyDelete, /noteIds: \[noteId\]/);
  assert.match(legacyDelete, /fetch\('\/pdf\/api\/anki-card-operation'/);
  assert.match(legacyDelete, /anki_web_sync/);
  assert.match(
    PI,
    /response = \{[\s\S]*?"next": nxt,[\s\S]*?"anki_web_sync": sync_layer/,
  );
});

test("Anki add/edit 先投影 Markdown，Windows 与 Pi 再安全本地化图片", () => {
  const card = method(COMPUTER, "function normalizeLocalAnkiCard(");
  assert.match(card, /canonical[\s\S]*projectionField\(canonical\.front/);
  assert.match(card, /projectionField\(canonical\.back/);
  assert.match(card, /projectionField\(canonical\.cloze/);
  assert.match(card, /64000/);
  const addRequest = method(
    COMPUTER,
    "function normalizeLocalAnkiAddRequest(",
  );
  assert.match(addRequest, /card: normalizedCard\.canonical/);
  assert.match(addRequest, /projection: normalizedCard\.projection/);
  assert.match(addRequest, /192 \* 1024/);
  const projection = method(COMPUTER, "function ankiProjectionHtml(");
  assert.match(projection, /typeof RC\.md === "function"/);
  assert.match(projection, /querySelectorAll\("img"\)/);
  assert.match(projection, /ankiProjectionImageSource/);
  const source = method(COMPUTER, "function ankiProjectionImageSource(");
  assert.match(source, /parsed\.protocol !== "https:"/);
  assert.match(source, /BW_READER_ANKI_MEDIA_URL_INVALID/);
  assert.match(source, /localhost\|local\|lan\|internal/);

  assert.match(LOCAL_ANKI, /class BoundedReaderPublicImageFetcher/);
  assert.match(LOCAL_ANKI, /Dns\.GetHostAddressesAsync/);
  assert.match(LOCAL_ANKI, /ConnectCallback/);
  assert.match(LOCAL_ANKI, /UseProxy = false/);
  assert.match(LOCAL_ANKI, /"storeMediaFile"/);
  assert.match(LOCAL_ANKI, /Convert\.ToBase64String\(image\.Data\)/);
  assert.match(LOCAL_ANKI, /"bw-reader-img-" \+ digest \+ extension/);
  assert.match(LOCAL_ANKI, /ValidateImageContent/);
  assert.match(LOCAL_ANKI, /MaximumProjectionImages = 8/);
  assert.match(LOCAL_ANKI, /MaximumProjectionImageBytes = 32L \* 1024 \* 1024/);
  assert.match(LOCAL_ANKI, /registered\.CanonicalCard\.DeepClone/);
  assert.match(LOCAL_ANKI, /NormalizeProjectionCard/);
  assert.match(
    LOCAL_ANKI,
    /canonicalCard\["type"\][\s\S]*projectionCard\["type"\][\s\S]*类型不一致/,
  );
  assert.match(LOCAL_ANKI, /srcset\|style\|xlink:href/);
  assert.doesNotMatch(LOCAL_ANKI, /NetworkStylePattern/);
  assert.match(LOCAL_ANKI, /maximumTextLength: 64_000/);
  assert.match(DIRECT_PROTOCOL, /hasProjection \? projectionValue : cardValue/);
  assert.match(DIRECT_PROTOCOL, /192 \* 1024/);

  assert.match(PI, /def _anki_projection_localize_fields\(/);
  assert.match(
    PI,
    /_fetch_public_image\([\s\S]*allowed_schemes=\("https",\)/,
  );
  assert.match(PI, /"storeMediaFile"/);
  assert.match(PI, /base64\.b64encode\(content\)/);
  assert.match(PI, /def _anki_projection_media_filename\(/);
  assert.match(PI, /hashlib\.sha256\(bytes\(content\)\)/);
  assert.match(PI, /class _AnkiProjectionHtmlInspector/);
  assert.match(PI, /if lowered == "style"/);
  assert.match(PI, /_ANKI_PROJECTION_MAX_IMAGES = 8/);
  assert.match(
    PI,
    /remaining_time = state\.deadline - time\.monotonic\(\)[\s\S]*"storeMediaFile"[\s\S]*timeout=min\(15\.0/,
  );
  assert.match(PI, /anki_add_partial_outcome_unknown/);
  assert.doesNotMatch(PI, /card\.get\("front"\)[^\n]*\[:8000\]/);
  assert.match(PI, /import markdown[\s\S]*markdown\.markdown\(/);
});
