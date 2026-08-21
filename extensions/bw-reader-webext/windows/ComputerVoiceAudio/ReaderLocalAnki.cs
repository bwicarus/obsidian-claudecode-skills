using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderLocalAnkiException : Exception
{
    internal ReaderLocalAnkiException(
        string code,
        string message,
        bool retryable = false,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }

    internal string Code { get; }
    internal bool Retryable { get; }
}

internal sealed record ReaderLocalAnkiRegisteredCard(
    string SourceInstanceId,
    string DraftId,
    int CardIndex,
    string File,
    JsonObject Target,
    string SourceText,
    JsonObject Card);

internal sealed record ReaderLocalAnkiAddResult(
    long[] NoteIds,
    long[] CardIds,
    Dictionary<string, long[]> CardIdsByNote)
{
    internal JsonObject ToPayload(bool dedup) => new()
    {
        ["ok"] = true,
        ["added"] = NoteIds.Length,
        ["note_ids"] = new JsonArray(
            NoteIds.Select(value => (JsonNode?)value).ToArray()),
        ["card_ids"] = new JsonArray(
            CardIds.Select(value => (JsonNode?)value).ToArray()),
        ["card_ids_by_note"] = new JsonObject(
            CardIdsByNote.Select(pair =>
                KeyValuePair.Create<string, JsonNode?>(
                    pair.Key,
                    new JsonArray(
                        pair.Value.Select(value => (JsonNode?)value)
                            .ToArray())))),
        ["dedup"] = dedup,
    };
}

internal sealed record ReaderLocalAnkiWriteOutcome(
    ReaderLocalAnkiAddResult Result,
    bool Dedup);

internal sealed record ReaderLocalAnkiPreparedNote(
    string ModelName,
    JsonObject Fields,
    string[] Tags);

internal sealed record ReaderLocalAnkiAnswer(long CardId, int Ease);

internal sealed record ReaderLocalAnkiOperationRequest(
    string Operation,
    string? MutationId,
    long[] NoteIds,
    long[] CardIds,
    JsonObject? Fields,
    ReaderLocalAnkiAnswer[] Answers,
    string? SyncMode);

internal sealed record ReaderLocalAnkiSyncOutcome(
    string Status,
    string? Error = null)
{
    internal JsonObject ToPayload()
    {
        JsonObject result = new() { ["status"] = Status };
        if (!string.IsNullOrWhiteSpace(Error))
        {
            result["error"] = Error;
        }
        return result;
    }
}

internal sealed record ReaderLocalAnkiReceipt(
    string State,
    string Fingerprint,
    ReaderLocalAnkiAddResult? Result);

internal sealed record ReaderLocalAnkiMutationReceipt(
    string State,
    string Fingerprint,
    JsonObject? Result);

internal enum ReaderLocalAnkiClaimOutcome
{
    Claimed,
    Pending,
    Done,
    Reused,
}

internal sealed record ReaderLocalAnkiClaim(
    ReaderLocalAnkiClaimOutcome Outcome,
    ReaderLocalAnkiAddResult? Result);

/// <summary>
/// Shared provenance and idempotency store used by the short-lived MCP
/// process and the long-running Direct service. Every mutation holds a named
/// Windows mutex and replaces one same-directory file atomically.
/// </summary>
internal sealed class ReaderLocalAnkiRegistry
{
    internal const string RegistryContract =
        "reader-local-anki-registry/2";
    private const string LegacyRegistryContract =
        "reader-local-anki-registry/1";
    internal const string RegistryFileName =
        "reader-local-anki-registry.json";

