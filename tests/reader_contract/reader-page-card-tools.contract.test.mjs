// Windows Codex voice exposes page-card reads and guarded writes through MCP.
//
// The visible number is deliberately not an identity: deleting a lower card
// renumbers the page, and free placements have no number at all. Every write
// therefore carries the stable placement id and record revision from the last
// read. A bound card may additionally carry its visible number as a shortcut;
// the runtime then checks number + id + revision together.
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
const COMPUTER = read("_server_deploy/static/pdf/rc-computer-voice.js");

function method(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  assert.fail(`unbalanced ${signature}`);
}

function toolSpec(constant, length = 5200) {
  const marker = `["name"] = ${constant}`;
  const start = MCP.indexOf(marker);
  assert.notEqual(start, -1, `missing tool ${constant}`);
  return MCP.slice(start, start + length);
}

test("四个页面卡片工具都注册在与实现相同的依赖门下", () => {
  for (const name of [
    "reader_page_cards",
    "reader_page_card_read",
    "reader_page_card_edit",
    "reader_page_card_delete",
  ]) {
    assert.match(MCP, new RegExp(`"${name}"`));
  }
  const queryBlock = MCP.slice(
    MCP.indexOf("if (_queryReaderAsync is not null)"),
    MCP.indexOf("return new JsonObject", MCP.indexOf("if (_queryReaderAsync is not null)")),
  );
  assert.match(queryBlock, /\["name"\] = PageCardsToolName/);
  assert.match(queryBlock, /\["name"\] = PageCardReadToolName/);
  const outputBlock = MCP.slice(
    MCP.indexOf("if (_sendOutputAsync is not null)"),
    MCP.indexOf("if (_queryReaderAsync is not null)"),
  );
  assert.match(outputBlock, /\["name"\] = PageCardEditToolName/);
  assert.match(outputBlock, /\["name"\] = PageCardDeleteToolName/);
});

