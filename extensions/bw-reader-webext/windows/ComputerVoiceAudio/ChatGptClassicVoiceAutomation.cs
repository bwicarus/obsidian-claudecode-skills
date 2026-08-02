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
            static AutomationElement[] FindUsable(
                AutomationElement scope,
                Condition query) =>
                scope
                    .FindAll(TreeScope.Descendants, query)
                    .Cast<AutomationElement>()
                    .Where(element =>
                        element.Current.IsEnabled
                        && !element.Current.IsOffscreen)
                    .ToArray();

            // A Classic the bridge just launched has a window before it has a
            // rendered UI, so the button genuinely does not exist yet for the
            // first second or two -- which is why a cold start used to fail and
            // the same click succeeded on the second attempt. Poll rather than
            // give up on the first look.
            //
            // A non-empty composer is the other reason the button can be
            // missing: Classic gives voice and send the same slot, so leftover
            // text (an injected page context that was never submitted, say)
            // replaces "启动语音功能" / "结束语音功能" with send and locks
            // voice both on and off. That one is cleared once, up front,
            // instead of on every pass.
            AutomationElement[] candidates = FindUsable(root, condition);
            if (candidates.Length == 0 && TryClearComposer(root))
            {
                candidates = FindUsable(root, condition);
            }
            for (
                int attempt = 1;
                candidates.Length == 0 && attempt < ButtonAppearAttempts;
                attempt++)
            {
                Thread.Sleep(ButtonAppearDelayMilliseconds);
                candidates = FindUsable(root, condition);
            }
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

    private const string ComposerAutomationId = "prompt-textarea";
    // Roughly four seconds: enough for a freshly launched Classic to render
    // its composer area, short enough that a genuinely absent button still
    // fails while the caller's own start timeout has room left.
    private const int ButtonAppearAttempts = 14;
    private const int ButtonAppearDelayMilliseconds = 300;
    private static readonly TimeSpan ComposerClearSettleDelay =
        TimeSpan.FromMilliseconds(350);

    /// <summary>
    /// Empty the composer so the voice button reclaims its slot.
    /// </summary>
    /// <remarks>
    /// Only reached once the wanted button was not found, so a composer the
    /// user is actively typing into is left alone in the normal case: the
    /// button is present and this never runs. Returns whether anything was
    /// cleared, and stays silent on failure -- the caller then reports the
    /// missing button, which is the more useful diagnosis.
    /// </remarks>
    private static bool TryClearComposer(AutomationElement root)
    {
        try
        {
            AutomationElement composer = root.FindFirst(
                TreeScope.Descendants,
                new PropertyCondition(
                    AutomationElement.AutomationIdProperty,
                    ComposerAutomationId));
            if (composer is null)
            {
                return false;
            }
            if (
                !composer.TryGetCurrentPattern(
                    ValuePattern.Pattern,
                    out object? pattern)
                || pattern is not ValuePattern value
                || value.Current.IsReadOnly
            )
            {
                return false;
            }
            value.SetValue(string.Empty);
            // The button swap is a re-render, not an immediate property change.
            Thread.Sleep(ComposerClearSettleDelay);
            return true;
        }
        catch (Exception exception) when (
            exception is ElementNotAvailableException
            or InvalidOperationException
            or COMException)
        {
            return false;
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
