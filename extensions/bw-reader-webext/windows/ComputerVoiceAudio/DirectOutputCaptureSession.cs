namespace BwReader.ComputerVoiceAudio;

// A small common owner for the two supported output sources.  Legacy /4 and
// /5 keep process-loopback for rollback compatibility; fixed-bus /6 captures
// the dedicated B cable directly and never discovers a Chromium AudioService
// process.
internal sealed class DirectOutputCaptureSession : IAsyncDisposable
{
    private readonly ProcessLoopbackCaptureSession? _process;
    private readonly ExplicitMicrophoneCaptureSession? _endpoint;

    private DirectOutputCaptureSession(
        ProcessLoopbackCaptureSession? process,
        ExplicitMicrophoneCaptureSession? endpoint)
    {
        if ((process is null) == (endpoint is null))
        {
            throw new ArgumentException(
                "Exactly one direct output capture source is required.");
        }
        _process = process;
        _endpoint = endpoint;
    }

    internal CaptureSessionState State =>
        _endpoint?.State ?? _process!.State;

    internal Task Completion =>
        _endpoint?.Completion ?? _process!.Completion;

    internal PcmAudioFormat? Format =>
        _endpoint?.Format ?? _process!.Format;

    internal static DirectOutputCaptureSession Prepare(
        DirectMediaStartRequest request,
        IBoundedPcmSink sink)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(sink);

        if (request.FixedVirtualAudioBus)
        {
            return new DirectOutputCaptureSession(
                process: null,
                endpoint: ExplicitMicrophoneCaptureSession.Prepare(
                    MicCaptureRequest.Create(
                        request.VirtualSpeakerCaptureEndpointId),
                    sink));
        }
        return new DirectOutputCaptureSession(
            process: ProcessLoopbackCaptureSession.Prepare(
                request.RootProcessId,
                sink),
            endpoint: null);
    }

    internal Task StartAsync(
        CancellationToken cancellationToken = default) =>
        _endpoint?.StartAsync(cancellationToken)
        ?? _process!.StartAsync(cancellationToken);

    internal Task StopAsync(
        CancellationToken cancellationToken = default) =>
        _endpoint?.StopAsync(cancellationToken)
        ?? _process!.StopAsync(cancellationToken);

    public ValueTask DisposeAsync() =>
        _endpoint is not null
            ? _endpoint.DisposeAsync()
            : _process!.DisposeAsync();
}
