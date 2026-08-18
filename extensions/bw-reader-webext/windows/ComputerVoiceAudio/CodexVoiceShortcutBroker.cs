using System.Globalization;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace BwReader.ComputerVoiceAudio;

internal interface ICodexVoiceShortcutBrokerTransport
{
    string Exchange(string requestLine);
}

internal sealed class NamedPipeCodexVoiceShortcutBrokerTransport
    : ICodexVoiceShortcutBrokerTransport
{
    internal const string PipeName =
        "bw-reader-codex-voice-shortcut-v1";
    internal static readonly TimeSpan ExchangeTimeout =
        TimeSpan.FromSeconds(2);
    internal const int MaximumReceiptCharacters = 4096;

    public string Exchange(string requestLine)
    {
        if (
            string.IsNullOrWhiteSpace(requestLine)
            || requestLine.Contains('\n', StringComparison.Ordinal)
            || requestLine.Contains('\r', StringComparison.Ordinal)
        )
        {
            throw BrokerFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_REQUEST_INVALID",
                "快捷键代理请求无效");
        }

        using CancellationTokenSource timeout = new(ExchangeTimeout);
        try
        {
            using NamedPipeClientStream pipe = new(
                ".",
                PipeName,
                PipeDirection.InOut,
                PipeOptions.Asynchronous);
            pipe.ConnectAsync(timeout.Token).GetAwaiter().GetResult();

            using StreamWriter writer = new(
                pipe,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                bufferSize: 1024,
                leaveOpen: true)
            {
                AutoFlush = true,
                NewLine = "\n",
            };
            writer.WriteLineAsync(
                    requestLine.AsMemory(),
                    timeout.Token)
                .GetAwaiter()
                .GetResult();

            using StreamReader reader = new(
                pipe,
                new UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false,
                    throwOnInvalidBytes: true),
                detectEncodingFromByteOrderMarks: false,
                bufferSize: 1024,
                leaveOpen: true);
            string? receipt = reader.ReadLineAsync(timeout.Token)
                .AsTask()
                .GetAwaiter()
                .GetResult();
            if (
                string.IsNullOrWhiteSpace(receipt)
                || receipt.Length > MaximumReceiptCharacters
            )
            {
                throw BrokerFailure(
                    "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_RECEIPT_INVALID",
                    "快捷键代理未返回有效回执");
            }
            return receipt;
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (OperationCanceledException exception)
        {
            throw BrokerFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_TIMEOUT",
                "等待快捷键代理超时",
                exception);
        }
        catch (TimeoutException exception)
        {
            throw BrokerFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_TIMEOUT",
                "等待快捷键代理超时",
                exception);
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ObjectDisposedException
        )
        {
            throw BrokerFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_UNAVAILABLE",
                "Windows 交互快捷键代理不可用",
                exception);
        }
    }

    private static DirectProtocolException BrokerFailure(
        string code,
        string message,
        Exception? innerException = null) =>
        new(
            code,
            message,
            retryable: true,
            innerException);
}

internal static partial class CodexVoiceShortcutBrokerContract
{
    internal const string Contract = "bw-codex-voice-shortcut/1";
    internal const string RequestType = "toggle";
    internal const string ReceiptType = "receipt";
    internal const string Shortcut = "F24";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false,
    };

    [GeneratedRegex(
        "^shortcut-[A-Za-z0-9_-]{22}$",
        RegexOptions.CultureInvariant)]
    private static partial Regex RequestIdPattern();

    [GeneratedRegex(
        "^BW_[A-Z0-9_]{1,120}$",
        RegexOptions.CultureInvariant)]
    private static partial Regex FailureCodePattern();

    internal static string NewRequestId() =>
        "shortcut-"
        + DirectBase64Url.Encode(RandomNumberGenerator.GetBytes(16));

    internal static string SerializeRequest(
        string requestId,
        CodexAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(target);
        if (!RequestIdPattern().IsMatch(requestId))
        {
            throw new ArgumentException(
                "快捷键代理 requestId 无效",
                nameof(requestId));
        }
        // windowHandle 允许为 0：Codex 常驻托盘时一个可见窗口都没有（本机实测 16 个
        // 顶层窗口全隐藏）。它在这条路上只是**签名的一部分**用来幂等去重，
        // 而 rootProcessId + startTime 已经足以唯一 —— F24 是全局盲发的，
        // 从来不靠这个句柄定位。把它当必填就等于要求 Codex 必须开着窗口。
        if (
            target.RootProcessId == 0
            || target.RootProcessStartFileTimeUtc <= 0
            || target.WindowHandle < 0
        )
        {
            throw new ArgumentException(
                "Codex 快捷键目标无效",
                nameof(target));
        }

        string startTimeUtc;
        try
        {
            startTimeUtc = DateTime
                .FromFileTimeUtc(target.RootProcessStartFileTimeUtc)
                .ToString("O", CultureInfo.InvariantCulture);
        }
        catch (ArgumentOutOfRangeException exception)
        {
            throw new ArgumentException(
                "Codex 快捷键目标启动时间无效",
                nameof(target),
                exception);
        }

        return JsonSerializer.Serialize(
            new
            {
                contract = Contract,
                type = RequestType,
                requestId,
                rootProcessId = target.RootProcessId,
                rootProcessStartTimeUtc = startTimeUtc,
                windowHandle = target.WindowHandle.ToInt64(),
            },
            JsonOptions);
    }

    internal static void RequireSuccessfulReceipt(
        string receiptJson,
        string expectedRequestId)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(receiptJson);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw InvalidReceipt();
            }
            bool ok = RequireBoolean(root, "ok");
            string[] expectedNames = ok
                ? ["contract", "type", "requestId", "ok"]
                : ["contract", "type", "requestId", "ok", "code"];
            string[] actualNames = root.EnumerateObject()
                .Select(property => property.Name)
                .Order(StringComparer.Ordinal)
                .ToArray();
            if (!actualNames.SequenceEqual(
                    expectedNames.Order(StringComparer.Ordinal),
                    StringComparer.Ordinal)
                || RequireString(root, "contract") != Contract
                || RequireString(root, "type") != ReceiptType
                || RequireString(root, "requestId")
                    != expectedRequestId
            )
            {
                throw InvalidReceipt();
            }
            if (ok)
            {
                return;
            }
            string code = RequireString(root, "code");
            if (!FailureCodePattern().IsMatch(code))
            {
                throw InvalidReceipt();
            }
            throw new DirectProtocolException(
                code,
                "Windows 交互快捷键代理拒绝请求",
                retryable: true);
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or KeyNotFoundException
        )
        {
            throw InvalidReceipt(exception);
        }
    }

    private static string RequireString(
        JsonElement root,
        string name)
    {
        if (
            !root.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
        )
        {
            throw new InvalidOperationException(
                $"缺少字符串字段 {name}");
        }
        return value.GetString() ?? "";
    }

    private static bool RequireBoolean(
        JsonElement root,
        string name)
    {
        if (!root.TryGetProperty(name, out JsonElement value))
        {
            throw new KeyNotFoundException(name);
        }
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new InvalidOperationException(
                $"字段 {name} 必须是布尔值"),
        };
    }

    private static DirectProtocolException InvalidReceipt(
        Exception? innerException = null) =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_RECEIPT_INVALID",
            "快捷键代理回执无效",
            retryable: true,
            innerException);
}