test("索引读保持有界，单卡读按稳定选择器分块返回完整源内容", () => {
  const spec = toolSpec("PageCardsToolName", 2400);
  const prose = spec.replace(/"\s*\+\s*"/g, "");
  assert.match(spec, /\["properties"\] = new JsonObject\(\)/);
  assert.match(spec, /\["additionalProperties"\] = false/);
  assert.match(spec, /\["annotations"\] = ReadOnlyAnnotations\(\)/);
  assert.match(prose, /same automatic number order shown beside the page text/);
  assert.match(prose, /bounded index/);
  assert.match(prose, /anchor-word label/);
  assert.match(prose, /stable id and the shared revision/);
  assert.match(prose, /fallback index for cards absent from the snapshot/);
  assert.match(prose, /reader_page_card_read only for a source marked truncated/);

  const readSpec = toolSpec("PageCardReadToolName", 4200);
  const readProse = readSpec.replace(/"\s*\+\s*"/g, "");
  assert.match(readProse, /exactly one selector/);
  assert.match(readProse, /stable JSON/);
  assert.match(readProse, /bounded chunks/);
  assert.match(readProse, /next_offset/);
  assert.match(readProse, /expectedRevision/);
  assert.match(readProse, /restart from offset 0/);
  assert.match(readSpec, /\["annotations"\] = ReadOnlyAnnotations\(\)/);

  const handler = MCP.slice(
    MCP.indexOf("toolName == PageCardsToolName"),
    MCP.indexOf("toolName == TocToolName"),
  );
  assert.match(handler, /HasNoArguments\(arguments\)/);
  assert.match(handler, /RunReaderQueryAsync\([\s\S]*"page-cards"/);
  assert.match(QUERY, /or "page-text" or "page-cards" or "page-card" or "lookup"/);
  assert.match(
    QUERY,
    /"search" or "page-text"[\s\S]*kind is "pdf" or "epub"[\s\S]*"page-cards" => kind is "pdf"/,
  );
});

test("写工具 schema 强制 id + expectedRevision，number 只是锚定卡可选别名", () => {
  const guards = method(MCP, "private static JsonObject BuildPageCardGuardProperties()");
  assert.match(guards, /\["minimum"\] = 1/);
  assert.ok(
    guards.includes('["pattern"] = "^[A-Za-z0-9_-]{2,96}$"'),
    "placement id must use the native localRecordId contract",
  );
  assert.match(guards, /\["id"\] = new JsonObject/);
  assert.doesNotMatch(guards, /\["expectedId"\] = new JsonObject/);
  assert.match(guards, /Optional current visible number for a bound card/);
  assert.match(guards, /\["minimum"\] = 0/);
  assert.match(guards, /\["maximum"\] = MaximumSafeInteger/);

  const editSchema = method(MCP, "private static JsonObject BuildPageCardEditArgumentsSchema()");
  for (const field of ["id", "expectedRevision"]) {
    assert.match(editSchema, new RegExp(`"${field}"`));
  }
  assert.match(
    editSchema,
    /\["required"\] = new JsonArray\(\s*"id",\s*"expectedRevision"\)/,
  );
  assert.doesNotMatch(
    editSchema,
    /\["required"\] = new JsonArray\(\s*"number"/,
  );
  assert.match(editSchema, /\["oneOf"\] = new JsonArray/);
  assert.match(editSchema, /new JsonArray\("content"\)/);
  assert.match(editSchema, /new JsonArray\("cards"\)/);
  assert.match(
    editSchema,
    /ReaderRealtimeOutputProtocol\.MaximumPageCardContentCharacters/,
  );

  const deleteSchema = method(MCP, "private static JsonObject BuildPageCardDeleteArgumentsSchema()");
  assert.doesNotMatch(deleteSchema, /"content"|"cards"/);
  assert.match(deleteSchema, /BuildPageCardGuardProperties\(\)/);
  assert.match(
    deleteSchema,
    /\["required"\] = new JsonArray\(\s*"id",\s*"expectedRevision"\)/,
  );
  assert.doesNotMatch(
    deleteSchema,
    /\["required"\] = new JsonArray\(\s*"number"/,
  );
});

test("学习卡 replacement 只接受严格 basic/cloze 结构", () => {
  const schema = method(MCP, "private static JsonObject BuildPageCardCardsSchema()");
  assert.match(schema, /\["minItems"\] = 1/);
  assert.match(schema, /\["maxItems"\] = 12/);
  assert.match(schema, /new JsonArray\("type", "front", "back"\)/);
  assert.match(schema, /new JsonArray\("type", "cloze"\)/);
  assert.match(
    schema,
    /\["back"\] = PageCardFaceSchema\(\)/,
    "basic back is required and non-empty, not the draft-only empty-back shape",
  );
  assert.match(schema, /\["cloze"\] = PageCardClozeFaceSchema\(\)/);
  assert.doesNotMatch(schema, /"page"/, "replacement cards never accept a page field");
  assert.ok(
    (schema.match(/\["additionalProperties"\] = false/g) || []).length >= 2,
    "both card variants must reject unknown fields",
  );

  const validator = method(MCP, "private static bool ValidatePageCardReplacementCards(");
  assert.match(validator, /fields\.SetEquals\(new\[\] \{ "type", "front", "back" \}\)/);
  assert.match(validator, /fields\.SetEquals\(new\[\] \{ "type", "cloze" \}\)/);
  assert.match(validator, /type == "basic"/);
  assert.match(validator, /type == "cloze"/);
  assert.match(validator, /TryReadPageCardFace\(card, "back", allowEmpty: false\)/);
  assert.match(validator, /ContainsPageCardClozeDeletion/);
  assert.match(validator, /else[\s\S]*return false/);

  const clozeSchema = method(MCP, "private static JsonObject PageCardClozeFaceSchema()");
  assert.match(
    clozeSchema,
    /ReaderRealtimeOutputProtocol\.MaximumPageCardContentCharacters/,
  );
  const faceSchema = method(MCP, "private static JsonObject PageCardFaceSchema()");
  assert.match(
    faceSchema,
    /ReaderRealtimeOutputProtocol\.MaximumPageCardContentCharacters/,
  );
  assert.ok(
    clozeSchema.includes('["pattern"] = "\\\\{\\\\{c[1-9][0-9]*::[\\\\s\\\\S]+?\\\\}\\\\}"'),
    "schema tells the caller that canonical cloze markup is mandatory",
  );
  const clozeCheck = method(MCP, "private static bool ContainsPageCardClozeDeletion(");
  assert.match(clozeCheck, /value\[cursor\] is < '1' or > '9'/);
  assert.match(clozeCheck, /value\.IndexOf\("}}", contentStart/);
  assert.match(clozeCheck, /close > contentStart/);
});

test("工具处理器严格校验形状后才生成 pcard 操作并发送 client-action", () => {
  const parser = method(MCP, "internal static bool TryReadPageCardMutation(");
  assert.match(parser, /DirectJsonValidation\.RequireNoDuplicateKeys\(arguments\)/);
  assert.match(parser, /hasContent == hasCards/);
  assert.match(parser, /actual\.SetEquals\(expected\)/);
  assert.match(parser, /\["id", "expectedRevision"\]/);
  assert.match(
    parser,
    /actual\.Contains\("number"\)[\s\S]*expected\.Add\("number"\)/,
  );
  assert.match(parser, /TryGetProperty\(\s*"id",\s*out JsonElement idValue\)/);
  assert.match(
    parser,
    /TryGetProperty\(\s*"number",\s*out JsonElement numberValue\)/,
  );
  assert.match(parser, /parsedNumber < 1/);
  assert.match(parser, /IsPageCardPlacementId\(expectedId\)/);
  assert.match(parser, /expectedRevision > MaximumSafeInteger/);
  assert.match(parser, /\["expectedId"\] = expectedId/);
  assert.match(
    parser,
    /if \(number is int currentNumber\)[\s\S]*mutation\["number"\] = currentNumber/,
  );
  assert.match(parser, /"pcard_" \+ Guid\.NewGuid\(\)\.ToString\("N"\)\[\.\.24\]/);
  assert.match(parser, /\["fn"\] = "_nativeReaderPageCardMutate"/);
  assert.match(parser, /\["operation"\] = operation/);
  assert.match(parser, /\["replacement"\] = replacement/);
  assert.match(
    parser,
    /ReaderRealtimeOutputProtocol\.ValidatePayload\([\s\S]*"client-action"/,
    "the MCP handler must pass the same C# envelope validator as every other output",
  );

  const dispatch = MCP.slice(
    MCP.indexOf("(toolName == PageCardEditToolName"),
    MCP.indexOf("toolName == NoteEditToolName"),
  );
  assert.match(dispatch, /\? "edit"\s*: "delete"/);
  assert.match(dispatch, /SendReaderOutputAsync\([\s\S]*"client-action"/);
});

test("C# 输出闸再次逐字段验证 edit/delete，删除没有 replacement", () => {
  const validator = method(OUTPUT, "private static void ValidateClientAction(");
  assert.match(validator, /or "_nativeReaderPageCardMutate"/);
  const pageCard = validator.slice(
    validator.indexOf('if (fn is "_nativeReaderPageCardMutate")'),
    validator.indexOf('if (fn is "_bwWebNoteCreate")'),
  );
  assert.match(pageCard, /operation == "edit"/);
  assert.match(pageCard, /operation == "delete"/);
  assert.match(
    pageCard,
    /bool hasNumber = mutation\.TryGetProperty\(\s*"number",\s*out JsonElement numberValue\)/,
  );
  assert.match(pageCard, /if \(hasNumber\)/);
  assert.match(
    pageCard,
    /hasNumber[\s\S]*numberValue\.TryGetInt32\(out int number\)[\s\S]*number < 1/,
  );
  assert.match(pageCard, /"pcard_"/);
  assert.match(pageCard, /pageCardOperationId\[operationPrefix\.Length\.\.\]/);
  assert.match(pageCard, /expectedId\.Length < 2/);
  assert.match(pageCard, /expectedRevision > 9_007_199_254_740_991L/);
  assert.match(pageCard, /hasContent == hasCards/);
  assert.match(pageCard, /ValidatePageCardReplacementCards/);
  assert.match(pageCard, /MaximumPageCardContentCharacters/);

  const deleteStart = pageCard.indexOf('else if (operation == "delete")');
  const deleteEnd = pageCard.indexOf(
    'throw Invalid("Reader 页面卡片操作无效")',
    deleteStart,
  );
  const deleteBranch = pageCard.slice(deleteStart, deleteEnd);
  assert.match(deleteBranch, /if \(hasNumber\)/);
  assert.match(deleteBranch, /"number"/);
  assert.match(deleteBranch, /"expectedRevision"/);
  assert.doesNotMatch(deleteBranch, /"replacement"/);

  const cards = method(OUTPUT, "private static void ValidatePageCardReplacementCards(");
  assert.match(cards, /GetArrayLength\(\) is < 1 or > 12/);
  assert.match(cards, /Exact\(card, "type", "front", "back"\)/);
  assert.match(cards, /"front",\s*MaximumPageCardContentCharacters/);
  assert.match(cards, /"back",\s*MaximumPageCardContentCharacters/);
  assert.match(cards, /Exact\(card, "type", "cloze"\)/);
  assert.match(cards, /ContainsPageCardClozeDeletion\(cloze\)/);
  assert.doesNotMatch(cards, /allowEmpty: true|"page"/);

  const protocolHeader = OUTPUT.slice(
    OUTPUT.indexOf("internal static class ReaderRealtimeOutputProtocol"),
    OUTPUT.indexOf("internal static bool IsKind"),
  );
  assert.match(
    protocolHeader,
    /MaximumPageCardContentCharacters = 100_000/,
  );
  const payloadValidator = method(OUTPUT, "internal static JsonNode ValidatePayload(");
  assert.match(
    payloadValidator,
    /IsPageCardMutation\(kind, payload\)[\s\S]*DirectBridgeContract\.MaximumMessageBytes[\s\S]*MaximumPayloadBytes/,
    "only page-card mutation payloads may use the direct-frame budget",
  );
});

test("页面 normalizer 保留 ID-only 自由卡写入且不伪造 number", () => {
  const start = COMPUTER.indexOf(
    '} else if (actionFn === "_nativeReaderPageCardMutate")',
  );
  const end = COMPUTER.indexOf(
    '} else if (actionFn === "__upStartTask")',
    start,
  );
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const pageCard = COMPUTER.slice(start, end);
  assert.match(pageCard, /hasMutationNumber/);
  assert.match(pageCard, /\["number"\]/);
  assert.match(
    pageCard,
    /if \(hasMutationNumber\) normalizedMutation\.number = mutation\.number/,
  );
  assert.doesNotMatch(
    pageCard,
    /number:\s*mutation\.number/,
    "omitted free-card number must stay omitted",
  );
});

test("工具描述说明自动重排、placement-only 删除与 Anki 边界", () => {
  const edit = toolSpec("PageCardEditToolName").replace(/"\s*\+\s*"/g, "");
  const remove = toolSpec("PageCardDeleteToolName").replace(/"\s*\+\s*"/g, "");
  assert.match(edit, /any card placement/);
  assert.match(edit, /unbound manually dragged card/);
  assert.match(edit, /stable id and revision already present in a currentPage CARD marker/);
  assert.match(edit, /id and expectedRevision/);
  assert.match(edit, /call this tool directly without a preliminary card query/);
  assert.match(edit, /optional shortcut/);
  assert.match(edit, /Omit number for an unbound card/);
  assert.match(edit, /does not silently rewrite an already exported Anki note/);
  assert.match(remove, /Delete only one bound or unbound card placement/);
  assert.match(remove, /number is an optional visible shortcut/);
  assert.match(remove, /deletion by number alone is always refused/);
  assert.match(remove, /remaining visible numbers and AI context renumber automatically/);
  assert.match(remove, /underlying learning-card entity/);
  assert.match(remove, /already exported Anki note are not deleted/);
  assert.match(remove, /\["destructiveHint"\] = true/);
});
