using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Globalization;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectActiveReading(
    string Kind,
    string File,
    string? Title,
    JsonElement Page,
    string SelectionState,
    string? Selection,
    long ObservedAtEpochMilliseconds,
    string? ViewFile = null,
    JsonElement? ViewPage = null,
    string? SourceInstanceId = null,
    JsonElement? SelectionRegions = null);

internal sealed record DirectViewportContext(
    string SourceInstanceId,
    string DocumentKey,
    string Url,
    string? Title,
    string BeforeText,
    string VisibleText,
    string AfterText,
    string SelectionState,
    string? Selection,
    long ObservedAtEpochMilliseconds,
    string? ControlCorrelation = null);

internal sealed record DirectSnapshotForwardResult(
    string Outcome,
    long Revision);

internal interface IDirectSnapshotContextAdapter
{
    Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    public Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    private static DirectProtocolException Unavailable() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_UNAVAILABLE",
            "Windows 本地 Reader 快照存储尚未接线",
            retryable: true);
}

internal sealed class FileDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    internal const string SnapshotContract = "reader-context-snapshot/1";
    internal const string ActiveReadingContract =
        "reader-active-reading/1";
    internal const string SnapshotFileName =
        "reader-context-snapshot.json";
    internal const string MarkdownFileName =
        "reader-context-live.md";

    private const int MaximumSnapshotBytes = 128 * 1024;
    private const int RecentEventLimit = 256;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _statePath;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly string _producerInstanceId =
        Guid.NewGuid().ToString("N");
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Queue<string> _recentEventOrder = new();
    private readonly HashSet<string> _recentEventIds =
        new(StringComparer.Ordinal);
    private long _revision;
    private JsonObject? _stablePage;
    private JsonObject? _activeReading;
    private JsonObject _selection = UnknownSelection(
        "snapshot-not-received");
    private JsonObject _focus = UnknownFocus(
        "snapshot-not-received");
    private JsonObject? _latestEvent;

    private sealed record AdapterState(
        long Revision,
        JsonObject? StablePage,
        JsonObject? ActiveReading,
        JsonObject Selection,
        JsonObject Focus,
        JsonObject? LatestEvent,
        IReadOnlyList<string> RecentEventOrder);

    private sealed record FocusFoldResult(
        JsonObject Focus,
        JsonObject? Selection);

    internal FileDirectSnapshotContextAdapter(
        string statePath,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!System.IO.Path.IsPathFullyQualified(statePath))
        {
            throw new ArgumentException(
                "snapshot state path must be absolute",
                nameof(statePath));
        }
        _statePath = System.IO.Path.GetFullPath(statePath);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
        LoadExistingState();
    }

    internal string StatePath => _statePath;

    public async Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_recentEventIds.Contains(contextEvent.EventId))
            {
                return new DirectSnapshotForwardResult(
                    "duplicate",
                    _revision);
            }
            AdapterState before = CaptureState();
            try
            {
                FoldJournal(contextEvent);
                RememberEvent(contextEvent.EventId);
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult>
        ForwardActiveReadingAsync(
            string requestId,
            string sessionId,
            DirectActiveReading activeReading,
            CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                JsonObject next = new()
                {
                    ["kind"] = activeReading.Kind,
                    ["file"] = activeReading.File,
                    ["title"] = activeReading.Title,
                    ["page"] = JsonNode.Parse(
                        activeReading.Page.GetRawText()),
                    ["fresh"] = true,
                    ["ageSec"] = 0,
                    ["observedAtEpochMs"] =
                        activeReading.ObservedAtEpochMilliseconds,
                    ["receivedAtEpochMs"] =
                        _utcNow().ToUnixTimeMilliseconds(),
                };
                if (
                    activeReading.ViewFile is not null
                    && activeReading.ViewPage is JsonElement viewPage
                )
                {
                    next["viewFile"] = activeReading.ViewFile;
                    next["viewPage"] = JsonNode.Parse(
                        viewPage.GetRawText());
                }
                if (activeReading.SourceInstanceId is not null)
                {
                    next["sourceInstanceId"] =
                        activeReading.SourceInstanceId;
                }
                if (activeReading.SelectionRegions is JsonElement regions)
                {
                    next["selectionRegions"] = JsonNode.Parse(
                        regions.GetRawText());
                }
                if (_activeReading is JsonObject priorActive)
                {
                    PreserveActiveReadingContinuity(
                        priorActive,
                        next);
                }
                bool changedPage = _activeReading is not null
                    && !SameActiveReadingIdentity(_activeReading, next);
                _activeReading = next;
                if (activeReading.SelectionState == "active")
                {
                    _selection = ActiveSelection(
                        activeReading.Selection!,
                        next);
                }
                else if (activeReading.SelectionState == "cleared")
                {
                    _selection = ClearedSelection(
                        "active-reading-cleared");
                }
                else if (
                    changedPage
                    || (
                        _selection["ref"] is JsonObject selectionRef
                        && !SameLogicalPage(next, selectionRef, next)
                    )
                )
                {
                    _selection = UnknownSelection(
                        "active-page-changed");
                }
                if (changedPage)
                {
                    _focus = UnknownFocus("active-page-changed");
                }
                _revision = checked(_revision + 1);
                _latestEvent = new JsonObject
                {
                    ["source"] = "active-reading",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "active.reading",
                    ["ts"] =
                        activeReading.ObservedAtEpochMilliseconds,
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                if (
                    _activeReading is not JsonObject active
                    || !string.Equals(
                        StringValue(active["sourceInstanceId"]),
                        viewport.SourceInstanceId,
                        StringComparison.Ordinal)
                    || !string.Equals(
                        StringValue(active["file"]),
                        viewport.Url,
                        StringComparison.Ordinal)
                    || _stablePage is not JsonObject stable
                    || !string.Equals(
                        StringValue(stable["file"]),
                        viewport.Url,
                        StringComparison.Ordinal)
                )
                {
                    throw new DirectProtocolException(
                        "BW_READER_VIEWPORT_IDENTITY_MISMATCH",
                        "Reader 当前视口与活动页面身份不一致",
                        retryable: true);
                }
                JsonObject viewportJson = new()
                {
                    ["contract"] = "reader-viewport/1",
                    ["sourceInstanceId"] = viewport.SourceInstanceId,
                    ["documentKey"] = viewport.DocumentKey,
                    ["url"] = viewport.Url,
                    ["title"] = viewport.Title,
                    ["beforeText"] = viewport.BeforeText,
                    ["visibleText"] = viewport.VisibleText,
                    ["afterText"] = viewport.AfterText,
                    ["selectionState"] = viewport.SelectionState,
                    ["selection"] = viewport.Selection,
                    ["observedAtEpochMs"] =
                        viewport.ObservedAtEpochMilliseconds,
                    ["receivedAtEpochMs"] =
                        _utcNow().ToUnixTimeMilliseconds(),
                };
                if (viewport.ControlCorrelation is not null)
                {
                    viewportJson["controlCorrelation"] =
                        viewport.ControlCorrelation;
                }
                stable["sourceInstanceId"] = viewport.SourceInstanceId;
                stable["documentKey"] = viewport.DocumentKey;
                stable["readingWindow"] = viewportJson;
                stable["text"] = viewport.VisibleText;
                stable["textAvailable"] =
                    !string.IsNullOrWhiteSpace(viewport.VisibleText);
                _revision = checked(_revision + 1);
                _latestEvent = new JsonObject
                {
                    ["source"] = "viewport",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "viewport.context",
                    ["ts"] = viewport.ObservedAtEpochMilliseconds,
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                _revision = checked(_revision + 1);
                _stablePage = null;
                _activeReading = null;
                _selection = UnknownSelection("snapshot-cleared");
                _focus = UnknownFocus("snapshot-cleared");
                _latestEvent = new JsonObject
                {
                    ["source"] = "context-control",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "context.clear",
                    ["ts"] = _utcNow().ToUnixTimeMilliseconds(),
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult(
                "accepted",
                _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal static DirectActiveReading ValidateActiveReading(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> keys = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        string[] requiredKeys =
        {
            "kind",
            "file",
            "title",
            "page",
            "selectionState",
            "selection",
            "observedAtEpochMs",
        };
        HashSet<string> allowedKeys = requiredKeys
            .Append("viewFile")
            .Append("viewPage")
            .Append("sourceInstanceId")
            .Append("selectionRegions")
            .ToHashSet(StringComparer.Ordinal);
        if (
            requiredKeys.Any(key => !keys.Contains(key))
            || keys.Any(key => !allowedKeys.Contains(key))
            || keys.Contains("viewFile") != keys.Contains("viewPage")
        )
        {
            throw ActiveReadingInvalid();
        }
        if (
            value.GetProperty("kind").ValueKind != JsonValueKind.String
            || value.GetProperty("kind").GetString() is not string kind
            || kind is not ("pdf" or "epub" or "html" or "web")
            || value.GetProperty("file").ValueKind != JsonValueKind.String
            || value.GetProperty("file").GetString() is not string file
            || file.Length is < 1 or > 4096
            || file.Any(char.IsControl)
            || file.StartsWith("vbook:", StringComparison.Ordinal)
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement page = value.GetProperty("page");
        bool pageValid = page.ValueKind switch
        {
            JsonValueKind.Null => true,
            JsonValueKind.Number =>
                page.TryGetInt64(out long number)
                && number is >= 0 and <= 9_007_199_254_740_991,
            JsonValueKind.String =>
                page.GetString() is string text
                && text.Length is >= 1 and <= 256
                && !text.Any(char.IsControl),
            _ => false,
        };
        if (
            !pageValid
            || value.GetProperty("selectionState").ValueKind
                != JsonValueKind.String
            || value.GetProperty("selectionState").GetString()
                is not string selectionState
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw ActiveReadingInvalid();
        }
        string? viewFile = null;
        JsonElement? viewPage = null;
        if (keys.Contains("viewFile"))
        {
            JsonElement viewFileValue = value.GetProperty("viewFile");
            JsonElement viewPageValue = value.GetProperty("viewPage");
            if (
                kind == "web"
                || viewFileValue.ValueKind != JsonValueKind.String
                || viewFileValue.GetString() is not string candidateViewFile
                || !candidateViewFile.StartsWith(
                    "vbook:",
                    StringComparison.Ordinal)
                || candidateViewFile.Length is < 7 or > 4096
                || candidateViewFile.Any(char.IsControl)
                || !ValidPageIdentifier(
                    JsonNode.Parse(viewPageValue.GetRawText()),
                    allowNull: false)
            )
            {
                throw ActiveReadingInvalid();
            }
            viewFile = candidateViewFile;
            viewPage = viewPageValue.Clone();
        }
        string? sourceInstanceId = null;
        if (keys.Contains("sourceInstanceId"))
        {
            JsonElement sourceValue = value.GetProperty(
                "sourceInstanceId");
            if (
                sourceValue.ValueKind != JsonValueKind.String
                || sourceValue.GetString() is not string candidateSource
                || candidateSource.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(candidateSource)
            )
            {
                throw ActiveReadingInvalid();
            }
            sourceInstanceId = candidateSource;
        }
        JsonElement? selectionRegions = null;
        if (keys.Contains("selectionRegions"))
        {
            selectionRegions = ValidateSelectionRegions(
                value.GetProperty("selectionRegions"));
        }
        JsonElement selectionValue = value.GetProperty("selection");
        string? selection = selectionValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => selectionValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            selection is { Length: > 400 }
            || (
                selectionState == "active"
                && string.IsNullOrWhiteSpace(selection)
            )
            || (
                selectionState != "active"
                && selection is not null
            )
        )
        {
            throw ActiveReadingInvalid();
        }
        return new DirectActiveReading(
            kind,
            file,
            title,
            page.Clone(),
            selectionState,
            selection,
            observedAt,
            viewFile,
            viewPage,
            sourceInstanceId,
            selectionRegions);
    }

    private static JsonElement ValidateSelectionRegions(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> fields = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            !fields.SetEquals(new[]
            {
                "contract", "total", "truncated", "items",
            })
            || value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("contract").GetString()
                != "reader-selection-regions/1"
            || !value.GetProperty("total").TryGetInt32(out int total)
            || total is < 0 or > 1_000_000
            || value.GetProperty("truncated").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
            || value.GetProperty("items").ValueKind
                != JsonValueKind.Array
        )
        {
            throw ActiveReadingInvalid();
        }
        bool truncated = value.GetProperty("truncated").GetBoolean();
        JsonElement items = value.GetProperty("items");
        int count = items.GetArrayLength();
        if (
            count > 128
            || truncated != (total > count)
            || (!truncated && total != count)
        )
        {
            throw ActiveReadingInvalid();
        }
        HashSet<string> ids = new(StringComparer.Ordinal);
        int priorOrdinal = 0;
        foreach (JsonElement item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw ActiveReadingInvalid();
            }
            try
            {
                DirectJsonValidation.RequireNoDuplicateKeys(item);
            }
            catch (DirectProtocolException exception)
            {
                throw ActiveReadingInvalid(exception);
            }
            HashSet<string> itemFields = item.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            if (
                !itemFields.SetEquals(new[]
                {
                    "selectionId", "label", "ordinal",
                    "createdAtEpochMs",
                })
                || item.GetProperty("selectionId").ValueKind
                    != JsonValueKind.String
                || item.GetProperty("selectionId").GetString()
                    is not string selectionId
                || selectionId.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(selectionId)
                || !ids.Add(selectionId)
                || item.GetProperty("label").ValueKind
                    != JsonValueKind.String
                || item.GetProperty("label").GetString()
                    is not string label
                || label.Length is < 1 or > 80
                || label.Any(char.IsControl)
                || !item.GetProperty("ordinal").TryGetInt32(
                    out int ordinal)
                || ordinal <= priorOrdinal
                || ordinal < 1
                || !label.StartsWith(
                    "#" + ordinal.ToString(
                        CultureInfo.InvariantCulture) + " ",
                    StringComparison.Ordinal)
                || !item.GetProperty("createdAtEpochMs").TryGetInt64(
                    out long createdAt)
                || createdAt is < 0 or > 9_007_199_254_740_991
            )
            {
                throw ActiveReadingInvalid();
            }
            priorOrdinal = ordinal;
        }
        return value.Clone();
    }

    internal static bool SelectionRegionExists(
        JsonNode? source,
        string? selectionId)
    {
        if (
            source is null
            || string.IsNullOrEmpty(selectionId)
            || selectionId.Length > 160
            || !DirectBridgeContract.IsSafeId(selectionId)
        )
        {
            return false;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                source.ToJsonString(DirectBridgeContract.JsonOptions));
            JsonElement validated = ValidateSelectionRegions(
                document.RootElement);
            return validated.GetProperty("items")
                .EnumerateArray()
                .Any(item => string.Equals(
                    item.GetProperty("selectionId").GetString(),
                    selectionId,
                    StringComparison.Ordinal));
        }
        catch (Exception exception) when (
            exception is DirectProtocolException
            or JsonException
            or InvalidOperationException
        )
        {
            return false;
        }
    }

    internal static DirectViewportContext ValidateViewport(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ViewportInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ViewportInvalid(exception);
        }
        string[] requiredFields =
        {
            "contract",
            "sourceInstanceId",
            "documentKey",
            "url",
            "title",
            "beforeText",
            "visibleText",
            "afterText",
            "selectionState",
            "selection",
            "observedAtEpochMs",
        };
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        HashSet<string> allowed = requiredFields
            .Append("controlCorrelation")
            .ToHashSet(StringComparer.Ordinal);
        if (
            requiredFields.Any(field => !actual.Contains(field))
            || actual.Any(field => !allowed.Contains(field))
        )
        {
            throw ViewportInvalid();
        }
        string sourceInstanceId = RequiredSafeViewportId(
            value,
            "sourceInstanceId",
            160);
        string documentKey = RequiredViewportUrl(
            value,
            "documentKey");
        if (
            value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("contract").GetString()
                != "reader-viewport/1"
            || value.GetProperty("url").ValueKind
                != JsonValueKind.String
            || value.GetProperty("url").GetString() is not string url
            || url.Length is < 1 or > 4096
            || url.Any(char.IsControl)
            || !Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
            || uri.Scheme is not ("http" or "https")
            || value.GetProperty("selectionState").ValueKind
                != JsonValueKind.String
            || value.GetProperty("selectionState").GetString()
                is not string selectionState
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw ViewportInvalid();
        }
        string beforeText = RequiredViewportText(
            value,
            "beforeText",
            12_000);
        string visibleText = RequiredViewportText(
            value,
            "visibleText",
            24_000);
        string afterText = RequiredViewportText(
            value,
            "afterText",
            12_000);
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw ViewportInvalid(),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw ViewportInvalid();
        }
        JsonElement selectionValue = value.GetProperty("selection");
        string? selection = selectionValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => selectionValue.GetString(),
            _ => throw ViewportInvalid(),
        };
        if (
            selection is { Length: > 400 }
            || (
                selectionState == "active"
                && string.IsNullOrWhiteSpace(selection)
            )
            || (selectionState != "active" && selection is not null)
        )
        {
            throw ViewportInvalid();
        }
        string? controlCorrelation = null;
        if (actual.Contains("controlCorrelation"))
        {
            controlCorrelation = RequiredSafeViewportId(
                value,
                "controlCorrelation",
                160);
        }
        return new DirectViewportContext(
            sourceInstanceId,
            documentKey,
            url,
            title,
            beforeText,
            visibleText,
            afterText,
            selectionState,
            selection,
            observedAt,
            controlCorrelation);
    }

    private static string RequiredSafeViewportId(
        JsonElement value,
        string field,
        int maximumLength)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
            || !DirectBridgeContract.IsSafeId(result)
        )
        {
            throw ViewportInvalid();
        }
        return result;
    }

    private static string RequiredViewportUrl(
        JsonElement value,
        string field)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length is < 1 or > 4096
            || result.Any(char.IsControl)
            || !Uri.TryCreate(result, UriKind.Absolute, out Uri? uri)
            || uri.Scheme is not ("http" or "https")
        )
        {
            throw ViewportInvalid();
        }
        return result;
    }

    private static string RequiredViewportText(
        JsonElement value,
        string field,
        int maximumLength)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length > maximumLength
            || result.Any(character =>
                char.IsControl(character)
                && character is not ('\r' or '\n' or '\t'))
        )
        {
            throw ViewportInvalid();
        }
        return result.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Trim();
    }

    private void FoldJournal(DirectContextEvent contextEvent)
    {
        JsonObject value = JsonNode.Parse(
            contextEvent.Payload.GetRawText()) as JsonObject
            ?? throw JournalInvalid();
        JsonObject latestEvent = new()
        {
            ["source"] = "outgoing-context",
            ["seq"] = contextEvent.Sequence,
            ["id"] = contextEvent.EventId,
            ["type"] = contextEvent.Type,
            ["ts"] = value["ts"]?.DeepClone(),
        };

        if (contextEvent.Type == "page.context")
        {
            (JsonObject stablePage, JsonObject activeReading) =
                BuildPageContext(value);
            bool changedPage = _stablePage is not null
                && !SamePage(_stablePage, stablePage);
            if (
                _activeReading is JsonObject priorActive
                && SamePageEquivalent(priorActive, activeReading)
            )
            {
                PreserveActiveReadingContinuity(
                    priorActive,
                    activeReading);
            }
            _stablePage = stablePage;
            _activeReading = activeReading;
            if (changedPage)
            {
                _selection = ClearedSelection(
                    "stable-page-changed");
                _focus = UnknownFocus("stable-page-changed");
            }
        }
        else if (contextEvent.Type == "focus")
        {
            FocusFoldResult folded = BuildFocus(
                value,
                _stablePage,
                _activeReading);
            _focus = folded.Focus;
            if (folded.Selection is not null)
            {
                _selection = folded.Selection;
            }
        }
        else if (contextEvent.Type == "drawing")
        {
            _stablePage = FoldDrawingEvent(value, _stablePage);
        }
        else if (contextEvent.Type == "command-failed")
        {
            AddCommandFailure(latestEvent, value);
        }
        _revision = checked(_revision + 1);
        _latestEvent = latestEvent;
    }

    private (JsonObject StablePage, JsonObject ActiveReading)
        BuildPageContext(JsonObject value)
    {
        string? file = StringValue(value["file"])
            ?? StringValue(value["book_id"]);
        string? kind = StringValue(value["kind"]);
        JsonNode? page = value["page"]?.DeepClone();
        JsonObject? pageContext = value["page_context"] as JsonObject;
        bool stable = BooleanValue(
            value["stable"],
            out bool stableValue)
            && stableValue;
        if (
            string.IsNullOrWhiteSpace(file)
            || file.Length > 4096
            || file.Any(char.IsControl)
            || kind is not ("pdf" or "epub" or "html" or "web")
            || !ValidPageIdentifier(page, allowNull: false)
            || pageContext is null
            || !stable
        )
        {
            throw JournalInvalid();
        }
        JsonNode safePage = page
            ?? throw JournalInvalid();
        JsonObject next = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = safePage.DeepClone(),
            ["stable"] = true,
            ["reason"] = StringValue(pageContext["reason"]),
            ["text"] = StringValue(pageContext["text"]) ?? "",
            ["textAvailable"] =
                pageContext["text_available"]?.GetValue<bool?>(),
            ["textSource"] =
                StringValue(pageContext["text_source"]),
            ["fallbackReason"] =
                StringValue(pageContext["fallback_reason"]),
            ["truncated"] =
                pageContext["truncated"]?.GetValue<bool?>(),
        };
        JsonObject? visual = CopyVisual(
            pageContext["visual"],
            file,
            safePage);
        if (visual is not null)
        {
            next["visual"] = visual;
        }
        JsonObject? embeds = CopyEmbeds(pageContext["embeds"]);
        if (embeds is not null)
        {
            next["embeds"] = embeds;
        }
        JsonObject? viewport = CopyViewport(
            pageContext["viewport"],
            kind);
        if (viewport is not null)
        {
            next["viewport"] = viewport;
        }
        JsonObject activeReading = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = safePage.DeepClone(),
            ["fresh"] = true,
            ["ageSec"] = 0,
            ["observedAtEpochMs"] =
                EpochSecondsToMilliseconds(value["ts"]),
            ["receivedAtEpochMs"] =
                _utcNow().ToUnixTimeMilliseconds(),
        };
        return (next, activeReading);
    }

    private static FocusFoldResult BuildFocus(
        JsonObject value,
        JsonObject? stablePage,
        JsonObject? activeReading)
    {
        string? action = StringValue(value["action"]);
        if (action == "cancel")
        {
            JsonObject? cancelled =
                value["cancelledObject"] as JsonObject;
            string? kind = StringValue(cancelled?["kind"]);
            JsonObject? reference = cancelled?["ref"] as JsonObject;
            if (
                !ValidFocusKind(kind)
                || reference is null
            )
            {
                throw JournalInvalid();
            }
            JsonObject safeReference = CopyFocusReference(
                kind!,
                reference);
            return new FocusFoldResult(
                new JsonObject
                {
                    ["state"] = "cleared",
                    ["kind"] = kind,
                    ["ref"] = safeReference,
                    ["reason"] = "explicit-cancel",
                },
                kind == "text"
                    ? ClearedSelection("explicit-focus-cancel")
                    : null);
        }
        if (action is not ("set" or "replace"))
        {
            throw JournalInvalid();
        }
        string? focusKind = StringValue(value["kind"]);
        JsonObject? focusReference = value["ref"] as JsonObject;
        if (
            !ValidFocusKind(focusKind)
            || focusReference is null
        )
        {
            throw JournalInvalid();
        }
        JsonObject safe = CopyFocusReference(
            focusKind!,
            focusReference);
        JsonObject focus = new()
        {
            ["state"] = "active",
            ["kind"] = focusKind,
            ["ref"] = safe.DeepClone(),
            ["reason"] = null,
        };
        if (focusKind != "text")
        {
            return new FocusFoldResult(focus, null);
        }
        string? text = StringValue(safe["text"]);
        bool samePage =
            stablePage is not null
            && SameLogicalPage(stablePage, safe, activeReading);
        if (!samePage)
        {
            return new FocusFoldResult(
                focus,
                UnknownSelection("focus-page-mismatch"));
        }
        return new FocusFoldResult(
            focus,
            new JsonObject
            {
                ["state"] = "active",
                ["text"] = text,
                ["ref"] = new JsonObject
                {
                    ["file"] = safe["file"]?.DeepClone(),
                    ["page"] = safe["page"]?.DeepClone(),
                },
                ["reason"] = null,
            });
    }

    private static bool ValidFocusKind(string? kind) =>
        kind is "text" or "image" or "card"
            or "drawing" or "region";

    private static JsonObject CopyFocusReference(
        string kind,
        JsonObject reference)
    {
        JsonObject safe = new();
        if (kind == "text")
        {
            string file = RequiredFocusString(
                reference,
                "file",
                4096);
            JsonNode? page = reference["page"]?.DeepClone();
            string text = RequiredFocusString(
                reference,
                "text",
                4000,
                multiline: true);
            if (!ValidPageIdentifier(page, allowNull: false))
            {
                throw JournalInvalid();
            }
            safe["file"] = file;
            safe["page"] = page;
            safe["text"] = text;
            return safe;
        }

        if (kind == "drawing")
        {
            string file = RequiredFocusString(
                reference,
                "file",
                4096);
            JsonNode? page = reference["page"]?.DeepClone();
            string revision = RequiredFocusString(
                reference,
                "drawingRevision",
                64);
            if (
                !ValidPageIdentifier(page, allowNull: false)
                || !IsDrawingRevision(revision)
            )
            {
                throw JournalInvalid();
            }
            safe["file"] = file;
            safe["page"] = page;
            safe["drawingRevision"] = revision;
            safe["region"] = CopyFocusRegion(reference["region"]);
            return safe;
        }

        foreach (string field in new[]
        {
            "id",
            "cid",
            "label",
            "brief",
            "url",
            "src",
            "alt",
            "text",
            "file",
        })
        {
            string? value = OptionalFocusString(
                reference,
                field,
                field is "brief" or "text" ? 4000 : 4096,
                multiline: field is "brief" or "text");
            if (value is not null)
            {
                safe[field] = value;
            }
        }
        JsonNode? anchoredPage = reference["page"]?.DeepClone();
        if (anchoredPage is not null)
        {
            if (!ValidPageIdentifier(anchoredPage, allowNull: false))
            {
                throw JournalInvalid();
            }
            safe["page"] = anchoredPage;
        }
        if (reference.ContainsKey("region"))
        {
            safe["region"] = CopyFocusRegion(reference["region"]);
        }
        bool hasLocator =
            safe["id"] is not null
            || safe["cid"] is not null
            || safe["url"] is not null
            || (
                safe["file"] is not null
                && safe["page"] is not null
            );
        if (!hasLocator)
        {
            throw JournalInvalid();
        }
        return safe;
    }

    private static JsonNode? CopyFocusRegion(JsonNode? node)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        if (node is not JsonObject region)
        {
            throw JournalInvalid();
        }
        JsonObject safe = new();
        foreach (string field in new[]
        {
            "x",
            "y",
            "w",
            "h",
            "left",
            "top",
            "right",
            "bottom",
            "width",
            "height",
        })
        {
            if (!region.ContainsKey(field))
            {
                continue;
            }
            double? value = NumericValue(region[field]);
            if (value is null)
            {
                throw JournalInvalid();
            }
            safe[field] = value.Value;
        }
        if (safe.Count == 0 && region.Count != 0)
        {
            throw JournalInvalid();
        }
        return safe;
    }

    private static string RequiredFocusString(
        JsonObject source,
        string field,
        int maximumLength,
        bool multiline = false)
    {
        string? value = OptionalFocusString(
            source,
            field,
            maximumLength,
            multiline);
        if (string.IsNullOrWhiteSpace(value))
        {
            throw JournalInvalid();
        }
        return value;
    }

    private static string? OptionalFocusString(
        JsonObject source,
        string field,
        int maximumLength,
        bool multiline = false)
    {
        string? value = StringValue(source[field]);
        if (value is null)
        {
            return null;
        }
        if (
            value.Length > maximumLength
            || (
                multiline
                    ? value.Any(character =>
                        char.IsControl(character)
                        && character is not ('\r' or '\n' or '\t'))
                    : value.Any(char.IsControl)
            )
        )
        {
            throw JournalInvalid();
        }
        return multiline
            ? NormalizeEmbedText(value)
            : value;
    }

    private static void AddCommandFailure(
        JsonObject latestEvent,
        JsonObject value)
    {
        string correlation = RequiredFocusString(
            value,
            "correlation",
            80);
        string commandId = RequiredFocusString(
            value,
            "commandId",
            80);
        string taskId = RequiredFocusString(
            value,
            "taskId",
            80);
        string error = RequiredFocusString(
            value,
            "error",
            2000,
            multiline: true);
        if (
            !NonNegativeInteger(value["step"], out long step)
            || !BooleanValue(
                value["retryable"],
                out bool retryable)
        )
        {
            throw JournalInvalid();
        }
        latestEvent["correlation"] = correlation;
        latestEvent["commandId"] = commandId;
        latestEvent["taskId"] = taskId;
        latestEvent["step"] = step;
        latestEvent["retryable"] = retryable;
        latestEvent["error"] = error;
    }

    private static JsonObject? FoldDrawingEvent(
        JsonObject value,
        JsonObject? stablePage)
    {
        if (stablePage is null)
        {
            return null;
        }
        string? file = StringValue(value["file"]);
        JsonNode? page = value["page"];
        string? state = StringValue(value["state"]);
        if (
            string.IsNullOrWhiteSpace(file)
            || page is null
            || state is not ("pending" or "stable")
        )
        {
            throw JournalInvalid();
        }
        if (
            !string.Equals(
                StringValue(stablePage["file"]),
                file,
                StringComparison.Ordinal)
            || !PageEquivalent(stablePage["page"], page)
        )
        {
            return stablePage;
        }

        JsonObject next = stablePage.DeepClone() as JsonObject
            ?? throw JournalInvalid();
        JsonObject visual = next["visual"] as JsonObject
            ?? new JsonObject
            {
                ["page_image"] = null,
                ["has_ink"] = true,
            };
        JsonObject existingDrawing =
            visual["drawing"] as JsonObject
            ?? new JsonObject();
        double eventSeconds = NumericValue(value["ts"]) ?? 0;
        double lastEditedAt =
            NumericValue(existingDrawing["lastEditedAt"])
            ?? eventSeconds;
        double freshWindow =
            NumericValue(existingDrawing["freshWindowS"])
            ?? 120.0;
        if (state == "pending")
        {
            visual["drawing"] = new JsonObject
            {
                ["contract"] = "reader-outgoing-context/1",
                ["file"] = file,
                ["page"] = stablePage["page"]?.DeepClone(),
                ["freshness"] = "recent",
                ["lastEditedAt"] = eventSeconds,
                ["freshWindowS"] = freshWindow,
                ["inProgress"] = true,
                ["stable"] = false,
                ["drawingRevision"] = null,
                ["pendingSince"] = 0.0,
                ["ref"] = null,
                ["empty"] = false,
            };
        }
        else
        {
            string? revision = StringValue(value["drawingRevision"]);
            JsonObject? reference = value["ref"] as JsonObject;
            if (
                !IsDrawingRevision(revision)
                || reference is null
                || StringValue(reference["kind"]) != "drawing"
                || !string.Equals(
                    StringValue(reference["file"]),
                    file,
                    StringComparison.Ordinal)
                || !PageEquivalent(reference["page"], page)
                || StringValue(reference["revision"]) != revision
            )
            {
                throw JournalInvalid();
            }
            visual["drawing"] = new JsonObject
            {
                ["contract"] = "reader-outgoing-context/1",
                ["file"] = file,
                ["page"] = stablePage["page"]?.DeepClone(),
                ["freshness"] =
                    DrawingFreshness(existingDrawing["freshness"]),
                ["lastEditedAt"] = lastEditedAt,
                ["freshWindowS"] = freshWindow,
                ["inProgress"] = false,
                ["stable"] = true,
                ["drawingRevision"] = revision,
                ["pendingSince"] = null,
                ["ref"] = new JsonObject
                {
                    ["kind"] = "drawing",
                    ["file"] = file,
                    ["page"] = stablePage["page"]?.DeepClone(),
                    ["revision"] = revision,
                },
                ["empty"] = false,
            };
        }
        visual["has_ink"] = true;
        next["visual"] = visual;
        return next;
    }

    private static JsonObject? CopyVisual(
        JsonNode? node,
        string file,
        JsonNode page)
    {
        if (node is null)
        {
            return null;
        }
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        JsonNode? imageNode = value["page_image"];
        string? image = StringValue(imageNode);
        if (
            imageNode is not null
            && imageNode.GetValueKind() != JsonValueKind.Null
            && (
                image is null
                || !image.StartsWith(
                    "/pdf/api/page-image?",
                    StringComparison.Ordinal)
                || image.Length > 8192
                || image.Any(char.IsControl)
            )
        )
        {
            throw JournalInvalid();
        }
        if (!BooleanValue(value["has_ink"], out bool hasInk))
        {
            throw JournalInvalid();
        }
        JsonObject? drawing = CopyDrawing(
            value["drawing"],
            file,
            page);
        if (
            (drawing is null && hasInk)
            || (
                drawing is not null
                && drawing["empty"]?.GetValue<bool?>()
                    == hasInk
            )
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["page_image"] = image,
            ["has_ink"] = hasInk,
            ["drawing"] = drawing,
        };
    }

    private static JsonObject? CopyDrawing(
        JsonNode? node,
        string file,
        JsonNode page)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        string? contract = StringValue(value["contract"]);
        string? drawingFile = StringValue(value["file"]);
        JsonNode? drawingPage = value["page"];
        string? freshness = StringValue(value["freshness"]);
        string? revision = StringValue(value["drawingRevision"]);
        if (
            contract != "reader-outgoing-context/1"
            || !string.Equals(
                drawingFile,
                file,
                StringComparison.Ordinal)
            || drawingPage is null
            || !PageEquivalent(page, drawingPage)
            || freshness is not ("none" or "recent" or "stale")
            || !BooleanValue(value["inProgress"], out bool inProgress)
            || !BooleanValue(value["stable"], out bool stable)
            || !BooleanValue(value["empty"], out bool empty)
        )
        {
            throw JournalInvalid();
        }
        double? lastEditedAt = NullableNumericValue(
            value["lastEditedAt"]);
        double? freshWindow = NumericValue(value["freshWindowS"]);
        double? pendingSince = NullableNumericValue(
            value["pendingSince"]);
        if (
            freshWindow is null
            || freshWindow is <= 0 or > 3600
            || (
                lastEditedAt is not null
                && (
                    !double.IsFinite(lastEditedAt.Value)
                    || lastEditedAt <= 0
                )
            )
            || (
                pendingSince is not null
                && (
                    !double.IsFinite(pendingSince.Value)
                    || pendingSince < 0
                )
            )
        )
        {
            throw JournalInvalid();
        }

        JsonObject? reference = null;
        if (value["ref"] is JsonObject sourceReference)
        {
            if (
                StringValue(sourceReference["kind"]) != "drawing"
                || !string.Equals(
                    StringValue(sourceReference["file"]),
                    file,
                    StringComparison.Ordinal)
                || sourceReference["page"] is not JsonNode refPage
                || !PageEquivalent(page, refPage)
                || StringValue(sourceReference["revision"])
                    != revision
            )
            {
                throw JournalInvalid();
            }
            reference = new JsonObject
            {
                ["kind"] = "drawing",
                ["file"] = file,
                ["page"] = page.DeepClone(),
                ["revision"] = revision,
            };
        }
        else if (
            value["ref"] is not null
            && value["ref"]!.GetValueKind() != JsonValueKind.Null
        )
        {
            throw JournalInvalid();
        }

        if (
            empty != (freshness == "none")
            || (empty && (
                inProgress
                || stable
                || revision is not null
                || reference is not null
                || lastEditedAt is not null
                || pendingSince is not null
            ))
            || (inProgress && (empty || stable))
            || (stable && (
                !IsDrawingRevision(revision)
                || reference is null
                || inProgress
                || pendingSince is not null
            ))
            || (!stable && (
                revision is not null
                || reference is not null
            ))
            || (!empty && !stable && pendingSince is null)
            || (!empty && lastEditedAt is null)
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["contract"] = contract,
            ["file"] = file,
            ["page"] = page.DeepClone(),
            ["freshness"] = freshness,
            ["lastEditedAt"] = lastEditedAt,
            ["freshWindowS"] = freshWindow,
            ["inProgress"] = inProgress,
            ["stable"] = stable,
            ["drawingRevision"] = revision,
            ["pendingSince"] = pendingSince,
            ["ref"] = reference,
            ["empty"] = empty,
        };
    }

    private static JsonObject? CopyEmbeds(JsonNode? node)
    {
        if (node is null)
        {
            return null;
        }
        if (
            node is not JsonObject value
            || !NonNegativeInteger(
                value["highlights"],
                out long highlights)
            || !NonNegativeInteger(
                value["blocks"],
                out long blocks)
            || value["unanchored"] is not JsonArray unanchored
            || unanchored.Count > 64
        )
        {
            throw JournalInvalid();
        }
        JsonArray safeUnanchored = [];
        foreach (JsonNode? itemNode in unanchored)
        {
            if (itemNode is not JsonObject item)
            {
                throw JournalInvalid();
            }
            string? reason = StringValue(item["_reason"]);
            if (reason is not (
                "empty_text"
                or "no_text"
                or "not_found_in_page_text"
                or "whitespace_mismatch"
                or "overlaps_earlier_highlight"))
            {
                throw JournalInvalid();
            }
            JsonObject safe = new()
            {
                ["_reason"] = reason,
            };
            foreach (string field in new[]
            {
                "id",
                "text",
                "color",
                "note",
                "kind",
            })
            {
                JsonNode? source = item[field];
                if (source is null)
                {
                    continue;
                }
                string? text = StringValue(source);
                bool multiline = field is "text" or "note";
                if (
                    text is null
                    || text.Length > 4000
                    || (
                        !multiline
                        && text.Any(char.IsControl)
                    )
                    || (
                        multiline
                        && text.Any(character =>
                            char.IsControl(character)
                            && character is not (
                                '\r' or '\n' or '\t'))
                    )
                )
                {
                    throw JournalInvalid();
                }
                safe[field] = multiline
                    ? NormalizeEmbedText(text)
                    : text;
            }
            safeUnanchored.Add(safe);
        }
        return new JsonObject
        {
            ["highlights"] = highlights,
            ["blocks"] = blocks,
            ["unanchored"] = safeUnanchored,
        };
    }

    private static JsonObject? CopyViewport(
        JsonNode? node,
        string kind)
    {
        if (node is null)
        {
            return null;
        }
        if (
            kind != "epub"
            || node is not JsonObject value
            || !NonNegativeInteger(value["center"], out long center)
            || !NonNegativeInteger(value["from"], out long from)
            || !NonNegativeInteger(value["to"], out long to)
            || !NonNegativeInteger(value["total"], out long total)
            || !NonNegativeInteger(value["pad"], out long pad)
            || from > center
            || center >= to
            || to > total
            || from != Math.Max(0, center - pad)
            || to != Math.Min(total, center + pad + 1)
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["center"] = center,
            ["from"] = from,
            ["to"] = to,
            ["total"] = total,
            ["pad"] = pad,
        };
    }

    private static JsonObject CopyReadingWindow(
        JsonNode? node,
        string sourceInstanceId,
        string documentKey,
        string file)
    {
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        string? contract = StringValue(value["contract"]);
        string? source = StringValue(value["sourceInstanceId"]);
        string? key = StringValue(value["documentKey"]);
        string? url = StringValue(value["url"]);
        string? title = StringValue(value["title"]);
        string? before = StringValue(value["beforeText"]);
        string? visible = StringValue(value["visibleText"]);
        string? after = StringValue(value["afterText"]);
        string? selectionState = StringValue(
            value["selectionState"]);
        string? selection = StringValue(value["selection"]);
        long? observedAt = value["observedAtEpochMs"]
            ?.GetValue<long?>();
        long? receivedAt = value["receivedAtEpochMs"]
            ?.GetValue<long?>();
        bool hasControlCorrelation = value.ContainsKey(
            "controlCorrelation");
        string? controlCorrelation = StringValue(
            value["controlCorrelation"]);
        if (
            contract != "reader-viewport/1"
            || !string.Equals(
                source,
                sourceInstanceId,
                StringComparison.Ordinal)
            || !string.Equals(
                key,
                documentKey,
                StringComparison.Ordinal)
            || !string.Equals(url, file, StringComparison.Ordinal)
            || title is { Length: > 1024 }
            || before is null or { Length: > 12_000 }
            || visible is null or { Length: > 24_000 }
            || after is null or { Length: > 12_000 }
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || selection is { Length: > 400 }
            || (selectionState == "active")
                != !string.IsNullOrWhiteSpace(selection)
            || observedAt is null or < 1
            || receivedAt is null or < 1
            || (
                hasControlCorrelation
                && (
                    string.IsNullOrEmpty(controlCorrelation)
                    || controlCorrelation.Length > 160
                    || !DirectBridgeContract.IsSafeId(
                        controlCorrelation)
                )
            )
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["contract"] = contract,
            ["sourceInstanceId"] = source,
            ["documentKey"] = key,
            ["url"] = url,
            ["title"] = title,
            ["beforeText"] = before,
            ["visibleText"] = visible,
            ["afterText"] = after,
            ["selectionState"] = selectionState,
            ["selection"] = selectionState == "active"
                ? selection
                : null,
            ["observedAtEpochMs"] = observedAt,
            ["receivedAtEpochMs"] = receivedAt,
        };
        if (controlCorrelation is not null)
        {
            restored["controlCorrelation"] = controlCorrelation;
        }
        return restored;
    }

    private static bool BooleanValue(
        JsonNode? node,
        out bool value)
    {
        if (
            node is JsonValue jsonValue
            && jsonValue.TryGetValue(out value)
        )
        {
            return true;
        }
        value = false;
        return false;
    }

    private static bool NonNegativeInteger(
        JsonNode? node,
        out long value)
    {
        if (
            node is JsonValue jsonValue
            && jsonValue.TryGetValue(out value)
            && value >= 0
        )
        {
            return true;
        }
        value = 0;
        return false;
    }

    private static bool ValidPageIdentifier(
        JsonNode? node,
        bool allowNull)
    {
        if (node is null)
        {
            return allowNull;
        }
        if (node is not JsonValue value)
        {
            return false;
        }
        if (value.TryGetValue(out long number))
        {
            return number is >= 0 and <= 9_007_199_254_740_991;
        }
        return value.TryGetValue(out string? text)
            && text is { Length: >= 1 and <= 256 }
            && !text.Any(char.IsControl);
    }

    private static string NormalizeEmbedText(string value) =>
        value.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace("\r", "\n", StringComparison.Ordinal)
            .Replace("\t", "    ", StringComparison.Ordinal);

    private static double? NumericValue(JsonNode? node)
    {
        if (node is not JsonValue value)
        {
            return null;
        }
        if (
            value.TryGetValue(out double number)
            && double.IsFinite(number)
        )
        {
            return number;
        }
        return null;
    }

    private static double? NullableNumericValue(JsonNode? node)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        double? value = NumericValue(node);
        return value ?? throw JournalInvalid();
    }

    private static bool PageEquivalent(
        JsonNode? left,
        JsonNode? right)
    {
        if (JsonNode.DeepEquals(left, right))
        {
            return true;
        }
        return left is not null
            && right is not null
            && string.Equals(
                left.ToJsonString().Trim('"'),
                right.ToJsonString().Trim('"'),
                StringComparison.Ordinal);
    }

    private static bool IsDrawingRevision(string? value) =>
        value is { Length: 19 }
        && value.StartsWith("dr_", StringComparison.Ordinal)
        && value[3..].All(character =>
            character is >= '0' and <= '9'
            or >= 'a' and <= 'f');

    private static string DrawingFreshness(JsonNode? node)
    {
        string? value = StringValue(node);
        return value is "recent" or "stale"
            ? value
            : "recent";
    }

    private JsonObject BuildSnapshot()
    {
        string contextStatus = _stablePage is null
            ? "pending"
            : "ready";
        JsonObject? effectivePage = _stablePage?.DeepClone()
            as JsonObject;
        if (_activeReading is not null)
        {
            if (
                _stablePage is null
                || !SamePage(_stablePage, _activeReading)
                || WebSourceDiffers(_stablePage, _activeReading)
            )
            {
                contextStatus = "pending";
                effectivePage = PendingPage(_activeReading);
            }
        }
        if (
            contextStatus == "ready"
            && effectivePage is not null
            && _activeReading is not null
            && SamePageEquivalent(effectivePage, _activeReading)
        )
        {
            // page.context carries content, while active-reading carries the
            // live source identity used by on-demand visual capture.  A local
            // PDF/EPUB page does not send a separate viewport packet, so the
            // two facts must be joined here once their canonical page matches.
            // Without this, the snapshot is ready and has ink, but visual
            // tools reject it because currentPage has no sourceInstanceId.
            if (
                StringValue(_activeReading["sourceInstanceId"])
                    is string sourceInstanceId
            )
            {
                effectivePage["sourceInstanceId"] = sourceInstanceId;
            }
            if (
                _activeReading["selectionRegions"]
                    is JsonObject regions
            )
            {
                effectivePage["selectionRegions"] = regions.DeepClone();
            }
        }
        return new JsonObject
        {
            ["schema"] = SnapshotContract,
            // revision is monotonic only for one running writer.  A stable
            // producer identity lets long-lived MCP clients distinguish a
            // service restart from an out-of-order write.
            ["producerInstanceId"] = _producerInstanceId,
            ["revision"] = _revision,
            ["updatedAtUtc"] = _utcNow()
                .ToString("O"),
            ["latestEvent"] = _latestEvent?.DeepClone(),
            ["activeReading"] = _activeReading?.DeepClone(),
            ["contextStatus"] = contextStatus,
            ["currentPage"] = effectivePage,
            ["selection"] = _selection.DeepClone(),
            ["focus"] = _focus.DeepClone(),
        };
    }

    private async Task PersistAsync(CancellationToken cancellationToken)
    {
        JsonObject snapshot = BuildSnapshot();
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(
            snapshot,
            DirectBridgeContract.JsonOptions);
        if (payload.Length is < 1 or > MaximumSnapshotBytes)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_TOO_LARGE",
                "Windows 本地 Reader 快照超过大小上限");
        }
        string? directory = System.IO.Path.GetDirectoryName(_statePath);
        if (directory is null)
        {
            throw SnapshotWriteFailed();
        }
        string temporaryPath = _statePath + ".tmp";
        try
        {
            Directory.CreateDirectory(directory);
            await File.WriteAllTextAsync(
                temporaryPath,
                Utf8WithoutBom.GetString(payload),
                Utf8WithoutBom,
                cancellationToken).ConfigureAwait(false);
            File.Move(temporaryPath, _statePath, overwrite: true);
            await DirectSnapshotMarkdown.WriteBestEffortAsync(
                snapshot,
                _statePath,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
        )
        {
            throw SnapshotWriteFailed(exception);
        }
        finally
        {
            Array.Clear(payload);
            try
            {
                File.Delete(temporaryPath);
            }
            catch
            {
            }
        }
    }

    private AdapterState CaptureState() =>
        new(
            _revision,
            _stablePage?.DeepClone() as JsonObject,
            _activeReading?.DeepClone() as JsonObject,
            _selection.DeepClone() as JsonObject
                ?? UnknownSelection("snapshot-checkpoint-failed"),
            _focus.DeepClone() as JsonObject
                ?? UnknownFocus("snapshot-checkpoint-failed"),
            _latestEvent?.DeepClone() as JsonObject,
            _recentEventOrder.ToArray());

    private void RestoreState(AdapterState state)
    {
        _revision = state.Revision;
        _stablePage = state.StablePage?.DeepClone() as JsonObject;
        _activeReading =
            state.ActiveReading?.DeepClone() as JsonObject;
        _selection = state.Selection.DeepClone() as JsonObject
            ?? UnknownSelection("snapshot-rollback-failed");
        _focus = state.Focus.DeepClone() as JsonObject
            ?? UnknownFocus("snapshot-rollback-failed");
        _latestEvent =
            state.LatestEvent?.DeepClone() as JsonObject;
        _recentEventOrder.Clear();
        _recentEventIds.Clear();
        foreach (string eventId in state.RecentEventOrder)
        {
            _recentEventOrder.Enqueue(eventId);
            _recentEventIds.Add(eventId);
        }
    }

    private void LoadExistingState()
    {
        try
        {
            if (!File.Exists(_statePath))
            {
                return;
            }
            FileInfo info = new(_statePath);
            if (info.Length is <= 0 or > MaximumSnapshotBytes)
            {
                return;
            }
            JsonObject? root = JsonNode.Parse(
                File.ReadAllText(_statePath, Encoding.UTF8)) as JsonObject;
            if (
                root?["schema"]?.GetValue<string>() != SnapshotContract
                || root["revision"]?.GetValue<long?>() is not long revision
                || revision < 0
            )
            {
                return;
            }
            _revision = revision;
            _activeReading = RestoreActiveReading(
                root["activeReading"] as JsonObject);
            JsonObject? currentPage = root["currentPage"]
                as JsonObject;
            if (
                currentPage?["stable"]?.GetValue<bool?>() == true
            )
            {
                _stablePage = RestoreStablePage(
                    currentPage,
                    _activeReading);
            }
            _selection = RestoreSelection(
                root["selection"] as JsonObject);
            _focus = RestoreFocus(
                root["focus"] as JsonObject);
            _latestEvent = root["latestEvent"]?.DeepClone()
                as JsonObject;
        }
        catch
        {
            // A damaged prior cache is not authoritative. Keep it untouched
            // until the first validated update atomically replaces it.
            _revision = 0;
            _stablePage = null;
            _activeReading = null;
            _selection = UnknownSelection("snapshot-invalid");
            _focus = UnknownFocus("snapshot-invalid");
            _latestEvent = null;
        }
    }

    private void RememberEvent(string eventId)
    {
        if (!_recentEventIds.Add(eventId))
        {
            return;
        }
        _recentEventOrder.Enqueue(eventId);
        while (_recentEventOrder.Count > RecentEventLimit)
        {
            _recentEventIds.Remove(_recentEventOrder.Dequeue());
        }
    }

    private static JsonObject PendingPage(JsonObject active) =>
        new()
        {
            ["kind"] = active["kind"]?.DeepClone(),
            ["file"] = active["file"]?.DeepClone(),
            ["title"] = active["title"]?.DeepClone(),
            ["page"] = active["page"]?.DeepClone(),
            ["stable"] = false,
            ["text"] = "",
            ["textAvailable"] = false,
        };

    private static JsonObject? RestoreActiveReading(
        JsonObject? source)
    {
        if (source is null)
        {
            return null;
        }
        string? kind = StringValue(source["kind"]);
        string? file = StringValue(source["file"]);
        JsonNode? page = source["page"]?.DeepClone();
        if (
            kind is not ("pdf" or "epub" or "html" or "web")
            || string.IsNullOrWhiteSpace(file)
            || !source.ContainsKey("page")
            || !ValidPageIdentifier(page, allowNull: true)
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(source["title"]),
            ["page"] = page,
            ["fresh"] = source["fresh"]?.GetValue<bool?>(),
            ["ageSec"] = source["ageSec"]?.GetValue<long?>(),
            ["observedAtEpochMs"] =
                source["observedAtEpochMs"]?.GetValue<long?>(),
            ["receivedAtEpochMs"] =
                source["receivedAtEpochMs"]?.GetValue<long?>(),
        };
        string? sourceInstanceId = StringValue(
            source["sourceInstanceId"]);
        if (sourceInstanceId is not null)
        {
            if (
                sourceInstanceId.Length > 160
                || !DirectBridgeContract.IsSafeId(sourceInstanceId)
            )
            {
                throw JournalInvalid();
            }
            restored["sourceInstanceId"] = sourceInstanceId;
        }
        bool hasViewFile = source.ContainsKey("viewFile");
        bool hasViewPage = source.ContainsKey("viewPage");
        if (hasViewFile != hasViewPage)
        {
            throw JournalInvalid();
        }
        if (hasViewFile)
        {
            string? viewFile = StringValue(source["viewFile"]);
            JsonNode? viewPage = source["viewPage"]?.DeepClone();
            if (
                kind == "web"
                || viewFile is null
                || !viewFile.StartsWith("vbook:", StringComparison.Ordinal)
                || viewFile.Length is < 7 or > 4096
                || viewFile.Any(char.IsControl)
                || !ValidPageIdentifier(viewPage, allowNull: false)
            )
            {
                throw JournalInvalid();
            }
            restored["viewFile"] = viewFile;
            restored["viewPage"] = viewPage;
        }
        JsonObject? selectionRegions = RestoreSelectionRegions(
            source["selectionRegions"]);
        if (selectionRegions is not null)
        {
            restored["selectionRegions"] = selectionRegions;
        }
        return restored;
    }

    private static JsonObject? RestoreSelectionRegions(JsonNode? source)
    {
        if (source is null)
        {
            return null;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                source.ToJsonString(DirectBridgeContract.JsonOptions));
            JsonElement validated = ValidateSelectionRegions(
                document.RootElement);
            return JsonNode.Parse(validated.GetRawText()) as JsonObject
                ?? throw JournalInvalid();
        }
        catch (DirectProtocolException)
        {
            throw JournalInvalid();
        }
        catch (JsonException)
        {
            throw JournalInvalid();
        }
    }

    private static JsonObject RestoreStablePage(
        JsonObject source,
        JsonObject? active)
    {
        string? file = StringValue(source["file"]);
        string? kind = StringValue(source["kind"])
            ?? StringValue(active?["kind"])
            ?? InferKind(file);
        JsonNode? page = source["page"]?.DeepClone();
        if (
            string.IsNullOrWhiteSpace(file)
            || kind is not ("pdf" or "epub" or "html" or "web")
            || !ValidPageIdentifier(page, allowNull: false)
            || source["stable"]?.GetValue<bool?>() != true
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(source["title"]),
            ["page"] = page,
            ["stable"] = true,
            ["reason"] = StringValue(source["reason"]),
            ["text"] = StringValue(source["text"]) ?? "",
            ["textAvailable"] =
                source["textAvailable"]?.GetValue<bool?>(),
            ["textSource"] = StringValue(source["textSource"]),
            ["fallbackReason"] =
                StringValue(source["fallbackReason"]),
            ["truncated"] =
                source["truncated"]?.GetValue<bool?>(),
        };
        JsonObject? visual = CopyVisual(
            source["visual"],
            file,
            page!);
        if (visual is not null)
        {
            restored["visual"] = visual;
        }
        JsonObject? embeds = CopyEmbeds(source["embeds"]);
        if (embeds is not null)
        {
            restored["embeds"] = embeds;
        }
        JsonObject? viewport = CopyViewport(
            source["viewport"],
            kind);
        if (viewport is not null)
        {
            restored["viewport"] = viewport;
        }
        string? sourceInstanceId = StringValue(
            source["sourceInstanceId"]);
        string? documentKey = StringValue(source["documentKey"]);
        if (sourceInstanceId is not null || documentKey is not null)
        {
            if (
                string.IsNullOrEmpty(sourceInstanceId)
                || sourceInstanceId.Length > 160
                || !DirectBridgeContract.IsSafeId(sourceInstanceId)
                || string.IsNullOrEmpty(documentKey)
                || documentKey.Length > 4096
                || documentKey.Any(char.IsControl)
                || !Uri.TryCreate(
                    documentKey,
                    UriKind.Absolute,
                    out Uri? documentUri)
                || documentUri.Scheme is not ("http" or "https")
            )
            {
                throw JournalInvalid();
            }
            restored["sourceInstanceId"] = sourceInstanceId;
            restored["documentKey"] = documentKey;
            restored["readingWindow"] = CopyReadingWindow(
                source["readingWindow"],
                sourceInstanceId,
                documentKey,
                file);
        }
        return restored;
    }

    private static JsonObject RestoreSelection(JsonObject? source)
    {
        string? state = StringValue(source?["state"]);
        if (state is not ("active" or "cleared" or "unknown"))
        {
            return UnknownSelection("snapshot-invalid-selection");
        }
        string? text = StringValue(source?["text"]);
        if (
            state == "active"
            && string.IsNullOrWhiteSpace(text)
        )
        {
            return UnknownSelection("snapshot-invalid-selection");
        }
        return new JsonObject
        {
            ["state"] = state,
            ["text"] = state == "active" ? text : null,
            ["ref"] = source?["ref"]?.DeepClone(),
            ["reason"] = StringValue(source?["reason"]),
        };
    }

    private static JsonObject RestoreFocus(JsonObject? source)
    {
        string? state = StringValue(source?["state"]);
        if (state == "unknown")
        {
            return UnknownFocus(
                StringValue(source?["reason"])
                    ?? "snapshot-invalid-focus");
        }
        if (state is not ("active" or "cleared"))
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
        string? kind = StringValue(source?["kind"]);
        if (
            !ValidFocusKind(kind)
            || source?["ref"] is not JsonObject reference
        )
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
        try
        {
            return new JsonObject
            {
                ["state"] = state,
                ["kind"] = kind,
                ["ref"] = CopyFocusReference(kind!, reference),
                ["reason"] = StringValue(source?["reason"]),
            };
        }
        catch (DirectProtocolException)
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
    }

    private static string? InferKind(string? file)
    {
        string extension = System.IO.Path.GetExtension(file ?? "");
        return extension.ToLowerInvariant() switch
        {
            ".pdf" => "pdf",
            ".epub" => "epub",
            ".html" or ".htm" or ".md" => "html",
            _ => null,
        };
    }

    private static JsonObject ClearedSelection(string reason) =>
        new()
        {
            ["state"] = "cleared",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject UnknownSelection(string reason) =>
        new()
        {
            ["state"] = "unknown",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject UnknownFocus(string reason) =>
        new()
        {
            ["state"] = "unknown",
            ["kind"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject ActiveSelection(
        string text,
        JsonObject active) =>
        new()
        {
            ["state"] = "active",
            ["text"] = text,
            ["ref"] = new JsonObject
            {
                ["file"] = active["file"]?.DeepClone(),
                ["page"] = active["page"]?.DeepClone(),
            },
            ["reason"] = null,
        };

    private static bool SamePage(JsonObject left, JsonObject right) =>
        string.Equals(
            StringValue(left["file"]),
            StringValue(right["file"]),
            StringComparison.Ordinal)
        && JsonNode.DeepEquals(left["page"], right["page"]);

    // Same page by equivalence rather than by raw JSON shape.
    //
    // SamePage uses DeepEquals, so 12 and "12" read as different pages even
    // though the readers treat them as one. PageEquivalent already knows the
    // difference; these comparisons need it too.
    private static bool SamePageEquivalent(
        JsonObject left,
        JsonObject right) =>
        string.Equals(
            StringValue(left["file"]),
            StringValue(right["file"]),
            StringComparison.Ordinal)
        && PageEquivalent(left["page"], right["page"]);

    // Content and location updates are allowed to omit live-source metadata.
    // On the same canonical page, omission means "unchanged", not "clear".
    // Dropping the source here leaves a readable snapshot with no route for
    // highlighter, browser-control, or on-demand visual commands.
    private static void PreserveActiveReadingContinuity(
        JsonObject prior,
        JsonObject next)
    {
        if (!SamePageEquivalent(prior, next))
        {
            return;
        }
        string? priorSource = StringValue(
            prior["sourceInstanceId"]);
        string? nextSource = StringValue(
            next["sourceInstanceId"]);
        if (
            nextSource is not null
            && !string.Equals(
                nextSource,
                priorSource,
                StringComparison.Ordinal)
        )
        {
            return;
        }
        if (!HasViewBinding(next) && HasViewBinding(prior))
        {
            next["viewFile"] = prior["viewFile"]?.DeepClone();
            next["viewPage"] = prior["viewPage"]?.DeepClone();
        }
        if (nextSource is null && priorSource is not null)
        {
            next["sourceInstanceId"] = priorSource;
        }
        if (
            next["selectionRegions"] is null
            && prior["selectionRegions"] is JsonObject priorRegions
        )
        {
            next["selectionRegions"] = priorRegions.DeepClone();
        }
    }

    private static bool WebSourceDiffers(
        JsonObject stablePage,
        JsonObject activeReading)
    {
        if (
            !string.Equals(
                StringValue(activeReading["kind"]),
                "web",
                StringComparison.Ordinal)
            || StringValue(activeReading["sourceInstanceId"])
                is not string activeSource
        )
        {
            return false;
        }
        return !string.Equals(
            StringValue(stablePage["sourceInstanceId"]),
            activeSource,
            StringComparison.Ordinal);
    }

    // Whether this reading carries a usable volume view.
    //
    // Both halves must be present and well formed. A half-written binding is
    // worse than none: it would claim a mapping the other half cannot honour.
    private static bool HasViewBinding(JsonObject value) =>
        StringValue(value["viewFile"]) is string viewFile
        && viewFile.StartsWith("vbook:", StringComparison.Ordinal)
        && ValidPageIdentifier(value["viewPage"], allowNull: false);

    // Identity of an active reading, view binding included.
    //
    // Two readings on the same canonical page are still different readings if
    // one is being viewed through a volume and the other is not, or through a
    // different volume page. Comparing only the canonical page would call them
    // equal and the selection would survive a move it should not have.
    private static bool SameActiveReadingIdentity(
        JsonObject left,
        JsonObject right)
    {
        if (!SamePageEquivalent(left, right))
        {
            return false;
        }
        bool leftHasView = HasViewBinding(left);
        bool rightHasView = HasViewBinding(right);
        if (leftHasView != rightHasView)
        {
            return false;
        }
        return !leftHasView
            || (
                string.Equals(
                    StringValue(left["viewFile"]),
                    StringValue(right["viewFile"]),
                    StringComparison.Ordinal)
                && PageEquivalent(
                    left["viewPage"],
                    right["viewPage"])
            );
    }

    // Whether a reference names the same page as the canonical one, allowing
    // for the reference having been written in volume coordinates.
    //
    // A selection made while reading a merged volume records the volume's file
    // and page. Compared directly against the canonical member it never
    // matches, and the selection gets discarded on every context event. The
    // active reading holds the mapping between the two, so it is consulted
    // before declaring a mismatch.
    private static bool SameLogicalPage(
        JsonObject canonicalPage,
        JsonObject reference,
        JsonObject? activeReading)
    {
        if (SamePageEquivalent(canonicalPage, reference))
        {
            return true;
        }
        return activeReading is not null
            && HasViewBinding(activeReading)
            && SamePageEquivalent(canonicalPage, activeReading)
            && string.Equals(
                StringValue(activeReading["viewFile"]),
                StringValue(reference["file"]),
                StringComparison.Ordinal)
            && PageEquivalent(
                activeReading["viewPage"],
                reference["page"]);
    }

    private static string? StringValue(JsonNode? value)
    {
        if (value is null)
        {
            return null;
        }
        try
        {
            return value.GetValue<string>();
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    private static long? EpochSecondsToMilliseconds(JsonNode? value)
    {
        try
        {
            return checked(value?.GetValue<long>() * 1000);
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or OverflowException
        )
        {
            return null;
        }
    }

    private static void ValidateRequestIdentity(
        string requestId,
        string sessionId)
    {
        if (
            !DirectBridgeContract.IsSafeId(requestId)
            || DirectPcmFrameCodec.ParseSessionId(sessionId).Length != 16
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_SCHEMA_INVALID",
                "Windows 本地 Reader 快照请求标识无效");
        }
    }

    private static DirectProtocolException ActiveReadingInvalid(
        Exception? inner = null) =>
        new(
            "BW_READER_ACTIVE_READING_SCHEMA_INVALID",
            "Reader active-reading 更新无效",
            retryable: false,
            innerException: inner);

    private static DirectProtocolException ViewportInvalid(
        Exception? inner = null) =>
        new(
            "BW_READER_VIEWPORT_SCHEMA_INVALID",
            "Reader 当前视口更新无效",
            retryable: false,
            innerException: inner);

    private static DirectProtocolException JournalInvalid() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_EVENT_INVALID",
            "Reader outgoing context 无法折叠为本地快照");

    private static DirectProtocolException SnapshotWriteFailed(
        Exception? inner = null) =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_WRITE_FAILED",
            "Windows 本地 Reader 快照写入失败",
            retryable: true,
            innerException: inner);
}