    private const string MutexName =
        "Local\\BWReaderLocalAnkiRegistryV1";
    private const int MaximumDrafts = 128;
    private const int MaximumReceipts = 2_000;
    private const int MaximumMutations = 2_000;
    private const long MaximumRegistryBytes = 64L * 1024 * 1024;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);

    private readonly string _path;
    private readonly Func<DateTimeOffset> _utcNow;

    internal ReaderLocalAnkiRegistry(
        string path,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!Path.IsPathFullyQualified(path))
        {
            throw new ArgumentException(
                "Reader local Anki registry path must be absolute",
                nameof(path));
        }
        _path = Path.GetFullPath(path);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal Task RegisterDraftAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken)
    {
        if (request.Kind != "anki-draft"
            || request.Payload is not JsonObject payload)
        {
            throw Invalid("Reader 本地 Anki 草稿无效");
        }
        JsonObject draft = NormalizeDraft(request, payload);
        string draftId = draft["draftId"]!.GetValue<string>();
        return WithLockAsync(root =>
        {
            JsonObject drafts = RequireObject(root, "drafts");
            if (drafts[draftId] is JsonNode previous)
            {
                JsonNode comparablePrevious = previous.DeepClone();
                JsonNode comparableDraft = draft.DeepClone();
                comparablePrevious.AsObject().Remove("registeredAtUtc");
                comparableDraft.AsObject().Remove("registeredAtUtc");
                if (!JsonNode.DeepEquals(
                    comparablePrevious,
                    comparableDraft))
                {
                    throw new ReaderLocalAnkiException(
                        "BW_READER_ANKI_DRAFT_REUSED",
                        "Reader Anki 草稿编号已对应另一份内容");
                }
                return false;
            }
            drafts[draftId] = draft;
            TrimOldest(drafts, MaximumDrafts, "registeredAtUtc");
            return true;
        }, cancellationToken);
    }

    internal Task<ReaderLocalAnkiRegisteredCard> ResolveCardAsync(
        string sourceInstanceId,
        string draftId,
        int cardIndex,
        JsonObject suppliedCard,
        CancellationToken cancellationToken)
    {
        RequireSafeSource(sourceInstanceId);
        RequireDraftId(draftId);
        JsonObject normalizedCard = NormalizeCard(suppliedCard);
        if (cardIndex is < 0 or >= 20)
        {
            throw Invalid("Reader 本地 Anki 卡片序号无效");
        }
        return WithLockAsync(root =>
        {
            JsonObject drafts = RequireObject(root, "drafts");
            if (drafts[draftId] is not JsonObject draft)
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_DRAFT_NOT_REGISTERED",
                    "Reader 本地 Anki 草稿尚未登记，请重新生成草稿");
            }
            ValidateStoredDraft(draftId, draft);
            if (!string.Equals(
                draft["sourceInstanceId"]!.GetValue<string>(),
                sourceInstanceId,
                StringComparison.Ordinal))
            {
                // 这里比对的是 sourceInstanceId，即产生该草稿的那个 Reader 输出实例
                // （页面模块的生命周期内有效，页面重载即换新），用来阻止串实例导出。
                // 它与页码、锚点、placement 都无关；也不等同于用户会话，
                // 所以文案不提"会话"，免得引出另一个同样错误的排查方向。
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_DRAFT_SOURCE_MISMATCH",
                    "该草稿来自另一个 Reader 输出实例（可能页面已重载），已阻止导出到电脑 Anki");
            }
            JsonArray cards = (JsonArray)draft["cards"]!;
            if (cardIndex >= cards.Count
                || cards[cardIndex] is not JsonObject)
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_DRAFT_CARD_INDEX_INVALID",
                    "Reader 本地 Anki 卡片序号不在已登记草稿中");
            }
            return new ReaderLocalAnkiRegisteredCard(
                sourceInstanceId,
                draftId,
                cardIndex,
                draft["file"]!.GetValue<string>(),
                ((JsonObject)draft["target"]!).DeepClone()
                    .AsObject(),
                draft["sourceText"]!.GetValue<string>(),
                normalizedCard);
        }, write: false, cancellationToken);
    }

    internal Task<ReaderLocalAnkiReceipt?> ReadReceiptAsync(
        string aid,
        CancellationToken cancellationToken)
    {
        RequireAid(aid);
        return WithLockAsync(root =>
        {
            JsonObject receipts = RequireObject(root, "receipts");
            return receipts[aid] is JsonObject receipt
                ? ParseReceipt(aid, receipt)
                : null;
        }, write: false, cancellationToken);
    }

    internal Task<ReaderLocalAnkiClaim> ClaimAsync(
        string aid,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        RequireAid(aid);
        RequireFingerprint(fingerprint);
        return WithLockAsync(root =>
        {
            JsonObject receipts = RequireObject(root, "receipts");
            if (receipts[aid] is JsonObject existing)
            {
                ReaderLocalAnkiReceipt receipt = ParseReceipt(
                    aid,
                    existing);
                if (!string.Equals(
                    receipt.Fingerprint,
                    fingerprint,
                    StringComparison.Ordinal))
                {
                    return new ReaderLocalAnkiClaim(
                        ReaderLocalAnkiClaimOutcome.Reused,
                        null);
                }
                return new ReaderLocalAnkiClaim(
                    receipt.State == "done"
                        ? ReaderLocalAnkiClaimOutcome.Done
                        : ReaderLocalAnkiClaimOutcome.Pending,
                    receipt.Result);
            }
            string now = _utcNow().ToString("O");
            receipts[aid] = new JsonObject
            {
                ["state"] = "pending",
                ["fingerprint"] = fingerprint,
                ["createdAtUtc"] = now,
                ["updatedAtUtc"] = now,
                ["result"] = null,
            };
            TrimOldest(receipts, MaximumReceipts, "updatedAtUtc",
                preservePending: true);
            return new ReaderLocalAnkiClaim(
                ReaderLocalAnkiClaimOutcome.Claimed,
                null);
        }, cancellationToken);
    }

    internal Task CommitAsync(
        string aid,
        string fingerprint,
        ReaderLocalAnkiAddResult result,
        CancellationToken cancellationToken)
    {
        RequireAid(aid);
        RequireFingerprint(fingerprint);
        return WithLockAsync(root =>
        {
            JsonObject receipts = RequireObject(root, "receipts");
            if (receipts[aid] is not JsonObject existing)
            {
                throw RegistryInvalid("Anki 本地写入 claim 已丢失");
            }
            ReaderLocalAnkiReceipt receipt = ParseReceipt(aid, existing);
            if (receipt.Fingerprint != fingerprint
                || receipt.State != "pending")
            {
                throw RegistryInvalid("Anki 本地写入 claim 已改变");
            }
            existing["state"] = "done";
            existing["updatedAtUtc"] = _utcNow().ToString("O");
            existing["result"] = ResultNode(result);
            return true;
        }, cancellationToken);
    }

    internal Task ReleaseClaimAsync(
        string aid,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        RequireAid(aid);
        RequireFingerprint(fingerprint);
        return WithLockAsync(root =>
        {
            JsonObject receipts = RequireObject(root, "receipts");
            if (receipts[aid] is not JsonObject existing)
            {
                return false;
            }
            ReaderLocalAnkiReceipt receipt = ParseReceipt(aid, existing);
            if (receipt.State != "pending"
                || receipt.Fingerprint != fingerprint)
            {
                throw RegistryInvalid("Anki 本地写入 claim 无法安全释放");
            }
            return receipts.Remove(aid);
        }, cancellationToken);
    }

    internal Task<ReaderLocalAnkiMutationReceipt?>
        ReadMutationReceiptAsync(
            string mutationId,
            CancellationToken cancellationToken)
    {
        RequireMutationId(mutationId);
        return WithLockAsync(root =>
        {
            JsonObject mutations = RequireObject(root, "mutations");
            return mutations[mutationId] is JsonObject receipt
                ? ParseMutationReceipt(mutationId, receipt)
                : null;
        }, write: false, cancellationToken);
    }

    internal Task<ReaderLocalAnkiClaimOutcome> ClaimMutationAsync(
        string mutationId,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        RequireMutationId(mutationId);
        RequireFingerprint(fingerprint);
        return WithLockAsync(root =>
        {
            JsonObject mutations = RequireObject(root, "mutations");
            if (mutations[mutationId] is JsonObject existing)
            {
                ReaderLocalAnkiMutationReceipt receipt =
                    ParseMutationReceipt(mutationId, existing);
                if (!string.Equals(
                    receipt.Fingerprint,
                    fingerprint,
                    StringComparison.Ordinal))
                {
                    return ReaderLocalAnkiClaimOutcome.Reused;
                }
                return receipt.State == "done"
                    ? ReaderLocalAnkiClaimOutcome.Done
                    : ReaderLocalAnkiClaimOutcome.Pending;
            }
            string now = _utcNow().ToString("O");
            mutations[mutationId] = new JsonObject
            {
                ["state"] = "pending",
                ["fingerprint"] = fingerprint,
                ["createdAtUtc"] = now,
                ["updatedAtUtc"] = now,
                ["result"] = null,
            };
            TrimOldest(
                mutations,
                MaximumMutations,
                "updatedAtUtc",
                preservePending: true);
            return ReaderLocalAnkiClaimOutcome.Claimed;
        }, cancellationToken);
    }

    internal Task CommitMutationAsync(
        string mutationId,
        string fingerprint,
        JsonObject result,
        CancellationToken cancellationToken)
    {
        RequireMutationId(mutationId);
        RequireFingerprint(fingerprint);
        ValidateMutationResult(result);
        return WithLockAsync(root =>
        {
            JsonObject mutations = RequireObject(root, "mutations");
            if (mutations[mutationId] is not JsonObject existing)
            {
                throw RegistryInvalid("Anki 本地操作 claim 已丢失");
            }
            ReaderLocalAnkiMutationReceipt receipt =
                ParseMutationReceipt(mutationId, existing);
            if (receipt.Fingerprint != fingerprint
                || receipt.State != "pending")
            {
                throw RegistryInvalid("Anki 本地操作 claim 已改变");
            }
            existing["state"] = "done";
            existing["updatedAtUtc"] = _utcNow().ToString("O");
            existing["result"] = result.DeepClone();
            return true;
        }, cancellationToken);
    }

    internal Task UpdateMutationResultAsync(
        string mutationId,
        string fingerprint,
        JsonObject result,
        CancellationToken cancellationToken)
    {
        RequireMutationId(mutationId);
        RequireFingerprint(fingerprint);
        ValidateMutationResult(result);
        return WithLockAsync(root =>
        {
            JsonObject mutations = RequireObject(root, "mutations");
            if (mutations[mutationId] is not JsonObject existing)
            {
                throw RegistryInvalid("Anki 本地操作回执已丢失");
            }
            ReaderLocalAnkiMutationReceipt receipt =
                ParseMutationReceipt(mutationId, existing);
            if (receipt.Fingerprint != fingerprint
                || receipt.State != "done")
            {
                throw RegistryInvalid("Anki 本地操作回执已改变");
            }
            existing["updatedAtUtc"] = _utcNow().ToString("O");
            existing["result"] = result.DeepClone();
            return true;
        }, cancellationToken);
    }

    internal Task ReleaseMutationClaimAsync(
        string mutationId,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        RequireMutationId(mutationId);
        RequireFingerprint(fingerprint);
        return WithLockAsync(root =>
        {
            JsonObject mutations = RequireObject(root, "mutations");
            if (mutations[mutationId] is not JsonObject existing)
            {
                return false;
            }
            ReaderLocalAnkiMutationReceipt receipt =
                ParseMutationReceipt(mutationId, existing);
            if (receipt.State != "pending"
                || receipt.Fingerprint != fingerprint)
            {
                throw RegistryInvalid(
                    "Anki 本地操作 claim 无法安全释放");
            }
            return mutations.Remove(mutationId);
        }, cancellationToken);
    }

    internal static JsonObject NormalizeCard(JsonObject card)
    {
        using JsonDocument document = JsonDocument.Parse(
            card.ToJsonString(DirectBridgeContract.JsonOptions));
        JsonElement value = document.RootElement;
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        if (!value.TryGetProperty("type", out JsonElement typeValue)
            || typeValue.ValueKind != JsonValueKind.String)
        {
            throw Invalid("Reader 本地 Anki 卡片类型无效");
        }
        string type = typeValue.GetString()!;
        if (type == "basic")
        {
            RequireExact(value, "type", "front", "back");
            return new JsonObject
            {
                ["type"] = "basic",
                ["front"] = RequireCardText(value, "front", false),
                ["back"] = RequireCardText(value, "back", true),
            };
        }
        if (type == "cloze")
        {
            RequireExact(value, "type", "cloze");
            return new JsonObject
            {
                ["type"] = "cloze",
                ["cloze"] = RequireCardText(value, "cloze", false),
            };
        }
        throw Invalid("Reader 本地 Anki 卡片类型无效");
    }

    internal static string Fingerprint(
        ReaderLocalAnkiRegisteredCard registered)
    {
        JsonObject canonical = new()
        {
            ["sourceInstanceId"] = registered.SourceInstanceId,
            ["draftId"] = registered.DraftId,
            ["cardIndex"] = registered.CardIndex,
            ["card"] = registered.Card.DeepClone(),
        };
        return Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(canonical.ToJsonString(
                DirectBridgeContract.JsonOptions))))
            .ToLowerInvariant();
    }

    internal static void RequireAid(string aid)
    {
        if (aid.Length != 35
            || !aid.StartsWith("fc_", StringComparison.Ordinal)
            || aid[3..].Any(character =>
                character is not (>= '0' and <= '9'
                    or >= 'a' and <= 'f')))
        {
            throw Invalid("Reader 本地 Anki aid 无效");
        }
    }

    private JsonObject NormalizeDraft(
        ReaderRealtimeOutputRequest request,
        JsonObject payload)
    {
        string draftId = payload["draftId"]?.GetValue<string>()
            ?? throw Invalid("Reader 本地 Anki draftId 无效");
        RequireDraftId(draftId);
        RequireSafeSource(request.SourceInstanceId);
        bool hasFile = payload.ContainsKey("file");
        bool hasTarget = payload.ContainsKey("target");
        bool hasSourceText = payload.ContainsKey("sourceText");
        bool exactSource = hasFile && hasTarget && hasSourceText;
        if ((hasFile || hasTarget || hasSourceText) && !exactSource)
        {
            throw Invalid(
                "Reader 本地 Anki 引用来源必须同时提供 file/target/sourceText");
        }
        string file = exactSource
            ? payload["file"]?.GetValue<string>() ?? ""
            : "";
        string sourceText = exactSource
            ? payload["sourceText"]?.GetValue<string>() ?? ""
            : "";
        JsonObject target = exactSource
            ? payload["target"] as JsonObject
                ?? throw Invalid("Reader 本地 Anki 来源无效")
            : new JsonObject();
        if (exactSource
            && (file.Length is < 1 or > 4096
                || file.Any(char.IsControl)
                || sourceText.Length is < 1 or > 8000
                || sourceText.Contains('\0')))
        {
            throw Invalid("Reader 本地 Anki 来源无效");
        }
        if (payload["cards"] is not JsonArray cards
            || cards.Count is < 1 or > 20)
        {
            throw Invalid("Reader 本地 Anki 草稿内容无效");
        }
        JsonArray normalizedCards = [];
        foreach (JsonNode? node in cards)
        {
            if (node is not JsonObject card)
            {
                throw Invalid("Reader 本地 Anki 卡片无效");
            }
            normalizedCards.Add(NormalizeCard(card));
        }
        return new JsonObject
        {
            ["draftId"] = draftId,
            ["sourceInstanceId"] = request.SourceInstanceId,
            ["file"] = file,
            ["target"] = target.DeepClone(),
            ["sourceText"] = sourceText,
            ["cards"] = normalizedCards,
            ["registeredAtUtc"] = _utcNow().ToString("O"),
        };
    }

    private static void ValidateStoredDraft(
        string expectedDraftId,
        JsonObject draft)
    {
        RequireExact(
            JsonDocument.Parse(draft.ToJsonString(
                DirectBridgeContract.JsonOptions)).RootElement,
            "draftId",
            "sourceInstanceId",
            "file",
            "target",
            "sourceText",
            "cards",
            "registeredAtUtc");
        string draftId = draft["draftId"]?.GetValue<string>() ?? "";
        string source = draft["sourceInstanceId"]?.GetValue<string>() ?? "";
        string file = draft["file"]?.GetValue<string>() ?? "";
        string sourceText = draft["sourceText"]?.GetValue<string>() ?? "";
        JsonObject? target = draft["target"] as JsonObject;
        bool hasFile = file.Length > 0;
        bool hasSourceText = sourceText.Length > 0;
        bool hasTarget = target is { Count: > 0 };
        bool exactSource = hasFile && hasSourceText && hasTarget;
        bool genericSource = !hasFile && !hasSourceText && !hasTarget;
        if (draftId != expectedDraftId
            || !DateTimeOffset.TryParse(
                draft["registeredAtUtc"]?.GetValue<string>(),
                out _)
            || (!exactSource && !genericSource)
            || (exactSource
                && (file.Length > 4096
                    || file.Any(char.IsControl)
                    || sourceText.Length > 8000
                    || sourceText.Contains('\0')))
            || draft["cards"] is not JsonArray cards
            || cards.Count is < 1 or > 20)
        {
            throw RegistryInvalid("Reader 本地 Anki 草稿登记无效");
        }
        RequireDraftId(draftId);
        RequireSafeSource(source);
        foreach (JsonNode? card in cards)
        {
            if (card is not JsonObject value
                || !JsonNode.DeepEquals(value, NormalizeCard(value)))
            {
                throw RegistryInvalid("Reader 本地 Anki 草稿卡片无效");
            }
        }
    }

    private Task WithLockAsync(
        Func<JsonObject, bool> action,
        CancellationToken cancellationToken) =>
        WithLockAsync(root =>
        {
            _ = action(root);
            return true;
        }, write: true, cancellationToken);

    private Task<T> WithLockAsync<T>(
        Func<JsonObject, T> action,
        CancellationToken cancellationToken) =>
        WithLockAsync(action, write: true, cancellationToken);

    private Task<T> WithLockAsync<T>(
        Func<JsonObject, T> action,
        bool write,
        CancellationToken cancellationToken)
    {
        return Task.Run(() =>
        {
            using Mutex mutex = new(initiallyOwned: false, MutexName);
            bool acquired = false;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    acquired = mutex.WaitOne(TimeSpan.FromSeconds(5));
                }
                catch (AbandonedMutexException)
                {
                    acquired = true;
                }
                if (!acquired)
                {
                    throw new ReaderLocalAnkiException(
                        "BW_READER_ANKI_REGISTRY_BUSY",
                        "Reader 本地 Anki 登记表正忙，请稍后重试",
                        retryable: true);
                }
                JsonObject root = Load();
                T result = action(root);
                if (write)
                {
                    Persist(root);
                }
                return result;
            }
            finally
            {
                if (acquired)
                {
                    mutex.ReleaseMutex();
                }
            }
        }, cancellationToken);
    }

    private JsonObject Load()
    {
        if (!File.Exists(_path))
        {
            return EmptyRoot();
        }
        FileInfo info = new(_path);
        if (info.Length is <= 0 or > MaximumRegistryBytes)
        {
            throw RegistryInvalid("Reader 本地 Anki 登记表大小无效");
        }
        try
        {
            using FileStream stream = new(
                _path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                32 * 1024,
                FileOptions.SequentialScan);
            JsonObject root = JsonNode.Parse(
                stream,
                nodeOptions: null,
                documentOptions: new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                }) as JsonObject
                ?? throw RegistryInvalid("Reader 本地 Anki 登记表无效");
            string? contract = root["contract"]?.GetValue<string>();
            if (contract == LegacyRegistryContract
                && root.Count == 3
                && root["drafts"] is JsonObject
                && root["receipts"] is JsonObject)
            {
                root["contract"] = RegistryContract;
                root["mutations"] = new JsonObject();
                contract = RegistryContract;
            }
            if (root.Count != 4
                || contract != RegistryContract
                || root["drafts"] is not JsonObject drafts
                || root["receipts"] is not JsonObject receipts
                || root["mutations"] is not JsonObject mutations
                || drafts.Count > MaximumDrafts
                || receipts.Count > MaximumReceipts
                || mutations.Count > MaximumMutations)
            {
                throw RegistryInvalid("Reader 本地 Anki 登记表合同无效");
            }
            foreach ((string draftId, JsonNode? node) in drafts)
            {
                if (node is not JsonObject draft)
                {
                    throw RegistryInvalid("Reader 本地 Anki 草稿登记无效");
                }
                ValidateStoredDraft(draftId, draft);
            }
            foreach ((string aid, JsonNode? node) in receipts)
            {
                if (node is not JsonObject receipt)
                {
                    throw RegistryInvalid("Reader 本地 Anki 回执无效");
                }
                _ = ParseReceipt(aid, receipt);
            }
            foreach ((string mutationId, JsonNode? node) in mutations)
            {
                if (node is not JsonObject receipt)
                {
                    throw RegistryInvalid(
                        "Reader 本地 Anki 操作回执无效");
                }
                _ = ParseMutationReceipt(mutationId, receipt);
            }
            return root;
        }
        catch (ReaderLocalAnkiException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or InvalidOperationException
            or FormatException)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_REGISTRY_INVALID",
                "Reader 本地 Anki 登记表无法读取",
                innerException: exception);
        }
    }

    private void Persist(JsonObject root)
    {
        string? directory = Path.GetDirectoryName(_path);
        if (directory is null)
        {
            throw RegistryInvalid("Reader 本地 Anki 登记目录无效");
        }
        Directory.CreateDirectory(directory);
        byte[] bytes = Utf8WithoutBom.GetBytes(root.ToJsonString(
            DirectBridgeContract.JsonOptions));
        if (bytes.LongLength > MaximumRegistryBytes)
        {
            throw RegistryInvalid("Reader 本地 Anki 登记表超过大小上限");
        }
        string temporary = Path.Combine(
            directory,
            "." + Path.GetFileName(_path) + "."
                + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            using (FileStream stream = new(
                temporary,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                32 * 1024,
                FileOptions.WriteThrough))
            {
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }
            if (File.Exists(_path))
            {
                File.Replace(
                    temporary,
                    _path,
                    destinationBackupFileName: null,
                    ignoreMetadataErrors: true);
            }
            else
            {
                File.Move(temporary, _path);
            }
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or NotSupportedException)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_REGISTRY_WRITE_FAILED",
                "Reader 本地 Anki 登记表无法保存",
                retryable: true,
                innerException: exception);
        }
        finally
        {
            try
            {
                File.Delete(temporary);
            }
            catch
            {
            }
        }
    }

    private static JsonObject EmptyRoot() => new()
    {
        ["contract"] = RegistryContract,
        ["drafts"] = new JsonObject(),
        ["receipts"] = new JsonObject(),
        ["mutations"] = new JsonObject(),
    };

    private static JsonObject RequireObject(JsonObject root, string name) =>
        root[name] as JsonObject
        ?? throw RegistryInvalid("Reader 本地 Anki 登记表字段无效");

    private static ReaderLocalAnkiReceipt ParseReceipt(
        string aid,
        JsonObject value)
    {
        RequireAid(aid);
        if (value.Count != 5
            || value["state"]?.GetValue<string>() is not string state
            || state is not ("pending" or "done")
            || value["fingerprint"]?.GetValue<string>()
                is not string fingerprint
            || !DateTimeOffset.TryParse(
                value["createdAtUtc"]?.GetValue<string>(), out _)
            || !DateTimeOffset.TryParse(
                value["updatedAtUtc"]?.GetValue<string>(), out _))
        {
            throw RegistryInvalid("Reader 本地 Anki 回执无效");
        }
        RequireFingerprint(fingerprint);
        ReaderLocalAnkiAddResult? result = value["result"] switch
        {
            null when state == "pending" => null,
            JsonObject resultObject when state == "done" =>
                ParseResult(resultObject),
            _ => throw RegistryInvalid("Reader 本地 Anki 回执结果无效"),
        };
        return new ReaderLocalAnkiReceipt(state, fingerprint, result);
    }

    private static ReaderLocalAnkiMutationReceipt ParseMutationReceipt(
        string mutationId,
        JsonObject value)
    {
        RequireMutationId(mutationId);
        if (value.Count != 5
            || value["state"]?.GetValue<string>() is not string state
            || state is not ("pending" or "done")
            || value["fingerprint"]?.GetValue<string>()
                is not string fingerprint
            || !DateTimeOffset.TryParse(
                value["createdAtUtc"]?.GetValue<string>(), out _)
            || !DateTimeOffset.TryParse(
                value["updatedAtUtc"]?.GetValue<string>(), out _))
        {
            throw RegistryInvalid("Reader 本地 Anki 操作回执无效");
        }
        RequireFingerprint(fingerprint);
        JsonObject? result = value["result"] switch
        {
            null when state == "pending" => null,
            JsonObject resultObject when state == "done" =>
                resultObject.DeepClone().AsObject(),
            _ => throw RegistryInvalid(
                "Reader 本地 Anki 操作回执结果无效"),
        };
        if (result is not null)
        {
            ValidateMutationResult(result);
        }
        return new ReaderLocalAnkiMutationReceipt(
            state,
            fingerprint,
            result);
    }

    private static void ValidateMutationResult(JsonObject value)
    {
        if (value.Count is < 7 or > 12
            || value["ok"]?.GetValue<bool>() != true
            || value["operation"]?.GetValue<string>() is not string operation
            || operation is not (
                "update-note-fields"
                or "delete-notes"
                or "answer-cards"
                or "sync")
            || value["dedup"]?.GetValue<bool>() is not bool
            || value["anki_local_applied"]?.GetValue<bool>() is not bool
            || value["anki_local_status"]?.GetValue<string>()
                is not string localStatus
            || localStatus is not (
                "succeeded" or "deduplicated" or "not-applicable")
            || value["anki_web_sync"] is not JsonObject sync
            || sync["status"]?.GetValue<string>() is not string syncStatus
            || syncStatus is not (
                "not-requested"
                or "requested"
                or "succeeded"
                or "failed"
                or "unknown"))
        {
            throw RegistryInvalid(
                "Reader 本地 Anki 操作回执结果无效");
        }
        if (sync.Count is < 1 or > 2
            || (sync.Count == 2
                && (sync["error"]?.GetValue<string>() is not string error
                    || error.Length is < 1 or > 300)))
        {
            throw RegistryInvalid(
                "Reader 本地 Anki 同步回执无效");
        }
    }

    private static ReaderLocalAnkiAddResult ParseResult(JsonObject value)
    {
        if (value.Count != 3
            || value["note_ids"] is not JsonArray notes
            || value["card_ids"] is not JsonArray cards
            || value["card_ids_by_note"] is not JsonObject byNote)
        {
            throw RegistryInvalid("Reader 本地 Anki 回执结果无效");
        }
        long[] noteIds = PositiveIds(notes);
        long[] cardIds = PositiveIds(cards);
        Dictionary<string, long[]> mapped = new(StringComparer.Ordinal);
        foreach ((string noteId, JsonNode? ids) in byNote)
        {
            if (!long.TryParse(noteId, out long parsed)
                || parsed <= 0
                || ids is not JsonArray array)
            {
                throw RegistryInvalid("Reader 本地 Anki 卡片回执无效");
            }
            mapped[noteId] = PositiveIds(array);
        }
        return new ReaderLocalAnkiAddResult(noteIds, cardIds, mapped);
    }

    private static JsonObject ResultNode(ReaderLocalAnkiAddResult result) =>
        new()
        {
            ["note_ids"] = new JsonArray(
                result.NoteIds.Select(value => (JsonNode?)value).ToArray()),
            ["card_ids"] = new JsonArray(
                result.CardIds.Select(value => (JsonNode?)value).ToArray()),
            ["card_ids_by_note"] = new JsonObject(
                result.CardIdsByNote.Select(pair =>
                    KeyValuePair.Create<string, JsonNode?>(
                        pair.Key,
                        new JsonArray(pair.Value
                            .Select(value => (JsonNode?)value).ToArray())))),
        };

    private static long[] PositiveIds(JsonArray values)
    {
        List<long> result = [];
        foreach (JsonNode? value in values)
        {
            if (value is not JsonValue jsonValue
                || !jsonValue.TryGetValue(out long parsed)
                || parsed <= 0)
            {
                throw RegistryInvalid("Reader 本地 Anki ID 无效");
            }
            result.Add(parsed);
        }
        return result.ToArray();
    }

    private static void TrimOldest(
        JsonObject values,
        int maximum,
        string timestampField,
        bool preservePending = false)
    {
        while (values.Count > maximum)
        {
            string? oldest = values
                .Where(pair => !preservePending
                    || pair.Value?["state"]?.GetValue<string>() != "pending")
                .OrderBy(pair =>
                    pair.Value?[timestampField]?.GetValue<string>() ?? "",
                    StringComparer.Ordinal)
                .Select(pair => pair.Key)
                .FirstOrDefault();
            if (oldest is null)
            {
                throw RegistryInvalid("Reader 本地 Anki 未决回执过多");
            }
            values.Remove(oldest);
        }
    }

    private static void RequireDraftId(string value)
    {
        if (value.Length != 38
            || !value.StartsWith("draft-", StringComparison.Ordinal)
            || value[6..].Any(character =>
                character is not (>= '0' and <= '9'
                    or >= 'a' and <= 'f')))
        {
            throw Invalid("Reader 本地 Anki draftId 无效");
        }
    }

    private static void RequireSafeSource(string value)
    {
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid("Reader 本地 Anki 来源 ID 无效");
        }
    }

    private static void RequireFingerprint(string value)
    {
        if (value.Length != 64
            || value.Any(character =>
                character is not (>= '0' and <= '9'
                    or >= 'a' and <= 'f')))
        {
            throw RegistryInvalid("Reader 本地 Anki payload 指纹无效");
        }
    }

    private static void RequireMutationId(string value)
    {
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid("Reader 本地 Anki mutationId 无效");
        }
    }

    private static void RequireExact(
        JsonElement value,
        params string[] names)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 本地 Anki 对象无效");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(names))
        {
            throw Invalid("Reader 本地 Anki 字段不匹配");
        }
    }

    private static string RequireCardText(
        JsonElement card,
        string name,
        bool allowEmpty)
    {
        if (!card.TryGetProperty(name, out JsonElement field)
            || field.ValueKind != JsonValueKind.String
            || field.GetString() is not string value
            || value.Length > 8000
            || (!allowEmpty && value.Length == 0)
            || value.Contains('\0'))
        {
            throw Invalid($"Reader 本地 Anki {name} 字段无效");
        }
        return value;
    }

    private static ReaderLocalAnkiException Invalid(string message) =>
        new("BW_READER_ANKI_REQUEST_INVALID", message);

    private static ReaderLocalAnkiException RegistryInvalid(
        string message) =>
        new("BW_READER_ANKI_REGISTRY_INVALID", message);
}

