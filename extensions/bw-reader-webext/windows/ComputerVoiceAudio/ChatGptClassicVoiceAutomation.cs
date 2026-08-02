using System.Runtime.InteropServices;
using System.Windows.Automation;

namespace BwReader.ComputerVoiceAudio;

internal interface IChatGptClassicVoiceButtonInvoker
{
    void Invoke(CodexAppTarget target, string accessibleName);
}

internal sealed class WindowsChatGptClassicVoiceButtonInvoker
    : IChatGptClassicVoiceButtonInvoker
{
    public void Invoke(CodexAppTarget target, string accessibleName)
    {
        ArgumentNullException.ThrowIfNull(target);
        WindowsCodexAppProbe.RequireCurrentReadyTarget(target);
        if (
            target.AppKind != DirectAppTargets.ChatGptClassic
            || target.WindowHandle == 0
            || string.IsNullOrWhiteSpace(accessibleName)
            || GetWindowThreadProcessId(
                target.WindowHandle,
                out uint windowProcessId) == 0
            || !target.ProcessTree.Contains(windowProcessId)
        )
        {
            throw TargetInvalid();
        }

        try
        {
            AutomationElement root =
                AutomationElement.FromHandle(target.WindowHandle);
            Condition condition = new AndCondition(
                new PropertyCondition(
                    AutomationElement.ControlTypeProperty,
                    ControlType.Button),
                new PropertyCondition(
                    AutomationElement.NameProperty,
                    accessibleName));
            AutomationElement[] candidates = root
                .FindAll(TreeScope.Descendants, condition)
                .Cast<AutomationElement>()
                .Where(element =>
                    element.Current.IsEnabled
                    && !element.Current.IsOffscreen)
                .ToArray();
            if (candidates.Length != 1)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_CLASSIC_VOICE_BUTTON_AMBIGUOUS",
                    "GPT Classic 语音按钮不是唯一可用目标",
                    retryable: true);
            }
            if (
                !candidates[0].TryGetCurrentPattern(
                    InvokePattern.Pattern,
                    out object? pattern)
                || pattern is not InvokePattern invokePattern
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_CLASSIC_VOICE_BUTTON_NOT_INVOKABLE",
                    "GPT Classic 语音按钮不支持安全调用",
                    retryable: true);
            }
            invokePattern.Invoke();
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is ElementNotAvailableException
            or InvalidOperationException
            or COMException)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CLASSIC_VOICE_UIA_FAILED",
                "GPT Classic 语音按钮调用失败",
                retryable: true,
                innerException: exception);
        }
    }

    private static DirectProtocolException TargetInvalid() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_CLASSIC_VOICE_TARGET_INVALID",
            "GPT Classic 窗口目标校验失败",
            retryable: true);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(
        nint windowHandle,
        out uint processId);
}

internal sealed class WindowsChatGptClassicVoiceShortcutSender
    : ICodexVoiceShortcutSender
{
    internal const string StartButtonName = "启动语音功能";
    internal const string StopButtonName = "结束语音功能";

    private readonly IChatGptClassicVoiceButtonInvoker _invoker;

    internal WindowsChatGptClassicVoiceShortcutSender()
        : this(new WindowsChatGptClassicVoiceButtonInvoker())
    {
    }

    internal WindowsChatGptClassicVoiceShortcutSender(
        IChatGptClassicVoiceButtonInvoker invoker)
    {
        ArgumentNullException.ThrowIfNull(invoker);
        _invoker = invoker;
    }

    public void Send(
        CodexAppTarget target,
        DirectVoiceCommand command)
    {
        if (target.AppKind != DirectAppTargets.ChatGptClassic)
        {
            throw TargetInvalid();
        }
        WindowsCodexAppProbe.RequireCurrentReadyTarget(target);
        _invoker.Invoke(
            target,
            command switch
            {
                DirectVoiceCommand.Start => StartButtonName,
                DirectVoiceCommand.Stop => StopButtonName,
                _ => throw new ArgumentOutOfRangeException(
                    nameof(command)),
            });
    }

    private static DirectProtocolException TargetInvalid() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_CLASSIC_VOICE_TARGET_INVALID",
            "GPT Classic 窗口目标校验失败",
            retryable: true);
}