internal interface IReaderAnkiConnectClient
{
    Task<JsonNode?> CallAsync(
        string action,
        JsonObject parameters,
        CancellationToken cancellationToken);
}

internal enum ReaderAnkiConnectFailure
{
    Unreachable,
    InvalidResponse,
    RemoteError,
}

internal sealed class ReaderAnkiConnectException : Exception
{
    internal ReaderAnkiConnectException(
        ReaderAnkiConnectFailure failure,
        string message,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Failure = failure;
    }

    internal ReaderAnkiConnectFailure Failure { get; }
}

internal sealed class FixedLoopbackAnkiConnectClient :
    IReaderAnkiConnectClient
{
    private static readonly Uri Endpoint = new("http://127.0.0.1:8765/");
    private static readonly HttpClient Client = new()
    {
        Timeout = TimeSpan.FromSeconds(10),
    };

    public async Task<JsonNode?> CallAsync(
        string action,
        JsonObject parameters,
        CancellationToken cancellationToken)
    {
        byte[] body = Encoding.UTF8.GetBytes(new JsonObject
        {
            ["action"] = action,
            ["version"] = 6,
            ["params"] = parameters.DeepClone(),
        }.ToJsonString(DirectBridgeContract.JsonOptions));
        try
        {
            using HttpRequestMessage request = new(HttpMethod.Post, Endpoint)
            {
                Content = new ByteArrayContent(body),
            };
            request.Content.Headers.ContentType =
                new System.Net.Http.Headers.MediaTypeHeaderValue(
                    "application/json");
            using HttpResponseMessage response = await Client.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                throw new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.InvalidResponse,
                    $"AnkiConnect HTTP {(int)response.StatusCode}");
            }
            byte[] bytes = await response.Content.ReadAsByteArrayAsync(
                cancellationToken).ConfigureAwait(false);
            if (bytes.Length is <= 0 or > 2 * 1024 * 1024)
            {
                throw InvalidResponse();
            }
            JsonObject root = JsonNode.Parse(
                bytes,
                nodeOptions: null,
                documentOptions: new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                }) as JsonObject
                ?? throw InvalidResponse();
            if (root.Count != 2
                || !root.ContainsKey("result")
                || !root.ContainsKey("error"))
            {
                throw InvalidResponse();
            }
            if (root["error"] is JsonValue errorValue
                && errorValue.TryGetValue(out string? error)
                && !string.IsNullOrWhiteSpace(error))
            {
                throw new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.RemoteError,
                    error.Length <= 300 ? error : error[..300]);
            }
            if (root["error"] is not null)
            {
                throw InvalidResponse();
            }
            return root["result"]?.DeepClone();
        }
        catch (ReaderAnkiConnectException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is HttpRequestException
            or TaskCanceledException
            or IOException)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.Unreachable,
                "AnkiConnect 不可达",
                exception);
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect 响应无效",
                exception);
        }
    }

    private static ReaderAnkiConnectException InvalidResponse() =>
        new(
            ReaderAnkiConnectFailure.InvalidResponse,
            "AnkiConnect 响应无效");
}

internal interface IReaderLocalAnkiWriter
{
    Task<ReaderLocalAnkiWriteOutcome> AddAsync(
        string sourceInstanceId,
        string draftId,
        int cardIndex,
        string aid,
        JsonObject card,
        CancellationToken cancellationToken);

    Task<JsonObject> OperateAsync(
        ReaderLocalAnkiOperationRequest request,
        CancellationToken cancellationToken);
}

internal sealed class ReaderLocalAnkiWriter : IReaderLocalAnkiWriter
{
    private const string DeckName = "QA";
    private static readonly SemaphoreSlim AddGate = new(1, 1);
    private static readonly SemaphoreSlim OperationGate = AddGate;

    private readonly ReaderLocalAnkiRegistry _registry;
    private readonly IReaderAnkiConnectClient _client;
    private readonly object _backgroundSyncLock = new();
    private Task<ReaderLocalAnkiSyncOutcome>? _backgroundSync;

    internal ReaderLocalAnkiWriter(
        ReaderLocalAnkiRegistry registry,
        IReaderAnkiConnectClient? client = null)
    {
        _registry = registry;
        _client = client ?? new FixedLoopbackAnkiConnectClient();
    }

    public async Task<ReaderLocalAnkiWriteOutcome> AddAsync(
        string sourceInstanceId,
        string draftId,
        int cardIndex,
        string aid,
        JsonObject card,
        CancellationToken cancellationToken)
    {
        ReaderLocalAnkiRegistry.RequireAid(aid);
        ReaderLocalAnkiRegisteredCard registered =
            await _registry.ResolveCardAsync(
                sourceInstanceId,
                draftId,
                cardIndex,
                card,
                cancellationToken).ConfigureAwait(false);
        string fingerprint = ReaderLocalAnkiRegistry.Fingerprint(registered);
        await AddGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ReaderLocalAnkiReceipt? receipt =
                await _registry.ReadReceiptAsync(aid, cancellationToken)
                    .ConfigureAwait(false);
            if (receipt is not null
                && receipt.Fingerprint != fingerprint)
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_AID_REUSED",
                    "同一 aid 不能写入不同的 Anki 内容");
            }
            if (receipt?.State == "done" && receipt.Result is not null)
            {
                return AddOutcomeWithBackgroundSync(
                    receipt.Result,
                    Dedup: true);
            }

            string aidTag = AidTag(aid);
            string fingerprintTag = FingerprintTag(fingerprint);
            long[] existing;
            try
            {
                existing = RequireIds(await _client.CallAsync(
                    "findNotes",
                    new JsonObject { ["query"] = "tag:" + aidTag },
                    cancellationToken).ConfigureAwait(false));
            }
            catch (ReaderAnkiConnectException exception)
            {
                throw MapConnectFailure(exception, receipt is not null);
            }
            catch (Exception exception) when (
                exception is JsonException
                or InvalidOperationException
                or FormatException)
            {
                throw MapConnectFailure(
                    new ReaderAnkiConnectException(
                        ReaderAnkiConnectFailure.InvalidResponse,
                        "AnkiConnect 响应无效",
                        exception),
                    receipt is not null);
            }
            if (existing.Length > 0)
            {
                ReaderLocalAnkiAddResult recovered =
                    await RecoverExistingAsync(
                        existing,
                        aidTag,
                        fingerprintTag,
                        cancellationToken).ConfigureAwait(false);
                if (receipt is null)
                {
                    ReaderLocalAnkiClaim recoveredClaim =
                        await _registry.ClaimAsync(
                            aid,
                            fingerprint,
                            cancellationToken).ConfigureAwait(false);
                    if (recoveredClaim.Outcome
                        == ReaderLocalAnkiClaimOutcome.Reused)
                    {
                        throw new ReaderLocalAnkiException(
                            "BW_READER_ANKI_AID_REUSED",
                            "同一 aid 不能写入不同的 Anki 内容");
                    }
                }
                await CommitIfPendingAsync(
                    aid,
                    fingerprint,
                    recovered,
                    cancellationToken).ConfigureAwait(false);
                return AddOutcomeWithBackgroundSync(
                    recovered,
                    Dedup: true);
            }
            if (receipt?.State == "pending")
            {
                throw UnknownOutcome();
            }

            ReaderLocalAnkiPreparedNote prepared;
            try
            {
                prepared = await PrepareNoteAsync(
                    registered,
                    aidTag,
                    fingerprintTag,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderAnkiConnectException exception)
            {
                throw MapConnectFailure(exception, pending: false);
            }
            catch (Exception exception) when (
                exception is JsonException
                or InvalidOperationException
                or FormatException)
            {
                throw MapConnectFailure(
                    new ReaderAnkiConnectException(
                        ReaderAnkiConnectFailure.InvalidResponse,
                        "AnkiConnect 响应无效",
                        exception),
                    pending: false);
            }

            ReaderLocalAnkiClaim claim = await _registry.ClaimAsync(
                aid,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
            if (claim.Outcome == ReaderLocalAnkiClaimOutcome.Reused)
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_AID_REUSED",
                    "同一 aid 不能写入不同的 Anki 内容");
            }
            if (claim.Outcome == ReaderLocalAnkiClaimOutcome.Done
                && claim.Result is not null)
            {
                return AddOutcomeWithBackgroundSync(
                    claim.Result,
                    Dedup: true);
            }
            if (claim.Outcome != ReaderLocalAnkiClaimOutcome.Claimed)
            {
                throw UnknownOutcome();
            }

            try
            {
                long noteId;
                try
                {
                    noteId = await AddClaimedAsync(
                        prepared,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (ReaderAnkiConnectException exception)
                {
                    // addNote was submitted but did not produce an explicit
                    // positive id. Its outcome is unknown regardless of the
                    // transport/error shape, so the pending claim must stay.
                    throw UnknownOutcome(exception);
                }
                catch (Exception exception) when (
                    exception is JsonException
                    or InvalidOperationException
                    or FormatException)
                {
                    throw UnknownOutcome(exception);
                }
                // addNote has returned a positive note id. From this point the
                // irreversible mutation is proven, so optional enrichment is
                // never allowed to turn the claim into unknown.
                ReaderLocalAnkiAddResult result =
                    await CompleteResultAsync(
                        [noteId], cancellationToken).ConfigureAwait(false);
                await _registry.CommitAsync(
                    aid,
                    fingerprint,
                    result,
                    cancellationToken).ConfigureAwait(false);
                return AddOutcomeWithBackgroundSync(
                    result,
                    Dedup: false);
            }
            catch (ReaderLocalAnkiException exception) when (
                exception.Code == "BW_READER_ANKI_ADD_OUTCOME_UNKNOWN")
            {
                throw;
            }
        }
        finally
        {
            AddGate.Release();
        }
    }

    public async Task<JsonObject> OperateAsync(
        ReaderLocalAnkiOperationRequest request,
        CancellationToken cancellationToken)
    {
        ValidateOperationRequest(request);
        if (request.Operation == "read-notes")
        {
            JsonArray notes = await ReadInfoAsync(
                "notesInfo",
                "notes",
                request.NoteIds,
                "noteId",
                cancellationToken).ConfigureAwait(false);
            JsonObject result = OperationPayload(
                request.Operation,
                mutationId: null,
                dedup: false,
                localApplied: false,
                localStatus: "read",
                new ReaderLocalAnkiSyncOutcome("not-requested"));
            result["notes"] = notes;
            return result;
        }
        if (request.Operation == "read-cards")
        {
            JsonArray cards = await ReadInfoAsync(
                "cardsInfo",
                "cards",
                request.CardIds,
                "cardId",
                cancellationToken).ConfigureAwait(false);
            JsonObject result = OperationPayload(
                request.Operation,
                mutationId: null,
                dedup: false,
                localApplied: false,
                localStatus: "read",
                new ReaderLocalAnkiSyncOutcome("not-requested"));
            result["cards"] = cards;
            return result;
        }

        string mutationId = request.MutationId!;
        string fingerprint = OperationFingerprint(request);
        await OperationGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            ReaderLocalAnkiMutationReceipt? receipt =
                await _registry.ReadMutationReceiptAsync(
                    mutationId,
                    cancellationToken).ConfigureAwait(false);
            if (receipt is not null
                && receipt.Fingerprint != fingerprint)
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_MUTATION_REUSED",
                    "同一 mutationId 不能执行不同的 Anki 操作");
            }
            if (receipt?.State == "done" && receipt.Result is not null)
            {
                return Deduplicated(receipt.Result);
            }

            return request.Operation switch
            {
                "update-note-fields" =>
                    await UpdateNoteFieldsAsync(
                        request,
                        fingerprint,
                        receipt,
                        cancellationToken).ConfigureAwait(false),
                "delete-notes" =>
                    await DeleteNotesAsync(
                        request,
                        fingerprint,
                        receipt,
                        cancellationToken).ConfigureAwait(false),
                "answer-cards" =>
                    await AnswerCardsAsync(
                        request,
                        fingerprint,
                        receipt,
                        cancellationToken).ConfigureAwait(false),
                "sync" =>
                    await SyncExplicitAsync(
                        request,
                        fingerprint,
                        receipt,
                        cancellationToken).ConfigureAwait(false),
                _ => throw InvalidOperationRequest(
                    "Reader 本地 Anki 操作类型无效"),
            };
        }
        finally
        {
            OperationGate.Release();
        }
    }

    private async Task<JsonObject> UpdateNoteFieldsAsync(
        ReaderLocalAnkiOperationRequest request,
        string fingerprint,
        ReaderLocalAnkiMutationReceipt? receipt,
        CancellationToken cancellationToken)
    {
        long noteId = request.NoteIds[0];
        JsonObject desired = NormalizeFields(request.Fields!);
        JsonArray info = await ReadInfoAsync(
            "notesInfo",
            "notes",
            [noteId],
            "noteId",
            cancellationToken).ConfigureAwait(false);
        if (info.Count != 1 || info[0] is not JsonObject note)
        {
            if (receipt?.State == "pending")
            {
                throw UnknownOperationOutcome();
            }
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_NOTE_NOT_FOUND",
                "Anki 中找不到要修改的 note");
        }
        ValidateFieldsExist(note, desired);
        bool alreadyApplied = FieldsMatch(note, desired);
        if (receipt?.State == "pending")
        {
            if (!alreadyApplied)
            {
                throw UnknownOperationOutcome();
            }
            JsonObject recovered = WriteResult(
                request,
                dedup: true,
                [noteId],
                [],
                new ReaderLocalAnkiSyncOutcome("requested"));
            await _registry.CommitMutationAsync(
                request.MutationId!,
                fingerprint,
                recovered,
                cancellationToken).ConfigureAwait(false);
            return await FinishSyncAsync(
                request,
                fingerprint,
                recovered,
                cancellationToken).ConfigureAwait(false);
        }

        ReaderLocalAnkiClaimOutcome claim =
            await ClaimOperationAsync(
                request.MutationId!,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
        if (claim == ReaderLocalAnkiClaimOutcome.Done)
        {
            return await ReadCommittedMutationAsync(
                request.MutationId!,
                cancellationToken).ConfigureAwait(false);
        }
        if (claim != ReaderLocalAnkiClaimOutcome.Claimed)
        {
            throw UnknownOperationOutcome();
        }
        if (alreadyApplied)
        {
            JsonObject unchanged = WriteResult(
                request,
                dedup: true,
                [noteId],
                [],
                new ReaderLocalAnkiSyncOutcome("not-requested"));
            await _registry.CommitMutationAsync(
                request.MutationId!,
                fingerprint,
                unchanged,
                cancellationToken).ConfigureAwait(false);
            return unchanged;
        }
        try
        {
            JsonNode? response = await _client.CallAsync(
                "updateNoteFields",
                new JsonObject
                {
                    ["note"] = new JsonObject
                    {
                        ["id"] = noteId,
                        ["fields"] = desired.DeepClone(),
                    },
                },
                cancellationToken).ConfigureAwait(false);
            RequireNullMutationResult(response, "updateNoteFields");
        }
        catch (ReaderAnkiConnectException exception)
        {
            await HandleClaimedMutationFailureAsync(
                request.MutationId!,
                fingerprint,
                exception,
                cancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException)
        {
            throw UnknownOperationOutcome(exception);
        }
        JsonObject result = WriteResult(
            request,
            dedup: false,
            [noteId],
            [],
            new ReaderLocalAnkiSyncOutcome("requested"));
        await _registry.CommitMutationAsync(
            request.MutationId!,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
        return await FinishSyncAsync(
            request,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonObject> DeleteNotesAsync(
        ReaderLocalAnkiOperationRequest request,
        string fingerprint,
        ReaderLocalAnkiMutationReceipt? receipt,
        CancellationToken cancellationToken)
    {
        JsonArray info = await ReadInfoAsync(
            "notesInfo",
            "notes",
            request.NoteIds,
            "noteId",
            cancellationToken).ConfigureAwait(false);
        if (receipt?.State == "pending")
        {
            if (info.Count != 0)
            {
                throw UnknownOperationOutcome();
            }
            JsonObject recovered = WriteResult(
                request,
                dedup: true,
                request.NoteIds,
                [],
                new ReaderLocalAnkiSyncOutcome("requested"));
            await _registry.CommitMutationAsync(
                request.MutationId!,
                fingerprint,
                recovered,
                cancellationToken).ConfigureAwait(false);
            return await FinishSyncAsync(
                request,
                fingerprint,
                recovered,
                cancellationToken).ConfigureAwait(false);
        }
        if (info.Count != request.NoteIds.Length)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_NOTE_NOT_FOUND",
                "Anki 中找不到全部待删除 note");
        }
        ReaderLocalAnkiClaimOutcome claim =
            await ClaimOperationAsync(
                request.MutationId!,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
        if (claim == ReaderLocalAnkiClaimOutcome.Done)
        {
            return await ReadCommittedMutationAsync(
                request.MutationId!,
                cancellationToken).ConfigureAwait(false);
        }
        if (claim != ReaderLocalAnkiClaimOutcome.Claimed)
        {
            throw UnknownOperationOutcome();
        }
        try
        {
            JsonNode? response = await _client.CallAsync(
                "deleteNotes",
                new JsonObject
                {
                    ["notes"] = IdArray(request.NoteIds),
                },
                cancellationToken).ConfigureAwait(false);
            RequireNullMutationResult(response, "deleteNotes");
        }
        catch (ReaderAnkiConnectException exception)
        {
            await HandleClaimedMutationFailureAsync(
                request.MutationId!,
                fingerprint,
                exception,
                cancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException)
        {
            throw UnknownOperationOutcome(exception);
        }
        JsonObject result = WriteResult(
            request,
            dedup: false,
            request.NoteIds,
            [],
            new ReaderLocalAnkiSyncOutcome("requested"));
        await _registry.CommitMutationAsync(
            request.MutationId!,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
        return await FinishSyncAsync(
            request,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonObject> AnswerCardsAsync(
        ReaderLocalAnkiOperationRequest request,
        string fingerprint,
        ReaderLocalAnkiMutationReceipt? receipt,
        CancellationToken cancellationToken)
    {
        if (receipt?.State == "pending")
        {
            // cardsInfo cannot prove whether the scheduler already accepted
            // a previous answer. Never submit it a second time.
            throw UnknownOperationOutcome();
        }
        long[] cardIds = request.Answers
            .Select(answer => answer.CardId)
            .ToArray();
        JsonArray info = await ReadInfoAsync(
            "cardsInfo",
            "cards",
            cardIds,
            "cardId",
            cancellationToken).ConfigureAwait(false);
        if (info.Count != cardIds.Length)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_CARD_NOT_FOUND",
                "Anki 中找不到要评分的 card");
        }
        ReaderLocalAnkiClaimOutcome claim =
            await ClaimOperationAsync(
                request.MutationId!,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
        if (claim == ReaderLocalAnkiClaimOutcome.Done)
        {
            return await ReadCommittedMutationAsync(
                request.MutationId!,
                cancellationToken).ConfigureAwait(false);
        }
        if (claim != ReaderLocalAnkiClaimOutcome.Claimed)
        {
            throw UnknownOperationOutcome();
        }
        try
        {
            JsonNode? response = await _client.CallAsync(
                "answerCards",
                new JsonObject
                {
                    ["answers"] = new JsonArray(
                        request.Answers.Select(answer =>
                            (JsonNode?)new JsonObject
                            {
                                ["cardId"] = answer.CardId,
                                ["ease"] = answer.Ease,
                            }).ToArray()),
                },
                cancellationToken).ConfigureAwait(false);
            if (response is not JsonArray accepted
                || accepted.Count != request.Answers.Length
                || accepted.Any(value => value?.GetValue<bool>() != true))
            {
                throw UnknownOperationOutcome();
            }
        }
        catch (ReaderAnkiConnectException exception)
        {
            await HandleClaimedMutationFailureAsync(
                request.MutationId!,
                fingerprint,
                exception,
                cancellationToken).ConfigureAwait(false);
        }
        JsonObject result = WriteResult(
            request,
            dedup: false,
            [],
            cardIds,
            new ReaderLocalAnkiSyncOutcome("requested"));
        result["answers"] = new JsonArray(request.Answers.Select(answer =>
            (JsonNode?)new JsonObject
            {
                ["card_id"] = answer.CardId,
                ["ease"] = answer.Ease,
            }).ToArray());
        await _registry.CommitMutationAsync(
            request.MutationId!,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
        return await FinishSyncAsync(
            request,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonObject> SyncExplicitAsync(
        ReaderLocalAnkiOperationRequest request,
        string fingerprint,
        ReaderLocalAnkiMutationReceipt? receipt,
        CancellationToken cancellationToken)
    {
        if (receipt?.State == "pending")
        {
            throw UnknownOperationOutcome();
        }
        ReaderLocalAnkiClaimOutcome claim =
            await ClaimOperationAsync(
                request.MutationId!,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
        if (claim == ReaderLocalAnkiClaimOutcome.Done)
        {
            return await ReadCommittedMutationAsync(
                request.MutationId!,
                cancellationToken).ConfigureAwait(false);
        }
        if (claim != ReaderLocalAnkiClaimOutcome.Claimed)
        {
            throw UnknownOperationOutcome();
        }
        ReaderLocalAnkiSyncOutcome sync =
            await RunSyncAsync(cancellationToken).ConfigureAwait(false);
        JsonObject result = OperationPayload(
            request.Operation,
            request.MutationId,
            dedup: false,
            localApplied: false,
            localStatus: "not-applicable",
            sync);
        await _registry.CommitMutationAsync(
            request.MutationId!,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
        return result;
    }

    private async Task<JsonObject> FinishSyncAsync(
        ReaderLocalAnkiOperationRequest request,
        string fingerprint,
        JsonObject result,
        CancellationToken cancellationToken)
    {
        if (request.SyncMode == "background")
        {
            _ = TrackBackgroundSyncAsync(
                request.MutationId!,
                fingerprint,
                result.DeepClone().AsObject());
            return result;
        }
        ReaderLocalAnkiSyncOutcome sync =
            await RunSyncAsync(cancellationToken).ConfigureAwait(false);
        result["anki_web_sync"] = sync.ToPayload();
        await _registry.UpdateMutationResultAsync(
            request.MutationId!,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
        return result;
    }

    private async Task TrackBackgroundSyncAsync(
        string mutationId,
        string fingerprint,
        JsonObject result)
    {
        try
        {
            ReaderLocalAnkiSyncOutcome sync =
                await RequestBackgroundSyncAsync().ConfigureAwait(false);
            result["anki_web_sync"] = sync.ToPayload();
            await _registry.UpdateMutationResultAsync(
                mutationId,
                fingerprint,
                result,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            // The durable receipt remains "requested". A later explicit sync
            // can safely reconcile it without replaying the card mutation.
        }
    }

    private Task<ReaderLocalAnkiSyncOutcome> RequestBackgroundSyncAsync()
    {
        lock (_backgroundSyncLock)
        {
            if (_backgroundSync is null || _backgroundSync.IsCompleted)
            {
                _backgroundSync = RunBackgroundSyncAsync();
            }
            return _backgroundSync;
        }
    }

    private async Task<ReaderLocalAnkiSyncOutcome> RunBackgroundSyncAsync()
    {
        await Task.Delay(TimeSpan.FromMilliseconds(250))
            .ConfigureAwait(false);
        return await RunSyncAsync(CancellationToken.None)
            .ConfigureAwait(false);
    }

    private async Task<ReaderLocalAnkiSyncOutcome> RunSyncAsync(
        CancellationToken cancellationToken)
    {
        try
        {
            JsonNode? response = await _client.CallAsync(
                "sync",
                new JsonObject(),
                cancellationToken).ConfigureAwait(false);
            if (response is not null)
            {
                return new ReaderLocalAnkiSyncOutcome(
                    "unknown",
                    "AnkiConnect sync 响应无效");
            }
            return new ReaderLocalAnkiSyncOutcome("succeeded");
        }
        catch (ReaderAnkiConnectException exception)
        {
            return exception.Failure == ReaderAnkiConnectFailure.RemoteError
                ? new ReaderLocalAnkiSyncOutcome(
                    "failed",
                    SafeError(exception.Message))
                : new ReaderLocalAnkiSyncOutcome(
                    "unknown",
                    SafeError(exception.Message));
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException)
        {
            return new ReaderLocalAnkiSyncOutcome(
                "unknown",
                SafeError(exception.Message));
        }
    }

    private ReaderLocalAnkiWriteOutcome AddOutcomeWithBackgroundSync(
        ReaderLocalAnkiAddResult result,
        bool Dedup)
    {
        _ = RequestBackgroundSyncAsync();
        return new ReaderLocalAnkiWriteOutcome(result, Dedup);
    }

    private async Task<JsonArray> ReadInfoAsync(
        string action,
        string parameterName,
        long[] ids,
        string idProperty,
        CancellationToken cancellationToken)
    {
        try
        {
            JsonNode? response = await _client.CallAsync(
                action,
                new JsonObject { [parameterName] = IdArray(ids) },
                cancellationToken).ConfigureAwait(false);
            if (response is not JsonArray values || values.Count > ids.Length)
            {
                throw new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.InvalidResponse,
                    $"AnkiConnect {action} 响应无效");
            }
            HashSet<long> requested = ids.ToHashSet();
            HashSet<long> seen = [];
            JsonArray result = new();
            foreach (JsonNode? value in values)
            {
                if (value is not JsonObject item
                    || !TryPositiveId(item[idProperty], out long id)
                    || !requested.Contains(id)
                    || !seen.Add(id))
                {
                    throw new ReaderAnkiConnectException(
                        ReaderAnkiConnectFailure.InvalidResponse,
                        $"AnkiConnect {action} 响应无效");
                }
                if (action == "notesInfo"
                    && (item["modelName"] is not JsonValue modelValue
                        || !modelValue.TryGetValue(out string? modelName)
                        || string.IsNullOrWhiteSpace(modelName)
                        || modelName.Length > 256
                        || item["fields"] is not JsonObject))
                {
                    throw new ReaderAnkiConnectException(
                        ReaderAnkiConnectFailure.InvalidResponse,
                        "AnkiConnect notesInfo 缺少 modelName/fields");
                }
                result.Add(item.DeepClone());
            }
            return result;
        }
        catch (ReaderAnkiConnectException exception)
        {
            throw MapOperationConnectFailure(exception);
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException)
        {
            throw MapOperationConnectFailure(
                new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.InvalidResponse,
                    "AnkiConnect 响应无效",
                    exception));
        }
    }

    private async Task<ReaderLocalAnkiClaimOutcome> ClaimOperationAsync(
        string mutationId,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        ReaderLocalAnkiClaimOutcome claim =
            await _registry.ClaimMutationAsync(
                mutationId,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
        if (claim == ReaderLocalAnkiClaimOutcome.Reused)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_MUTATION_REUSED",
                "同一 mutationId 不能执行不同的 Anki 操作");
        }
        return claim;
    }

    private async Task<JsonObject> ReadCommittedMutationAsync(
        string mutationId,
        CancellationToken cancellationToken)
    {
        ReaderLocalAnkiMutationReceipt? receipt =
            await _registry.ReadMutationReceiptAsync(
                mutationId,
                cancellationToken).ConfigureAwait(false);
        if (receipt?.State != "done" || receipt.Result is null)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_REGISTRY_INVALID",
                "Anki 本地操作回执状态无效");
        }
        return Deduplicated(receipt.Result);
    }

    private async Task HandleClaimedMutationFailureAsync(
        string mutationId,
        string fingerprint,
        ReaderAnkiConnectException exception,
        CancellationToken cancellationToken)
    {
        if (exception.Failure == ReaderAnkiConnectFailure.RemoteError)
        {
            await _registry.ReleaseMutationClaimAsync(
                mutationId,
                fingerprint,
                cancellationToken).ConfigureAwait(false);
            throw MapOperationConnectFailure(exception);
        }
        throw UnknownOperationOutcome(exception);
    }

    private static JsonObject NormalizeFields(JsonObject fields)
    {
        if (fields.Count is < 1 or > 32)
        {
            throw InvalidOperationRequest("Anki 字段数量无效");
        }
        JsonObject normalized = new();
        foreach ((string name, JsonNode? node) in fields
            .OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            if (name.Length is < 1 or > 128
                || name.Any(char.IsControl)
                || node is not JsonValue value
                || !value.TryGetValue(out string? text)
                || text.Length > 100_000
                || text.Contains('\0'))
            {
                throw InvalidOperationRequest("Anki 字段内容无效");
            }
            normalized[name] = text;
        }
        return normalized;
    }

    private static void ValidateFieldsExist(
        JsonObject note,
        JsonObject desired)
    {
        if (note["fields"] is not JsonObject existing)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_CONNECT_RESPONSE_INVALID",
                "AnkiConnect notesInfo 缺少字段结构");
        }
        foreach (string name in desired.Select(pair => pair.Key))
        {
            if (existing[name] is not JsonObject field
                || field["value"] is not JsonValue value
                || !value.TryGetValue(out string? _))
            {
                throw new ReaderLocalAnkiException(
                    "BW_READER_ANKI_FIELD_NOT_FOUND",
                    "Anki note 不包含字段：" + name);
            }
        }
    }

    private static bool FieldsMatch(
        JsonObject note,
        JsonObject desired)
    {
        JsonObject existing = (JsonObject)note["fields"]!;
        foreach ((string name, JsonNode? node) in desired)
        {
            string desiredValue = node!.GetValue<string>();
            string actual = existing[name]!["value"]!.GetValue<string>();
            if (!string.Equals(actual, desiredValue, StringComparison.Ordinal))
            {
                return false;
            }
        }
        return true;
    }

    private static JsonObject WriteResult(
        ReaderLocalAnkiOperationRequest request,
        bool dedup,
        long[] noteIds,
        long[] cardIds,
        ReaderLocalAnkiSyncOutcome sync)
    {
        JsonObject result = OperationPayload(
            request.Operation,
            request.MutationId,
            dedup,
            localApplied: true,
            localStatus: dedup ? "deduplicated" : "succeeded",
            sync);
        result["note_ids"] = IdArray(noteIds);
        result["card_ids"] = IdArray(cardIds);
        return result;
    }

    private static JsonObject OperationPayload(
        string operation,
        string? mutationId,
        bool dedup,
        bool localApplied,
        string localStatus,
        ReaderLocalAnkiSyncOutcome sync) => new()
        {
            ["ok"] = true,
            ["operation"] = operation,
            ["mutation_id"] = mutationId,
            ["dedup"] = dedup,
            ["anki_local_applied"] = localApplied,
            ["anki_local_status"] = localStatus,
            ["anki_web_sync"] = sync.ToPayload(),
        };

    private static JsonObject Deduplicated(JsonObject result)
    {
        JsonObject clone = result.DeepClone().AsObject();
        clone["dedup"] = true;
        if (clone["anki_local_status"]?.GetValue<string>() == "succeeded")
        {
            clone["anki_local_status"] = "deduplicated";
        }
        return clone;
    }

    private static JsonArray IdArray(IEnumerable<long> ids) => new(
        ids.Select(id => (JsonNode?)id).ToArray());

    private static bool TryPositiveId(JsonNode? node, out long id)
    {
        id = 0;
        if (node is not JsonValue value)
        {
            return false;
        }
        if (value.TryGetValue(out long parsedLong))
        {
            id = parsedLong;
            return id > 0;
        }
        if (value.TryGetValue(out int parsedInt))
        {
            id = parsedInt;
            return id > 0;
        }
        return false;
    }

    private static void RequireNullMutationResult(
        JsonNode? result,
        string action)
    {
        if (result is not null)
        {
            throw new InvalidOperationException(
                $"AnkiConnect {action} 响应无效");
        }
    }

    private static string OperationFingerprint(
        ReaderLocalAnkiOperationRequest request)
    {
        JsonObject canonical = new()
        {
            ["operation"] = request.Operation,
            ["noteIds"] = IdArray(request.NoteIds),
            ["cardIds"] = IdArray(request.CardIds),
            ["fields"] = request.Fields is null
                ? null
                : NormalizeFields(request.Fields),
            ["answers"] = new JsonArray(request.Answers.Select(answer =>
                (JsonNode?)new JsonObject
                {
                    ["cardId"] = answer.CardId,
                    ["ease"] = answer.Ease,
                }).ToArray()),
            ["syncMode"] = request.SyncMode,
        };
        return Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(canonical.ToJsonString(
                DirectBridgeContract.JsonOptions))))
            .ToLowerInvariant();
    }

    private static void ValidateOperationRequest(
        ReaderLocalAnkiOperationRequest request)
    {
        if (request.Operation is not (
            "read-notes"
            or "read-cards"
            or "update-note-fields"
            or "delete-notes"
            or "answer-cards"
            or "sync"))
        {
            throw InvalidOperationRequest("Reader 本地 Anki 操作类型无效");
        }
        RequireDistinctPositiveIds(request.NoteIds, "note");
        RequireDistinctPositiveIds(request.CardIds, "card");
        if (request.Operation == "read-notes"
            && (request.NoteIds.Length is < 1 or > 20
                || request.CardIds.Length != 0
                || request.Fields is not null
                || request.Answers.Length != 0
                || request.MutationId is not null
                || request.SyncMode is not null))
        {
            throw InvalidOperationRequest("read-notes 请求无效");
        }
        if (request.Operation == "read-cards"
            && (request.CardIds.Length is < 1 or > 20
                || request.NoteIds.Length != 0
                || request.Fields is not null
                || request.Answers.Length != 0
                || request.MutationId is not null
                || request.SyncMode is not null))
        {
            throw InvalidOperationRequest("read-cards 请求无效");
        }
        if (request.Operation == "update-note-fields"
            && (request.NoteIds.Length != 1
                || request.CardIds.Length != 0
                || request.Fields is null
                || request.Answers.Length != 0
                || !IsMutation(request)))
        {
            throw InvalidOperationRequest(
                "update-note-fields 请求无效");
        }
        if (request.Operation == "delete-notes"
            && (request.NoteIds.Length is < 1 or > 20
                || request.CardIds.Length != 0
                || request.Fields is not null
                || request.Answers.Length != 0
                || !IsMutation(request)))
        {
            throw InvalidOperationRequest("delete-notes 请求无效");
        }
        if (request.Operation == "answer-cards"
            && (request.NoteIds.Length != 0
                || request.CardIds.Length != 0
                || request.Fields is not null
                || request.Answers.Length is < 1 or > 20
                || request.Answers.Any(answer =>
                    answer.CardId <= 0 || answer.Ease is < 1 or > 4)
                || request.Answers.Select(answer => answer.CardId)
                    .Distinct().Count() != request.Answers.Length
                || !IsMutation(request)))
        {
            throw InvalidOperationRequest("answer-cards 请求无效");
        }
        if (request.Operation == "sync"
            && (request.NoteIds.Length != 0
                || request.CardIds.Length != 0
                || request.Fields is not null
                || request.Answers.Length != 0
                || request.SyncMode is not null
                || request.MutationId is null
                || !DirectBridgeContract.IsSafeId(request.MutationId)))
        {
            throw InvalidOperationRequest("sync 请求无效");
        }
        if (request.Fields is not null)
        {
            _ = NormalizeFields(request.Fields);
        }
    }

    private static bool IsMutation(ReaderLocalAnkiOperationRequest request) =>
        request.MutationId is not null
        && DirectBridgeContract.IsSafeId(request.MutationId)
        && request.SyncMode is "background" or "wait";

    private static void RequireDistinctPositiveIds(
        long[] ids,
        string kind)
    {
        if (ids.Any(id => id <= 0)
            || ids.Distinct().Count() != ids.Length)
        {
            throw InvalidOperationRequest($"Anki {kind} ID 无效");
        }
    }

    private static ReaderLocalAnkiException MapOperationConnectFailure(
        ReaderAnkiConnectException exception) => exception.Failure switch
        {
            ReaderAnkiConnectFailure.Unreachable => new(
                "BW_READER_ANKI_CONNECT_UNREACHABLE",
                "AnkiConnect 不可达，请先启动 Anki",
                retryable: true,
                innerException: exception),
            ReaderAnkiConnectFailure.InvalidResponse => new(
                "BW_READER_ANKI_CONNECT_RESPONSE_INVALID",
                "AnkiConnect 响应无效",
                innerException: exception),
            _ => new(
                "BW_READER_ANKI_CONNECT_ERROR",
                "AnkiConnect 拒绝操作：" + SafeError(exception.Message),
                retryable: true,
                innerException: exception),
        };

    private static ReaderLocalAnkiException UnknownOperationOutcome(
        Exception? exception = null) => new(
            "BW_READER_ANKI_OPERATION_OUTCOME_UNKNOWN",
            "上一次 Anki 操作结果未知；为避免重复修改，本次不会再次写入",
            retryable: false,
            innerException: exception);

    private static ReaderLocalAnkiException InvalidOperationRequest(
        string message) => new(
            "BW_READER_ANKI_REQUEST_INVALID",
            message);

    private static string SafeError(string value)
    {
        string safe = value.Replace('\0', ' ').Trim();
        return safe.Length <= 300 ? safe : safe[..300];
    }

    private async Task<ReaderLocalAnkiPreparedNote> PrepareNoteAsync(
        ReaderLocalAnkiRegisteredCard registered,
        string aidTag,
        string fingerprintTag,
        CancellationToken cancellationToken)
    {
        string[] models = RequireStrings(await _client.CallAsync(
            "modelNames", new JsonObject(), cancellationToken)
            .ConfigureAwait(false));
        string type = registered.Card["type"]!.GetValue<string>();
        string model = type == "cloze"
            ? PickModel(models, "Cloze", "填空题", "挖空题")
            : PickModel(models, "Basic", "基础的", "基本");
        string[] fields = RequireStrings(await _client.CallAsync(
            "modelFieldNames",
            new JsonObject { ["modelName"] = model },
            cancellationToken).ConfigureAwait(false));
        if (fields.Length == 0)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "Anki 模型没有可写字段");
        }
        _ = await _client.CallAsync(
            "createDeck",
            new JsonObject { ["deck"] = DeckName },
            cancellationToken).ConfigureAwait(false);

        JsonObject noteFields = BuildFields(registered, type, fields);
        return new ReaderLocalAnkiPreparedNote(
            model,
            noteFields,
            ["pdf-snippets", "card-lab", aidTag, fingerprintTag]);
    }

    private async Task<long> AddClaimedAsync(
        ReaderLocalAnkiPreparedNote prepared,
        CancellationToken cancellationToken)
    {
        JsonNode? noteResult = await _client.CallAsync(
            "addNote",
            new JsonObject
            {
                ["note"] = new JsonObject
                {
                    ["deckName"] = DeckName,
                    ["modelName"] = prepared.ModelName,
                    ["fields"] = prepared.Fields.DeepClone(),
                    ["tags"] = new JsonArray(
                        prepared.Tags.Select(value => (JsonNode?)value)
                            .ToArray()),
                },
            },
            cancellationToken).ConfigureAwait(false);
        return RequirePositiveId(noteResult);
    }

    private async Task<ReaderLocalAnkiAddResult> RecoverExistingAsync(
        long[] noteIds,
        string aidTag,
        string fingerprintTag,
        CancellationToken cancellationToken)
    {
        if (noteIds.Length != 1)
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_AID_AMBIGUOUS",
                "Anki 中同一 aid 对应多条笔记，已停止写入");
        }
        JsonNode? infoNode = await _client.CallAsync(
            "notesInfo",
            new JsonObject
            {
                ["notes"] = new JsonArray(
                    noteIds.Select(value => (JsonNode?)value).ToArray()),
            },
            cancellationToken).ConfigureAwait(false);
        if (infoNode is not JsonArray info || info.Count != 1
            || info[0] is not JsonObject note
            || note["tags"] is not JsonArray tags)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect notesInfo 响应无效");
        }
        HashSet<string> tagSet = tags
            .Select(node => node?.GetValue<string>() ?? "")
            .ToHashSet(StringComparer.Ordinal);
        if (!tagSet.Contains(aidTag)
            || !tagSet.Contains(fingerprintTag))
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_AID_REUSED",
                "Anki 中的 aid 已对应另一份内容");
        }
        return await CompleteResultAsync(
            noteIds, cancellationToken).ConfigureAwait(false);
    }

    private async Task<ReaderLocalAnkiAddResult> CompleteResultAsync(
        long[] noteIds,
        CancellationToken cancellationToken)
    {
        List<long> allCards = [];
        Dictionary<string, long[]> byNote = new(StringComparer.Ordinal);
        foreach (long noteId in noteIds)
        {
            long[] cardIds;
            try
            {
                cardIds = RequireIds(await _client.CallAsync(
                    "findCards",
                    new JsonObject { ["query"] = $"nid:{noteId}" },
                    cancellationToken).ConfigureAwait(false));
                if (cardIds.Length > 0)
                {
                    _ = await _client.CallAsync(
                        "changeDeck",
                        new JsonObject
                        {
                            ["cards"] = new JsonArray(cardIds
                                .Select(value => (JsonNode?)value).ToArray()),
                            ["deck"] = DeckName,
                        },
                        cancellationToken).ConfigureAwait(false);
                }
            }
            catch (ReaderAnkiConnectException)
            {
                // addNote/findNotes already proved the note identity. Never
                // repeat that irreversible mutation merely because optional
                // card-id/deck enrichment failed.
                cardIds = [];
            }
            byNote[noteId.ToString()] = cardIds;
            allCards.AddRange(cardIds);
        }
        return new ReaderLocalAnkiAddResult(
            noteIds,
            allCards.Distinct().Order().ToArray(),
            byNote);
    }

    private async Task CommitIfPendingAsync(
        string aid,
        string fingerprint,
        ReaderLocalAnkiAddResult result,
        CancellationToken cancellationToken)
    {
        ReaderLocalAnkiReceipt? receipt = await _registry.ReadReceiptAsync(
            aid, cancellationToken).ConfigureAwait(false);
        if (receipt?.State == "done")
        {
            return;
        }
        if (receipt?.State != "pending")
        {
            throw new ReaderLocalAnkiException(
                "BW_READER_ANKI_REGISTRY_INVALID",
                "Anki 本地回执状态无效");
        }
        await _registry.CommitAsync(
            aid,
            fingerprint,
            result,
            cancellationToken).ConfigureAwait(false);
    }

    private static JsonObject BuildFields(
        ReaderLocalAnkiRegisteredCard registered,
        string type,
        string[] fields)
    {
        string footer = ProvenanceFooter(registered);
        JsonObject result = new();
        if (type == "cloze")
        {
            result[fields[0]] = registered.Card["cloze"]!.GetValue<string>();
            if (fields.Length > 1)
            {
                result[fields[1]] = footer;
            }
            else
            {
                result[fields[0]] = result[fields[0]]!.GetValue<string>()
                    + footer;
            }
            return result;
        }
        string front = registered.Card["front"]!.GetValue<string>();
        string back = registered.Card["back"]!.GetValue<string>() + footer;
        result[fields[0]] = front;
        if (fields.Length > 1)
        {
            result[fields[1]] = back;
        }
        else
        {
            result[fields[0]] = front + "<hr>" + back;
        }
        return result;
    }

    private static string ProvenanceFooter(
        ReaderLocalAnkiRegisteredCard registered)
    {
        if (string.IsNullOrWhiteSpace(registered.File))
        {
            return "";
        }
        string location = registered.Target["kind"]?.GetValue<string>() switch
        {
            "pdf" => "p" + registered.Target["page"]?.GetValue<long>(),
            "epub" => "section "
                + registered.Target["section"]?.GetValue<long>(),
            _ => "",
        };
        string source = WebUtility.HtmlEncode(
            registered.File + (location.Length > 0 ? "#" + location : ""));
        return "<hr><div style=\"font-size:0.85em;color:#666;\">"
            + "来源：" + source + "</div>";
    }

    private static string PickModel(
        string[] available,
        params string[] candidates)
    {
        foreach (string candidate in candidates)
        {
            if (available.Contains(candidate, StringComparer.Ordinal))
            {
                return candidate;
            }
        }
        throw new ReaderAnkiConnectException(
            ReaderAnkiConnectFailure.RemoteError,
            "Anki 缺少兼容的 " + candidates[0] + " 模型");
    }

    private static string[] RequireStrings(JsonNode? node)
    {
        if (node is not JsonArray values)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect 响应无效");
        }
        List<string> result = [];
        foreach (JsonNode? value in values)
        {
            if (value is not JsonValue jsonValue
                || !jsonValue.TryGetValue(out string? text)
                || string.IsNullOrEmpty(text)
                || text.Length > 256
                || text.Contains('\0'))
            {
                throw new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.InvalidResponse,
                    "AnkiConnect 响应无效");
            }
            result.Add(text);
        }
        return result.ToArray();
    }

    private static long[] RequireIds(JsonNode? node)
    {
        if (node is not JsonArray values || values.Count > 100)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect ID 响应无效");
        }
        List<long> result = [];
        foreach (JsonNode? value in values)
        {
            if (value is not JsonValue jsonValue
                || !jsonValue.TryGetValue(out long parsed)
                || parsed <= 0)
            {
                throw new ReaderAnkiConnectException(
                    ReaderAnkiConnectFailure.InvalidResponse,
                    "AnkiConnect ID 响应无效");
            }
            result.Add(parsed);
        }
        return result.Distinct().Order().ToArray();
    }

    private static long RequirePositiveId(JsonNode? node)
    {
        long value;
        if (node is not JsonValue jsonValue)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect addNote 响应无效");
        }
        try
        {
            if (!long.TryParse(
                jsonValue.ToJsonString(
                    DirectBridgeContract.JsonOptions),
                System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture,
                out value))
            {
                throw new FormatException("Anki note id is not an integer");
            }
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or FormatException)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect addNote 响应无效",
                exception);
        }
        if (value <= 0)
        {
            throw new ReaderAnkiConnectException(
                ReaderAnkiConnectFailure.InvalidResponse,
                "AnkiConnect addNote 响应无效");
        }
        return value;
    }

    private static string AidTag(string aid) => "bw_reader_aid_" + aid;
    private static string FingerprintTag(string fingerprint) =>
        "bw_reader_payload_" + fingerprint;

    private static ReaderLocalAnkiException MapConnectFailure(
        ReaderAnkiConnectException exception,
        bool pending)
    {
        if (pending)
        {
            return UnknownOutcome(exception);
        }
        return exception.Failure switch
        {
            ReaderAnkiConnectFailure.Unreachable => new(
                "BW_READER_ANKI_CONNECT_UNREACHABLE",
                "AnkiConnect 不可达，请先启动 Anki",
                retryable: true,
                innerException: exception),
            ReaderAnkiConnectFailure.InvalidResponse => new(
                "BW_READER_ANKI_CONNECT_RESPONSE_INVALID",
                "AnkiConnect 响应无效",
                innerException: exception),
            _ => new(
                "BW_READER_ANKI_CONNECT_ERROR",
                "AnkiConnect 拒绝写入：" + exception.Message,
                retryable: true,
                innerException: exception),
        };
    }

    private static ReaderLocalAnkiException UnknownOutcome(
        Exception? exception = null) =>
        new(
            "BW_READER_ANKI_ADD_OUTCOME_UNKNOWN",
            "上一次 addNote 结果未知；为避免重复制卡，本次不会再次写入",
            retryable: false,
            innerException: exception);
}
